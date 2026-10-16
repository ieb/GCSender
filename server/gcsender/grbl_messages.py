"""Human help for GRBL errors, alarms and settings (0.9j and 1.1).

0.9j prints most errors as text ("error: Bad number format") and the rest as
"error: Invalid gcode ID:NN"; 1.1 prints numbers ("error:2"). Both map onto
the same code table, which 0.9j and 1.1 share for codes 1-12 and 20-37.
"""

from __future__ import annotations

import re
from typing import Any

ERRORS: dict[int, tuple[str, str]] = {
    1: ("Expected command letter", "G-code words consist of a letter and a value. The letter was not found."),
    2: ("Bad number format", "A numeric value is missing or not valid."),
    3: ("Invalid statement", "The '$' system command was not recognised or is not supported by this firmware."),
    4: ("Value < 0", "A negative value was given where a positive value is required."),
    5: ("Setting disabled", "Homing is not enabled ($22=0). This machine has no home switches, so $H is unavailable."),
    6: ("Value < 3 usec", "The minimum step pulse time must be at least 3 microseconds."),
    7: ("EEPROM read fail", "EEPROM read failed; GRBL restored default settings. Check all settings."),
    8: ("Not idle", "This '$' command can only be used while GRBL is Idle (or in Alarm). Wait for motion to finish."),
    9: ("Alarm lock", "G-code is locked out during an alarm (or jog). Unlock with $X (or home) after checking the machine."),
    10: ("Homing not enabled", "Soft limits cannot be enabled without homing also being enabled."),
    11: ("Line overflow", "The line is longer than GRBL's line buffer (80 characters). It was not executed."),
    12: ("Step rate too high", "A setting value exceeds the maximum step rate supported."),
    13: ("Safety door", "The safety door was detected as open."),
    14: ("Line length exceeded", "The build info or startup line exceeds the EEPROM line length limit."),
    15: ("Travel exceeded", "The jog target exceeds machine travel; the command was ignored."),
    16: ("Invalid jog command", "Jog command has no '=' or contains prohibited G-code."),
    17: ("Laser mode requires PWM", "Laser mode requires a PWM spindle output."),
    20: ("Unsupported command", "An unsupported or invalid G-code command was found in the block."),
    21: ("Modal group violation", "More than one command from the same modal group was found in the block."),
    22: ("Undefined feed rate", "The feed rate has not been set. Add an F word (e.g. F300) to the move."),
    23: ("Command value not integer", "The G-code command requires an integer value."),
    24: ("Axis command conflict", "Two G-code commands that both need the XYZ axis words were in the same block."),
    25: ("Word repeated", "A G-code word was repeated in the block."),
    26: ("No axis words", "The command requires XYZ axis words in the block, but none were found."),
    27: ("Invalid line number", "The N line number is not within the valid range 1 - 9,999,999."),
    28: ("Value word missing", "The command is missing a required P or L value word."),
    29: ("Unsupported coordinate system", "Only G54-G59 are supported (G59.1-G59.3 are not)."),
    30: ("G53 invalid motion mode", "G53 requires G0 or G1 motion mode to be active."),
    31: ("Axis words exist", "There are unused axis words in the block and G80 (motion cancel) is active."),
    32: ("No axis words in plane", "A G2/G3 arc has no XYZ axis words in the selected plane."),
    33: ("Invalid target", "The motion target is invalid: the arc is impossible, or the probe target is the current position."),
    34: ("Arc radius error", "Arc geometry failed for an R-format arc. Split the arc or use I/J/K offsets."),
    35: ("No offsets in plane", "A G2/G3 arc (offset format) is missing I/J/K words for the selected plane."),
    36: ("Unused words", "There are leftover G-code words that no command in the block used."),
    37: ("G43.1 axis error", "G43.1 dynamic tool length offset can only be applied to the Z axis."),
    38: ("Invalid tool number", "The tool number is greater than the maximum supported value."),
}

# 0.9 alarm codes differ from 1.1 - they are identified by text.
ALARMS_09: dict[str, tuple[int, str]] = {
    "Hard limit": (1, "A limit switch was triggered. Machine position is likely lost; re-zero before continuing."),
    "Soft limit": (2, "A motion target exceeded machine travel. Position is retained; unlock with $X."),
    "Abort during cycle": (3, "Reset while moving: position may be lost (missed steps). Check and re-zero, then unlock with $X."),
    "Probe fail": (4, "The probe did not make contact within the programmed travel (or was already triggered). Check the probe clip and plate, then unlock with $X."),
    "Homing fail": (5, "The homing cycle failed to find the switches."),
}
ALARMS_11: dict[int, tuple[str, str]] = {
    1: ("Hard limit", "A limit switch was triggered. Machine position is likely lost; re-homing is recommended."),
    2: ("Soft limit", "A motion target exceeds machine travel. Position is retained; unlock with $X."),
    3: ("Abort during cycle", "Reset while in motion: position may be lost. Re-home or re-zero, then unlock with $X."),
    4: ("Probe fail", "The probe was not in the expected initial state before the probe cycle (already touching?)."),
    5: ("Probe fail", "The probe did not contact the workpiece within the programmed travel."),
    6: ("Homing fail", "Reset during an active homing cycle."),
    7: ("Homing fail", "The safety door was opened during the homing cycle."),
    8: ("Homing fail", "Homing could not clear the limit switch when pulling off. Increase the pull-off ($27) or check wiring."),
    9: ("Homing fail", "Homing could not find the limit switch within the search distance."),
}

_ERR_09_TEXT = {title.lower(): code for code, (title, _) in ERRORS.items()}
_ERR_09_TEXT["eeprom read fail. using defaults"] = 7
_ERR_09_TEXT["busy or queued commands"] = 8


def explain_error(text: str, code: int | None) -> dict[str, Any]:
    """``text``/``code`` as produced by protocol.classify for an error line."""
    if code is None:
        m = re.match(r"Invalid gcode ID:(\d+)", text)
        if m:
            code = int(m.group(1))
        else:
            code = _ERR_09_TEXT.get(text.lower())
    title, help_text = ERRORS.get(code, (text or "Error", "")) if code is not None else (text or "Error", "")
    return {"code": code, "title": title, "help": help_text}


def explain_alarm(text: str, code: int | None) -> dict[str, Any]:
    if code is not None:
        title, help_text = ALARMS_11.get(code, (f"Alarm {code}", ""))
        return {"code": code, "title": title, "help": help_text}
    for key, (c, help_text) in ALARMS_09.items():
        if text.lower().startswith(key.lower()):
            return {"code": c, "title": key, "help": help_text}
    return {"code": None, "title": text or "Alarm", "help": ""}


# id: (name, unit, kind, help) - kind: int | float | bool | mask
SETTINGS: dict[int, tuple[str, str, str, str]] = {
    0: ("Step pulse", "µs", "int", "Length of the step pulse sent to the stepper drivers."),
    1: ("Step idle delay", "ms", "int", "How long steppers stay enabled after motion. 255 keeps them always enabled (holds position)."),
    2: ("Step port invert", "", "mask", "Inverts the step signal per axis (bit 0=X, 1=Y, 2=Z)."),
    3: ("Direction invert", "", "mask", "Reverses an axis direction (bit 0=X, 1=Y, 2=Z)."),
    4: ("Step enable invert", "", "bool", "Inverts the stepper enable pin."),
    5: ("Limit pins invert", "", "bool", "Inverts the limit switch inputs."),
    6: ("Probe pin invert", "", "bool", "Inverts the probe input."),
    10: ("Status report mask", "", "mask", "0.9: bit0 MPos, bit1 WPos, bit2 planner buffer, bit3 RX buffer, bit4 limit pins. "
         "1.1: bit0 MPos (else WPos), bit1 buffer data. GCSender needs MPos and WPos on 0.9 ($10=3)."),
    11: ("Junction deviation", "mm", "float", "Cornering speed: higher is faster through corners but rougher."),
    12: ("Arc tolerance", "mm", "float", "Accuracy of arc segmentation."),
    13: ("Report inches", "", "bool", "Report positions in inches. GCSender expects millimetres ($13=0)."),
    20: ("Soft limits", "", "bool", "Reject moves beyond the max travel ($130-$132). Requires homing."),
    21: ("Hard limits", "", "bool", "Stop immediately when a limit switch triggers."),
    22: ("Homing cycle", "", "bool", "Enable $H. Requires home switches."),
    23: ("Homing dir invert", "", "mask", "Homing direction per axis."),
    24: ("Homing feed", "mm/min", "float", "Slow locate feed rate during homing."),
    25: ("Homing seek", "mm/min", "float", "Fast search rate during homing."),
    26: ("Homing debounce", "ms", "int", "Switch debounce delay."),
    27: ("Homing pull-off", "mm", "float", "Distance to back off the switches after homing."),
    30: ("Max spindle speed", "RPM", "float", "Spindle speed at 100 % PWM (1.1)."),
    31: ("Min spindle speed", "RPM", "float", "Spindle speed at minimum PWM (1.1)."),
    32: ("Laser mode", "", "bool", "Laser mode: no stop on spindle-speed changes (1.1)."),
    100: ("X steps/mm", "steps/mm", "float", "Motor steps per millimetre of X travel."),
    101: ("Y steps/mm", "steps/mm", "float", "Motor steps per millimetre of Y travel."),
    102: ("Z steps/mm", "steps/mm", "float", "Motor steps per millimetre of Z travel."),
    110: ("X max rate", "mm/min", "float", "Maximum X speed (also used for rapids)."),
    111: ("Y max rate", "mm/min", "float", "Maximum Y speed (also used for rapids)."),
    112: ("Z max rate", "mm/min", "float", "Maximum Z speed (also used for rapids)."),
    120: ("X acceleration", "mm/s²", "float", "X axis acceleration."),
    121: ("Y acceleration", "mm/s²", "float", "Y axis acceleration."),
    122: ("Z acceleration", "mm/s²", "float", "Z axis acceleration."),
    130: ("X max travel", "mm", "float", "X travel length (used for soft limits and jog bounds)."),
    131: ("Y max travel", "mm", "float", "Y travel length (used for soft limits and jog bounds)."),
    132: ("Z max travel", "mm", "float", "Z travel length (used for soft limits and jog bounds)."),
}


def setting_meta(sid: int) -> dict[str, Any]:
    name, unit, kind, help_text = SETTINGS.get(sid, (f"${sid}", "", "float", ""))
    return {"name": name, "unit": unit, "kind": kind, "help": help_text}
