# device_test.py
# name=Test Controller

import transport
import ui
import midi
import channels
import playlist
import patterns
import arrangement
import general
import device
import mixer
import plugins
import time
import sys
import json
import math

# --- Script output forwarding ---------------------------------------------
# Every print() also goes to the MCP server as SysEx over the loopMIDI port, so
# the server can read this script's output (its get_fl_log tool). Frame format,
# shared with trigger.py:
#   F0 7D <type> <msg id> <chunk hi> <chunk lo> <count hi> <count lo> <payload> F7
# 7D is the MIDI "non-commercial" manufacturer ID; the payload is a chunk of
# ASCII JSON, and the server joins a message's chunks before parsing.
SYSEX_ID = 0x7D
SYSEX_LOG = 0x01      # FL -> server: a print() line
SYSEX_REPLY = 0x02    # FL -> server: {"id", "ok", "result" | "error"}
SYSEX_REQUEST = 0x10  # server -> FL: {"id", "cmd", "args"}
SYSEX_CHUNK_SIZE = 200
_sysex_msg_id = 0
_builtin_print = print

def send_sysex(msg_type, obj):
    global _sysex_msg_id
    payload = json.dumps(obj).encode("ascii")  # json escapes non-ASCII, keeping every byte 7-bit
    chunks = [payload[i:i + SYSEX_CHUNK_SIZE] for i in range(0, len(payload), SYSEX_CHUNK_SIZE)] or [b""]
    count = len(chunks)
    _sysex_msg_id = (_sysex_msg_id + 1) & 0x7F
    for index, chunk in enumerate(chunks):
        header = bytes([0xF0, SYSEX_ID, msg_type, _sysex_msg_id,
                        index >> 7, index & 0x7F, count >> 7, count & 0x7F])
        device.midiOutSysex(header + chunk + bytes([0xF7]))

def print(*args, sep=" ", end="\n", **kwargs):
    _builtin_print(*args, sep=sep, end=end, **kwargs)
    try:
        if device.isAssigned():
            send_sysex(SYSEX_LOG, sep.join(str(a) for a in args))
    except Exception as e:
        _builtin_print(f"(log forwarding failed: {type(e).__name__}: {e})")

# --- MCP server requests -----------------------------------------------------
# The server sends JSON requests as SysEx (same framing); each is answered with
# one SysEx reply. Our own log lines and replies also loop back in on the same
# loopMIDI port, so only SYSEX_REQUEST messages are acted on.
_partial_requests = {}  # msg id -> {chunk index: bytes}

def OnSysEx(event):
    data = bytes(event.sysex or b"")
    if data[:1] == b"\xf0":
        data = data[1:]
    if data[-1:] == b"\xf7":
        data = data[:-1]
    if len(data) < 7 or data[0] != SYSEX_ID:
        return
    event.handled = True
    if data[1] != SYSEX_REQUEST:
        return
    msg_id = data[2]
    index = (data[3] << 7) | data[4]
    count = (data[5] << 7) | data[6]
    if index == 0:
        _partial_requests[msg_id] = {}  # message ids wrap around; drop any stale leftovers
    chunks = _partial_requests.setdefault(msg_id, {})
    chunks[index] = data[7:]
    if len(chunks) < count:
        return
    del _partial_requests[msg_id]
    handle_request(json.loads(b"".join(chunks[i] for i in range(count)).decode("ascii")))

def handle_request(request):
    try:
        handler = REQUEST_HANDLERS[request["cmd"]]
        reply = {"id": request["id"], "ok": True, "result": handler(**request.get("args", {}))}
    except Exception as e:
        reply = {"id": request.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"}
    send_sysex(SYSEX_REPLY, reply)

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
    """Global channel index for a channel name (case-insensitive)"""
    names = [channels.getChannelName(c, True) for c in range(channels.channelCount(True))]
    for c, channel_name in enumerate(names):
        if channel_name.lower() == str(name).lower():
            return c
    raise ValueError(f"No channel named {name!r}. Channels: {names}")

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

REQUEST_HANDLERS = {
    "ping": lambda: "pong",
    "get_mixer_track": req_get_mixer_track,
    "get_pattern_names": req_get_pattern_names,
    "get_channel_names": req_get_channel_names,
    "get_pattern": req_get_pattern,
    "get_all_patterns": req_get_all_patterns,
    "get_plugin_params": req_get_plugin_params,
}

# Global variables
running = True
command_history = []
current_pattern = 0
current_channel = 0
terminal_active = False

collecting_tempo_notes = False
tempo_note_array = []
NOTE_TEMPO_START = 72  # C4 starts tempo note collection
NOTE_TEMPO_END = 73    # C#4 ends collection and applies tempo change

channel_to_edit = 0
step_to_edit = 0

def midi_notes_to_int(midi_notes):
    """
    Convert an array of MIDI note values (7 bits each) into a single integer
    
    This function takes a list of MIDI notes and combines them into a single
    integer value, with the first note being the most significant.
    
    Args:
        midi_notes (list): A list of MIDI note values (each 0-127)
        
    Returns:
        int: The combined integer value
    """
    result = 0
    for note in midi_notes:
        # Ensure each value is within MIDI range (0-127)
        note_value = min(127, max(0, note))
        # Shift left by 7 bits (MIDI values use 7 bits) and combine
        result = (result << 7) | note_value
    return result

def OnInit():
    """Called when the script is loaded by FL Studio"""
    print("FL Studio Terminal Beat Builder initialized")
    print("Type 'help' for a list of commands")

    return

def OnDeInit():
    """Called when the script is unloaded by FL Studio"""
    global running
    running = False  # Signal the terminal thread to exit
    print("FL Studio Terminal Beat Builder deinitialized")
    return

def OnRefresh(flags):
    """Called when FL Studio's state changes or when a refresh is needed"""
    # Update terminal with current state if needed
    return

def OnMidiIn(event):
    """Called whenever the device sends a MIDI message to FL Studio"""
    #print(f"MIDI In - Status: {event.status}, Data1: {event.data1}, Data2: {event.data2}")
    return

def change_tempo(bpm):
    """
    Change the tempo in FL Studio to the specified BPM value
    
    Args:
        bpm (float): The desired tempo in beats per minute
    """
    # FL Studio stores tempo as BPM * 1000
    tempo_value = int(bpm * 1000)
    
    # Use processRECEvent to set the tempo
    # REC_Tempo is the event ID for tempo
    # REC_Control | REC_UpdateControl flags ensure the value is set and the UI updates
    general.processRECEvent(
        midi.REC_Tempo,
        tempo_value,
        midi.REC_Control | midi.REC_UpdateControl
    )

def process_received_midi(note, velocity):

    global current_note, current_velocity, current_length, current_position
    global decimal_state, decimal_value, decimal_target
    
    # Special MIDI commands
    DECIMAL_MARKER = 100   # Indicates next value is a decimal part
    LENGTH_MARKER = 101    # Next value affects length
    POSITION_MARKER = 102  # Next value affects position
    
    # Process based on message type
    if note == DECIMAL_MARKER:
        # Next value will be a decimal
        decimal_state = 1
        return False
        
    elif note == LENGTH_MARKER:
        # Next value affects length
        decimal_target = "length"
        decimal_state = 0
        decimal_value = 0
        return False
        
    elif note == POSITION_MARKER:
        # Next value affects position
        decimal_target = "position"
        decimal_state = 0
        decimal_value = 0
        return False
        
    elif decimal_state == 1:
        # This is a decimal part value
        decimal_value = note / 10.0  # Convert to decimal (0-9 becomes 0.0-0.9)
        
        # Apply to the correct parameter
        if decimal_target == "length":
            current_length = (current_length or 0) + decimal_value
            print(f"Set length decimal: {current_length:.2f}")
        elif decimal_target == "position":
            current_position = (current_position or 0) + decimal_value
            print(f"Set position decimal: {current_position:.2f}")
            
        decimal_state = 0
        return False
        
    elif decimal_target is not None:
        # This is a whole number part for a specific parameter
        if decimal_target == "length":
            current_length = float(note)
            print(f"Set length whole: {current_length:.2f}")
        elif decimal_target == "position":
            current_position = float(note)
            print(f"Set position whole: {current_position:.2f}")
        return False
        
    else:
        # This is a note value and velocity
        # Check if we have a complete previous note to add
        add_note = (current_note is not None and 
                   current_velocity is not None and 
                   current_length is not None and 
                   current_position is not None)
        
        # Start a new note
        current_note = note
        current_velocity = velocity
        # Use default values if not specified
        if current_length is None:
            current_length = 1.0
        if current_position is None:
            current_position = 0.0
        print(f"Started new note: {current_note}, velocity: {current_velocity}")
        
        return add_note


def OnMidiMsg(event, timestamp=0):
    """Called when a processed MIDI message is received"""
    
    global receiving_mode, message_count, messages_received
    global current_note, current_velocity, current_length, current_position
    global decimal_state, decimal_target, midi_notes_array
    
    if 'receiving_mode' not in globals():
        global receiving_mode
        receiving_mode = False
    
    if 'note_count' not in globals():
        global note_count
        note_count = 0
    
    if 'values_received' not in globals():
        global values_received
        values_received = 0
    
    if 'midi_data' not in globals():
        global midi_data
        midi_data = []
    
    if 'midi_notes_array' not in globals():
        global midi_notes_array
        midi_notes_array = []

    # Tempo change: CC 113 = BPM high 7 bits, CC 114 = BPM low 7 bits (applies it)
    global tempo_msb
    if event.status >= midi.MIDI_CONTROLCHANGE and event.status < midi.MIDI_CONTROLCHANGE + 16:
        if event.data1 == 113:
            tempo_msb = event.data2
            event.handled = True
            return
        if event.data1 == 114:
            bpm = (globals().get('tempo_msb', 0) << 7) | event.data2
            tempo_msb = 0
            bpm = max(20, min(bpm, 999))
            change_tempo(bpm)
            print(f"Tempo changed to {bpm} BPM")
            event.handled = True
            return
        # Tempo request: reply with BPM * 10 as CC 116 (high 7 bits) + CC 117 (low 7 bits)
        if event.data1 == 115:
            tempo_x10 = int(round(mixer.getCurrentTempo() / 100))
            device.midiOutMsg(midi.MIDI_CONTROLCHANGE, 0, 116, (tempo_x10 >> 7) & 0x7F)
            device.midiOutMsg(midi.MIDI_CONTROLCHANGE, 0, 117, tempo_x10 & 0x7F)
            print(f"Sent current tempo: {tempo_x10 / 10} BPM")
            event.handled = True
            return
        # Our own tempo replies loop back in on the same port; ignore them
        if event.data1 in (116, 117):
            event.handled = True
            return

    # Only process Note On messages with velocity > 0
    if event.status >= midi.MIDI_NOTEON and event.status < midi.MIDI_NOTEON + 16 and event.data2 > 0:
        note_value = event.data1
        
        # Toggle receiving mode with note 0
        if note_value == 0 and not receiving_mode:
            receiving_mode = True
            print("Started receiving MIDI notes")
            midi_data = []
            note_count = 0
            values_received = 0
            midi_notes_array = []
            event.handled = True
            return
        
        # Only process further messages if in receiving mode
        if not receiving_mode:
            return
        
        # Second message is the note count
        if note_count == 0:
            note_count = note_value
            print(f"Expecting {note_count} notes")
            event.handled = True
            return
        
        # All subsequent messages are MIDI values (6 per note)
        midi_data.append(note_value)
        values_received += 1
        
        # Process completed notes (every 6 values)
        if len(midi_data) >= 6 and len(midi_data) % 6 == 0:
            # Process the last complete note
            i = len(midi_data) - 6
            note = midi_data[i]
            velocity = midi_data[i+1]
            length_whole = midi_data[i+2]
            length_decimal = midi_data[i+3]
            position_whole = midi_data[i+4]
            position_decimal = midi_data[i+5]
            
            # Calculate full values
            length = length_whole + (length_decimal / 10.0)
            position = position_whole + (position_decimal / 10.0)
            
            # Add to notes array
            midi_notes_array.append((note, velocity, length, position))
            print(f"Added note: note={note}, velocity={velocity}, length={length:.1f}, position={position:.1f}")
            print(f"Current array size: {len(midi_notes_array)}")

            if len(midi_notes_array) >= note_count or note_value == 127:
                print(f"Received all {len(midi_notes_array)} notes or termination signal")
                receiving_mode = False
                
                # Only process if we have actual notes
                if midi_notes_array:
                    # Print all collected notes
                    print(f"Collected {len(midi_notes_array)} notes:")
                    for i, (note, vel, length, pos) in enumerate(midi_notes_array):
                        print(f"  Note {i+1}: note={note}, velocity={vel}, length={length:.1f}, position={pos:.1f}")
                    
                    print("\nFinal array:")
                    print(midi_notes_array)
                    
                    # Process the notes using the record_notes_batch function
                    record_notes_batch(midi_notes_array)
                
                event.handled = True
                return
        
        # Check if we've received all expected notes
        # if len(midi_notes_array) >= note_count:
        #     print(f"Received all {note_count} notes")
        #     receiving_mode = False
            
        #     # Print all collected notes
        #     print(f"Collected {len(midi_notes_array)} notes:")
        #     for i, (note, vel, length, pos) in enumerate(midi_notes_array):
        #         print(f"  Note {i+1}: note={note}, velocity={vel}, length={length:.1f}, position={pos:.1f}")
            
        #     print("\nFinal array:")
        #     print(midi_notes_array)

        #     record_notes_batch(midi_notes_array)
            
        #     # Process the notes here if needed
        #     # record_notes_batch(midi_notes_array)
        
        # event.handled = True
        


    # elif note == 72:
    #         collecting_tempo_notes = True
    #         tempo_note_array = []
    #         print("Started collecting notes for tempo change")
    #         event.handled = True
            
    #     # End collection and apply tempo change
    # elif note == 73:
    #         if collecting_tempo_notes and tempo_note_array:
    #             bpm = change_tempo_from_notes(tempo_note_array)
    #             print(f"Tempo changed to {bpm} BPM from collected notes: {tempo_note_array}")
    #             collecting_tempo_notes = False
    #             tempo_note_array = []
    #         else:
    #             print("No tempo notes collected, tempo unchanged")
    #         event.handled = True
            
    #     # Collect notes for tempo if in collection mode
    # elif collecting_tempo_notes:
    #         tempo_note_array.append(note)
    #         print(f"Added note {note} to tempo collection, current array: {tempo_note_array}")
    #         event.handled = True
    
    # Handle Control Change messages
    # elif event.status >= midi.MIDI_CONTROLCHANGE and event.status < midi.MIDI_CONTROLCHANGE + 16:
    #     # CC 100: Select channel to edit
    #     if event.data1 == 100:
    #         channel_to_edit = event.data2
    #         channels.selectOneChannel(channel_to_edit)
    #         print(f"Selected channel {channel_to_edit} for grid editing")
    #         event.handled = True
            
    #     # CC 110: Select step to edit
    #     elif event.data1 == 110:
    #         step_to_edit = event.data2
    #         print(f"Selected step {step_to_edit} for grid editing")
    #         event.handled = True
            
    #     # CC 111: Toggle step on/off
    #     elif event.data1 == 111:
    #         enabled = event.data2 > 0
    #         channels.setGridBit(channel_to_edit, step_to_edit, enabled)
    #         print(f"Set grid bit for channel {channel_to_edit}, step {step_to_edit} to {enabled}")
    #         commit_pattern_changes()  # Force UI update
    #         event.handled = True
            
    #     # CC 112: Set step velocity/level
    #     elif event.data1 == 112:
    #         velocity = event.data2
    #         channels.setStepLevel(channel_to_edit, step_to_edit, velocity)
    #         print(f"Set step level for channel {channel_to_edit}, step {step_to_edit} to {velocity}")
    #         commit_pattern_changes()  # Force UI update
    #         event.handled = True
        
    #     # Process other CC messages
    #     else:
    #         # Handle other CC messages with your existing code...
    #         pass
    
    # # Handle other MIDI message types if needed
    # else:
    #     # Process other MIDI message types
    #     pass
    
    # Handle Note On messages with your existing code...
    # [rest of your existing OnMidiMsg function]
    #record_notes_batch(midi_notes_array)

# Make sure your commit_pattern_changes function is defined:
def commit_pattern_changes(pattern_num=None):
    """Force FL Studio to update the pattern data visually"""
    if pattern_num is None:
        pattern_num = patterns.patternNumber()
    
    # Force FL Studio to redraw and commit changes
    ui.crDisplayRect()
    
    # Force channel rack to update
    ui.setFocused(midi.widChannelRack)
    
    # Update playlist if needed
    playlist.refresh()
def OnTransport(isPlaying):
    """Called when the transport state changes (play/stop)"""
    print(f"Transport state changed: {'Playing' if isPlaying else 'Stopped'}")
    return

def OnTempoChange(tempo):
    """Called when the tempo changes"""
    print(f"Tempo changed to: {tempo} BPM")
    return


# # Terminal interface functions
# def start_terminal_thread():
#     """Start a thread to handle terminal input"""
#     global terminal_active
#     if not terminal_active:
#         terminal_active = True
#         thread = threading.Thread(target=terminal_loop)
#         thread.daemon = True
#         thread.start()

# def terminal_loop():
#     """Main terminal input loop"""
#     global running, terminal_active
    
#     print("\n===== FL STUDIO TERMINAL BEAT BUILDER =====")
#     print("Enter commands to build your beat (type 'help' for commands)")
    
#     while running:
#         try:
#             command = input("\nFLBEAT> ")
#             command_history.append(command)
#             process_command(command)
#         except Exception as e:
#             print(f"Error processing command: {e}")
    
#     terminal_active = False

def record_note(note=60, velocity=100, length_beats=1.0, position_beats=0.0, quantize=True):
    """
    Records a single note to the piano roll synced with project tempo
    
    Args:
        note (int): MIDI note number (60 = middle C)
        velocity (int): Note velocity (0-127)
        length_beats (float): Length of note in beats (1.0 = quarter note)
        position_beats (float): Position to place note in beats from start
        quantize (bool): Whether to quantize the recording afterward
    """
    # Make sure transport is stopped first
    if transport.isPlaying():
        transport.stop()
    
    # Get the current channel
    channel = channels.selectedChannel()
    
    # Get the project's PPQ (pulses per quarter note)
    ppq = general.getRecPPQ()
    
    # Calculate ticks based on beats
    length_ticks = int(length_beats * ppq)
    position_ticks = int(position_beats * ppq)
    
    # Set playback position
    transport.setSongPos(position_ticks, 2)  # 2 = SONGLENGTH_ABSTICKS
    
    # Toggle recording mode if needed
    if not transport.isRecording():
        transport.record()
    
    print(f"Recording note {note} to channel {channel}")
    print(f"Position: {position_beats} beats, Length: {length_beats} beats")
    
    # Calculate the exact tick positions where we need to place note and note-off
    start_tick = position_ticks
    end_tick = start_tick + length_ticks
    
    # Start playback to begin recording
    transport.start()
    
    # Record the note at the exact start position
    channels.midiNoteOn(channel, note, velocity)
    
    # Get the current tempo (BPM)
    tempo = 120  # Default fallback
    
    # Try to get actual tempo from the project
    try:
        import mixer
        tempo = mixer.getCurrentTempo()
        tempo = tempo/1000
        print(f"Using project tempo: {tempo} BPM")
    except (ImportError, AttributeError):
        print("Using default tempo: 120 BPM")
    
    # Calculate the time to wait in seconds
    seconds_to_wait = (length_beats * 60) / tempo
    
    print(f"Waiting for {seconds_to_wait:.2f} seconds...")
    
    # Wait the calculated time
    time.sleep(seconds_to_wait)
    
    # Send note-off event
    channels.midiNoteOn(channel, note, 0)
    
    # Stop playback
    transport.stop()
    
    # Exit recording mode if it was active
    if transport.isRecording():
        transport.record()
    
    # Quantize if requested
    if quantize:
        channels.quickQuantize(channel)
        print("Recording quantized")
    
    print(f"Note {note} recorded to piano roll")
    
    # Return to beginning
    transport.setSongPos(0, 2)

def rec_hihat_pattern():
    """
    Records a predefined hi-hat pattern to the piano roll using record_notes_batch
    
    This creates a 4-bar hi-hat pattern with variations in velocity, rhythm, and types of hats
    """
    # Stop playback and rewind to beginning first
    if transport.isPlaying():
        transport.stop()
    
    transport.setSongPos(0, 2)  # Go to the beginning
    
    print("Recording hi-hat pattern...")
    
    # Common hi-hat MIDI notes:
    # 42 = Closed hi-hat
    # 44 = Pedal hi-hat
    # 46 = Open hi-hat
    
    # Define the pattern as a list of notes
    # Each tuple contains (note, velocity, length_beats, position_beats)
    hihat_pattern = [
        # BAR 1 - Basic pattern
        (42, 90, 0.1, 0.0),     # Closed hat on beat 1
        (42, 65, 0.1, 0.5),     # Closed hat on off-beat
        (42, 90, 0.1, 1.0),     # Closed hat on beat 2
        (42, 65, 0.1, 1.5),     # Closed hat on off-beat
        (42, 90, 0.1, 2.0),     # Closed hat on beat 3
        (42, 65, 0.1, 2.5),     # Closed hat on off-beat
        (42, 90, 0.1, 3.0),     # Closed hat on beat 4
        (42, 65, 0.1, 3.5),     # Closed hat on off-beat
        
        # BAR 2 - Adding 16th notes
        (42, 90, 0.1, 4.0),     # Closed hat on beat 1
        (42, 60, 0.1, 4.25),    # Closed hat on 16th
        (42, 70, 0.1, 4.5),     # Closed hat on off-beat
        (42, 60, 0.1, 4.75),    # Closed hat on 16th
        (42, 90, 0.1, 5.0),     # Closed hat on beat 2
        (42, 60, 0.1, 5.25),    # Closed hat on 16th
        (42, 70, 0.1, 5.5),     # Closed hat on off-beat
        (42, 60, 0.1, 5.75),    # Closed hat on 16th
        (42, 90, 0.1, 6.0),     # Closed hat on beat 3
        (42, 60, 0.1, 6.25),    # Closed hat on 16th
        (42, 70, 0.1, 6.5),     # Closed hat on off-beat
        (42, 60, 0.1, 6.75),    # Closed hat on 16th
        (42, 90, 0.1, 7.0),     # Closed hat on beat 4
        (46, 80, 0.2, 7.5),     # Open hat on off-beat
        
        # BAR 3 - Mixing closed and open hats
        (42, 100, 0.1, 8.0),    # Closed hat on beat 1
        (42, 70, 0.1, 8.5),     # Closed hat on off-beat
        (46, 85, 0.2, 9.0),     # Open hat on beat 2
        (42, 70, 0.1, 9.5),     # Closed hat on off-beat
        (42, 95, 0.1, 10.0),    # Closed hat on beat 3
        (42, 70, 0.1, 10.5),    # Closed hat on off-beat
        (46, 85, 0.2, 11.0),    # Open hat on beat 4
        
        # Triplet fill at the end of bar 3
        (42, 80, 0.08, 11.33),  # Closed hat - triplet 1
        (42, 85, 0.08, 11.66),  # Closed hat - triplet 2
        (42, 90, 0.08, 11.99),  # Closed hat - triplet 3
        
        # BAR 4 - Complex pattern with pedal hats
        (42, 100, 0.1, 12.0),   # Closed hat on beat 1
        (44, 75, 0.1, 12.25),   # Pedal hat on 16th
        (42, 80, 0.1, 12.5),    # Closed hat on off-beat
        (44, 70, 0.1, 12.75),   # Pedal hat on 16th
        (42, 90, 0.1, 13.0),    # Closed hat on beat 2
        (46, 85, 0.3, 13.5),    # Open hat on off-beat
        
        # Beat 3-4: Building intensity
        (42, 95, 0.1, 14.0),    # Closed hat on beat 3
        (42, 75, 0.1, 14.25),   # Closed hat on 16th
        (42, 85, 0.1, 14.5),    # Closed hat on off-beat
        (42, 80, 0.1, 14.75),   # Closed hat on 16th
        
        # Final fill
        (42, 85, 0.05, 15.0),   # Closed hat - 32nd note 1
        (42, 90, 0.05, 15.125), # Closed hat - 32nd note 2
        (42, 95, 0.05, 15.25),  # Closed hat - 32nd note 3
        (42, 100, 0.05, 15.375),# Closed hat - 32nd note 4
        (42, 105, 0.05, 15.5),  # Closed hat - 32nd note 5
        (42, 110, 0.05, 15.625),# Closed hat - 32nd note 6
        (42, 115, 0.05, 15.75), # Closed hat - 32nd note 7
        (46, 120, 0.25, 15.875),# Open hat - final accent
    ]
    
    # Record the hi-hat pattern using the batch recording function
    record_notes_batch(hihat_pattern)
    
    print("Hi-hat pattern recording complete!")
    
    # Quantize the hi-hat pattern
    channel = channels.selectedChannel()
    channels.quickQuantize(channel)
    
    # Return to beginning
    transport.setSongPos(0, 2)

def record_notes_batch(notes_array):
    """
    Records a batch of notes to FL Studio, handling simultaneous notes properly
    
    Args:
        notes_array: List of tuples, each containing (note, velocity, length_beats, position_beats)
    """
    # Sort notes by their starting position
    sorted_notes = sorted(notes_array, key=lambda x: x[3])
    
    # Group notes by their starting positions
    position_groups = {}
    for note in sorted_notes:
        position = note[3]  # position_beats is the 4th element (index 3)
        if position not in position_groups:
            position_groups[position] = []
        position_groups[position].append(note)
    
    # Process each position group
    positions = sorted(position_groups.keys())
    for position in positions:
        notes_at_position = position_groups[position]
        
        # Find the longest note in this group to determine recording length
        max_length = max(note[2] for note in notes_at_position)
        
        # Make sure transport is stopped first
        if transport.isPlaying():
            transport.stop()
        
        # Get the current channel
        channel = channels.selectedChannel()
        
        # Get the project's PPQ (pulses per quarter note)
        ppq = general.getRecPPQ()
        
        # Calculate ticks based on beats
        position_ticks = int(position * ppq)
        
        # Set playback position
        transport.setSongPos(position_ticks, 2)  # 2 = SONGLENGTH_ABSTICKS
        
        # Toggle recording mode if needed
        if not transport.isRecording():
            transport.record()
        
        print(f"Recording {len(notes_at_position)} simultaneous notes at position {position}")
        
        # Start playback to begin recording
        transport.start()
        
        # Record all notes at this position simultaneously
        for note, velocity, length, _ in notes_at_position:
            channels.midiNoteOn(channel, note, velocity)
        
        # Get the current tempo
        try:
            import mixer
            tempo = mixer.getCurrentTempo()
            tempo = tempo/1000
        except (ImportError, AttributeError):
            tempo = 120  # Default fallback
            
        print(f"Using tempo: {tempo} BPM")
        
        # Calculate the time to wait in seconds based on the longest note
        seconds_to_wait = (max_length * 60) / tempo
        
        print(f"Waiting for {seconds_to_wait:.2f} seconds...")
        
        # Wait the calculated time
        time.sleep(seconds_to_wait)
        
        # Send note-off events for all notes
        for note, _, _, _ in notes_at_position:
            channels.midiNoteOn(channel, note, 0)
        
        # Stop playback
        transport.stop()
        
        # Exit recording mode if it was active
        if transport.isRecording():
            transport.record()
        
        # Small pause between recordings to avoid potential issues
        time.sleep(0.2)
    
    print("All notes recorded successfully")
    
    # Return to beginning
    transport.setSongPos(0, 2)



def rec_melody():
    """
    Records a predefined melody to the piano roll by calling record_notes_batch
    
    The melody is a robust 4-bar composition with melody notes and chord accompaniment
    """
    # Stop playback and rewind to beginning first
    if transport.isPlaying():
        transport.stop()
    
    transport.setSongPos(0, 2)  # Go to the beginning
    
    print("Recording melody...")
    
    # Define the melody as a list of notes
    # Each tuple contains (note, velocity, length_beats, position_beats)
    melody = [
        # BAR 1
        # Beat 1: C major chord
        (60, 100, 1.0, 0.0),    # C4 - root note
        (64, 85, 1.0, 0.0),     # E4 - chord tone
        (67, 80, 1.0, 0.0),     # G4 - chord tone
        
        # Beat 1.5: Melody note
        (72, 110, 0.5, 0.5),    # C5 - melody
        
        # Beat 2: G7 chord
        (55, 90, 1.0, 1.0),     # G3 - bass note
        (59, 75, 1.0, 1.0),     # B3 - chord tone
        (62, 75, 1.0, 1.0),     # D4 - chord tone
        (65, 75, 1.0, 1.0),     # F4 - chord tone
        
        # Beat 2.5-3: Melody phrase
        (71, 105, 0.25, 1.5),   # B4 - melody
        (69, 95, 0.25, 1.75),   # A4 - melody
        (67, 90, 0.5, 2.0),     # G4 - melody
        
        # Beat 3: C major chord
        (48, 95, 1.0, 2.0),     # C3 - bass note
        (64, 75, 1.0, 2.0),     # E4 - chord tone
        (67, 75, 1.0, 2.0),     # G4 - chord tone
        
        # Beat 4: Melody fill
        (64, 100, 0.5, 3.0),    # E4 - melody
        (65, 90, 0.25, 3.5),    # F4 - melody
        (67, 95, 0.25, 3.75),   # G4 - melody
        
        # BAR 2
        # Beat 1: Am chord
        (57, 95, 1.0, 4.0),     # A3 - bass note
        (60, 80, 1.0, 4.0),     # C4 - chord tone
        (64, 80, 1.0, 4.0),     # E4 - chord tone
        
        # Beat 1-2: Melody note
        (69, 110, 0.75, 4.0),   # A4 - melody
        (67, 90, 0.25, 4.75),   # G4 - melody
        
        # Beat 2: F major chord
        (53, 90, 1.0, 5.0),     # F3 - bass note
        (57, 75, 1.0, 5.0),     # A3 - chord tone
        (60, 75, 1.0, 5.0),     # C4 - chord tone
        
        # Beat 2.5-3: Melody
        (65, 100, 0.5, 5.5),    # F4 - melody
        (64, 90, 0.5, 6.0),     # E4 - melody
        
        # Beat 3: G7 chord
        (55, 95, 1.0, 6.0),     # G3 - bass note
        (59, 80, 1.0, 6.0),     # B3 - chord tone
        (62, 80, 1.0, 6.0),     # D4 - chord tone
        
        # Beat 3.5-4: Melody fill
        (62, 100, 0.25, 6.5),   # D4 - melody
        (64, 95, 0.25, 6.75),   # E4 - melody
        (65, 90, 0.25, 7.0),    # F4 - melody
        (67, 105, 0.75, 7.25),  # G4 - melody
        
        # BAR 3
        # Beat 1: C major chord
        (48, 100, 1.0, 8.0),    # C3 - bass note
        (60, 85, 1.0, 8.0),     # C4 - chord tone
        (64, 85, 1.0, 8.0),     # E4 - chord tone
        (67, 85, 1.0, 8.0),     # G4 - chord tone
        
        # Beat 1-2: Melody
        (72, 110, 1.0, 8.0),    # C5 - melody
        
        # Beat 2: Em chord
        (52, 90, 1.0, 9.0),     # E3 - bass note
        (59, 75, 1.0, 9.0),     # B3 - chord tone
        (64, 75, 1.0, 9.0),     # E4 - chord tone
        
        # Beat 2.5-3.5: Melody run
        (71, 105, 0.25, 9.5),   # B4 - melody
        (72, 100, 0.25, 9.75),  # C5 - melody
        (74, 110, 0.5, 10.0),   # D5 - melody
        (76, 115, 0.5, 10.5),   # E5 - melody
        
        # Beat 3: Am chord
        (57, 95, 1.0, 10.0),    # A3 - bass note
        (60, 80, 1.0, 10.0),    # C4 - chord tone
        (64, 80, 1.0, 10.0),    # E4 - chord tone
        
        # Beat 4: Descending run
        (74, 100, 0.25, 11.0),  # D5 - melody
        (72, 95, 0.25, 11.25),  # C5 - melody
        (71, 90, 0.25, 11.5),   # B4 - melody
        (69, 85, 0.25, 11.75),  # A4 - melody
        
        # BAR 4
        # Beat 1: F major chord
        (53, 95, 1.0, 12.0),    # F3 - bass note
        (60, 80, 1.0, 12.0),    # C4 - chord tone
        (65, 80, 1.0, 12.0),    # F4 - chord tone
        
        # Beat 1-2: Melody
        (67, 100, 1.0, 12.0),   # G4 - melody
        
        # Beat 2: G7 chord
        (55, 90, 1.0, 13.0),    # G3 - bass note
        (59, 75, 1.0, 13.0),    # B3 - chord tone
        (62, 75, 1.0, 13.0),    # D4 - chord tone
        
        # Beat 2-3: Melody
        (65, 95, 0.5, 13.0),    # F4 - melody
        (64, 90, 0.5, 13.5),    # E4 - melody
        
        # Beat 3-4: Final C major chord
        (48, 110, 2.0, 14.0),   # C3 - bass note
        (60, 95, 2.0, 14.0),    # C4 - chord tone
        (64, 95, 2.0, 14.0),    # E4 - chord tone
        (67, 95, 2.0, 14.0),    # G4 - chord tone
        
        # Final melody note
        (72, 120, 2.0, 14.0),   # C5 - melody final note
    ]
    
    # Record the melody using the batch recording function
    record_notes_batch(melody)
    
    print("Melody recording complete!")
    
def change_tempo_from_notes(note_array):
    """
    Change the tempo in FL Studio based on an array of MIDI notes
    
    This function converts an array of MIDI notes to a single integer value
    and uses that value as the new tempo.
    
    Args:
        note_array (list): A list of MIDI note values (each 0-127)
    """
    # Convert note array to integer
    bpm_value = midi_notes_to_int(note_array)
    
    # Limit to a reasonable BPM range
    if bpm_value < 20:
        bpm_value = 20  # Minimum reasonable tempo
    elif bpm_value > 999:
        bpm_value = 999  # Maximum reasonable tempo
    
    # Change the tempo
    print(f"Changing tempo to {bpm_value} BPM from note array {note_array}")
    change_tempo(bpm_value)
    
    return bpm_value

# Start the terminal interface when loaded in FL Studio
# No need to call this explicitly as OnInit will be called by FL Studio
