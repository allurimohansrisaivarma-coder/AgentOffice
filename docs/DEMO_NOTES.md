# Demo notes (internal)

Not for the public README — keep the recording checklist here.

## Video checklist (1–4 min)

Show a **full loop**, not only a happy path:

1. Attach a spreadsheet and state the task (try the Arabic prompt on `uae_staff_lookup.xlsx`)
2. Data Profiler / PM plan (and Spec Critic if it forces a revise)
3. **QA rejecting a version**, then Engineer revise, then a passing round
4. Human approval of the **code hash**
5. Download the filled file / audit sheet
6. Call out Zetaris (`upload_cells` or lookup SQL) and Nightshift/Nemotron in the trace

## Side-by-side (≈15 s) — clearest innovation beat

1. Show a **naive exact-match** lookup on `uae_staff_lookup.xlsx` missing rows (`Mohammed` ≠ `محمد`, `Emp-١٢٣` ≠ `Emp-123`)
2. Cut to AgentOffice: Spec Critic rejects a plan without `arabic_names` / `arabic_digits`, then the fixed plan matches **8/8**
3. Say the measured line out loud: *naive name match 0%, AgentOffice 100% (synthetic file)*

Expense claims remains the clearest recorded QA rejection: [`output_examples/task_expense_claims.trace.jsonl`](../output_examples/task_expense_claims.trace.jsonl).
