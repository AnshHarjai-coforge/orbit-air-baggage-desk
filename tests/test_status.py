"""Branch 2 (feature/assign-and-status): assign, resolve, close, allowed changes, history."""

from datetime import timedelta

import pytest

import claims
from conftest import ACTOR, NOW

# Later moments for status changes, so history rows have distinct, ordered times.
T1 = NOW + timedelta(hours=1)
T2 = NOW + timedelta(hours=2)
T3 = NOW + timedelta(hours=3)


def _file(conn, passenger="Asha Rao", tier="standard"):
    return claims.file_claim(conn, passenger, "OA101", "lost", tier, None, now=NOW, actor=ACTOR)


def _make_escalated(conn, claim_id, agent=None):
    """Setup only: force a claim into 'escalated' with direct SQL.

    The real escalation path arrives on branch 3, so this writes no history row.
    """
    with conn:
        conn.execute(
            "UPDATE claims SET status = 'escalated', assigned_agent = ? WHERE id = ?",
            (agent, claim_id),
        )


def _claim_row(conn, claim_id):
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def _history_rows(conn, claim_id):
    return conn.execute(
        "SELECT * FROM history WHERE claim_id = ? ORDER BY changed_at, id", (claim_id,)
    ).fetchall()


def _assert_last_history(conn, claim_id, old, new, at):
    last = _history_rows(conn, claim_id)[-1]
    assert last["old_status"] == old
    assert last["new_status"] == new
    assert last["changed_by"] == ACTOR
    assert last["changed_at"] == at.isoformat()


def _assert_unchanged(conn, claim_id, before_row, before_history_count):
    assert dict(_claim_row(conn, claim_id)) == dict(before_row)
    assert len(_history_rows(conn, claim_id)) == before_history_count


# 1
def test_assign_open_claim(conn):
    claim_id = _file(conn)

    claims.assign_claim(conn, claim_id, "Ravi", now=T1, actor=ACTOR)

    row = claims.get_claim(conn, claim_id)
    assert row["status"] == "assigned"
    assert row["assigned_agent"] == "Ravi"
    assert len(_history_rows(conn, claim_id)) == 2
    _assert_last_history(conn, claim_id, "open", "assigned", T1)


# 2
def test_assign_escalated_claim_reassigns(conn):
    claim_id = _file(conn)
    _make_escalated(conn, claim_id, agent="Ravi")

    claims.assign_claim(conn, claim_id, "Meera", now=T1, actor=ACTOR)

    row = _claim_row(conn, claim_id)
    assert row["status"] == "assigned"
    assert row["assigned_agent"] == "Meera"
    _assert_last_history(conn, claim_id, "escalated", "assigned", T1)


# 3
def test_assign_requires_agent_name(conn):
    claim_id = _file(conn)
    before = _claim_row(conn, claim_id)

    for blank in ("", "   "):
        with pytest.raises(claims.ClaimError, match="agent"):
            claims.assign_claim(conn, claim_id, blank, now=T1, actor=ACTOR)

    _assert_unchanged(conn, claim_id, before, 1)


# 4
def test_resolve_assigned_and_escalated_claims(conn):
    assigned_id = _file(conn, passenger="A")
    claims.assign_claim(conn, assigned_id, "Ravi", now=T1, actor=ACTOR)
    escalated_id = _file(conn, passenger="B")
    _make_escalated(conn, escalated_id, agent="Ravi")

    claims.resolve_claim(conn, assigned_id, now=T2, actor=ACTOR)
    claims.resolve_claim(conn, escalated_id, now=T2, actor=ACTOR)

    assert _claim_row(conn, assigned_id)["status"] == "resolved"
    _assert_last_history(conn, assigned_id, "assigned", "resolved", T2)
    assert _claim_row(conn, escalated_id)["status"] == "resolved"
    _assert_last_history(conn, escalated_id, "escalated", "resolved", T2)


# 5
def test_close_resolved_claim(conn):
    claim_id = _file(conn)
    claims.assign_claim(conn, claim_id, "Ravi", now=T1, actor=ACTOR)
    claims.resolve_claim(conn, claim_id, now=T2, actor=ACTOR)

    claims.close_claim(conn, claim_id, now=T3, actor=ACTOR)

    assert _claim_row(conn, claim_id)["status"] == "closed"
    _assert_last_history(conn, claim_id, "resolved", "closed", T3)


# 6
def test_disallowed_changes_from_open_rejected(conn):
    claim_id = _file(conn)
    before = _claim_row(conn, claim_id)

    with pytest.raises(claims.ClaimError, match="'open' to 'closed'"):
        claims.close_claim(conn, claim_id, now=T1, actor=ACTOR)
    with pytest.raises(claims.ClaimError, match="'open' to 'resolved'"):
        claims.resolve_claim(conn, claim_id, now=T1, actor=ACTOR)

    assert _claim_row(conn, claim_id)["status"] == "open"
    _assert_unchanged(conn, claim_id, before, 1)


# 7
def test_disallowed_changes_from_assigned_and_resolved_rejected(conn):
    assigned_id = _file(conn, passenger="A")
    claims.assign_claim(conn, assigned_id, "Ravi", now=T1, actor=ACTOR)
    assigned_before = _claim_row(conn, assigned_id)

    with pytest.raises(claims.ClaimError, match="'assigned' to 'assigned'"):
        claims.assign_claim(conn, assigned_id, "Meera", now=T2, actor=ACTOR)
    _assert_unchanged(conn, assigned_id, assigned_before, 2)

    resolved_id = _file(conn, passenger="B")
    claims.assign_claim(conn, resolved_id, "Ravi", now=T1, actor=ACTOR)
    claims.resolve_claim(conn, resolved_id, now=T2, actor=ACTOR)
    resolved_before = _claim_row(conn, resolved_id)

    with pytest.raises(claims.ClaimError, match="'resolved' to 'assigned'"):
        claims.assign_claim(conn, resolved_id, "Meera", now=T3, actor=ACTOR)
    _assert_unchanged(conn, resolved_id, resolved_before, 3)


# 8
def test_closed_claim_cannot_change(conn):
    claim_id = _file(conn)
    claims.assign_claim(conn, claim_id, "Ravi", now=T1, actor=ACTOR)
    claims.resolve_claim(conn, claim_id, now=T2, actor=ACTOR)
    claims.close_claim(conn, claim_id, now=T3, actor=ACTOR)
    before = _claim_row(conn, claim_id)
    later = T3 + timedelta(hours=1)

    with pytest.raises(claims.ClaimError, match="'closed' to 'assigned'"):
        claims.assign_claim(conn, claim_id, "Meera", now=later, actor=ACTOR)
    with pytest.raises(claims.ClaimError, match="'closed' to 'resolved'"):
        claims.resolve_claim(conn, claim_id, now=later, actor=ACTOR)
    with pytest.raises(claims.ClaimError, match="'closed' to 'closed'"):
        claims.close_claim(conn, claim_id, now=later, actor=ACTOR)

    _assert_unchanged(conn, claim_id, before, 4)


# 9
def test_full_lifecycle_history_is_complete_and_ordered(conn):
    claim_id = _file(conn)
    claims.assign_claim(conn, claim_id, "Ravi", now=T1, actor="lead")
    claims.resolve_claim(conn, claim_id, now=T2, actor="Ravi")
    claims.close_claim(conn, claim_id, now=T3, actor="lead")

    history = claims.get_history(conn, claim_id)

    assert [(h["old_status"], h["new_status"], h["changed_by"], h["changed_at"]) for h in history] == [
        (None, "open", ACTOR, NOW.isoformat()),
        ("open", "assigned", "lead", T1.isoformat()),
        ("assigned", "resolved", "Ravi", T2.isoformat()),
        ("resolved", "closed", "lead", T3.isoformat()),
    ]
    for entry in history[1:]:
        assert all(entry[field] for field in ("old_status", "new_status", "changed_by", "changed_at"))
