# AgentOffice (working name)

**A simulated AI team that finds where an office team's time goes, explains why with evidence, and then decides, builds and tests the automation that fixes it, with a human approving before anything goes live.**

Open Agent Hackathon 2026 submission. Primary track: **Track 3, Reasoning Architecture.** Stages also address Track 1 (Fragmented Intelligence) and Track 2 (Explain Why).

> **Status (23 Oct 2026):** scaffold only. The API, sponsor adapters, tracing and connection check exist; the agent pipeline does not yet, so `POST /run` returns a structured `not_implemented` error. This README only describes what the code actually does.

## Quick start (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env          # then fill in your own values; never commit .env
.\.venv\Scripts\python.exe -m pytest -q
```

Check the sponsor services (Zetaris and Meterless Nightshift are reported separately; exit code 0 only if every check passes):

```powershell
.\.venv\Scripts\python.exe scripts\check_connections.py
```

It runs `SELECT 1` and `SHOW LIGHTNING DATABASES` on Zetaris, then `GET /models` and one short chat call on Nightshift, and writes each result to `logs/trace-<id>.jsonl`.

Start the API on `0.0.0.0:8000` (`GET /health`, `POST /run`):

```powershell
.\.venv\Scripts\python.exe run.py
```

- Hackathon brief and agent rules: [AGENTS.md](AGENTS.md)
- What we're building and in what order: [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md)
- Design ideas: [docs/INSPIRATION.md](docs/INSPIRATION.md)

Sponsor technology: Zetaris (data layer), Meterless Nightshift (primary model provider, NVIDIA Nemotron lanes), Meterless World Model engine (+ H-MEM planned).

Developed with Claude Code. All AI-generated code is reviewed and tested by the team.
