"""AgentOffice on the Windows desktop: a floating island on top of every app, Ctrl+Shift+Space opens it.

    python scripts/island.py                 # the API must be running (python run.py)
    python scripts/island.py --serve         # start the API too, if it is not running
    python scripts/island.py --url http://localhost:8000

How it works (standard library only, through ctypes):
  * Microsoft Edge (or Chrome) opens the chat UI in app mode (/?shell=desktop) with its own profile under
    state/island-browser, so it never touches your normal browser profile.
  * The window loses its title bar and taskbar button, stays on top, and is clipped to a pill (collapsed) or a
    rounded panel (open), centred at the top of the main screen like a dynamic island.
  * A global hotkey (Ctrl+Shift+Space) focuses the window, which opens the panel; pressing it again, Esc, or
    clicking elsewhere folds it back. The page reports its state in the window title ("... · island" or
    "... · panel"), and this script resizes the window to match.
Close it with Ctrl+C here, or by closing the window (Alt+F4 while it is focused).
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
PANEL = (460, 760)
TOP_MARGIN = 10
TITLE_PREFIX = "AgentOffice ·"

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CAPTION, WS_THICKFRAME, WS_SYSMENU = 0x00C00000, 0x00040000, 0x00080000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00020000, 0x00010000
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
HWND_TOPMOST = -1
SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x0010, 0x0020, 0x0040
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0002, 0x0004, 0x4000
VK_SPACE, WM_HOTKEY, PM_REMOVE = 0x20, 0x0312, 0x0001
DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND = 33, 1


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
    g.CreateRoundRectRgn.restype = wintypes.HRGN


def browser_path() -> str | None:
    candidates = [shutil.which("msedge"), shutil.which("chrome")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [str(Path(base) / "Microsoft/Edge/Application/msedge.exe"),
                           str(Path(base) / "Google/Chrome/Application/chrome.exe")]
    return next((c for c in candidates if c and Path(c).exists()), None)


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
        self.size = (0, 0)
        self.previous = None

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
        pref = ctypes.c_int(DWMWCP_DONOTROUND)   # we clip our own shape
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                                       ctypes.byref(pref), ctypes.sizeof(pref))
        except OSError:
            pass

    def place(self, w: int, h: int, radius: int) -> None:
        x = (self.screen_w - w) // 2
        y = int(TOP_MARGIN * self.scale)
        self.user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, w, h, SWP_FRAMECHANGED | SWP_SHOWWINDOW
                                 | SWP_NOACTIVATE)
        region = self.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
        self.user32.SetWindowRgn(self.hwnd, region, True)   # the system owns the region afterwards
        self.size = (w, h)

    def show(self, mode: str, animate: bool = True) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        target = self.px(PANEL if mode == "panel" else ISLAND)
        radius = int((28 if mode == "panel" else ISLAND[1] / 2) * self.scale)
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AgentOffice desktop island (Windows).")
    parser.add_argument("--url", default=f"http://localhost:{os.environ.get('PORT', '8000')}")
    parser.add_argument("--serve", action="store_true", help="start the API (run.py) if it is not running")
    args = parser.parse_args(argv)
    if os.name != "nt":
        print("The desktop island is Windows-only; open the chat UI in a browser instead:", args.url)
        return 1
    _bind()
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2
    except (AttributeError, OSError):
        pass

    server = None
    if not healthy(args.url):
        if not args.serve:
            print(f"The AgentOffice API is not running at {args.url}. Start it with `python run.py`, or pass --serve.")
            return 1
        server = subprocess.Popen([sys.executable, str(ROOT / "run.py")], cwd=ROOT)
        for _ in range(60):
            if healthy(args.url):
                break
            time.sleep(0.5)
        else:
            print("The API did not start; see its output above.")
            server.terminate()
            return 1

    exe = browser_path()
    if exe is None:
        print("Microsoft Edge or Google Chrome is needed for the desktop island. Open", args.url, "in a browser.")
        return 1
    profile = ROOT / "state" / "island-browser"
    profile.mkdir(parents=True, exist_ok=True)
    browser = subprocess.Popen([exe, f"--app={args.url}/?shell=desktop", f"--user-data-dir={profile}",
                                "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
                                f"--window-size={ISLAND[0]},{ISLAND[1]}"])
    hwnd = find_window()
    if hwnd is None:
        print("Could not find the AgentOffice window.")
        browser.terminate()
        return 1

    island = Island(hwnd)
    island.strip_frame()
    island.show("island", animate=False)
    user32 = ctypes.windll.user32
    hotkey = bool(user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_SPACE))
    print("AgentOffice island is running at the top of your screen.",
          "Press Ctrl+Shift+Space to open it." if hotkey else
          "Ctrl+Shift+Space is taken by another app; click the island to open it.", "Ctrl+C here to quit.")
    msg = wintypes.MSG()
    try:
        while user32.IsWindow(hwnd):
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_HOTKEY:
                    island.toggle_focus()
            title = island.title()
            if title.startswith(TITLE_PREFIX):
                island.show("panel" if title.endswith("panel") else "island")
            time.sleep(0.04)
    except KeyboardInterrupt:
        pass
    finally:
        if hotkey:
            user32.UnregisterHotKey(None, 1)
        if browser.poll() is None:
            browser.terminate()
        if server is not None:
            server.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
