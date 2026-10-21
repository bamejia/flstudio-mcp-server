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

def match_names(query, names):
    """Positions of the names matching query (case-insensitive): the exact matches, else
    the names starting with it, so a short start like "elec" finds "Electric"."""
    query = str(query).strip().lower()
    exact = [i for i, n in enumerate(names) if n.lower() == query]
    return exact or ([i for i, n in enumerate(names) if n.lower().startswith(query)] if query else [])

def no_match(kind, query, names, matches):
    """Error for a name that matched nothing or more than one thing (lists capped at 10)"""
    if matches:
        return ValueError(f"{query!r} matches several {kind}s: {[names[i] for i in matches][:10]}")
    return ValueError(f"No {kind} named {query!r}. {kind.capitalize()}s: {names[:10]}"
                      + (f" (+{len(names) - 10} more)" if len(names) > 10 else ""))

def find_patterns(pattern):
    """Pattern numbers matching a pattern number, or a name or its start (case-insensitive).
    Several patterns may share a name, but a start must not fit different names."""
    used = used_patterns()
    if isinstance(pattern, int) or str(pattern).strip().isdigit():
        number = int(pattern)
        if number not in used:
            raise ValueError(f"Pattern {number} doesn't exist or is empty")
        return [number]
    names = [patterns.getPatternName(p) for p in used]
    matches = match_names(pattern, names)
    if not matches or len({names[i].lower() for i in matches}) > 1:
        raise no_match("pattern", pattern, names, matches)
    return [used[i] for i in matches]

def find_channel(name):
    """Global channel index for a channel name or its start (case-insensitive), or a global
    index (an int, or a string of digits that isn't also a channel's name)"""
    names = [channels.getChannelName(c, True) for c in range(channels.channelCount(True))]
    if not isinstance(name, int):
        query = str(name).strip()
        matches = match_names(query, names)
        exact = bool(matches) and names[matches[0]].lower() == query.lower()
        if exact or (len(matches) == 1 and not query.isdigit()):
            return matches[0]  # with duplicate names, the first
        if not query.isdigit():
            raise no_match("channel", name, names, matches)
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

STEPS_PER_BEAT = 4  # FL's default 16th-note steps

def pattern_steps(p):
    """Pattern length in 16th-note steps. getPatternLength counts steps, not beats as
    documented: a pattern it reports as 32 loops after 8 beats (measured live)."""
    return int(patterns.getPatternLength(p))

def pattern_beats(p):
    return pattern_steps(p) / STEPS_PER_BEAT

def read_pattern(p, channel_indexes, fields):
    """Read one pattern. Steps need it to be the active pattern, so jump to it and back."""
    step_count = pattern_steps(p)
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
            "length_beats": pattern_beats(p), "channels": chans}

def req_get_pattern_names():
    return [{"pattern": p, "name": patterns.getPatternName(p), "length_beats": pattern_beats(p)}
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
    step_count = pattern_steps(p)
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
    """Mixer track index from a number (0 = Master) or a name or its start (case-insensitive)"""
    last = last_mixer_track()
    if isinstance(track, int) or str(track).strip().isdigit():
        number = int(track)
        if not 0 <= number <= last:
            raise ValueError(f"Mixer track {number} doesn't exist (0-{last})")
        return number
    names = [mixer.getTrackName(t) for t in range(last + 1)]
    matches = match_names(track, names)
    if len(matches) == 1 or (matches and names[matches[0]].lower() == str(track).strip().lower()):
        return matches[0]
    raise no_match("mixer track", track, names, matches)

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

SONGLENGTH_ABSTICKS = getattr(midi, "SONGLENGTH_ABSTICKS", 2)  # setSongPos/getSongLength unit: ticks

def transport_state():
    """What the server checks after a transport change (FL may apply it only after the request returns)"""
    return {"playing": bool(transport.isPlaying()), "recording": bool(transport.isRecording()),
            "mode": "song" if transport.getLoopMode() else "pattern",
            "beat": round(transport.getSongPos(SONGLENGTH_ABSTICKS) / general.getRecPPQ(), 3)}

def req_transport_state():
    return transport_state()

def req_play(mode=None, beat=None):
    """
    Start or resume playback. start() toggles play/pause, so it's only called when
    not already playing. mode ("pattern"/"song") flips FL's PAT/SONG switch first;
    beat jumps there (beats from the pattern or song start), also while playing.
    Refuses while the record button is on, since playing would then record.
    """
    if transport.isRecording():
        raise ValueError("FL Studio's record button is on, so playing would record. "
                         "Turn it off with record(False) first.")
    song = transport.getLoopMode() == 1
    if mode is not None and song != (mode == "song"):
        transport.setLoopMode()
        song = not song
    if beat is not None:
        ppq = general.getRecPPQ()
        length = (transport.getSongLength(SONGLENGTH_ABSTICKS) / ppq if song
                  else pattern_beats(patterns.patternNumber()))
        if beat < 0 or (beat > 0 and beat >= length):
            raise ValueError(f"beat must be 0 to under {length:g} (the {'song' if song else 'pattern'} length)")
        transport.setSongPos(int(round(beat * ppq)), SONGLENGTH_ABSTICKS)
    if not transport.isPlaying():
        transport.start()
    return transport_state()

def req_pause():
    """Pause playback, keeping the position (start() toggles play/pause)"""
    was_playing = bool(transport.isPlaying())
    if was_playing:
        transport.start()
    return dict(transport_state(), was_playing=was_playing)

def req_stop():
    """Stop playback (FL goes back to the start)"""
    transport.stop()
    return transport_state()

def req_record(on):
    """Turn FL's record button on or off (record() toggles it)"""
    if bool(transport.isRecording()) != bool(on):
        transport.record()
    return transport_state()

REQUEST_HANDLERS = {
    "transport_state": req_transport_state,
    "play": req_play,
    "pause": req_pause,
    "stop": req_stop,
    "record": req_record,
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
