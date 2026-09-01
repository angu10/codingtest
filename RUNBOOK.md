# Runbook

Every command here was run against a clean checkout, and the output below each one is what it
actually printed. **Section 2 is the only one that needs an API key**, and it can be skipped.

**Terminal 1** — leave this running:

```bash
make demo-app
```

**Terminal 2** — everything else. The demo data lives in memory, so **restart Terminal 1 before each
write scenario** if you want counts to start from zero.

---

## 1. The target, first

Open <http://127.0.0.1:8000/member/58431> and view source. No `data-testid`, no `id` on any
interactive control, class names like `.c1` and `.tbl2`, a table layout, and the account pane is a
real `<iframe>`. Two rows carry an identical *Open Sub-Account* link.

That is the problem this project is about. A site with clean selectors would make it disappear.

Then open these two side by side and keep them in mind — section 3 comes back to them:

- <http://127.0.0.1:8000/account-pane/55501>
- <http://127.0.0.1:8000/account-pane/55506>

---

## 2. Discovery — where a capability comes from

**This is the only command that needs an API key, and the only one that costs money.** Everything
after it runs offline. If you would rather not spend anything, skip to section 3 — a capability from
a real run is already committed.

```bash
python -m interface_cua.cli discover \
  --goal 'For member 58431, open the savings sub-account form. Return the member name
          and the current balance of the Savings Account.' \
  --input member_id=58431 --capability-id discovered-demo \
  --out artifacts/discovered-demo.yaml --landmark 'Member Search' \
  --max-steps 12 --headed
```

`--headed` lets you watch the model drive. It finishes in about four steps and writes
`artifacts/discovered-demo.yaml`. Open it. The thing worth looking at is that the model saw
`/member/58431` and wrote `/member/:member_id` bound to an input — that is the difference between a
recording and a capability.

Now replay what the model just wrote, **with the key removed**:

```bash
env -u ANTHROPIC_API_KEY python -m interface_cua.cli replay \
  artifacts/discovered-demo.yaml --input member_id=58431 --allow-draft
```

A goal that asks for regulated data is refused instead:

```bash
python -m interface_cua.cli discover \
  --goal 'Get the Social Security Number for member 58431.' \
  --input member_id=58431 --out artifacts/never-written.yaml \
  --landmark 'Member Search' --max-steps 8
# policy_denied, exit 1, no artifact written — and the SSN is not in the log either
```

### If you skipped the paid run

`artifacts/discovered-open-sub-account-v1.yaml` came out of a real Opus 5 run
(`discovery_run_id: disc-live-03`), with its event log and browser trace in `evidence/discovery/`.
It replays with no key:

```bash
env -u ANTHROPIC_API_KEY python -m interface_cua.cli replay \
  artifacts/discovered-open-sub-account-v1.yaml --input member_id=58431 --allow-draft
```

**It only works for member 58431**, and that is worth understanding rather than hiding. The compiler
turned the member id in the URL into a parameter but left the account id (`sav-42`) literal, because
`account_id` was never a declared input and a single run cannot tell a derived value from a
constant. Try another member and replay reaches the right page and *still* refuses to call it
success, because the declared postcondition does not match. An over-specified contract fails closed.

### The two artifacts

Discovery writes a draft with the mechanics in it. A human then declares what the outcomes mean and
which steps are risky — that judgement is not something one run can produce.

`artifacts/open-sub-account-review-v1.yaml`, used for the rest of this runbook, is that fuller
artifact. It was **hand-authored** (`provenance.discovery_run_id: manual-bootstrap`) rather than
derived from the discovered one, so the round trip from discovery to a fully-declared capability is
not automated here. Every artifact in the repo is still `approval_state: draft`.

---

## 3. Replay, with no model at all

One capability, `artifacts/open-sub-account-review-v1.yaml`, driven with eight different members.
`make replay` unsets `ANTHROPIC_API_KEY` before every one of these.

```bash
make replay MEMBER=58431
```
```json
{ "status": "success",
  "outputs": { "member_name": "Morgan Chen", "current_balance": "9876.54" },
  "reconciled": false }
```
Exit 0. Two accounts on this page carry an identical *Open Sub-Account* link, so the locator has to
disambiguate by table row.

```bash
make replay MEMBER=12345
```
```json
{ "status": "success",
  "outputs": { "member_name": "Jordan Rivera", "current_balance": "1250.00" },
  "reconciled": false }
```
Exit 0. Same capability, different member, no changes.

```bash
make replay MEMBER=99999
```
```json
{ "status": "business_outcome", "code": "MEMBER_NOT_FOUND", "step": "search-member", "outputs": {} }
```
**Exit 0.** "No such member" is an answer, not a crash.

```bash
make replay MEMBER=55502
```
```json
{ "status": "success",
  "outputs": { "member_name": "Taylor Delay", "current_balance": "600.00" },
  "reconciled": false }
```
Exit 0. This page takes six seconds to load. Nothing sleeps; a bounded wait re-checks a declared
condition until it holds.

```bash
make replay MEMBER=55504
```
```json
{ "status": "success",
  "outputs": { "member_name": "Avery Modal", "current_balance": "800.00" },
  "reconciled": false }
```
Exit 0. A maintenance modal covers the page. The capability declared it as an interstitial, so it is
dismissed once and the run continues.

```bash
make replay MEMBER=55503
```
```json
{ "status": "needs_human", "reason": "SESSION_EXPIRED", "step": "search-member",
  "lease_required": "HUMAN" }
```
**Exit 2.** A declared escalation. Section 6 takes the session over.

### The pair that matters

Run these two next to each other, and open both screens in a browser first.

```bash
make replay MEMBER=55501
```
```json
{ "status": "business_outcome", "code": "MEMBER_RESTRICTED", "step": "search-member", "outputs": {} }
```
**Exit 0.**

```bash
make replay MEMBER=55506
```
```json
{ "status": "failure",
  "category": "postcondition_failed",
  "retryable": false,
  "step": "search-member",
  "expected": "one of the declared postconditions: member-found, member-not-found, member-restricted, session-expired",
  "observed": "MERIDIAN CU   Servicing Console\nMember Detail\nMember\tDrew Entitlement\nReference\t•••5506\nDate of birth\t1983-09-27\nSSN\t***0006\n\nPermission denied\n\nYour operator role lacks the servicing entitlement.",
  "locator_attempts": [] }
```
**Exit 1.**

Both screens say "Permission denied". Both return HTTP 403. Neither contains a machine-readable
code. One exits 0 as a business outcome and the other exits 1 as a system failure, and the only
difference is that the capability declared one of them.

```bash
grep -rn "MEMBER_\|SESSION_\|AUTHORIZATION_" src/
```
Returns nothing. The engine never reads this application's wording.

Also note the `observed` field above: the reference number and SSN are already masked, because the
failure bundle quotes whatever was on the screen.

---

## 4. Refusing to start

`make replay` always passes `--allow-draft`. Drop it and call the CLI directly to see the gates.

```bash
env -u ANTHROPIC_API_KEY python -m interface_cua.cli replay \
  artifacts/open-sub-account-review-v1.yaml \
  --input member_id=58431 --input account_type=savings
```
```json
{ "status": "validation_required", "check": "approval",
  "reason": "capability has not been approved for replay",
  "expected": { "approval_state": "approved" },
  "observed": { "approval_state": "draft" } }
```
**Exit 4.** Every artifact in this repo is a draft — nothing has been through a review process that
does not exist yet.

```bash
env -u ANTHROPIC_API_KEY python -m interface_cua.cli replay \
  artifacts/open-sub-account-review-v1.yaml \
  --input member_id=58431 --input account_type=savings --allow-draft --family acme
```
```json
{ "status": "validation_required", "check": "application",
  "reason": "capability was authored for a different application or version",
  "expected": { "family": "meridian-cu", "supported_versions": ["demo-v1"] },
  "observed": { "family": "acme", "version": "demo-v1" } }
```
**Exit 4.** Wrong application. It refuses before taking any action.

```bash
make replay MEMBER=abc
```
```json
{ "status": "failure", "category": "precondition_failed", "retryable": false,
  "step": "input-validation",
  "expected": "arguments matching the capability input schema",
  "observed": "member_id is shorter than min_length" }
```
**Exit 1.** Fails the declared input pattern. The browser never opens.

```bash
env -u ANTHROPIC_API_KEY python -m interface_cua.cli replay \
  artifacts/open-sub-account-review-v1.yaml --input nope=1 --allow-draft
```
```
'nope' is not an input of open-sub-account-review; declared inputs are ['account_type', 'member_id']
```
**Exit 1**, on stderr. The CLI will not pass an argument the capability never declared.

**Now stop Terminal 1** and run any replay:
```json
{ "status": "validation_required", "check": "entry_route",
  "reason": "the target application is not reachable",
  "expected": { "base_url": "http://127.0.0.1:8000" },
  "observed": { "error": "Page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:8000/" } }
```
**Exit 4**, not a stack trace. Restart Terminal 1 before continuing.

---

## 5. Creating something

`artifacts/create-sub-account-v1.yaml`. This one writes. **Restart Terminal 1 first.**

### The gate

```bash
env -u ANTHROPIC_API_KEY python -m interface_cua.cli replay \
  artifacts/create-sub-account-v1.yaml \
  --input member_id=12345 --input account_type=savings \
  --input nickname='Holiday Fund' --input opening_deposit=250.00 --allow-draft
```
```json
{ "status": "needs_human", "reason": "risk:consequential_write", "step": "submit-create",
  "lease_required": "HUMAN" }
```
**Exit 2.** No `--confirm`, so it stops before the click. Verify nothing was written:
```bash
curl -s http://127.0.0.1:8000/api/members/12345/sub-accounts
# {"member_id":"12345","sub_accounts":[]}
```

### A clean write

```bash
make replay-create MEMBER=12345 NICKNAME='Holiday Fund'
```
```json
{ "status": "success", "outputs": {}, "reconciled": false }
```
Exit 0. `reconciled: false` means it never had to check — the write returned a clear answer.

### The ambiguous write

```bash
make replay-create MEMBER=55505 FLAGS="--evidence /tmp/ev --run-id r1"
```
```json
{ "status": "success", "outputs": {}, "reconciled": true }
```
Exit 0. The write landed, and *then* the server returned HTTP 500. Replay does not know whether it
worked, so it does not click again — it reads an independent route the artifact declared.
`reconciled: true` tells a caller this run succeeded only after checking.

The two verifications:

```bash
curl -s http://127.0.0.1:8000/api/members/55505/sub-accounts
# exactly one record, nickname "Rainy Day"

grep -c STEP_RETRIED /tmp/ev/r1/events.jsonl
# 0

grep -o '"kind": *"RECONCILIATION[A-Z_]*"' /tmp/ev/r1/events.jsonl
# "kind": "RECONCILIATION_STARTED"
# "kind": "RECONCILIATION_CONFIRMED"
```

The record count alone would not prove much here — this demo app deduplicates identical payloads, so
it would read `1` even if replay had clicked twice. The absent `STEP_RETRIED` event is the assertion
that actually bites, and it is what `tests/test_reconcile.py` checks.

### Two rejections that look alike and are not

```bash
make replay-create MEMBER=58431 NICKNAME='Test Fund' DEPOSIT=-5.00
```
```json
{ "status": "business_outcome", "code": "DEPOSIT_INVALID", "step": "continue-to-review",
  "outputs": {} }
```
**Exit 0.** A well-formed number the *form* rejected. Declared, so it is an answer.

```bash
make replay-create MEMBER=58431 NICKNAME='Test Fund' DEPOSIT=abc
```
```json
{ "status": "failure", "category": "precondition_failed", "retryable": false,
  "step": "input-validation",
  "observed": "opening_deposit must be a decimal" }
```
**Exit 1.** Not a number at all. Rejected before Chromium launches.

Same field, opposite classes. (A nickname shorter than `min_length` also fails input validation, so
keep the nickname valid or both runs return the same error.)

---

## 6. A human taking over

```bash
env -u ANTHROPIC_API_KEY python -m interface_cua.cli replay \
  artifacts/open-sub-account-review-v1.yaml \
  --input member_id=55503 --input account_type=savings \
  --allow-draft --headed --handoff
```

A Chromium window opens and the run stops on an expired session. An operator page appears at
<http://127.0.0.1:8765> with two buttons, Resume and Abort. **Open it in a different browser** — the
Chromium window is the live session and it is yours to drive.

To watch a resume succeed: go to <http://127.0.0.1:8000/>, type `58431` into the box, **do not press
Search**, then click Resume. The step does the clicking.

To watch it refuse: leave the browser somewhere else and click Resume. The step re-checks its own
precondition and stops again rather than trusting the click.

Everything you do is recorded by role and accessible name, with values masked inside the page before
they reach the log.

---

## 7. Everything offline

```bash
make verify
```

Lint, the full suite, and schema generation. Expect `83 passed`. The suite runs with
`ANTHROPIC_API_KEY` removed from the environment, which is how "no model in replay" is checked
rather than asserted.
