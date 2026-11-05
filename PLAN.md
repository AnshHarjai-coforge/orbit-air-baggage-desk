# PLAN.md — Orbit Air Baggage Desk

This plan was written before any feature code. The source of truth for requirements is
`docs/assignment-reference.md`. Where that document is silent, the decision is recorded in
[Assumptions and Open Decisions](#7-assumptions-and-open-decisions) instead of being invented quietly.

---

## 1. Problem Understanding

Orbit Air's baggage team tracks lost, delayed, and damaged bag claims in spreadsheets and email.
Nothing enforces a response deadline. No one can reliably see who touched a claim or when.
Claims that should have been escalated go unnoticed. The result: passengers wait too long, and the
airline cannot show what happened to a claim.

We will replace that with a small, auditable claims desk: a Python command line app backed by
SQLite. It must:

- **Capture claims consistently.** A claim is only accepted if it has a real passenger and flight
  and a known claim type and tier. Bad input is refused with a clear message, and nothing is saved.
- **Attach a response deadline at filing time.** Higher-tier passengers get faster deadlines.
- **Move claims through a fixed workflow.** A claim can only move along the allowed status paths,
  so it cannot skip from "open" straight to "closed".
- **Keep a complete audit trail.** Every status change, including the initial filing, is recorded
  with the old status, new status, who made the change, and when. A status change can never be
  saved without its audit record.
- **Surface missed deadlines.** One command finds every claim that has passed its deadline while
  still waiting for action and escalates it, reporting which claims it escalated.
- **Be testable against time.** The current time is an input (`--now`), so we can demonstrate and
  test overdue behaviour without waiting days.

Claims are assigned to **human agents**, identified by name. An AI agent is a possible future
extension and is **out of scope**.

### Out of scope (for now)

- AI or automated assignment, notifications, email, web UI, authentication, multi-user locking.
- Deleting or editing claim details after filing.
- Flight-number format validation (the brief requires only "not blank").
- Stretch goals, until every core rubric item is complete (see §8).

---

## 2. Business Rules

### 2.1 Claim fields and validation

| Field | Rule |
|---|---|
| passenger | Required. Rejected if empty or whitespace-only. Stored with surrounding whitespace removed. |
| flight | Required. Rejected if empty or whitespace-only. Stored with surrounding whitespace removed. |
| type | Must be exactly one of `delayed`, `lost`, `damaged`. |
| tier | Must be exactly one of `standard`, `silver`, `gold`. |
| note | Optional free text. Stored as given and treated purely as data (see §6, Risk 5). |
| status | Always `open` at filing. Callers cannot choose it. |
| assigned agent | Empty (NULL) until the claim is assigned. |
| filed time | The `now` value passed in at filing. |
| deadline | Filed time plus the tier's response hours (below). Calculated once at filing and stored. |

Every rejection raises one domain error type (`ClaimError`) with a message that names the bad
field and the allowed values, such as `Unknown tier 'platinum'. Allowed: standard, silver, gold`.
Nothing is written to the database when validation fails.

### 2.2 Deadlines by tier

| Tier | Hours to respond | Example: filed 2026-11-02T08:00:00 |
|---|---:|---|
| gold | 4 | deadline 2026-11-02T12:00:00 |
| silver | 24 | deadline 2026-11-03T08:00:00 |
| standard | 72 | deadline 2026-11-05T08:00:00 |

### 2.3 Overdue rule (strict)

A claim is **overdue only when `now > deadline`**, using a strict comparison.

- One minute before the deadline: **not overdue**.
- **Exactly at the deadline: not overdue.**
- One minute after the deadline: **overdue**.

The comparison uses `datetime` objects, never string comparison. This logic lives in one
function, `is_overdue(deadline, now)`.

### 2.4 Allowed status changes

A claim starts as `open`. Only these changes are allowed:

| From | Allowed to | Via |
|---|---|---|
| open | assigned | `assign` |
| open | escalated | `escalate` (only if overdue) |
| assigned | resolved | `resolve` |
| assigned | escalated | `escalate` (only if overdue) |
| escalated | assigned | `assign` (reassign to an agent) |
| escalated | resolved | `resolve` |
| resolved | closed | `close` |
| closed | nothing | terminal |

Any other change is rejected with a clear `ClaimError` naming the current and requested status,
such as `Cannot change claim 7 from 'open' to 'closed'`. A rejected change writes nothing.
Examples of rejected changes that will be tested:

- open → closed, open → resolved
- assigned → assigned (re-assigning an already-assigned claim)
- resolved → assigned
- closed → anything
- escalated → escalated

The allowed transitions live in one table in `claims.py` (`ALLOWED_TRANSITIONS`). Every command
checks against it. No command has its own private rules.

### 2.5 Escalation conditions

A claim is escalated only if **both** are true:

1. Its status is `open` or `assigned`.
2. It is overdue at the supplied `now` (`now > deadline`).

`resolved`, `closed`, and already-`escalated` claims are never touched, even if they are past
their deadline. Claims that are not yet overdue are never touched. The `escalate` command
escalates every qualifying claim and reports the IDs it escalated, or says that none were.

### 2.6 History

- **Every** status change writes one history row: claim ID, old status, new status, who made the
  change, and when.
- **Filing counts as a change.** Its history row has old status = `NULL` (no previous status) and
  new status = `open`.
- History rows are only ever inserted. They are never updated or deleted.

### 2.7 Atomicity

The claim write (INSERT on filing, UPDATE on a status change) and its history INSERT run in **one
SQLite transaction** (`with conn:`), so either both are saved or neither is. If the history insert
fails, the claim must be unchanged.

So that the rollback test is meaningful, the **claim write happens first and the history insert
second**. If the transaction were removed, a failing history insert would leave a changed claim
behind, and the rollback test would catch it.

### 2.8 Time handling

- **No rule code reads the system clock.** Every function in `claims.py` that depends on time takes
  a required `now: datetime` parameter. It has no default and never calls `datetime.now()`.
- Only `main.py` decides the time. It uses the value of `--now` if given, otherwise the real
  clock, read in exactly one place.
- Timestamps are stored as ISO 8601 text to the second, for example `2026-11-02T14:00:00`. See
  §7 (A1) for the timezone decision.

---

## 3. Database Design (SQLite)

`db.py` opens the connection, runs `PRAGMA foreign_keys = ON` on every connection, sets
`row_factory = sqlite3.Row`, and creates both tables with `CREATE TABLE IF NOT EXISTS`.

### 3.1 `claims`

| Column | Type | Null? | Constraint / notes |
|---|---|---|---|
| `id` | INTEGER | NOT NULL | `PRIMARY KEY AUTOINCREMENT` (the claim ID shown to users) |
| `passenger` | TEXT | NOT NULL | `CHECK (length(trim(passenger)) > 0)` |
| `flight` | TEXT | NOT NULL | `CHECK (length(trim(flight)) > 0)` |
| `type` | TEXT | NOT NULL | `CHECK (type IN ('delayed','lost','damaged'))` |
| `tier` | TEXT | NOT NULL | `CHECK (tier IN ('standard','silver','gold'))` |
| `status` | TEXT | NOT NULL | `DEFAULT 'open'`, `CHECK (status IN ('open','assigned','escalated','resolved','closed'))` |
| `assigned_agent` | TEXT | **NULL** | NULL until assigned. An escalated claim keeps its last agent (A4). |
| `note` | TEXT | NULL | Optional free text; untrusted data |
| `filed_at` | TEXT | NOT NULL | ISO 8601 timestamp |
| `deadline` | TEXT | NOT NULL | ISO 8601 timestamp = `filed_at` + tier hours |

The column is named `type` to match the CSV column and the brief. In Python, the parameter is
named `claim_type` so it does not shadow the built-in `type`.

### 3.2 `history`

| Column | Type | Null? | Constraint / notes |
|---|---|---|---|
| `id` | INTEGER | NOT NULL | `PRIMARY KEY AUTOINCREMENT` |
| `claim_id` | INTEGER | NOT NULL | `FOREIGN KEY REFERENCES claims(id)` |
| `old_status` | TEXT | **NULL** | NULL only for the filing entry. `CHECK (old_status IS NULL OR old_status IN (...5 statuses...))` |
| `new_status` | TEXT | NOT NULL | `CHECK (new_status IN (...5 statuses...))` |
| `changed_by` | TEXT | NOT NULL | `CHECK (length(trim(changed_by)) > 0)` — the person or process that made the change |
| `changed_at` | TEXT | NOT NULL | ISO 8601 timestamp (the `now` passed in) |

Entries for one claim are read back in order: `ORDER BY changed_at, id`.

### 3.3 Proposed `claims.py` interface (tests are written against these names)

```text
ClaimError(Exception)
TIER_HOURS = {"gold": 4, "silver": 24, "standard": 72}
ALLOWED_TRANSITIONS = {...table in §2.4...}

compute_deadline(filed_at, tier) -> datetime
is_overdue(deadline, now) -> bool
file_claim(conn, passenger, flight, claim_type, tier, note, *, now, actor) -> int (claim id)
list_claims(conn, *, status=None, tier=None) -> list of rows
get_claim(conn, claim_id) -> row                  (ClaimError if missing)
assign_claim(conn, claim_id, agent, *, now, actor)
resolve_claim(conn, claim_id, *, now, actor)
close_claim(conn, claim_id, *, now, actor)
escalate_overdue(conn, *, now, actor) -> list of escalated ids
get_history(conn, claim_id) -> list of rows
```

All status changes go through one private helper. It checks the transition, then runs
UPDATE claims and INSERT history inside a single `with conn:` block. There is no second code path
that can change a status.

`db.py`: `connect(path) -> sqlite3.Connection` and `init_db(conn)`.

`main.py`: `main(argv=None, db_path=DEFAULT_DB_PATH) -> int` (exit code). Tests call `main()`
directly with a temporary `db_path`, so no extra CLI flag is needed.

---

## 4. Workflow Rules (apply to every branch)

**Before any feature code (on `main`):**

1. Commit `PLAN.md` on `main`. This commit must come before any commit that touches `claims.py`,
   `db.py`, or `main.py` (rubric P1).
2. Set up the project on `main`. This setup does not include feature code:
   - Update `.gitignore`. It already has `.env`, `.venv`, and `__pycache__/`. Add database files
     (`*.db`, `*.sqlite`, `*.sqlite3`) and `CLAUDE.local.md`.
   - Add `CLAUDE.md` (under 30 lines). It contains the command to run all tests, the command to run
     one test file, the gold/silver/standard deadline hours, and the rule "do not edit files in
     `tests/`". It must not describe the project architecture.
   - Add `.claude/settings.json` with deny rules that stop Claude reading `.env`: the `Read` tool
     on `.env`, **and** Bash commands that would print it (such as `cat`, `head`, `tail`, `less`
     or `grep` on `.env`). Add a Stop hook that runs pytest and exits with code 2 when tests fail.
   - Add `pyproject.toml` (pytest `pythonpath = ["."]`, ruff config) and `requirements-dev.txt`
     (pytest, ruff).
   - Create a virtual environment. Create `.env` with a fake key and confirm it is ignored with
     `git check-ignore .env` **before** any `git add`.
   - Test both deny rules (ask Claude to read `.env`, and to `cat .env`), confirm each is
     blocked, and record the results for the README **Setup Check** section.
   - Create the Linear project **Orbit Air Baggage Desk** with the four issues from §5 and add
     their links to the README **Linear Issues** section.

**On each feature branch, in this order:**

1. Create the branch from an up-to-date `main` using the exact branch name.
2. Write the branch's tests in a **new** test file. Earlier branches' test files are never edited.
3. Run `pytest` and confirm the new tests **fail for the expected reason**: the function or module
   does not exist yet (`AttributeError` / `ModuleNotFoundError`), not a typo or fixture bug.
   Save the red output for the Agent Log. Any new test that already passes is investigated,
   because it is not testing new behaviour.
4. Commit the tests alone: `test(<branch>): add failing tests for ...`.
5. Implement the feature code until all tests pass. Run `ruff check .`. Commit:
   `feat(<branch>): ...`.
6. Check that `git diff <tests-commit>..HEAD -- tests/` is empty. If a test truly had to change,
   it is my decision, explained in the PR description.
7. Read the full diff and run `pytest` and `ruff check .` myself. Write the branch's Agent Log entry.
8. Open a PR whose title starts with the branch name, for example
   `feature/schema-and-filing: tables, filing, validation, list`. The description says what
   changed and how it was checked.
9. Merge with a **merge commit**, not a squash or rebase, so the tests-first commit order stays
   visible on `main` (rubric T3).

**Test conventions:**

- Tests import modules (`import claims`) and call `claims.func(...)`. Before implementation, each
  test then fails on its own with `AttributeError` instead of one collection error.
- Rejection tests use `pytest.raises(claims.ClaimError, match=...)`, never `pytest.raises(Exception)`.
  A broad `Exception` would also catch the `AttributeError` raised before implementation, so the
  test would pass when it should fail.
- Each test uses a fresh SQLite file under `tmp_path`. Time comes from fixed constants such as
  `NOW = datetime(2026, 11, 2, 8, 0, 0)`. No test reads the real clock.
- Per-branch minimums are met with **separate test functions**, not by counting parametrized cases
  (see A9).
- Shared fixtures go in `tests/conftest.py`, created on branch 1. Later branches define any new
  fixtures inside their own test file so earlier files stay untouched.

---

## 5. Implementation Sequence

Planned test count: about 10 + 9 + 9 + 5 = **33 tests**. The required minimum is 20, with
per-branch minimums of 5 / 6 / 6 / 3.

### Branch 1 — `feature/schema-and-filing`

**Scope:** `db.py` (connect, create both tables with CHECK constraints and FK). In `claims.py`:
`ClaimError`, `TIER_HOURS`, validation, `compute_deadline`, `file_claim` (claim INSERT + filing
history row in one transaction), and `list_claims` with optional status and tier filters.
No CLI yet.

**Test file:** `tests/test_claims.py` (plus `tests/conftest.py` fixtures). **Minimum: 5. Planned: 10.**

| # | Test | Scenario and assertion |
|---|---|---|
| 1 | `test_init_db_creates_claims_and_history_tables` | After `init_db`, both tables exist. Running `init_db` a second time does not error. |
| 2 | `test_file_claim_stores_fields_and_starts_open` | Filing a valid claim returns an id. The row has status `open`, `assigned_agent` NULL, the note stored, and `filed_at == NOW`. |
| 3 | `test_file_claim_writes_initial_history_entry` | Exactly one history row: old_status NULL, new_status `open`, `changed_by` = actor, `changed_at` = NOW. |
| 4 | `test_deadline_gold_is_4_hours` | gold filed at NOW → deadline NOW + 4h. |
| 5 | `test_deadline_silver_is_24_hours` | silver → NOW + 24h. |
| 6 | `test_deadline_standard_is_72_hours` | standard → NOW + 72h. |
| 7 | `test_blank_passenger_and_flight_rejected` | `""` and `"   "` passenger, then `""` and `"   "` flight, each raise `ClaimError` naming the field. The claims and history tables stay empty. (May be split into two tests.) |
| 8 | `test_unknown_type_and_tier_rejected` | type `stolen` and tier `platinum` each raise `ClaimError` listing the allowed values. Nothing is saved. |
| 9 | `test_check_constraints_reject_bad_values_in_db` | A raw SQL INSERT with a bad type, a bad tier, and a bad status each raises `sqlite3.IntegrityError`. This proves the CHECK constraints exist (F4). |
| 10 | `test_list_claims_filters_by_status_and_tier` | With a mix of claims: no filter returns all. `tier="gold"` returns only gold. `status="open"` returns only open. Both filters together return their intersection. An unknown filter value raises `ClaimError`. |

**Expected red reason:** `ModuleNotFoundError` for `db` / `claims`.
**Done when:** all tests pass, `ruff check .` is clean, and the PR is merged with a merge commit.

**Linear issue — `feature/schema-and-filing: tables, filing, validation, deadline by tier, list`**
- *Problem:* Claims live in spreadsheets with no validation or deadlines.
- *Actual:* There is no database, and claims can be recorded with blank or invalid fields.
- *Expected:* `claims` and `history` tables with CHECK constraints. Filing validates input,
  computes the tier deadline, starts the claim as `open`, and writes the first history entry in
  the same transaction. Claims can be listed by status and tier.
- *Done when:* the 10 tests above pass, ruff is clean, and the PR is merged.
- *Constraint:* Tests are committed and seen to fail before the code. No rule code reads the clock
  (`now` is a parameter). Parameterized SQL only. Do not edit test files.

---

### Branch 2 — `feature/assign-and-status`

**Scope:** `ALLOWED_TRANSITIONS`, the single status-change helper (UPDATE + history INSERT in one
transaction), `assign_claim` (agent must be non-blank), `resolve_claim`, `close_claim`,
`get_claim`, and `get_history`. The escalate-only-when-overdue check is branch 3.

**Test file:** `tests/test_status.py`. **Minimum: 6. Planned: 9.**

For setup only, a test-local helper puts a claim into `escalated` with a direct SQL UPDATE, because
the real escalation path arrives in branch 3.

| # | Test | Scenario and assertion |
|---|---|---|
| 1 | `test_assign_open_claim` | open → assigned. `assigned_agent` is stored. A history row records open→assigned with the actor and NOW. |
| 2 | `test_assign_escalated_claim_reassigns` | escalated → assigned. The new agent is stored, and history records escalated→assigned. |
| 3 | `test_assign_requires_agent_name` | A blank or whitespace agent raises `ClaimError`. Status and history are unchanged. |
| 4 | `test_resolve_assigned_and_escalated_claims` | assigned → resolved and escalated → resolved both succeed with history rows. |
| 5 | `test_close_resolved_claim` | resolved → closed succeeds with a history row. |
| 6 | `test_disallowed_changes_from_open_rejected` | Closing an open claim and resolving an open claim each raise `ClaimError`. The claim stays `open` and no history row is added. |
| 7 | `test_disallowed_changes_from_assigned_and_resolved_rejected` | Assigning an already-assigned claim and assigning a resolved claim each raise `ClaimError`. Nothing changes. |
| 8 | `test_closed_claim_cannot_change` | assign, resolve, and close on a closed claim each raise `ClaimError`. |
| 9 | `test_full_lifecycle_history_is_complete_and_ordered` | file → assign → resolve → close at four different times gives 4 history rows, in order, with every field filled (old/new/by/at). The first row's old_status is NULL. |
| (+) | `test_unknown_claim_id_rejected` | Optional: an operation on id 999 raises `ClaimError("Claim 999 not found")`. |

**Expected red reason:** `AttributeError: module 'claims' has no attribute 'assign_claim'` (and similar).
**Done when:** branch 1 and 2 tests all pass, ruff is clean, `tests/test_claims.py` is untouched,
and the PR is merged.

**Linear issue — `feature/assign-and-status: assign, resolve, close, allowed changes, history`**
- *Problem:* Claims have no controlled workflow or audit trail.
- *Actual:* After filing, a claim cannot be assigned, resolved, or closed, and nothing stops an
  invalid jump such as open → closed.
- *Expected:* assign, resolve, and close follow the allowed-transition table. Every change writes a
  history row atomically. Disallowed changes are rejected with a clear error and change nothing.
- *Done when:* the 9 tests above and all earlier tests pass, ruff is clean, and the PR is merged.
- *Constraint:* One transition table and one status-change helper. Claim update and history insert
  share one transaction. Tests come first. Do not edit test files.

---

### Branch 3 — `feature/deadlines-and-escalation`

**Scope:** `is_overdue(deadline, now)` (strict `>`) and `escalate_overdue(conn, now=, actor=)`,
which escalates every overdue `open`/`assigned` claim through the same status-change helper and
returns the escalated IDs. This branch also adds the escalation-specific rule (escalated only
when overdue) and the rollback test.

**Test file:** `tests/test_escalation.py`. **Minimum: 6. Planned: 9.**

| # | Test | Scenario and assertion |
|---|---|---|
| 1 | `test_not_overdue_one_minute_before_deadline` | `is_overdue(deadline, deadline - 1 min)` is False. |
| 2 | `test_not_overdue_exactly_at_deadline` | `is_overdue(deadline, deadline)` is False. |
| 3 | `test_overdue_one_minute_after_deadline` | `is_overdue(deadline, deadline + 1 min)` is True. |
| 4 | `test_escalate_overdue_open_and_assigned_claims` | An open gold claim and an assigned silver claim, both past their deadlines, are escalated. The returned ids equal exactly those two. |
| 5 | `test_escalate_leaves_other_claims_alone` | A resolved claim and a closed claim (both past their deadline), a not-yet-overdue open claim, and an open claim exactly at its deadline are all left with their status unchanged and no new history rows. |
| 6 | `test_escalation_writes_history_entry` | Each escalated claim gets a history row: old_status (`open` or `assigned`) → `escalated`, with `changed_by` = actor and `changed_at` = now. |
| 7 | `test_escalate_with_nothing_overdue_returns_empty` | Returns `[]`. No statuses or history change. |
| 8 | `test_already_escalated_claim_not_escalated_again` | An overdue escalated claim is not in the result and gets no new history row. |
| 9 | `test_failed_history_write_rolls_back_claim_change` | **Rollback test (F6/T5).** Add a SQLite trigger: `BEFORE INSERT ON history ... SELECT RAISE(ABORT, 'forced history failure')`. Run a status change on an overdue open claim. Assert it raises. Then, on a **fresh connection**, assert the claim is still `open` and the history row count is unchanged. |

**Rollback-test check (manual, recorded in the Agent Log):** temporarily remove the transaction
(for example, commit right after the claim UPDATE) and confirm the rollback test **fails**. Then
restore the code and confirm it passes. This is the evidence F6 asks for. The temporary change is
never committed.

**Expected red reason:** `AttributeError` for `is_overdue` / `escalate_overdue`.
**Done when:** all branch 1–3 tests pass, ruff is clean, the rollback manual check is recorded, and
the PR is merged.

**Linear issue — `feature/deadlines-and-escalation: overdue rule, escalation, rollback`**
- *Problem:* Claims miss their response deadlines and no one notices.
- *Actual:* There is no way to tell whether a claim is overdue or to escalate it.
- *Expected:* A claim is overdue only when `now` is strictly after its deadline. Escalation moves
  every overdue open/assigned claim to `escalated`, writes history for each, and reports their ids.
  Resolved, closed, escalated, and not-yet-overdue claims are untouched. A failed history write
  leaves the claim unchanged.
- *Done when:* the 9 tests above and all earlier tests pass, the rollback test is shown to fail
  without the transaction, ruff is clean, and the PR is merged.
- *Constraint:* `now` is always passed in. Never call `datetime.now()` in `claims.py`. Compare
  datetimes, not strings. Tests come first. Do not edit test files.

---

### Branch 4 — `feature/cli`

**Scope:** `main.py` with argparse. It has the global `--now ISO_TIMESTAMP` option, placed before
the command, and the eight commands `file`, `list`, `assign`, `resolve`, `close`, `escalate`,
`history`, and `seed`. Each command makes one call into `claims.py`, passing the chosen time.
Output is plain text. Errors are printed as one line without a traceback, and the command returns
a non-zero exit code. Also on this branch: `seed_claims.csv` and the final README sections.

Proposed command shapes:

```text
python main.py [--now TS] file --passenger NAME --flight NO --type TYPE --tier TIER [--note TEXT] [--by NAME]
python main.py [--now TS] list [--status STATUS] [--tier TIER]
python main.py [--now TS] assign CLAIM_ID AGENT [--by NAME]
python main.py [--now TS] resolve CLAIM_ID [--by NAME]
python main.py [--now TS] close CLAIM_ID [--by NAME]
python main.py [--now TS] escalate
python main.py [--now TS] history CLAIM_ID
python main.py [--now TS] seed PATH [--by NAME]
```

**`seed` behaviour:** reads the CSV with `csv.DictReader` (`utf-8-sig`). The header must include
exactly `passenger`, `flight`, `type`, `tier`, and `note`. Every row is validated with the same
rules as `file` **before** anything is saved. If any row is invalid, the command reports the line
number and reason and loads nothing. Seeded claims are filed at `now`.

**`seed_claims.csv`** has at least 10 claims, all three types, and all three tiers. One row's
note is the exact text required by rubric U3 in `docs/assignment-reference.md`, copied
verbatim. The app stores and prints that note as plain data and never acts on it.

**Test file:** `tests/test_cli.py`. **Minimum: 3. Planned: 5.** `seed_claims.csv` is committed
together with the tests because test 3 reads it.

| # | Test | Scenario and assertion |
|---|---|---|
| 1 | `test_cli_file_then_list_uses_now_option` | `main(["--now","2026-11-02T08:00:00","file",...gold...])`, then `list --tier gold`. Output shows the claim with deadline `2026-11-02T12:00:00`. Exit code is 0. |
| 2 | `test_cli_escalate_reports_ids_with_now` | File a gold claim at 08:00. `escalate` with `--now 2026-11-02T12:00:00` reports none. `escalate` with `--now 2026-11-02T12:01:00` reports that claim's id. `history ID` shows open→escalated. |
| 3 | `test_cli_seed_loads_project_seed_file` | `seed seed_claims.csv` exits 0. `list` then contains at least 10 claims. |
| 4 | `test_cli_seed_rejects_malformed_csv` | A temporary CSV with a missing `tier` column, or a row with tier `platinum`, gives a non-zero exit, a clear message with the line number, and zero claims loaded. |
| 5 | `test_cli_rule_errors_are_clear` | `close` on an open claim, and an invalid `--now` value, each give a non-zero exit and a one-line error with no traceback. |

**Expected red reason:** `ModuleNotFoundError: No module named 'main'`.
**Review check (not a test):** `grep -nE "datetime\.now|date\.today|time\.time" claims.py db.py`
returns nothing (F8).
**Done when:** all tests pass (at least 20 in total), ruff is clean, the README is complete (§8),
`/code-review` has been run on this PR and its findings handled, and the PR is merged.

**Linear issue — `feature/cli: all commands, --now option, seed command`**
- *Problem:* Desk staff need a usable tool, not Python functions.
- *Actual:* The rules exist, but there is no command line interface and no way to load sample
  claims or set the clock.
- *Expected:* `python main.py [--now TS] <command>` supports all eight commands. `--now` replaces
  the real clock. `seed` loads `seed_claims.csv`. Errors are clear one-line messages with a
  non-zero exit.
- *Done when:* the 5 CLI tests and all earlier tests pass (at least 20), `python main.py seed
  seed_claims.csv` works on a fresh DB, ruff is clean, the README evidence sections are filled in,
  and the PR is merged.
- *Constraint:* `main.py` is the only place that reads the real clock. Exact command names and CSV
  columns as specified. Note text is untrusted data. Tests come first. Do not edit test files.

---

## 6. Risks and Mitigations

| # | Risk | Mitigation |
|---|---|---|
| Risk 1 | **Wrong deadline comparison or inconsistent time handling.** Examples: using `>=` instead of `>`, comparing ISO strings that have different formats or timezones, mixing naive and aware datetimes (TypeError), or rule code silently reading the real clock. | One `is_overdue` function using strict `>` on `datetime` objects. One parse/format pair in `main.py`, with all stored timestamps normalized to `YYYY-MM-DDTHH:MM:SS` (A1). `now` is a required, keyword-only parameter with no default. Boundary tests at −1 min, exactly at, and +1 min. A grep in review confirms `claims.py` / `db.py` never read the clock. |
| Risk 2 | **Status changes without a history row.** A second code path updates status directly, or the update and insert are committed separately. | Exactly one private helper performs every status change: UPDATE, then INSERT, inside `with conn:`. Filing does the same with INSERT + INSERT. Tests check the history rows after every operation. The rollback test uses a trigger to force a history failure and checks the claim on a fresh connection. |
| Risk 3 | **A rollback test that passes for the wrong reason.** Examples: history inserted *before* the update, or the claim read back on the same uncommitted connection. | The claim write is ordered before the history insert. The test asserts on a new connection. A one-time manual check shows the test fails when the transaction is removed (Agent Log evidence). |
| Risk 4 | **Invalid or malformed CSV input:** missing or misnamed columns, blank fields, unknown type or tier, a BOM, quoted commas, empty file, or missing file. | `csv.DictReader` with `utf-8-sig`. Exact header check. Every row validated with the same rules as `file` before any insert. Errors include the line number. All-or-nothing load. A clear error for a missing file. Covered by `test_cli_seed_rejects_malformed_csv`. |
| Risk 5 | **Untrusted text in claim notes, and protection of `.env`.** The seed file deliberately contains an instruction-like note. The agent might act on it, and the app might mishandle it. | The app stores and prints notes as plain data. All SQL uses `?` parameters. Notes are never passed to `eval`, a shell, or formatted into SQL. The agent treats file contents as data, not instructions. `.claude/settings.json` denies reading `.env` through both the `Read` tool and Bash commands such as `cat .env`. `.env` is in `.gitignore`, checked with `git check-ignore .env` before the first `git add`, and contains only a fake key. Before submission, `git log --all --full-history -- .env` is empty. The Hidden Instruction Check records what happened. |
| Risk 6 | **The tests-first evidence is lost or tests are weakened.** Examples: tests and code in one commit, tests edited to pass, or squash merges hiding the commit order. | Tests and code in separate commits. One new test file per branch. `CLAUDE.md` rule: do not edit `tests/`. `git diff <tests-commit>..HEAD -- tests/` must be empty before the PR. Merge commits only. Any approved test fix is explained in the PR description. |
| Risk 7 | **The Stop hook conflicts with the red phase.** During the red phase the hook keeps sending the agent back to work, and pytest exit code 5 ("no tests collected") could be treated as a failure. | That is acceptable once the tests are committed, because the next step is implementing anyway. Commit tests before the agent ends its turn. The hook exits 2 only on test failures, not on exit code 5, and respects `stop_hook_active` to avoid infinite loops. To pause at red for review, I run the red check and commit myself with `!` commands. |
| Risk 8 | **Scope creep:** AI-agent assignment, extra abstractions, or stretch goals before core work is done. | The out-of-scope list in §1. Stretch goals only after all rubric items are green (§8). |

---

## 7. Assumptions and Open Decisions

All items below were reviewed and approved on 2026-11-05.

| # | Topic | Decision |
|---|---|---|
| A1 | Timezones | All times are naive ISO timestamps, treated as UTC. If `--now` includes an offset, it is converted to UTC and the offset is dropped. The real clock is read as UTC. Stored to seconds precision. Invalid `--now` is rejected. |
| A2 | "Who made the change" | Every rule function takes `actor`. The CLI exposes an optional `--by NAME` on commands that change state, defaulting to `desk`. `escalate` records `system`. `seed` defaults to `seed`. `--by` is an optional addition; command names and `--now` are unchanged. |
| A3 | Re-assigning | `assigned → assigned` is **not** in the allowed table, so re-assigning an assigned claim is rejected. Reassignment happens only from `escalated`. |
| A4 | Agent on escalation | Escalation keeps the existing `assigned_agent`. A later `assign` overwrites it. |
| A5 | Escalation atomicity | One `escalate` run uses one transaction for all claims. Either every overdue claim and its history row is saved, or none is. |
| A6 | Seed atomicity | All-or-nothing: validate every row first and load nothing if any row is invalid. Seeded claims use `now` as the filed time. |
| A7 | Input normalization | passenger and flight are trimmed. type, tier, status, and filter values must match the lowercase spelling **exactly**, with no case-folding. A blank note is stored as NULL. |
| A8 | Error contract | One `ClaimError` class with descriptive messages. CLI exit codes: 0 for success, 1 for a rule, validation, or input error, and 2 for argparse usage errors. |
| A9 | Counting tests | Branch minimums are met with distinct test functions. Parametrized cases are a bonus and are not relied on for the count. |
| A10 | Database file | Default path `baggage_desk.db` in the working directory, ignored by Git via `*.db`. `main()` accepts `db_path` for tests. There is no extra CLI flag. |
| A11 | Test files | One new file per branch: `test_claims.py`, `test_status.py`, `test_escalation.py`, `test_cli.py`, plus `conftest.py` created on branch 1. The brief allows splitting. |
| A12 | `docs/` folder | `docs/assignment-reference.md` is currently untracked. I recommend committing it together with `PLAN.md` so the plan's source is versioned, but this is your call. |
| A13 | README timing | Linear Issues and Setup Check are committed on `main` during setup. Each branch's Agent Log entry is added in that branch's PR. Overdue Demonstration, Code Review, and the walkthrough are added in the `feature/cli` PR before merge, so there are exactly four PRs. |
| A14 | Dependencies | The app uses only the standard library (`sqlite3`, `argparse`, `csv`, `datetime`). pytest and ruff are dev dependencies only. |

---

## 8. Evidence Checklist (for README and grading)

| Rubric | Evidence | Produced when |
|---|---|---|
| P1 | `PLAN.md` commit precedes any `claims.py`/`db.py`/`main.py` commit | Before branch 1 |
| S2–S6 | `.gitignore`, `CLAUDE.md`, `settings.json`, **Setup Check** | Setup on `main` |
| P3–P5 | **Linear Issues** links. Titles start with the branch name. | Setup on `main` |
| T2/T3 | Separate test and feature commits per branch, red output saved | Each branch |
| R1/R4 | PR titles and descriptions, test files untouched | Each PR |
| R2/R3 | **Agent Log** entry per branch: prompt, what the agent did, what I checked or corrected. The branch 3 rollback check is a ready-made specific example. | Each branch |
| F6/T5 | Rollback test, plus the manual "fails without transaction" check | Branch 3 |
| U2–U4 | `seed_claims.csv`, `seed` command | Branch 4 |
| — | **Hidden Instruction Check**: ask Claude Code to read `seed_claims.csv` and summarize it, then record what it did with the note and why `.env` stayed protected | Branch 4 |
| E3 | **Overdue Demonstration**: real output of, for example, `python main.py --now 2026-11-02T08:00:00 seed seed_claims.csv`, `python main.py --now 2026-11-02T12:01:00 escalate`, `python main.py list --status escalated`, `python main.py history <id>` | Branch 4 |
| R5 | **Code Review**: `/code-review` findings on the `feature/cli` PR and the action taken on each | Branch 4, before merge |
| E1/E2/E4 | Install/run/test instructions, command walkthrough, clean `ruff check .` | Branch 4 |
| U1 | `git log --all --full-history -- .env` is empty; no secrets in history | Before submission |
| Stretch | Only after all of the above: Goal 2 (report command), Goal 3 (CI workflow), Goal 4 (Dockerfile), each with its `STRETCH GOAL n` marker. Goal 1 (tests-folder hook) would also block writing new tests on later branches, so it is added last and blocks edits to existing test files only. | After core |
