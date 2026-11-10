"""Business rules for baggage claims.

No function here reads the system clock: the current time is always passed in as `now`.
"""

from datetime import datetime, timedelta

TIER_HOURS = {"gold": 4, "silver": 24, "standard": 72}

CLAIM_TYPES = ("delayed", "lost", "damaged")
TIERS = ("standard", "silver", "gold")
STATUSES = ("open", "assigned", "escalated", "resolved", "closed")

# The only status changes allowed. Every status-changing command checks against this table.
ALLOWED_TRANSITIONS = {
    "open": {"assigned", "escalated"},
    "assigned": {"resolved", "escalated"},
    "escalated": {"assigned", "resolved"},
    "resolved": {"closed"},
    "closed": set(),
}


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


def validate_claim(passenger, flight, claim_type, tier, note):
    """Apply the filing rules without saving anything.

    Returns the cleaned values (passenger, flight, claim_type, tier, note), or raises ClaimError.
    """
    passenger = _require_text("passenger", passenger)
    flight = _require_text("flight", flight)
    _require_choice("type", claim_type, CLAIM_TYPES)
    _require_choice("tier", tier, TIERS)
    if note is not None and not note.strip():
        note = None
    return passenger, flight, claim_type, tier, note


def _insert_claim(conn, claim, *, now, actor):
    """Insert one validated claim and its filing history entry. It does not commit."""
    passenger, flight, claim_type, tier, note = claim
    filed_at = _timestamp(now)
    deadline = _timestamp(compute_deadline(now, tier))
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


def file_claim(conn, passenger, flight, claim_type, tier, note, *, now, actor):
    """Validate and file a new claim as 'open'. Returns the new claim id.

    The claim row and its filing history entry are written in one transaction.
    """
    claim = validate_claim(passenger, flight, claim_type, tier, note)
    actor = _require_text("actor", actor)
    with conn:
        return _insert_claim(conn, claim, now=now, actor=actor)


def file_claims(conn, claims_to_file, *, now, actor):
    """File several claims all-or-nothing. Returns the new claim ids in order.

    Each item is (passenger, flight, claim_type, tier, note). Every item is validated before
    anything is written, and all inserts share one transaction (A6).
    """
    validated = [validate_claim(*claim) for claim in claims_to_file]
    actor = _require_text("actor", actor)
    with conn:
        return [_insert_claim(conn, claim, now=now, actor=actor) for claim in validated]


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


def get_claim(conn, claim_id):
    """Return one claim row, or raise ClaimError if it does not exist."""
    row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        raise ClaimError(f"Claim {claim_id} not found")
    return row


def get_history(conn, claim_id):
    """Return a claim's history rows, oldest first. An unknown claim id gives an empty list."""
    return conn.execute(
        "SELECT * FROM history WHERE claim_id = ? ORDER BY changed_at, id", (claim_id,)
    ).fetchall()


def is_overdue(deadline, now):
    """A claim is overdue only when now is strictly later than its deadline."""
    return now > deadline


def _apply_status_change(conn, claim_id, new_status, *, now, actor, agent=None):
    """The single place a claim's status is written. It does not commit.

    Checks the change against ALLOWED_TRANSITIONS (and, for escalation, that the claim is
    overdue), then updates the claim and inserts its history row. Callers run it inside a
    transaction. The claim update runs first, so if the history insert fails the rollback is
    what keeps the claim unchanged.
    """
    claim = get_claim(conn, claim_id)
    old_status = claim["status"]
    if new_status not in ALLOWED_TRANSITIONS[old_status]:
        raise ClaimError(f"Cannot change claim {claim_id} from {old_status!r} to {new_status!r}")
    if new_status == "escalated" and not is_overdue(datetime.fromisoformat(claim["deadline"]), now):
        raise ClaimError(f"Cannot escalate claim {claim_id}: it is not overdue")

    if agent is None:
        conn.execute("UPDATE claims SET status = ? WHERE id = ?", (new_status, claim_id))
    else:
        conn.execute(
            "UPDATE claims SET status = ?, assigned_agent = ? WHERE id = ?",
            (new_status, agent, claim_id),
        )
    conn.execute(
        "INSERT INTO history (claim_id, old_status, new_status, changed_by, changed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (claim_id, old_status, new_status, actor, _timestamp(now)),
    )


def _change_status(conn, claim_id, new_status, *, now, actor, agent=None):
    """Change one claim's status and write its history row in one transaction."""
    actor = _require_text("actor", actor)
    with conn:
        _apply_status_change(conn, claim_id, new_status, now=now, actor=actor, agent=agent)


def assign_claim(conn, claim_id, agent, *, now, actor):
    """Assign a claim to an agent (open -> assigned, or reassign escalated -> assigned)."""
    agent = _require_text("agent", agent)
    _change_status(conn, claim_id, "assigned", now=now, actor=actor, agent=agent)


def resolve_claim(conn, claim_id, *, now, actor):
    """Mark a claim resolved (from assigned or escalated)."""
    _change_status(conn, claim_id, "resolved", now=now, actor=actor)


def close_claim(conn, claim_id, *, now, actor):
    """Close a resolved claim. Closed is terminal."""
    _change_status(conn, claim_id, "closed", now=now, actor=actor)


def escalate_overdue(conn, *, now, actor):
    """Escalate every overdue 'open' or 'assigned' claim. Returns the escalated ids.

    The whole run is one transaction (A5): either every overdue claim and its history row is
    saved, or none is. Escalation keeps the claim's existing agent (A4).
    """
    actor = _require_text("actor", actor)
    candidates = conn.execute(
        "SELECT id, deadline FROM claims WHERE status IN ('open', 'assigned') ORDER BY id"
    ).fetchall()
    overdue_ids = [
        row["id"] for row in candidates if is_overdue(datetime.fromisoformat(row["deadline"]), now)
    ]

    with conn:
        for claim_id in overdue_ids:
            _apply_status_change(conn, claim_id, "escalated", now=now, actor=actor)
    return overdue_ids
