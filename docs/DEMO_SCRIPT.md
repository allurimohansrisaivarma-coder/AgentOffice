# Demo video script (target 3:30, hard limit 4:00)

A script for a screen recording with voice-over. Each beat lists what's on screen, then what you say. Live runs with Nemotron take a few minutes per task, so **record the live run once beforehand** and cut the waiting, or say that the video is sped up while the agents work.

## Before you press record

- [ ] Docker Desktop is running, and Zetaris is up (`docker ps` shows `zetaris-*` healthy).
- [ ] The API is running in **default mode**: `.\.venv\Scripts\python.exe run.py`
- [ ] Nightshift is open, with the NVIDIA provider connected (route `meterless/deep` → `nvidia/nemotron-3-ultra-550b-a55b`).
- [ ] The desktop island is running: `.\.venv\Scripts\python.exe scripts\island.py`
- [ ] **Nothing secret is on screen.** Close `.env`, Zetaris **Management → API Tokens**, Nightshift **Setup → Providers**, and any terminal showing a key.
- [ ] Hide desktop notifications. Have Excel (or LibreOffice) ready to open the downloaded file.
- [ ] Do one dry run of the whole flow so nothing surprises you.

---

## 0:00–0:20 · The problem

**Screen:** the deck's cover slide, then your normal desktop with the island at the top.

**Say:** "Office teams fill the same spreadsheets by hand every week: look up who's in which team, fix dates, tidy categories, flag the big ones, total it up. AgentOffice is an AI office team you can just tell what to do. It plans the work, writes the automation, tests it on your real data, and asks you before it touches anything."

## 0:20–0:40 · Open it like Copilot

**Screen:** press **Ctrl+Shift+Space**. The island opens into the chat panel over whatever app is open.

**Say:** "It lives at the top of the screen like a dynamic island. One shortcut opens it from any app."

## 0:40–1:30 · Give it a task and a file

**Screen:** drag `data/sample/tasks/expense_claims.xlsx` onto the panel (or click the demo card) and send:
> Fill in each employee's team and role from our staff directory, tidy the category names, convert the dates to YYYY-MM-DD, add the amount including 10% GST, flag claims over 500 for approval and give me totals by team on a separate sheet.

Let the **Team at work** timeline run; collapse the panel with Esc for a moment to show the island reporting progress ("Engineer · wrote the automation"), then reopen it.

**Say:** "The front desk routes the request. The data profiler reads my file *through Zetaris*: the upload is a Zetaris REST table, so it's profiled with SQL like any company data. The PM turns my sentence into a precise step plan, and a spec critic checks it against the real file. The staff directory comes from Zetaris too. The engineer, Nemotron through Meterless Nightshift, writes the Python. QA tests it against expectations computed from the plan, never from the code: my real rows, edge cases, and a renamed column that must fail loudly."

## 1:30–2:15 · Review and approve

**Screen:** the approval card: plan steps, QA stats (cases, pass rate, rounds), the preview with new columns highlighted, the *Totals by Team* sheet, the Manager's note, and "View the code".

**Say:** "Before anything is produced, I see the plan, a preview of my file after the change, and QA's results. The manager notes that two emails aren't in the directory. Approving runs exactly this code: the approval is bound to its hash."

Type your name, click **Approve & run**, then click **Filled spreadsheet** and open it in Excel. Show the *Claims* sheet, *Totals by Team*, and the *AgentOffice* sheet (approver, approval ID, code hash).

**Say:** "Here's my filled spreadsheet, with an audit sheet recording who approved which code."

## 2:15–2:45 · It remembers

**Screen:** next week's file: send the same request on the same file again (or a new month's claims with the same columns).

**Say:** "Next week I ask again. H-MEM, the team's long-term memory, remembers I approved this automation. The PM reuses it, QA re-tests it on the new file, and because it's the exact code I approved, it runs straight away and hands me the file."

## 2:45–3:15 · It also finds what to automate

**Screen:** ask: *"Where is the finance team losing time, and what should we automate first?"* Show the analysis card (findings, ranking, decision). If time is short, show a saved run from `output_examples/example_1.json`.

**Say:** "Ask it where the time goes, and thirteen agents analyse the workforce data in Zetaris: they reconcile three systems that disagree, explain why with cited rows, and propose the automation worth building first, here invoice matching at about 24 hours a week."

## 3:15–3:30 · Wrap up

**Screen:** the closing slide.

**Say:** "AgentOffice is a team that plans, tests, remembers, and asks before it acts. Zetaris is the data layer for your files and the company's data, Meterless Nightshift serves Nemotron to every agent, and two Meterless engines, World Model and H-MEM, hold what the team knows. It runs without keys in sample mode. Thanks for watching."

---

## Fallback without the desktop island

The same UI runs in any browser at http://localhost:8000 (Ctrl+Shift+Space or Ctrl+K opens the island). Sample mode needs no keys: `$env:SAMPLE_MODE = "true"; .\.venv\Scripts\python.exe run.py`.

## Checks after recording

- [ ] It's 4:00 or shorter, and any sped-up waiting is labelled.
- [ ] No key, token or password is visible in any frame. Scrub through at 2× speed.
- [ ] The video link opens in a private or incognito window.
- [ ] Everything you said matches what the code did in that recording.
