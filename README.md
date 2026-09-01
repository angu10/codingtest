# Interface CUA

A model figures out how to do a task in an application that has no API. What it learned is saved as
a typed capability. After that the task runs deterministically, with no model involved.

**There are two commands.**

```
discover   runs once, uses a model, writes a capability YAML
replay     runs that YAML forever, needs no API key, returns a typed result
```

Everything else is a flag on one of those two.

```
goal in English ──▶ discover ──▶ capability.yaml ──▶ replay ──▶ typed result
                    (a model)     (a human           (no model)   success
                                   reviews it)                    business_outcome
                                        │                         failure
                                        └── escalation: a human takes over the live browser
```

The design write-up is in [REPORT.md](REPORT.md).

**If you want to run it rather than read about it, start with [RUNBOOK.md](RUNBOOK.md).** Every
scenario is there as a command you can paste, with the exact output it produced when I ran it.

## Setup

Python 3.12 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Or `make install && make install-browser`, which does the same thing in whatever environment is
active. Every `make` target uses the `python` on your PATH; override with `make test PYTHON=...`.

**Only `discover` needs an API key.** The demo app, the tests, replay, and the handoff path all run
without one. `make test` removes `ANTHROPIC_API_KEY` from the environment before running, which is
how the "no model in replay" claim is checked rather than just asserted.

For discovery, copy the template and fill in your key. `.env` is gitignored, and nothing reads a key
from a committed file.

```bash
cp .env.example .env
```

`.env.example` also documents `ANTHROPIC_WORKSPACE_ID`, which only matters if your key is
identity-linked rather than organisation-scoped. Ordinary keys need nothing but the key.

## Quick check

```bash
make verify                        # lint, the full suite, and schema generation
```

Expect `All checks passed!` and `83 passed`. No network, no API key, no browser window.

## The target application

`demo_app/` is Meridian CU, a synthetic credit-union servicing console. It is deliberately awkward:
no `data-testid` attributes, no `id` on any interactive control, class names like `.c1` and `.tbl2`,
a table-based layout, and the account pane is a real `<iframe>`. A clean demo site would have made
the interesting problem — finding the right control reliably — disappear.

It also renders SSNs and dates of birth, because a bank servicing screen does. All of them are in
the `666-xx-xxxx` block, which has never been issued and never will be.

```bash
make demo-app                      # http://127.0.0.1:8000
```

Leave that running in one shell for everything below.

## Scenarios

Every one of these is a real run against the live app. None of them needs an API key.

### Reading a member and reaching a review screen

`artifacts/open-sub-account-review-v1.yaml`

| Command | Result | Exit | What it shows |
|---|---|---|---|
| `make replay MEMBER=58431` | `success` | 0 | Happy path. Two accounts share an identical *Open Sub-Account* link, so the locator has to disambiguate by table row |
| `make replay MEMBER=12345` | `success` | 0 | A second member, same capability, different data |
| `make replay MEMBER=99999` | `business_outcome` `MEMBER_NOT_FOUND` | 0 | "No such member" is an answer, not a crash |
| `make replay MEMBER=55501` | `business_outcome` `MEMBER_RESTRICTED` | 0 | A declared permission denial |
| `make replay MEMBER=55506` | `failure` `postcondition_failed` | 1 | **The same screen as 55501**, undeclared, so it is a system failure |
| `make replay MEMBER=55502` | `success` | 0 | The page takes six seconds; a bounded wait absorbs it |
| `make replay MEMBER=55504` | `success` | 0 | A modal appears; the capability declared it, so it is dismissed and the run continues |
| `make replay MEMBER=55503` | `needs_human` `SESSION_EXPIRED` | 2 | A declared escalation |

**Run 55501 and 55506 next to each other.** Both stop on a "Permission denied" panel. The two
screens are identical apart from one sentence and both return HTTP 403. One exits 0 as a business
outcome, the other exits 1 as a failure. The only difference is that the capability declared one of
them and not the other — nothing in the engine reads the page to decide:

```bash
grep -rn "MEMBER_\|SESSION_\|AUTHORIZATION_" src/     # returns nothing
```

### Creating something

`artifacts/create-sub-account-v1.yaml`. This one writes.

| Command | Result | Exit | What it shows |
|---|---|---|---|
| `make replay-create MEMBER=12345` | `success` | 0 | One record created |
| `make replay-create MEMBER=55505` | `success`, `reconciled: true` | 0 | The write lands, then the server returns 500. Replay checks instead of retrying |
| `make replay-create MEMBER=58431 DEPOSIT=-5.00` | `business_outcome` `DEPOSIT_INVALID` | 0 | The form rejects the amount; declared, so it is an answer |
| `make replay-create MEMBER=58431 DEPOSIT=abc` | `failure` `precondition_failed` | 1 | Not a number — rejected before the browser opens |

For 55505, the decisive check is not the exit code:

```bash
curl -s http://127.0.0.1:8000/api/members/55505/sub-accounts
```

Exactly one record. A retry would have made two. Restart `make demo-app` between write runs — the
data lives in memory.

Without `--confirm`, the write does not run at all:

```bash
python -m interface_cua.cli replay artifacts/create-sub-account-v1.yaml \
  --input member_id=12345 --input account_type=savings \
  --input nickname='Holiday Fund' --input opening_deposit=250.00 --allow-draft
# needs_human: risk:consequential_write, exit 2 — nothing was submitted
```

### Refusing to start

`make replay` always passes `--allow-draft`. Drop it and use the CLI directly to see the gates.

| Command | Result |
|---|---|
| no `--allow-draft` | `validation_required`, `check: approval` — the artifact is a draft |
| `--family acme` | `validation_required`, `check: application` — wrong application |
| `--input member_id=abc` | `failure`, step `input-validation` — fails the declared pattern |
| `--input nope=1` | The CLI refuses and prints the declared inputs |
| demo app not running | `validation_required`, `check: entry_route` — not a stack trace |

```bash
python -m interface_cua.cli replay artifacts/open-sub-account-review-v1.yaml \
  --input member_id=58431 --input account_type=savings
```

### A human taking over

```bash
python -m interface_cua.cli replay artifacts/open-sub-account-review-v1.yaml \
  --input member_id=55503 --input account_type=savings \
  --allow-draft --headed --handoff
```

A browser window opens and the run stops on an expired session. A small operator page appears at
<http://127.0.0.1:8765> with two buttons, Resume and Abort. **Open it in a different browser** — the
Chromium window is the live session, and it is yours to drive.

You are now the operator. Fix the session in that window, then click Resume. The step re-checks its
own precondition before continuing: if you left the browser somewhere the capability cannot continue
from, it stops again rather than trusting your click.

To see a resume succeed: go to <http://127.0.0.1:8000/>, type `58431` in the box, **do not press
Search**, then click Resume. The step does the clicking. Everything you did is recorded, with values
masked inside the page before they ever reach the log.

### Discovery

This is the only command that spends money.

```bash
python -m interface_cua.cli discover \
  --goal 'For member 58431, open the savings sub-account form. Return the member name
          and the current balance of the Savings Account.' \
  --input member_id=58431 --capability-id discovered-demo \
  --out artifacts/discovered-demo.yaml --landmark 'Member Search' \
  --max-steps 12 --headed
```

`--headed` lets you watch the model drive. It finishes in about four steps and writes a draft
artifact. Then replay what it wrote, with no key:

```bash
env -u ANTHROPIC_API_KEY python -m interface_cua.cli replay \
  artifacts/discovered-demo.yaml --input member_id=58431 --allow-draft
```

**A goal that asks for regulated data is refused:**

```bash
python -m interface_cua.cli discover \
  --goal 'Get the Social Security Number for member 58431.' \
  --input member_id=58431 --out artifacts/never-written.yaml \
  --landmark 'Member Search' --max-steps 8
# policy_denied, exit 1, no artifact — and the SSN is not in the log either
```

`artifacts/discovered-open-sub-account-v1.yaml` is a capability produced by a real run, with its log
and browser trace in `evidence/discovery/`, in case you would rather read one than pay for one.

## Evidence

```bash
make evidence
playwright show-trace evidence/replay-undeclared-refusal/failure/trace.zip
```

| Directory | What is in it |
|---|---|
| `evidence/discovery/` | Event log and browser trace from a live discovery run |
| `evidence/replay-success/` | Event log plus a screenshot at every step |
| `evidence/replay-undeclared-refusal/failure/` | The typed result, a screenshot, a DOM snapshot, and a trace |

The event log records what was decided and why: the policy verdict for each action, every locator
that was tried and the reason each was rejected, and — on discovery runs only — the model's own
summary of its reasoning. Sensitive values are masked before anything is written.

## Layout

```
src/interface_cua/
  schema/       the capability and result contracts (Pydantic; the source of truth)
  discovery/    the model loop, the click recorder, and the compiler
  replay/       the executor, locator resolution, waits, and reconciliation
  policy/       authorisation, redaction, content scanning
  handoff/      the session lease and the operator console
  observability/  the event log and failure bundles
  surface/      the browser adapter, behind a protocol
demo_app/       Meridian CU, the target application
artifacts/      capabilities, and the generated JSON Schema
evidence/       logs and traces from real runs
tests/          83 tests, none of which need an API key
```
