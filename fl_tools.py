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

    Returns the tempo FL Studio reports afterwards, so the change can be checked.
    """
    # Ensure BPM is within a reasonable range
    if bpm < 20 or bpm > 999:
        print(f"Warning: BPM value {bpm} is outside normal range (20-999)", file=sys.stderr)
        bpm = max(20, min(bpm, 999))

    bpm_int = int(round(bpm))

    send(Message('control_change', control=CC_TEMPO_MSB, value=bpm_int >> 7))
    send(Message('control_change', control=CC_TEMPO_LSB, value=bpm_int & 0x7F))

    time.sleep(0.2)  # Give FL Studio a moment to apply it
    return get_tempo()

def get_tempo(timeout: float = 2.0):
    """
    Read the current project tempo from FL Studio

    Sends a request CC; the FL Studio script replies with BPM * 10 as two CC
    messages on the input port. Returns the tempo in BPM.

    Args:
        timeout (float): Seconds to wait for FL Studio's reply
    """
    if not ensure_listener():
        raise FLStudioError(f"No MIDI input named '{INPUT_PORT_NAME}'. Available inputs: {mido.get_input_names()}")

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
            bpm = ((msb << 7) | msg.value) / 10
            return int(bpm) if bpm.is_integer() else bpm

    raise FLStudioError(f"No tempo reply from FL Studio within {timeout}s. Check that FL Studio's MIDI output "
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

# --- Reading and editing the project ------------------------------------------
# Tools return only what was asked for, to keep responses small: callers pick
# fields, a single requested value comes back bare, results are keyed by name
# instead of repeating index/name/metadata, and defaults are left out.

CHANNEL_FIELD_GROUPS = {
    "info": ("type", "plugin", "mixer_track"),
    "mixer": ("volume", "volume_db", "pan", "pitch_cents", "muted", "solo"),
    "steps": ("steps",),
    "notes": ("notes",),  # opt-in: reading notes brings FL Studio to the front
}
CHANNEL_FIELDS = tuple(f for group in CHANNEL_FIELD_GROUPS.values() for f in group)

def expand_fields(fields, default_groups):
    """Requested fields (individual names and/or group names) as a list of individual fields"""
    expanded = []
    for f in fields or default_groups:
        expanded.extend(CHANNEL_FIELD_GROUPS.get(f, (f,)))
    unknown = [f for f in expanded if f not in CHANNEL_FIELDS]
    if unknown:
        raise ValueError(f"Unknown fields {unknown}. Choose from {list(CHANNEL_FIELDS)} "
                         f"or the groups {list(CHANNEL_FIELD_GROUPS)}")
    return list(dict.fromkeys(expanded))

def fl_groups(fields):
    """Field groups the FL script has to compute: notes are read separately and
    need the step grid to find the channels that have any"""
    groups = [g for g, members in CHANNEL_FIELD_GROUPS.items()
              if g != "notes" and any(f in members for f in fields)]
    if "notes" in fields and "steps" not in groups:
        groups.append("steps")
    return groups

def pick(data, fields):
    """Just the requested fields: a bare value for one field, else a dict"""
    return data[fields[0]] if len(fields) == 1 else {f: data[f] for f in fields}

def labels(items, name_key, id_key):
    """Names to key results by; a name used more than once gets its id appended"""
    names = [item[name_key] for item in items]
    return [n if names.count(n) == 1 else f"{n} #{item[id_key]}" for n, item in zip(names, items)]

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Piano roll note properties at their defaults are left out of results to save space
NOTE_DEFAULTS = {"velocity": 100, "pan": 0.5, "release": 0.5, "fcut": 0.5, "fres": 0.5, "pitchofs": 0,
                 "color": 0, "slide": False, "porta": False, "muted": False}

def compact_note(n, ppq):
    note = {
        "note": n["number"],                          # MIDI number; FL Studio names 60 as C5
        "start": round(n["time_ticks"] / ppq, 4),     # beats from the pattern start
        "length": round(n["length_ticks"] / ppq, 4),  # beats
    }
    values = dict(n, velocity=min(127, round(n["velocity"] * 128)))  # FL's 0-1 velocity as MIDI 0-127
    for key, default in NOTE_DEFAULTS.items():
        value = values[key]
        if (abs(value - default) > 0.01) if isinstance(default, float) else value != default:
            note[key] = round(value, 3) if isinstance(value, float) else value
    return note

def read_notes(channel, pattern=None, new_notes=None, removed_notes=None):
    """Export one channel's piano roll notes in a pattern (channel: name or global index),
    after removing removed_notes ({note, start}) and adding new_notes ({note, start,
    length, velocity}) if given. Returns {"notes", "removed", "warnings"}."""
    prep = fl_request("prepare_piano_roll", channel=channel, pattern=pattern)
    debug_trail = []

    def step(action, want_open=None, timeout=1.5):
        """Send one piano roll step; FL applies it after the request returns, so poll for the result"""
        state = fl_request("piano_roll_step", action=action)
        deadline = time.time() + timeout
        while want_open is not None and state["visible"] != want_open and time.time() < deadline:
            time.sleep(0.05)
            state = fl_request("piano_roll_step", action="state")
        debug_trail.append(f"{action}: open={state['visible']} focused={state['caption']!r}")
        return state

    def open_piano_roll():
        # Runs once FL Studio is in front (doing this while FL is in the background
        # stops Windows from bringing it forward). showWindow alone keeps the channel
        # the piano roll last showed; closing it and reopening it with F7 switches it
        # to the selected channel. FL's caption is just "Piano roll -", so this only
        # checks the piano roll has focus; the step-grid comparison below checks the channel.
        step("show", want_open=True)
        step("hide", want_open=False)
        if not step("f7", want_open=True)["visible"]:
            raise FLStudioError(f"FL Studio's piano roll didn't reopen. Steps: {debug_trail}")
        step("focus")
        time.sleep(0.1)
        caption = step("state")["caption"]
        if not caption.lower().startswith("piano roll"):
            raise FLStudioError(f"FL Studio's piano roll didn't get focus (focused window: {caption!r}). "
                                f"Steps: {debug_trail}")

    warnings = []
    try:
        try:
            edits = {key: value for key, value in (("add_notes", new_notes), ("remove_notes", removed_notes)) if value}
            state = run_export_notes(before_shortcut=open_piano_roll, window_warnings=warnings,
                                     request=edits or None)
        except FLStudioError as e:
            raise FLStudioError(f"{e} Steps: {debug_trail}") if debug_trail and "Steps:" not in str(e) else e
    finally:
        fl_request("restore_selection", **prep["restore"])
    ppq = state["ppq"]
    notes = sorted((compact_note(n, ppq) for n in state["notes"]), key=lambda n: (n["start"], n["note"]))
    removed = state.get("removed", 0)
    if removed_notes and removed < len(removed_notes):
        warnings.append(f"Only {removed} of {len(removed_notes)} notes were found and removed "
                        "(matched by note number and start).")
    # FL's step grid marks the step each note starts on; a mismatch suggests the wrong piano
    # roll was read. The grid was read before any edits, so expect the added notes' steps too;
    # a removed note may empty its step, so with removals the notes only have to fit in the grid.
    note_steps = sorted({n["time_ticks"] * 4 // ppq for n in state["notes"]})
    expected_steps = sorted(set(prep["steps_on"]) | {int(n["start"] * 4) for n in new_notes or []})
    if (not set(note_steps) <= set(expected_steps)) if removed_notes else note_steps != expected_steps:
        warnings.append(f"{prep['channel_name']}: note start steps {note_steps} don't match the step grid "
                        f"{expected_steps}; the export may come from a different piano roll.")
    return {"notes": notes, "removed": removed, "warnings": warnings}

def with_warnings(result, warnings):
    """Attach warnings only when there are any"""
    return {"result": result, "warnings": warnings} if warnings else result

def shape_patterns(data, fields, single_channel):
    """Shape FL's get_pattern / get_all_patterns data: {pattern: {channel: picked fields}}
    (or just the picked fields for a single channel), reading notes if asked for"""
    warnings = []
    content_only = set(fields) <= {"steps", "notes"}
    shaped = {}
    for pattern, pattern_label in zip(data, labels(data, "name", "pattern")):
        channels = {}
        for ch, channel_label in zip(pattern["channels"], labels(pattern["channels"], "name", "index")):
            ch["steps"] = ch.pop("steps_on", None)
            if "notes" in fields:
                # The step grid marks every note start, so channels without steps have no notes
                read = read_notes(ch["index"], pattern["pattern"]) if ch["steps"] else {"notes": [], "warnings": []}
                ch["notes"] = read["notes"]
                warnings.extend(read["warnings"])
            if content_only and not single_channel and not any(ch[f] for f in fields):
                continue
            channels[channel_label] = pick(ch, fields)
        shaped[pattern_label] = next(iter(channels.values()), None) if single_channel else channels
    return shaped, warnings

def get_pattern_names():
    """List the names of every non-empty pattern in the FL Studio project"""
    data = fl_request("get_pattern_names")
    return labels(data, "name", "pattern")

def get_channel_names():
    """List the names of every channel in the channel rack, in channel rack order"""
    data = fl_request("get_channel_names")
    return labels(data, "name", "index")

def get_channel(channel: str, fields: list[str] | None = None):
    """
    Read a channel's settings (the same in every pattern)

    Args:
        channel (str): Channel name (case-insensitive), or its position in the channel rack (0 = first)
        fields (list[str]): Any of "type", "plugin", "mixer_track", "volume", "volume_db",
            "pan" (-1 left .. 1 right), "pitch_cents", "muted", "solo", or the groups
            "info" (type, plugin, mixer_track) and "mixer" (volume .. solo).
            Default: info and mixer.

    One field returns just its value (e.g. ["mixer_track"] -> 0); several return a dict.
    """
    fields = expand_fields(fields, ("info", "mixer"))
    if any(f in ("steps", "notes") for f in fields):
        raise ValueError("Steps and notes belong to a pattern: use get_pattern or get_notes")
    return pick(fl_request("get_channel", channel=channel, fields=fl_groups(fields)), fields)

def get_pattern(pattern: str, channel: str = "", fields: list[str] | None = None):
    """
    Read a pattern's per-channel data from FL Studio

    Args:
        pattern (str): Pattern name (case-insensitive) or number
        channel (str): Only this channel (name, or position in the channel rack). Empty for all.
        fields (list[str]): Any of the get_channel fields, plus "steps" (step sequencer steps
            that are on, 16th-note step numbers from 0) and "notes" (piano roll notes as in
            get_notes; opt-in, FL Studio comes to the front while each channel is read).
            Groups: "info", "mixer", "steps", "notes". Default: info, mixer, steps.

    Returns {channel: value} with each value just the requested field (or a dict of
    several); with a channel given, just that channel's value. Asking only for steps
    and/or notes leaves out channels with nothing in the pattern. If several patterns
    share the name, the result is keyed by pattern too. Reading steps briefly switches
    FL Studio to that pattern and back.
    """
    fields = expand_fields(fields, ("info", "mixer", "steps"))
    data = fl_request("get_pattern", pattern=pattern, channel=channel or None, fields=fl_groups(fields))
    shaped, warnings = shape_patterns(data, fields, single_channel=bool(channel))
    return with_warnings(shaped if len(shaped) > 1 else next(iter(shaped.values())), warnings)

def get_all_patterns(fields: list[str] | None = None):
    """
    Read every non-empty pattern's per-channel data from FL Studio

    Args:
        fields (list[str]): Same as get_pattern (default: ["steps"], the cheapest way to see
            what each pattern contains; channel settings are the same in every pattern, so
            read those with get_channel). "notes" reads every channel's piano roll in every
            pattern, one at a time, so it is slow.

    Returns {pattern: {channel: value}}. Steps are read by switching FL Studio through
    each pattern, then back to the one that was active.
    """
    fields = expand_fields(fields, ("steps",))
    data = fl_request("get_all_patterns", timeout=60.0, fields=fl_groups(fields))
    shaped, warnings = shape_patterns(data, fields, single_channel=False)
    return with_warnings(shaped, warnings)

def get_notes(channel: str, pattern: str = ""):
    """
    Read the piano roll notes of one channel in a pattern

    Args:
        channel (str): Channel name (case-insensitive)
        pattern (str): Pattern name or number; empty uses the active pattern

    Returns a list of notes: {"note": MIDI number (FL Studio names 60 as C5), "start"
    and "length" in beats from the pattern start}, plus velocity (0-127), pan, release,
    filter, fine pitch, color, slide, porta or muted only where not at their defaults
    (velocity 100).

    How it works: FL Studio is brought to the front with that channel's piano roll
    open, the ExportNotes piano roll script is run with Ctrl+Alt+Y ("Run last script
    again") and focus and window order are put back afterwards. ExportNotes must be
    the last piano roll script run in FL Studio; otherwise Ctrl+Alt+Y runs a different one.
    """
    read = read_notes(channel, pattern or None)
    return with_warnings(read["notes"], read["warnings"])

def add_notes(channel: str, notes: list[dict], pattern: str = "", return_notes: bool = False):
    """
    Add notes to one channel's piano roll in a pattern

    Args:
        channel (str): Channel name (case-insensitive)
        notes (list[dict]): Notes to add, each {"note": MIDI number (60 = C5 in FL
            naming), "start": beats from the pattern start, "length": beats,
            "velocity": 0-127 (default 100)}
        pattern (str): Pattern name or number; empty uses the active pattern
        return_notes (bool): Also return all of the channel's notes afterwards

    Existing notes are kept (calling it twice adds the notes twice). Returns
    {"added", "total"}; the export afterwards is checked against FL's step grid and
    any problem is returned as a warning. Works like get_notes (FL Studio comes to the
    front briefly). Undo in FL Studio with Ctrl+Z.
    """
    for n in notes:
        if not 0 <= int(n["note"]) <= 127 or n["start"] < 0 or n["length"] < 0:
            raise ValueError(f"Invalid note {n}: note must be 0-127, start and length at least 0")
    read = read_notes(channel, pattern or None, new_notes=notes)
    result = {"added": len(notes), "total": len(read["notes"])}
    if return_notes:
        result["notes"] = read["notes"]
    return with_warnings(result, read["warnings"])

def remove_notes(channel: str, notes: list[dict], pattern: str = "", return_notes: bool = False):
    """
    Remove notes from one channel's piano roll in a pattern

    Args:
        channel (str): Channel name (case-insensitive)
        notes (list[dict]): Notes to remove, each {"note": MIDI number (60 = C5 in FL
            naming), "start": beats from the pattern start}. Each entry removes one
            matching note; other notes are left alone.
        pattern (str): Pattern name or number; empty uses the active pattern
        return_notes (bool): Also return all of the channel's notes afterwards

    Returns {"removed", "total"} (a warning if some notes weren't found). Works like
    get_notes (FL Studio comes to the front briefly). Undo in FL Studio with Ctrl+Z.
    """
    for n in notes:
        if not 0 <= int(n["note"]) <= 127 or n["start"] < 0:
            raise ValueError(f"Invalid note {n}: note must be 0-127 and start at least 0")
    read = read_notes(channel, pattern or None, removed_notes=notes)
    result = {"removed": read["removed"], "total": len(read["notes"])}
    if return_notes:
        result["notes"] = read["notes"]
    return with_warnings(result, read["warnings"])

def move_history(steps, undo):
    """Undo (or redo) steps actions and return FL's "position/length" hint. The API
    says negative undoes, but FL 21 swapped related functions, so check which way the
    position moved (0 = most recent, higher = further back) and correct a wrong move."""
    if steps < 1:
        raise ValueError("steps must be at least 1")
    delta = -steps if undo else steps
    before = fl_request("undo_state")["position"]
    fl_request("undo_move", delta=delta)
    after = fl_request("undo_state")
    if after["position"] != before and (after["position"] > before) != undo:
        fl_request("undo_move", delta=-2 * delta)  # went the wrong way: go back past the start
        after = fl_request("undo_state")
    return after["hint"]

def undo(steps: int = 1):
    """
    Undo the last actions in FL Studio (step back through its undo history)

    Args:
        steps (int): How many actions to undo

    Returns FL's undo position as "position/length" (1 = most recent action).
    Each add_notes / remove_notes call is one action.
    """
    return move_history(steps, undo=True)

def redo(steps: int = 1):
    """
    Redo actions undone with undo (step forward through FL Studio's undo history)

    Args:
        steps (int): How many actions to redo

    Returns FL's undo position as "position/length" (1 = most recent action).
    """
    return move_history(steps, undo=False)

MIXER_TRACK_FIELDS = ("name", "volume", "volume_db", "pan", "stereo_separation", "muted", "solo",
                      "armed", "effects_enabled", "effects", "sends_to", "channels")

def get_mixer_track(track: str, fields: list[str] | None = None):
    """
    Read a mixer track from FL Studio

    Args:
        track (str): Mixer track number (0 = Master; the number get_channel reports as
                     "mixer_track") or track name (case-insensitive)
        fields (list[str]): Any of "name", "volume" (0-1), "volume_db", "pan" (-1 left ..
            1 right), "stereo_separation", "muted", "solo", "armed", "effects_enabled",
            "effects" ({slot: plugin}), "sends_to" ({track: send level}), "channels"
            (channels routed into it). Default: all.

    One field returns just its value; several return a dict.
    """
    fields = list(fields or MIXER_TRACK_FIELDS)
    unknown = [f for f in fields if f not in MIXER_TRACK_FIELDS]
    if unknown:
        raise ValueError(f"Unknown fields {unknown}. Choose from {list(MIXER_TRACK_FIELDS)}")
    data = fl_request("get_mixer_track", track=track)
    data["effects"] = {str(e["slot"]): e.get("name", e["plugin"]) for e in data["effects"]}
    data["sends_to"] = {s["name"]: s["level"] for s in data["sends_to"]}
    return pick(data, fields)

def get_plugin_params(channel: str, name_filter: str = "", limit: int = 200):
    """
    Read the settings of the plugin on a channel

    Args:
        channel (str): Channel name (case-insensitive)
        name_filter (str): Only parameters whose name contains this text (case-insensitive)
        limit (int): Maximum number of parameters to return

    Returns {parameter name: value}, the value being the plugin's display text (such
    as "-6.0 dB") where it provides one, else the normalized 0-1 value; "_more": true
    when the limit cut the list short. Unnamed VST slots and MIDI CC mappings are
    skipped. FL Studio doesn't expose defaults, so this can't tell which were changed.
    """
    data = fl_request("get_plugin_params", timeout=30.0, channel=channel, name_filter=name_filter, limit=limit)
    params = {p["name"]: p["display"] if p["display"] is not None else p["value"] for p in data["params"]}
    if data["returned"] >= limit:
        params["_more"] = True
    return params

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
    get_pattern_names, get_channel_names, get_channel, get_pattern, get_all_patterns, get_notes, add_notes, remove_notes, undo, redo,
    get_mixer_track, get_plugin_params, send_melody, send_midi_note,
]
