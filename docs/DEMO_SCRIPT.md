# Demo video script (target 3:30, hard limit 4:00)

This is a script for a screen recording with voice-over. Each beat lists what's on screen, then what you say. Items in **[brackets]** must be filled in after the Nightshift run.

## Before you press record

- [ ] Docker Desktop is running, and Zetaris is up (`docker ps` shows `zetaris-*` healthy).
- [ ] The API is running in **default mode**:
  ```powershell
  .\.venv\Scripts\python.exe run.py
  ```
- [ ] Nightshift is open, with the Nemotron lanes configured.
- [ ] **Nothing secret is on screen.** Close `.env`, Zetaris **Management → API Tokens**, Nightshift **Setup → Providers**, and any terminal showing a key. Use a fresh terminal.
- [ ] Zoom the terminal font to at least 18 pt, and hide your desktop and taskbar notifications.
- [ ] Do one dry run of every command below, so nothing surprises you.

---

## 0:00–0:20 · The problem

**Screen:** the deck's cover slide, then the "Three systems, three answers" slide.

**Say:** "Small office teams lose hours to the same manual steps every day, but nobody has time to find the routine, prove it's worth fixing, and build the fix safely. Even the data disagrees. In our synthetic dataset, timesheets say invoice matching takes 71.5 hours a week, tickets say 19.5, and the activity log measures 32.9. AgentOffice is a team of agents that sorts this out."

## 0:20–0:45 · Ask the question

**Screen:** a terminal. Run:
```powershell
curl.exe -s -X POST http://localhost:8000/run -H "Content-Type: application/json" --data-binary "@input_examples/example_1.json" -o run1.json
```
Then show the summary:
```powershell
(Get-Content run1.json -Encoding UTF8 | ConvertFrom-Json).result.summary
```

**Say:** "I ask one plain question: where is the finance team losing time, and what should we automate first? Thirteen agents work through three stages: discover, explain, and decide and build. Here's the answer: automate invoice matching, about [N] hours a week, and the automation passed QA."

## 0:45–1:25 · Show how it got there (the trace)

**Screen:** filter the trace to the handoffs and critiques:
```powershell
$log = (Get-Content run1.json -Encoding UTF8 | ConvertFrom-Json).log_path
Get-Content $log -Encoding UTF8 | ConvertFrom-Json | Where-Object { $_.action -match 'critique|handoff|data_query|write_code|decide|request_approval' } | Select-Object agent_name, action, status, model | Format-Table -AutoSize
```

**Say:** "Every step is in a hash-chained trace. Here's the Source Mapper querying Zetaris, the Conflict Critic flagging the three disagreeing sources, and the Insight Critic checking that every number in the report points to a real cell. Then the PM proposes a plan, the Priority Critic [approves it / sends it back], the Engineer writes code, and QA tests it. The model column shows **[Nemotron route]** answering through Meterless Nightshift."

*If the run had a revision, pause on the `needs_revision` row:* "Here QA rejected the first draft, and the Engineer fixed it."

## 1:25–1:55 · Zetaris is the data layer

**Screen:** the Zetaris web UI at http://localhost:3000, in the SQL editor. Run:
```sql
SELECT r.task_category, ROUND(SUM(r.hours) / 6.0, 2) AS hours_per_week FROM workforce_api.timesheets LATERAL VIEW explode(rows) AS r GROUP BY r.task_category ORDER BY hours_per_week DESC
```

**Say:** "All the data lives in Zetaris. Our synthetic tables are registered as Lightning REST tables. The agents write plain SQL, and the adapter turns it into reads like this one. Every number the agents used came from queries like this."

## 1:55–2:30 · QA tests the spec, not the code

**Screen:** open `run1.json` and scroll to `result.automation.qa.checks`, or show the deck's "QA tests the spec" slide.

**Say:** "The PM doesn't write prose. It writes a spec in a small rule language. QA works out the expected results from the spec alone: every branch, tolerance edges, and a renamed column that must fail loudly. Then it runs the code in a sandbox, so QA can't just agree with buggy code."

## 2:30–3:10 · Memory changes behaviour

**Screen:** reject the proposal as the human approver:
```powershell
$apr = (Get-Content run1.json -Encoding UTF8 | ConvertFrom-Json).approval.approval_id
@{decision="reject"; approver="finance manager"; note="ERP writes need my sign-off first"} | ConvertTo-Json | Set-Content reject.json
curl.exe -s -X POST "http://localhost:8000/approvals/$apr" -H "Content-Type: application/json" --data-binary "@reject.json"
```
Then ask a team-neutral question, and show the new choice and the memory that was applied:
```powershell
@{request_id="demo-2"; user_query="Which repetitive work should we automate first?"} | ConvertTo-Json | Set-Content q2.json
curl.exe -s -X POST http://localhost:8000/run -H "Content-Type: application/json" --data-binary "@q2.json" -o run2.json
$r2 = Get-Content run2.json -Encoding UTF8 | ConvertFrom-Json; $r2.result.automation.routine; $r2.memory.human_decisions_applied
```

**Say:** "Now I reject it, as the finance manager, with a reason. That decision goes into H-MEM, the team's long-term memory. On the next run, the team remembers and proposes a different routine. And if I ask about Finance again, it escalates instead of asking me to approve the same rejected code."

## 3:10–3:30 · Wrap up

**Screen:** the closing slide.

**Say:** "AgentOffice is a team that argues, tests and remembers, and asks before it acts. It's built on Zetaris as the data layer, Meterless Nightshift as the model provider with Nemotron, and two Meterless engines, World Model and H-MEM. The code is on GitHub, and it runs without keys in sample mode. Thanks for watching."

---

## Checks after recording

- [ ] It's 4:00 or shorter.
- [ ] No key, token or password is visible in any frame. Scrub through at 2× speed.
- [ ] The video link opens in a private or incognito window.
- [ ] Everything you said matches what the code did in that recording.
