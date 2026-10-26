# HTTP API

Everything the chat UI does is plain HTTP on the local service (`http://localhost:8000` by default), so you can drive AgentOffice from a script with no UI at all. Every failure comes back as a structured error, never a stack trace:

```json
{"status": "error", "error": {"type": "not_found", "message": "no task 'task_x'", "recoverable": false}, "trace_id": null}
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | The chat UI (island and panel). `?shell=desktop` is the variant the desktop app loads. |
| `POST /uploads?filename=<name>` | Upload a CSV or XLSX as the raw request body. Returns the `upload_id`, sheets, columns, row counts and a preview. `GET /uploads/{id}` returns the same. |
| `GET /samples` | The three demo files (`demo_expense_claims`, `demo_vendor_invoices`, `demo_leads_and_contacts`), each with a suggested request. |
| `POST /chat` | `{"message", "upload_id"?, "options": {"sample_mode"}}`. Starts a background job and returns `{job_id}`. The front desk routes the message to task mode, the analysis pipeline, a question, an email draft or a plain reply. The finished job's `result.type` is `task`, `analysis`, `assist` (questions and email drafts: `result.assist` holds `kind`, `answer` or `to`/`subject`/`body`, `evidence` and `grounding`) or `reply`. |
| `GET /jobs/{id}?after=<n>` | The job's live agent events from event `n` on, its `status` (`running`, `completed` or `failed`), and then its `result` (`type` is `task`, `analysis`, `assist` or `reply`) or a structured `error`, plus the `trace_id`. |
| `GET /tasks/{id}` | A spreadsheet task: request, understanding, spec, references, code and hash, QA checks and preview, the Manager's memo, approval and outputs. |
| `GET /tasks/{id}/code` | The automation as a `.py` file. |
| `POST /tasks/{id}/decision` | `{"decision": "approve" \| "reject", "approver", "note", "code_hash"}`. Records the decision in the approval ledger and in H-MEM. **Approving runs exactly that code** on the file and returns the outputs. A `code_hash` that doesn't match returns 409. |
| `POST /tasks/{id}/run` | Runs an approved task again. |
| `GET /files/{file_id}` | Downloads a filled workbook (`.xlsx`) or `.csv`. |
| `POST /run` | The analysis pipeline, synchronous. See [below](#post-run). |
| `GET /approvals` | Every approval, newest first; `?status=pending` filters. |
| `GET /approvals/{id}`, `POST /approvals/{id}` | The human approval gate, with the same body as the task decision. It's hash-chained and bound to the code hash, and the decision becomes a long-term H-MEM preference that later runs respect. For automations from analysis mode, approving only records the decision; nothing is deployed. |
| `GET /traces`, `GET /traces/{run_id}` | Past runs on this machine, newest first, read from their own trace files in `logs/` (the committed `output_examples/` are never read). A run's events come back for the Team pane's replay, with `chain_intact` (the hash chain still verifies) and the sponsor calls it actually made. |
| `GET /health` | Configuration status: mode, provider, missing settings and the addresses in effect. Makes no external calls. |
| `GET /config` | What the UI shows: mode, model route, data source and hotkeys. No secrets. |
| `GET /providers/meterless` | Test Connection for the primary model provider. The token is never returned. |
| `GET /data/workforce/{table}` | The synthetic workforce tables, served for Zetaris to register as REST tables. |
| `GET /data/uploads/cells` | Recent uploads as long-format cells, the Zetaris REST table `upload_cells`. |

## Sample mode over the API

The UI switches to sample mode by itself when keys are missing; an API caller has to ask for it. Without keys, add `"options": {"sample_mode": true}` to `POST /chat` and `POST /run`, or set `SAMPLE_MODE=true` for the service. Otherwise the job fails with `agent_not_configured`, and the message lists the missing settings.

## Example: a spreadsheet task from start to finish

These run in Windows PowerShell against a service in sample mode, using the bundled demo file `demo_vendor_invoices`.

```powershell
$api = "http://localhost:8000"

# 1. Send the request with the file. Use POST /uploads first for a file of your own.
$chat = Invoke-RestMethod "$api/chat" -Method Post -ContentType "application/json" -Body (@{
    message   = "Remove duplicate invoices, fill blank statuses with Pending, mark unpaid invoices due before 2026-10-15 as Overdue and sort by due date."
    upload_id = "demo_vendor_invoices"
    options   = @{ sample_mode = $true }
} | ConvertTo-Json)

# 2. Wait for the team to plan, build and test the automation.
do { Start-Sleep 1; $job = Invoke-RestMethod "$api/jobs/$($chat.job_id)" } while ($job.status -eq "running")
$task = $job.result.task
"$($task.status): QA passed $($task.qa.cases) cases, code hash $($task.code_hash)"

# 3. Approve exactly that code. AgentOffice runs it and writes the filled file.
if ($task.status -eq "awaiting_approval") {
    $task = Invoke-RestMethod "$api/tasks/$($task.task_id)/decision" -Method Post -ContentType "application/json" -Body (@{
        decision = "approve"; approver = "Your Name"; code_hash = $task.code_hash
    } | ConvertTo-Json)
}

# 4. Download the result.
$file = $task.outputs.files[0].file_id
Invoke-WebRequest "$api/files/$file" -OutFile $file
```

Run it a second time and step 3 is skipped: H-MEM remembers that you approved this exact code, so the task comes back `completed` (with `reused` naming the automation) and already has its outputs. Approving the same approval twice returns 409.

To upload a file of your own (step 1), send its bytes as the body:

```powershell
$upload = Invoke-RestMethod "$api/uploads?filename=claims.xlsx" -Method Post -InFile .\claims.xlsx -ContentType "application/octet-stream"
$upload.upload_id
```

## `POST /run`

The analysis pipeline as one synchronous call: the hackathon's programmatic entry point.

```bash
curl -X POST http://localhost:8000/run -H "Content-Type: application/json" -d @input_examples/example_1.json
```

In Windows PowerShell, type `curl.exe` instead of `curl` and quote the file argument: `-d "@input_examples/example_1.json"`. The examples in [`input_examples/`](../input_examples/) ask for default mode; to run one without keys, set `"sample_mode": true` in its `options`.

**Request:** `{"request_id", "user_query", "context", "options": {"sample_mode", "stages": ["discover", "explain", "build"]}}`. `stages` is optional and defaults to all three.

**Response:**

| Field | What it holds |
|---|---|
| `status` | `success`, or `error` with a structured `error{type, message, recoverable}` |
| `agents_used` | the agents that ran |
| `result` | `summary`, `findings` (each citing the SQL rows behind it), `confidence`, `ranking`, `decision`, `automation` |
| `collaboration` | `feedback_loop_completed`, `revision_count`, `evaluator_findings`, and the other critique details |
| `stages`, `approval`, `memory`, `usage`, `mode` | per-stage detail, the approval request, what H-MEM recalled and stored, call counts, and the mode used |
| `trace_id`, `log_path` | the run's trace, `logs/trace-<run_id>.jsonl` |
| `execution_time_seconds` | wall time |

Real responses with their traces are in [`output_examples/`](../output_examples/).
