#!/usr/bin/env python3
"""
MacTV - turns your Mac into a home Apple TV.

  * Giant full-screen interface on the TV, navigable with arrow keys.
  * Web-based remote control for your phone.
  * Each app opens on its own desktop (macOS full screen) and closing it
    brings you back to MacTV.

No dependencies: just Python 3 and macOS.
"""

import ctypes
import ctypes.util
import hashlib
import json
import os
import plistlib
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = int(os.environ.get("MACTV_PORT", "8777"))
# Seconds the addresses stay visible in the terminal before MacTV takes over
# the screen with its own desktop. MACTV_DELAY=0 skips it.
LAUNCH_DELAY = float(os.environ.get("MACTV_DELAY", "3"))
HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
CACHE = Path.home() / ".mactv"
ICONS = CACHE / "icons"
CONFIG = CACHE / "config.json"
SLEEPFILE = CACHE / "sleep.json"

CONFIG_VERSION = 3

APP_DIRS = [
    Path("/Applications"),
    Path("/Applications/Utilities"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path.home() / "Applications",
    Path.home() / "Applications/Brave Browser Apps.localized",
    Path.home() / "Applications/Chrome Apps.localized",
]

DEFAULT_FEATURED = ["Netflix", "YouTube", "Disney+", "Twitch", "Stremio", "Spotify"]

DEFAULT_HIDDEN = [
    "Activity Monitor", "AirPort Utility", "Android Studio", "App Store",
    "Audio MIDI Setup", "Automator", "Battle.net", "Blackmagic Proxy Generator Lite",
    "Bluetooth File Exchange", "Books", "Boot Camp Assistant", "Calculator",
    "Canva", "ChatGPT Classic", "Chess", "Claude", "Claude Code URL Handler",
    "Clock", "ColorSync Utility", "Console", "Contacts", "Dictionary",
    "Digital Color Meter", "Discord", "Disk Utility", "Dolphin", "FaceTime",
    "Feedback Assistant", "Find My", "FindMy", "Font Book", "Freeform", "Games",
    "Grapher", "Home", "Image Capture", "Image Playground", "IntelliJ IDEA",
    "Journal", "Keychain Access", "Magnifier", "Mail", "Maps", "Messages",
    "Microsoft To Do", "Migration Assistant", "MongoDB Compass", "Music", "News",
    "Notes", "Notion", "Pages", "Pages Creator Studio", "Passwords", "Phone",
    "Photo Booth", "Photos", "Podcasts", "Preview", "Print Center",
    "QuickTime Player", "RAID Utility", "Reminders", "Screen Sharing",
    "Screenshot", "Script Editor", "Shortcuts", "Siri", "Stickies", "Stocks",
    "System Information", "TablePlus", "Terminal", "Texas Poker", "TextEdit",
    "The Unarchiver", "Ticket Viewer", "Time Machine", "Tips", "TV",
    "Visual Studio Code", "VLC", "VMware Fusion", "VoiceInk", "Voice Memos",
    "VoiceMemos", "VoiceOver Utility", "Weather", "Wireless Diagnostics",
    "qbittorrent",
]

HIDE_RE = re.compile(
    r"(uninstall|installer|helper|updater|diagnos|setup assistant|"
    r"crash report|remote desktop host)", re.I)

SLEEP_OPTIONS = [5, 10, 15, 30, 45, 60]

STATE = {
    "apps": [], "featured": [], "by_id": {}, "cfg": {},
    "sleep_options": SLEEP_OPTIONS,
    "launcher_title": "MacTV", "launcher_pid": 0, "launcher_win": None,
    "fresh_pids": set(), "running": {},
}

# ---------------------------------------------------------------------------
# Event injection (CoreGraphics via ctypes)
# ---------------------------------------------------------------------------

_cg = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
_cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
_cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
_cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
_cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
_cg.CGEventCreateScrollWheelEvent.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32]
_cg.CGEventCreate.restype = ctypes.c_void_p
_cg.CGEventCreate.argtypes = [ctypes.c_void_p]
_cg.CGEventGetLocation.restype = CGPoint
_cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
_cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_cg.CGEventKeyboardSetUnicodeString.argtypes = [
    ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]
_cf.CFRelease.argtypes = [ctypes.c_void_p]
_cg.AXIsProcessTrusted.restype = ctypes.c_bool

HID_TAP = 0
MOUSE_MOVED, L_DOWN, L_UP, R_DOWN, R_UP, SCROLL = 5, 1, 2, 3, 4, 22

MODS = {"cmd": 1 << 20, "shift": 1 << 17, "alt": 1 << 19, "ctrl": 1 << 18, "fn": 1 << 23}

KEYS = {
    "up": 126, "down": 125, "left": 123, "right": 124,
    "enter": 36, "return": 36, "tab": 48, "space": 49,
    "backspace": 51, "delete": 51, "esc": 53, "escape": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f": 3, "k": 40, "m": 46, "j": 38, "l": 37, "c": 8, "w": 13, "t": 17, "r": 15,
    "q": 12, "n": 45, "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23,
    "minus": 27, "plus": 24, "comma": 43, "period": 47, "slash": 44,
    "volumeup": 72, "volumedown": 73, "mute": 74, "f11": 103, "f12": 111,
}

_input_lock = threading.Lock()


def _flags(mods):
    f = 0
    for m in mods or []:
        f |= MODS.get(str(m).lower(), 0)
    return f


def press(key, mods=None):
    code = KEYS.get(str(key).lower())
    if code is None:
        return False
    f = _flags(mods)
    with _input_lock:
        for down in (True, False):
            ev = _cg.CGEventCreateKeyboardEvent(None, code, down)
            if f:
                _cg.CGEventSetFlags(ev, f)
            _cg.CGEventPost(HID_TAP, ev)
            _cf.CFRelease(ev)
    return True


def type_text(text):
    if not text:
        return
    with _input_lock:
        for chunk in (text[i:i + 16] for i in range(0, len(text), 16)):
            buf = chunk.encode("utf-16-le")
            n = len(buf) // 2
            arr = (ctypes.c_uint16 * n).from_buffer_copy(buf)
            for down in (True, False):
                ev = _cg.CGEventCreateKeyboardEvent(None, 0, down)
                _cg.CGEventKeyboardSetUnicodeString(ev, n, arr)
                _cg.CGEventPost(HID_TAP, ev)
                _cf.CFRelease(ev)
            time.sleep(0.004)


def _cursor():
    ev = _cg.CGEventCreate(None)
    p = _cg.CGEventGetLocation(ev)
    _cf.CFRelease(ev)
    return p


def mouse_move(dx, dy):
    p = _cursor()
    with _input_lock:
        ev = _cg.CGEventCreateMouseEvent(
            None, MOUSE_MOVED, CGPoint(p.x + float(dx), p.y + float(dy)), 0)
        _cg.CGEventPost(HID_TAP, ev)
        _cf.CFRelease(ev)


def click_at(x, y):
    """Click at a specific point on the screen."""
    pt = CGPoint(float(x), float(y))
    with _input_lock:
        ev = _cg.CGEventCreateMouseEvent(None, MOUSE_MOVED, pt, 0)
        _cg.CGEventPost(HID_TAP, ev); _cf.CFRelease(ev)
        for t in (L_DOWN, L_UP):
            ev = _cg.CGEventCreateMouseEvent(None, t, pt, 0)
            _cg.CGEventPost(HID_TAP, ev); _cf.CFRelease(ev)


def mouse_click(button="left"):
    p = _cursor()
    down, up, btn = (L_DOWN, L_UP, 0) if button == "left" else (R_DOWN, R_UP, 1)
    with _input_lock:
        for t in (down, up):
            ev = _cg.CGEventCreateMouseEvent(None, t, p, btn)
            _cg.CGEventPost(HID_TAP, ev)
            _cf.CFRelease(ev)


def mouse_scroll(dy, dx=0):
    with _input_lock:
        ev = _cg.CGEventCreateScrollWheelEvent(None, 0, 2, int(dy), int(dx))
        _cg.CGEventPost(HID_TAP, ev)
        _cf.CFRelease(ev)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def osa(script, lang="AppleScript", timeout=12):
    try:
        r = subprocess.run(["osascript", "-l", lang, "-e", script],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def app_info(app_path):
    try:
        with open(Path(app_path) / "Contents/Info.plist", "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def bundle_id(app_path):
    return app_info(app_path).get("CFBundleIdentifier") or ""


def browser():
    for name, path in (("Brave Browser", "/Applications/Brave Browser.app"),
                       ("Google Chrome", "/Applications/Google Chrome.app"),
                       ("Microsoft Edge", "/Applications/Microsoft Edge.app"),
                       ("Chromium", "/Applications/Chromium.app")):
        if os.path.isdir(path):
            return name, path
    return None, None


def browser_bundle():
    _, path = browser()
    return bundle_id(path) if path else ""


def local_addresses():
    """All the machine's IPv4 addresses, main one first."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True,
                             timeout=6).stdout
    except Exception:
        out = ""
    seen, ips = set(), []
    for ip in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out):
        if ip.startswith("127.") or ip in seen:
            continue
        seen.add(ip)
        ips.append(ip)
    main = lan_ip()
    if main in ips:
        ips.remove(main)
    if main:
        ips.insert(0, main)
    return ips


def remote_url():
    host = (STATE["cfg"].get("remote_host") or "").strip()
    return f"{host or lan_ip()}:{PORT}/remote"


def lan_ip():
    for iface in ("en0", "en1", "en2"):
        try:
            ip = subprocess.run(["ipconfig", "getifaddr", iface],
                                capture_output=True, text=True, timeout=3).stdout.strip()
            if ip:
                return ip
        except Exception:
            pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
_typing = {"at": 0.0, "value": False}


def typing_now():
    """True if the frontmost app has focus on a text field."""
    now = time.time()
    if now - _typing["at"] < 1.0:
        return _typing["value"]
    out = osa('''
    tell application "System Events"
      try
        set p to first process whose frontmost is true
        return (role of (value of attribute "AXFocusedUIElement" of p)) as text
      on error
        return "none"
      end try
    end tell''', timeout=6)
    _typing["at"] = time.time()
    _typing["value"] = out in TEXT_ROLES
    return _typing["value"]


# ---------------------------------------------------------------------------
# Windows
#
# Accessibility alone isn't enough: once a window goes full screen, System
# Events stops counting it, and the browser's web apps (Netflix, YouTube...)
# aren't their own processes but windows of the browser, so "Brave's front
# window" could be any tab the user has open.
#
# That's why it uses CoreGraphics' window list instead, which gives the
# identifier, PID and exact frame of every window, and then targets the
# specific window by its frame.
# ---------------------------------------------------------------------------

# 16 = exclude desktop elements; +1 = only the ones on screen, i.e. only the
# ones on the active desktop.
WINLIST_JXA = r"""
ObjC.import("CoreGraphics"); ObjC.import("Foundation"); ObjC.import("AppKit");
var scr = $.NSScreen.mainScreen.frame;
var arr = ObjC.castRefToObject($.CGWindowListCopyWindowInfo(OPTS, 0));
var n = arr.count, out = [];
for (var i = 0; i < n; i++) {
  var w = arr.objectAtIndex(i);
  if (ObjC.unwrap(w.objectForKey("kCGWindowLayer")) !== 0) continue;
  var b = w.objectForKey("kCGWindowBounds");
  if (!b) continue;
  var wd = ObjC.unwrap(b.objectForKey("Width"));
  var h  = ObjC.unwrap(b.objectForKey("Height"));
  if (wd < 150 || h < 100) continue;
  out.push([ObjC.unwrap(w.objectForKey("kCGWindowNumber")),
            ObjC.unwrap(w.objectForKey("kCGWindowOwnerPID")),
            ObjC.unwrap(b.objectForKey("X")), ObjC.unwrap(b.objectForKey("Y")),
            wd, h]);
}
JSON.stringify({screen: [scr.size.width, scr.size.height], win: out});
"""

# (id, pid, x, y, ancho, alto)
W_ID, W_PID, W_X, W_Y, W_W, W_H = range(6)


def window_snapshot(onscreen=False):
    """((width, height) of the screen, [windows]).

    With `onscreen` it only returns the ones on the active desktop, which is
    how we know which desktop we're on.
    """
    try:
        js = WINLIST_JXA.replace("OPTS", "17" if onscreen else "16")
        d = json.loads(osa(js, lang="JavaScript", timeout=10) or "{}")
        return tuple(d.get("screen") or (0, 0)), [tuple(x) for x in d.get("win", [])]
    except Exception:
        return (0, 0), []


def pids_of(bundle):
    if not bundle:
        return set()
    out = osa('tell application "System Events" to get unix id of '
              f'(every process whose bundle identifier is "{bundle}")', timeout=8)
    return {int(x) for x in re.findall(r"\d+", out)}


def is_fullscreen(win, scr):
    return bool(scr and scr[0]) and win[W_W] >= scr[0] - 1 and win[W_H] >= scr[1] - 60


def frontmost_pid():
    out = osa('tell application "System Events" to return (unix id of '
              '(first process whose frontmost is true)) as text', timeout=8)
    try:
        return int(out)
    except ValueError:
        return 0


def focused_frame(pid):
    """Marco (x, y, ancho, alto) de la ventana con el foco en ese proceso."""
    out = osa(f'''
    tell application "System Events"
      set ps to (every process whose unix id is {pid})
      if ps is {{}} then return "none"
      try
        set w to value of attribute "AXFocusedWindow" of (item 1 of ps)
        set pp to position of w
        set ss to size of w
        return ((item 1 of pp) as text) & "," & ((item 2 of pp) as text) & "," & \
               ((item 1 of ss) as text) & "," & ((item 2 of ss) as text)
      on error
        return "none"
      end try
    end tell''', timeout=8)
    nums = re.findall(r"-?\d+", out)
    return tuple(int(n) for n in nums[:4]) if len(nums) >= 4 else None


def match_window(wins, pid, frame, tol=24, wy=1.0):
    """The window in `wins` belonging to that process whose frame is closest
    to `frame`.

    `wy` weighs vertical differences less: a full-screen browser reports
    taking up the whole height, while the system subtracts the menu bar, so
    width is the reliable signal.
    """
    best, dist = None, 1e9
    for w in wins:
        if pid and w[W_PID] != pid:
            continue
        d = (abs(w[W_X] - frame[0]) + abs(w[W_W] - frame[2])
             + wy * (abs(w[W_Y] - frame[1]) + abs(w[W_H] - frame[3])))
        if d < dist:
            best, dist = w, d
    return best if dist <= tol else None


def focused_window(pid):
    """The focused window of that process, as an entry from the CG list."""
    fr = focused_frame(pid)
    if not fr:
        return None
    return match_window(window_snapshot()[1], pid, fr)


def window_action(pid, win, body, tol=12):
    """Runs `body` on THE window of that process whose frame matches.

    Acting on "the front window" doesn't work: the launcher and the web apps
    are windows of the same browser, and you end up touching a random tab.
    """
    return osa(f'''
    tell application "System Events"
      set ps to (every process whose unix id is {pid})
      if ps is {{}} then return "gone"
      repeat with w in windows of (item 1 of ps)
        try
          set pp to position of w
          set ss to size of w
          set ex to (item 1 of pp) - {int(win[W_X])}
          set ey to (item 2 of pp) - {int(win[W_Y])}
          set ew to (item 1 of ss) - {int(win[W_W])}
          set eh to (item 2 of ss) - {int(win[W_H])}
          if ex < 0 then set ex to -ex
          if ey < 0 then set ey to -ey
          if ew < 0 then set ew to -ew
          if eh < 0 then set eh to -eh
          if ex < {tol} and ey < {tol} and ew < {tol} and eh < {tol} then
            {body}
            return "ok"
          end if
        end try
      end repeat
      return "nomatch"
    end tell''', timeout=14)


def activate_pid(pid):
    return osa(f'''
    tell application "System Events"
      set ps to (every process whose unix id is {pid})
      if ps is {{}} then return "gone"
      set frontmost of item 1 of ps to true
      return "ok"
    end tell''', timeout=10) == "ok"


def raise_and_activate(pid, win):
    """Brings that window to the front and activates its app, in that order."""
    window_action(pid, win, 'perform action "AXRaise" of w')
    return activate_pid(pid)


def hide_pid(pid):
    """Hides that app (Cmd+H). If it was full screen, macOS leaves its
    desktop, which is exactly what's needed to get back to MacTV's."""
    if not pid:
        return False
    return osa(f'''
    tell application "System Events"
      set ps to (every process whose unix id is {pid})
      if ps is {{}} then return "gone"
      try
        set visible of item 1 of ps to false
      end try
      return "ok"
    end tell''', timeout=8) == "ok"


def make_fullscreen(pid, win, before, alive=lambda: True):
    """Makes full screen the window the app just opened.

    On macOS, full screen is, precisely, its own desktop. The result is
    confirmed by MEASURING the frame: once in full screen, accessibility
    stops seeing the window, so asking it doesn't work.
    """
    def look():
        scr, wins = window_snapshot()
        cur = next((w for w in wins if w[W_ID] == win[W_ID]), None)
        done = any(w[W_PID] == pid and w[W_ID] not in before and is_fullscreen(w, scr)
                   for w in wins)
        return cur, done

    cur, done = look()
    if done:
        return True
    if cur is None:
        return False

    # 1. window to the front, its app active
    raise_and_activate(pid, cur)
    time.sleep(0.7)

    # 2. full screen on that same window
    for _ in range(2):
        if not alive():
            return False
        cur, done = look()
        if done:
            return True
        if cur is None:
            return False
        window_action(pid, cur,
                      'set value of attribute "AXFullScreen" of w to true')
        for _ in range(5):
            if not alive():
                return False
            time.sleep(0.9)
            if look()[1]:
                return True

    # 3. plan B: the system shortcut, only if the app is in front and the
    #    focused window really is ours
    if not alive() or frontmost_pid() != pid:
        return False
    fw = focused_window(pid)
    if not fw or fw[W_ID] != win[W_ID]:
        return False
    press("f", ["cmd", "ctrl"])
    for _ in range(6):
        if not alive():
            return False
        time.sleep(0.9)
        if look()[1]:
            return True
    return False


def wait_for_window(owner_bundle, before, timeout=18, alive=lambda: True,
                    prefer_pids=None):
    """Waits for a new window of that bundle to appear.

    `prefer_pids` lets you keep only the ones from specific processes: the
    launcher starts its own browser instance, so it isn't mixed up with a
    window the user opens at the same time in theirs.
    """
    deadline = time.time() + timeout
    while time.time() < deadline and alive():
        pids = pids_of(owner_bundle)
        if prefer_pids is not None:
            pids &= prefer_pids
        if pids:
            _, wins = window_snapshot()
            new = [w for w in wins if w[W_PID] in pids and w[W_ID] not in before]
            if new:
                return max(new, key=lambda w: w[W_W] * w[W_H])
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    data = {}
    if CONFIG.exists():
        try:
            data = json.loads(CONFIG.read_text())
        except Exception:
            data = {}
    if data.get("version") != CONFIG_VERSION:
        data = {"version": CONFIG_VERSION, "featured": DEFAULT_FEATURED,
                "hidden": DEFAULT_HIDDEN, "sleep_options": SLEEP_OPTIONS,
                "fullscreen": True, "return_home": True, "remote_host": ""}
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    data.setdefault("featured", DEFAULT_FEATURED)
    data.setdefault("hidden", DEFAULT_HIDDEN)
    data.setdefault("sleep_options", SLEEP_OPTIONS)
    data.setdefault("fullscreen", True)
    data.setdefault("return_home", True)
    data.setdefault("remote_host", "")
    return data


def save_config():
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(STATE["cfg"], indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Apps and icons
# ---------------------------------------------------------------------------

def slug(s):
    return hashlib.md5(s.encode()).hexdigest()[:12]


def scan_apps(hidden):
    hidden = {h.lower() for h in hidden}
    seen, apps = set(), []
    for d in APP_DIRS:
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.suffix != ".app" or not p.is_dir():
                continue
            name = p.stem
            key = name.lower()
            if key in seen or key in hidden or HIDE_RE.search(name):
                continue
            seen.add(key)
            info = app_info(p)
            apps.append({"id": slug(str(p)), "name": name, "path": str(p),
                         "bundle": info.get("CFBundleIdentifier") or "",
                         "url": info.get("CrAppModeShortcutURL") or ""})
    apps.sort(key=lambda a: a["name"].lower())
    return apps


def split_featured(apps, featured_names):
    by_name = {a["name"].lower(): a for a in apps}
    featured, used = [], set()
    for n in featured_names:
        a = by_name.get(n.lower())
        if a and a["id"] not in used:
            featured.append(a)
            used.add(a["id"])
    for a in apps:
        if a["id"] not in used and ("Browser Apps" in a["path"]
                                    or "Chrome Apps" in a["path"]):
            featured.append(a)
            used.add(a["id"])
    return featured, [a for a in apps if a["id"] not in used]


ICON_JXA = r'''
ObjC.import("AppKit");
var jobs = JSON.parse($.NSString.stringWithContentsOfFileEncodingError(
    ARG_FILE, $.NSUTF8StringEncoding, $()).js);
var ws = $.NSWorkspace.sharedWorkspace;
jobs.forEach(function (j) {
  try {
    var img = ws.iconForFile(j[0]);
    var rep = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
    var png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $());
    png.writeToFileAtomically(j[1], true);
  } catch (e) {}
});
"done";
'''


def build_icons(apps, log=print):
    ICONS.mkdir(parents=True, exist_ok=True)
    todo = [(a["path"], str(ICONS / (a["id"] + ".png")))
            for a in apps if not (ICONS / (a["id"] + ".png")).exists()]
    if not todo:
        return
    log(f"  extracting {len(todo)} icons (first run only)...", flush=True)
    jobs = CACHE / "iconjobs.json"
    raw = CACHE / "raw"
    raw.mkdir(exist_ok=True)
    jobs.write_text(json.dumps([(s, str(raw / Path(d).name)) for s, d in todo]))
    osa(ICON_JXA.replace("ARG_FILE", json.dumps(str(jobs))),
        lang="JavaScript", timeout=600)
    procs = []
    for _, dst in todo:
        src = raw / Path(dst).name
        if not src.exists():
            continue
        procs.append(subprocess.Popen(["sips", "-Z", "256", str(src), "--out", dst],
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL))
        if len(procs) >= 12:
            procs.pop(0).wait()
    for pr in procs:
        pr.wait()
    subprocess.run(["rm", "-rf", str(raw)], capture_output=True)
    jobs.unlink(missing_ok=True)
    log(f"  icons ready: "
        f"{sum(1 for a in apps if (ICONS / (a['id'] + '.png')).exists())}/{len(apps)}",
        flush=True)


# ---------------------------------------------------------------------------
# Sleep timer
# ---------------------------------------------------------------------------

def sleep_status():
    if not SLEEPFILE.exists():
        return {"active": False}
    try:
        d = json.loads(SLEEPFILE.read_text())
        os.kill(d["pid"], 0)
    except Exception:
        SLEEPFILE.unlink(missing_ok=True)
        return {"active": False}
    left = d["deadline"] - time.time()
    if left <= 0:
        return {"active": False}
    return {"active": True, "remaining": int(left), "minutes": d["minutes"]}


def cancel_sleep():
    if not SLEEPFILE.exists():
        return
    try:
        d = json.loads(SLEEPFILE.read_text())
        os.killpg(os.getpgid(d["pid"]), signal.SIGTERM)
    except Exception:
        pass
    SLEEPFILE.unlink(missing_ok=True)


def schedule_sleep(minutes):
    """Separate process, so it survives even if MacTV is closed."""
    cancel_sleep()
    minutes = int(minutes)
    if minutes <= 0:
        return {"active": False}
    secs = minutes * 60
    p = subprocess.Popen(
        ["/bin/sh", "-c",
         f"""sleep {secs}; osascript -e 'tell application "System Events" to sleep'"""],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    SLEEPFILE.write_text(json.dumps(
        {"pid": p.pid, "deadline": time.time() + secs, "minutes": minutes}))
    return sleep_status()


# ---------------------------------------------------------------------------
# Launching apps and returning to MacTV
# ---------------------------------------------------------------------------

_launch = {"token": 0}

# Flags that actually get real full screen (verified):
# --start-fullscreen is only honored by the process that creates the window,
# which is why each app opens in its own browser instance.
BROWSER_FLAGS = ["--start-fullscreen", "--no-first-run", "--no-default-browser-check"]


def on_this_desktop(pid):
    """Is there a window of that process on the desktop I'm currently viewing?"""
    if not pid:
        return False
    return any(w[W_PID] == pid for w in window_snapshot(onscreen=True)[1])


def launcher_fullscreen(pid=None):
    """Is MacTV full screen, i.e. on its own desktop?"""
    pid = pid or STATE.get("launcher_pid")
    if not pid:
        return False
    scr, wins = window_snapshot()
    return any(w[W_PID] == pid and is_fullscreen(w, scr) for w in wins)


def goto_desktop():
    """Goes to the desktop where MacTV is.

    With MacTV full screen, its desktop belongs only to it, and you get there
    by activating its process: the launcher is its own browser instance with
    a single window, so activating it can't lead anywhere else. The only
    thing that gets in the way is coming from a full-screen app, because
    macOS stays on it; that's why it hides it first (hiding a full-screen app
    returns you to the previous desktop).

    If the launcher lost full screen it lives on the normal desktop, and then
    the usual path applies: activate some OTHER app that's there, and the
    Finder is always there.
    """
    pid = STATE.get("launcher_pid")
    if not pid:
        return False
    if on_this_desktop(pid):
        return True
    if launcher_fullscreen(pid):
        for _ in range(3):
            for other in list(STATE["running"].values()):
                if other != pid and proc_alive(other):
                    hide_pid(other)
            activate_pid(pid)
            for _ in range(7):             # the animation takes almost a second
                time.sleep(0.4)
                if on_this_desktop(pid):
                    return True
        return False
    for _ in range(3):
        osa('tell application "Finder" to activate', timeout=8)
        for _ in range(7):                 # the animation takes almost a second
            time.sleep(0.4)
            if on_this_desktop(pid):
                return True
    return False


def goto_normal_desktop():
    """Lands on a normal desktop (the Finder's) before opening an app.

    --start-fullscreen is only applied by macOS if the browser launches from
    a normal desktop, and accessibility can only touch windows on the active
    desktop. Since MacTV is full screen, it has to step out first.
    """
    pid = STATE.get("launcher_pid")
    if not launcher_fullscreen(pid):
        # MacTV already lives on the normal desktop: just go back to it
        return goto_desktop()
    for _ in range(3):
        osa('tell application "Finder" to activate', timeout=8)
        for _ in range(7):
            time.sleep(0.4)
            if not on_this_desktop(pid):
                return True
    return False


def launcher_window(pid):
    wins = windows_of(pid)
    if not wins:
        return None
    win = max(wins, key=lambda w: w[W_W] * w[W_H])
    STATE["launcher_win"] = win
    return win


def windows_of(pid):
    return [w for w in window_snapshot()[1] if w[W_PID] == pid]


def proc_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def import_session(profile, log=lambda *a: None):
    """Copies cookies and storage from the user's Brave into the app's profile.

    That way you don't have to log back into Netflix and friends. If
    something fails, no harm done: the app will just ask you to log in once.
    """
    src = Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser"
    dst = Path(profile)
    if not src.is_dir():
        return
    try:
        # Local State is NOT copied. On macOS the cookie encryption key lives
        # in the Keychain ("Brave Safe Storage"), not there, so copying it
        # adds nothing; and copying it drags along "app_shims", the registry
        # of the regular Brave's web apps. Brave would think they're
        # installed in this profile too, try to reconcile their .app from
        # ~/Applications, and throw an error dialog for each one on launch.
        for rel in ("Default/Cookies", "Default/Network/Cookies"):
            f = src / rel
            if f.exists():
                (dst / rel).parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["cp", str(f), str(dst / rel)], capture_output=True)
        ls = src / "Default/Local Storage"
        if ls.is_dir():
            (dst / "Default").mkdir(parents=True, exist_ok=True)
            subprocess.run(["cp", "-R", str(ls), str(dst / "Default/Local Storage")],
                           capture_output=True)
    except Exception:
        pass


def singleton_owner(profile):
    """PID of the browser holding that profile, or 0 if nobody has it.

    Chromium leaves SingletonLock as a symlink to "<host>-<pid>".
    """
    try:
        target = os.readlink(Path(profile) / "SingletonLock")
    except OSError:
        return 0
    try:
        pid = int(target.rsplit("-", 1)[-1])
    except ValueError:
        return 0
    if pid <= 0 or not proc_alive(pid):
        return 0
    # in case the PID got recycled and is now something else entirely
    comm = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                          capture_output=True, text=True).stdout
    return pid if (browser()[0] or "?") in comm else 0


def clear_singleton(profile):
    """Removes the locks the browser leaves behind if it was force-quit.

    If they're left behind, next time the process starts up and quits right
    away.

    Only removed if nobody's really still behind them. Removing them with
    the process still alive leaves two browsers on the same profile, and
    then the second one can't lock any database: Chromium throws a
    "There was a problem opening your profile" error for every subsystem
    that fails. Returns False if the profile is busy.
    """
    if singleton_owner(profile):
        return False
    for n in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (Path(profile) / n).unlink()
        except (FileNotFoundError, OSError):
            pass
    return True


def release_profile(profile, timeout=8):
    """Closes the browser holding the profile and waits for it to let go."""
    pid = singleton_owner(profile)
    if not pid:
        clear_singleton(profile)
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not singleton_owner(profile):
            break
        time.sleep(0.25)
    return clear_singleton(profile)


def launch_browser_app(app):
    """Opens a web app in its own browser instance, full screen from the very
    first moment (and therefore on its own desktop)."""
    name, path = browser()
    if not name:
        return 0
    profile = CACHE / "apps" / app["id"]
    # Survived a previous MacTV: go back to that window. Opening a second
    # browser on the same profile would just throw profile errors.
    alive = singleton_owner(str(profile))
    if alive:
        activate_pid(alive)
        return alive
    first = not profile.exists()
    seed_profile(str(profile))
    clear_singleton(profile)
    if first:
        import_session(str(profile))
    proc = subprocess.Popen(
        [f"{path}/Contents/MacOS/{Path(path).stem}",
         f"--user-data-dir={profile}", f"--app={app['url']}"] + BROWSER_FLAGS,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return proc.pid


def discover_launcher():
    """Fallback: finds the launcher process by its window title."""
    out = osa(f'''
    tell application "System Events"
      repeat with p in (every process whose bundle identifier is "{browser_bundle()}")
        try
          repeat with w in windows of p
            if name of w contains "{STATE['launcher_title']}" then
              return (unix id of p) as text
            end if
          end repeat
        end try
      end repeat
      return "none"
    end tell''', timeout=12)
    try:
        return int(out)
    except ValueError:
        return 0


def register_launcher(frame):
    """The launcher page reports where its window is; serves as a fallback
    if the PID were ever lost (for example if the browser restarts)."""
    pid = STATE.get("launcher_pid")
    if pid and proc_alive(pid):
        wins = windows_of(pid)
        if wins:
            STATE["launcher_win"] = max(wins, key=lambda w: w[W_W] * w[W_H])
            return True
    _, wins = window_snapshot()
    pool = [w for w in wins if w[W_PID] in pids_of(browser_bundle())]
    w = match_window(pool, None, frame, tol=90, wy=.35) if pool else None
    if not w:
        return False
    STATE["launcher_pid"] = w[W_PID]
    STATE["launcher_win"] = w
    return True


def go_home():
    """Goes back to MacTV's desktop without closing anything.

    In full screen, MacTV has its own desktop and a single window: once
    there it's already in front and focused, nothing else to do. If it
    isn't, it shares the desktop with other browser windows and two more
    steps are needed: raise it above the rest (already possible: it's on the
    active desktop) and give it back keyboard focus with a click on its
    title bar. Activating the browser by name does NOT work: macOS would go
    to whichever window it has in front, which could be the full-screen
    app's, and we'd be kicked out of the desktop again.
    """
    pid = STATE.get("launcher_pid") or discover_launcher()
    if not pid:
        return False
    STATE["launcher_pid"] = pid
    for _ in range(3):
        if not goto_desktop():
            continue
        if launcher_fullscreen(pid):
            launcher_window(pid)
            return True
        win = launcher_window(pid)
        if win:
            window_action(pid, win, 'perform action "AXRaise" of w')
            click_at(win[W_X] + win[W_W] / 2, win[W_Y] + 8)
            time.sleep(0.4)
        if on_this_desktop(pid):
            return True
    return False


def ensure_fullscreen_later(pid):
    """Safety net: if the window didn't come out full screen, force it via
    accessibility (possible, because it's on the active desktop)."""
    win = None
    for _ in range(30):
        time.sleep(0.5)
        wins = windows_of(pid)
        if wins:
            win = max(wins, key=lambda w: w[W_W] * w[W_H])
            break
    if not win:
        return
    for _ in range(6):
        time.sleep(1.0)
        scr, wins = window_snapshot()
        if any(w[W_PID] == pid and is_fullscreen(w, scr) for w in wins):
            return
    window_action(pid, win, 'perform action "AXRaise" of w')
    window_action(pid, win, 'set value of attribute "AXFullScreen" of w to true')


def supervise(app_id, pid, token, cfg):
    """Watches the app: once it's left with no windows, goes back to MacTV."""
    alive = lambda: _launch["token"] == token
    for _ in range(40):
        if not alive():
            return
        if windows_of(pid):
            break
        time.sleep(0.5)
    else:
        STATE["running"].pop(app_id, None)
        return
    if not cfg.get("return_home", True):
        return
    misses = 0
    while alive():
        time.sleep(1.5)
        if not alive():
            return
        if not proc_alive(pid) or not windows_of(pid):
            misses += 1
            if misses >= 2:
                STATE["running"].pop(app_id, None)
                if alive():
                    go_home()
                return
        else:
            misses = 0


def launch_app(app_id):
    app = STATE["by_id"].get(app_id)
    if not app:
        return False
    cfg = STATE["cfg"]

    # already open: just go to its desktop
    pid = STATE["running"].get(app_id)
    if pid and proc_alive(pid) and windows_of(pid):
        activate_pid(pid)
        return True

    bbid = browser_bundle()
    es_web = bool(app.get("url")) and bbid and app["bundle"].startswith(bbid + ".")

    if es_web and cfg.get("fullscreen", True):
        # --start-fullscreen is only applied by macOS if launched from the
        # normal desktop; from a full-screen desktop it's silently ignored
        goto_normal_desktop()
        pid = launch_browser_app(app)
        if not pid:
            return False
        threading.Thread(target=ensure_fullscreen_later,
                         args=(pid,), daemon=True).start()
    else:
        # native app: the system opens it. If it accepts full screen, it's
        # attempted, but no guarantees: macOS won't let accessibility touch
        # windows that aren't on the active desktop, so we have to leave
        # MacTV's desktop first.
        goto_normal_desktop()
        before = {w[W_ID] for w in window_snapshot()[1]}
        subprocess.run(["open", "-a", app["path"]], capture_output=True)
        activate_pid_of_bundle(app["bundle"])
        win = wait_for_window(app["bundle"], before, timeout=15)
        if not win:
            return True
        pid = win[W_PID]
        if cfg.get("fullscreen", True):
            make_fullscreen(pid, win, before)

    STATE["running"][app_id] = pid
    _launch["token"] += 1
    threading.Thread(target=supervise,
                     args=(app_id, pid, _launch["token"], cfg), daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Standalone actions
# ---------------------------------------------------------------------------

def set_volume(direction):
    if direction == "mute":
        osa("set volume output muted (not output muted of (get volume settings))")
        return
    try:
        v = int(osa("output volume of (get volume settings)"))
    except ValueError:
        v = 40
    osa(f"set volume output volume {max(0, min(100, v + (8 if direction == 'up' else -8)))}")


def system_action(action):
    if action == "sleep":
        osa('tell application "System Events" to sleep')
    elif action == "displaysleep":
        subprocess.Popen(["pmset", "displaysleepnow"])
    elif action == "closewindow":
        press("w", ["cmd"])
    elif action == "quitapp":
        press("q", ["cmd"])
    elif action == "mission":
        press("up", ["ctrl"])
    elif action == "fullscreen":
        press("f", ["cmd", "ctrl"])


# ---------------------------------------------------------------------------
# The launcher
# ---------------------------------------------------------------------------

def strip_app_shims(local_state):
    """Strips the web-app registry from a MacTV profile's Local State.

    If it's there (older versions copied it from the regular Brave), Brave
    thinks the user's PWAs are installed here too and throws an error dialog
    for each one on launch.
    """
    try:
        d = json.loads(local_state.read_text())
    except (OSError, ValueError):
        return
    if not any(k in d for k in ("app_shims", "app_shims_cdhash_hmac_key")):
        return
    d.pop("app_shims", None)
    d.pop("app_shims_cdhash_hmac_key", None)
    try:
        local_state.write_text(json.dumps(d))
    except OSError:
        pass


def seed_profile(profile):
    """Silences the browser's first-run notices."""
    root = Path(profile)
    (root / "Default").mkdir(parents=True, exist_ok=True)
    local = root / "Local State"
    if not local.exists():
        local.write_text(json.dumps({"brave": {
            "p3a": {"notice_acknowledged": True, "enabled": False},
            "stats": {"reporting_enabled": False},
            "new_tab_page": {"show_sponsored_images": False}}}))
    else:
        strip_app_shims(local)
    prefs = root / "Default/Preferences"
    if prefs.exists():
        return
    prefs.write_text(json.dumps({
        "brave": {"rewards": {"inline_tip_buttons_enabled": False},
                  "shields": {"stats_badge_visible": False}},
        "bookmark_bar": {"show_on_all_tabs": False},
        "credentials_enable_service": False,
        "profile": {"exit_type": "Normal", "exited_cleanly": True}}))
    (root / "First Run").touch()


def open_launcher(url):
    """Opens MacTV full screen on its own desktop.

    It launches as its own browser instance, so its PID is the one of the
    process we just started: "Menu" can come back here unambiguously.
    """
    name, path = browser()
    if not name:
        subprocess.Popen(["open", url])
        return
    profile = str(CACHE / "launcher-profile")
    # When exiting with Ctrl+C the browser stays alive (start_new_session).
    # That launcher points at a server that no longer exists, so it's closed
    # before opening the new one: otherwise two Braves would share
    # launcher-profile.
    release_profile(profile)
    seed_profile(profile)
    scr, _ = window_snapshot()
    w, h = int(scr[0] or 1440), int(scr[1] or 900)
    # MacTV full screen, i.e. on its own desktop, just like the apps. It used
    # to stay on the normal one because --start-fullscreen doesn't work if
    # the browser launches from a full-screen desktop; now
    # goto_normal_desktop() handles that, stepping out before opening each
    # app. The window size is still set as a fallback in case full screen
    # doesn't land.
    proc = subprocess.Popen(
        [f"{path}/Contents/MacOS/{Path(path).stem}",
         f"--user-data-dir={profile}", f"--app={url}",
         "--start-fullscreen",
         "--window-position=0,0", f"--window-size={w},{h}",
         "--no-first-run", "--no-default-browser-check"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    STATE["launcher_pid"] = proc.pid

    def settle():
        for _ in range(40):
            wins = windows_of(proc.pid)
            if wins:
                STATE["launcher_win"] = max(wins, key=lambda w: w[W_W] * w[W_H])
                break
            time.sleep(0.5)

    threading.Thread(target=settle, daemon=True).start()
    # red de seguridad: si --start-fullscreen no cuajo, se fuerza despues
    threading.Thread(target=ensure_fullscreen_later,
                     args=(proc.pid,), daemon=True).start()


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", cache=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control",
                         "public, max-age=86400" if cache else "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _file(self, path, ctype, cache=False):
        try:
            self._send(200, Path(path).read_bytes(), ctype, cache)
        except OSError:
            self._send(404, b'{"error":"not found"}')

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            return self._file(WEB / "launcher.html", "text/html; charset=utf-8")
        if p in ("/remote", "/remote/", "/m"):
            return self._file(WEB / "remote.html", "text/html; charset=utf-8")
        if p == "/ui.js":
            return self._file(WEB / "ui.js", "text/javascript; charset=utf-8")
        if p in ("/haptic", "/haptic/"):
            return self._file(WEB / "haptic.html", "text/html; charset=utf-8")
        if p in ("/sounds", "/sounds/"):
            return self._file(WEB / "sounds.html", "text/html; charset=utf-8")
        if p.startswith("/icons/"):
            m = re.fullmatch(r"([a-f0-9]{6,32})\.png", p[7:])
            if not m:
                return self._send(404, b'{"error":"bad icon"}')
            return self._file(ICONS / (m.group(1) + ".png"), "image/png", cache=True)
        if p == "/api/apps":
            pub = lambda a: {"id": a["id"], "name": a["name"]}
            return self._json({"featured": [pub(a) for a in STATE["featured"]],
                               "apps": [pub(a) for a in STATE["apps"]],
                               "remote": remote_url(),
                               "addresses": [f"{ip}:{PORT}/remote"
                                             for ip in local_addresses()],
                               "sleepOptions": STATE["sleep_options"]})
        if p == "/api/sleep":
            return self._json(sleep_status())
        if p == "/api/state":
            return self._json({"typing": typing_now(), "sleep": sleep_status()})
        if p == "/api/ping":
            return self._json({"ok": True})
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}

        if p == "/api/key":
            return self._json({"ok": press(body.get("key", ""), body.get("mods"))})
        if p == "/api/text":
            type_text(body.get("text", ""))
            return self._json({"ok": True})
        if p == "/api/mouse":
            if "dx" in body or "dy" in body:
                mouse_move(body.get("dx", 0), body.get("dy", 0))
            if body.get("click"):
                mouse_click(body["click"])
            if body.get("scroll") is not None:
                mouse_scroll(body["scroll"], body.get("scrollx", 0))
            return self._json({"ok": True})
        if p == "/api/launch":
            launch_app(body.get("id", ""))
            return self._json({"ok": True})
        if p == "/api/home":
            go_home()
            return self._json({"ok": True})
        if p == "/api/volume":
            set_volume(body.get("dir", "up"))
            return self._json({"ok": True})
        if p == "/api/system":
            system_action(body.get("action", ""))
            return self._json({"ok": True})
        if p == "/api/sleep":
            return self._json(schedule_sleep(body.get("minutes", 0)))
        if p == "/api/launcher":
            try:
                frame = (float(body["x"]), float(body["y"]),
                         float(body["w"]), float(body["h"]))
            except (KeyError, TypeError, ValueError):
                return self._json({"ok": False})
            ok = register_launcher(frame)
            return self._json({"ok": ok, "pid": STATE["launcher_pid"]})
        if p == "/api/remote":
            host = re.sub(r"[^A-Za-z0-9.\-:]", "", str(body.get("host", "")))[:120]
            host = host.split(":")[0]
            STATE["cfg"]["remote_host"] = host
            save_config()
            return self._json({"remote": remote_url()})
        self._send(404, b'{"error":"not found"}')


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    print("\n  \033[1mMacTV\033[0m\n")

    if not _cg.AXIsProcessTrusted():
        print("  \033[33mAccessibility permission is missing.\033[0m")
        print("  System Settings > Privacy & Security > Accessibility")
        print("  and enable the terminal app you're using. Then restart.\n")
        osa('tell application "System Events" to open location '
            '"x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"')

    cfg = load_config()
    everything = scan_apps(cfg["hidden"])
    STATE["featured"], STATE["apps"] = split_featured(everything, cfg["featured"])
    STATE["by_id"] = {a["id"]: a for a in everything}
    STATE["sleep_options"] = cfg["sleep_options"]
    STATE["cfg"] = cfg

    print(f"  {len(STATE['featured'])} featured  |  {len(STATE['apps'])} apps",
          flush=True)

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print(f"\n  TV     ->  http://localhost:{PORT}/")
    print(f"  \033[1;36mRemote ->  http://{remote_url()}\033[0m")
    otras = [a for a in local_addresses()][1:]
    if otras:
        print(f"  (other addresses: {', '.join(otras)})")
    print("\n  Ctrl+C to quit.\n", flush=True)

    # Icons go after printing: so what's above shows up right away, not
    # only once extraction finishes.
    build_icons(everything)

    if "--no-ui" not in sys.argv:
        # MacTV opens full screen, i.e. on its own desktop, and the terminal
        # is left behind. This pause gives time to read the addresses before
        # the screen switches.
        time.sleep(LAUNCH_DELAY)
        open_launcher(f"http://localhost:{PORT}/")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n  Goodbye.\n")


if __name__ == "__main__":
    main()
