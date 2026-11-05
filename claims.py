"""Business rules for baggage claims.

No function here reads the system clock: the current time is always passed in as `now`.
"""

from datetime import timedelta

TIER_HOURS = {"gold": 4, "silver": 24, "standard": 72}

CLAIM_TYPES = ("delayed", "lost", "damaged")
TIERS = ("standard", "silver", "gold")
STATUSES = ("open", "assigned", "escalated", "resolved", "closed")


class ClaimError(Exception):
    """A claim was rejected by a business rule or failed validation."""


def _timestamp(moment):
    """Store times as ISO 8601 text to the second, e.g. 2026-11-02T14:00:00."""
    return moment.isoformat(timespec="seconds")


def _require_text(field, value):
    if not isinstance(value, str) or not value.strip():
        raise ClaimError(f"{field} is required and cannot be blank")
    return value.strip()


def _require_choice(field, value, allowed):
    if value not in allowed:
        raise ClaimError(f"Unknown {field} {value!r}. Allowed: {', '.join(allowed)}")
    return value


def compute_deadline(filed_at, tier):
    """Return the response deadline: the filed time plus the tier's hours."""
    _require_choice("tier", tier, TIERS)
    return filed_at + timedelta(hours=TIER_HOURS[tier])


def file_claim(conn, passenger, flight, claim_type, tier, note, *, now, actor):
    """Validate and file a new claim as 'open'. Returns the new claim id.

    The claim row and its filing history entry are written in one transaction.
    """
    passenger = _require_text("passenger", passenger)
    flight = _require_text("flight", flight)
    _require_choice("type", claim_type, CLAIM_TYPES)
    _require_choice("tier", tier, TIERS)
    actor = _require_text("actor", actor)
    if note is not None and not note.strip():
        note = None

    filed_at = _timestamp(now)
    deadline = _timestamp(compute_deadline(now, tier))

    with conn:
        cursor = conn.execute(
            "INSERT INTO claims (passenger, flight, type, tier, status, note, filed_at, deadline) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
            (passenger, flight, claim_type, tier, note, filed_at, deadline),
        )
        claim_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO history (claim_id, old_status, new_status, changed_by, changed_at) "
            "VALUES (?, NULL, 'open', ?, ?)",
            (claim_id, actor, filed_at),
        )
    return claim_id


def list_claims(conn, *, status=None, tier=None):
    """Return claims ordered by id, optionally filtered by status and/or tier."""
    conditions = []
    params = []
    if status is not None:
        conditions.append("status = ?")
        params.append(_require_choice("status", status, STATUSES))
    if tier is not None:
        conditions.append("tier = ?")
        params.append(_require_choice("tier", tier, TIERS))

    sql = "SELECT * FROM claims"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id"
    return conn.execute(sql, params).fetchall()
