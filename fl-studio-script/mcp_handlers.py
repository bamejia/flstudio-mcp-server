# mcp_handlers.py - request handlers for the flstudio MCP server
#
# Imported by device_test.py (the Test Controller script). Kept in a separate
# file so the server can reload it with the "reload_handlers" request (its
# reload_server tool does this) without reopening FL Studio's MIDI Settings.
# Each REQUEST_HANDLERS entry takes the request's args as keyword arguments and
# returns JSON-serializable data.

import builtins
import importlib
import math
import os
import sys

import channels
import device
import general
import midi
import mixer
import patterns
import playlist
import plugins
import transport
import ui

# device_test.py.py runs as __main__; its print() also forwards each line to the
# MCP server (get_fl_log), so prints here and in keybinds.py use it too
print = getattr(sys.modules.get("__main__"), "print", builtins.print)

class ForwardedStderr:
    """sys.stderr that also sends each line to the MCP server's log, so the tracebacks
    FL prints for this script's errors reach get_fl_log too"""

    def __init__(self, stream):
        self.wrapped_stream = stream
        self.pending = ""

    def write(self, text):
        if self.wrapped_stream is not None:
            self.wrapped_stream.write(text)
        self.pending += text
        *lines, self.pending = self.pending.split("\n")
        for line in lines:
            if line.strip():
                try:
                    if device.isAssigned():
                        sys.modules["__main__"].send_sysex(SYSEX_LOG, line)
                except Exception:
                    pass  # never raise from stderr
        return len(text)

    def flush(self):
        if self.wrapped_stream is not None:
            self.wrapped_stream.flush()

SYSEX_LOG = 0x01  # as in device_test.py.py
# Replace any wrapper from before a reload, so its code is current
sys.stderr = ForwardedStderr(getattr(sys.stderr, "wrapped_stream", sys.stderr))

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

def req_get_plugin_params(channel=None, name_filter="", limit=200, track=None, slot=None):
    plugin = plugin_target(channel, track, slot)
    count = plugins.getParamCount(*plugin)
    params = []
    for i in range(min(count, 4096)):  # VST indexes 4096+ are MIDI CC/aftertouch mappings, not settings
        name = plugins.getParamName(i, *plugin)
        if not name or name_filter.lower() not in name.lower():
            continue
        params.append(param_state(i, plugin))
        if len(params) >= limit:
            break
    return {"plugin": plugins.getPluginName(plugin[0], plugin[1], False, plugin[2]),
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
    """Index of the last insert track. TN_LastIns said 500 in a project with tracks 0-19
    (trackCount() 20), and trackCount() may also count a special 'current' track."""
    return min(mixer.getTrackInfo(getattr(midi, "TN_LastIns", 2)), mixer.trackCount() - 1)

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

WINDOWS = {"mixer": midi.widMixer, "channel_rack": midi.widChannelRack,
           "playlist": midi.widPlaylist, "piano_roll": midi.widPianoRoll, "browser": midi.widBrowser}

def req_select(channel=None, mixer_track=None, focus=None):
    """Select one channel and/or a mixer track, and show and focus a window (WINDOWS key);
    returns the previous selection, to put back with another select"""
    previous = {"channel": channels.selectedChannel(False, False, True), "mixer_track": mixer.trackNumber()}
    if focus is not None:
        ui.showWindow(WINDOWS[focus])
        ui.setFocused(WINDOWS[focus])
    if channel is not None:
        channels.selectOneChannel(find_channel(channel), True)
        if focus == "channel_rack":
            ui.scrollWindow(midi.widChannelRack, find_channel(channel))
    if mixer_track is not None:
        mixer.setTrackNumber(find_mixer_track(mixer_track), getattr(midi, "curfxScrollToMakeVisible", 1))
    return previous

def req_instrument_link(channel):
    """A channel's instrument-track clues: FL names a playlist track and the channel's mixer
    track after the channel when it links them, and gives both their default names back when
    it unlinks them (no API tells)"""
    c = find_channel(channel)
    name = channels.getChannelName(c, True)
    track = channels.getTargetFxTrack(c, True)
    return {"index": c, "name": name, "mixer_track": track, "mixer_name": mixer.getTrackName(track),
            "playlist_tracks": [t for t in range(1, playlist.trackCount() + 1) if playlist.getTrackName(t) == name],
            "same_name": sum(channels.getChannelName(i, True) == name for i in range(channels.channelCount(True)))}

def req_active_effect(focus=False):
    """The mixer effect whose window has focus ([track, slot], its name) or None, and the
    focused FL window's caption; focus brings that effect's window to the front (to close it
    with Esc: browser loads replace the active effect)"""
    active = mixer.getActiveEffectIndex()
    if active is not None and focus:
        mixer.focusEditor(*active)
    return {"active": active, "name": plugins.getPluginName(*active, True) if active is not None else None,
            "focused": ui.getFocusedFormCaption()}

def req_browser(move=0, tab=None):
    """Move the browser's selection move items down (negative: up), after switching tab
    ("left"/"right") if given. Returns the tab's name (with tab) and [caption, file type]
    after each step, or of the current item. Folders can only be opened with keys, and
    ui.selectBrowserMenuItem opens a menu that blocks this script until it closes, so the
    server presses Right / Enter instead."""
    result = {}
    if tab:
        result["tab"] = ui.navigateBrowserTabs(midi.FPT_Left if tab == "left" else midi.FPT_Right)
    items = []
    for _ in range(abs(move)):
        ui.navigateBrowser(midi.FPT_Down if move > 0 else midi.FPT_Up, False)
        items.append([ui.getFocusedNodeCaption(), ui.getFocusedNodeFileType()])
    result["items"] = items or [[ui.getFocusedNodeCaption(), ui.getFocusedNodeFileType()]]
    result["focused"] = ui.getFocusedFormCaption()  # keys go to this window
    return result

def req_get_mixer_track(track):
    t = find_mixer_track(track)
    effects = []
    for s in range(MIXER_SLOTS):
        if plugins.isValid(t, s):
            effect = {"slot": s, "plugin": plugins.getPluginName(t, s), "on": bool(slot_switch(t, s))}
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

# --- Changing the project ------------------------------------------------------
# Setters take only what to change (None leaves a value alone) and return the
# object's values as FL now has them; the server keeps the ones that were set.
# Colors are 0xRRGGBB (FL's utils.ColorToRGB); volumes are FL's 0-1 faders.

def rgb(color):
    return color & 0xFFFFFF  # FL may report an alpha byte too

def set_db(set_volume, get_db, db):
    """Set a fader to read db dB. FL's fader curve isn't documented, so bisect it."""
    low, high = 0.0, 1.0
    for _ in range(24):
        middle = (low + high) / 2
        set_volume(middle)
        if get_db() < db:
            low = middle
        else:
            high = middle
    set_volume(high)

def req_set_channel(channel, name=None, color=None, volume=None, db=None, pan=None, muted=None, solo=None,
                    mixer_track=None):
    c = find_channel(channel)
    if name is not None:
        channels.setChannelName(c, name, True)
    if color is not None:
        channels.setChannelColor(c, color, True)
    if db is not None:
        set_db(lambda v: channels.setChannelVolume(c, v, midi.PIM_None, True),
               lambda: channels.getChannelVolume(c, 1, True), db)
    elif volume is not None:
        channels.setChannelVolume(c, volume, midi.PIM_None, True)
    if pan is not None:
        channels.setChannelPan(c, pan, midi.PIM_None, True)
    if muted is not None:
        channels.muteChannel(c, int(muted), True)
    if solo is not None:
        channels.soloChannel(c, int(solo), True)
    if mixer_track is not None:
        channels.setTargetFxTrack(c, find_mixer_track(mixer_track), True)
    return dict(channel_data(c, ("info", "mixer"), 0), color=rgb(channels.getChannelColor(c, True)))

def effect_slot(t, slot):
    slot = int(slot)
    if not (0 <= slot < MIXER_SLOTS and plugins.isValid(t, slot)):
        raise ValueError(f"Mixer track {t} has no effect in slot {slot}")
    return slot

def slot_switch(t, slot, value=None):
    """A mixer effect slot's on switch (REC_Plug_Mute event, despite the name 1 = on); value
    sets it. Returns the switch's value."""
    event = mixer.getTrackPluginId(t, slot) + midi.REC_Plug_Mute
    if value is not None:
        general.processRECEvent(event, value, midi.REC_UpdateValue | midi.REC_UpdateControl)
    return general.processRECEvent(event, 0, midi.REC_GetValue)

def req_set_mixer_track(track, name=None, color=None, volume=None, db=None, pan=None, sep=None, muted=None,
                        solo=None, armed=None, fx_on=None, sends=None, slot_mix=None, slot_on=None):
    t = find_mixer_track(track)
    if name is not None:
        mixer.setTrackName(t, name)
    if color is not None:
        mixer.setTrackColor(t, color)
    if db is not None:
        set_db(lambda v: mixer.setTrackVolume(t, v, midi.PIM_None), lambda: mixer.getTrackVolume(t, 1), db)
    elif volume is not None:
        mixer.setTrackVolume(t, volume, midi.PIM_None)
    if pan is not None:
        mixer.setTrackPan(t, pan, midi.PIM_None)
    if sep is not None:
        mixer.setTrackStereoSep(t, sep, midi.PIM_None)
    if muted is not None:
        mixer.muteTrack(t, int(muted))
    if solo is not None:
        mixer.soloTrack(t, int(solo))
    if armed is not None and bool(mixer.isTrackArmed(t)) != armed:
        mixer.armTrack(t)
    if fx_on is not None:
        mixer.enableTrackSlots(t, int(fx_on))
    for dest, level in (sends or {}).items():
        # level: a 0-1 send level, True (route at the current level) or False (unroute)
        d = find_mixer_track(dest)
        mixer.setRouteTo(t, d, int(level is not False))
        if not isinstance(level, bool):
            mixer.setRouteToLevel(t, d, float(level))
    if sends:
        mixer.afterRoutingChanged()
    for slot, level in (slot_mix or {}).items():
        mixer.setPluginMixLevel(t, effect_slot(t, slot), float(level), midi.PIM_None)
    for slot, on in (slot_on or {}).items():
        slot_switch(t, effect_slot(t, slot), int(on))
    data = req_get_mixer_track(t)
    data["color"] = rgb(mixer.getTrackColor(t))
    data["slot_mix"] = {e["slot"]: finite(mixer.getPluginMixLevel(t, e["slot"]), 3) for e in data["effects"]}
    data["slot_on"] = {e["slot"]: e["on"] for e in data["effects"]}
    return data

def plugin_target(channel=None, track=None, slot=None):
    """(index, slot, useGlobalIndex) of a channel's plugin, or of the effect in a mixer track's slot"""
    if channel is not None:
        c = find_channel(channel)
        if not plugins.isValid(c, -1, True):
            raise ValueError(f"Channel {channel!r} has no plugin (it may be an audio sample)")
        return c, -1, True
    if track is None or slot is None:
        raise ValueError("Give a channel, or a mixer track and slot")
    t = find_mixer_track(track)
    return t, effect_slot(t, slot), False

def find_param(param, plugin):
    """Parameter index from an index, or a name or its start (case-insensitive)"""
    count = min(plugins.getParamCount(*plugin), 4096)  # 4096+ are MIDI CC mappings
    if isinstance(param, int) or str(param).strip().isdigit():
        if not 0 <= int(param) < count:
            raise ValueError(f"Parameter {param} doesn't exist (0-{count - 1})")
        return int(param)
    names = [plugins.getParamName(i, *plugin) for i in range(count)]
    matches = match_names(param, names)
    if len(matches) == 1 or (matches and names[matches[0]].lower() == str(param).strip().lower()):
        return matches[0]  # with duplicate names, the first
    raise no_match("parameter", param, [n for n in names if n], matches)

def param_state(p, plugin):
    return {"index": p, "name": plugins.getParamName(p, *plugin),
            "value": finite(plugins.getParamValue(p, *plugin), 4),
            "display": plugins.getParamValueString(p, *plugin).strip() or None}

def req_set_param(param, value, channel=None, track=None, slot=None):
    """Set a plugin parameter to a 0-1 value"""
    plugin = plugin_target(channel, track, slot)
    p = find_param(param, plugin)
    plugins.setParamValue(float(value), p, plugin[0], plugin[1], midi.PIM_None, plugin[2])
    return param_state(p, plugin)

def req_param_state(param, channel=None, track=None, slot=None):
    """A plugin parameter's value and text (some plugins, like Vital, update the text only
    after the request that set it)"""
    plugin = plugin_target(channel, track, slot)
    return param_state(find_param(param, plugin), plugin)

def req_probe_param(param, values, channel=None, track=None, slot=None):
    """A parameter's display text at each 0-1 value (to find the value for a text like "-6 dB");
    the parameter is put back afterwards"""
    plugin = plugin_target(channel, track, slot)
    p = find_param(param, plugin)
    original = plugins.getParamValue(p, *plugin)
    texts = []
    try:
        for value in values:
            plugins.setParamValue(float(value), p, plugin[0], plugin[1], midi.PIM_None, plugin[2])
            texts.append(plugins.getParamValueString(p, *plugin).strip())
    finally:
        plugins.setParamValue(original, p, plugin[0], plugin[1], midi.PIM_None, plugin[2])
    return dict(param_state(p, plugin), texts=texts)

def req_preset(steps=0, names=False, channel=None, track=None, slot=None):
    """Move steps presets forward (negative: back). Returns the preset count and the current
    preset's name, which FL updates only after the request returns: read it with steps=0.
    names: also each preset's name (only some plugins name them)."""
    plugin = plugin_target(channel, track, slot)
    for _ in range(abs(steps)):
        (plugins.nextPreset if steps > 0 else plugins.prevPreset)(*plugin)
    preset_name = lambda i: plugins.getName(plugin[0], plugin[1], midi.FPN_Preset, i, plugin[2])
    count = plugins.getPresetCount(*plugin)
    result = {"count": count, "current": preset_name(-1)}
    if names:
        result["names"] = [preset_name(i) for i in range(count)]
    return result

def req_set_pattern(pattern=None, clone=False, name=None, color=None):
    """Change a pattern, a new clone of it (FL puts it after the pattern), or (no pattern) a new
    empty one; a new one becomes the active pattern. (setPatternLength needs API v39.)"""
    if pattern is None:
        patterns.findFirstNextEmptyPat(midi.FFNEP_DontPromptName)
        p = patterns.patternNumber()
    else:
        found = find_patterns(pattern)
        if len(found) > 1:
            raise ValueError(f"Patterns {found} are all named {pattern!r}: use a number")
        p = found[0]
        if clone:
            patterns.clonePattern(p)
            p = patterns.patternNumber()
    if name is not None:
        patterns.setPatternName(p, name)
    if color is not None:
        patterns.setPatternColor(p, color)
    return {"pattern": p, "name": patterns.getPatternName(p)}

def find_playlist_track(track):
    """Playlist track number (1 = first) from a number, or a name or its start (case-insensitive)"""
    count = playlist.trackCount()
    if isinstance(track, int) or str(track).strip().isdigit():
        if not 1 <= int(track) <= count:
            raise ValueError(f"Playlist track {track} doesn't exist (1-{count})")
        return int(track)
    names = [playlist.getTrackName(t) for t in range(1, count + 1)]
    matches = match_names(track, names)
    if len(matches) == 1 or (matches and names[matches[0]].lower() == str(track).strip().lower()):
        return matches[0] + 1
    raise no_match("playlist track", track, names, matches)

def req_set_playlist_track(track, name=None, color=None, muted=None, solo=None):
    t = find_playlist_track(track)
    if name is not None:
        playlist.setTrackName(t, name)
    if color is not None:
        playlist.setTrackColor(t, color)
    if muted is not None:
        playlist.muteTrack(t, int(muted))
    if solo is not None:
        playlist.soloTrack(t, int(solo))
    return {"track": t, "name": playlist.getTrackName(t), "color": rgb(playlist.getTrackColor(t)),
            "muted": bool(playlist.isTrackMuted(t)), "solo": bool(playlist.isTrackSolo(t))}

# Automation is recorded the way a MIDI controller records it: with FL recording,
# each value sent as a REC event with REC_Store lands in the active pattern's event
# data (or wherever FL's record settings put it). automation_start starts FL playing
# and recording; the server then calls automation_tick as fast as it can, which sends
# the ramp's value at FL's current song position, and stops FL after the last point.
# Without REC_Init: with it FL also set the control's "Init song with this position" value
# (Tools > Last tweaked), which undoing the recording doesn't remove.
MIXER_EVENTS = {"volume": midi.REC_Mixer_Vol, "pan": midi.REC_Mixer_Pan, "sep": midi.REC_Mixer_SS}
automation = {}

def automation_event(param, channel=None, track=None, slot=None):
    """(REC event id, name) of a plugin parameter, a mixer effect's "mix" level, or (track
    without slot) a mixer track's volume/pan/sep"""
    if track is not None and slot is None:
        if param not in MIXER_EVENTS:
            raise ValueError(f"Without a slot, param is one of {list(MIXER_EVENTS)}")
        return mixer.getTrackPluginId(find_mixer_track(track), 0) + MIXER_EVENTS[param], param
    plugin = plugin_target(channel, track, slot)
    if channel is None and param == "mix":
        return mixer.getTrackPluginId(plugin[0], plugin[1]) + midi.REC_Plug_MixLevel, "mix"
    p = find_param(param, plugin)
    base = (channels.getRecEventId(plugin[0], True) + midi.REC_Chan_Plugin_First if channel is not None
            else mixer.getTrackPluginId(plugin[0], plugin[1]) + midi.REC_Plug_Plugin_First)
    return base + p, plugins.getParamName(p, *plugin)

def send_value(event, value, flags):
    general.processRECEvent(event, int(round(min(max(value, 0.0), 1.0) * midi.FromMIDI_Max)),
                            flags | midi.REC_FromMIDI)

def ramp_value(points, tick):
    """Linear ramp through points [(tick, value)], held before the first and after the last"""
    tick = max(tick, points[0][0])
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if tick <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (tick - t0) / (t1 - t0)
    return points[-1][1]

def req_automation_start(param, points, channel=None, track=None, slot=None):
    if transport.isPlaying():
        raise ValueError("FL Studio is playing: stop it first")
    armed = [t for t in range(last_mixer_track() + 1) if mixer.isTrackArmed(t)]
    if armed:
        raise ValueError(f"Mixer tracks {armed} are armed for recording audio: disarm them first")
    event, name = automation_event(param, channel, track, slot)
    ppq = general.getRecPPQ()
    ticks = sorted((float(beat) * ppq, float(value)) for beat, value in points)
    song = transport.getLoopMode() == 1
    if not song and ticks[-1][0] > pattern_beats(patterns.patternNumber()) * ppq:
        raise ValueError(f"The last point is past the end of the pattern ({pattern_beats(patterns.patternNumber()):g} beats), "
                         "where FL would loop: use a longer pattern or song mode")
    song_ticks = transport.getSongLength(SONGLENGTH_ABSTICKS)
    if song and ticks[-1][0] > song_ticks:
        raise ValueError(f"The last point is past the end of the song ({song_ticks / ppq:g} beats; "
                         "an empty playlist doesn't play): place clips first or use pattern mode")
    send_value(event, ticks[0][1], midi.REC_UpdateValue | midi.REC_UpdateControl)
    automation.clear()
    automation.update(event=event, points=ticks, last=ticks[0][0])
    transport.setSongPos(int(ticks[0][0]), SONGLENGTH_ABSTICKS)
    if not transport.isRecording():
        transport.record()
    transport.start()
    return {"param": name, "into": "song" if song else f"pattern {patterns.getPatternName(patterns.patternNumber())!r}"}

def req_automation_tick():
    """Send the ramp's value at the song position; after the last point, stop FL and its recording"""
    if not automation:
        raise ValueError("No automation is being recorded")
    tick = transport.getSongPos(SONGLENGTH_ABSTICKS)
    points = automation["points"]
    # Done after the last point, when FL stopped, or when the position wrapped (pattern loop)
    done = tick >= points[-1][0] or tick < automation["last"] or not transport.isPlaying()
    if tick >= points[0][0] or done:
        send_value(automation["event"], ramp_value(points, points[-1][0] if done else tick),
                   midi.REC_MIDIController & ~midi.REC_Init)
    automation["last"] = tick
    if done:
        transport.stop()
        if transport.isRecording():
            transport.record()
        automation.clear()
    return {"beat": round(tick / general.getRecPPQ(), 3), "done": done}

def req_undo_state():
    """Undo history position: 0 = most recent action, higher = further back;
    hint is FL's "position/length" text"""
    return {"position": general.getUndoHistoryLast(), "hint": general.getUndoLevelHint()}

def req_save_undo(name):
    """Add a named point to FL's undo history (undoing it shows "Undone: name")"""
    general.saveUndo(name, midi.UF_PL)
    return req_undo_state()

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

# --- Keybinds ------------------------------------------------------------------
# keybinds.py (next to this file) maps key combos to actions. The keyboard
# listener (trigger\keyboard_listener.py in the repo) asks for the combos with
# "keybinds", catches them while FL Studio is focused and runs them with
# "run_keybind". Combos FL Studio already uses are switched off and reported in
# FL's Script output (and get_fl_log), so those keys keep working in FL.

def load_module(name):
    """Import, or re-import from disk, a module next to this file; its print() is forwarded"""
    module = importlib.reload(sys.modules[name]) if name in sys.modules else importlib.import_module(name)
    module.print = print
    return module

def keybind_call(action):
    """The function that runs a keybinds.py action; ValueError if it isn't valid"""
    if callable(action):
        return action
    name, args = (action, {}) if isinstance(action, str) else (tuple(action) + ({},))[:2]
    if name not in REQUEST_HANDLERS or not isinstance(args, dict):
        raise ValueError(f"unknown action {action!r}. Use a request name, (name, {{args}}) or "
                         f"a function. Requests: play, pause, stop, record")
    return lambda: REQUEST_HANDLERS[name](**args)

def read_keybinds(keybinds):
    """canonical combo -> (modifier bits, key code, function), and messages about skipped combos"""
    import fl_shortcuts
    binds, notes = {}, []
    for combo, action in getattr(keybinds, "KEYBINDS", {}).items():
        try:
            name, mods, key = fl_shortcuts.parse_combo(combo)
            call = keybind_call(action)
        except (ValueError, TypeError) as e:
            notes.append(f"{combo!r} skipped: {e}")
            continue
        used = fl_shortcuts.in_use(name)
        if used:
            notes.append(f"{name} is off, FL Studio uses it ({used}). Pick another combo.")
        elif name in binds:
            notes.append(f"{name} is listed twice; the first one is used.")
        else:
            binds[name] = (mods, key, call)
    return binds, notes

def req_keybinds():
    """Load keybinds.py from disk; returns its path and the active combos as [combo, modifier bits, key code]"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keybinds.py")
    load_module("fl_shortcuts")
    try:
        keybinds = load_module("keybinds")
    except Exception as e:
        print(f"keybinds: keybinds.py not loaded ({type(e).__name__}: {e}). Fix it and save again.")
        raise
    binds, notes = read_keybinds(keybinds)
    for note in notes:
        print(f"keybinds: {note}")
    print(f"keybinds: {len(binds)} on" + (f", {len(notes)} skipped (see above)" if notes else ""))
    return {"file": path, "binds": [[name, mods, key] for name, (mods, key, _) in binds.items()]}

def req_run_keybind(combo):
    """Run the keybinds.py action for a combo the listener caught"""
    keybinds = sys.modules.get("keybinds") or load_module("keybinds")
    bind = read_keybinds(keybinds)[0].get(combo)
    if bind is None:
        raise KeyError(f"{combo} isn't an active combo in keybinds.py")
    try:
        bind[2]()
    except Exception as e:
        print(f"keybinds: {combo} failed: {type(e).__name__}: {e}")
        raise

API_MODULES = ("arrangement", "channels", "device", "general", "mixer", "patterns", "playlist", "plugins",
               "transport", "ui")

def req_api_names(module):
    """Function names one FL API module has in this FL version (they vary by version)"""
    if module not in API_MODULES:
        raise ValueError(f"module is one of {API_MODULES}")
    return [n for n in dir(importlib.import_module(module)) if not n.startswith("_")]

def req_song_state():
    """Song-mode facts for the playlist: song length, time selection, performance mode"""
    import arrangement
    ppq = general.getRecPPQ()
    state = {"mode": "song" if transport.getLoopMode() else "pattern", "ppq": ppq,
             "song_length_beats": transport.getSongLength(SONGLENGTH_ABSTICKS) / ppq,
             "selection_beats": [arrangement.selectionStart() / ppq, arrangement.selectionEnd() / ppq],
             "performance_mode": playlist.getPerformanceModeState()}
    return state

def req_time_selection(start=None, end=None):
    """Sets the playlist's time selection in beats (none given: clears it); returns it"""
    import arrangement
    ppq = general.getRecPPQ()
    if start is None:
        arrangement.selectionClear()
    else:
        arrangement.selectionSet(round(start * ppq), round(end * ppq))
    return [arrangement.selectionStart() / ppq, arrangement.selectionEnd() / ppq]

def req_tweak(param, value, channel=None, track=None, slot=None):
    """Set a control through the API, which makes it FL's last tweaked one (Tools > Last
    tweaked): a plugin parameter, a mixer effect's "mix" level, or (track without slot) a mixer
    track's volume, pan or sep; value 0-1 (pan/sep: 0.5 = center). Returns the value it had,
    the value it has (a stepped parameter rounds) and the parameter's name (plugins only)."""
    name = None
    if track is not None and slot is None:
        t = find_mixer_track(track)
        controls = {
            "volume": (lambda: mixer.getTrackVolume(t), lambda v: mixer.setTrackVolume(t, v, midi.PIM_None)),
            "pan": (lambda: (mixer.getTrackPan(t) + 1) / 2, lambda v: mixer.setTrackPan(t, v * 2 - 1, midi.PIM_None)),
            "sep": (lambda: (mixer.getTrackStereoSep(t) + 1) / 2,
                    lambda v: mixer.setTrackStereoSep(t, v * 2 - 1, midi.PIM_None))}
        if param not in controls:
            raise ValueError(f"Without a slot, param is one of {list(controls)}")
        get, put = controls[param]
    elif channel is None and param == "mix":
        t = find_mixer_track(track)
        s = effect_slot(t, slot)
        get, put = lambda: mixer.getPluginMixLevel(t, s), lambda v: mixer.setPluginMixLevel(t, s, v, midi.PIM_None)
    else:
        plugin = plugin_target(channel, track, slot)
        p = find_param(param, plugin)
        name = plugins.getParamName(p, *plugin)
        get = lambda: plugins.getParamValue(p, *plugin)
        put = lambda v: plugins.setParamValue(v, p, plugin[0], plugin[1], midi.PIM_None, plugin[2])
    before = get()
    value = min(max(float(value), 0.0), 1.0)
    # A set that changes nothing may not count as a tweak: move it a little first
    put(value + 0.01 if value < 0.5 else value - 0.01)
    put(value)
    return {"before": finite(before, 6), "after": finite(get(), 6), "name": name}

# --- Playlist (mouse) -----------------------------------------------------------
# No API places clips or edits arrangements, so the server uses the mouse (only while
# the user allows it): these prepare the playlist and read FL's hint bar, which names
# what's under the mouse, so each click is checked before and after.

def req_playlist_view(bar=None, track=None, snap=None, pattern=None):
    """Show and focus the playlist; scroll so bar is at its left edge and track at the top,
    set the snap (midi.Snap_*) and the active pattern (the draw tool's clip). Returns what to
    put back and the timing facts."""
    before = {"vis_bar": playlist.getVisTimeBar(), "snap": ui.getSnapMode(), "pattern": patterns.patternNumber()}
    ui.showWindow(midi.widPlaylist)
    ui.setFocused(midi.widPlaylist)
    if bar is not None:
        ui.scrollWindow(midi.widPlaylist, int(bar), 1)
    if track is not None:
        ui.scrollWindow(midi.widPlaylist, find_playlist_track(track), 0)
    if snap is not None:
        ui.setSnapMode(snap)
    if pattern is not None:
        found = find_patterns(pattern)
        if len(found) > 1:
            raise ValueError(f"Patterns {found} are all named {pattern!r}: use a number")
        if found[0] != patterns.patternNumber():
            patterns.jumpToPattern(found[0])
    return dict(before, ppq=general.getRecPPQ(), ppb=general.getRecPPB())

ARRANGEMENT_HINT = "Current playlist arrangement: "

def req_arrangement_jog(step):
    """Switch to the next (step 1) or previous (-1) arrangement; no API names arrangements,
    but FL's hint does after a switch. Returns the new current one's name, or None when there
    was none to switch to."""
    ui.setHintMsg("")
    transport.globalTransport(midi.FPT_ArrangementJog, int(step))
    hint = ui.getHintMsg()
    return hint[len(ARRANGEMENT_HINT):] if hint.startswith(ARRANGEMENT_HINT) else None

def req_hint():
    """FL's hint bar text (describes what's under the mouse) and the focused window"""
    return {"hint": ui.getHintMsg(), "focused": ui.getFocusedFormCaption(), "form_id": ui.getFocusedFormID(),
            "vis_bar": playlist.getVisTimeBar(), "vis_step": playlist.getVisTimeStep(),
            "vis_tick": playlist.getVisTimeTick()}

REQUEST_HANDLERS = {
    "playlist_view": req_playlist_view,
    "hint": req_hint,
    "arrangement_jog": req_arrangement_jog,
    "api_names": req_api_names,
    "song_state": req_song_state,
    "time_selection": req_time_selection,
    "tweak": req_tweak,
    "keybinds": req_keybinds,
    "run_keybind": req_run_keybind,
    "transport_state": req_transport_state,
    "play": req_play,
    "pause": req_pause,
    "stop": req_stop,
    "record": req_record,
    "undo_state": req_undo_state,
    "undo_move": req_undo_move,
    "save_undo": req_save_undo,
    "ping": lambda: "pong",
    "get_mixer_track": req_get_mixer_track,
    "select": req_select,
    "instrument_link": req_instrument_link,
    "browser": req_browser,
    "active_effect": req_active_effect,
    "get_channel": req_get_channel,
    "prepare_piano_roll": req_prepare_piano_roll,
    "piano_roll_step": req_piano_roll_step,
    "restore_selection": req_restore_selection,
    "get_pattern_names": req_get_pattern_names,
    "get_channel_names": req_get_channel_names,
    "get_pattern": req_get_pattern,
    "get_all_patterns": req_get_all_patterns,
    "get_plugin_params": req_get_plugin_params,
    "set_channel": req_set_channel,
    "set_mixer_track": req_set_mixer_track,
    "set_param": req_set_param,
    "param_state": req_param_state,
    "probe_param": req_probe_param,
    "preset": req_preset,
    "set_pattern": req_set_pattern,
    "set_playlist_track": req_set_playlist_track,
    "automation_start": req_automation_start,
    "automation_tick": req_automation_tick,
}
