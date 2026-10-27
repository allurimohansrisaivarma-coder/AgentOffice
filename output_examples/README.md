# Saved runs (evidence, not ROI)

These JSON + `.trace.jsonl` files are **real agent runs** (live Nightshift/Zetaris or sample mode) kept so judges can inspect plans, QA and hashes.

Some analysis summaries still contain phrases like “about N h/week saved.” Those numbers are **measured on the synthetic demo activity log** (or the same log loaded into Zetaris) so the Priority Critic has deterministic evidence. They are **not** a claim that AgentOffice saved N hours for a named customer.

For Impact scoring, use:

- Task-mode outcomes (cells filled, QA pass/fail/recover, code-hash approval)
- Arabic key normalisation on lookups
- The UAE demos (`task_uae_site_claims.trace.jsonl`; `scripts/measure_arabic_lookup.py`)

…not the demo-corpus hours in these files.
