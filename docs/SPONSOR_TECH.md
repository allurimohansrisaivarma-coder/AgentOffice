# Sponsor technology explanation

The text for the HackOS "sponsor technology" field. Items in **[brackets]** must be filled in after the Meterless Nightshift run. Everything else is true of the code as committed.

## Short version (for a single form field)

AgentOffice uses **Zetaris** as its data layer. Our synthetic workforce tables are registered in a local Zetaris instance as Lightning REST tables, and every number the agents reason over comes from Zetaris SQL. On a live local install, the pipeline's 15 queries return findings identical to our sample run.

**Meterless Nightshift** is the primary model provider. We implemented its provider contract: URL normalization, status from `GET /v1/models`, auto-detect, connect links and a 180-second timeout. Every model call records which model answered, routed to **NVIDIA Nemotron** [route].

We implemented two **Meterless engines** from scratch:
- **World Model**, the shared state that all three stages read and write. Its conflict records are what our Conflict Critic judges.
- **H-MEM**, cross-run memory. A human's rejection changes what the team proposes next.

The code was built with **Claude Code**.

## Full version

### Zetaris: the data layer
- **Role.** Every finding is grounded in Zetaris SQL rows. The agents never read the data any other way in default mode.
- **How the data gets in.** Zetaris can't read local files, and it has no `INSERT` for loading data. So our API serves each synthetic table as JSON (`GET /data/workforce/<table>`). [`scripts/load_zetaris.py`](../scripts/load_zetaris.py) registers those endpoints as **Lightning REST tables**: one `CREATE LIGHTNING DATABASE` statement and five `CREATE LIGHTNING REST TABLE` statements, sent one at a time after a dry run. On our local Zetaris 2.4.3, all six succeeded, and every row count matches the data generator's manifest (12 / 827 / 223 / 2,672 / 8,266).
- **How agents query it.** Agents write portable SQL against `workforce.<table>`. The adapter ([`app/data.py`](../app/data.py)) rewrites each reference to read the REST table, `(SELECT r.* FROM workforce_api.<table> LATERAL VIEW explode(rows) AS r)`, so we needed no views or other database objects. The trace records both the agent's SQL and the executed SQL.
- **The adapter** ([`app/adapters/zetaris.py`](../app/adapters/zetaris.py)) follows the REST contract:
  - `Authorization: Bearer`, `X-Org-ID`, and a fresh UUID for `X-Request-ID`
  - one statement per call, with only read-only statements retried
  - timeouts and structured errors
  - Zetaris's `headers` + `data` rows of strings turned into typed records
- **Verified.** We ran the full pipeline's 15 queries against the live instance, and the findings were identical to the sample-mode run.

### Meterless Nightshift: the primary model provider (Power rule)
- [`app/adapters/meterless.py`](../app/adapters/meterless.py) implements the published provider contract:
  - provider `meterless` / "Meterless Runtime"
  - pasted URLs normalized to `<host>/v1`, and the `rt-local` placeholder rejected
  - `meterless-app` on every request, and `meterless-scope: user` on chat
  - status from `GET /v1/models` (online, unauthorized or offline)
  - auto-detect across ports 8088, 8090, 8787 and 11434
  - connect links (`/connect#k=`)
  - a 180-second timeout
- **Test Connection:** `GET /providers/meterless` and `scripts/check_connections.py --detect`.
- **One seam for every agent.** Each of the 13 agents calls the model through a single injected generator ([`app/llm.py`](../app/llm.py)), so Nightshift serves every agent.
- **Traced.** Each call's trace records the model that actually answered.
- **Live run:** [date, route, and a trace showing Nemotron answering].

### Meterless Engines (Build rule)
- **World Model** ([`app/engines/world_model/`](../app/engines/world_model/)), written from scratch to the Meterless reference design:
  - content-hash stable IDs
  - an append-only observation log that requires provenance back to source rows
  - a 10-step idempotent, resumable ingest
  - canonical reconstruction strategies, merge proposals that are never applied automatically, and a structured, budgeted context builder

  **What it does in the product:** all three stages read their context from it and write what they learn back. When timesheets, tickets and the activity log disagree about a task's hours, the World Model records the conflict with its evidence. Our Conflict Critic judges exactly those records. 18 tests cover it, including re-ingest giving an empty diff, resume after a crash, and concurrent writers matching a serial run.
- **H-MEM** ([`app/engines/hmem/`](../app/engines/hmem/)), written from scratch to the Meterless H-MEM contract:
  - the reference record fields, types and layers
  - a programmable approval boundary
  - conflicts resolved on the record, with the loser `supersededBy` and never deleted
  - the reference 8-signal retrieval formula with a `retrievalReason`
  - a hash-chained trust ledger

  **What it does in the product:** a human's approval decision becomes a long-term memory. On later runs, the PM won't re-propose a routine a human already decided on while alternatives exist, and the Manager escalates rather than re-submit identical rejected code. QA lessons are fed into the Engineer's prompt. A three-session test covers this: learned → retrieved → behaved differently.

### NVIDIA
- **Nemotron** models are served through Nightshift lanes, hosted, with no local GPU. They power every agent's reasoning: planning, SQL writing, code writing, critique and the decision memo.
- **Route used:** [meterless/… route from `GET /v1/models`]. The trace for each run shows which Nemotron model answered.

### AI coding tool
- **We built the whole codebase with Claude Code (Anthropic)**, starting from an empty repository on 23 Oct 2026.
- **How we used it:** scaffolding, adapters, both engines, the agents, tests and the Docker setup. The team reviewed and tested all generated code (158 tests, also run inside the Docker image on Python 3.11).
- **No Meterless or NVIDIA credentials** were ever given to the coding tool.
- **If the form asks how Cursor was used:** we didn't use Cursor. Claude Code was our coding tool.

### What's unique
- **A Zetaris-native data path with no copies or views.** REST tables fed by our own API, with agent SQL rewritten at query time, and verified to match the offline path exactly.
- **A Meterless engine as the evidence for an agent.** The World Model's conflict records are the Conflict Critic's evidence, and H-MEM memories change later decisions, not just the prompt text.
- **Tests that can't flatter the code.** QA derives its expected results from the PM's structured spec, never from the Engineer's code.
