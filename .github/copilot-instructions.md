# OctoPrint Dremel 3D45 Plugin - AI Agent Instructions

## Project Overview
This is an OctoPrint plugin that enables network-based control of Dremel 3D45 printers via REST API, presenting as a **virtual serial connection**. The printer has no USB-serial support—all communication happens over HTTP.

## Architecture Pattern: Virtual Serial Transport
The plugin uses OctoPrint's **virtual serial factory hooks** (same pattern as the bundled `virtual_printer` plugin):
- `octoprint.comm.transport.serial.factory` - Creates virtual serial on port "DREMEL3D45"
- `octoprint.comm.transport.serial.additional_port_names` - Adds port to dropdown
- GCode commands (M105, M115, M24, etc.) are **translated** to REST API calls, not streamed

**Key files:**
- [octoprint_dremel3d45/__init__.py](../octoprint_dremel3d45/__init__.py) - Plugin entry, hooks, settings
- [octoprint_dremel3d45/virtual_serial.py](../octoprint_dremel3d45/virtual_serial.py) - GCode↔REST translation layer

## dremel3dpy Library Usage
All printer communication uses the [dremel3dpy](https://github.com/godely/dremel3dpy) library (v2.0.0+). Do NOT make direct REST calls.

```python
from dremel3dpy import Dremel3DPrinter

printer = Dremel3DPrinter(host)
printer.get_temperature_type("extruder")      # Current temp (31)
printer.get_temperature_type("platform")      # Use lowercase names
printer.get_temperature_attributes("extruder") # {"target_temp": 0, "max_temp": 280}
printer.is_printing() / is_paused() / is_ready()
printer.pause_print() / resume_print() / stop_print()
printer.start_print_from_file(filepath)       # Local file path (uploads + starts)
```

## Dremel API Field Mappings
The raw Dremel REST API has non-standard field names. The `dremel3dpy` library normalizes these, but if you need raw API access:
| Dremel API Field | Meaning |
|------------------|---------|
| `temperature` | Extruder current temp |
| `platform_temperature` | Bed current temp |
| `extruder_target_temperature` | Extruder target |
| `buildPlate_target_temperature` | Bed target |
| `elaspedtime` (typo!) | Elapsed time |
| `SN` | Serial number |
| `firmware_version` | Firmware version |

## GCode Handler Pattern
Add new GCode support via handler methods in `DremelVirtualSerial`. The plugin currently implements standard handlers (M106/M107/G90/G91 etc) to ensure compatibility with standard slicer outputs.

```python
def _gcode_M106(self, command: str) -> None:
    """Set fan speed. Format: M106 S<speed>"""
    # Dremel doesn't support fan control - just acknowledge
    self._send("ok")
```

Handlers are auto-discovered by `_process_command()` using `getattr(self, f"_gcode_{cmd}")`.

## Protocol Compatibility Notes
The virtual transport aims to behave like a typical Marlin/pyserial connection so that OctoPrint and other printer plugins work without special-casing:

- **Multi-line writes**: `write()` accepts multiple `\n`-separated commands.
- **Comment stripping**: supports `;` and `( ... )` comments.
- **Line numbers + checksums**: accepts `N123 ...*45` format and validates XOR checksums when provided.
- **Auto-report toggles**: supports `M155 S<sec>` (temp) and `M27 S<sec>` (SD status) to enable Marlin-style background reports.
- **pyserial helpers**: implements `read()`, `in_waiting`, `isOpen()`, and buffer reset/flush helpers.

## Testing
Run the connection test to verify printer communication:
```bash
python test_connection.py <printer_ip>
```
Tests: dremel3dpy library → Direct REST → Camera → Virtual serial translation

## Development Environment
```bash
cd /home/nbetcher/octoprint-dremel3d45
source .venv/bin/activate
pip install -e .  # Install in development mode
```

## Key Constraints
- **No motion control**: The Dremel doesn't support streaming G0/G1 commands. Position tracking is simulated.
- **No direct temp control**: M104/M140 are acknowledged but don't change temps—the print file handles heating.
- **No SD file listing**: `dremel3dpy` does not provide a directory listing. The plugin maintains a session-scoped index of files uploaded via OctoPrint.
- **Camera on port 10123**: Stream at `http://<ip>:10123/?action=stream`, snapshot at `?action=snapshot`

## Reference Implementation
The [ha-dremel-int/](../ha-dremel-int/) folder contains the Home Assistant integration for the same printer—useful for understanding sensor mappings and `dremel3dpy` usage patterns.
