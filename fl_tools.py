"""
The flstudio MCP server's tools. Every function in TOOLS is registered by
trigger.py and re-registered when the reload_server tool reloads this module.
"""
import queue
import sys
import time

import mido
from mido import Message

from fl_bridge import (CC_TEMPO_LSB, CC_TEMPO_MSB, CC_TEMPO_REPLY_LSB, CC_TEMPO_REPLY_MSB,
                       CC_TEMPO_REQUEST, INPUT_PORT_NAME, FLStudioError, ensure_listener, fl_log,
                       fl_request, run_export_notes, send, tempo_replies)

# MIDI Note mappings for FL Studio commands
NOTE_PLAY = 60          # C3
NOTE_STOP = 61          # C#3
NOTE_RECORD = 62        # D3
NOTE_NEW_PROJECT = 63   # D#3
NOTE_SET_BPM = 64       # E3
NOTE_NEW_PATTERN = 65   # F3
NOTE_SELECT_PATTERN = 66  # F#3
NOTE_ADD_CHANNEL = 67   # G3
NOTE_NAME_CHANNEL = 68  # G#3
NOTE_ADD_NOTE = 69      # A3
NOTE_ADD_TO_PLAYLIST = 70  # A#3
NOTE_SET_PATTERN_LEN = 71  # B3
NOTE_CHANGE_TEMPO = 72

# Drum sound MIDI notes
KICK = 36      # C1
SNARE = 38     # D1
CLAP = 39      # D#1
CLOSED_HAT = 42  # F#1
OPEN_HAT = 46  # A#1



def list_midi_ports():
    """List all available MIDI input ports"""
    print("\nAvailable MIDI Input Ports:", file=sys.stderr)
    input_ports = mido.get_output_names()
    if not input_ports:
        print("  No MIDI input ports found", file=sys.stderr)
    else:
        for i, port in enumerate(input_ports):
            print(f"  {i}: {port}", file=sys.stderr)

    return input_ports

def play():
    """Send MIDI message to start playback in FL Studio"""
    # Send Note On for C3 (note 60)
    send(mido.Message('note_on', note=60, velocity=100))
    time.sleep(0.1)  # Small delay
    send(mido.Message('note_off', note=60, velocity=0))
    print("Sent Play command", file=sys.stderr)

def stop():
    """Send MIDI message to stop playback in FL Studio"""
    # Send Note On for C#3 (note 61)
    send(mido.Message('note_on', note=61, velocity=100))
    time.sleep(0.1)  # Small delay
    send(mido.Message('note_off', note=61, velocity=0))
    print("Sent Stop command", file=sys.stderr)

def int_to_midi_bytes(value):
    """
    Convert an integer value into an array of MIDI-compatible bytes (7-bit values)

    Args:
        value (int): The integer value to convert

    Returns:
        list: Array of MIDI bytes (each 0-127)
    """
    if value < 0:
        print("Warning: Negative values not supported, converting to positive", file=sys.stderr)
        value = abs(value)

    # Special case for zero
    if value == 0:
        return [0]

    # Convert to MIDI bytes (7-bit values, MSB first)
    midi_bytes = []
    while value > 0:
        # Extract the lowest 7 bits and prepend to array
        midi_bytes.insert(0, value & 0x7F)  # 0x7F = 127 (binary: 01111111)
        # Shift right by 7 bits
        value >>= 7

    return midi_bytes

def change_tempo(bpm: float):
    """
    Change the tempo in FL Studio

    Sends the BPM as two CC messages (high 7 bits, then low 7 bits) so it
    can't be confused with the note-based melody transfer. FL Studio applies
    the tempo when the low-bits CC arrives.

    Args:
        bpm (float): The desired tempo in beats per minute (20-999)
    """
    # Ensure BPM is within a reasonable range
    if bpm < 20 or bpm > 999:
        print(f"Warning: BPM value {bpm} is outside normal range (20-999)", file=sys.stderr)
        bpm = max(20, min(bpm, 999))

    bpm_int = int(round(bpm))

    send(Message('control_change', control=CC_TEMPO_MSB, value=bpm_int >> 7))
    send(Message('control_change', control=CC_TEMPO_LSB, value=bpm_int & 0x7F))

    time.sleep(0.2)  # Give FL Studio a moment to apply it
    return f"Tempo change to {bpm_int} BPM sent to FL Studio. {get_tempo()}"

def get_tempo(timeout: float = 2.0):
    """
    Read the current project tempo from FL Studio

    Sends a request CC; the FL Studio script replies with BPM * 10 as two CC
    messages on the input port.

    Args:
        timeout (float): Seconds to wait for FL Studio's reply
    """
    if not ensure_listener():
        return f"No MIDI input named '{INPUT_PORT_NAME}'. Available inputs: {mido.get_input_names()}"

    # Drop any stale replies left over from earlier requests
    while not tempo_replies.empty():
        tempo_replies.get_nowait()

    send(Message('control_change', control=CC_TEMPO_REQUEST, value=0))

    msb = None
    deadline = time.time() + timeout
    while (remaining := deadline - time.time()) > 0:
        try:
            msg = tempo_replies.get(timeout=remaining)
        except queue.Empty:
            break
        if msg.control == CC_TEMPO_REPLY_MSB:
            msb = msg.value
        elif msg.control == CC_TEMPO_REPLY_LSB and msb is not None:
            return f"Current FL Studio tempo: {((msb << 7) | msg.value) / 10:g} BPM"

    return (f"No tempo reply from FL Studio within {timeout}s. Check that FL Studio's MIDI output "
            f"'{INPUT_PORT_NAME}' is enabled with the same port number as the Test Controller input.")

def get_fl_log(lines: int = 50, contains: str = "", clear: bool = False):
    """
    Read recent output from the FL Studio controller script (its print() lines)

    The script forwards everything it prints to this server over SysEx. Only
    output sent while this server was running is available (last 1000 lines).

    Args:
        lines (int): How many of the most recent matching lines to return
        contains (str): Only return lines containing this text (case-insensitive)
        clear (bool): Empty the log after reading it
    """
    ensure_listener()
    entries = [e for e in fl_log if contains.lower() in e[1].lower()][-lines:]
    if clear:
        fl_log.clear()
    if not entries:
        return ("No FL Studio script output received yet. Reload the script in FL Studio "
                "(MIDI Settings) to make it print, and check that the 'loopMIDI Port' output is enabled.")
    return "\n".join(f"{time.strftime('%H:%M:%S', time.localtime(t))}  {text}" for t, text in entries)

DEFAULT_FIELDS = ("info", "mixer", "steps")
CHANNEL_FIELDS = DEFAULT_FIELDS + ("notes",)  # notes are opt-in: reading them focuses FL Studio

def check_fields(fields):
    fields = list(fields or DEFAULT_FIELDS)
    unknown = [f for f in fields if f not in CHANNEL_FIELDS]
    if unknown:
        raise ValueError(f"Unknown fields {unknown}. Choose from {list(CHANNEL_FIELDS)}")
    return fields

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Piano roll note properties at their defaults are left out of results to save space
NOTE_DEFAULTS = {"pan": 0.5, "release": 0.5, "fcut": 0.5, "fres": 0.5, "pitchofs": 0, "color": 0,
                 "slide": False, "porta": False, "muted": False}

def compact_note(n, ppq):
    note = {
        "note": n["number"],
        "name": f"{NOTE_NAMES[n['number'] % 12]}{n['number'] // 12}",  # FL Studio naming: 60 = C5
        "start": round(n["time_ticks"] / ppq, 4),     # beats from the pattern start
        "length": round(n["length_ticks"] / ppq, 4),  # beats
        "velocity": min(127, round(n["velocity"] * 128)),  # FL's 0-1 velocity as MIDI 0-127
    }
    for key, default in NOTE_DEFAULTS.items():
        value = n[key]
        if (abs(value - default) > 0.01) if isinstance(default, float) else value != default:
            note[key] = round(value, 3) if isinstance(value, float) else value
    return note

def read_notes(channel, pattern=None):
    """Export one channel's piano roll notes in a pattern (channel: name or global index)"""
    prep = fl_request("prepare_piano_roll", channel=channel, pattern=pattern)
    try:
        # FL's caption is just "Piano roll -" (no channel name), so this only checks the
        # piano roll has focus; the step-grid comparison below checks the channel
        if not prep["caption"].lower().startswith("piano roll"):
            raise FLStudioError(f"FL Studio's piano roll didn't get focus (focused window: {prep['caption']!r})")
        # Re-focus the piano roll once FL is on screen: focus set while it was minimized doesn't stick
        window_warnings = []
        state = run_export_notes(before_shortcut=lambda: fl_request("focus_piano_roll"),
                                 window_warnings=window_warnings)
    finally:
        fl_request("restore_selection", **prep["restore"])
    ppq = state["ppq"]
    result = {"pattern": prep["pattern"], "pattern_name": prep["pattern_name"], "channel": prep["channel_name"],
              "notes": sorted((compact_note(n, ppq) for n in state["notes"]), key=lambda n: (n["start"], n["note"]))}
    # FL's step grid marks the step each note starts on; a mismatch suggests the wrong piano roll was read
    note_steps = sorted({n["time_ticks"] * 4 // ppq for n in state["notes"]})
    if note_steps != prep["steps_on"]:
        result["warning"] = (f"Note start steps {note_steps} don't match the channel's step grid "
                             f"{prep['steps_on']}; the export may come from a different piano roll.")
    if window_warnings:
        result["window_warning"] = "; ".join(window_warnings)
    return result

def add_notes(patterns_data, keep_steps):
    """Add piano roll notes to get_pattern/get_all_patterns results, for channels that have any"""
    for pattern in patterns_data:
        for channel in pattern["channels"]:
            # The step grid marks every note start, so channels without steps have no notes
            channel["notes"] = read_notes(channel["index"], pattern["pattern"])["notes"] if channel["steps_on"] else []
            if not keep_steps:
                del channel["steps_on"]
    return patterns_data

def request_fields(fields):
    """Fields to ask the FL script for: notes are read separately and need steps to find channels with notes"""
    asked = [f for f in fields if f != "notes"]
    if "notes" in fields and "steps" not in asked:
        asked.append("steps")
    return asked

def get_pattern_names():
    """List every non-empty pattern in the FL Studio project: number, name and length in beats"""
    return fl_request("get_pattern_names")

def get_channel_names():
    """List every channel in the channel rack: global index, name and plugin (None for audio samples)"""
    return fl_request("get_channel_names")

def get_pattern(pattern: str, channel: str = "", fields: list[str] | None = None):
    """
    Read a pattern's per-channel data from FL Studio

    Args:
        pattern (str): Pattern name (case-insensitive) or number. If several patterns
                       share the name, all of them are returned.
        channel (str): Only return this channel (by name). Empty returns every channel.
        fields (list[str]): Which data to include for each channel; any of
            "info"  - channel type, plugin name, mixer track
            "mixer" - volume (0-1 and dB), pan (-1 left .. 1 right), pitch (cents), mute, solo
            "steps" - step sequencer steps that are on (16th-note step numbers from 0)
            "notes" - piano roll notes, as in get_notes (opt-in: FL Studio comes to the
                      front and opens each channel's piano roll while they're read)
            Default: info, mixer, steps. Asking for only "steps" and/or "notes" also
            leaves out channels with nothing in the pattern.

    Reading steps briefly switches FL Studio to that pattern and back.
    """
    fields = check_fields(fields)
    result = fl_request("get_pattern", pattern=pattern, channel=channel or None, fields=request_fields(fields))
    return add_notes(result, "steps" in fields) if "notes" in fields else result

def get_all_patterns(fields: list[str] | None = None):
    """
    Read every non-empty pattern's per-channel data from FL Studio

    Args:
        fields (list[str]): Same as get_pattern: any of "info", "mixer", "steps", "notes"
                            (default: info, mixer, steps). "info" and "mixer" are the same
                            for every pattern, so ["steps"] is the cheapest way to see what
                            each pattern contains. "notes" reads every channel's piano roll
                            in every pattern, one at a time, so it is slow.

    Steps are read by switching FL Studio through each pattern, then back to the
    one that was active.
    """
    fields = check_fields(fields)
    result = fl_request("get_all_patterns", timeout=60.0, fields=request_fields(fields))
    return add_notes(result, "steps" in fields) if "notes" in fields else result

def get_notes(channel: str, pattern: str = ""):
    """
    Read the piano roll notes of one channel in a pattern

    Args:
        channel (str): Channel name (case-insensitive)
        pattern (str): Pattern name or number; empty uses the active pattern

    Each note has its MIDI number, name (FL Studio naming, 60 = C5), start and
    length in beats from the pattern start, and velocity (0-127). Pan, release,
    filter, fine pitch, color, slide, porta and mute are only included when not
    at their defaults.

    How it works: FL Studio is brought to the front with that channel's piano roll
    open, the ExportNotes piano roll script is run with Ctrl+Alt+Y ("Run last script
    again") and focus goes back afterwards. ExportNotes must be the last piano roll
    script run in FL Studio; otherwise Ctrl+Alt+Y runs a different script.
    """
    return read_notes(channel, pattern or None)

def get_mixer_track(track: str):
    """
    Read a mixer track from FL Studio

    Args:
        track (str): Mixer track number (0 = Master; the same number get_pattern
                     reports as a channel's "mixer_track") or track name (case-insensitive)

    Returns the track's name, volume (0-1 and dB), pan (-1 left .. 1 right), stereo
    separation, mute/solo/arm state, the effect plugins in its slots, the tracks it
    sends to (with send level) and the channels routed into it.
    """
    return fl_request("get_mixer_track", track=track)

def get_plugin_params(channel: str, name_filter: str = "", limit: int = 200):
    """
    Read the settings of the plugin on a channel

    Args:
        channel (str): Channel name (case-insensitive)
        name_filter (str): Only return parameters whose name contains this text (case-insensitive)
        limit (int): Maximum number of parameters to return

    Each parameter has its index, name, normalized value (0-1) and, where the
    plugin provides it, a display string such as "-6.0 dB". Unnamed VST slots
    and MIDI CC mappings are skipped. FL Studio doesn't expose default values,
    so this can't tell which settings were changed.
    """
    return fl_request("get_plugin_params", timeout=30.0, channel=channel, name_filter=name_filter, limit=limit)

def send_melody(notes_data):
    """
    Send a sequence of MIDI notes with timing information to FL Studio for recording

    Args:
        notes_data (str): String containing note data in format "note,velocity,length,position"
                         with each note on a new line
    """
    # Parse the notes_data string into a list of note tuples
    notes = []
    for line in notes_data.strip().split('\n'):
        if not line.strip():
            continue

        parts = line.strip().split(',')
        if len(parts) != 4:
            print(f"Warning: Skipping invalid line: {line}", file=sys.stderr)
            continue

        try:
            note = min(127, max(0, int(parts[0])))
            velocity = min(127, max(0, int(parts[1])))
            length = max(0, float(parts[2]))
            position = max(0, float(parts[3]))
            notes.append((note, velocity, length, position))
        except ValueError:
            print(f"Warning: Skipping line with invalid values: {line}", file=sys.stderr)
            continue

    if not notes:
        return "No valid notes found in input data"

    # Create the MIDI data array (5 values per note)
    midi_data = []
    for note, velocity, length, position in notes:
        # 1. Note value (0-127)
        midi_data.append(note)

        # 2. Velocity value (0-127)
        midi_data.append(velocity)

        # 3. Length whole part (0-127)
        length_whole = min(127, int(length))
        midi_data.append(length_whole)

        # 4. Length decimal part (0-9)
        length_decimal = int(round((length - length_whole) * 10)) % 10
        midi_data.append(length_decimal)

        # 5. Position whole part (0-127)
        position_whole = min(127, int(position))
        midi_data.append(position_whole)

        # 6. Position decimal part (0-9)
        position_decimal = int(round((position - position_whole) * 10)) % 10
        midi_data.append(position_decimal)

    # Start MIDI transfer
    print(f"Transferring {len(notes)} notes ({len(midi_data)} MIDI values)...", file=sys.stderr)

    # Initial toggle signal (note 0)
    send_midi_note(0)
    time.sleep(0.01)

    # Send total count of notes
    send_midi_note(min(127, len(notes)))
    time.sleep(0.01)

    # Send all MIDI data values
    for i, value in enumerate(midi_data):
        send_midi_note(value)
        #time.sleep(0.1)
        #print(f"Sent MIDI value {i+1}/{len(midi_data)}: {value}")

    send_midi_note(127)

    #print(f"Melody transfer complete: {len(notes)} notes sent")
    return f"Melody successfully transferred: {len(notes)} notes ({len(midi_data)} MIDI values) sent to FL Studio"

# Send a MIDI note message
def send_midi_note(note: int, velocity: int = 1, duration: float = 0.01):
    """Send a MIDI note on/off message with specified duration"""
    note_on = Message('note_on', note=note, velocity=velocity)
    send(note_on)
    #print(f"Sent MIDI note {note} (on), velocity {velocity}")
    time.sleep(duration)
    note_off = Message('note_off', note=note, velocity=0)
    send(note_off)
    print(f"Sent MIDI note {note} (off)", file=sys.stderr)
    #time.sleep(0.1)  # Small pause between messages

def after_reload():
    """Run by reload_server after reloading this module: also reload the FL script's request handlers"""
    try:
        return fl_request("reload_handlers", timeout=5.0)
    except FLStudioError as e:
        return f"FL Studio handlers not reloaded: {e}"

TOOLS = [
    list_midi_ports, play, stop, change_tempo, get_tempo, get_fl_log,
    get_pattern_names, get_channel_names, get_pattern, get_all_patterns, get_notes,
    get_mixer_track, get_plugin_params, send_melody, send_midi_note,
]
