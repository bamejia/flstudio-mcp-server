# mcp_handlers.py - request handlers for the flstudio MCP server
#
# Imported by device_test.py (the Test Controller script). Kept in a separate
# file so the server can reload it with the "reload_handlers" request (its
# reload_server tool does this) without reopening FL Studio's MIDI Settings.
# Each REQUEST_HANDLERS entry takes the request's args as keyword arguments and
# returns JSON-serializable data.

import math

import channels
import general
import midi
import mixer
import patterns
import plugins
import transport
import ui

# Channel type constants (midi.CT_Sampler, CT_GenPlug, ...) -> readable names
CHANNEL_TYPES = {getattr(midi, n): n[3:] for n in dir(midi) if n.startswith("CT_")}

def finite(value, digits):
    """Round a float for JSON; -inf (e.g. silent volume in dB) becomes None"""
    return round(value, digits) if math.isfinite(value) else None

def used_patterns():
    """Pattern numbers (1-based) that have been changed from the empty default"""
    return [p for p in range(1, patterns.patternMax() + 1) if not patterns.isPatternDefault(p)]

def find_patterns(pattern):
    """Pattern numbers matching a name (case-insensitive) or a pattern number"""
    used = used_patterns()
    if isinstance(pattern, int) or str(pattern).strip().isdigit():
        number = int(pattern)
        if number not in used:
            raise ValueError(f"Pattern {number} doesn't exist or is empty")
        return [number]
    matches = [p for p in used if patterns.getPatternName(p).lower() == str(pattern).lower()]
    if not matches:
        raise ValueError(f"No pattern named {pattern!r}. Patterns: {[patterns.getPatternName(p) for p in used]}")
    return matches

def find_channel(name):
    """Global channel index for a channel name (case-insensitive) or a global index
    (an int, or a string of digits that isn't also a channel's name)"""
    names = [channels.getChannelName(c, True) for c in range(channels.channelCount(True))]
    if not isinstance(name, int):
        for c, channel_name in enumerate(names):
            if channel_name.lower() == str(name).lower():
                return c
        if not str(name).strip().isdigit():
            raise ValueError(f"No channel named {name!r}. Channels: {names}")
    index = int(name)
    if not 0 <= index < len(names):
        raise ValueError(f"Channel index {index} doesn't exist (0-{len(names) - 1})")
    return index

def channel_data(c, fields, step_count):
    """Data for one channel (global index); steps are read from the active pattern"""
    data = {"index": c, "name": channels.getChannelName(c, True)}
    if "info" in fields:
        data["type"] = CHANNEL_TYPES.get(channels.getChannelType(c, True), channels.getChannelType(c, True))
        data["plugin"] = plugins.getPluginName(c, -1, False, True) if plugins.isValid(c, -1, True) else None
        data["mixer_track"] = channels.getTargetFxTrack(c, True)
    if "mixer" in fields:
        data["volume"] = finite(channels.getChannelVolume(c, False, True), 3)
        data["volume_db"] = finite(channels.getChannelVolume(c, True, True), 1)
        data["pan"] = finite(channels.getChannelPan(c, True), 3)
        data["pitch_cents"] = channels.getChannelPitch(c, 1, True)
        data["muted"] = bool(channels.isChannelMuted(c, True))
        data["solo"] = bool(channels.isChannelSolo(c, True))
    if "steps" in fields:
        data["steps_on"] = [s for s in range(step_count) if channels.getGridBit(c, s, True)]
    return data

def read_pattern(p, channel_indexes, fields):
    """Read one pattern. Steps need it to be the active pattern, so jump to it and back."""
    step_count = int(patterns.getPatternLength(p) * 4)  # 4 sixteenth-note steps per beat
    original = patterns.patternNumber()
    if "steps" in fields and p != original:
        patterns.jumpToPattern(p)
    try:
        chans = [channel_data(c, fields, step_count) for c in channel_indexes]
    finally:
        if patterns.patternNumber() != original:
            patterns.jumpToPattern(original)
    if set(fields) == {"steps"} and len(chans) > 1:
        chans = [ch for ch in chans if ch["steps_on"]]  # only steps asked for: skip channels without any
    return {"pattern": p, "name": patterns.getPatternName(p),
            "length_beats": patterns.getPatternLength(p), "channels": chans}

def req_get_pattern_names():
    return [{"pattern": p, "name": patterns.getPatternName(p), "length_beats": patterns.getPatternLength(p)}
            for p in used_patterns()]

def req_get_channel_names():
    return [{"index": c, "name": channels.getChannelName(c, True),
             "plugin": plugins.getPluginName(c, -1, False, True) if plugins.isValid(c, -1, True) else None}
            for c in range(channels.channelCount(True))]

def req_get_pattern(pattern, channel=None, fields=("info", "mixer", "steps")):
    channel_indexes = [find_channel(channel)] if channel else list(range(channels.channelCount(True)))
    return [read_pattern(p, channel_indexes, set(fields)) for p in find_patterns(pattern)]

def req_get_channel(channel, fields=("info", "mixer")):
    """One channel's settings (no pattern needed: steps aren't included)"""
    return channel_data(find_channel(channel), set(fields) - {"steps"}, 0)

def req_get_all_patterns(fields=("info", "mixer", "steps")):
    channel_indexes = list(range(channels.channelCount(True)))
    return [read_pattern(p, channel_indexes, set(fields)) for p in used_patterns()]

def req_get_plugin_params(channel, name_filter="", limit=200):
    c = find_channel(channel)
    if not plugins.isValid(c, -1, True):
        raise ValueError(f"Channel {channel!r} has no plugin (it may be an audio sample)")
    count = plugins.getParamCount(c, -1, True)
    params = []
    for i in range(min(count, 4096)):  # VST indexes 4096+ are MIDI CC/aftertouch mappings, not settings
        name = plugins.getParamName(i, c, -1, True)
        if not name or name_filter.lower() not in name.lower():
            continue
        params.append({"index": i, "name": name,
                       "value": finite(plugins.getParamValue(i, c, -1, True), 4),
                       "display": plugins.getParamValueString(i, c, -1, True).strip() or None})
        if len(params) >= limit:
            break
    return {"channel": channels.getChannelName(c, True), "plugin": plugins.getPluginName(c, -1, False, True),
            "param_count": count, "returned": len(params), "params": params}

def req_prepare_piano_roll(channel, pattern=None):
    """
    Select the pattern and channel whose notes the server is about to export.
    The piano roll itself is opened later by focus_piano_roll, once FL Studio is
    in front. Returns the channel's step positions (the server's cross-check that
    the right channel was exported) and the selection to restore afterwards.
    """
    c = find_channel(channel)
    p = find_patterns(pattern)[0] if pattern not in (None, "") else patterns.patternNumber()
    restore = {"pattern": patterns.patternNumber(), "channel": channels.selectedChannel(False, 0, True)}
    if p != patterns.patternNumber():
        patterns.jumpToPattern(p)
    channels.selectOneChannel(c, True)
    step_count = int(patterns.getPatternLength(p) * 4)
    return {"pattern": p, "pattern_name": patterns.getPatternName(p), "channel": c,
            "channel_name": channels.getChannelName(c, True),
            "steps_on": [s for s in range(step_count) if channels.getGridBit(c, s, True)],
            "restore": restore}

def req_piano_roll_step(action):
    """
    One step of opening the selected channel's piano roll, driven by the server
    once FL Studio is in front. showWindow alone keeps whatever channel the piano
    roll last showed; closing it and reopening it with F7 (FPT_F7) switches it to
    the selected channel. FL applies window changes only after a request returns,
    so the server sends each step separately and checks the state in between.
      "show"  - open the piano roll (as it is)
      "hide"  - close it
      "f7"    - toggle it like the F7 key (reopens it on the selected channel)
      "focus" - make it FL's focused window
      "state" - change nothing
    Returns whether it's open and the focused window caption (FL's piano roll
    caption is just "Piano roll -", without the channel name).
    """
    if action == "show":
        ui.showWindow(midi.widPianoRoll)
    elif action == "hide":
        ui.hideWindow(midi.widPianoRoll)
    elif action == "f7":
        transport.globalTransport(midi.FPT_F7, 1)
    elif action == "focus":
        ui.setFocused(midi.widPianoRoll)
    elif action != "state":
        raise ValueError(f"Unknown piano roll step {action!r}")
    return {"visible": bool(ui.getVisible(midi.widPianoRoll)), "caption": ui.getFocusedFormCaption()}

def req_restore_selection(pattern, channel):
    """Put back the pattern and selected channel that were active before prepare_piano_roll"""
    if pattern != patterns.patternNumber():
        patterns.jumpToPattern(pattern)
    if channel >= 0:
        channels.selectOneChannel(channel, True)
    return True

MIXER_SLOTS = 10  # effect slots per mixer track

def last_mixer_track():
    """Index of the last insert track (trackCount() also counts a special 'current' track)"""
    return mixer.getTrackInfo(getattr(midi, "TN_LastIns", 2))

def find_mixer_track(track):
    """Mixer track index from a number (0 = Master) or a name (case-insensitive)"""
    last = last_mixer_track()
    if isinstance(track, int) or str(track).strip().isdigit():
        number = int(track)
        if not 0 <= number <= last:
            raise ValueError(f"Mixer track {number} doesn't exist (0-{last})")
        return number
    for t in range(last + 1):
        if mixer.getTrackName(t).lower() == str(track).lower():
            return t
    raise ValueError(f"No mixer track named {track!r}")

def req_get_mixer_track(track):
    t = find_mixer_track(track)
    effects = []
    for s in range(MIXER_SLOTS):
        if plugins.isValid(t, s):
            effect = {"slot": s, "plugin": plugins.getPluginName(t, s)}
            if plugins.getPluginName(t, s, True) != effect["plugin"]:
                effect["name"] = plugins.getPluginName(t, s, True)  # renamed by the user
            effects.append(effect)
    return {
        "index": t,
        "name": mixer.getTrackName(t),
        "volume": finite(mixer.getTrackVolume(t), 3),
        "volume_db": finite(mixer.getTrackVolume(t, 1), 1),
        "pan": finite(mixer.getTrackPan(t), 3),
        "stereo_separation": finite(mixer.getTrackStereoSep(t), 3),
        "muted": bool(mixer.isTrackMuted(t)),
        "solo": bool(mixer.isTrackSolo(t)),
        "armed": bool(mixer.isTrackArmed(t)),
        "effects_enabled": bool(mixer.isTrackSlotsEnabled(t)),
        "effects": effects,
        "sends_to": [{"track": d, "name": mixer.getTrackName(d), "level": finite(mixer.getRouteToLevel(t, d), 3)}
                     for d in range(last_mixer_track() + 1) if d != t and mixer.getRouteSendActive(t, d)],
        "channels": [channels.getChannelName(c, True) for c in range(channels.channelCount(True))
                     if channels.getTargetFxTrack(c, True) == t],
    }

def req_undo_state():
    """Undo history position: 0 = most recent action, higher = further back;
    hint is FL's "position/length" text"""
    return {"position": general.getUndoHistoryLast(), "hint": general.getUndoLevelHint()}

def req_undo_move(delta):
    """Move through the undo history: negative undoes, positive redoes (per the API
    docs; the server checks the direction, since FL 21 swapped related functions)"""
    general.undoUpDown(delta)
    return True

REQUEST_HANDLERS = {
    "undo_state": req_undo_state,
    "undo_move": req_undo_move,
    "ping": lambda: "pong",
    "get_mixer_track": req_get_mixer_track,
    "get_channel": req_get_channel,
    "prepare_piano_roll": req_prepare_piano_roll,
    "piano_roll_step": req_piano_roll_step,
    "restore_selection": req_restore_selection,
    "get_pattern_names": req_get_pattern_names,
    "get_channel_names": req_get_channel_names,
    "get_pattern": req_get_pattern,
    "get_all_patterns": req_get_all_patterns,
    "get_plugin_params": req_get_plugin_params,
}
