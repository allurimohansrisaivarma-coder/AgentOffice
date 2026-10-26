"""AgentOffice on the Windows desktop: a floating island on top of every app, Ctrl+Shift+Space opens it.

    python scripts/island.py                 # the API must be running (python run.py)
    python scripts/island.py --serve         # start the API too (hidden), if it is not running
    AgentOffice.pyw                          # the desktop app: --serve with no console at all
    python scripts/island.py --url http://localhost:8000

How it works (standard library only, through ctypes):
  * Microsoft Edge (or Chrome, with --browser chrome) opens the chat UI in app mode (/?shell=desktop) with its own profile under
    state/island-browser, so it never touches your normal browser profile.
  * The window loses its title bar and taskbar button, stays on top, and is clipped to a pill (collapsed) or a
    rounded panel (open), centred at the top of the main screen like a dynamic island.
  * A global hotkey (Ctrl+Shift+Space) focuses the window, which opens the panel; pressing it again, Esc, or
    clicking elsewhere folds it back. The page reports its state in the window title ("... · island",
    "... · panel", or "... · chat" with the Team/Sheet pane hidden), and this script resizes the window to match.
Quit with the power button in the panel (the page titles itself "... · quit"), Ctrl+C here, or Alt+F4;
quitting also stops a service this script started. A second launch only opens the running island.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISLAND = (300, 44)            # CSS pixels
PANEL = (960, 760)            # split view: chat + Team/Sheet
CHAT = (480, 760)             # the panel with the Team/Sheet pane hidden
TOP_MARGIN = 10
TITLE_PREFIX = "AgentOffice ·"

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CAPTION, WS_THICKFRAME, WS_SYSMENU = 0x00C00000, 0x00040000, 0x00080000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00020000, 0x00010000
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0010, 0x0020, 0x0040
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0002, 0x0004, 0x4000
VK_SPACE, WM_HOTKEY, WM_CLOSE, PM_REMOVE = 0x20, 0x0312, 0x0010, 0x0001
CREATE_NO_WINDOW = 0x08000000
DWMWA_NCRENDERING_POLICY, DWMNCRP_DISABLED = 2, 1
DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND = 33, 1
DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE = 34, 0xFFFFFFFE
DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE = 38, 1


def _bind() -> None:
    """Pointer-sized handles need explicit signatures on 64-bit Windows."""
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    H = wintypes.HWND
    u.SetWindowPos.argtypes = [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u.SetWindowRgn.argtypes = [H, wintypes.HRGN, wintypes.BOOL]
    u.GetForegroundWindow.restype = H
    u.GetShellWindow.restype = H
    u.SetForegroundWindow.argtypes = [H]
    u.BringWindowToTop.argtypes = [H]
    u.IsWindow.argtypes = [H]
    u.IsWindowVisible.argtypes = [H]
    u.GetWindowTextW.argtypes = [H, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowLongW.argtypes = [H, ctypes.c_int]
    u.SetWindowLongW.argtypes = [H, ctypes.c_int, wintypes.LONG]
    u.GetWindowThreadProcessId.argtypes = [H, ctypes.c_void_p]
    u.GetDpiForWindow.argtypes = [H]
    u.PostMessageW.argtypes = [H, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    u.GetWindowRect.argtypes = [H, ctypes.POINTER(wintypes.RECT)]
    u.GetClientRect.argtypes = [H, ctypes.POINTER(wintypes.RECT)]
    u.ClientToScreen.argtypes = [H, ctypes.POINTER(wintypes.POINT)]
    g.CreateRoundRectRgn.restype = wintypes.HRGN
    g.CreateRectRgn.restype = wintypes.HRGN
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    u.GetWindowRgn.argtypes = [H, wintypes.HRGN]


def browser_path(prefer: str = "edge") -> str | None:
    """Edge first: its app window accepts a borderless, clipped frame (Chrome draws its own title bar)."""
    bases = [os.environ.get(v) for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    found = {"edge": [shutil.which("msedge"), *(str(Path(b) / "Microsoft/Edge/Application/msedge.exe")
                                                  for b in bases if b)],
             "chrome": [shutil.which("chrome"), *(str(Path(b) / "Google/Chrome/Application/chrome.exe")
                                                   for b in bases if b)]}
    order = found[prefer] + found["chrome" if prefer == "edge" else "edge"]
    return next((c for c in order if c and Path(c).exists()), None)


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


class Island:
    def __init__(self, hwnd: int):
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32
        self.hwnd = hwnd
        self.scale = (self.user32.GetDpiForWindow(hwnd) or 96) / 96
        self.screen_w = self.user32.GetSystemMetrics(0)
        self.screen_h = self.user32.GetSystemMetrics(1)
        self.mode = None
        self.frame = 0           # height of the browser's own title bar, hidden above the screen edge
        self.size = (0, 0)
        self.radius = 0
        self.borders = (0, 0, 0, 0)   # left, top, right, bottom: window rect minus client rect
        self.previous = None

    def measure_borders(self) -> None:
        win, client, origin = wintypes.RECT(), wintypes.RECT(), wintypes.POINT(0, 0)
        self.user32.GetWindowRect(self.hwnd, ctypes.byref(win))
        self.user32.GetClientRect(self.hwnd, ctypes.byref(client))
        self.user32.ClientToScreen(self.hwnd, ctypes.byref(origin))
        left, top = origin.x - win.left, origin.y - win.top
        self.borders = (left, top, win.right - origin.x - client.right, win.bottom - origin.y - client.bottom)

    def px(self, css: tuple[int, int]) -> tuple[int, int]:
        w, h = css
        h = min(h, int(self.screen_h / self.scale) - 2 * TOP_MARGIN - 40)
        return int(w * self.scale), int(h * self.scale)

    def strip_frame(self) -> None:
        u = self.user32
        style = u.GetWindowLongW(self.hwnd, GWL_STYLE)
        u.SetWindowLongW(self.hwnd, GWL_STYLE, style & ~(WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX
                                                         | WS_MAXIMIZEBOX))
        ex = u.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(self.hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)   # no taskbar button
        # we clip our own shape: no Mica backdrop (it ignores the clip), frame, border or corner rounding
        for attribute, value in ((DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE),
                                 (DWMWA_NCRENDERING_POLICY, DWMNCRP_DISABLED),
                                 (DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND),
                                 (DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)):
            v = ctypes.c_uint(value)
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, attribute, ctypes.byref(v), ctypes.sizeof(v))
            except OSError:
                pass
        u.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FRAMECHANGED | SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOMOVE)
        self.measure_borders()

    def clip(self) -> None:
        """Clip to the pill/panel shape below the browser's title bar (the system owns the region after)."""
        w, h = self.size
        left, top = self.borders[0], self.borders[1] + self.frame
        region = self.gdi32.CreateRoundRectRgn(left, top, left + w + 1, top + h + 1, self.radius * 2,
                                               self.radius * 2)
        self.user32.SetWindowRgn(self.hwnd, region, True)

    def ensure_clipped(self) -> None:
        """Browsers may reset the region on their own repaints; put it back if it is gone."""
        probe = self.gdi32.CreateRectRgn(0, 0, 0, 0)
        kind = self.user32.GetWindowRgn(self.hwnd, probe)
        self.gdi32.DeleteObject(probe)
        if kind == 0 and self.size != (0, 0):   # ERROR: no region
            self.clip()

    def place(self, w: int, h: int, radius: int) -> None:
        """Show the page's w x h area centred at the top of the screen; the title bar sits above the edge."""
        bl, bt, br, bb = self.borders
        x = (self.screen_w - w) // 2 - bl
        y = int(TOP_MARGIN * self.scale) - self.frame - bt
        self.size, self.radius = (w, h), radius
        self.user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, w + bl + br, h + self.frame + bt + bb,
                                 SWP_SHOWWINDOW | SWP_NOACTIVATE)
        self.clip()

    def show(self, mode: str, frame_css: int, animate: bool = True) -> None:
        # the page reports outerHeight - innerHeight, which also counts the window's (invisible) top and
        # bottom borders; only the title bar above the page must be hidden
        frame = max(0, int(round(frame_css * self.scale)) - self.borders[1] - self.borders[3])
        if mode == self.mode and frame == self.frame:
            return
        self.mode, self.frame = mode, frame
        target = self.px({"panel": PANEL, "chat": CHAT}.get(mode, ISLAND))
        radius = int((28 if mode in ("panel", "chat") else ISLAND[1] / 2) * self.scale)
        start = self.size if animate and self.size != (0, 0) else target
        frames = 9 if animate and start != target else 1
        for i in range(1, frames + 1):
            t = 1 - (1 - i / frames) ** 3    # ease-out
            w = int(start[0] + (target[0] - start[0]) * t)
            h = int(start[1] + (target[1] - start[1]) * t)
            self.place(w, h, min(radius, h // 2))
            time.sleep(0.012)

    def title(self) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self.user32.GetWindowTextW(self.hwnd, buf, 256)
        return buf.value

    def toggle_focus(self) -> None:
        u = self.user32
        foreground = u.GetForegroundWindow()
        if foreground == self.hwnd:
            # hand focus back to where it was: the page folds itself on blur
            if self.previous and u.IsWindow(self.previous):
                u.SetForegroundWindow(self.previous)
            else:
                u.SetForegroundWindow(u.GetShellWindow())
            return
        self.previous = foreground
        this_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        other = u.GetWindowThreadProcessId(foreground, None)
        u.AttachThreadInput(this_thread, other, True)
        u.BringWindowToTop(self.hwnd)
        u.SetForegroundWindow(self.hwnd)
        u.AttachThreadInput(this_thread, other, False)


def parse_title(title: str) -> tuple[str, int]:
    """"AgentOffice · panel · 33" -> ("panel", 33): the page's state and the browser title-bar height.
    "chat" is the open panel with the Team/Sheet pane hidden."""
    parts = [p.strip() for p in title.split("·")]
    mode = parts[1] if len(parts) > 1 and parts[1] in ("panel", "chat", "quit") else "island"
    frame = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return mode, min(frame, 120)


def find_window(timeout_s: float = 20.0) -> int | None:
    user32 = ctypes.windll.user32
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value.startswith(TITLE_PREFIX):
                found.append(hwnd)
                return False
        return True

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found.clear()
        user32.EnumWindows(proc(callback), 0)
        if found:
            return found[0]
        time.sleep(0.25)
    return None


def notify(message: str, error: bool = False) -> None:
    """Print in a terminal; show a message box when started without one (AgentOffice.pyw, a shortcut)."""
    if sys.stdout is not None and sys.stdout.isatty():
        print(message, flush=True)
    elif error:
        ctypes.windll.user32.MessageBoxW(None, message, "AgentOffice", 0x10)


def start_server(url: str) -> subprocess.Popen:
    """run.py in the background: no console window, output in logs/agentoffice-server.log."""
    # The service needs the project's dependencies, so prefer its .venv: a double-clicked AgentOffice.pyw runs
    # with whichever Python Windows associates with .pyw. The island itself needs only the standard library.
    venv = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    pythonw = venv if venv.exists() else Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw if pythonw.exists() else sys.executable)
    (ROOT / "logs").mkdir(exist_ok=True)
    log = (ROOT / "logs" / "agentoffice-server.log").open("ab")
    server = subprocess.Popen([exe, str(ROOT / "run.py")], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    for _ in range(90):
        if healthy(url) or server.poll() is not None:
            break
        time.sleep(0.5)
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AgentOffice desktop island (Windows).")
    parser.add_argument("--url", default=f"http://localhost:{os.environ.get('PORT', '8000')}")
    parser.add_argument("--serve", action="store_true", help="start the API (run.py) if it is not running")
    parser.add_argument("--browser", choices=("edge", "chrome"), default="edge")
    args = parser.parse_args(argv)
    if os.name != "nt":
        print("The desktop island is Windows-only; open the chat UI in a browser instead:", args.url)
        return 1
    _bind()
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2
    except (AttributeError, OSError):
        pass

    running = find_window(timeout_s=0.5)
    if running is not None:   # one island only: a second launch just opens the first one
        Island(running).toggle_focus()
        return 0

    server = None
    if not healthy(args.url):
        if not args.serve:
            notify(f"The AgentOffice API is not running at {args.url}. Start it with `python run.py`, "
                   "or pass --serve.", error=True)
            return 1
        server = start_server(args.url)
        if not healthy(args.url):
            notify("AgentOffice could not start its background service. See logs/agentoffice-server.log.",
                   error=True)
            server.terminate()
            return 1

    exe = browser_path(args.browser)
    if exe is None:
        notify("Microsoft Edge or Google Chrome is needed for the AgentOffice island.", error=True)
        if server is not None:
            server.terminate()
        return 1
    profile = ROOT / "state" / "island-browser"
    profile.mkdir(parents=True, exist_ok=True)
    browser = subprocess.Popen([exe, f"--app={args.url}/?shell=desktop", f"--user-data-dir={profile}",
                                "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
                                f"--window-size={ISLAND[0]},{ISLAND[1]}"])
    hwnd = find_window()
    if hwnd is None:
        notify("Could not find the AgentOffice window.", error=True)
        browser.terminate()
        if server is not None:
            server.terminate()
        return 1

    island = Island(hwnd)
    island.strip_frame()
    island.show(*parse_title(island.title()), animate=False)
    user32 = ctypes.windll.user32
    hotkey = bool(user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_SPACE))
    notify("AgentOffice island is running at the top of your screen. "
           + ("Press Ctrl+Shift+Space to open it. " if hotkey else
              "Ctrl+Shift+Space is taken by another app; click the island to open it. ")
           + "Quit from the power button in the panel, or Ctrl+C here.")
    msg = wintypes.MSG()
    tick = 0
    try:
        while user32.IsWindow(hwnd):
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY:
                    island.toggle_focus()
            title = island.title()
            if title.startswith(TITLE_PREFIX):
                mode, frame = parse_title(title)
                if mode == "quit":
                    break
                island.show(mode, frame)
            tick += 1
            if tick % 12 == 0:
                island.ensure_clipped()
            time.sleep(0.04)
    except KeyboardInterrupt:
        pass
    finally:
        if hotkey:
            user32.UnregisterHotKey(None, 1)
        if user32.IsWindow(hwnd):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if browser.poll() is None:
            browser.terminate()
        if server is not None:
            server.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
