# Design inspiration (ideas only, no code)

These are design patterns worth reimplementing from scratch. **No code is copied into this repo.** If code is ever copied from an earlier project, it must be declared on the HackOS form as a pre-existing component.

## 1. Detect → Generate → Test → Repair → Approve → Run

The core loop for stage 3 (Decide & Build):

1. **Detect** what is repeated (in our case, from Zetaris workforce data).
2. **Confirm a plan**: a structured spec of the automation (target rows, condition, action), not free text.
3. **Generate** code from the plan using the model.
4. **Test** the code against expectations derived **independently from the plan**, not from the code. This is the key idea: the test can't simply agree with whatever the code does.
5. **Repair**: send the failures back to the generator as feedback, with a hard cap (e.g. 3 attempts).
6. **Approve**: a human approves, bound to the exact code hash and its passing test report.
7. **Dry run** (show the diff, write nothing), then **run**, then make it possible to **undo** the run.

## 2. A QA checklist for generated code

Things an Evaluator/QA agent can check on its own, cheapest first:
- no forbidden constructs (imports, `eval`, network, file system outside a workspace)
- it compiles or parses
- no hardcoded credentials
- the output has the right shape
- it selects the same rows and action the plan expects
- it only references columns that actually exist
- no personal data in the output
- it gives the same result on a second run (determinism)
- **injected drift:** rename a column on a copy of the data, and check the code doesn't silently "pass" (this catches code that memorised the test data)

Each failed check becomes a structured critique message the Executor must address. Those messages are exactly what judges want to see in traces.

## 3. Sandbox for running generated code

- Run it in a child process with a timeout, input passed over stdin, and no environment secrets.
- Allow only a whitelist of modules and attributes.
- Cap memory and forbid child processes where the OS allows it. An optional Docker mode can add no network and a read-only root, and should fail closed if Docker is missing.
- Keep a small "attack" test set (network, file escape, fork bomb, memory bomb) as proof it works.

## 4. Privacy before any model call

- Mask or pseudonymise personal fields before a prompt leaves the machine, using reversible tokens so the answer can be mapped back.
- Store names and IDs, never raw values, in captured events.

## 5. Trust and audit

- Use a hash-chained, append-only log for decisions and approvals: each entry includes the previous entry's hash, so tampering is detectable. This fits the JSONL trace requirement well.
- Record who approved what, and which code hash was approved.

## 6. Routine detection

- Group events into per-row "instances" of work, then find sequences that repeat across instances.
- Stitch interrupted instances back together, since real work gets interrupted.
- An instance only counts as finished once there is a "done" signal or the row is touched again later.

## 7. Honest README habits

- Have a "Done vs not yet" section and "Known limits" in the README, each verified against the code.
- Every number in the README comes from a script anyone can rerun.
