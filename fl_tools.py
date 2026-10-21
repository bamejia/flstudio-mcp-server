"""
The flstudio MCP server's tools. Every function in TOOLS is registered by
trigger.py and re-registered when the reload_server tool reloads this module.
"""
import inspect
import queue
import sys
import time

import mido
from mido import Message

from fl_bridge import (CC_TEMPO_LSB, CC_TEMPO_MSB, CC_TEMPO_REPLY_LSB, CC_TEMPO_REPLY_MSB,
                       CC_TEMPO_REQUEST, INPUT_PORT_NAME, FLStudioError, ensure_listener, fl_log,
                       fl_request, run_export_notes, send, tempo_replies)

# Drum sound MIDI notes
KICK = 36      # C1
SNARE = 38     # D1
CLAP = 39      # D#1
CLOSED_HAT = 42  # F#1
OPEN_HAT = 46  # A#1



def list_midi_ports():
    """Names of the MIDI output ports."""
    print("\nAvailable MIDI Input Ports:", file=sys.stderr)
    input_ports = mido.get_output_names()
    if not input_ports:
        print("  No MIDI input ports found", file=sys.stderr)
    else:
        for i, port in enumerate(input_ports):
            print(f"  {i}: {port}", file=sys.stderr)

    return input_ports

PLAY_MODES = {"p": "pattern", "pat": "pattern", "pattern": "pattern", "s": "song", "song": "song"}

def transport_request(cmd, done, **args):
    """Send a transport request, then poll FL's transport state until done(state)
    holds, since FL may apply the change only after the request returns"""
    state = fl_request(cmd, **args)
    for _ in range(20):
        if done(state):
            return state
        time.sleep(0.05)
        state = fl_request("transport_state")
    raise FLStudioError(f"FL Studio didn't apply {cmd}; its transport state is {state}")

def play(mode: str = "", beat: float | None = None):
    """
    Start or resume playback (no-op if already playing). Returns "playing".
    mode: "p"/"pattern" or "s"/"song"; sets FL's PAT/SONG switch, which stays set.
    beat: jump to this beat from the pattern/song start, also while playing.
    Fails while record is on (play would record); record(False) turns it off.
    """
    args = {}
    if mode:
        if mode.lower() not in PLAY_MODES:
            raise ValueError('mode must be "pattern"/"p" or "song"/"s"')
        args["mode"] = PLAY_MODES[mode.lower()]
    if beat is not None:
        args["beat"] = beat
    # A jump shows as a position at (or, still playing, just past) the beat
    transport_request("play", lambda s: s["playing"] and s["mode"] == args.get("mode", s["mode"])
                      and (beat is None or beat - 0.01 <= s["beat"] < beat + 1), **args)
    return "playing"

def pause():
    """Pause, keeping the position (play resumes). Returns "paused" or "not playing"."""
    state = transport_request("pause", lambda s: not s["playing"])
    return "paused" if state.get("was_playing", True) else "not playing"

def stop():
    """Stop playback (back to the start). Returns "stopped"."""
    transport_request("stop", lambda s: not s["playing"])
    return "stopped"

def record(on: bool = True):
    """Set FL's record button (on: True/False); while on, play records. Returns "on"/"off"."""
    transport_request("record", lambda s: s["recording"] == on, on=on)
    return "on" if on else "off"

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
    """Set the tempo (bpm 20-999, rounded to whole BPM). Returns the tempo FL reports."""
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
    """Project tempo in BPM. timeout: seconds to wait for FL's reply."""
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
    Recent print() lines of the FL controller script (last 1000 received while this server ran).
    lines: how many of the latest; contains: only lines with this text (case-insensitive);
    clear: empty the log after reading.
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

# Short aliases accepted for field names, to keep calls short (the full names work too)
FIELD_ALIASES = {
    "i": "instruments", "ins": "instruments", "inst": "instruments",
    "inf": "info", "m": "mixer", "mix": "mixer", "s": "steps", "st": "steps", "n": "notes",
    "t": "type", "pl": "plugin", "plug": "plugin", "mt": "mixer_track", "trk": "mixer_track", "track": "mixer_track",
    "v": "volume", "vol": "volume", "db": "volume_db", "p": "pan", "pc": "pitch_cents", "pitch": "pitch_cents",
    "mu": "muted", "mute": "muted", "so": "solo",
    "sep": "stereo_separation", "a": "armed", "arm": "armed", "fxon": "effects_enabled", "fx": "effects",
    "sends": "sends_to", "ch": "channels",
}

def unalias(fields):
    """Field names with short aliases replaced by the full names"""
    return [FIELD_ALIASES.get(f.lower(), f.lower()) for f in fields]

def field_choices(names):
    """Field names with their shortest alias, for error messages: "volume(v) pan(p) ..." """
    short = {}
    for alias, name in FIELD_ALIASES.items():
        if name in names and len(alias) < len(short.get(name, name)):
            short[name] = alias
    return " ".join(f"{n}({short[n]})" if n in short else n for n in names)

def expand_fields(fields, default_groups):
    """Requested fields (individual names and/or group names, full or aliased) as a
    list of individual fields"""
    expanded = []
    for f in unalias(fields or default_groups):
        expanded.extend(CHANNEL_FIELD_GROUPS.get(f, (f,)))
    if "instruments" in expanded:
        raise ValueError('"instruments" (i) works alone, with get_pattern or get_all_patterns')
    unknown = [f for f in expanded if f not in CHANNEL_FIELDS]
    if unknown:
        raise ValueError(f"Unknown fields {unknown}. Choose from {field_choices(CHANNEL_FIELDS)} "
                         f"or the groups {field_choices(tuple(CHANNEL_FIELD_GROUPS))}")
    return list(dict.fromkeys(expanded))

def pattern_fields(fields, default_groups):
    """expand_fields for pattern tools, which also take "instruments" on its own:
    just the names of the channels used in the pattern"""
    if fields and unalias(fields) == ["instruments"]:
        return ["instruments"]
    return expand_fields(fields, default_groups)

def fl_groups(fields):
    """Field groups the FL script has to compute: notes are read separately, and
    they and instruments need the step grid to find the channels that have any"""
    groups = [g for g, members in CHANNEL_FIELD_GROUPS.items()
              if g != "notes" and any(f in members for f in fields)]
    if ("notes" in fields or "instruments" in fields) and "steps" not in groups:
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
    (or just the picked fields for a single channel), reading notes if asked for;
    for ["instruments"], {pattern: [names of the channels used in it]}"""
    warnings = []
    content_only = set(fields) <= {"steps", "notes"}
    shaped = {}
    for pattern, pattern_label in zip(data, labels(data, "name", "pattern")):
        channel_labels = labels(pattern["channels"], "name", "index")
        if fields == ["instruments"]:
            # The step grid marks every note start, so a channel with notes has steps too
            shaped[pattern_label] = [label for ch, label in zip(pattern["channels"], channel_labels)
                                     if ch["steps_on"]]
            continue
        channels = {}
        for ch, channel_label in zip(pattern["channels"], channel_labels):
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
    """Names of the non-empty patterns."""
    data = fl_request("get_pattern_names")
    return labels(data, "name", "pattern")

def get_channel_names():
    """Channel rack channel names, in order."""
    data = fl_request("get_channel_names")
    return labels(data, "name", "index")

def get_channel(channel: str, fields: list[str] | None = None):
    """
    A channel's settings (same in every pattern).
    channel: name or its start (case-insensitive), or channel rack index (0 = first).
    fields: type(t) plugin(pl) mixer_track(mt) volume(v) volume_db(db) pan(p: -1..1)
      pitch_cents(pc) muted(mu) solo(so); groups info(inf: type, plugin, mixer_track),
      mixer(m: volume..solo). Default: info, mixer.
    One field returns its bare value (["mt"] -> 0); several a dict.
    """
    fields = expand_fields(fields, ("info", "mixer"))
    if any(f in ("steps", "notes") for f in fields):
        raise ValueError("Steps and notes belong to a pattern: use get_pattern or get_notes")
    return pick(fl_request("get_channel", channel=channel, fields=fl_groups(fields)), fields)

def get_pattern(pattern: str, channel: str = "", fields: list[str] | None = None):
    """
    Per-channel data of a pattern: {channel: value}.
    pattern: number, or name or its start. channel: only this one (as get_channel); empty = all.
    fields: get_channel's, plus steps(s: on-steps as 16ths from 0) and notes(n: as get_notes;
      slow, FL comes to the front); or instruments(i) alone: names of the channels used.
      Default: info, mixer, steps.
    One field gives bare values; with channel, just its value. Only steps/notes skips empty
    channels. Patterns sharing a name are keyed by pattern.
    """
    fields = pattern_fields(fields, ("info", "mixer", "steps"))
    data = fl_request("get_pattern", pattern=pattern, channel=channel or None, fields=fl_groups(fields))
    shaped, warnings = shape_patterns(data, fields, single_channel=bool(channel))
    return with_warnings(shaped if len(shaped) > 1 else next(iter(shaped.values())), warnings)

def get_all_patterns(fields: list[str] | None = None):
    """
    Every non-empty pattern's data: {pattern: {channel: value}}.
    fields: as get_pattern. Default ["s"] (steps); ["i"] gives {pattern: [channel names]};
    notes(n) reads every piano roll (slow). For channel settings use get_channel.
    """
    fields = pattern_fields(fields, ("steps",))
    data = fl_request("get_all_patterns", timeout=60.0, fields=fl_groups(fields))
    shaped, warnings = shape_patterns(data, fields, single_channel=False)
    return with_warnings(shaped, warnings)

def get_notes(channel: str, pattern: str = ""):
    """
    Piano roll notes of a channel in a pattern: [{note, start, length}]; note = MIDI number
    (60 = C5 in FL), start/length in beats from the pattern start; velocity (0-127, default
    100), pan, release, fcut, fres, pitchofs, color, slide, porta, muted only when non-default.
    channel: name or its start, or channel rack index. pattern: number, or name or its
    start; empty = active pattern.
    FL comes to the front briefly; ExportNotes must be FL's last-run piano roll script.
    """
    read = read_notes(channel, pattern or None)
    return with_warnings(read["notes"], read["warnings"])

NOTE_KEYS = ("note", "start", "length", "velocity")
NOTE_KEY_ALIASES = {"n": "note", "s": "start", "l": "length", "len": "length", "v": "velocity", "vel": "velocity"}

def note_input(n, keys):
    """A note given to add_notes/remove_notes as {key: value} with the full keys. Accepts a
    list [note, start, length, velocity] or a dict with full or short keys (n, s, l, v);
    keys are the ones it must have."""
    note = dict(zip(NOTE_KEYS, n)) if isinstance(n, (list, tuple)) else {
        NOTE_KEY_ALIASES.get(k.lower(), k.lower()): v for k, v in n.items()}
    if any(k not in note for k in keys) or not 0 <= int(note["note"]) <= 127 or note["start"] < 0 \
            or note.get("length", 0) < 0:
        raise ValueError(f"Invalid note {n}: needs {list(keys)}; note 0-127, start and length at least 0")
    for k in ("note", "velocity"):  # list input arrives as floats
        if k in note:
            note[k] = int(note[k])
    return note

def add_notes(channel: str, notes: list[dict | list[float]], pattern: str = "", return_notes: bool = False):
    """
    Add notes to a channel's piano roll (existing notes kept). Returns {added, total}.
    notes: each [note, start, length, velocity?] or {n, s, l, v}: MIDI number (60 = C5),
      beats from the pattern start, beats, 0-127 (default 100). E.g. [[60,0,1],[64,1,0.5,90]].
    channel, pattern: as get_notes. return_notes: also return all of its notes.
    FL comes to the front briefly; problems come back as warnings; undo() reverts.
    """
    notes = [note_input(n, ("note", "start", "length")) for n in notes]
    read = read_notes(channel, pattern or None, new_notes=notes)
    result = {"added": len(notes), "total": len(read["notes"])}
    if return_notes:
        result["notes"] = read["notes"]
    return with_warnings(result, read["warnings"])

def remove_notes(channel: str, notes: list[dict | list[float]], pattern: str = "", return_notes: bool = False):
    """
    Remove notes from a channel's piano roll; each [note, start] or {n, s} removes one
    match. channel, pattern, return_notes: as add_notes. Returns {removed, total}, with a
    warning if some weren't found. undo() reverts.
    """
    notes = [note_input(n, ("note", "start")) for n in notes]
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
    """Undo FL actions (steps; each add/remove_notes call is one). Returns FL's "position/length" (1 = latest)."""
    return move_history(steps, undo=True)

def redo(steps: int = 1):
    """Redo undone FL actions (steps). Returns FL's "position/length"."""
    return move_history(steps, undo=False)

MIXER_TRACK_FIELDS = ("name", "volume", "volume_db", "pan", "stereo_separation", "muted", "solo",
                      "armed", "effects_enabled", "effects", "sends_to", "channels")

def get_mixer_track(track: str, fields: list[str] | None = None):
    """
    A mixer track's data.
    track: number (0 = Master; get_channel's mixer_track), or name or its start.
    fields: name volume(v: 0-1) volume_db(db) pan(p: -1..1) stereo_separation(sep) muted(mu)
      solo(so) armed(a) effects_enabled(fxon) effects(fx: {slot: plugin}) sends_to(sends:
      {track: level}) channels(ch: channels routed in). Default: all.
    One field returns its bare value; several a dict.
    """
    fields = unalias(fields or MIXER_TRACK_FIELDS)
    unknown = [f for f in fields if f not in MIXER_TRACK_FIELDS]
    if unknown:
        raise ValueError(f"Unknown fields {unknown}. Choose from {field_choices(MIXER_TRACK_FIELDS)}")
    data = fl_request("get_mixer_track", track=track)
    data["effects"] = {str(e["slot"]): e.get("name", e["plugin"]) for e in data["effects"]}
    data["sends_to"] = {s["name"]: s["level"] for s in data["sends_to"]}
    return pick(data, fields)

def get_plugin_params(channel: str, name_filter: str = "", limit: int = 200):
    """
    Settings of a channel's plugin: {param: display text, else 0-1 value}; "_more": true
    if cut at limit. Defaults aren't available.
    channel: as get_channel. name_filter: only names containing this (case-insensitive).
    """
    data = fl_request("get_plugin_params", timeout=30.0, channel=channel, name_filter=name_filter, limit=limit)
    params = {p["name"]: p["display"] if p["display"] is not None else p["value"] for p in data["params"]}
    if data["returned"] >= limit:
        params["_more"] = True
    return params

def send_melody(notes_data):
    """(Legacy; prefer add_notes.) Records notes over MIDI: notes_data lines "note,velocity,length,position" (beats; 0.1 resolution; max 127)."""
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
    """Send a MIDI note on, then off after duration seconds."""
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
    list_midi_ports, play, pause, stop, record, change_tempo, get_tempo, get_fl_log,
    get_pattern_names, get_channel_names, get_channel, get_pattern, get_all_patterns, get_notes, add_notes, remove_notes, undo, redo,
    get_mixer_track, get_plugin_params, send_melody, send_midi_note,
]

# A tool's description is its docstring, sent whenever the tools load: drop the indentation
for _tool in TOOLS:
    _tool.__doc__ = inspect.cleandoc(_tool.__doc__ or "")
