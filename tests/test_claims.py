"""Branch 1 (feature/schema-and-filing): tables, filing, validation, deadlines, listing."""

import sqlite3
from datetime import timedelta

import pytest

import claims
import db
from conftest import ACTOR, NOW


def _file(conn, passenger="Asha Rao", flight="OA101", claim_type="lost", tier="standard",
          note=None):
    return claims.file_claim(conn, passenger, flight, claim_type, tier, note, now=NOW, actor=ACTOR)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _raw_insert(conn, claim_type="lost", tier="standard", status="open"):
    conn.execute(
        "INSERT INTO claims (passenger, flight, type, tier, status, filed_at, deadline) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Raw Row", "OA999", claim_type, tier, status, NOW.isoformat(), NOW.isoformat()),
    )


# 1
def test_init_db_creates_claims_and_history_tables(conn):
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"claims", "history"} <= tables

    # Running it again must not error.
    db.init_db(conn)


# 2
def test_file_claim_stores_fields_and_starts_open(conn):
    claim_id = _file(
        conn, passenger="  Asha Rao  ", flight="  OA101 ", claim_type="damaged",
        tier="silver", note="Blue suitcase, red ribbon",
    )
    assert isinstance(claim_id, int)

    row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    assert row["passenger"] == "Asha Rao"
    assert row["flight"] == "OA101"
    assert row["type"] == "damaged"
    assert row["tier"] == "silver"
    assert row["status"] == "open"
    assert row["assigned_agent"] is None
    assert row["note"] == "Blue suitcase, red ribbon"
    assert row["filed_at"] == NOW.isoformat()


# 3
def test_file_claim_writes_initial_history_entry(conn):
    claim_id = _file(conn)

    rows = conn.execute("SELECT * FROM history WHERE claim_id = ?", (claim_id,)).fetchall()
    assert len(rows) == 1
    entry = rows[0]
    assert entry["old_status"] is None
    assert entry["new_status"] == "open"
    assert entry["changed_by"] == ACTOR
    assert entry["changed_at"] == NOW.isoformat()


def _assert_deadline(conn, tier, hours):
    expected = NOW + timedelta(hours=hours)
    assert claims.compute_deadline(NOW, tier) == expected

    claim_id = _file(conn, tier=tier)
    stored = conn.execute("SELECT deadline FROM claims WHERE id = ?", (claim_id,)).fetchone()[0]
    assert stored == expected.isoformat()


# 4
def test_deadline_gold_is_4_hours(conn):
    _assert_deadline(conn, "gold", 4)


# 5
def test_deadline_silver_is_24_hours(conn):
    _assert_deadline(conn, "silver", 24)


# 6
def test_deadline_standard_is_72_hours(conn):
    _assert_deadline(conn, "standard", 72)


# 7
def test_blank_passenger_and_flight_rejected(conn):
    for blank in ("", "   "):
        with pytest.raises(claims.ClaimError, match="passenger"):
            _file(conn, passenger=blank)
        with pytest.raises(claims.ClaimError, match="flight"):
            _file(conn, flight=blank)

    assert _count(conn, "claims") == 0
    assert _count(conn, "history") == 0


# 8
def test_unknown_type_and_tier_rejected(conn):
    with pytest.raises(claims.ClaimError, match="stolen") as type_error:
        _file(conn, claim_type="stolen")
    for allowed in ("delayed", "lost", "damaged"):
        assert allowed in str(type_error.value)

    with pytest.raises(claims.ClaimError, match="platinum") as tier_error:
        _file(conn, tier="platinum")
    for allowed in ("standard", "silver", "gold"):
        assert allowed in str(tier_error.value)

    assert _count(conn, "claims") == 0
    assert _count(conn, "history") == 0


# 9
def test_check_constraints_reject_bad_values_in_db(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(conn, claim_type="stolen")
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(conn, tier="platinum")
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(conn, status="pending")


# 10
def test_list_claims_filters_by_status_and_tier(conn):
    gold_open = _file(conn, passenger="A", tier="gold")
    gold_assigned = _file(conn, passenger="B", tier="gold")
    silver_open = _file(conn, passenger="C", tier="silver")
    standard_assigned = _file(conn, passenger="D", tier="standard")

    # No status-change function exists on this branch, so move two claims directly in SQL.
    with conn:
        conn.execute(
            "UPDATE claims SET status = 'assigned', assigned_agent = 'Ravi' WHERE id IN (?, ?)",
            (gold_assigned, standard_assigned),
        )

    def ids(**filters):
        return {row["id"] for row in claims.list_claims(conn, **filters)}

    assert ids() == {gold_open, gold_assigned, silver_open, standard_assigned}
    assert ids(tier="gold") == {gold_open, gold_assigned}
    assert ids(status="open") == {gold_open, silver_open}
    assert ids(status="open", tier="gold") == {gold_open}

    with pytest.raises(claims.ClaimError, match="pending"):
        claims.list_claims(conn, status="pending")
    with pytest.raises(claims.ClaimError, match="platinum"):
        claims.list_claims(conn, tier="platinum")
