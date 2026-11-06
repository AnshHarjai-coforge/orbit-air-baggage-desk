"""Branch 3 (feature/deadlines-and-escalation): overdue rule, escalation, rollback."""

import sqlite3
from datetime import timedelta

import pytest

import claims
import db
from conftest import ACTOR, NOW

ESCALATOR = "system"
ONE_MINUTE = timedelta(minutes=1)

# A fixed deadline for the pure is_overdue tests.
DEADLINE = NOW + timedelta(hours=4)


def _file(conn, passenger="Asha Rao", tier="gold", filed_at=NOW):
    return claims.file_claim(conn, passenger, "OA101", "lost", tier, None, now=filed_at, actor=ACTOR)


def _claim_row(conn, claim_id):
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def _history_rows(conn, claim_id):
    return conn.execute(
        "SELECT * FROM history WHERE claim_id = ? ORDER BY changed_at, id", (claim_id,)
    ).fetchall()


def _history_count(conn):
    return conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]


def _snapshot(conn, claim_id):
    return dict(_claim_row(conn, claim_id)), len(_history_rows(conn, claim_id))


def _db_path(conn):
    return conn.execute("PRAGMA database_list").fetchone()["file"]


# 1
def test_not_overdue_one_minute_before_deadline():
    assert claims.is_overdue(DEADLINE, DEADLINE - ONE_MINUTE) is False


# 2
def test_not_overdue_exactly_at_deadline():
    assert claims.is_overdue(DEADLINE, DEADLINE) is False


# 3
def test_overdue_one_minute_after_deadline():
    assert claims.is_overdue(DEADLINE, DEADLINE + ONE_MINUTE) is True


# 4
def test_escalate_overdue_open_and_assigned_claims(conn):
    open_gold = _file(conn, passenger="A", tier="gold")  # deadline NOW + 4h
    assigned_silver = _file(conn, passenger="B", tier="silver")  # deadline NOW + 24h
    claims.assign_claim(conn, assigned_silver, "Ravi", now=NOW + timedelta(hours=1), actor=ACTOR)
    escalate_at = NOW + timedelta(hours=24) + ONE_MINUTE

    escalated = claims.escalate_overdue(conn, now=escalate_at, actor=ESCALATOR)

    assert sorted(escalated) == sorted([open_gold, assigned_silver])
    assert _claim_row(conn, open_gold)["status"] == "escalated"
    assert _claim_row(conn, assigned_silver)["status"] == "escalated"
    # A4: escalation keeps the existing agent (none for the open claim).
    assert _claim_row(conn, open_gold)["assigned_agent"] is None
    assert _claim_row(conn, assigned_silver)["assigned_agent"] == "Ravi"


# 5
def test_escalate_leaves_other_claims_alone(conn):
    early = NOW - timedelta(hours=10)  # gold deadline NOW - 6h, long past at escalate time
    resolved_id = _file(conn, passenger="Resolved", filed_at=early)
    claims.assign_claim(conn, resolved_id, "Ravi", now=early + timedelta(hours=1), actor=ACTOR)
    claims.resolve_claim(conn, resolved_id, now=early + timedelta(hours=2), actor=ACTOR)

    closed_id = _file(conn, passenger="Closed", filed_at=early)
    claims.assign_claim(conn, closed_id, "Ravi", now=early + timedelta(hours=1), actor=ACTOR)
    claims.resolve_claim(conn, closed_id, now=early + timedelta(hours=2), actor=ACTOR)
    claims.close_claim(conn, closed_id, now=early + timedelta(hours=3), actor=ACTOR)

    not_yet_overdue_id = _file(conn, passenger="Standard", tier="standard")  # deadline NOW + 72h
    at_deadline_id = _file(conn, passenger="AtDeadline", tier="gold")  # deadline NOW + 4h
    escalate_at = NOW + timedelta(hours=4)  # exactly at_deadline_id's deadline

    ids = [resolved_id, closed_id, not_yet_overdue_id, at_deadline_id]
    before = {claim_id: _snapshot(conn, claim_id) for claim_id in ids}

    escalated = claims.escalate_overdue(conn, now=escalate_at, actor=ESCALATOR)

    assert escalated == []
    for claim_id in ids:
        assert _snapshot(conn, claim_id) == before[claim_id]


# 6
def test_escalation_writes_history_entry(conn):
    open_id = _file(conn, passenger="A")
    assigned_id = _file(conn, passenger="B")
    claims.assign_claim(conn, assigned_id, "Ravi", now=NOW + timedelta(hours=1), actor=ACTOR)
    escalate_at = NOW + timedelta(hours=5)

    claims.escalate_overdue(conn, now=escalate_at, actor=ESCALATOR)

    for claim_id, old_status in ((open_id, "open"), (assigned_id, "assigned")):
        last = _history_rows(conn, claim_id)[-1]
        assert last["old_status"] == old_status
        assert last["new_status"] == "escalated"
        assert last["changed_by"] == ESCALATOR
        assert last["changed_at"] == escalate_at.isoformat()
    assert len(_history_rows(conn, open_id)) == 2
    assert len(_history_rows(conn, assigned_id)) == 3


# 7
def test_escalate_with_nothing_overdue_returns_empty(conn):
    gold_id = _file(conn, passenger="A", tier="gold")
    silver_id = _file(conn, passenger="B", tier="silver")
    before = {claim_id: _snapshot(conn, claim_id) for claim_id in (gold_id, silver_id)}

    escalated = claims.escalate_overdue(conn, now=NOW + timedelta(hours=1), actor=ESCALATOR)

    assert escalated == []
    for claim_id in (gold_id, silver_id):
        assert _snapshot(conn, claim_id) == before[claim_id]


# 8
def test_already_escalated_claim_not_escalated_again(conn):
    claim_id = _file(conn)
    first_run = NOW + timedelta(hours=5)
    assert claims.escalate_overdue(conn, now=first_run, actor=ESCALATOR) == [claim_id]
    before = _snapshot(conn, claim_id)

    second_run = NOW + timedelta(hours=6)
    escalated = claims.escalate_overdue(conn, now=second_run, actor=ESCALATOR)

    assert escalated == []
    assert _snapshot(conn, claim_id) == before
    assert before[0]["status"] == "escalated"


# 9
def test_failed_history_write_rolls_back_claim_change(conn):
    """F6/T5 and A5: a failed history insert leaves every claim in the run unchanged.

    The trigger fails only on the second claim's history row, so the first claim's update and
    history insert have already run. They must be rolled back too, because one escalate run is
    one transaction.
    """
    first_id = _file(conn, passenger="A")
    second_id = _file(conn, passenger="B")
    with conn:
        conn.execute(
            "CREATE TRIGGER fail_history BEFORE INSERT ON history "
            f"WHEN NEW.claim_id = {second_id} "
            "BEGIN SELECT RAISE(ABORT, 'forced history failure'); END"
        )
    history_before = _history_count(conn)

    with pytest.raises(sqlite3.IntegrityError, match="forced history failure"):
        claims.escalate_overdue(conn, now=NOW + timedelta(hours=5), actor=ESCALATOR)

    fresh = db.connect(_db_path(conn))
    try:
        assert _claim_row(fresh, first_id)["status"] == "open"
        assert _claim_row(fresh, second_id)["status"] == "open"
        assert _history_count(fresh) == history_before
    finally:
        fresh.close()
