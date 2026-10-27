"""AgentOffice desktop island: a floating pill on top of every app, Ctrl+Shift+Space opens it.

    python scripts/island.py                 # API must already be running (python run.py)
    python scripts/island.py --serve         # start the API too, if it is not running
    AgentOffice.pyw                          # Windows: --serve with no console
    ./AgentOffice.command                    # macOS: same, double-clickable

How it works (standard library only):
  * Chrome or Edge opens /?shell=desktop in app mode with its own profile under
    state/island-browser (your normal browser profile is never touched).
  * The window is kept at the top centre of the main screen as a pill (collapsed) or
    a panel (open). The page reports state in the title ("… · island" / "… · panel" /
    "… · quit"); this script resizes to match.
  * Ctrl+Shift+Space focuses the window (Windows: RegisterHotKey; macOS: Carbon
    hotkey, or click the pill if Accessibility / hotkey registration is denied).

Windows uses Win32 ctypes for a borderless clipped frame. macOS uses Chrome app
mode plus AppleScript for position/size (macOS may ask for Accessibility once).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISLAND = (300, 44)            # CSS pixels
PANEL = (960, 760)            # split view: chat + Team/Sheet
TOP_MARGIN = 10
TITLE_PREFIX = "AgentOffice ·"


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def parse_title(title: str) -> tuple[str, int]:
    """"AgentOffice · panel · 33" -> ("panel", 33): page state and browser title-bar height."""
    parts = [p.strip() for p in title.split("·")]
    mode = parts[1] if len(parts) > 1 and parts[1] in ("panel", "quit") else "island"
    frame = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return mode, min(frame, 120)


def venv_python() -> str:
    if os.name == "nt":
        for name in ("pythonw.exe", "python.exe"):
            candidate = ROOT / ".venv" / "Scripts" / name
            if candidate.exists():
                return str(candidate)
        return sys.executable
    candidate = ROOT / ".venv" / "bin" / "python"
    return str(candidate if candidate.exists() else Path(sys.executable))


def _port_of(url: str) -> str:
    return url.rstrip("/").rsplit(":", 1)[-1]


def is_agentoffice(url: str) -> bool:
    """True when /health is our API (not some other service on the same port)."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=2) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status == 200 and '"status"' in body and ("sample_mode" in body or "llm_provider" in body)
    except OSError:
        return False


def port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def resolve_serve_url(preferred: str) -> str:
    """Prefer an already-running AgentOffice; else a free port (skip foreign listeners)."""
    base = preferred.rstrip("/")
    host = base.rsplit(":", 1)[0]
    try:
        start = int(_port_of(base))
    except ValueError:
        start = 8000
    candidates = [start, 8010, 8020, 8030, 8040, 8050]
    seen = set()
    ordered = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    for port in ordered:
        url = f"{host}:{port}"
        if is_agentoffice(url):
            return url
    for port in ordered:
        if port_free(port):
            return f"{host}:{port}"
    return f"{host}:{start}"


def start_server(url: str) -> subprocess.Popen:
    """run.py in the background; log to logs/agentoffice-server.log."""
    (ROOT / "logs").mkdir(exist_ok=True)
    log = (ROOT / "logs" / "agentoffice-server.log").open("ab")
    env = os.environ.copy()
    env["PORT"] = _port_of(url)
    kwargs: dict = {"cwd": ROOT, "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                    "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    server = subprocess.Popen([venv_python(), str(ROOT / "run.py")], **kwargs)
    for _ in range(90):
        if is_agentoffice(url) or healthy(url) or server.poll() is not None:
            break
        time.sleep(0.5)
    return server


def notify(message: str, error: bool = False) -> None:
    if sys.stdout is not None and sys.stdout.isatty():
        print(message, flush=True)
    if sys.platform == "darwin":
        kind = "display alert \"AgentOffice\" message " + repr(message) + (" as critical" if error else "")
        note = f'display notification {message.splitlines()[0]!r} with title "AgentOffice"'
        subprocess.run(["osascript", "-e", note], check=False)
        if error:
            subprocess.run(["osascript", "-e", kind], check=False)
    elif error and os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "AgentOffice", 0x10)


def screen_size() -> tuple[int, int]:
    if os.name == "nt":
        import ctypes
        u = ctypes.windll.user32
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
                text=True,
            )
            # "0, 0, 1440, 900"
            parts = [int(p.strip()) for p in out.split(",")]
            return parts[2] - parts[0], parts[3] - parts[1]
        except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
            pass
    return 1440, 900


# ------------------------------------------------------------------ browsers

def browser_path(prefer: str = "edge") -> str | None:
    if os.name == "nt":
        bases = [os.environ.get(v) for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        found = {
            "edge": [shutil.which("msedge"), *(str(Path(b) / "Microsoft/Edge/Application/msedge.exe")
                                               for b in bases if b)],
            "chrome": [shutil.which("chrome"), *(str(Path(b) / "Google/Chrome/Application/chrome.exe")
                                                  for b in bases if b)],
        }
        order = found[prefer] + found["chrome" if prefer == "edge" else "edge"]
        return next((c for c in order if c and Path(c).exists()), None)

    if sys.platform == "darwin":
        apps = {
            "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "edge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "chromium": "/Applications/Chromium.app/Contents/MacOS/Chromium",
        }
        # Prefer Chrome on Mac: Edge app-mode window shaping is less consistent.
        order = (["chrome", "edge", "chromium"] if prefer == "chrome"
                 else ["edge", "chrome", "chromium"] if prefer == "edge"
                 else ["chrome", "edge", "chromium"])
        for key in order:
            path = apps[key]
            if Path(path).exists():
                return path
        for name in ("google-chrome", "chromium", "msedge"):
            found = shutil.which(name)
            if found:
                return found
    return None


def browser_process_name(exe: str) -> str:
    lower = exe.lower()
    if "edge" in lower:
        return "Microsoft Edge"
    if "chromium" in lower:
        return "Chromium"
    return "Google Chrome"


# ------------------------------------------------------------------ Windows

def _main_windows(args) -> int:
    import ctypes
    from ctypes import wintypes

    GWL_STYLE, GWL_EXSTYLE = -16, -20
    WS_CAPTION, WS_THICKFRAME, WS_SYSMENU = 0x00C00000, 0x00040000, 0x00080000
    WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00020000, 0x00010000
    WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
    HWND_TOPMOST = -1
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0010, 0x0020, 0x0040
    MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0002, 0x0004, 0x4000
    VK_SPACE, WM_HOTKEY, WM_CLOSE, PM_REMOVE = 0x20, 0x0312, 0x0010, 0x0001
    DWMWA_NCRENDERING_POLICY, DWMNCRP_DISABLED = 2, 1
    DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND = 33, 1
    DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE = 34, 0xFFFFFFFE
    DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE = 38, 1

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

    try:
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass

    def find_window(timeout_s: float = 20.0) -> int | None:
        found: list[int] = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _):
            if u.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                u.GetWindowTextW(hwnd, buf, 256)
                if buf.value.startswith(TITLE_PREFIX):
                    found.append(hwnd)
                    return False
            return True

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            found.clear()
            u.EnumWindows(proc(callback), 0)
            if found:
                return found[0]
            time.sleep(0.25)
        return None

    class Island:
        def __init__(self, hwnd: int):
            self.hwnd = hwnd
            self.scale = (u.GetDpiForWindow(hwnd) or 96) / 96
            self.screen_w = u.GetSystemMetrics(0)
            self.screen_h = u.GetSystemMetrics(1)
            self.mode = None
            self.frame = 0
            self.size = (0, 0)
            self.radius = 0
            self.borders = (0, 0, 0, 0)
            self.previous = None

        def measure_borders(self) -> None:
            win, client, origin = wintypes.RECT(), wintypes.RECT(), wintypes.POINT(0, 0)
            u.GetWindowRect(self.hwnd, ctypes.byref(win))
            u.GetClientRect(self.hwnd, ctypes.byref(client))
            u.ClientToScreen(self.hwnd, ctypes.byref(origin))
            left, top = origin.x - win.left, origin.y - win.top
            self.borders = (left, top, win.right - origin.x - client.right, win.bottom - origin.y - client.bottom)

        def px(self, css: tuple[int, int]) -> tuple[int, int]:
            w, h = css
            h = min(h, int(self.screen_h / self.scale) - 2 * TOP_MARGIN - 40)
            return int(w * self.scale), int(h * self.scale)

        def strip_frame(self) -> None:
            style = u.GetWindowLongW(self.hwnd, GWL_STYLE)
            u.SetWindowLongW(self.hwnd, GWL_STYLE, style & ~(WS_CAPTION | WS_THICKFRAME | WS_SYSMENU
                                                             | WS_MINIMIZEBOX | WS_MAXIMIZEBOX))
            ex = u.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(self.hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
            for attribute, value in ((DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE),
                                     (DWMWA_NCRENDERING_POLICY, DWMNCRP_DISABLED),
                                     (DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND),
                                     (DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)):
                v = ctypes.c_uint(value)
                try:
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, attribute, ctypes.byref(v),
                                                               ctypes.sizeof(v))
                except OSError:
                    pass
            u.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                           SWP_FRAMECHANGED | SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOMOVE)
            self.measure_borders()

        def clip(self) -> None:
            w, h = self.size
            left, top = self.borders[0], self.borders[1] + self.frame
            region = g.CreateRoundRectRgn(left, top, left + w + 1, top + h + 1, self.radius * 2, self.radius * 2)
            u.SetWindowRgn(self.hwnd, region, True)

        def ensure_clipped(self) -> None:
            probe = g.CreateRectRgn(0, 0, 0, 0)
            kind = u.GetWindowRgn(self.hwnd, probe)
            g.DeleteObject(probe)
            if kind == 0 and self.size != (0, 0):
                self.clip()

        def place(self, w: int, h: int, radius: int) -> None:
            bl, bt, br, bb = self.borders
            x = (self.screen_w - w) // 2 - bl
            y = int(TOP_MARGIN * self.scale) - self.frame - bt
            self.size, self.radius = (w, h), radius
            u.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, w + bl + br, h + self.frame + bt + bb,
                           SWP_SHOWWINDOW | SWP_NOACTIVATE)
            self.clip()

        def show(self, mode: str, frame_css: int, animate: bool = True) -> None:
            frame = max(0, int(round(frame_css * self.scale)) - self.borders[1] - self.borders[3])
            if mode == self.mode and frame == self.frame:
                return
            self.mode, self.frame = mode, frame
            target = self.px(PANEL if mode == "panel" else ISLAND)
            radius = int((28 if mode == "panel" else ISLAND[1] / 2) * self.scale)
            start = self.size if animate and self.size != (0, 0) else target
            frames = 9 if animate and start != target else 1
            for i in range(1, frames + 1):
                t = 1 - (1 - i / frames) ** 3
                ww = int(start[0] + (target[0] - start[0]) * t)
                hh = int(start[1] + (target[1] - start[1]) * t)
                self.place(ww, hh, min(radius, hh // 2))
                time.sleep(0.012)

        def title(self) -> str:
            buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(self.hwnd, buf, 256)
            return buf.value

        def toggle_focus(self) -> None:
            foreground = u.GetForegroundWindow()
            if foreground == self.hwnd:
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

    running = find_window(timeout_s=0.5)
    if running is not None:
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
    hotkey = bool(u.RegisterHotKey(None, 1, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_SPACE))
    notify("AgentOffice island is running at the top of your screen. "
           + ("Press Ctrl+Shift+Space to open it. " if hotkey else
              "Ctrl+Shift+Space is taken by another app; click the island to open it. ")
           + "Quit from the power button in the panel, or Ctrl+C here.")
    msg = wintypes.MSG()
    tick = 0
    try:
        while u.IsWindow(hwnd):
            while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
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
            u.UnregisterHotKey(None, 1)
        if u.IsWindow(hwnd):
            u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if browser.poll() is None:
            browser.terminate()
        if server is not None:
            server.terminate()
    return 0


# ------------------------------------------------------------------ macOS

def _osascript(script: str) -> str:
    return subprocess.check_output(["osascript", "-e", script], text=True, stderr=subprocess.DEVNULL).strip()


def _osascript_ok(script: str) -> bool:
    try:
        _osascript(script)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _ours_title(title: str) -> bool:
    t = (title or "").strip()
    return t.startswith("AgentOffice") or t.startswith(TITLE_PREFIX)


def _main_darwin(args) -> int:
    """Hardened Mac shell: visible panel on launch, port failover, keep-alive, relaunch."""
    screen_w, screen_h = screen_size()
    (ROOT / "logs").mkdir(exist_ok=True)
    pid_path = ROOT / "logs" / "agentoffice-island.pid"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def read_title() -> str:
        script = '''
        tell application "System Events"
          repeat with proc in (every process whose background only is false)
            repeat with w in windows of proc
              try
                set n to name of w as text
                if n starts with "AgentOffice" then return n
              end try
            end repeat
          end repeat
        end tell
        return ""
        '''
        try:
            return _osascript(script)
        except (OSError, subprocess.CalledProcessError):
            return ""

    def place_window(css_w: int, css_h: int) -> None:
        h = min(css_h, screen_h - 2 * TOP_MARGIN - 40)
        w = min(css_w, screen_w - 40)
        x = max(0, (screen_w - w) // 2)
        y = TOP_MARGIN
        script = f'''
        tell application "System Events"
          repeat with proc in (every process whose background only is false)
            repeat with win in windows of proc
              try
                set n to name of win as text
                if n starts with "AgentOffice" then
                  set position of win to {{{x}, {y}}}
                  set size of win to {{{w}, {h}}}
                  return
                end if
              end try
            end repeat
          end repeat
        end tell
        '''
        _osascript_ok(script)

    def activate_window() -> None:
        _osascript_ok('''
        tell application "System Events"
          repeat with proc in (every process whose background only is false)
            repeat with win in windows of proc
              try
                set n to name of win as text
                if n starts with "AgentOffice" then
                  set frontmost of proc to true
                  try
                    perform action "AXRaise" of win
                  end try
                  return
                end if
              end try
            end repeat
          end repeat
        end tell
        ''')
        # Also poke Chrome by name — app-mode windows often live here.
        _osascript_ok('tell application "Google Chrome" to activate')

    def close_window() -> None:
        _osascript_ok('''
        tell application "System Events"
          repeat with proc in (every process whose background only is false)
            repeat with win in windows of proc
              try
                set n to name of win as text
                if n starts with "AgentOffice" then
                  try
                    click button 1 of win
                  end try
                end if
              end try
            end repeat
          end repeat
        end tell
        ''')

    def launch_browser(exe: str, url: str) -> subprocess.Popen:
        profile = ROOT / "state" / "island-browser"
        profile.mkdir(parents=True, exist_ok=True)
        # Start at PANEL size so the window is impossible to miss (not a 44px pill).
        x = max(0, (screen_w - PANEL[0]) // 2)
        y = max(40, TOP_MARGIN + 20)
        app_url = f"{url.rstrip('/')}/?shell=desktop"
        return subprocess.Popen([
            exe,
            f"--app={app_url}",
            f"--user-data-dir={str(profile)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            f"--window-size={PANEL[0]},{PANEL[1]}",
            f"--window-position={x},{y}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Second launch: just raise the existing window.
    existing = read_title()
    if _ours_title(existing):
        activate_window()
        place_window(*PANEL)
        notify("AgentOffice is already running — brought it to the front.")
        return 0

    # Port failover: don't attach to a foreign service on :8000.
    if args.serve:
        args.url = resolve_serve_url(args.url)
    server = None
    if not is_agentoffice(args.url):
        if not args.serve:
            notify(f"The AgentOffice API is not running at {args.url}.\n"
                   "Start it with `python run.py`, or pass --serve.", error=True)
            return 1
        if not port_free(int(_port_of(args.url))) and not is_agentoffice(args.url):
            args.url = resolve_serve_url(args.url)
        server = start_server(args.url)
        if not is_agentoffice(args.url):
            notify("AgentOffice could not start its background service.\n"
                   "See logs/agentoffice-server.log (is another app using the port?).", error=True)
            if server is not None:
                server.terminate()
            return 1

    prefer = args.browser if args.browser in ("chrome", "edge") else "chrome"
    exe = browser_path(prefer) or browser_path("chrome") or browser_path("edge")
    if exe is None:
        notify("Google Chrome or Microsoft Edge is required for the Mac app.\n"
               f"Install Chrome, or open {args.url} in any browser.", error=True)
        if server is not None:
            server.terminate()
        return 1

    browser = launch_browser(exe, args.url)
    # Wait for document.title from the page (JS sets "AgentOffice · …").
    title = ""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        title = read_title()
        if _ours_title(title):
            break
        if browser.poll() is not None:
            break
        time.sleep(0.25)

    if not _ours_title(title):
        # Last resort: open a normal Chrome window so the user always sees something.
        subprocess.run(["open", "-a", "Google Chrome", args.url], check=False)
        notify("Could not attach to the floating app window (Accessibility may be required).\n"
               f"Opened {args.url} in Chrome instead.\n"
               "System Settings → Privacy & Security → Accessibility → allow Terminal/Python, "
               "then run AgentOffice.command again.", error=True)
        # Keep supervising the API until Ctrl+C so --serve stays useful.
        try:
            while True:
                if server is not None and server.poll() is not None:
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            if browser.poll() is None:
                browser.terminate()
            if server is not None:
                server.terminate()
        return 1

    place_window(*PANEL)
    activate_window()
    notify(f"AgentOffice is open on Mac ({args.url}).\n"
           "Use the panel; collapse to the top pill when you want.\n"
           "Quit from the power button in the panel.")

    last_mode = "panel"
    place_window(*PANEL)
    missing = 0
    relaunches = 0
    user_quit = False
    try:
        while not user_quit:
            if browser.poll() is not None:
                if relaunches >= 3:
                    notify("The AgentOffice window closed repeatedly. Open "
                           f"{args.url} in Chrome, or run AgentOffice.command again.", error=True)
                    break
                relaunches += 1
                browser = launch_browser(exe, args.url)
                time.sleep(1.5)
                activate_window()
                place_window(*PANEL)
                continue

            title = read_title()
            if not _ours_title(title):
                missing += 1
                # Brief AX glitches shouldn't kill the app.
                if missing > 25:   # ~3s of misses
                    missing = 0
                    activate_window()
                time.sleep(0.12)
                continue
            missing = 0
            mode, _ = parse_title(title)
            if mode == "quit":
                user_quit = True
                break
            if mode != last_mode:
                place_window(*(PANEL if mode == "panel" else ISLAND))
                last_mode = mode
                if mode == "panel":
                    activate_window()
            time.sleep(0.12)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        close_window()
        if browser.poll() is None:
            browser.terminate()
            try:
                browser.wait(timeout=3)
            except subprocess.TimeoutExpired:
                browser.kill()
        if server is not None:
            server.terminate()
    return 0


# ------------------------------------------------------------------ entry

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AgentOffice desktop island (Windows and macOS).")
    parser.add_argument("--url", default=f"http://localhost:{os.environ.get('PORT', '8000')}")
    parser.add_argument("--serve", action="store_true", help="start the API (run.py) if it is not running")
    parser.add_argument("--browser", choices=("edge", "chrome"), default="edge")
    args = parser.parse_args(argv)

    if os.name == "nt":
        return _main_windows(args)
    if sys.platform == "darwin":
        argv_list = sys.argv if argv is None else list(argv)
        if "--browser" not in argv_list:
            args.browser = "chrome"
        if args.serve:
            args.url = resolve_serve_url(args.url)
        return _main_darwin(args)

    print(f"The floating desktop island runs on Windows and macOS. "
          f"On {platform.system()}, open the chat UI in a browser:", args.url)
    return 1


if __name__ == "__main__":
    sys.exit(main())
