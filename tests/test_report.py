"""Stretch report: open and overdue counts by tier.

Overdue means an open or assigned claim that claims.is_overdue says is past its
deadline. Those are the claims escalation would act on. Exactly at the deadline
is not overdue. Resolved, closed, and already escalated claims are not counted
as overdue.
"""

from datetime import datetime, timedelta

import pytest

import claims
import db
from conftest import ACTOR, NOW

ONE_MINUTE = timedelta(minutes=1)
REPORT_NOW = datetime(2026, 11, 2, 12, 0, 0)

# Same column layout as the list command: two spaces, padded, trailing space stripped.
AT_DEADLINE_REPORT = (
    "TIER      OPEN  OVERDUE\n"
    "standard  1     0\n"
    "silver    1     0\n"
    "gold      2     1\n"
)
MINUTE_LATER_REPORT = (
    "TIER      OPEN  OVERDUE\n"
    "standard  1     0\n"
    "silver    1     0\n"
    "gold      2     2\n"
)


def _file(conn, passenger, tier, filed_at):
    return claims.file_claim(
        conn, passenger, "OA101", "lost", tier, None, now=filed_at, actor=ACTOR
    )


def _filed_at(tier, deadline):
    return deadline - timedelta(hours=claims.TIER_HOURS[tier])


def _deadline(conn, claim_id):
    text = conn.execute("SELECT deadline FROM claims WHERE id = ?", (claim_id,)).fetchone()[0]
    return datetime.fromisoformat(text)


def _status(conn, claim_id):
    return conn.execute("SELECT status FROM claims WHERE id = ?", (claim_id,)).fetchone()[0]


def test_report_counts_open_claims_by_tier(conn):
    """Open counts are status 'open' only, with a zero for a tier that has none."""
    _file(conn, "Gold Open A", "gold", NOW)
    _file(conn, "Gold Open B", "gold", NOW)
    _file(conn, "Silver Open", "silver", NOW)
    assigned = _file(conn, "Gold Assigned", "gold", NOW)
    claims.assign_claim(conn, assigned, "Ravi", now=NOW + timedelta(minutes=5), actor=ACTOR)
    resolved = _file(conn, "Standard Resolved", "standard", NOW)
    claims.assign_claim(conn, resolved, "Ravi", now=NOW + timedelta(minutes=5), actor=ACTOR)
    claims.resolve_claim(conn, resolved, now=NOW + timedelta(minutes=10), actor=ACTOR)

    counts = claims.report_counts(conn, now=NOW)

    assert counts == {
        "standard": {"open": 0, "overdue": 0},
        "silver": {"open": 1, "overdue": 0},
        "gold": {"open": 2, "overdue": 0},
    }


def test_report_counts_overdue_follows_is_overdue(conn):
    """Fixed now: exactly at the deadline is not overdue; one minute later is.

    Overdue also includes an assigned claim past its deadline, and skips a
    resolved claim and an already escalated claim.
    """
    escalated = _file(conn, "Escalated Silver", "silver", _filed_at("silver", REPORT_NOW - timedelta(hours=1)))
    claims.escalate_overdue(conn, now=REPORT_NOW - timedelta(minutes=30), actor="system")

    resolved = _file(conn, "Resolved Gold", "gold", _filed_at("gold", REPORT_NOW - timedelta(hours=2)))
    claims.assign_claim(conn, resolved, "Ravi", now=REPORT_NOW - timedelta(hours=1, minutes=30), actor=ACTOR)
    claims.resolve_claim(conn, resolved, now=REPORT_NOW - timedelta(hours=1), actor=ACTOR)

    assigned_past = _file(conn, "Assigned Gold", "gold", _filed_at("gold", REPORT_NOW - timedelta(hours=1)))
    claims.assign_claim(conn, assigned_past, "Ravi", now=REPORT_NOW - timedelta(minutes=30), actor=ACTOR)

    assigned_exact = _file(conn, "Assigned Silver", "silver", _filed_at("silver", REPORT_NOW))
    claims.assign_claim(conn, assigned_exact, "Lena", now=REPORT_NOW - timedelta(hours=1), actor=ACTOR)

    exact = _file(conn, "Exact Gold", "gold", _filed_at("gold", REPORT_NOW))
    minute_past = _file(conn, "Minute Past Gold", "gold", _filed_at("gold", REPORT_NOW - ONE_MINUTE))
    _file(conn, "Future Standard", "standard", REPORT_NOW)

    assert claims.is_overdue(_deadline(conn, exact), REPORT_NOW) is False
    assert claims.is_overdue(_deadline(conn, exact), REPORT_NOW + ONE_MINUTE) is True
    assert claims.is_overdue(_deadline(conn, minute_past), REPORT_NOW) is True
    assert claims.is_overdue(_deadline(conn, assigned_exact), REPORT_NOW) is False
    assert claims.is_overdue(_deadline(conn, assigned_exact), REPORT_NOW + ONE_MINUTE) is True
    assert claims.is_overdue(_deadline(conn, assigned_past), REPORT_NOW) is True
    assert _status(conn, escalated) == "escalated"
    assert _status(conn, resolved) == "resolved"
    assert _status(conn, assigned_past) == "assigned"

    at_deadline = claims.report_counts(conn, now=REPORT_NOW)
    assert at_deadline == {
        "standard": {"open": 1, "overdue": 0},
        "silver": {"open": 0, "overdue": 0},
        "gold": {"open": 2, "overdue": 2},
    }

    one_minute_later = claims.report_counts(conn, now=REPORT_NOW + ONE_MINUTE)
    assert one_minute_later == {
        "standard": {"open": 1, "overdue": 0},
        "silver": {"open": 0, "overdue": 1},
        "gold": {"open": 2, "overdue": 3},
    }


@pytest.fixture
def db_path(tmp_path):
    """A path for a fresh database file. main() creates the tables on first use."""
    return tmp_path / "report.db"


@pytest.fixture
def run(db_path, capsys):
    """Run main() against the test database. Returns (exit code, stdout, stderr)."""

    def _run(*argv):
        import main

        code = main.main(list(argv), db_path=db_path)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def _cli_claims(db_path):
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT * FROM claims ORDER BY id").fetchall()
    finally:
        conn.close()


def _file_cli(run, now, passenger, tier):
    code, _, err = run(
        "--now", now,
        "file",
        "--passenger", passenger,
        "--flight", "OA101",
        "--type", "lost",
        "--tier", tier,
    )
    assert code == 0, err


def test_cli_report_prints_counts_for_now(run, db_path):
    """python main.py --now ... report prints open and overdue counts for every tier."""
    _file_cli(run, "2026-11-02T08:00:00", "Asha Rao", "gold")
    _file_cli(run, "2026-11-02T07:59:00", "Ben Cole", "gold")
    _file_cli(run, "2026-11-02T08:00:00", "Mei Lin", "silver")
    _file_cli(run, "2026-11-02T08:00:00", "Priya Nair", "standard")

    by_name = {row["passenger"]: row for row in _cli_claims(db_path)}
    asha = datetime.fromisoformat(by_name["Asha Rao"]["deadline"])
    ben = datetime.fromisoformat(by_name["Ben Cole"]["deadline"])
    at_deadline = datetime.fromisoformat("2026-11-02T12:00:00")
    minute_later = datetime.fromisoformat("2026-11-02T12:01:00")
    assert claims.is_overdue(asha, at_deadline) is False
    assert claims.is_overdue(ben, at_deadline) is True
    assert claims.is_overdue(asha, minute_later) is True

    conn = db.connect(db_path)
    try:
        assert claims.report_counts(conn, now=at_deadline) == {
            "standard": {"open": 1, "overdue": 0},
            "silver": {"open": 1, "overdue": 0},
            "gold": {"open": 2, "overdue": 1},
        }
        assert claims.report_counts(conn, now=minute_later)["gold"] == {"open": 2, "overdue": 2}
    finally:
        conn.close()

    code, out, err = run("--now", "2026-11-02T12:00:00", "report")
    assert code == 0, err
    assert out == AT_DEADLINE_REPORT

    code, out, err = run("--now", "2026-11-02T12:01:00", "report")
    assert code == 0, err
    assert out == MINUTE_LATER_REPORT
