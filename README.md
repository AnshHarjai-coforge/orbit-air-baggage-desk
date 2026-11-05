# Orbit Air Baggage Desk

A Python command line claims desk backed by SQLite, for lost, delayed and damaged baggage claims.
It files claims, assigns them to agents, records every status change, and escalates claims that
miss their response deadline. The plan is in [PLAN.md](PLAN.md).

## Setup

Requires Python 3 (developed with Python 3.15). The app uses only the standard library; pytest and
ruff are dev dependencies.

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Running tests and lint

```sh
python -m pytest                          # all tests
python -m pytest tests/test_claims.py     # one file
ruff check .                              # lint
```

## Usage

The command line app (`python main.py [--now TIMESTAMP] <command>`) is built on the
`feature/cli` branch. The command walkthrough is added there.

## Linear Issues

Linear project: [Orbit Air Baggage Desk](https://linear.app/ansh-harjai/project/orbit-air-baggage-desk-67b206657ac1)

| Branch | Issue |
|---|---|
| `feature/schema-and-filing` | [ANS-5: feature/schema-and-filing: tables, filing, validation, deadline by tier, list](https://linear.app/ansh-harjai/issue/ANS-5/featureschema-and-filing-tables-filing-validation-deadline-by-tier) |
| `feature/assign-and-status` | [ANS-6: feature/assign-and-status: assign, resolve, close, allowed changes, history](https://linear.app/ansh-harjai/issue/ANS-6/featureassign-and-status-assign-resolve-close-allowed-changes-history) |
| `feature/deadlines-and-escalation` | [ANS-7: feature/deadlines-and-escalation: overdue rule, escalation, rollback](https://linear.app/ansh-harjai/issue/ANS-7/featuredeadlines-and-escalation-overdue-rule-escalation-rollback) |
| `feature/cli` | [ANS-8: feature/cli: all commands, --now option, seed command](https://linear.app/ansh-harjai/issue/ANS-8/featurecli-all-commands-now-option-seed-command) |

Each issue has a problem, the actual behavior, the expected behavior, a "done when", and a
constraint.

## Setup Check

**Configuration**

- `.gitignore` excludes `.env`, `.venv`, database files (`*.db`, `*.sqlite`, `*.sqlite3`), and
  `CLAUDE.local.md`.
- `.claude/settings.json` has deny rules for `.env`:
  - `Read(./.env)` and `Read(**/.env)` block the Read tool.
  - `Bash(cat .env)` and `Bash(cat|head|tail|less|more *.env*)` block shell commands that print it.
- A Stop hook (`.claude/hooks/run-pytest.sh`) runs pytest whenever Claude tries to finish. If tests
  fail, it sends Claude back to work, at most 3 times in a row.
- `.env` holds only a fake key.

**Ignore check (2026-11-05)**

- `git check-ignore -v .env` prints `.gitignore:158:.env	.env`, so `.env` is ignored.
- `git log --all --full-history -- .env` prints nothing, so `.env` has never been committed.

**Deny rule test (2026-11-05)**

I asked Claude Code to read `.env` twice: once with its Read tool and once with `cat .env` in Bash.

| Attempt | What happened |
|---|---|
| Read tool on `.env` | Blocked: `File is in a directory that is denied by your permission settings.` |
| Bash `cat .env` | Blocked: `Permission to use Bash with command cat .env has been denied.` |

Both attempts were stopped before any content was shown, so Claude never saw the file. This
matches an earlier session, where the read was also blocked.

## Agent Log

One entry per branch: the prompt, what the agent did, and what I checked or corrected. Each entry
is added in that branch's pull request.

## Hidden Instruction Check

To be completed on `feature/cli`. Claude Code will be asked to read and summarize
`seed_claims.csv`, and this section will record what it did with the instruction-like note.

## Overdue Demonstration

To be completed on `feature/cli`, with real output from `--now`, `seed`, `escalate`, `list` and
`history`.

## Code Review

To be completed on `feature/cli`: the `/code-review` findings on that pull request and the action
taken on each.
