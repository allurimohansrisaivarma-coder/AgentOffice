"""Count which model answered every model call in saved traces (evidence for the NVIDIA Nemotron award).

    python scripts/nemotron_usage.py                      # the live runs in output_examples/
    python scripts/nemotron_usage.py logs/trace-*.jsonl   # any traces

Reads only the traces: each model-call event records the model that actually answered and its tokens.
"""

from __future__ import annotations

import glob
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    patterns = (argv if argv is not None else sys.argv[1:]) or [str(ROOT / "output_examples" / "*.trace.jsonl")]
    paths = sorted({p for pattern in patterns for p in glob.glob(pattern)})
    by_model, by_agent, tokens = Counter(), Counter(), 0
    print(f"{'trace':<42} calls  models")
    for path in paths:
        run = Counter()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            # a model call carries the answering model and its token count; skipped/offline steps don't
            if event.get("tokens") is None or event.get("model") in (None, "offline-fallback"):
                continue
            run[event["model"]] += 1
            by_agent[event["agent_name"]] += 1
            tokens += event["tokens"] or 0
        by_model.update(run)
        print(f"{Path(path).name:<42} {sum(run.values()):>5}  " + ", ".join(f"{m} x{n}" for m, n in run.items()))
    total = sum(by_model.values())
    nemotron = sum(n for m, n in by_model.items() if "nemotron" in m.lower())
    print(f"\n{total} model calls in {len(paths)} traces, {tokens:,} tokens; answered by Nemotron: {nemotron}/{total}")
    for model, n in by_model.most_common():
        print(f"  {model}: {n}")
    print(f"{len(by_agent)} agents: " + ", ".join(f"{a} {n}" for a, n in by_agent.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
