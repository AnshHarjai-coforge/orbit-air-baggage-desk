"""Command line interface for the Orbit Air baggage claims desk.

    python main.py [--now TIMESTAMP] <command> ...

This is the only module that reads the real clock (in `_resolve_now`). Every rule in claims.py
receives the chosen time as `now`.
"""

import argparse
import csv
import sqlite3
import sys
from datetime import UTC, datetime

import claims
import db

DEFAULT_DB_PATH = "baggage_desk.db"
SEED_COLUMNS = ("passenger", "flight", "type", "tier", "note")

# Who is recorded in history when --by is not given (A2).
DEFAULT_ACTOR = "desk"
ESCALATE_ACTOR = "system"
SEED_ACTOR = "seed"

LIST_COLUMNS = (
    ("id", "ID"),
    ("status", "STATUS"),
    ("tier", "TIER"),
    ("type", "TYPE"),
    ("passenger", "PASSENGER"),
    ("flight", "FLIGHT"),
    ("assigned_agent", "AGENT"),
    ("filed_at", "FILED"),
    ("deadline", "DEADLINE"),
    ("note", "NOTE"),
)


class InputError(Exception):
    """Bad command line input that is not a claim rule, such as an invalid --now or CSV file."""


def _resolve_now(value):
    """Return the current time as a naive UTC datetime, to the second (A1).

    Uses --now when given. Otherwise reads the real clock: the only clock read in the app.
    """
    if value is None:
        moment = datetime.now(UTC)
    else:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise InputError(
                f"Invalid --now {value!r}. Use an ISO timestamp such as 2026-11-02T14:00:00"
            ) from None
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.replace(microsecond=0)


def _build_parser():
    parser = argparse.ArgumentParser(prog="main.py", description="Orbit Air baggage claims desk")
    parser.add_argument(
        "--now",
        metavar="TIMESTAMP",
        help="ISO timestamp to use instead of the real clock, e.g. 2026-11-02T14:00:00",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def by_option(command, default=DEFAULT_ACTOR):
        command.add_argument(
            "--by", default=default, metavar="NAME", help=f"who made the change (default: {default})"
        )

    file_cmd = commands.add_parser("file", help="file a new claim")
    file_cmd.add_argument("--passenger", required=True, metavar="NAME")
    file_cmd.add_argument("--flight", required=True, metavar="NO")
    file_cmd.add_argument("--type", required=True, dest="claim_type", metavar="TYPE",
                          help="delayed, lost or damaged")
    file_cmd.add_argument("--tier", required=True, metavar="TIER", help="standard, silver or gold")
    file_cmd.add_argument("--note", metavar="TEXT")
    by_option(file_cmd)

    list_cmd = commands.add_parser("list", help="list claims")
    list_cmd.add_argument("--status", metavar="STATUS")
    list_cmd.add_argument("--tier", metavar="TIER")

    assign_cmd = commands.add_parser("assign", help="assign an open or escalated claim to an agent")
    assign_cmd.add_argument("claim_id", type=int, metavar="CLAIM_ID")
    assign_cmd.add_argument("agent", metavar="AGENT")
    by_option(assign_cmd)

    for name, help_text in (
        ("resolve", "resolve an assigned or escalated claim"),
        ("close", "close a resolved claim"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("claim_id", type=int, metavar="CLAIM_ID")
        by_option(command)

    commands.add_parser("escalate", help="escalate every overdue open or assigned claim")
    commands.add_parser("report", help="print open and overdue counts by tier")

    history_cmd = commands.add_parser("history", help="show the history of one claim")
    history_cmd.add_argument("claim_id", type=int, metavar="CLAIM_ID")

    seed_cmd = commands.add_parser("seed", help="load claims from a CSV file")
    seed_cmd.add_argument("path", metavar="PATH")
    by_option(seed_cmd, default=SEED_ACTOR)

    return parser


def _cell(value):
    return "-" if value is None else str(value)


def _print_report(counts):
    """Print open and overdue counts for every tier. Same padding as the list table."""
    rows = [("TIER", "OPEN", "OVERDUE")]
    for tier in claims.TIERS:
        counts_for_tier = counts[tier]
        rows.append((tier, str(counts_for_tier["open"]), str(counts_for_tier["overdue"])))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def _print_claims(rows):
    if not rows:
        print("No claims found.")
        return
    table = [[header for _, header in LIST_COLUMNS]]
    table += [[_cell(row[key]) for key, _ in LIST_COLUMNS] for row in rows]
    widths = [max(len(line[i]) for line in table) for i in range(len(LIST_COLUMNS))]
    for line in table:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip())


def _read_seed_rows(path):
    """Read and validate every CSV row before anything is saved (A6).

    Returns a list of (passenger, flight, claim_type, tier, note) tuples. Raises InputError naming
    the line number of the first bad row.
    """
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames
            if header is None:
                raise InputError(f"{path} is empty")
            if sorted(header) != sorted(SEED_COLUMNS):
                missing = [c for c in SEED_COLUMNS if c not in header]
                unexpected = [c for c in header if c not in SEED_COLUMNS]
                details = []
                if missing:
                    details.append(f"missing column(s): {', '.join(missing)}")
                if unexpected:
                    details.append(f"unexpected column(s): {', '.join(unexpected)}")
                if not details:
                    details.append("duplicate column names")
                raise InputError(
                    f"{path}: {'; '.join(details)}. Expected header: {','.join(SEED_COLUMNS)}"
                )

            rows = []
            for row in reader:
                line = reader.line_num
                if None in row:
                    raise InputError(f"{path} line {line}: more values than columns")
                if None in row.values():
                    raise InputError(f"{path} line {line}: fewer values than columns")
                try:
                    rows.append(
                        claims.validate_claim(
                            row["passenger"], row["flight"], row["type"], row["tier"], row["note"]
                        )
                    )
                except claims.ClaimError as error:
                    raise InputError(f"{path} line {line}: {error}") from None
    except OSError as error:
        raise InputError(f"Cannot read {path}: {error.strerror}") from None
    except (csv.Error, UnicodeDecodeError) as error:
        raise InputError(f"Cannot parse {path}: {error}") from None

    if not rows:
        raise InputError(f"{path} has no claims to load")
    return rows


def _run(args, conn, now):
    if getattr(args, "by", None) is not None and not args.by.strip():
        raise InputError("--by cannot be blank")

    if args.command == "file":
        claim_id = claims.file_claim(
            conn, args.passenger, args.flight, args.claim_type, args.tier, args.note,
            now=now, actor=args.by,
        )
        claim = claims.get_claim(conn, claim_id)
        print(f"Filed claim {claim_id} ({claim['tier']}), deadline {claim['deadline']}")

    elif args.command == "list":
        _print_claims(claims.list_claims(conn, status=args.status, tier=args.tier))

    elif args.command == "assign":
        claims.assign_claim(conn, args.claim_id, args.agent, now=now, actor=args.by)
        print(f"Claim {args.claim_id} assigned to {args.agent.strip()}")

    elif args.command == "resolve":
        claims.resolve_claim(conn, args.claim_id, now=now, actor=args.by)
        print(f"Claim {args.claim_id} resolved")

    elif args.command == "close":
        claims.close_claim(conn, args.claim_id, now=now, actor=args.by)
        print(f"Claim {args.claim_id} closed")

    elif args.command == "escalate":
        escalated = claims.escalate_overdue(conn, now=now, actor=ESCALATE_ACTOR)
        if escalated:
            print(f"Escalated {len(escalated)} claim(s): {', '.join(map(str, escalated))}")
        else:
            print("No overdue claims to escalate")

    elif args.command == "report":
        _print_report(claims.report_counts(conn, now=now))

    elif args.command == "history":
        claims.get_claim(conn, args.claim_id)  # unknown id -> ClaimError
        for entry in claims.get_history(conn, args.claim_id):
            old = entry["old_status"] or "-"
            print(
                f"{entry['changed_at']}  {old:<9} -> {entry['new_status']:<9}  "
                f"by {entry['changed_by']}"
            )

    elif args.command == "seed":
        rows = _read_seed_rows(args.path)
        claim_ids = claims.file_claims(conn, rows, now=now, actor=args.by)
        print(f"Loaded {len(claim_ids)} claims from {args.path} (ids {claim_ids[0]}-{claim_ids[-1]})")


def main(argv=None, db_path=DEFAULT_DB_PATH):
    """Run one command. Returns the exit code: 0 success, 1 rule or input error, 2 usage (A8)."""
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exit_:  # argparse has already printed usage or help
        return exit_.code if isinstance(exit_.code, int) else 2

    try:
        now = _resolve_now(args.now)
        conn = db.connect(db_path)
        try:
            db.init_db(conn)
            _run(args, conn, now)
        finally:
            conn.close()
    except (claims.ClaimError, InputError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except sqlite3.Error as error:
        print(f"Error: database error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
