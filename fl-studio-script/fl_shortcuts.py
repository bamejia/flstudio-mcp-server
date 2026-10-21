# fl_shortcuts.py - key names for keybinds.py, and FL Studio's own shortcuts
#
# Used by mcp_handlers.py to read the combos in keybinds.py and to switch off the
# ones FL Studio already uses. The shortcut list follows FL Studio's manual
# (Basics > Keyboard shortcuts); add any it misses to FL_SHORTCUTS.

import re

# Modifier name -> bit, in the order combos are written
MODIFIERS = {"ctrl": 1, "alt": 2, "shift": 4, "win": 8}
MODIFIER_ALIASES = {"control": "ctrl", "windows": "win"}

# Key name -> Windows virtual-key code (punctuation as on a US keyboard)
KEYS = {
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
    **{f"num{n}": 0x60 + n for n in range(10)},
    "num*": 0x6A, "num+": 0x6B, "num-": 0x6D, "num.": 0x6E, "num/": 0x6F,
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
KEY_ALIASES = {"escape": "esc", "return": "enter", "del": "delete", "ins": "insert",
               "pgup": "pageup", "pgdown": "pagedown", "pgdn": "pagedown"}
KEY_LIST = ("a-z 0-9 f1-f24 num0-num9 num* num+ num- num. num/ space enter tab esc backspace "
            "delete insert home end pageup pagedown up down left right ; = , - . / ` [ \\ ] '")

def parse_combo(combo):
    """'Ctrl+Alt+P' -> ('ctrl+alt+p', modifier bits, virtual-key code); ValueError if invalid"""
    mods, keys = 0, []
    for part in re.split(r"\+(?=.)", str(combo).strip().lower()):  # "+" ends "num+"
        part = part.strip()
        part = MODIFIER_ALIASES.get(part, part)
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        else:
            keys.append(KEY_ALIASES.get(part, part))
    if len(keys) != 1:
        raise ValueError(f"{combo!r}: use modifiers (ctrl, alt, shift, win) and one key, e.g. ctrl+alt+p")
    if keys[0] not in KEYS:
        raise ValueError(f"{combo!r}: unknown key {keys[0]!r}. Keys: {KEY_LIST}")
    names = [name for name, bit in MODIFIERS.items() if mods & bit] + keys
    return "+".join(names), mods, KEYS[keys[0]]

# window|combos (space-separated)|what FL Studio does
FL_SHORTCUTS_TEXT = """
global|ctrl+z|undo/redo
global|ctrl+alt+z|undo step by step
file|alt+0 alt+1 alt+2 alt+3 alt+4 alt+5 alt+6 alt+7 alt+8 alt+9|open recent file
file|ctrl+n|save new version
file|ctrl+o|open file
file|ctrl+r|export wave file
file|ctrl+s|save
file|ctrl+shift+s|save as
file|ctrl+shift+h|arrange windows
file|ctrl+shift+m|export MIDI file
file|ctrl+shift+r|export mp3 file
pattern selector|num1 num2 num3 num4 num5 num6 num7 num8 num9|select pattern 1-9
pattern selector|num+ num-|next/previous pattern
pattern selector|ctrl+f4 f4|next empty pattern
pattern selector|f2|rename pattern
channel rack|up down|select channel above/below
channel rack|pageup pagedown|select channel group
channel rack|alt+c|clone channels
channel rack|alt+delete|delete channels
channel rack|alt+up alt+down|move channels
channel rack|alt+g|group channels
channel rack|alt+m|mixer track selectors
channel rack|alt+u|unzip channels
channel rack|alt+z|zip channels
channel rack|ctrl+0 ctrl+1 ctrl+2 ctrl+3 ctrl+4 ctrl+5 ctrl+6 ctrl+7 ctrl+8 ctrl+9|solo channel
channel rack|ctrl+alt+r|restretch audio clips
channel rack|ctrl+c ctrl+v ctrl+x|copy/paste/cut steps
channel rack|ctrl+l|route channels to mixer
channel rack|ctrl+enter|focus playlist
channel rack|ctrl+shift+left ctrl+shift+right|shift steps
channel rack|shift+up shift+down|move channel selection
transport|num0|fast forward
transport|num* num/|next/previous bar
transport|space|start/stop
transport|ctrl+space|start/pause
transport|ctrl+b|blend notes
transport|ctrl+e|step edit mode
transport|ctrl+h|stop sound (panic)
transport|ctrl+i|wait for input
transport|ctrl+m|metronome
transport|ctrl+p|precount
transport|ctrl+t|typing keyboard
windows|enter|maximize playlist
windows|esc|close window
windows|tab|cycle windows
windows|f1|help
windows|f3|tool selector
windows|f5|playlist
windows|f6|channel rack
windows|f7|piano roll
windows|f8|plugin picker
windows|f9|mixer
windows|f10|MIDI settings
windows|f11|song info
windows|f12|close all windows
windows|alt+f8|browser
mixer|alt+l|select linked channels
mixer|alt+left alt+right|move track
mixer|alt+r|render armed tracks
mixer|alt+s|solo routed tracks
mixer|alt+w|wave view
mixer|ctrl+a|select all tracks
mixer|ctrl+shift+s|save track state
mixer|ctrl+shift+l|link channels from track
playlist|alt+num* alt+num/|next/previous song marker
playlist|alt+g|ungroup
playlist|alt+m alt+shift+m|mute/unmute selection
playlist|alt+p|picker panel
playlist|alt+t ctrl+t|add time marker
playlist|ctrl+a ctrl+d|select all/deselect
playlist|ctrl+alt+c ctrl+alt+shift+c|consolidate
playlist|ctrl+alt+g|grid color
playlist|ctrl+alt+home|resize from left
playlist|ctrl+b|duplicate selection
playlist|ctrl+c ctrl+v ctrl+x|copy/paste/cut
playlist|ctrl+delete ctrl+insert|delete/add space
playlist|ctrl+f8|project picker
playlist|ctrl+left ctrl+right|select time before/after
playlist|ctrl+enter|select time around selection
playlist|ctrl+p|performance mode
playlist|home|playback marker to start
playlist|insert|slice at cursor
playlist|delete|delete selection
playlist|backspace|global snap
playlist|pageup pagedown|zoom
playlist|shift+0 shift+1 shift+2 shift+3 shift+4 shift+5 shift+6|zoom
playlist|alt+shift+t|time signature
playlist|shift+c|select clips by source
playlist|shift+g|group clips
playlist|shift+i|invert selection
playlist|shift+left shift+right shift+up shift+down|move selection
playlist|shift+m|stretch mode
playlist|shift+q|quick quantize
piano roll|alt+a|arpeggiator
piano roll|alt+left alt+right alt+up alt+down|nudge notes
piano roll|alt+b|note helpers
piano roll|alt+c|note color
piano roll|alt+e|riff machine
piano roll|alt+f|flam
piano roll|alt+k|limit tool
piano roll|alt+l|articulate
piano roll|alt+n|waveform helper
piano roll|alt+o|LFO tool
piano roll|alt+q|quantize
piano roll|alt+r|randomize
piano roll|alt+s|strum
piano roll|alt+u|chop
piano roll|alt+v|ghost channels
piano roll|alt+w|claw machine
piano roll|alt+x|scale levels
piano roll|alt+y|flip
piano roll|ctrl+a ctrl+d|select all/deselect
piano roll|ctrl+b|duplicate selection
piano roll|ctrl+c ctrl+v ctrl+x|copy/paste/cut
piano roll|ctrl+delete ctrl+insert|delete/insert space
piano roll|ctrl+left ctrl+right|select time before/after
piano roll|ctrl+enter|select time around selection
piano roll|ctrl+alt+g|grid color
piano roll|ctrl+alt+home|resize from left
piano roll|shift+0 shift+1 shift+2 shift+3 shift+4 shift+5|zoom
piano roll|shift+left shift+right shift+up shift+down|move selection
piano roll|shift+c|select by color
piano roll|shift+g|group notes
piano roll|shift+i|invert selection
piano roll|shift+m|select note groups at random
piano roll|alt+g|ungroup notes
piano roll|alt+m alt+shift+m|mute/unmute selection
piano roll|alt+shift+t|time signature
piano roll|delete|delete selection
piano roll|pageup pagedown|zoom
piano roll|ctrl+g|glue notes
piano roll|ctrl+i|insert controller value
piano roll|ctrl+l|quick legato
piano roll|ctrl+m|import MIDI file
piano roll|ctrl+alt+q|quick quantize
piano roll|ctrl+alt+y|run last script (also used by get_notes)
piano roll|ctrl+up ctrl+down|transpose an octave
piano roll|ctrl+u|quick chop
piano roll|ctrl+shift+v|paste MIDI clipboard
piano roll|shift+d|discard note lengths
piano roll|shift+f|note properties
piano roll|shift+h shift+k|select next channel
piano roll|shift+n|turn off chord
piano roll|shift+r|select at random
misc|alt+num2 alt+num3 alt+num4 alt+num5 alt+num6|typing keyboard octave
misc|ctrl+alt+space alt+shift+space|text spaces
"""

def _read_shortcuts():
    shortcuts = {}
    for line in FL_SHORTCUTS_TEXT.strip().splitlines():
        window, combos, action = line.split("|")
        for combo in combos.split():
            name = parse_combo(combo)[0]
            use = f"{window}: {action}"
            shortcuts[name] = f"{shortcuts[name]}; {use}" if name in shortcuts else use
    return shortcuts

FL_SHORTCUTS = _read_shortcuts()  # canonical combo -> "window: action; ..."

def in_use(combo):
    """What FL Studio uses a canonical combo for, or None if it's free"""
    if combo in FL_SHORTCUTS:
        return FL_SHORTCUTS[combo]
    # Without modifiers FL Studio uses most keys itself (tool keys, the typing
    # keyboard); only F keys and numpad keys are left
    if "+" not in combo.rstrip("+") and not re.match(r"^(f\d+|num.)$", combo):
        return "plain keys: tools and the typing keyboard"
    return None
