# Sponsor technology explanation

The text for the HackOS "sponsor technology" field. Everything here is true of the code as committed; the live runs are saved in [`output_examples/`](../output_examples/).

## Short version (for a single form field)

AgentOffice is an AI office team you tell what to do: attach a spreadsheet, describe the repetitive work, and the agents plan it, write the automation, test it on your data and ask you to approve before producing the filled file. It also analyses where a team's time goes and what to automate first.

**Zetaris** is the data layer for both. The company's (synthetic) workforce tables *and every uploaded spreadsheet* are Lightning REST tables in a local Zetaris instance; agents profile uploads, fetch lookup data and ground every finding with Zetaris SQL.

**Meterless Nightshift** is the primary model provider for every agent (route `meterless/deep` → **NVIDIA Nemotron** `nvidia/nemotron-3-ultra-550b-a55b`; each trace records the model that answered). We implemented its provider contract: URL normalization, status from `GET /v1/models`, auto-detect, connect links and a 180-second timeout.

We implemented two **Meterless engines** from scratch: the **World Model** (shared state all agents read and write; its conflict records are the Conflict Critic's evidence) and **H-MEM** (cross-run memory: a human's approval lets the same automation run again without asking; a rejection changes what the team proposes next).

The code was built with **Claude Code**.

## Full version

### Zetaris: the data layer
- **Role.** In default mode the agents read data only through Zetaris SQL: the workforce tables for analysis, uploaded spreadsheets for task mode, and company reference data (for example the staff directory) for lookups.
- **How the data gets in.** Zetaris can't read local files and has no `INSERT` for loading data. So our API serves JSON and Zetaris pulls it as **Lightning REST tables** ([`scripts/load_zetaris.py`](../scripts/load_zetaris.py): one `CREATE LIGHTNING DATABASE` and six `CREATE LIGHTNING REST TABLE` statements, sent one at a time after a dry run):
  - `GET /data/workforce/<table>`: five synthetic tables; row counts verified against the generator's manifest (12 / 827 / 223 / 2,672 / 8,266).
  - `GET /data/uploads/cells`: every recent upload as long-format cells (`upload_id, sheet_name, row_no, col_name, cell_value, value_type`). Zetaris re-reads it at query time, so a file is queryable the moment it's uploaded (verified: upload, then `COUNT`/`SUM` by column through Zetaris, immediately).
- **How agents query it.** Agents write portable SQL against `workforce.<table>`. The adapter ([`app/data.py`](../app/data.py)) rewrites each reference to read the REST table, `(SELECT r.* FROM workforce_api.<table> LATERAL VIEW explode(rows) AS r)`, so no views or other objects are needed. The trace records both the agent's SQL and the executed SQL.
  - The **Data Profiler** profiles an upload with Zetaris SQL (blanks, numbers, distinct values per column, exact values of small columns) before the PM plans anything.
  - The PM's lookup steps carry their own SQL (e.g. `SELECT email, team, role FROM workforce.people`); the **Retriever** runs it on Zetaris and the **Spec Critic** checks the result covers the file's keys.
  - The analysis pipeline's 15 queries run on the live instance with findings identical to sample mode.
- **The adapter** ([`app/adapters/zetaris.py`](../app/adapters/zetaris.py)) follows the REST contract: `Authorization: Bearer`, `X-Org-ID`, a fresh UUID `X-Request-ID`, one statement per call, only read-only statements retried, timeouts and structured errors, and Zetaris's `headers` + string `data` turned into typed records.

### Meterless Nightshift: the primary model provider (Power rule)
- [`app/adapters/meterless.py`](../app/adapters/meterless.py) implements the published provider contract: provider `meterless` / "Meterless Runtime"; pasted URLs normalized to `<host>/v1` and the `rt-local` placeholder rejected; `meterless-app` on every request and `meterless-scope: user` on chat; status from `GET /v1/models`; auto-detect across ports 8088, 8090, 8787 and 11434; connect links (`/connect#k=`); a 180-second timeout.
- **Test Connection:** `GET /providers/meterless` and `scripts/check_connections.py --detect`.
- **One seam for every agent.** Every agent calls the model through one injected generator ([`app/llm.py`](../app/llm.py)), so Nightshift serves all of them: the front desk, PM, engineer, QA, manager and memory keeper in task mode, and the thirteen analysis agents.
- **Reasoning models handled properly.** Nemotron thinks before answering; we strip the reasoning (including replies with only a closing `</think>`), give a larger token budget, detect replies cut off at the length limit (`finish_reason: length`) and ask for a compact retry.
- **Live runs (25 Oct 2026):** all three analysis examples and the three spreadsheet tasks ran in default mode; traces in `output_examples/*.trace.jsonl` show `nvidia/nemotron-3-ultra-550b-a55b` answering through `meterless/deep`.

### Meterless Engines (Build rule)
- **World Model** ([`app/engines/world_model/`](../app/engines/world_model/)), written from scratch to the Meterless reference design: content-hash stable IDs; an append-only observation log that requires provenance; a 10-step idempotent, resumable ingest; canonical reconstruction; merge proposals never applied automatically; a structured, budgeted context builder.

  **In the product:** every analysis stage reads its context from it and writes back; when timesheets, tickets and the activity log disagree, the World Model records the conflict with evidence and the Conflict Critic judges exactly those records. Task mode records each spreadsheet, automation and their link. 18 tests (re-ingest gives an empty diff, resume after a crash, concurrent writers match a serial run).
- **H-MEM** ([`app/engines/hmem/`](../app/engines/hmem/)), written from scratch to the Meterless H-MEM contract: the reference record fields, types and layers; a programmable approval boundary; conflicts resolved on the record (loser `supersededBy`, never deleted); the reference 8-signal retrieval formula with a `retrievalReason`; a hash-chained trust ledger.

  **In the product:** a human's approval decision becomes a long-term memory. In task mode the Memory Keeper hands the PM the automations a human approved; the PM can reuse one, QA re-tests it on the new file, and because the code hash matches an approval on record the Operator runs it without asking again. In analysis mode the PM won't re-propose a routine a human already decided on while alternatives exist, and the Manager escalates rather than re-submit rejected code. QA lessons feed the Engineer's prompt.

### NVIDIA
- **Nemotron** (`nvidia/nemotron-3-ultra-550b-a55b`) is served through the Nightshift route `meterless/deep`, hosted, with no local GPU. It powers every agent's reasoning: routing, planning specs, SQL, code writing and revision, test design, code review, critique and the decision memo.

### AI coding tool
- **We built the whole codebase with Claude Code (Anthropic)**, from an empty repository on 23 Oct 2026: scaffolding, adapters, both engines, the agents, the task mode, the chat UI and desktop island, tests and the Docker setup. The team reviewed and tested all generated code (176 tests).
- **No Meterless or NVIDIA credentials** were ever given to the coding tool.
- **If the form asks how Cursor was used:** we didn't use Cursor; Claude Code was our coding tool.

### What's unique
- **Your spreadsheet becomes a Zetaris table.** Uploads are profiled and joined with company data through the same Zetaris SQL path as everything else, with no copies or views.
- **Tests that can't flatter the code.** The PM writes a structured step spec; QA computes expected output from that spec with an independent reference runtime and checks the Engineer's code against your real rows, edge cases and a renamed column.
- **Approval bound to code, remembered.** Approving releases exactly one code hash; H-MEM remembers it, so the same job next week runs straight away, and anything new waits for a human.
