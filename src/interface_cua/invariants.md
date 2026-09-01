# Architectural invariants

1. **No LLM decision during replay.** Enforced by a test that runs replay with
   `ANTHROPIC_API_KEY` unset.
2. **No unique target → no action.** A target resolves only if some strategy on its ladder matches
   exactly one element. A strategy matching two is recorded and skipped — never resolved by picking
   one of them — and if no strategy is unique the step fails. Two matches is a safety failure,
   never a coin flip.
3. **No checkpoint verified → no progress** to the next step.
4. **Never blind-retry an action whose side effect is uncertain.**
5. **Exactly one controller owns a session** — `AUTOMATION` or `HUMAN`, never both.
6. **Business outcome ≠ system failure**, and the artifact decides which is which.
7. **Artifacts store `${inputs.member_id}`, never a value.** Logs redact to last-4.
8. **Policy is evaluated below the model** — discovery and replay call the same `authorize()`.

