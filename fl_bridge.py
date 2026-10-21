"""
MIDI link between the MCP server and the FL Studio controller script.

Reloaded by the reload_server tool, so anything that must survive a reload
(open MIDI ports, queues, the script log) is only created on first import.
"""
import ctypes
import itertools
import json
import os
import queue
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

VK_CONTROL, VK_MENU, VK_Y = 0x11, 0x12, 0x59
KEYEVENTF_KEYUP = 0x0002
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
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

def fl_windows():
    """(handle, title) of FL Studio's visible top-level windows"""
    windows = []

    @WNDENUMPROC
    def check(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if process:
                path = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)) \
                        and path.value.lower().endswith("\\" + FL_PROCESS_NAME):
                    title = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, title, 512)
                    windows.append((hwnd, title.value))
                kernel32.CloseHandle(process)
        return True

    user32.EnumWindows(check, 0)
    return windows

def focus_window(hwnd):
    """Bring a window to the front. Windows only lets the foreground app hand
    over focus, so briefly attach to its input queue while switching."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    this_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(this_thread, foreground_thread, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.AttachThreadInput(this_thread, foreground_thread, False)
    return user32.GetForegroundWindow() == hwnd

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

def press_ctrl_alt_y():
    for vk in (VK_CONTROL, VK_MENU, VK_Y):
        user32.keybd_event(vk, 0, 0, 0)
    for vk in (VK_Y, VK_MENU, VK_CONTROL):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

def run_export_notes(timeout=5.0, before_shortcut=None, window_warnings=None):
    """Trigger ExportNotes in FL Studio's focused piano roll and return its output.
    before_shortcut runs once FL Studio is on screen, right before Ctrl+Alt+Y.
    Problems putting the windows back are appended to window_warnings."""
    if window_warnings is None:
        window_warnings = []
    windows = fl_windows()
    if not windows:
        raise FLStudioError("Couldn't find FL Studio's window. Is FL Studio running?")
    # A detached piano roll is its own window; otherwise send the shortcut to the
    # main window (titled "... FL Studio 20xx"; FL also has untitled helper windows)
    target = (next((hwnd for hwnd, title in windows if title.lower().startswith("piano roll")), None)
              or next((hwnd for hwnd, title in windows if "fl studio" in title.lower()), None)
              or next((hwnd for hwnd, _ in windows if not user32.GetWindow(hwnd, GW_OWNER)), windows[0][0]))
    request_id = f"{time.time():.6f}"
    EXPORT_REQUEST_FILE.write_text(json.dumps({"id": request_id}))
    previous = user32.GetForegroundWindow()  # whatever window the user is in, any app
    was_minimized = bool(user32.IsIconic(target))
    above_fl = visible_window_above(target)  # to put FL back at the same depth afterwards
    try:
        if not focus_window(target):
            raise FLStudioError("Windows didn't let FL Studio come to the front, so the shortcut wasn't sent.")
        # Un-minimizing animates; wait until FL is really restored and in front
        ready_by = time.time() + 2.0
        while (user32.IsIconic(target) or user32.GetForegroundWindow() != target) and time.time() < ready_by:
            time.sleep(0.05)
        time.sleep(0.5 if was_minimized else 0.15)
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
        # Hand focus back, unless the user already switched to another window meanwhile,
        # and slot FL back under the window that was just above it, so every window
        # ends up in the same stacking order as before
        if previous and user32.GetForegroundWindow() in {hwnd for hwnd, _ in fl_windows()}:
            focus_window(previous)
            if above_fl and not user32.SetWindowPos(target, above_fl, 0, 0, 0, 0,
                                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE):
                window_warnings.append(f"Couldn't put FL Studio back under {window_title(above_fl)!r} "
                                       f"(Windows error {ctypes.get_last_error()})")
            if was_minimized:
                user32.ShowWindow(target, SW_SHOWMINNOACTIVE)

# Open the ports on first import: the output fails loudly if loopMIDI isn't
# running, the input is opened now so script output is captured from the start
# (None, and retried on use, if the port doesn't exist yet).
if output_port is None:
    output_port = open_output_port()
if input_port is None:
    input_port = open_input_port()
