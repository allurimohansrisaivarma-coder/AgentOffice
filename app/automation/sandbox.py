"""Run generated code without trusting it.

1. Static policy (AST): only whitelisted stdlib imports; no eval/exec/open/
   __import__/getattr-style reflection; no dunder names or attributes; size cap.
2. Separate process: `python -I` (no user site, no PYTHON* env), an empty
   environment (no secrets can leak), input over stdin, hard timeout. On POSIX
   the child also gets CPU and address-space limits. Windows has no rlimit, so
   there the timeout and the static policy are the guards (a known limit).
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ALLOWED_IMPORTS = frozenset({"math", "re", "decimal", "datetime", "collections", "itertools", "functools",
                             "string", "unicodedata", "json", "statistics", "typing", "dataclasses", "copy"})
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "open", "__import__", "input", "globals", "locals",
                             "vars", "getattr", "setattr", "delattr", "breakpoint", "exit", "quit", "help",
                             "memoryview", "classmethod", "staticmethod", "super"})
MAX_CODE_CHARS = 20_000
_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|nvapi-[A-Za-z0-9_\-]{8,}|"
                     r"(?i:(api[_-]?key|secret|token|password))\s*=\s*['\"][^'\"]{6,}['\"])")
_MARKER = "__AGENTOFFICE_RESULT__"

_RUNNER = f'''
import importlib.util, json, sys
payload = json.loads(sys.stdin.read())
try:
    spec = importlib.util.spec_from_file_location("automation", payload["path"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except BaseException as e:
    sys.stdout.write("\\n{_MARKER}" + json.dumps({{"ok": False, "error": type(e).__name__ + ": " + str(e)[:300]}}))
    sys.exit(0)
results = []
for args in payload["calls"]:
    try:
        results.append({{"ok": True, "output": json.loads(json.dumps(module.run(*args)))}})
    except BaseException as e:
        results.append({{"ok": False, "error": type(e).__name__ + ": " + str(e)[:300]}})
sys.stdout.write("\\n{_MARKER}" + json.dumps({{"ok": True, "results": results}}) + "\\n")
'''


def policy_violations(code: str) -> list[str]:
    if len(code) > MAX_CODE_CHARS:
        return [f"code is {len(code)} characters; the limit is {MAX_CODE_CHARS}"]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"does not parse: {e.msg} (line {e.lineno})"]
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    problems.append(f"line {node.lineno}: import of {alias.name!r} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                problems.append(f"line {node.lineno}: import from {node.module!r} is not allowed")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_CALLS:
                problems.append(f"line {node.lineno}: use of {node.id!r} is not allowed")
            elif node.id.startswith("__") and node.id != "__name__":
                problems.append(f"line {node.lineno}: dunder name {node.id!r} is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"line {node.lineno}: dunder attribute {node.attr!r} is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"line {node.lineno}: global/nonlocal is not allowed")
    has_run = any(isinstance(n, ast.FunctionDef) and n.name == "run" and len(n.args.args) == 2
                  for n in tree.body)
    if not has_run:
        problems.append("must define a top-level function run(left, right)")
    return problems


def find_secrets(code: str) -> list[str]:
    return [f"possible hardcoded credential: {m.group(0)[:12]}..." for m in _SECRET.finditer(code)]


def _limits():  # pragma: no cover - POSIX only
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))


def run_batch(code: str, calls: list[list], *, timeout_s: float = 10.0) -> dict[str, Any]:
    """Call run(*args) once per entry of `calls`, all in one sandboxed process.
    {"ok", "results": [{"ok", "output" | "error"}] | "error", "timed_out", "elapsed_s"}; "ok" is False only
    when the module itself failed (import error, timeout, crash). Callers must run policy_violations first."""
    env = {"SYSTEMROOT": os.environ["SYSTEMROOT"]} if os.name == "nt" and "SYSTEMROOT" in os.environ else {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="agentoffice-sandbox-") as tmp:
        module = Path(tmp) / "automation.py"
        runner = Path(tmp) / "runner.py"
        module.write_text(code, encoding="utf-8")
        runner.write_text(_RUNNER, encoding="utf-8")
        payload = json.dumps({"path": str(module), "calls": calls})
        try:
            proc = subprocess.run([sys.executable, "-I", str(runner)], input=payload, capture_output=True,
                                  text=True, encoding="utf-8", timeout=timeout_s, cwd=tmp, env=env,
                                  preexec_fn=_limits if os.name == "posix" else None)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timed out after {timeout_s:g}s", "timed_out": True,
                    "elapsed_s": round(time.monotonic() - started, 3)}
    elapsed = round(time.monotonic() - started, 3)
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(_MARKER):
            return {**json.loads(line[len(_MARKER):]), "timed_out": False, "elapsed_s": elapsed}
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["no output"]
    return {"ok": False, "error": f"process exited {proc.returncode}: {tail[0][:300]}", "timed_out": False,
            "elapsed_s": elapsed}


def run_in_sandbox(code: str, left: list[dict], right: list[dict], *, timeout_s: float = 5.0) -> dict[str, Any]:
    """One call of run(left, right): {"ok", "output" | "error", "timed_out", "elapsed_s"}."""
    batch = run_batch(code, [[left, right]], timeout_s=timeout_s)
    if not batch["ok"]:
        return batch
    return {**batch.pop("results")[0], "timed_out": False, "elapsed_s": batch["elapsed_s"]}
