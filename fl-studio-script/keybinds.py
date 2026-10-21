# keybinds.py - your own key combos for FL Studio
#
# While FL Studio is the focused window, the keyboard listener catches these
# combos and FL Studio runs the action. Save this file and switch back to FL
# Studio to apply changes. Messages (combos switched off, errors, your own
# print() output) appear in FL's View > Script output.
#
# Combo: modifiers + one key, e.g. "ctrl+alt+p", "ctrl+shift+f5", "alt+num1".
#   Modifiers: ctrl, alt, shift, win
#   Keys: a-z 0-9 f1-f24 num0-num9 num* num+ num- num. num/ space enter tab esc
#         backspace delete insert home end pageup pagedown up down left right
#         ; = , - . / ` [ \ ] '
#   A combo FL Studio already uses is switched off (the key keeps working as
#   usual in FL) and reported, so pick another one.
#
# Action, one of:
#   "stop"                          a request without arguments
#   ("play", {"mode": "song"})      a request with arguments
#   toggle_record                   your own function (no arguments), using FL's API
# Requests: play(mode="pattern"|"song", beat=<beats from the start>), pause, stop,
#   record(on=True|False)

import transport

def toggle_record():
    transport.record()
    print("record", "on" if transport.isRecording() else "off")

KEYBINDS = {
    # Examples: remove the # to turn one on
    # "ctrl+alt+shift+s": ("play", {"mode": "song", "beat": 0}),     # play the song from the start
    # "ctrl+alt+shift+p": ("play", {"mode": "pattern", "beat": 0}),  # play the pattern from the start
    # "ctrl+alt+shift+r": toggle_record,
}
