"""
The flstudio MCP server's tools. Every function in TOOLS is registered by
trigger.py and re-registered when the reload_server tool reloads this module.
"""
import collections
import contextlib
import difflib
import inspect
import queue
import re
import sys
import time

import mido
from mido import Message

from fl_bridge import (CC_TEMPO_LSB, CC_TEMPO_MSB, CC_TEMPO_REPLY_LSB, CC_TEMPO_REPLY_MSB,
                       CC_TEMPO_REQUEST, FL_USER_DATA_DIR, INPUT_PORT_NAME, VK_CONTROL, VK_DOWN, VK_END,
                       VK_ESCAPE, VK_HOME, VK_L, VK_R, VK_RETURN, VK_RIGHT, VK_SHIFT, VK_UP, FLStudioError,
                       ensure_listener, fl_in_front, fl_install_dir, child_windows, click, cursor_kept, fl_log,
                       fl_main_window, fl_request, focus_kept, foreground_class, hover, fl_top_window,
                       fl_top_windows, open_in_fl, press_keys, require, run_export_notes, save_permissions,
                       screen_colors, send, tempo_replies, type_text, uncovered, window_dpi, window_rect,
                       window_title)

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
      solo(so) armed(a) effects_enabled(fxon) effects(fx: {slot: plugin, "+ (off)" if off}) sends_to(sends:
      {track: level}) channels(ch: channels routed in). Default: all.
    One field returns its bare value; several a dict.
    """
    fields = unalias(fields or MIXER_TRACK_FIELDS)
    unknown = [f for f in fields if f not in MIXER_TRACK_FIELDS]
    if unknown:
        raise ValueError(f"Unknown fields {unknown}. Choose from {field_choices(MIXER_TRACK_FIELDS)}")
    data = fl_request("get_mixer_track", track=track)
    data["effects"] = {str(e["slot"]): e.get("name", e["plugin"]) + ("" if e["on"] else " (off)")
                       for e in data["effects"]}
    data["sends_to"] = {s["name"]: s["level"] for s in data["sends_to"]}
    return pick(data, fields)

def set_permissions(mouse: bool | None = None, focus: bool | None = None):
    """
    Switch what FL tools may do. Only when the user explicitly says so. mouse: clicking in
    FL (default off). focus: bringing FL to the front for a moment for shortcut keys (default
    on). Saved until changed; tools refuse what's off. Returns both.
    """
    return save_permissions(mouse=mouse, focus=focus)

def plugin_args(channel, track, slot):
    """Request args for a channel's plugin, or the effect in a mixer track's slot"""
    if bool(channel) == bool(track):
        raise ValueError("Give either channel, or track and slot")
    return {"channel": channel} if channel else {"track": track, "slot": slot}

def get_plugin_params(channel: str = "", track: str = "", slot: int | None = None, name_filter: str = "",
                      limit: int = 200):
    """
    Settings of a channel's plugin, or of the effect in a mixer track's slot (0-9):
    {param: display text, else 0-1 value}; "_more": true if cut at limit. Defaults aren't available.
    channel: as get_channel. track: as get_mixer_track. name_filter: only names containing this.
    """
    data = fl_request("get_plugin_params", timeout=30.0, name_filter=name_filter, limit=limit,
                      **plugin_args(channel, track, slot))
    params = {p["name"]: p["display"] if p["display"] is not None else p["value"] for p in data["params"]}
    if data["returned"] >= limit:
        params["_more"] = True
    return params

# A value given as display text ("-6 dB", "1.2 kHz", "Low pass") is found by probing:
# FL reports a parameter's text only for its current value, so probe_param sets a row of
# values, reads each text and puts the parameter back. Numbers are bracketed and the
# bracket narrowed a few times; other texts are matched against the labels seen.
UNIT_PREFIXES = {"k": 1e3, "m": 1e-3, "µ": 1e-6, "u": 1e-6}
NUMBER = re.compile(r"([-+]?(?:inf|\d+(?:[.,]\d+)?|[.,]\d+))\s*([^\d\s]*)", re.IGNORECASE)

def parse_number(text):
    """(value in base units, unit) of a display text such as "1.2 kHz" -> (1200, "hz"), else None"""
    match = NUMBER.search(text or "")
    if not match:
        return None
    value, unit = float(match[1].replace(",", ".")), match[2].lower()
    if len(unit) > 1 and unit[0] in UNIT_PREFIXES and unit[1:] in ("hz", "s"):
        value, unit = value * UNIT_PREFIXES[unit[0]], unit[1:]
    elif unit == "k":
        value, unit = value * 1e3, ""
    return value, unit

def param_value(text, probe):
    """The 0-1 value at which a parameter displays text; probe(values) -> their texts"""
    grid = [i / 64 for i in range(65)]
    texts = probe(grid)
    target = parse_number(text)
    if target is None:
        values, labels = grid, texts
        for _ in range(2):  # then a finer row, for labels the first one skipped
            for match in (lambda t: t.lower() == text.lower(), lambda t: t.lower().startswith(text.lower())):
                hits = [v for v, t in zip(values, labels) if match(t)]
                if hits:
                    return hits[len(hits) // 2]
            values = [i / 512 for i in range(513)]
            labels = probe(values)
        raise ValueError(f"No value shows {text!r}. It shows e.g. {', '.join(dict.fromkeys(labels))[:300]}")
    numbers = [parse_number(t) for t in texts]
    units = {n[1] for n in numbers if n}
    if target[1] and units and target[1] not in units:
        raise ValueError(f"This parameter shows {texts[0]!r} to {texts[-1]!r}, not {target[1]!r}")
    best = min((abs(n[0] - target[0]), v) for v, n in zip(grid, numbers) if n)[1] if units else None
    if best is None:
        raise ValueError(f"This parameter shows no numbers ({texts[0]!r} to {texts[-1]!r})")
    low, high = max(best - 1 / 64, 0.0), min(best + 1 / 64, 1.0)
    for _ in range(3):  # narrow around the closest value
        values = [low + (high - low) * i / 32 for i in range(33)]
        numbers = [parse_number(t) for t in probe(values)]
        best = min((abs(n[0] - target[0]), v) for v, n in zip(values, numbers) if n)[1]
        step = (high - low) / 32
        low, high = max(best - step, 0.0), min(best + step, 1.0)
    return best

def resolve_value(value, param, args):
    """A 0-1 value as given, or the one for a display text"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not 0 <= value <= 1:
            raise ValueError("A number value must be 0-1; give FL's display text (e.g. \"-6 dB\") otherwise")
        return float(value)
    text = str(value).strip()
    try:
        return resolve_value(float(text), param, args)
    except ValueError:
        pass
    return param_value(text, lambda values: fl_request("probe_param", param=param, values=values, **args)["texts"])

def set_plugin_param(param: str, value: float | str, channel: str = "", track: str = "", slot: int | None = None):
    """
    Set a parameter of a channel's plugin, or of the effect in a mixer track's slot (0-9).
    param: name or its start (as get_plugin_params), or index. value: 0-1, or the text FL
    shows, e.g. "-6 dB", "1.2 kHz", "50%", "Low pass" (found by trying values).
    channel: as get_channel. track: as get_mixer_track. Returns the text FL shows (else the 0-1 value).
    """
    args = plugin_args(channel, track, slot)
    target = resolve_value(value, param, args)
    read = lambda: fl_request("param_state", param=param, **args)
    before = read()
    fl_request("set_param", param=param, value=target, **args)
    # Some plugins (Vital) update the text a moment later
    data = wait_for(read, lambda d: d["display"] != before["display"] or abs(before["value"] - target) < 1e-6, 1.0)
    return data["display"] if data["display"] is not None else data["value"]

def current_preset(args):
    """The plugin's current preset name, which FL updates just after a preset request returns"""
    time.sleep(0.1)
    return fl_request("preset", **args)["current"]

def change_preset(steps: int = 1, name: str = "", channel: str = "", track: str = "", slot: int | None = None):
    """
    Change the preset of a channel's plugin or of the effect in a mixer track's slot (0-9):
    steps forward (negative: back), or to the one called name (or its start; only plugins
    that name their presets). channel: as get_channel. track: as get_mixer_track.
    Returns the preset's name ("" if FL has none). FL renames a VST's channel after its preset.
    """
    args = plugin_args(channel, track, slot)
    if channel:  # the channel may be renamed after its preset: keep to its index
        args["channel"] = str(fl_request("get_channel", channel=channel, fields=[])["index"])
    if not name:
        fl_request("preset", steps=steps, **args)
        return current_preset(args)
    info = fl_request("preset", names=True, **args)
    names = [n.lower() for n in info["names"]]
    query = name.strip().lower()
    matches = ([i for i, n in enumerate(names) if n == query] or [i for i, n in enumerate(names) if n.startswith(query)]
               or [i for i, n in enumerate(names) if query in n])
    if not matches:
        raise ValueError(f"No preset {name!r}. Presets: {', '.join(n for n in info['names'] if n)[:400]}")
    target, current = matches[0], info["current"].lower()
    if names.count(current) != 1:  # no preset chosen yet, or a name used twice: step to a known one
        fl_request("preset", steps=1, **args)
        current = current_preset(args).lower()
    if names.count(current) == 1:
        forward = (target - names.index(current)) % len(names)
        fl_request("preset", steps=forward if forward <= len(names) / 2 else forward - len(names), **args)
    return current_preset(args)

def color_value(color):
    """FL's color int from "#RRGGBB" (or "RRGGBB", "0xRRGGBB")"""
    text = str(color).strip().lstrip("#")
    text = text[2:] if text.lower().startswith("0x") else text
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        raise ValueError(f'color must be like "#FF8800", not {color!r}')
    return int(text, 16)

def set_values(cmd, given, renamed=None, **ids):
    """Send a set_* request with the given values (None = unchanged; color as "#RRGGBB") and
    return just those values as FL now has them (renamed: request key -> result key)"""
    given = {k: v for k, v in given.items() if v is not None}
    if not given:
        raise ValueError("Nothing to change")
    data = fl_request(cmd, **ids, **{k: color_value(v) if k == "color" else v for k, v in given.items()})
    data["color"] = f"#{data['color']:06X}"
    if isinstance(data.get("sends_to"), list):
        data["sends_to"] = {s["name"]: s["level"] for s in data["sends_to"]}
    return pick(data, [(renamed or {}).get(k, k) for k in given])

def set_channel(channel: str, name: str | None = None, color: str | None = None, volume: float | None = None,
                db: float | None = None, pan: float | None = None, muted: bool | None = None,
                solo: bool | None = None, track: str | None = None):
    """
    Change a channel; give only what to change. channel: as get_channel.
    color: "#RRGGBB". volume: 0-1 fader, or db in dB. pan: -1..1.
    track: mixer track to route it to (as get_mixer_track), or "free": FL's Ctrl+L, to a free
    track named after the channel (FL comes to the front briefly).
    Returns the changed values as FL now has them (one value bare).
    """
    warnings = []
    given = dict(name=name, color=color, volume=volume, db=db, pan=pan, muted=muted, solo=solo)
    if track is not None and str(track).strip().lower() == "free":
        free = assign_free_mixer_track(channel, warnings)
        if not any(v is not None for v in given.values()):
            return with_warnings(free, warnings)
        track = None
    given["mixer_track"] = track
    result = set_values("set_channel", given, {"db": "volume_db"}, channel=channel)
    return with_warnings(result, warnings)

def set_mixer_track(track: str, name: str | None = None, color: str | None = None, volume: float | None = None,
                    db: float | None = None, pan: float | None = None, sep: float | None = None,
                    muted: bool | None = None, solo: bool | None = None, armed: bool | None = None,
                    fx_on: bool | None = None, sends: dict | None = None, slot_mix: dict | None = None,
                    slot_on: dict | None = None):
    """
    Change a mixer track; give only what to change. track: as get_mixer_track.
    color: "#RRGGBB". volume: 0-1 fader (0.8 = 0 dB), or db in dB. pan, sep: -1..1.
    fx_on: the track's effect slots switch. sends: {track: 0-1 level, true (route) or false
    (unroute)}. slot_mix: {slot: 0-1 effect mix}. slot_on: {slot: bool}.
    Returns the changed values as FL now has them (one value bare).
    """
    given = dict(name=name, color=color, volume=volume, db=db, pan=pan, sep=sep, muted=muted, solo=solo,
                 armed=armed, fx_on=fx_on, sends=sends, slot_mix=slot_mix, slot_on=slot_on)
    return set_values("set_mixer_track", given, {"db": "volume_db", "sep": "stereo_separation",
                                                 "fx_on": "effects_enabled", "sends": "sends_to"}, track=track)

def set_pattern(pattern: str = "", clone: bool = False, name: str | None = None, color: str | None = None):
    """
    Rename/recolor a pattern, or a new clone of it (clone: FL puts it after the pattern), or
    without pattern a new empty one; a new one becomes the active pattern.
    pattern: number, or name or its start. color: "#RRGGBB". Returns {pattern, name}.
    """
    if pattern and not clone and name is None and color is None:
        raise ValueError("Nothing to change")
    return fl_request("set_pattern", pattern=pattern or None, clone=clone, name=name,
                      color=None if color is None else color_value(color))

def set_playlist_track(track: str, name: str | None = None, color: str | None = None, muted: bool | None = None,
                       solo: bool | None = None):
    """
    Change a playlist track; give only what to change. track: number (1 = first), or name or
    its start. color: "#RRGGBB". Returns the changed values as FL now has them (one value bare).
    """
    return set_values("set_playlist_track", dict(name=name, color=color, muted=muted, solo=solo), track=track)

# --- Playlist clips with the mouse (only while the user allows it) --------------
# No API places or deletes clips, so FL's draw tool does it. Positions come from FL,
# not from pictures: the playlist's child windows bound the grid, FL's hint bar names
# the time ("bar:step:tick") or the track under the mouse, and the target track is
# renamed for the moment so its row can be found by name. Before each click the window
# under it must be the playlist, the hint must show the target time and the track
# column the target track.
HINT_TIME = re.compile(r"^(\d+):(\d+):(\d+)$")
ROW_TOKEN = "@mcp-target@"
VK_P = 0x50  # the playlist's draw tool

def hint_ticks(text, ppq, ppb):
    """Ticks from the song start for a hint like "21:12:05" (bar:16th step:tick), else None"""
    match = HINT_TIME.match(text)
    return None if match is None else (int(match[1]) - 1) * ppb + (int(match[2]) - 1) * ppq // 4 + int(match[3])

def playlist_layout():
    """(the playlist form, its grid rect (left, top, right, bottom) with the ruler, the x of the
    track names column), from FL's child windows: a scroller spans the grid's width above it
    and a scroll bar runs down its right side"""
    main = fl_main_window()[0]
    form = next((hwnd for hwnd, cls, title, _, visible in child_windows(main)
                 if cls == "TEventEditForm" and title.startswith("Playlist") and visible), None)
    if form is None:
        raise FLStudioError("FL Studio's playlist isn't showing.")
    inner = [(cls, rect) for _, cls, _, rect, visible in child_windows(form) if visible]
    scroller = next((rect for cls, rect in inner if cls == "TQuickScroller"), None)
    scroll_bars = sorted((rect for cls, rect in inner if cls == "TNumBar"), key=lambda rect: rect[0])
    if scroller is None or not scroll_bars:
        raise FLStudioError("Couldn't find the playlist's grid (FL Studio's layout differs from what's expected).")
    left, right = scroller[0], scroller[2]
    # With the picker panel showing, its scroll bar is the first one, left of the track names
    names_x = (scroll_bars[0][2] + left) // 2 if len(scroll_bars) > 1 else left - 100
    return form, (left, scroller[3], right, scroll_bars[-1][3]), names_x

class PlaylistMouse:
    """FL in front, the playlist scrolled to a bar and track, the target track renamed to
    ROW_TOKEN, covering FL windows hidden as needed; everything is put back on exit"""

    def __init__(self, track, bar, pattern=None):
        self.track, self.bar, self.pattern = track, bar, pattern
        self.warnings = []

    def __enter__(self):
        require("mouse", "focus")
        target = fl_request("set_playlist_track", track=self.track)
        self.track, self.name = target["track"], target["name"]
        self.exits = contextlib.ExitStack()
        try:
            self.exits.enter_context(fl_in_front(self.warnings))
            self.exits.enter_context(cursor_kept())
            # The target bar a bar in from the left edge, the target track at the top
            view = fl_request("playlist_view", bar=max(1, int(self.bar) - 1), track=self.track, pattern=self.pattern)
            self.exits.callback(fl_request, "playlist_view", bar=view["vis_bar"], pattern=view["pattern"])
            fl_request("set_playlist_track", track=self.track, name=ROW_TOKEN)
            self.exits.callback(self.restore_name)
            self.ppq, self.ppb = view["ppq"], view["ppb"]
            self.form, self.grid, self.names_x = playlist_layout()
            self.clear = self.exits.enter_context(uncovered(self.form))
            # A hint left from before (this track's name, say) would fool row(): show a fresh one
            self.hint(self.grid[0] + 4, self.grid[1] + 8)
        except BaseException:
            self.exits.close()
            raise
        return self

    def __exit__(self, *exc):
        self.exits.close()

    def restore_name(self):
        """The target track's own name again (rows can't be found by name after this)"""
        default = self.name == f"Track {self.track}"
        fl_request("set_playlist_track", track=self.track, name="" if default else self.name)

    def hint(self, x, y):
        self.clear(x, y)
        hover(x, y)
        return fl_request("hint")["hint"].strip()

    def stable_hint(self, x, y):
        """FL's hint at (x, y) once two reads agree (it can lag a mouse move)"""
        text = self.hint(x, y)
        for _ in range(4):
            again = fl_request("hint")["hint"].strip()
            if again == text:
                break
            text = again
        return text

    def x_of(self, ticks):
        """The x of a time in the grid, from two times read on the ruler (once)"""
        if not hasattr(self, "x_ticks"):
            left, top, right, _ = self.grid
            xs = (left + (right - left) * 0.1, right - (right - left) * 0.1)
            read = [hint_ticks(self.hint(x, top + 8), self.ppq, self.ppb) for x in xs]
            if None in read or read[0] == read[1]:
                raise FLStudioError("Couldn't read the playlist's time scale from FL's hint bar.")
            self.x_ticks = (xs[0], read[0], (xs[1] - xs[0]) / (read[1] - read[0]))
        x0, t0, per_tick = self.x_ticks
        return x0 + (ticks - t0) * per_tick

    def row(self):
        """(top, bottom) y of the target track's row: scan the track names column for the
        token, then bisect. The track gets its name back at once: FL's undo entries for clip
        changes keep the track names they saw, so undoing one later would bring the token back."""
        if not hasattr(self, "row_y"):
            try:
                self.row_y = self.find_row()
            finally:
                self.restore_name()
        return self.row_y

    def find_row(self):
        _, top, _, bottom = self.grid
        is_target = lambda y: self.hint(self.names_x, y) == ROW_TOKEN
        y = top
        while y < bottom and not is_target(y):
            y += 8
        if y >= bottom:
            raise FLStudioError(f"Couldn't find playlist track {self.track}'s row.")

        def edge(inside, outside):
            while abs(inside - outside) > 1:
                middle = (inside + outside) // 2
                inside, outside = (middle, outside) if is_target(middle) else (inside, middle)
            return inside
        row_top = edge(y, y - 8)
        below = y + 16
        while below < bottom and is_target(below):
            below += 16
        row_bottom = edge(below - 16, min(below, bottom))
        if row_bottom - row_top < 10:
            raise FLStudioError(f"Couldn't measure playlist track {self.track}'s row ({row_top}-{row_bottom}).")
        return row_top, row_bottom

    def row_middle(self):
        row_top, row_bottom = self.row()
        return (row_top + row_bottom) / 2

    def checked(self, x, y):
        """FL's hint at (x, y), after checking the spot is in the target track's row"""
        if not self.grid[0] <= x < self.grid[2]:
            raise FLStudioError("That time isn't visible in the playlist; nothing clicked.")
        if self.hint(self.names_x, y) != (ROW_TOKEN if not hasattr(self, "row_y") else self.name):
            raise FLStudioError("The playlist's track rows moved; nothing clicked.")
        return self.stable_hint(x, y)

    def clip_at(self, x, y):
        """(start, length) in ticks of the clip FL's hint names at (x, y), or None. Over a clip
        the hint is "start for length", e.g. "3:06:00 for 2:00:00" (a length counts from 0)."""
        text = self.checked(x, y)
        if " for " not in text:
            return None
        start, length = text.split(" for ", 1)
        bars, steps, ticks = (int(n) for n in length.split(":"))
        return hint_ticks(start, self.ppq, self.ppb), bars * self.ppb + steps * self.ppq // 4 + ticks

def clip_ticks(bar, ppq, ppb):
    """Ticks from the song start of a 1-based bar given in whole beats"""
    beats = (bar - 1) * ppb / ppq
    if bar < 1 or abs(beats - round(beats)) > 1e-6:
        raise ValueError("bar must be 1 or more, in whole beats (e.g. 3, or 3.25 = bar 3 beat 2 in 4/4)")
    return round(beats) * ppq

def tick_text(ticks, ppq, ppb):
    bars, rest = divmod(ticks, ppb)
    return f"{bars + 1}:{rest // (ppq // 4) + 1:02d}:{rest % (ppq // 4):02d}"

def clip_text(clip, ppq, ppb):
    return f"{tick_text(clip[0], ppq, ppb)} for {clip[1] / ppb:g} bar{'' if clip[1] == ppb else 's'}"

def place_pattern(pattern: str, bar: float, track: str = "1"):
    """
    Place a clip of a pattern in the playlist with FL's draw tool (mouse: only while the user
    allows it). pattern: number, or name or its start. bar: 1 = song start, in whole beats
    (3.25 = bar 3 beat 2 in 4/4). track: playlist track number or name; the spot must be free.
    Returns FL's "start for length" (bar:step:tick). FL comes to the front; FL windows over the
    playlist hide for a moment; selects the draw tool.
    """
    timing = fl_request("playlist_view")
    ticks = clip_ticks(bar, timing["ppq"], timing["ppb"])
    half_step = timing["ppq"] // 8
    with PlaylistMouse(track, bar, pattern=pattern) as view:
        y = view.row_middle()
        # The draw tool starts the clip at the 16th step nearest the click: click where the
        # hint shows the target time (within half a step), on empty grid
        x0, spot = round(view.x_of(ticks)), None
        for x in sorted(range(max(x0 - 4, view.grid[0]), x0 + 5), key=lambda x: abs(x - x0)):
            shown = hint_ticks(view.checked(x, y), view.ppq, view.ppb)
            if shown is not None and abs(shown - ticks) < half_step:
                spot = x
                break
        if spot is None:
            raise FLStudioError(f"Track {view.track} has a clip at {tick_text(ticks, view.ppq, view.ppb)} "
                                "or the time couldn't be found there; nothing placed.")
        press_keys(VK_P)
        time.sleep(0.1)
        hover(spot, y)
        click(spot, y)
        time.sleep(0.2)
        view.hint(spot, view.grid[1] + 8)  # off the clip, so the hint changes
        placed = view.clip_at(spot + 2, y)
        if placed is None or placed[0] != ticks:
            if placed is not None:
                move_history(1, undo=True)
            raise FLStudioError(f"The clip didn't land at {tick_text(ticks, view.ppq, view.ppb)} "
                                f"({'none placed' if placed is None else 'undone'}).")
    return with_warnings(clip_text(placed, view.ppq, view.ppb), view.warnings)

def delete_clip(bar: float, track: str):
    """
    Delete the playlist clip covering a time on a track (mouse: only while the user allows it):
    right-click with FL's draw tool. bar: a time inside the clip, as place_pattern. track:
    number or name. Returns the deleted clip's "start for length".
    """
    timing = fl_request("playlist_view")
    ticks = clip_ticks(bar, timing["ppq"], timing["ppb"])
    with PlaylistMouse(track, bar) as view:
        y = view.row_middle()
        x = view.x_of(ticks + timing["ppq"] // 8)
        clip = view.clip_at(x, y)
        if clip is None or not clip[0] <= ticks < clip[0] + clip[1]:
            raise FLStudioError(f"No clip covers {tick_text(ticks, view.ppq, view.ppb)} on track {view.track}; "
                                "nothing deleted.")
        press_keys(VK_P)
        time.sleep(0.1)
        hover(x, y)
        click(x, y, "right")
        time.sleep(0.2)
        view.hint(x, view.grid[1] + 8)
        if view.clip_at(x, y) == clip:
            raise FLStudioError("The clip is still there after the right-click.")
    return with_warnings(clip_text(clip, view.ppq, view.ppb), view.warnings)

# No API lists, adds or renames arrangements. The playlist caption's "Current Arrangement"
# button opens a menu: a header, the arrangements, a header, Clone, Rename, Delete, Merge
# with, a separator, Add one. Its underlined letters work. It opens with the current
# arrangement highlighted, so the rows' colors give the count and the current one; the
# Rename box's title names the current one; FPT_ArrangementJog switches to the next or
# previous one (wrapping) and FL's hint then names it.
ARRANGEMENT_BUTTON = "Current Arrangement"
MENU_ROW = 18  # menu row height at 100% scaling (22 px at 125%)
VK_A, VK_C, VK_D = 0x41, 0x43, 0x44
RENAME_TITLE = re.compile(r"Rename (.*) as\.\.\.", re.S)
ARRANGEMENT_ACTIONS = {"": "list", "l": "list", "a": "add", "c": "clone", "r": "rename", "d": "delete",
                       "del": "delete", "s": "switch", "go": "switch"}
arrangement_button_x = None  # where the button was last found

def close_color(a, b):
    return max(abs(x - y) for x, y in zip(a, b)) <= 12

def color_runs(colors):
    """[start, end, color] runs of similar colors, from top to bottom"""
    runs = []
    for y, color in enumerate(colors):
        if runs and close_color(runs[-1][2], color):
            runs[-1][1] = y + 1
        else:
            runs.append([y, y + 1, color])
    return runs

class ArrangementMenu:
    """FL in front with the playlist showing; the mouse, FL windows over the playlist's
    caption and any menu or box left open are put back on exit"""

    def __enter__(self):
        require("mouse", "focus")
        self.warnings = []
        self.exits = contextlib.ExitStack()
        try:
            self.exits.enter_context(fl_in_front(self.warnings))
            self.exits.enter_context(cursor_kept())
            fl_request("playlist_view")  # shows and focuses the playlist
            form = playlist_layout()[0]
            self.caption = next(rect for _, cls, _, rect, visible in child_windows(form)
                                if cls == "TNewCaption" and visible)
            self.clear = self.exits.enter_context(uncovered(form))
            self.exits.callback(self.close_popups)
        except BaseException:
            self.exits.close()
            raise
        return self

    def __exit__(self, *exc):
        self.exits.close()

    def close_popups(self):
        for _ in range(3):
            if not any(fl_top_window(cls) for cls in ("TQuickPopupMenuWindow", "TNameEditForm", "TMsgForm")):
                return
            press_keys(VK_ESCAPE)
            time.sleep(0.2)

    def hint(self, x, y):
        self.clear(x, y)
        hover(x, y)
        return fl_request("hint")["hint"]

    def button(self):
        """(x, y) of the Current Arrangement button, found by FL's hint along the caption"""
        global arrangement_button_x
        left, top, right, bottom = self.caption
        y = (top + bottom) // 2
        self.hint(left + 4, y)  # elsewhere first: the hint may still show a message
        if arrangement_button_x is not None and self.hint(arrangement_button_x, y) == ARRANGEMENT_BUTTON:
            return arrangement_button_x, y
        x, seen = left + 4, []  # the button is ~110 px wide at 125%
        while x < right and self.hint(x, y) != ARRANGEMENT_BUTTON:
            seen.append(fl_request("hint")["hint"])
            x += 20
        if x >= right:
            raise FLStudioError(f"Couldn't find the playlist's Current Arrangement button (caption {self.caption}, "
                                f"hints {list(dict.fromkeys(seen))[:12]}).")
        end = x
        while end + 20 < right and self.hint(end + 20, y) == ARRANGEMENT_BUTTON:
            end += 20
        arrangement_button_x = (x + end) // 2
        return arrangement_button_x, y

    def open(self):
        """Open the menu; returns (how many arrangements, the current one, 1-based)"""
        click(*self.button())
        popup = wait_for(lambda: fl_top_window("TQuickPopupMenuWindow"), bool, 1.5)
        if not popup:
            raise FLStudioError("The Current Arrangement menu didn't open.")
        time.sleep(0.1)  # drawn
        left, top, right, bottom = window_rect(popup)
        scale = window_dpi(popup) / 96
        runs = [run for run in color_runs(screen_colors(left + round(3 * scale), range(top, bottom)))
                if run[1] - run[0] >= 10 * scale]
        # The headers share a color: the arrangements' rows lie between the first two
        headers = [run for run in runs if close_color(run[2], runs[0][2])]
        if len(headers) < 2:
            raise FLStudioError("Couldn't read the Current Arrangement menu (its layout differs).")
        first, last = top + headers[0][1], top + headers[1][0]
        count = round((last - first) / (MENU_ROW * scale))
        if count < 1 or abs(last - first - count * MENU_ROW * scale) > count + 1:
            raise FLStudioError("Couldn't read the Current Arrangement menu (its rows differ).")
        row = (last - first) / count
        # The row below the second header (Clone) isn't highlighted
        rows = screen_colors(left + round(3 * scale), [first + (i + 0.5) * row for i in range(count)]
                             + [top + headers[1][1] + row / 2])
        lit = [i + 1 for i, color in enumerate(rows[:-1]) if not close_color(color, rows[-1])]
        if len(lit) != 1:
            raise FLStudioError("Couldn't tell the current arrangement in the menu.")
        return count, lit[0]

    def box(self, vk, cls):
        """Press a menu letter and wait for the box it opens"""
        press_keys(vk)
        form = wait_for(lambda: fl_top_window(cls), bool, 1.5)
        if not form:
            raise FLStudioError("FL's box for that didn't open.")
        return form

    def enter_name(self, vk, name):
        self.box(vk, "TNameEditForm")
        type_text(name)
        time.sleep(0.1)
        press_keys(VK_RETURN)
        if wait_for(lambda: fl_top_window("TNameEditForm"), lambda f: not f, 1.5):
            raise FLStudioError("FL's name box didn't close.")

    def state(self):
        """{"current": name, "index": its place in the menu, "count": arrangements}"""
        count, current = self.open()
        title = window_title(self.box(VK_R, "TNameEditForm"))
        self.close_popups()
        match = RENAME_TITLE.fullmatch(title)
        if match is None:
            raise FLStudioError(f"Unexpected rename box title {title!r}.")
        return {"current": match[1], "index": current, "count": count}

def jog_to(index, state):
    """Switch from state's current arrangement to the index-th, the shorter way round;
    returns its name"""
    ahead = (index - state["index"]) % state["count"]
    step, steps = (1, ahead) if ahead <= state["count"] / 2 else (-1, state["count"] - ahead)
    name = state["current"]
    for _ in range(steps):
        name = fl_request("arrangement_jog", step=step)
        if name is None:
            raise FLStudioError("FL didn't switch arrangements.")
    return name

def arrangement_names(state):
    """Every arrangement's name in menu order: one switch through all, back to the current"""
    names = [None] * state["count"]
    names[state["index"] - 1] = state["current"]
    for k in range(1, state["count"] + 1):
        name = fl_request("arrangement_jog", step=1)
        i = (state["index"] - 1 + k) % state["count"]
        if name is None or (names[i] is not None and name != names[i]):
            raise FLStudioError("FL's arrangements changed while reading them.")
        names[i] = name
    return names

def arrangement(action: str = "", name: str = ""):
    """
    Playlist arrangements (mouse: only while the user allows it). action: list(l, default),
    add(a), clone(c) (copies the current one), rename(r) (the current one), delete(d) (the
    current one), switch(s). name: the new name (add, clone, rename), or the one to switch to
    (number = place in FL's list, or name). New and cloned ones become current. Returns
    {"names": [...], "current": number} for list, else {"current": name, "index": number,
    "count": number}. FL comes to the front and the mouse opens the playlist's menu.
    """
    act = ARRANGEMENT_ACTIONS.get(action.lower().strip(), action.lower().strip())
    if act not in ARRANGEMENT_ACTIONS.values():
        raise ValueError("action: list(l) add(a) clone(c) rename(r) delete(d) switch(s)")
    if act in ("add", "clone", "rename", "switch") and not name.strip():
        raise ValueError(f"{act} needs a name")
    with ArrangementMenu() as menu:
        if act in ("list", "switch"):
            state = menu.state()
            if act == "list":
                return with_warnings({"names": arrangement_names(state), "current": state["index"]}, menu.warnings)
            if name.strip().isdigit():
                index = int(name)
                if not 1 <= index <= state["count"]:
                    raise ValueError(f"There are {state['count']} arrangements.")
            else:
                names = arrangement_names(state)
                found = [i + 1 for i, n in enumerate(names) if n == name] or \
                        [i + 1 for i, n in enumerate(names) if n.lower().startswith(name.lower())]
                if len(found) != 1:
                    raise ValueError(f"{'Several' if found else 'No'} arrangements match {name!r}: {names}")
                index = found[0]
            return with_warnings({"current": jog_to(index, state), "index": index, "count": state["count"]},
                                 menu.warnings)
        count, _ = menu.open()
        if act == "delete":
            if count == 1:
                raise FLStudioError("It's the only arrangement; nothing deleted.")
            menu.box(VK_D, "TMsgForm")
            press_keys(VK_RETURN)  # Ok is the default button
            if wait_for(lambda: fl_top_window("TMsgForm"), lambda f: not f, 1.5):
                raise FLStudioError("FL's Confirm box didn't close.")
        else:
            menu.enter_name({"add": VK_A, "clone": VK_C, "rename": VK_R}[act], name)
        time.sleep(0.2)
        state = menu.state()
    expected = count - 1 if act == "delete" else count if act == "rename" else count + 1
    if state["count"] != expected:
        raise FLStudioError(f"FL has {state['count']} arrangements afterwards, expected {expected}.")
    if act != "delete" and state["current"] != name:
        menu.warnings.append(f"FL shows the name as {state['current']!r}")
    return with_warnings(state, menu.warnings)

def record_automation(param: str, points: list[list], channel: str = "", track: str = "", slot: int | None = None):
    """
    Record automation by playing FL with recording on and moving the control in real time
    (accurate to a few ms; plays sound). It lands where FL records automation: the active
    pattern's events (pattern mode) or FL's record settings' choice.
    What: param of a channel's plugin or a mixer track's effect (slot 0-9; "mix" = its mix
    level), or a mixer track's (no slot) "volume", "pan" or "sep".
    points: [[beat, value], ...], linear between; value 0-1, or display text for plugin params.
    Needs "Automation" ticked in FL's record filter (right-click the record button).
    """
    if bool(channel) == bool(track):
        raise ValueError("Give either channel, or track (and slot)")
    args = {"channel": channel} if channel else {"track": track, "slot": slot}
    if track and slot is None:
        param = {"v": "volume", "vol": "volume", "p": "pan"}.get(param.lower(), param.lower())
    elif param != "mix":
        points = [[beat, resolve_value(value, param, args)] for beat, value in points]
    if not points:
        raise ValueError("Give at least one point")
    beats = max(p[0] for p in points) - min(p[0] for p in points)
    seconds = beats * 60 / max(get_tempo(), 10)
    start = fl_request("automation_start", param=param, points=points, **args)
    deadline = time.time() + seconds + 10
    sent, done = 0, False
    try:
        while not done and time.time() < deadline:
            done = fl_request("automation_tick")["done"]
            sent += 1
            time.sleep(0.005)
    finally:
        if not done:
            fl_request("stop")
            fl_request("record", on=False)
    if not done:
        raise FLStudioError("Recording didn't reach the last point in time; FL was stopped.")
    return dict(start, values=sent)

# No API makes automation clips; FL's Tools > Last tweaked > Create automation clip does, for
# the control last changed (API setters count). The clip spans the time selection, goes on the
# first playlist track free over it, is flat with a point at each end, and becomes a channel
# named after the control ("Vital - Macro 1", then "... #2") and the current clip source, so
# Shift+C selects it and Shift+Down moves it down a track. Over an automation clip FL's hint
# names the point under the mouse ("Point 2/3: 4:09:00"); Shift+right-click adds a point at the
# nearest snap step without changing the level; a point's right-click menu sets its value (Type
# in value..., the last item) and the curve that ends at it (Home lands on Delete, then CURVES).
CURVES = ["smooth", "hold", "single curve", "single curve 2", "single curve 3", "double curve", "double curve 2",
          "double curve 3", "half sine", "stairs", "smooth stairs", "pulse", "wave"]
CURVE_ALIASES = {"linear": None, "lin": None, "l": None, "step": "hold", "h": "hold", "s": "smooth",
                 "sine": "half sine", "single curve 1": "single curve", "double curve 1": "double curve"}
TOOLS_MENU_X = 0.858  # the middle of TOOLS across FL's main menu bar
TOOLS_MENU_SIZE = (445, 326)  # the Tools menu (two columns) at 100% scaling
LAST_TWEAKED_ROW = 27  # the middle of its "Last tweaked" row, above its bottom (100%)
CREATE_CLIP_ROW = 134  # rows' middles below the Last tweaked submenu's top (100%)
COPY_VALUE_ROW = 235
SNAP_STEP = 8  # midi.Snap_* for a 16th step

def popups():
    return fl_top_windows("TQuickPopupMenuWindow")

UNDO_MARKER = "MCP automation clip"  # a named undo point before the tool's first change

def undo_through(marker, limit=100):
    """Undo back through a named undo point (request save_undo); returns the steps taken.
    FL's history holds 100 entries, so its length can't count steps; its hint names each
    undone one ("Undone: create automation clip")."""
    for steps in range(1, limit + 1):
        move_history(1, undo=True)
        if fl_request("hint")["hint"] == f"Undone: {marker}":
            return steps
    raise FLStudioError(f"Couldn't find {marker!r} in FL's undo history.")

def close_boxes():
    """Close FL popup menus and input boxes left open (FL refuses API calls while one is)"""
    for _ in range(4):
        if not (popups() or fl_top_window("TNameEditForm")):
            return
        press_keys(VK_ESCAPE)
        time.sleep(0.2)

def value_text(value):
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"

def last_tweaked_item(row):
    """Click an item of Tools > Last tweaked in FL's main menu (FL in front); row: CREATE_CLIP_ROW
    or COPY_VALUE_ROW"""
    bar = next((rect for _, cls, _, rect, visible in child_windows(fl_main_window()[0])
                if cls == "TNewMenu" and visible), None)
    if bar is None:
        raise FLStudioError("Couldn't find FL Studio's main menu.")
    left, top, right, bottom = bar
    click(left + (right - left) * TOOLS_MENU_X, (top + bottom) / 2)
    opened = wait_for(popups, bool, 1.5)
    if len(opened) != 1:
        raise FLStudioError("FL's Tools menu didn't open.")
    tools = opened[0]
    left, top, right, bottom = window_rect(tools)
    scale = window_dpi(tools) / 96
    size = ((right - left) / scale, (bottom - top) / scale)
    if any(abs(a - b) > 40 for a, b in zip(size, TOOLS_MENU_SIZE)):
        raise FLStudioError(f"The menu that opened isn't FL's Tools menu as expected ({size[0]:.0f}x{size[1]:.0f}).")
    hover(left + 60 * scale, bottom - LAST_TWEAKED_ROW * scale)
    sub = wait_for(lambda: [p for p in popups() if p != tools], bool, 1.5)
    if len(sub) != 1:
        raise FLStudioError("FL's Last tweaked menu didn't open.")
    left, top, _, _ = window_rect(sub[0])
    click(left + 60 * scale, top + row * scale)
    if wait_for(popups, lambda p: not p, 1.5):
        raise FLStudioError("FL's Last tweaked menu stayed open.")

def insert_playlist_track(after, bar):
    """Insert an empty playlist track after one (Insert one, in the next track's menu, adds a
    track above that one); returns the new track's number"""
    with PlaylistMouse(after, bar) as view:
        top, bottom = view.row()
        below = view.track + 1
        name = fl_request("set_playlist_track", track=below)["name"]
        y = bottom + (bottom - top) // 2
        if view.stable_hint(view.names_x, y) != name:
            raise FLStudioError(f"Couldn't find playlist track {below}'s name below track {view.track}.")
        click(view.names_x, y, "right")
        if not wait_for(popups, bool, 1.5):
            raise FLStudioError("The playlist track's menu didn't open.")
        for vk in (VK_END, VK_UP, VK_UP, VK_UP, VK_UP, VK_RETURN):  # Move down, ..., Insert one
            press_keys(vk)
            time.sleep(0.05)
        wait_for(popups, lambda p: not p, 1.5)
    # The insert is the one undo entry after the marker
    steps = undo_through(UNDO_MARKER)
    move_history(steps, undo=False)
    if steps != 2:
        raise FLStudioError("FL didn't insert a playlist track.")
    return below

def automation_clip(param: str, points: list[list], channel: str = "", track: str = "", slot: int | None = None,
                    to: str = "", after: str = "", end: float | None = None, name: str = ""):
    """
    Make a playlist automation clip (FL's Tools > Last tweaked > Create automation clip, then its
    points in the playlist; mouse: only while the user allows it).
    What: as record_automation: param of a channel's plugin or a mixer track's effect (slot 0-9;
    "mix" = its mix level), or a mixer track's (no slot) "volume", "pan" or "sep".
    points: [[bar, value, curve], ...] in time order. bar: as place_pattern, on 16th steps (3.25 =
    bar 3 beat 2 in 4/4). value: 0-1 (pan/sep 0.5 = center), or display text for plugin params.
    curve (optional): how it goes on to the next point: linear (default), hold (step), smooth,
    single curve 2/3, double curve (2/3), half sine, stairs, smooth stairs, pulse, wave.
    to: playlist track (number or name) for the clip; the time range must be free there.
    after: instead, insert a new track after this one for it. end: the clip's end bar (default:
    the last point). name: rename the clip. Returns {"clip": name, "track": number, "undo": steps
    that undo it all}. FL comes to the front; FL windows over the playlist hide for a moment; the
    parameter is left at the first value. On an error everything is undone.
    """
    if bool(to) == bool(after):
        raise ValueError("Give either to (the playlist track for the clip) or after (a new track goes after it)")
    if bool(channel) == bool(track):
        raise ValueError("Give either channel, or track (and slot)")
    args = {"channel": channel} if channel else {"track": track, "slot": slot}
    if track and slot is None:
        param = {"v": "volume", "vol": "volume", "p": "pan"}.get(param.lower(), param.lower())
    plugin_param = not (track and slot is None) and param != "mix"
    timing = fl_request("playlist_view")
    ppq, ppb = timing["ppq"], timing["ppb"]
    step = ppq // 4

    def bar_ticks(bar):
        ticks = (float(bar) - 1) * ppb / step
        if bar < 1 or abs(ticks - round(ticks)) > 1e-6:
            raise ValueError(f"Bar {bar} isn't on a 16th step (e.g. 3.25 = bar 3 beat 2 in 4/4)")
        return round(ticks) * step

    clip = []  # [ticks, 0-1 value, curve on to the next point (None: linear)]
    for point in points:
        if not 2 <= len(point) <= 3:
            raise ValueError("Each point is [bar, value] or [bar, value, curve]")
        value = resolve_value(point[1], param, args) if plugin_param else float(point[1])
        if not 0 <= value <= 1:
            raise ValueError("A value must be 0-1")
        curve = str(point[2]).strip().lower() if len(point) > 2 else "linear"
        curve = CURVE_ALIASES.get(curve, curve)
        if curve is not None and curve not in CURVES:
            raise ValueError(f"curve is one of linear, {', '.join(CURVES)}")
        clip.append([bar_ticks(point[0]), value, curve])
    if not clip:
        raise ValueError("Give at least one point")
    if any(b[0] <= a[0] for a, b in zip(clip, clip[1:])):
        raise ValueError("Points must be in time order, at different times")
    start = clip[0][0]
    end_ticks = clip[-1][0] if end is None else bar_ticks(end)
    if end_ticks <= start or end_ticks < clip[-1][0]:
        raise ValueError("The clip needs an end after its first point and not before its last (end, or a 2nd point)")
    if end_ticks > clip[-1][0]:
        clip.append([end_ticks, clip[-1][1], None])
    clip[-1][2] = None
    start_bar = start / ppb + 1

    names_before = collections.Counter(c["name"] for c in fl_request("get_channel_names"))
    selection = fl_request("song_state")["selection_beats"]
    tweaked = None
    fl_request("save_undo", name=UNDO_MARKER)
    try:
        target = insert_playlist_track(after, start_bar) if after else to
        with PlaylistMouse(target, start_bar) as view:
            scale = window_dpi(view.form) / 96
            top, bottom = view.row()
            head_y = top + round(3 * scale)

            def x_at(ticks):
                """x of a time, scrolling the playlist when it's out of view"""
                x = view.x_of(ticks)
                if not view.grid[0] + 2 <= x <= view.grid[2] - 2:
                    fl_request("playlist_view", bar=max(1, ticks // ppb))
                    del view.x_ticks
                    x = view.x_of(ticks)
                return round(x)

            head_x = x_at(start + min(end_ticks - start, ppq) // 2)
            if to:  # over any clip's header the hint is "start for length", over the grid a time
                spacing = max(step, (end_ticks - start) // 64)
                for ticks in range(start, end_ticks, spacing):
                    shown = view.stable_hint(x_at(ticks + step // 2), head_y)
                    if hint_ticks(shown, ppq, ppb) is None:
                        raise FLStudioError(f"Track {view.track} isn't free at {tick_text(ticks, ppq, ppb)} "
                                            f"(FL's hint: {shown!r}); nothing made.")
            snap = fl_request("playlist_view", snap=SNAP_STEP)["snap"]
            view.exits.callback(fl_request, "playlist_view", snap=snap)
            view.exits.callback(close_boxes)  # runs first on the way out, with FL still in front

            # Create it: the control as last tweaked, the time selection as its range
            tweaked = fl_request("tweak", param=param, value=clip[0][1], **args)
            fl_request("time_selection", start=start / ppq, end=end_ticks / ppq)
            try:
                last_tweaked_item(CREATE_CLIP_ROW)
            finally:
                fl_request("time_selection", **({"start": selection[0], "end": selection[1]}
                                                 if selection[1] > selection[0] >= 0 else {}))
            def new_channels():
                now = fl_request("get_channel_names")
                added = collections.Counter(c["name"] for c in now) - names_before
                return [c for c in now if added[c["name"]]]
            made = wait_for(new_channels, bool, 3)
            if len(made) != 1:
                raise FLStudioError("FL made no automation clip.")
            created = made[0]["name"]
            if tweaked["name"] and tweaked["name"].lower() not in created.lower():
                raise FLStudioError(f"FL made the clip {created!r} for another control than {tweaked['name']!r}.")

            # Bring it down to the track: it went on the first track free over its range
            fl_request("playlist_view")  # the playlist takes the keys
            view.__dict__.pop("x_ticks", None)  # in case the view moved
            head_x = x_at(start + min(end_ticks - start, ppq) // 2)
            press_keys(VK_SHIFT, VK_C)
            time.sleep(0.15)
            length = end_ticks - start
            for _ in range(view.track):
                if view.clip_at(head_x, head_y) == (start, length):
                    break
                press_keys(VK_SHIFT, VK_DOWN)
                time.sleep(0.15)
            else:
                raise FLStudioError(f"Couldn't bring the new clip to track {view.track}.")

            # The body under the clip's header: value 1 at its top, 0 at its bottom
            inside, outside = head_y, min(top + round(40 * scale), bottom - 2)
            while outside - inside > 1:
                middle = (inside + outside) // 2
                inside, outside = (middle, outside) if " for " in view.stable_hint(head_x, middle) else (inside, middle)
            body_top, body_bottom = outside + 0.5, bottom - 0.5
            y_of = lambda value: round(body_bottom - value * (body_bottom - body_top))
            point_hint = lambda k, n, ticks: f"Point {k}/{n}: {tick_text(ticks, ppq, ppb)}"

            def on_point(shown, expected):
                """Over a point FL's hint is "Point k/n: time", or for controls with a value text
                (mixer volume: "0.0dB") that text; elsewhere a time or a tension handle"""
                if shown.startswith("Point "):
                    return shown == expected
                return bool(shown) and hint_ticks(shown, ppq, ppb) is None and " for " not in shown

            def point_menu(x, y, expected):
                shown = view.stable_hint(x, y)
                if not on_point(shown, expected):
                    raise FLStudioError(f"Expected FL's hint {expected!r} at a point, got {shown!r}.")
                click(x, y, "right")
                if not wait_for(popups, bool, 1.5):
                    raise FLStudioError("The point's menu didn't open.")
                time.sleep(0.1)

            # Add the points between the ends, while the line is still flat. A segment's tension
            # handle sits on the line halfway along it, where an evenly spaced point would go, so
            # click off the line: the point still lands on it
            level = tweaked["after"]  # FL's own value (a stepped parameter rounds)
            n = 2
            for k, (ticks, _, _) in enumerate(clip[1:-1], start=2):
                x, y = x_at(ticks), y_of(level)
                off = y_of(level - 0.35 if level > 0.5 else level + 0.35)
                view.clear(x, off)
                hover(x, off)
                click(x, off, "right", held=(VK_SHIFT,))
                n += 1
                shown = view.stable_hint(x, y)
                if not on_point(shown, point_hint(k, n, ticks)):
                    raise FLStudioError(f"Adding a point at {tick_text(ticks, ppq, ppb)}: FL's hint shows {shown!r}.")

            # Their values. Type in value... (the menu's last item) opens a box that takes 0-1 when
            # FL's hint names points "Point k/n", but the control's units when it shows a value
            # there (mixer volume: dB): then set the control, Copy value (Last tweaked menu) and
            # Paste value (the point menu's item above Type in value)
            typed = view.stable_hint(x_at(start), y_of(level)).startswith("Point ")
            copied, pasted = None, set()  # Paste value also makes a point's curve hold
            for k, (ticks, value, _) in enumerate(clip, start=1):
                if abs(value - level) < 1e-6:
                    continue
                x = x_at(ticks)
                if typed:
                    point_menu(x, y_of(level), point_hint(k, n, ticks))
                    front = foreground_class()
                    press_keys(VK_END)
                    press_keys(VK_RETURN)
                    if wait_for(foreground_class, lambda c: c != front, 1.5) == front:
                        raise FLStudioError("FL's value box didn't open.")
                    type_text(value_text(value))
                    press_keys(VK_RETURN)
                    wait_for(foreground_class, lambda c: c == front, 1.5)
                else:
                    if copied != value:
                        fl_request("tweak", param=param, value=value, **args)
                        last_tweaked_item(COPY_VALUE_ROW)
                        copied = value
                    point_menu(x, y_of(level), point_hint(k, n, ticks))
                    for vk in (VK_END, VK_UP, VK_RETURN):
                        press_keys(vk)
                        time.sleep(0.05)
                    if wait_for(popups, lambda p: not p, 1.5):
                        raise FLStudioError("The point's menu stayed open pasting its value.")
                    pasted.add(k)
                shown = view.stable_hint(x, y_of(value))
                if not on_point(shown, point_hint(k, n, ticks)):
                    raise FLStudioError(f"Point {k} didn't take the value {value_text(value)} (FL's hint: {shown!r}).")

            # Their curves: a point's curve is the segment ending at it
            for k in range(2, n + 1):
                curve = clip[k - 2][2] or ("single curve" if k in pasted else None)  # FL's default
                if curve is None:
                    continue
                ticks, value = clip[k - 1][:2]
                point_menu(x_at(ticks), y_of(value), point_hint(k, n, ticks))
                for vk in [VK_HOME] + [VK_DOWN] * (CURVES.index(curve) + 1) + [VK_RETURN]:
                    press_keys(vk)
                    time.sleep(0.05)
                if wait_for(popups, lambda p: not p, 1.5):
                    raise FLStudioError(f"The point's menu stayed open setting point {k}'s curve.")
                if curve == "hold":  # a held segment has no tension handle halfway
                    middle = (clip[k - 2][0] + ticks) // 2
                    shown = view.stable_hint(x_at(middle), y_of(clip[k - 2][1]))
                    if "tension" in shown:
                        raise FLStudioError(f"Point {k}'s curve didn't change to hold.")
            if copied is not None:
                fl_request("tweak", param=param, value=clip[0][1], **args)  # left at the first value
            placed_on = view.track
    except Exception as error:
        steps = undo_through(UNDO_MARKER)
        if tweaked:
            fl_request("tweak", param=param, value=tweaked["before"], **args)
        if steps > 1:
            raise FLStudioError(f"{error} Undid its {steps - 1} step(s); FL is as before.") from error
        raise
    # Count its undo steps (marker included), then redo them. A redone clip has FL's name again,
    # so rename it after (a rename isn't an undo entry)
    steps = undo_through(UNDO_MARKER)
    move_history(steps, undo=False)
    if name:
        fl_request("set_channel", channel=created, name=name)
        created = name
    return {"clip": created, "track": placed_on, "undo": steps}

def wait_for(read, done, timeout):
    """Poll read() until done(value) or timeout; returns the last value. FL can be too busy
    to answer for a moment (loading a plugin), so request errors are retried until timeout."""
    deadline = time.time() + timeout
    while True:
        try:
            value = read()
            if done(value) or time.time() > deadline:
                return value
        except FLStudioError:
            if time.time() > deadline:
                raise
        time.sleep(0.2)

# Plugins load the way FL's browser loads them. An instrument's .fst from the plugin
# database, opened in FL, becomes a new channel. An effect's .fst opened that way always
# goes to Master, so the browser is walked to it instead (Right opens a folder) and Enter
# loads it into the selected mixer track's next free slot. The browser's "Plugin database"
# is the user's folder only; FL's own folder has the .fst of every native plugin.
PLUGIN_KINDS = {"Generators": "instrument", "Effects": "effect"}
MIXER_SLOTS = 10

def plugin_database_dirs():
    """(the user's plugin database, FL's own)"""
    return (FL_USER_DATA_DIR / "Presets" / "Plugin database",
            fl_install_dir() / "Data" / "Patches" / "Plugin database")

def find_preset(plugin, kind):
    """(path parts below Plugin database, file) of a plugin's .fst; kind "Generators" or
    "Effects". Name exact, else its start, else contained (case-insensitive); the user's
    folder first, then Installed, then the shortest path."""
    user_dir, fl_dir = plugin_database_dirs()
    presets = {}
    for root in (user_dir, fl_dir):
        for folder in (root / kind, root / "Installed" / kind):
            for file in folder.rglob("*.fst"):
                presets.setdefault(file.relative_to(root).parts, file)
    names = {}
    for parts in presets:
        names.setdefault(parts[-1][:-4].lower(), parts[-1][:-4])
    query = plugin.strip().lower()
    matches = ([n for n in names if n == query] or [n for n in names if n.startswith(query)]
               or [n for n in names if query in n])
    if len(matches) != 1:
        what = PLUGIN_KINDS[kind]
        if matches:
            raise ValueError(f"{plugin!r} matches several {what}s: {', '.join(sorted(names[m] for m in matches)[:15])}")
        close = [names[n] for n in difflib.get_close_matches(query, list(names), 5, 0.5)]
        raise ValueError(f"No {what} {plugin!r} in FL's plugin database" + (f". Close: {', '.join(close)}" if close else ""))
    parts = min((p for p in presets if p[-1][:-4].lower() == matches[0]),
                key=lambda p: (not presets[p].is_relative_to(user_dir), p[0] != "Installed", len(p), p))
    return parts, presets[parts]

def browser(move=0, tab=None):
    """Move FL's browser selection; [caption, type] of each item passed (or the current one)"""
    return fl_request("browser", move=move, tab=tab)

def browser_names(parts):
    """Lower-case names of every folder and .fst below Plugin database/parts, as the browser shows them"""
    return {item.name.lower() for item in plugin_database_dirs()[0].joinpath(*parts).rglob("*")
            if item.is_dir() or item.suffix.lower() == ".fst"}

def find_below(name, expected, check_current):
    """Move down FL's browser to the next item called name. expected: the names that may
    pass on the way (None: any), so walking out of the folder is caught."""
    if check_current:
        caption = browser()["items"][0][0]
        if caption.lower() == name.lower():
            return
    for _ in range(40):
        items = browser(move=25)["items"]
        for i, (caption, _) in enumerate(items):
            if caption.lower() == name.lower():
                if i < len(items) - 1:
                    browser(move=i + 1 - len(items))
                return
            if expected is not None and caption.lower() not in expected:
                raise FLStudioError(f"Found {caption!r} in FL Studio's browser while looking for {name!r}.")
        if items[-1] == items[-2]:
            break  # the bottom
    raise FLStudioError(f"Couldn't find {name!r} in FL Studio's browser.")

def browser_keys(*vks):
    """Press keys in FL's browser, only if it has keyboard focus (elsewhere Ctrl+R exports the song)"""
    if browser()["focused"] != "Browser":
        raise FLStudioError("FL Studio's browser didn't get keyboard focus, so no keys were sent.")
    press_keys(*vks)


def walk_browser(path):
    """Select path (folders, then the file) in FL's browser, in the ALL tab, opening folders
    with the Right key. Expects FL in front with the browser focused."""
    browser_keys(VK_CONTROL, VK_UP)  # collapse all folders (this also clears the selection)
    time.sleep(0.2)
    browser(tab="right")  # switching tabs selects the tab's first item
    for _ in range(8):
        if browser(tab="left")["tab"].upper() == "ALL":
            break
    else:
        raise FLStudioError("Couldn't find the ALL tab in FL Studio's browser.")
    if not browser()["items"][0][0]:
        raise FLStudioError("FL Studio's browser has nothing selected to start from.")
    for _ in range(40):  # to the top, where moving up stops
        items = browser(move=-100)["items"]
        if items[-1] == items[-2]:
            break
    else:
        raise FLStudioError("Couldn't reach the top of FL Studio's browser.")
    for depth, name in enumerate(path):
        # An open folder's items follow it; pressing Right on one that's already open moves into it
        find_below(name, browser_names(path[1:depth]) if depth else None, check_current=depth > 0)
        if depth < len(path) - 1:
            browser_keys(VK_RIGHT)
            time.sleep(0.15)

def close_active_effects():
    """Close the focused mixer effect windows with Esc (FL in front): the browser loads into
    the active effect's slot, replacing it, and into the next free slot when there's none"""
    for _ in range(MIXER_SLOTS):
        effect = fl_request("active_effect", focus=True)
        if effect["active"] is None:
            return
        time.sleep(0.3)
        if not fl_request("active_effect")["focused"].startswith(effect["name"]):
            break  # its window didn't get focus: Esc would close something else
        press_keys(VK_ESCAPE)
        time.sleep(0.3)
    raise FLStudioError("Couldn't close the focused effect's window, so the new effect would replace it.")

def assign_free_mixer_track(channel, warnings):
    """FL's Ctrl+L on one channel; returns its new mixer track, or the one it already has alone
    (Ctrl+L would move it to another)"""
    info = fl_request("get_channel", channel=channel, fields=["info"])
    before = info["mixer_track"]
    if before and fl_request("get_mixer_track", track=before)["channels"] == [info["name"]]:
        return before
    with fl_in_front(warnings):
        previous = fl_request("select", channel=channel, focus="channel_rack")
        time.sleep(0.3)
        press_keys(VK_CONTROL, VK_L)
        track = wait_for(lambda: get_channel(channel, ["mt"]), lambda t: t != before, 3.0)
    if previous["channel"] >= 0:
        fl_request("select", channel=previous["channel"])
    if track == before:
        raise FLStudioError(f"Ctrl+L didn't change the channel's mixer track (still {track}).")
    return track

def add_instrument(plugin: str, link: bool = False):
    """
    Add a channel with an instrument from FL's plugin database, as FL's browser does.
    plugin: name or its start, e.g. "vital". link: also route it to a free mixer track (FL's Ctrl+L).
    Returns the new channel's name, or {channel, mixer_track} with link. FL may open the plugin's window.
    """
    _, preset = find_preset(plugin, "Generators")
    before = [c["name"] for c in fl_request("get_channel_names")]
    warnings = []
    with focus_kept(warnings):
        open_in_fl(preset)
        after = wait_for(lambda: [c["name"] for c in fl_request("get_channel_names")],
                         lambda names: len(names) > len(before), 20.0)
    if len(after) <= len(before):
        raise FLStudioError(f"FL Studio didn't add a {preset.stem} channel within 20s.")
    index = next((i for i, (a, b) in enumerate(zip(after, before)) if a != b), len(before))
    if not link:
        return with_warnings(after[index], warnings)
    track = assign_free_mixer_track(str(index), warnings)
    return with_warnings({"channel": after[index], "mixer_track": track}, warnings)

def add_effect(plugin: str, track: str):
    """
    Add an effect from FL's plugin database to a mixer track's next free slot.
    plugin: name or its start, e.g. "fruity filter". track: as get_mixer_track.
    Returns the slot (0-9). FL comes to the front briefly, closes the focused effect's window
    (else the new one would replace it) and leaves its browser at the plugin.
    Only effects in your plugin database (FL's favorites) work.
    """
    parts, _ = find_preset(plugin, "Effects")
    if not plugin_database_dirs()[0].joinpath(*parts).exists():
        # The user's plugin database is also their favorites menu, so nothing is copied into it
        raise ValueError(f"{parts[-1][:-4]} isn't in your plugin database, the only place FL's browser "
                         "shows effects: favorite it in FL (plugin menu > Add to plugin database) first.")
    data = fl_request("get_mixer_track", track=track)
    index, used = data["index"], {e["slot"] for e in data["effects"]}
    if len(used) >= MIXER_SLOTS:
        raise ValueError(f"Mixer track {index} has no free effect slot.")
    warnings = []
    previous = fl_request("select", mixer_track=index)
    try:
        with fl_in_front(warnings):
            close_active_effects()
            fl_request("select", focus="browser")
            time.sleep(0.2)
            # Reread the folders first: FL's browser keeps its folder list, and after some
            # changes (seen after undoing plugin loads) Enter on a file there loads nothing
            browser_keys(VK_CONTROL, VK_R)
            time.sleep(1.5)
            walk_browser(("Plugin database",) + parts)
            browser_keys(VK_RETURN)
            data = wait_for(lambda: fl_request("get_mixer_track", track=index),
                            lambda d: len(d["effects"]) > len(used), 15.0)
    finally:
        fl_request("select", mixer_track=previous["mixer_track"])
    new = [e["slot"] for e in data["effects"] if e["slot"] not in used]
    if not new:
        raise FLStudioError(f"FL Studio didn't add {parts[-1][:-4]} within 15s.")
    return with_warnings(new[0], warnings)

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
    set_permissions, get_mixer_track, get_plugin_params, set_plugin_param, change_preset, set_channel, set_mixer_track,
    set_pattern, set_playlist_track, place_pattern, delete_clip, arrangement, record_automation, automation_clip,
    add_instrument, add_effect, send_melody, send_midi_note,
]

# A tool's description is its docstring, sent whenever the tools load: drop the indentation
for _tool in TOOLS:
    _tool.__doc__ = inspect.cleandoc(_tool.__doc__ or "")
