from typing import Any
import httpx
from mcp.server.fastmcp import FastMCP
import mido
from mido import Message
import sys
import time

# Initialize FastMCP server
mcp = FastMCP("flstudio")

# Commands go to FL Studio on the loopMIDI port named "loopMIDI Port".
# Windows appends an index to port names, so match by prefix.
OUTPUT_PORT_NAME = 'loopMIDI Port'

def open_output_port():
    names = [n for n in mido.get_output_names() if n.startswith(OUTPUT_PORT_NAME)]
    if not names:
        raise IOError(f"No MIDI output named '{OUTPUT_PORT_NAME}'. Available outputs: {mido.get_output_names()}")
    return mido.open_output(names[0])

output_port = open_output_port()

def send(msg):
    """Send a MIDI message, reopening the port once if loopMIDI dropped it"""
    global output_port
    try:
        output_port.send(msg)
    except Exception:
        try:
            output_port.close()
        except Exception:
            pass
        output_port = open_output_port()
        output_port.send(msg)

# FL Studio sends replies (e.g. the current tempo) back on the same loopMIDI port.
# Our own outgoing messages show up there too, so readers filter by CC number.
# Opened on first use so the server still starts if the port doesn't exist yet.
INPUT_PORT_NAME = 'loopMIDI Port'
input_port = None

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

# Drum sound MIDI notes
KICK = 36      # C1
SNARE = 38     # D1
CLAP = 39      # D#1
CLOSED_HAT = 42  # F#1
OPEN_HAT = 46  # A#1



@mcp.tool()
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

@mcp.tool()
def play():
    """Send MIDI message to start playback in FL Studio"""
    # Send Note On for C3 (note 60)
    send(mido.Message('note_on', note=60, velocity=100))
    time.sleep(0.1)  # Small delay
    send(mido.Message('note_off', note=60, velocity=0))
    print("Sent Play command", file=sys.stderr)

@mcp.tool()
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

@mcp.tool()
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

@mcp.tool()
def get_tempo(timeout: float = 2.0):
    """
    Read the current project tempo from FL Studio

    Sends a request CC; the FL Studio script replies with BPM * 10 as two CC
    messages on the input port.

    Args:
        timeout (float): Seconds to wait for FL Studio's reply
    """
    global input_port
    if input_port is None:
        names = [n for n in mido.get_input_names() if n.startswith(INPUT_PORT_NAME)]
        if not names:
            return f"No MIDI input named '{INPUT_PORT_NAME}'. Available inputs: {mido.get_input_names()}"
        input_port = mido.open_input(names[0])

    # Drop any stale messages left over from earlier requests
    for _ in input_port.iter_pending():
        pass

    send(Message('control_change', control=CC_TEMPO_REQUEST, value=0))

    msb = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = input_port.poll()
        if msg is None:
            time.sleep(0.01)
            continue
        if msg.type != 'control_change':
            continue
        if msg.control == CC_TEMPO_REPLY_MSB:
            msb = msg.value
        elif msg.control == CC_TEMPO_REPLY_LSB and msb is not None:
            return f"Current FL Studio tempo: {((msb << 7) | msg.value) / 10:g} BPM"

    return (f"No tempo reply from FL Studio within {timeout}s. Check that FL Studio's MIDI output "
            f"'{INPUT_PORT_NAME}' is enabled with the same port number as the Test Controller input.")

@mcp.tool()
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
@mcp.tool()
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
    
if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport='stdio')
