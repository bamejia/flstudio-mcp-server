"""Keyboard listener for FL Studio keybinds

While FL Studio is the focused window, catches the key combos from keybinds.py
(next to the Test Controller script) and has the FL script run their actions,
over the same loopMIDI port as the MCP server. Other keys and the mouse are never
touched, and the keyboard isn't hooked at all while another program is focused.
The combos are fetched from FL Studio when it gets focus, if keybinds.py changed
or FL Studio restarted since the last fetch.

    pythonw keyboard_listener.py              run (a second copy exits at once)
    python keyboard_listener.py --install     run now and at every sign-in
    python keyboard_listener.py --uninstall   stop, and don't run at sign-in
    python keyboard_listener.py --stop        stop the running copy
Log: logs\\keyboard_listener.log
"""

import ctypes
import itertools
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

import fl_bridge

HERE = Path(__file__).resolve().parent
LOG_FILE = HERE / "logs" / "keyboard_listener.log"
STARTUP_LINK = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                / "Startup" / "FL Studio keybinds.lnk")
MUTEX_NAME = "Local\\flstudio-keyboard-listener"            # held while running
STOP_EVENT_NAME = "Local\\flstudio-keyboard-listener-stop"  # set by --stop

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
EVENT_SYSTEM_FOREGROUND = 0x0003
ERROR_ALREADY_EXISTS = 183
KEYEVENTF_KEYUP = 0x0002
QS_ALLINPUT = 0x04FF
MWMO_INPUTAVAILABLE = 0x0004
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
PM_REMOVE = 0x0001
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002

# Modifier bits (as in fl_shortcuts.MODIFIERS) -> the keys that set them
MODIFIER_KEYS = {1: (0x11,), 2: (0x12,), 4: (0x10,), 8: (0x5B, 0x5C)}  # ctrl, alt, shift, left/right win
ALT_OR_WIN = 2 | 8
MASK_KEY = 0xE8      # an unassigned key code
OWN_INPUT = 0x4B42   # dwExtraInfo of the keys this listener sends itself

LRESULT = ctypes.c_ssize_t

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.MsgWaitForMultipleObjectsEx.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
                                               wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
user32.MsgWaitForMultipleObjectsEx.restype = wintypes.DWORD
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                                wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

log = logging.getLogger("keyboard_listener")

binds = {}            # (modifier bits, key code) -> combo; replaced whole by the worker
held = set()          # key codes of caught combos, swallowed until released
hook = None
jobs = queue.Queue()  # ("run", combo, modifier bits) or ("refresh", FL process id)
loaded = {"pid": None, "file": None, "mtime": None}  # the last fetch of the combos

def modifiers():
    return sum(bit for bit, keys in MODIFIER_KEYS.items()
               if any(user32.GetAsyncKeyState(key) & 0x8000 for key in keys))

@HOOKPROC
def on_key(code, wparam, lparam):
    """Keyboard hook, installed only while FL Studio is focused: swallows the bound combos"""
    if code == HC_ACTION:
        key = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        if key.dwExtraInfo != OWN_INPUT:
            if key.vkCode in held:  # repeats and release of a caught key
                if wparam in (WM_KEYUP, WM_SYSKEYUP):
                    held.discard(key.vkCode)
                return 1
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                mods = modifiers()
                combo = binds.get((mods, key.vkCode))
                if combo:
                    held.add(key.vkCode)
                    jobs.put(("run", combo, mods))
                    return 1
    return user32.CallNextHookEx(None, code, wparam, lparam)

@WINEVENTPROC
def on_foreground(event_hook, event, hwnd, *_):
    follow_focus(hwnd)

def follow_focus(hwnd):
    """Hook the keyboard while FL Studio is the focused program, unhook otherwise"""
    global hook
    pid, path = fl_bridge.window_process(hwnd)
    if fl_bridge.is_fl_process(path):
        jobs.put(("refresh", pid))
        if not hook:
            hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, on_key, kernel32.GetModuleHandleW(None), 0)
            if not hook:
                log.error("Couldn't hook the keyboard (Windows error %s)", ctypes.get_last_error())
    elif hook:
        user32.UnhookWindowsHookEx(hook)
        hook = None
        held.clear()

def file_mtime(path):
    try:
        return os.path.getmtime(path) if path else None
    except OSError:
        return None

def refresh(pid):
    """Fetch the combos from FL Studio if it restarted or keybinds.py changed since the last fetch"""
    global binds
    if loaded["file"] and loaded["pid"] == pid and loaded["mtime"] == file_mtime(loaded["file"]):
        return
    try:
        result = fl_bridge.fl_request("keybinds", timeout=5)
    except fl_bridge.FLStudioError as e:
        if not str(e).startswith(("No reply", "No MIDI")):
            # FL answered: keybinds.py has an error, which FL shows in its Script
            # output. Keep the old combos until the file changes again.
            loaded.update(pid=pid, mtime=file_mtime(loaded["file"]))
        raise
    binds = {(mods, key): combo for combo, mods, key in result["binds"]}
    loaded.update(pid=pid, file=result["file"], mtime=file_mtime(result["file"]))
    log.info("%d combos on: %s", len(binds), ", ".join(binds.values()) or "-")

def run(combo, mods):
    if mods & ALT_OR_WIN:
        # The combo's key was swallowed, so Windows would see Alt/Win pressed and
        # released alone and open the menu bar / Start menu; another key in
        # between prevents that
        user32.keybd_event(MASK_KEY, 0, 0, OWN_INPUT)
        user32.keybd_event(MASK_KEY, 0, KEYEVENTF_KEYUP, OWN_INPUT)
    fl_bridge.fl_request("run_keybind", timeout=5, combo=combo)

def worker():
    while True:
        job, *args = jobs.get()
        try:
            refresh(*args) if job == "refresh" else run(*args)
        except Exception as e:
            log.warning("%s %s failed: %s: %s", job, args[0], type(e).__name__, e)

def run_listener():
    mutex = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return
    stop_event = wintypes.HANDLE(kernel32.CreateEventW(None, True, False, STOP_EVENT_NAME))
    LOG_FILE.parent.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=100_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.info("Started")
    # String ids keep FL's replies to this listener apart from the MCP server's numbered requests
    fl_bridge._request_ids = (f"kb{n}" for n in itertools.count(1))
    threading.Thread(target=worker, daemon=True).start()
    event_hook = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                                        on_foreground, 0, 0, 0)
    follow_focus(user32.GetForegroundWindow())
    msg = wintypes.MSG()
    while user32.MsgWaitForMultipleObjectsEx(1, ctypes.byref(stop_event), INFINITE, QS_ALLINPUT,
                                             MWMO_INPUTAVAILABLE) != WAIT_OBJECT_0:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    log.info("Stopped")
    # Exiting releases the hooks, the mutex and the MIDI ports

def stop():
    """Stop the running listener and wait until it has exited; False if none was running"""
    mutex = kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    event = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, STOP_EVENT_NAME)
    if event:
        kernel32.SetEvent(event)
        kernel32.CloseHandle(event)
    if mutex:
        kernel32.WaitForSingleObject(mutex, 5000)  # returns once the listener exits
        kernel32.CloseHandle(mutex)
    return bool(event)

def start():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pythonw), str(Path(__file__).resolve())], cwd=HERE,
                     creationflags=subprocess.DETACHED_PROCESS)

def install():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    command = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{STARTUP_LINK}'); "
               f"$s.TargetPath = '{pythonw}'; $s.Arguments = '\"{Path(__file__).resolve()}\"'; "
               f"$s.WorkingDirectory = '{HERE}'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", command], check=True)
    stop()
    start()
    print("Installed: running now and at every sign-in")

def uninstall():
    STARTUP_LINK.unlink(missing_ok=True)
    stop()
    print("Uninstalled: stopped, and won't run at sign-in")

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "--install":
        install()
    elif command == "--uninstall":
        uninstall()
    elif command == "--stop":
        print("Stopped" if stop() else "Not running")
    elif command:
        print(__doc__)
    else:
        run_listener()
