# Interface CUA

A computer-use automation system in which a model discovers workflows and saves them as typed,
reviewable capabilities. Production replay follows the artifact deterministically, with no model in
the decision loop.

This repository is under active implementation. The current vertical slice includes:

- the architectural invariants and Pydantic v2 artifact/result contracts;
- Meridian CU, a synthetic FastAPI/Jinja target with all eight planned fault records;
- a shared fail-closed policy engine, deterministic redaction, and content-scanner seam;
- a single-controller session lease;
- a Playwright surface, semantic unique-target resolution, and an LLM-free replay executor;
- a draft `open-sub-account-review` artifact and real Chromium tests covering success, declared
  business outcomes, escalation, interstitial recovery, and an undeclared refusal.

### The claim this slice exists to demonstrate

Members `55501` and `55506` both stop on a **"Permission denied"** panel. The two screens are
byte-for-byte identical apart from one sentence, carry the same HTTP status, and contain no
machine-readable outcome code. One is a legitimate answer about the member; the other is a defect in
our own entitlements. Replay tells them apart because the capability **declared** one of them in
`postcondition.any_of` and not the other — not because anything in the executor reads page copy.
`grep -rn "MEMBER_\|SESSION_\|AUTHORIZATION_" src/` returns nothing.

## Setup

Python commands intentionally run only in the `codingtest` Conda environment.

```bash
conda activate codingtest
make install
make install-browser
```

No API key is needed for the demo app, schema generation, or replay tests. Live discovery is not in
this slice yet; when added, it will read `ANTHROPIC_API_KEY` from the environment and never from a
committed file.

## Run and verify

```bash
make help
make demo-app       # http://127.0.0.1:8000
make schema         # artifacts/schema.json
make test           # explicitly removes ANTHROPIC_API_KEY
make lint
```

The browser test launches real headless Chromium and drives the same semantic artifact used by
replay:

```bash
make test-browser
```

## Demo path

With `make demo-app` running in another shell, replay the capability against each seeded fault. No
API key is set on any of these — that is the point, not an accident.

```bash
make replay MEMBER=58431   # success            → member_name + current_balance
make replay MEMBER=99999   # business_outcome   → MEMBER_NOT_FOUND      (declared)
make replay MEMBER=55501   # business_outcome   → MEMBER_RESTRICTED     (declared)
make replay MEMBER=55502   # success            → 6s stall absorbed by a bounded condition wait
make replay MEMBER=55504   # success            → declared interstitial dismissed, run continues
make replay MEMBER=55503   # needs_human        → SESSION_EXPIRED       (declared escalation)
make replay MEMBER=55506   # failure            → undeclared refusal, retryable:false
```

Exit codes are part of the contract: a declared business outcome is a **successful invocation**
(`0`), because the capability answered the question it was asked. Only states that need an operator
are non-zero — `needs_human` is `2`, `unknown_side_effect` is `3`, `validation_required` is `4`.

`make replay` passes `--allow-draft`, because the checked-in artifact is still `draft`. Drop the
flag and replay refuses to start with `validation_required` / `check: approval`.

The checked-in artifact remains `draft` because it was manually bootstrapped, not produced by a
completed discovery run. Normal replay rejects draft artifacts; automated development tests use an
explicit `allow_draft=True` override.

See [the reviewed implementation plan](docs/IMPLEMENTATION_PLAN.md) and
[review notes](docs/PLAN_REVIEW.md) for the remaining build sequence.
