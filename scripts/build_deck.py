"""Assemble docs/deck/ into one standalone HTML deck (and, with --pdf, a PDF via headless Chrome or Edge).

    python scripts/build_deck.py            # docs/deck/deck.html
    python scripts/build_deck.py --pdf      # also docs/AgentOffice-deck.pdf

The slides in docs/deck/slides/ are the source of the published deck. Images
live in docs/images/; speaker notes (<aside>) are kept as hidden notes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECK = ROOT / "docs" / "deck"

STYLE = """
@page { size: 1920px 1080px; margin: 0; }
* { box-sizing: border-box; margin: 0; }
html, body { background: #0e1116; }
section { width: 1920px; height: 1080px; position: relative; overflow: hidden; page-break-after: always;
          break-after: page; margin: 0 auto 24px; }
aside { display: none; }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 14px 18px; text-align: left; }
x-shape { display: block; flex: none; }
x-shape[kind="circle"] { border-radius: 50%; }
x-shape[kind="arrow-right"] { clip-path: polygon(0 30%, 60% 30%, 60% 0, 100% 50%, 60% 100%, 60% 70%, 0 70%); }
@media print { section { margin: 0; } }
"""


def build() -> Path:
    deck = json.loads((DECK / "deck.json").read_text(encoding="utf-8"))
    fonts = "".join(f'<link rel="stylesheet" href="{f["href"]}">' for f in deck.get("faces", {}).values())
    slides = [(DECK / "slides" / f"{name}.html").read_text(encoding="utf-8") for name in deck["order"]]
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{deck["title"]}</title>{fonts}'
            f"<style>{STYLE}</style></head><body>{''.join(slides)}</body></html>")
    out = DECK / "deck.html"
    out.write_text(html, encoding="utf-8")
    return out


def browser() -> str | None:
    for c in [shutil.which("chrome"), shutil.which("msedge"), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", shutil.which("chromium"),
              shutil.which("google-chrome")]:
        if c and Path(c).exists():
            return c
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the standalone deck.")
    parser.add_argument("--pdf", action="store_true")
    args = parser.parse_args(argv)
    html = build()
    print(f"wrote {html.relative_to(ROOT)}")
    if args.pdf:
        exe = browser()
        if exe is None:
            print("no Chrome/Edge found for the PDF")
            return 1
        pdf = ROOT / "docs" / "AgentOffice-deck.pdf"
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--allow-file-access-from-files",
                        "--no-pdf-header-footer", "--virtual-time-budget=8000", f"--print-to-pdf={pdf}",
                        html.as_uri()], check=True, capture_output=True)
        print(f"wrote {pdf.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
