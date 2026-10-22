"""
MIDI link between the MCP server and the FL Studio controller script.

Reloaded by the reload_server tool, so anything that must survive a reload
(open MIDI ports, queues, the script log) is only created on first import.
"""
import contextlib
import ctypes
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from pathlib import Path

import mido
from mido import Message

# Commands go to FL Studio on the loopMIDI port named "loopMIDI Port".
# Windows appends an index to port names, so match by prefix.
OUTPUT_PORT_NAME = 'loopMIDI Port'

# FL Studio sends replies back on the same loopMIDI port. A background listener
# (mido input callback) reads everything; our own outgoing messages show up
# there too, so it only keeps what FL Studio sends:
#   - CC 116/117 tempo replies -> tempo_replies queue (read by get_tempo)
#   - SysEx script output      -> fl_log buffer (read by get_fl_log)
#   - SysEx request replies    -> the waiting fl_request call
#
# SysEx frame, shared with the FL Studio script:
#   F0 7D <type> <msg id> <chunk hi> <chunk lo> <count hi> <count lo> <payload> F7
# 7D is the MIDI "non-commercial" manufacturer ID; the payload is a chunk of
# ASCII JSON, parsed once all of a message's chunks have arrived.
INPUT_PORT_NAME = 'loopMIDI Port'
SYSEX_ID = 0x7D
SYSEX_LOG = 0x01      # FL -> server: a print() line
SYSEX_REPLY = 0x02    # FL -> server: {"id", "ok", "result" | "error"}
SYSEX_REQUEST = 0x10  # server -> FL: {"id", "cmd", "args"}
SYSEX_CHUNK_SIZE = 200

# Define custom MIDI CC messages for direct step sequencer grid control
CC_SELECT_CHANNEL = 100  # Select which channel to edit
CC_SELECT_STEP = 110     # Select which step to edit
CC_TOGGLE_STEP = 111     # Toggle the selected step on/off
CC_STEP_VELOCITY = 112   # Set velocity for the selected step
CC_TEMPO_MSB = 113       # Tempo BPM, high 7 bits
CC_TEMPO_LSB = 114       # Tempo BPM, low 7 bits (applies the tempo)
CC_TEMPO_REQUEST = 115   # Ask FL Studio for the current tempo
CC_TEMPO_REPLY_MSB = 116 # Reply from FL Studio: BPM * 10, high 7 bits
CC_TEMPO_REPLY_LSB = 117 # Reply from FL Studio: BPM * 10, low 7 bits

# State kept across reloads
if "tempo_replies" not in globals():
    tempo_replies = queue.Queue()
    fl_log = deque(maxlen=1000)  # (unix time, text)
    _partial_sysex = {}          # (type, msg id) -> {chunk index: bytes}
    _pending_replies = {}        # request id -> queue.Queue for its reply
    _sysex_msg_id = 0
    _sysex_lock = threading.Lock()  # keeps one message's chunks together if tools run concurrently
    _request_ids = itertools.count(1)
    output_port = None
    input_port = None

def open_output_port():
    names = [n for n in mido.get_output_names() if n.startswith(OUTPUT_PORT_NAME)]
    if not names:
        raise IOError(f"No MIDI output named '{OUTPUT_PORT_NAME}'. Available outputs: {mido.get_output_names()}")
    return mido.open_output(names[0])

def send(msg):
    """Send a MIDI message, reopening the port once if loopMIDI dropped it"""
    global output_port
    if output_port is None:
        output_port = open_output_port()
    try:
        output_port.send(msg)
    except Exception:
        try:
            output_port.close()
        except Exception:
            pass
        output_port = open_output_port()
        output_port.send(msg)

def on_fl_message(msg):
    try:
        if msg.type == 'control_change' and msg.control in (CC_TEMPO_REPLY_MSB, CC_TEMPO_REPLY_LSB):
            tempo_replies.put(msg)
        elif msg.type == 'sysex' and len(msg.data) >= 7 and msg.data[0] == SYSEX_ID:
            handle_sysex(msg.data)
    except Exception as e:
        print(f"Error handling MIDI from FL Studio: {type(e).__name__}: {e}", file=sys.stderr)

def handle_sysex(data):
    msg_type, msg_id = data[1], data[2]
    index = (data[3] << 7) | data[4]
    count = (data[5] << 7) | data[6]
    key = (msg_type, msg_id)
    if index == 0:
        _partial_sysex[key] = {}  # message ids wrap around; drop any stale leftovers
    chunks = _partial_sysex.setdefault(key, {})
    chunks[index] = bytes(data[7:])
    if len(chunks) < count:
        return
    del _partial_sysex[key]
    payload = json.loads(b''.join(chunks[i] for i in range(count)).decode('ascii'))
    if msg_type == SYSEX_LOG:
        fl_log.append((time.time(), str(payload)))
    elif msg_type == SYSEX_REPLY:
        replies = _pending_replies.get(payload.get("id"))
        if replies is not None:
            replies.put(payload)

def open_input_port():
    names = [n for n in mido.get_input_names() if n.startswith(INPUT_PORT_NAME)]
    if not names:
        return None
    # Look the handler up on every message so a reloaded on_fl_message takes over
    return mido.open_input(names[0], callback=lambda msg: sys.modules[__name__].on_fl_message(msg))

def ensure_listener():
    global input_port
    if input_port is None:
        input_port = open_input_port()
    return input_port is not None

def send_sysex(msg_type, obj):
    global _sysex_msg_id
    payload = json.dumps(obj).encode('ascii')  # json escapes non-ASCII, keeping every byte 7-bit
    chunks = [payload[i:i + SYSEX_CHUNK_SIZE] for i in range(0, len(payload), SYSEX_CHUNK_SIZE)] or [b'']
    count = len(chunks)
    with _sysex_lock:
        _sysex_msg_id = (_sysex_msg_id + 1) & 0x7F
        for index, chunk in enumerate(chunks):
            send(Message('sysex', data=[SYSEX_ID, msg_type, _sysex_msg_id,
                                        index >> 7, index & 0x7F, count >> 7, count & 0x7F, *chunk]))

class FLStudioError(Exception):
    pass

# --- What the user allows ------------------------------------------------------
# The user switches these in chat (set_permissions): taking focus (bringing FL to
# the front for a moment, for shortcut keys) is allowed unless they switch it off;
# the mouse only while they've switched it on. Saved next to this file, so restarts
# and reloads keep them; every focus or mouse action checks them first.
PERMISSIONS_FILE = Path(__file__).with_name("permissions.json")
PERMISSION_DEFAULTS = {"focus": True, "mouse": False}
PERMISSION_ERRORS = {
    "focus": "Taking focus is switched off (the user asked), so FL Studio can't come to the front. "
             "Ask the user to switch it back on.",
    "mouse": "Mouse use is switched off. Only the user can switch it on.",
}

def permissions():
    try:
        saved = json.loads(PERMISSIONS_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    return {name: bool(saved.get(name, default)) for name, default in PERMISSION_DEFAULTS.items()}

def save_permissions(**changes):
    current = dict(permissions(), **{k: bool(v) for k, v in changes.items() if v is not None})
    PERMISSIONS_FILE.write_text(json.dumps(current))
    return current

def require(*names):
    """Raise unless the user allows each of names ("focus", "mouse")"""
    allowed = permissions()
    for name in names:
        if not allowed[name]:
            raise FLStudioError(PERMISSION_ERRORS[name])

def fl_request(cmd, timeout=10.0, **args):
    """Run a command in the FL Studio script and return its result"""
    if not ensure_listener():
        raise FLStudioError(f"No MIDI input named '{INPUT_PORT_NAME}'. Available inputs: {mido.get_input_names()}")
    request_id = next(_request_ids)
    replies = _pending_replies[request_id] = queue.Queue()
    try:
        send_sysex(SYSEX_REQUEST, {"id": request_id, "cmd": cmd, "args": args})
        try:
            reply = replies.get(timeout=timeout)
        except queue.Empty:
            raise FLStudioError(f"No reply from FL Studio within {timeout}s. Check that the Test Controller "
                                "script is loaded and up to date (reload it in MIDI Settings).")
    finally:
        del _pending_replies[request_id]
    if not reply["ok"]:
        raise FLStudioError(reply["error"])
    return reply["result"]

# --- Piano roll notes ----------------------------------------------------------
# The controller script can't read piano roll notes, but a piano roll script can:
# ExportNotes.pyscript writes them to piano_roll_state.json in its own folder.
# Piano roll scripts only run from FL's menu or its "Run last script again"
# shortcut (Ctrl+Alt+Y), so the server focuses FL Studio, presses that shortcut
# and waits for a file carrying the request id it wrote to export_request.json.
FL_USER_DATA_DIR = Path(os.environ.get("FL_USER_DATA_DIR", Path.home() / "Documents" / "Image-Line" / "FL Studio"))
PIANO_ROLL_SCRIPTS_DIR = FL_USER_DATA_DIR / "Settings" / "Piano roll scripts"
EXPORT_REQUEST_FILE = PIANO_ROLL_SCRIPTS_DIR / "export_request.json"
EXPORT_STATE_FILE = PIANO_ROLL_SCRIPTS_DIR / "piano_roll_state.json"
FL_PROCESS_NAME = "fl64.exe"

VK_CONTROL, VK_MENU, VK_Y, VK_L = 0x11, 0x12, 0x59, 0x4C
VK_RETURN, VK_RIGHT, VK_UP, VK_ESCAPE, VK_R = 0x0D, 0x27, 0x26, 0x1B, 0x52
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
SW_RESTORE = 9
SW_SHOWMINNOACTIVE = 7  # minimize without taking focus
GW_HWNDPREV = 3  # the window just above, in the stacking order
GW_OWNER = 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x0008
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Own DLL instances with explicit signatures, so 64-bit window handles aren't
# truncated and other ctypes users in the process are unaffected
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32")
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
try:
    # Real pixels for window rectangles and the cursor on scaled displays (per-monitor v2)
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    pass
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

def window_process(hwnd):
    """(process id, program path) of the process that owns a window; path is '' if unknown"""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        return pid.value, ""
    path = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(1024)
    found = kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size))
    kernel32.CloseHandle(process)
    return pid.value, path.value if found else ""

def is_fl_process(path):
    return path.lower().endswith("\\" + FL_PROCESS_NAME)

def fl_windows():
    """(handle, title) of FL Studio's visible top-level windows"""
    windows = []

    @WNDENUMPROC
    def check(hwnd, _):
        if user32.IsWindowVisible(hwnd) and is_fl_process(window_process(hwnd)[1]):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            windows.append((hwnd, title.value))
        return True

    user32.EnumWindows(check, 0)
    return windows

def focus_window(hwnd, accept=None, attempts=6):
    """Bring a window to the front; True once it (or any window in accept) is in
    front. Windows only lets the foreground app hand over focus, so briefly attach
    to its input queue while switching. That doesn't always work the first time,
    so retry, and after a few tries also tap Alt (which lifts Windows' lock)."""
    accept = set(accept or ()) | {hwnd}
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    for attempt in range(attempts):
        foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        this_thread = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(this_thread, foreground_thread, True)
        try:
            if attempt >= 3:
                user32.keybd_event(VK_MENU, 0, 0, 0)
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(this_thread, foreground_thread, False)
        time.sleep(0.05)
        if user32.GetForegroundWindow() in accept:
            return True
        time.sleep(0.1)
    return False

def window_title(hwnd):
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, 512)
    return title.value

def visible_window_above(hwnd):
    """The nearest window above hwnd in the stacking order that is a normal window on
    screen (visible, not minimized, not owned by another, not always-on-top), or None.
    Hidden helper windows sit in the order too, so the window directly above often isn't one."""
    above = user32.GetWindow(hwnd, GW_HWNDPREV)
    while above:
        if (user32.IsWindowVisible(above) and not user32.IsIconic(above)
                and not user32.GetWindow(above, GW_OWNER)
                and not user32.GetWindowLongW(above, GWL_EXSTYLE) & WS_EX_TOPMOST):
            return above
        above = user32.GetWindow(above, GW_HWNDPREV)
    return None

def press_keys(*vks):
    """Press keys together (modifiers first), then release them in reverse order. Page
    Up..Delete (arrows, Home, End, Insert) go as extended keys: otherwise Windows takes them
    for numpad keys and drops a held Shift (Shift+Down then scrolls instead of moving)."""
    flags = lambda vk: KEYEVENTF_EXTENDEDKEY if 0x21 <= vk <= 0x2E else 0
    for vk in vks:
        user32.keybd_event(vk, 0, flags(vk), 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, flags(vk) | KEYEVENTF_KEYUP, 0)

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        # Sized for the largest member (MOUSEINPUT), as SendInput expects
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
KEYEVENTF_UNICODE = 0x0004

def type_text(text):
    """Type text into the focused control as Unicode characters (any language, symbols).
    FL's name boxes dropped a Unicode space, so spaces go as the Space key."""
    for char in text:
        if char == " ":
            press_keys(0x20)
            time.sleep(0.01)
            continue
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            event = INPUT(type=1)  # INPUT_KEYBOARD
            event.ki = KEYBDINPUT(0, ord(char), flags, 0, 0)
            user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        time.sleep(0.01)

def press_ctrl_alt_y():
    press_keys(VK_CONTROL, VK_MENU, VK_Y)

def fl_main_window():
    """(FL's main window, handles of all its windows). FL's main window is titled
    "... FL Studio 20xx"; FL also has untitled helper windows. Detached editors like the
    piano roll are windows owned by it: they move with it in the stacking order, FL routes
    shortcuts to whichever of them it has focused, and the piano roll window is recreated
    when it's reopened, so its handle can't be kept."""
    windows = fl_windows()
    if not windows:
        raise FLStudioError("Couldn't find FL Studio's window. Is FL Studio running?")
    unowned = [(hwnd, title) for hwnd, title in windows if not user32.GetWindow(hwnd, GW_OWNER)]
    main = (next((hwnd for hwnd, title in unowned if "fl studio" in title.lower()), None)
            or (unowned or windows)[0][0])
    return main, {hwnd for hwnd, _ in windows}

@contextlib.contextmanager
def focus_kept(window_warnings):
    """Afterwards, if FL Studio is in front, hand focus back to the window the user was in
    (any app) and slot FL back under the window that was just above it, so every window
    ends up in the same stacking order as before; minimize FL again if it was. Problems
    are appended to window_warnings. Yields (FL's main window, its window handles)."""
    target, fl_handles = fl_main_window()
    previous = user32.GetForegroundWindow()
    was_minimized = bool(user32.IsIconic(target))
    above_fl = visible_window_above(target)
    try:
        yield target, fl_handles
    finally:
        # Unless the user already switched to another window meanwhile
        if previous and user32.GetForegroundWindow() in {hwnd for hwnd, _ in fl_windows()}:
            focus_window(previous)
            if above_fl and visible_window_above(target) != above_fl:
                if not user32.SetWindowPos(target, above_fl, 0, 0, 0, 0,
                                           SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE):
                    error = ctypes.get_last_error()  # read before any other Windows call overwrites it
                    window_warnings.append(f"Couldn't put FL Studio back under {window_title(above_fl)!r} "
                                           f"(Windows error {error})")
            if was_minimized:
                user32.ShowWindow(target, SW_SHOWMINNOACTIVE)

@contextlib.contextmanager
def fl_in_front(window_warnings):
    """Bring FL Studio's main window to the front so keys reach it; afterwards as focus_kept"""
    require("focus")
    with focus_kept(window_warnings) as (target, fl_handles):
        was_minimized = bool(user32.IsIconic(target))
        if not focus_window(target, accept=fl_handles):
            raise FLStudioError("Windows didn't let FL Studio come to the front, so no keys were sent.")
        # Un-minimizing animates; wait until FL is really restored and in front
        ready_by = time.time() + 2.0
        while (user32.IsIconic(target) or user32.GetForegroundWindow() not in fl_handles) and time.time() < ready_by:
            time.sleep(0.05)
        time.sleep(0.5 if was_minimized else 0.15)
        yield target

def open_in_fl(path):
    """Hand a file to the running FL Studio, as if opened from Explorer (a second FL64.exe
    passes it on to the running one). FL may come to the front, so it needs focus allowed."""
    require("focus")
    main, _ = fl_main_window()
    subprocess.Popen([window_process(main)[1], str(path)])

# --- Mouse (only while the user allows it) --------------------------------------
# FL's forms are real child windows, so their rectangles anchor click positions, and
# WindowFromPoint tells what a click would hit. FL keeps floating forms (piano roll,
# plugin windows) above docked ones whatever the stacking order says, so forms in the
# way are hidden for the moment and shown again afterwards.
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
GA_ROOT = 2
SW_HIDE, SW_SHOWNA = 0, 8

def class_name(hwnd):
    buffer = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buffer, 64)
    return buffer.value

def window_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom

def child_windows(parent):
    """All descendants of parent: [(hwnd, class, title, rect, visible)]"""
    found = []
    def collect(hwnd, _):
        found.append((hwnd, class_name(hwnd), window_title(hwnd), window_rect(hwnd), bool(user32.IsWindowVisible(hwnd))))
        return True
    user32.EnumChildWindows(parent, WNDENUMPROC(collect), 0)
    return found

def form_under(x, y, main):
    """The FL form a click at (x, y) would hit: the child of main holding the window there,
    a detached FL window, or None for another app's window"""
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    root = user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None
    if root != main:
        return root if root in {h for h, _ in fl_windows()} else None
    while user32.GetParent(hwnd) and user32.GetParent(hwnd) != main:
        hwnd = user32.GetParent(hwnd)
    return hwnd

@contextlib.contextmanager
def uncovered(target_form):
    """Yields clear(x, y): hides the FL forms covering target_form at (x, y). They're shown
    again on exit."""
    main = fl_main_window()[0]
    top = target_form
    while user32.GetParent(top) and user32.GetParent(top) != main:
        top = user32.GetParent(top)
    hidden = []

    def clear(x, y):
        for _ in range(10):
            form = form_under(int(x), int(y), main)
            if form == top:
                return
            if form is None:
                raise FLStudioError(f"Another app's window covers FL Studio at ({x:.0f}, {y:.0f}).")
            user32.ShowWindow(form, SW_HIDE)
            hidden.append(form)
            time.sleep(0.05)
        raise FLStudioError(f"Couldn't uncover FL Studio's {window_title(target_form)!r} at ({x:.0f}, {y:.0f}).")
    try:
        yield clear
    finally:
        for form in reversed(hidden):
            user32.ShowWindow(form, SW_SHOWNA)

@contextlib.contextmanager
def cursor_kept():
    """Put the mouse cursor back where it was"""
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    try:
        yield
    finally:
        user32.SetCursorPos(point.x, point.y)

def hover(x, y):
    """Move the mouse to (x, y) with a real move event, so FL updates its hint for that spot"""
    require("mouse", "focus")
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(MOUSEEVENTF_MOVE, 1, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_MOVE, 0xFFFFFFFF, 0, 0, 0)  # -1: back to (x, y)
    time.sleep(0.05)

def fl_top_window(cls):
    """FL's visible top-level window of a class (a popup menu, a dialog) that has a size, or None"""
    for hwnd, _ in fl_windows():
        left, top, right, bottom = window_rect(hwnd)
        if class_name(hwnd) == cls and right > left and bottom > top:
            return hwnd
    return None

gdi32 = ctypes.WinDLL("gdi32")
user32.GetDC.restype, user32.GetDC.argtypes = wintypes.HDC, [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.GetPixel.restype, gdi32.GetPixel.argtypes = wintypes.DWORD, [wintypes.HDC, ctypes.c_int, ctypes.c_int]

def screen_colors(x, ys):
    """(r, g, b) showing on screen at x for each y, popup menus included"""
    dc = user32.GetDC(None)
    try:
        return [(c & 0xFF, c >> 8 & 0xFF, c >> 16 & 0xFF) for c in (gdi32.GetPixel(dc, int(x), int(y)) for y in ys)]
    finally:
        user32.ReleaseDC(None, dc)

def window_dpi(hwnd):
    return user32.GetDpiForWindow(hwnd) or 96

def click(x, y, button="left"):
    require("mouse", "focus")
    down, up = ((MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP) if button == "left"
                else (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP))
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.03)
    user32.mouse_event(up, 0, 0, 0, 0)
    time.sleep(0.1)

def fl_install_dir():
    return Path(window_process(fl_main_window()[0])[1]).parent

def run_export_notes(timeout=5.0, before_shortcut=None, window_warnings=None, request=None):
    """Trigger ExportNotes in FL Studio's focused piano roll and return its output.
    before_shortcut runs once FL Studio is on screen, right before Ctrl+Alt+Y.
    Problems putting the windows back are appended to window_warnings.
    request adds fields to export_request.json, e.g. {"add_notes": [...]}."""
    if window_warnings is None:
        window_warnings = []
    request_id = f"{time.time():.6f}"
    EXPORT_REQUEST_FILE.write_text(json.dumps({**(request or {}), "id": request_id}))
    try:
        with fl_in_front(window_warnings):
            if before_shortcut:
                before_shortcut()
                time.sleep(0.1)
            deadline = time.time() + timeout
            retry_at = time.time() + 1.5  # press once more if the first press was missed
            press_ctrl_alt_y()
            while time.time() < deadline:
                try:
                    state = json.loads(EXPORT_STATE_FILE.read_text())
                except (OSError, ValueError):  # not written yet, or caught mid-write
                    state = None
                if state and state.get("request_id") == request_id:
                    return state
                if retry_at and time.time() > retry_at:
                    retry_at = None
                    press_ctrl_alt_y()
                time.sleep(0.05)
            raise FLStudioError("ExportNotes didn't respond to Ctrl+Alt+Y within "
                                f"{timeout}s. Run it once from the piano roll (Tools > Scripting > "
                                "ExportNotes) so it becomes the 'last script', then try again.")
    finally:
        # Remove the request so running ExportNotes by hand later can't apply it again
        EXPORT_REQUEST_FILE.unlink(missing_ok=True)

# Open the ports on first import: the output fails loudly if loopMIDI isn't
# running, the input is opened now so script output is captured from the start
# (None, and retried on use, if the port doesn't exist yet).
if output_port is None:
    output_port = open_output_port()
if input_port is None:
    input_port = open_input_port()
