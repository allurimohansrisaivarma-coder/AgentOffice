"""AgentOffice as a desktop app.

Windows: double-click this file, or the shortcut that scripts/install_shortcut.ps1 creates.
macOS:   double-click AgentOffice.command instead (same island, Chrome/Edge app mode).

No terminal tab required: the background service (run.py) starts hidden, the island
appears at the top of the screen, Ctrl+Shift+Space (⌃⇧Space on Mac) opens it from any
app when the hotkey is available, and the power button in the panel quits both.
Launching it again just opens the island.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import island  # noqa: E402

sys.exit(island.main(["--serve", *sys.argv[1:]]))
