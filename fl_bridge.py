"""
MIDI link between the MCP server and the FL Studio controller script.

Reloaded by the reload_server tool, so anything that must survive a reload
(open MIDI ports, queues, the script log) is only created on first import.
"""
import itertools
import json
import queue
import sys
import threading
import time
from collections import deque

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

# Open the ports on first import: the output fails loudly if loopMIDI isn't
# running, the input is opened now so script output is captured from the start
# (None, and retried on use, if the port doesn't exist yet).
if output_port is None:
    output_port = open_output_port()
if input_port is None:
    input_port = open_input_port()
