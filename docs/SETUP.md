# Setup guide

AgentOffice runs on your own computer. A small local service (`run.py`) hosts the agents, and you use it in one of three ways: the **Windows desktop app**, the same chat UI in **any browser**, or the **HTTP API**.

It starts in **sample mode**, which needs no keys and no network: the real agents run on the bundled synthetic data. **Default mode** connects the two sponsor services (Zetaris for data, Meterless Nightshift for the model) and works on your own files.

- [What you need](#what-you-need)
- [1. Install](#1-install)
- [2. Start it](#2-start-it)
- [3. Try it in sample mode](#3-try-it-in-sample-mode)
- [4. Default mode: connect Zetaris and Nightshift](#4-default-mode-connect-zetaris-and-nightshift)
- [Configuration reference](#configuration-reference)
- [Where AgentOffice keeps its data](#where-agentoffice-keeps-its-data)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)

## What you need

| | Sample mode | Default mode |
|---|---|---|
| **Python 3.11 or newer** ([python.org](https://www.python.org/downloads/)) | yes | yes |
| **Git** | yes | yes |
| **Microsoft Edge or Google Chrome** | for the desktop app (Edge is on every Windows 11 PC) | same |
| **Docker Desktop** with 8 GB RAM and 4 CPUs for Docker, plus 15 GB free disk | no | yes, for Zetaris |
| **Zetaris** (local install) and **Meterless Nightshift** with an NVIDIA API key | no | yes |

The desktop app is Windows-only. On macOS and Linux, use the browser UI or Docker.

## 1. Install

### Windows

```powershell
git clone https://github.com/allurimohansrisaivarma-coder/AgentOffice.git
cd AgentOffice
powershell -ExecutionPolicy Bypass -File setup.ps1
```

[`setup.ps1`](../setup.ps1) does the whole first-time setup:

1. finds Python 3.11 or newer (the `py` launcher first, then `python` on `PATH`)
2. creates the virtual environment in `.venv`
3. installs the dependencies from `requirements.txt`
4. creates your private `.env` from `.env.example` (an existing `.env` is never overwritten)
5. tells you which mode AgentOffice will run in, and which settings default mode still needs
6. adds an **AgentOffice** shortcut to the Desktop and the Start menu

It is safe to run again, for example after `git pull`.

| Option | What it adds |
|---|---|
| `-Dev` | installs pytest and runs the test suite (about 15 seconds) |
| `-Startup` | also starts AgentOffice when you sign in to Windows |
| `-NoShortcut` | skips the shortcuts |

### macOS and Linux

```bash
git clone https://github.com/allurimohansrisaivarma-coder/AgentOffice.git
cd AgentOffice
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

### Docker (any OS)

The image targets Python 3.11.

```bash
docker build -t oah-submission .
docker run --rm -p 8000:8000 -e SAMPLE_MODE=true oah-submission
```

For default mode, pass your settings instead: `docker run --rm -p 8000:8000 --env-file .env oah-submission`.

Inside the container, `localhost` would mean the container itself, so `localhost` and `127.0.0.1` URLs in `.env` are rewritten to `host.docker.internal`, where Zetaris and Nightshift run. `GET /health` shows the addresses in effect, and `AGENTOFFICE_NO_HOST_REWRITE=true` turns the rewrite off.

## 2. Start it

**Desktop app (Windows).** Open **AgentOffice** from the Start menu or the Desktop. With `-NoShortcut`, run `.venv\Scripts\pythonw.exe AgentOffice.pyw` instead.

- A dark pill appears at the top of the screen. **Ctrl+Shift+Space** opens it from any app; **Esc** or clicking elsewhere folds it back.
- The service runs hidden in the background and writes its log to `logs/agentoffice-server.log`. There's no terminal window and no browser tab.
- The power button in the panel quits everything. Opening AgentOffice a second time just opens the island.
- Windows Firewall may ask about `pythonw.exe` the first time. Allow it on private networks: in default mode, Zetaris (in Docker) reads your uploads from the service.

**Browser (any OS).**

```powershell
.\.venv\Scripts\python.exe run.py
```

Use `.venv/bin/python run.py` on macOS and Linux. Then open http://localhost:8000. Click the pill, or press **Ctrl+Shift+Space** or **Ctrl+K**.

**API.** Everything the UI does is plain HTTP. See [API.md](API.md).

## 3. Try it in sample mode

With no keys in `.env`, the UI switches to sample mode by itself; the badge in the panel says so.

1. Open the panel and pick one of the three demo files (or drop one from [`data/sample/tasks/`](../data/sample/tasks/)). Each comes with a suggested request; for example, for `expense_claims.xlsx`:
   > Fill in each employee's team and role from our staff directory, tidy the category names, convert the dates to YYYY-MM-DD, add the amount including 10% GST, flag claims over 500 for approval and give me totals by team on a separate sheet.
2. Watch the team at work: profiler, PM, spec critic, engineer, QA and manager each post their step.
3. Review the approval card: the plan, the QA results, and a preview of your file after the change. Type your name and click **Approve & run**.
4. Download the filled spreadsheet. Its **AgentOffice** sheet records who approved which code.
5. Ask an analysis question without a file:
   > Where is the finance team losing time, and what should we automate first?

In sample mode no model is called. Each agent uses a deterministic fallback, and only the three demo files can be planned; any other file returns `agent_no_offline_plan`. Your own files need default mode.

To run all the saved examples without the UI:

```powershell
.\.venv\Scripts\python.exe scripts\run_examples.py --sample
.\.venv\Scripts\python.exe scripts\run_examples.py --sample --tasks --approver "your name"
```

These write to `output_examples/sample_mode/`.

## 4. Default mode: connect Zetaris and Nightshift

### 4.1 Zetaris (the data layer)

1. Sign in at cloud.enterprise.zetaris.com, open the **Run locally** page, and download the `zetaris-platform.zip` bundle and the registry credentials.
2. In the unzipped folder, run `docker login zetarishackathon.azurecr.io` and `./preflight.sh`.
3. Set the admin variables in the platform's own `.env` **before the first start**. That file belongs to Zetaris; never copy it into this repository.
4. Start it with `docker compose pull`, then `docker compose up -d`, and check it with `./verify.sh` (9/9).
5. Open the web UI at http://localhost:3000 and create an API key under **Management → API Tokens**. Registering the tables (step 4.4) needs an administrator key.

The REST API is at `http://localhost:8888/api/v1.0`, and the organisation ID of a local install is `1`. Don't use port 8889; it's internal to Zetaris.

### 4.2 Meterless Nightshift (the model provider)

1. Download and install Nightshift from [meterless.ai](https://meterless.ai), and keep it running.
2. Create an NVIDIA API key at build.nvidia.com and add it in Nightshift under **Setup → Providers → NVIDIA**. The NVIDIA key lives only there; AgentOffice never needs it.
3. Add Nemotron models under **NIGHT → LANES**.
4. Copy the runtime URL and token from the **Console** (top right), or copy the connect link.

AgentOffice was run with `METERLESS_MODEL=meterless/deep`, which Nightshift routed to `nvidia/nemotron-3-ultra-550b-a55b` (and a few analysis calls to `nvidia/nemotron-3.5-lightning-30b-a3b`). Use any route that `GET /v1/models` lists. `meterless/auto` works too, but it may route to a model that isn't from NVIDIA.

### 4.3 Fill in `.env` and check both services

Set at least these in `.env`:

```dotenv
ZETARIS_API_KEY=<the key from step 4.1>
METERLESS_RUNTIME_TOKEN=<the token from step 4.2>
METERLESS_MODEL=meterless/deep
```

Instead of the URL and the token, you can paste the connect link: `METERLESS_CONNECT_LINK=http://<host>:<port>/connect#k=<key>`. Then check both services:

```powershell
.\.venv\Scripts\python.exe scripts\check_connections.py --detect
```

It runs `SELECT 1` and `SHOW LIGHTNING DATABASES` on Zetaris, then lists Nightshift's models and makes one short chat call, reporting the model that answered. Every check must say `PASS`. Secrets are never printed.

### 4.4 Register the data in Zetaris

Zetaris can't read files on your computer, so it pulls them from the AgentOffice service as REST tables. Start the service first (the desktop app, or `run.py`), then:

```powershell
.\.venv\Scripts\python.exe scripts\load_zetaris.py           # dry run: prints every statement, runs nothing
.\.venv\Scripts\python.exe scripts\load_zetaris.py --apply   # runs them one at a time, then checks the row counts
.\.venv\Scripts\python.exe scripts\load_zetaris.py --verify  # re-checks the row counts later (read-only)
```

This registers the five synthetic workforce tables and `upload_cells` (every uploaded spreadsheet, one row per cell). It's needed once per Zetaris install. The Zetaris container reaches the service at `http://host.docker.internal:<PORT>`; if yours is somewhere else, pass `--public-url` or set `AGENTOFFICE_PUBLIC_URL`.

### 4.5 Use it

Restart AgentOffice so it reads the new `.env`. The panel's badge now shows the model route and Zetaris, and the sample-mode switch can be turned off. Attach any CSV or Excel file (up to 5 MB or 20,000 rows) and describe the work.

Live runs take time: Nemotron reasons for minutes on a large plan, so a spreadsheet task takes about 5 to 10 minutes end to end. The panel shows each step as it happens.

To save live examples: `.\.venv\Scripts\python.exe scripts\run_examples.py --url http://localhost:8000` (add `--tasks` for the spreadsheet tasks).

## Configuration reference

All settings come from environment variables or the private `.env` file; real environment variables win. Values still starting with `your-` (as copied from `.env.example`) count as unset.

| Variable | Default | What it does |
|---|---|---|
| `SAMPLE_MODE` | `false` | `true` forces sample mode, even with keys set |
| `LLM_PROVIDER` | `meterless` | `meterless` (primary) or `openai_compatible` (uses the `MODEL_*` variables) |
| `METERLESS_BASE_URL` | `http://127.0.0.1:8088/v1` in `.env.example` | Nightshift's runtime URL; pasted URLs are normalised to `<host>/v1` |
| `METERLESS_RUNTIME_TOKEN` | none | Nightshift runtime token |
| `METERLESS_CONNECT_LINK` | none | a Nightshift connect link, instead of the two above |
| `METERLESS_MODEL` | `meterless/auto` | the Nightshift route every agent uses |
| `METERLESS_APP` | `agentoffice` | sent as the `meterless-app` header |
| `MODEL_BASE_URL`, `MODEL_API_KEY`, `MODEL_NAME` | none | any OpenAI-compatible endpoint, with `LLM_PROVIDER=openai_compatible` |
| `ZETARIS_REST_URL` | `http://localhost:8888/api/v1.0` in `.env.example` | Zetaris REST API |
| `ZETARIS_ORG_ID` | `1` | Zetaris organisation ID |
| `ZETARIS_API_KEY` | none | Zetaris API key |
| `PORT` | `8000` | the service's port |
| `LOG_LEVEL` | `INFO` | server log level |
| `LOG_DIR` | `logs/` | where traces and ledgers are written |
| `WORLD_DB_PATH` | `state/world.db` | the World Model store |
| `AGENTOFFICE_NO_HOST_REWRITE` | `false` | in Docker, keep `localhost` URLs as written |
| `AGENTOFFICE_PUBLIC_URL` | `http://host.docker.internal:<PORT>` | where Zetaris reaches the service (`load_zetaris.py` only) |

## Where AgentOffice keeps its data

Everything stays in the project folder, and none of it is committed.

| Path | What's in it |
|---|---|
| `state/uploads/`, `state/outputs/` | your uploaded spreadsheets and the filled files (kept so Zetaris can read the uploads) |
| `state/tasks/`, `state/automations/`, `state/approvals.jsonl` | each task's plan and code, the approved code, and the hash-chained approval ledger |
| `state/world*.db`, `state/memory*.db` | the World Model and H-MEM stores (separate files for sample mode) |
| `state/island-browser/` | the desktop app's own browser profile (your normal browser profile is never touched) |
| `logs/trace-<run_id>.jsonl` | one hash-chained trace per run |
| `logs/hmem-ledger*.jsonl`, `logs/agentoffice-server.log` | the H-MEM trust ledger and the desktop app's service log |

To start fresh, quit AgentOffice and delete `state/` and the files in `logs/`.

## Troubleshooting

| Problem | Fix |
|---|---|
| *"running scripts is disabled on this system"* | Start the script as shown: `powershell -ExecutionPolicy Bypass -File setup.ps1`. That allows only this run. |
| *"Python 3.11 or newer was not found"* | Install Python from python.org and run setup again. The `python` that only opens the Microsoft Store isn't a real interpreter. |
| *".venv uses Python 3.x"* | Delete the `.venv` folder and run setup again. |
| Double-clicking `AgentOffice.pyw` does nothing | Windows has no program associated with `.pyw` files. Use the shortcut that setup creates, or run `.venv\Scripts\pythonw.exe AgentOffice.pyw`. |
| *"AgentOffice could not start its background service"* | Read `logs/agentoffice-server.log`. Usually another program is using port 8000. Close it, or set `PORT` as a Windows environment variable: the desktop app reads `PORT` from the environment, not from `.env`. |
| The island doesn't appear | The desktop app needs Microsoft Edge or Google Chrome. Use the browser UI meanwhile. |
| *"Ctrl+Shift+Space is taken by another app"* | Another app registered the shortcut first. Click the island to open it instead. |
| `agent_no_offline_plan` | Sample mode only plans the three demo files. Your own files need default mode. |
| `agent_not_configured` from the API | Default mode is missing settings (the message lists them). Pass `"options": {"sample_mode": true}` or set `SAMPLE_MODE=true`. |
| `check_connections.py` fails for Zetaris | Check that `docker ps` shows the Zetaris containers as healthy, and that the API key is current. A 401 means a bad or expired key. |
| `check_connections.py` fails for Nightshift | Check that Nightshift is open and that an NVIDIA provider is connected. `--detect` scans ports 8088, 8090, 8787 and 11434. |
| `load_zetaris.py --apply` can't reach the service | Start AgentOffice first, and allow `python.exe` and `pythonw.exe` through Windows Firewall on private networks. |

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1 -Remove
```

This removes the shortcuts. Then delete the project folder.
