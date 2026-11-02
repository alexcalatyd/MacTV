#!/usr/bin/env python3
"""
MacTV - convierte el Mac en un Apple TV casero.

  * Interfaz gigante a pantalla completa en la tele, navegable con flechas.
  * Mando a distancia web para el movil.
  * Cada app se abre en su propio escritorio (pantalla completa de macOS) y al
    cerrarla vuelves a MacTV.

Sin dependencias: solo Python 3 y macOS.
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
# Segundos que las direcciones se quedan a la vista en la terminal antes de que
# MacTV se lleve la pantalla a su escritorio. MACTV_DELAY=0 lo quita.
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
# Inyeccion de eventos (CoreGraphics via ctypes)
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
    """Clic en un punto concreto de la pantalla."""
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
# Utilidades
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
    """Todas las IPv4 del equipo, la principal primero."""
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
    """True si la app en primer plano tiene el foco en un campo de texto."""
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
# Ventanas
#
# La accesibilidad sola no basta: cuando una ventana pasa a pantalla completa
# System Events deja de contarla, y las apps web del navegador (Netflix,
# YouTube...) no son procesos propios sino ventanas del navegador, asi que
# "la ventana frontal de Brave" puede ser cualquier pestaña del usuario.
#
# Por eso se usa la lista de ventanas de CoreGraphics, que da identificador,
# PID y marco exacto de cada ventana, y luego se apunta a la ventana concreta
# por su marco.
# ---------------------------------------------------------------------------

# 16 = excluir elementos del escritorio; +1 = solo las que estan en pantalla,
# es decir, solo las del escritorio activo.
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
    """((ancho, alto) de la pantalla, [ventanas]).

    Con `onscreen` solo devuelve las del escritorio activo, que es como se sabe
    en que escritorio estamos.
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
    """La ventana de `wins` de ese proceso cuyo marco mas se parece a `frame`.

    `wy` pesa menos las diferencias verticales: un navegador a pantalla completa
    dice que ocupa toda la altura, mientras el sistema descuenta la barra de
    menus, asi que la anchura es la pista fiable.
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
    """La ventana con el foco de ese proceso, como entrada de la lista CG."""
    fr = focused_frame(pid)
    if not fr:
        return None
    return match_window(window_snapshot()[1], pid, fr)


def window_action(pid, win, body, tol=12):
    """Ejecuta `body` sobre LA ventana de ese proceso cuyo marco coincide.

    Actuar sobre "la ventana frontal" no vale: el lanzador y las apps web son
    ventanas del mismo navegador y acabas tocando una pestaña cualquiera.
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
    """Pone esa ventana delante y su app al frente, en ese orden."""
    window_action(pid, win, 'perform action "AXRaise" of w')
    return activate_pid(pid)


def hide_pid(pid):
    """Esconde esa app (Cmd+H). Si estaba a pantalla completa macOS sale de su
    escritorio, que es justo lo que hace falta para volver al de MacTV."""
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
    """Pone a pantalla completa la ventana que la app acaba de abrir.

    En macOS pantalla completa es, justamente, un escritorio propio. El
    resultado se confirma MIDIENDO el marco: estando en pantalla completa la
    accesibilidad deja de ver la ventana, asi que preguntarle no sirve.
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

    # 1. la ventana delante y su app activa
    raise_and_activate(pid, cur)
    time.sleep(0.7)

    # 2. pantalla completa sobre esa misma ventana
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

    # 3. plan B: el atajo del sistema, solo si la app esta delante y la ventana
    #    con el foco es de verdad la nuestra
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
    """Espera a que aparezca una ventana nueva de ese bundle.

    `prefer_pids` permite quedarse solo con las de procesos concretos: el
    lanzador arranca su propia instancia del navegador, y asi no se confunde
    con una ventana que el usuario abra a la vez en la suya.
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
# Configuracion
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
# Apps e iconos
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
    log(f"  extrayendo {len(todo)} iconos (solo la primera vez)...", flush=True)
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
    log(f"  iconos listos: "
        f"{sum(1 for a in apps if (ICONS / (a['id'] + '.png')).exists())}/{len(apps)}",
        flush=True)


# ---------------------------------------------------------------------------
# Temporizador de suspension
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
    """Proceso aparte, para que sobreviva aunque se cierre MacTV."""
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
# Lanzar apps y volver a MacTV
# ---------------------------------------------------------------------------

_launch = {"token": 0}

# Banderas con las que SI se consigue pantalla completa de verdad (comprobado):
# --start-fullscreen solo lo respeta el proceso que crea la ventana, por eso
# cada app se abre en su propia instancia del navegador.
BROWSER_FLAGS = ["--start-fullscreen", "--no-first-run", "--no-default-browser-check"]


def on_this_desktop(pid):
    """¿Hay alguna ventana de ese proceso en el escritorio que estoy viendo?"""
    if not pid:
        return False
    return any(w[W_PID] == pid for w in window_snapshot(onscreen=True)[1])


def launcher_fullscreen(pid=None):
    """¿Esta MacTV a pantalla completa, o sea, en su propio escritorio?"""
    pid = pid or STATE.get("launcher_pid")
    if not pid:
        return False
    scr, wins = window_snapshot()
    return any(w[W_PID] == pid and is_fullscreen(w, scr) for w in wins)


def goto_desktop():
    """Va al escritorio donde esta MacTV.

    Con MacTV a pantalla completa su escritorio es solo suyo, y se llega
    activando su proceso: el lanzador es una instancia propia del navegador,
    con una unica ventana, asi que activarlo no puede llevar a otro sitio. Lo
    unico que estorba es venir de una app a pantalla completa, porque macOS se
    queda en ella; por eso se esconde antes (esconder una app a pantalla
    completa devuelve al escritorio anterior).

    Si el lanzador se quedo sin pantalla completa vive en el escritorio normal,
    y entonces el camino es el de siempre: activar OTRA aplicacion que este
    alli, y el Finder siempre lo esta.
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
            for _ in range(7):             # la animacion tarda casi un segundo
                time.sleep(0.4)
                if on_this_desktop(pid):
                    return True
        return False
    for _ in range(3):
        osa('tell application "Finder" to activate', timeout=8)
        for _ in range(7):                 # la animacion tarda casi un segundo
            time.sleep(0.4)
            if on_this_desktop(pid):
                return True
    return False


def goto_normal_desktop():
    """Se planta en un escritorio normal (el del Finder) antes de abrir una app.

    --start-fullscreen solo lo aplica macOS si el navegador se lanza desde un
    escritorio normal, y la accesibilidad solo puede tocar ventanas del
    escritorio activo. Como MacTV esta a pantalla completa, hay que salir.
    """
    pid = STATE.get("launcher_pid")
    if not launcher_fullscreen(pid):
        # MacTV ya vive en el escritorio normal: basta con volver a el
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
    """Copia cookies y almacenamiento del Brave del usuario al perfil de la app.

    Asi no hay que volver a iniciar sesion en Netflix y compania. Si algo falla
    no pasa nada: la app pedira que inicies sesion una vez.
    """
    src = Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser"
    dst = Path(profile)
    if not src.is_dir():
        return
    try:
        # Local State NO se copia. En macOS la clave de cifrado de las cookies
        # vive en el Llavero ("Brave Safe Storage"), no ahi, asi que copiarlo
        # no aporta nada; y si se copia arrastra "app_shims", el registro de
        # las apps web del Brave normal. Brave las daria por instaladas en
        # este perfil, intentaria reconciliar sus .app de ~/Applications y
        # soltaria un dialogo de error por cada una al arrancar.
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
    """PID del navegador que tiene cogido ese perfil, o 0 si no hay nadie.

    Chromium deja SingletonLock como enlace simbolico a "<host>-<pid>".
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
    # por si el PID se ha reciclado y ahora es otra cosa cualquiera
    comm = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                          capture_output=True, text=True).stdout
    return pid if (browser()[0] or "?") in comm else 0


def clear_singleton(profile):
    """Quita los cerrojos que deja el navegador si se cerro a lo bruto.

    Si se quedan, la siguiente vez el proceso arranca y se cierra solo.

    Solo se borran si de verdad no hay nadie detras. Borrarlos con el proceso
    vivo deja dos navegadores sobre el mismo perfil, y entonces el segundo no
    puede bloquear ninguna base de datos: Chromium suelta un
    "Se ha producido un error al abrir tu perfil" por cada subsistema que
    falla. Devuelve False si el perfil esta ocupado.
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
    """Cierra el navegador que tenga cogido el perfil y espera a que suelte."""
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
    """Abre una app web en su propia instancia del navegador, a pantalla
    completa desde el primer momento (y por tanto en su propio escritorio)."""
    name, path = browser()
    if not name:
        return 0
    profile = CACHE / "apps" / app["id"]
    # Sobrevivio a un MacTV anterior: vuelve a esa ventana. Abrir un segundo
    # navegador sobre el mismo perfil solo daria errores de perfil.
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
    """Respaldo: busca el proceso del lanzador por el titulo de su ventana."""
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
    """La pagina del lanzador dice donde esta su ventana; sirve de respaldo si
    se perdiera el PID (por ejemplo si el navegador se reinicia)."""
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
    """Vuelve al escritorio de MacTV sin cerrar nada.

    A pantalla completa MacTV tiene escritorio propio y una sola ventana: al
    llegar ya esta delante y con el foco, no hay nada mas que hacer. Si no lo
    esta, comparte escritorio con otras ventanas del navegador y hacen falta
    dos pasos mas: subirla por encima (ya se puede: esta en el escritorio
    activo) y devolverle el foco del teclado con un clic en su barra de titulo.
    Activar el navegador por su nombre NO vale: macOS se iria a la ventana que
    tenga delante, que puede ser la de la app a pantalla completa, y
    saldriamos del escritorio otra vez.
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
    """Red de seguridad: si la ventana no salio a pantalla completa, se fuerza
    por accesibilidad (posible, porque esta en el escritorio activo)."""
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
    """Vigila la app: cuando se queda sin ventanas, vuelve a MacTV."""
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

    # ya abierta: solo hay que ir a su escritorio
    pid = STATE["running"].get(app_id)
    if pid and proc_alive(pid) and windows_of(pid):
        activate_pid(pid)
        return True

    bbid = browser_bundle()
    es_web = bool(app.get("url")) and bbid and app["bundle"].startswith(bbid + ".")

    if es_web and cfg.get("fullscreen", True):
        # --start-fullscreen solo lo aplica macOS si se lanza desde el
        # escritorio normal; desde un escritorio a pantalla completa lo ignora
        goto_normal_desktop()
        pid = launch_browser_app(app)
        if not pid:
            return False
        threading.Thread(target=ensure_fullscreen_later,
                         args=(pid,), daemon=True).start()
    else:
        # app nativa: la abre el sistema. Si acepta pantalla completa se
        # intenta, pero sin garantias: macOS no deja tocar por accesibilidad
        # las ventanas que no estan en el escritorio activo, asi que primero
        # hay que salir del escritorio de MacTV.
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
# Acciones sueltas
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
# El lanzador
# ---------------------------------------------------------------------------

def strip_app_shims(local_state):
    """Borra el registro de apps web del Local State de un perfil de MacTV.

    Si esta ahi (versiones viejas lo copiaban del Brave normal), Brave cree que
    las PWAs del usuario estan instaladas aqui y saca un dialogo de error por
    cada una al arrancar.
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
    """Silencia los avisos de primera ejecucion del navegador."""
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
    """Abre MacTV a pantalla completa en su propio escritorio.

    Se lanza como instancia propia del navegador, asi que su PID es el del
    proceso que arrancamos: "Menu" puede volver aqui sin ambiguedad.
    """
    name, path = browser()
    if not name:
        subprocess.Popen(["open", url])
        return
    profile = str(CACHE / "launcher-profile")
    # Al salir con Ctrl+C el navegador se queda vivo (start_new_session). Ese
    # lanzador apunta a un servidor que ya no existe, asi que se cierra antes
    # de abrir el nuevo: si no, dos Braves comparten launcher-profile.
    release_profile(profile)
    seed_profile(profile)
    scr, _ = window_snapshot()
    w, h = int(scr[0] or 1440), int(scr[1] or 900)
    # MacTV a pantalla completa, o sea, en su propio escritorio, igual que las
    # apps. Antes se quedaba en el normal porque --start-fullscreen no funciona
    # si el navegador se lanza desde un escritorio a pantalla completa; ahora
    # eso lo resuelve goto_normal_desktop(), que sale de aqui antes de abrir
    # cada app. El tamano de ventana sigue puesto como respaldo por si la
    # pantalla completa no llegara a cuajar.
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
# Servidor HTTP
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
# Arranque
# ---------------------------------------------------------------------------

def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    print("\n  \033[1mMacTV\033[0m\n")

    if not _cg.AXIsProcessTrusted():
        print("  \033[33mFalta permiso de Accesibilidad.\033[0m")
        print("  Ajustes del Sistema > Privacidad y Seguridad > Accesibilidad")
        print("  y activa la app de terminal que estas usando. Luego reinicia.\n")
        osa('tell application "System Events" to open location '
            '"x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"')

    cfg = load_config()
    everything = scan_apps(cfg["hidden"])
    STATE["featured"], STATE["apps"] = split_featured(everything, cfg["featured"])
    STATE["by_id"] = {a["id"]: a for a in everything}
    STATE["sleep_options"] = cfg["sleep_options"]
    STATE["cfg"] = cfg

    print(f"  {len(STATE['featured'])} destacadas  |  {len(STATE['apps'])} apps",
          flush=True)

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print(f"\n  TV     ->  http://localhost:{PORT}/")
    print(f"  \033[1;36mMovil  ->  http://{remote_url()}\033[0m")
    otras = [a for a in local_addresses()][1:]
    if otras:
        print(f"  (otras direcciones: {', '.join(otras)})")
    print("\n  Ctrl+C para salir.\n", flush=True)

    # Los iconos van despues de imprimir: asi lo de arriba se ve desde el
    # primer momento, no cuando termine la extraccion.
    build_icons(everything)

    if "--no-ui" not in sys.argv:
        # MacTV se abre a pantalla completa, o sea, en su propio escritorio, y
        # la terminal se queda atras. Esta pausa da tiempo a leer las
        # direcciones antes de que la pantalla cambie.
        time.sleep(LAUNCH_DELAY)
        open_launcher(f"http://localhost:{PORT}/")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n  Adios.\n")


if __name__ == "__main__":
    main()
