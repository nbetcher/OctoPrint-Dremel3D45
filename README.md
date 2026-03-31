# OctoPrint Dremel 3D45 Plugin

An OctoPrint plugin that enables network-based control of Dremel 3D45 printers via REST API, presenting as a **virtual serial connection**. Version 1.0.0.

## Overview

The Dremel 3D45 printer has no USB-serial support—all communication happens over HTTP. This plugin creates a virtual serial port (`DREMEL3D45`) that translates standard Marlin GCode commands into Dremel REST API calls, allowing OctoPrint to control your printer over the network.

When OctoPrint (or a slicer like Dremel 3D Slicer) sends a local file to print, the plugin automatically intercepts the streamed GCode, uploads the file to the Dremel, and starts the print via the printer's native REST workflow. This happens transparently—you just click "Print" and it works.

## Features

### Core Functionality
- **Virtual serial port** — Appears as `DREMEL3D45` in OctoPrint's connection dropdown
- **Temperature monitoring** — Real-time extruder, bed, and chamber temperatures with Marlin-format reporting
- **Temperature control** — Set nozzle (0–280 °C) and bed (0–100 °C) temperatures via M104/M140/M109/M190 (blocked during active printing, allowed when paused)
- **Print control** — Start, pause, resume, and cancel prints via M24/M25/M524
- **Progress tracking** — Print progress, elapsed time, remaining time, current layer, and total layers
- **Local print redirect** — Automatically uploads and starts local files on the Dremel when OctoPrint tries to stream GCode
- **Print time estimation** — Provides Dremel API–sourced remaining time estimates to OctoPrint
- **Webcam integration** — Use the Dremel's built-in camera (port 10123) in OctoPrint
- **Marlin protocol compliance** — Line numbering, checksums, auto-temp reporting, M115 capability declarations

### Supported GCode Commands

| Category | Commands | Notes |
|----------|----------|-------|
| **Temperature (read)** | M105 | Reports `T:<ext> /<target> B:<bed> /<target> C:<chamber>` |
| **Temperature (write)** | M104, M109, M140, M155, M190 | Sets temps via REST API; **blocked during active printing** (allowed when paused) |
| **Print Control** | M24, M25, M27, M524 | Start/pause/resume/cancel; M24 resumes when paused |
| **Information** | M31, M73, M75–M77, M114, M115, M117–M119, M532 | Status, progress, firmware info, endstop/door state |
| **Motion** | G0, G1, G4, G10, G11, G28, G29, G90–G92 | **Acknowledged only** — Dremel doesn't support streamed motion control |
| **Configuration** | M82, M83, M106, M107, M110, M201, M205, M220, M221, M301, M304, M400, M420, M500, M503, M851 | Acknowledged for slicer compatibility |
| **Miscellaneous** | M0, M1, M108, M112, M600, M862, M999, T0, T1 | Emergency stop, pause triggers, tool select |

### Marlin Protocol Features
- **Line numbering & checksums** — Validates `N<n> <cmd>*<checksum>` format with XOR checksums
- **Auto-report temperature** — `M155 S<seconds>` enables periodic M105 reports
- **M115 capabilities** — Declares `AUTOREPORT_TEMP`, `EMERGENCY_PARSER`, etc.
- **Comment stripping** — Handles `;` and `(...)` comment styles
- **Emergency cancel** — Ctrl-X (0x18) support

## Installation

### Prerequisites

- OctoPrint 1.5.0 or newer
- Python 3.7+
- Dremel 3D45 printer on the same network

### Install from Source

```bash
# Navigate to your OctoPrint virtual environment
source ~/oprint/bin/activate  # adjust path to your OctoPrint venv

# Install the plugin (dremel3dpy is vendored—no heavy dependencies)
pip install https://github.com/nbetcher/octoprint-dremel3d45/archive/main.zip

# Restart OctoPrint
sudo systemctl restart octoprint
```

### Install via OctoPrint Plugin Manager

In OctoPrint, go to **Settings → Plugin Manager → Get More → ...from URL** and enter:

```
https://github.com/nbetcher/octoprint-dremel3d45/archive/main.zip
```

### Install for Development

```bash
git clone https://github.com/nbetcher/octoprint-dremel3d45.git
cd octoprint-dremel3d45
pip install -e .
```

## Configuration

1. Go to **Settings → Dremel 3D45** in OctoPrint
2. Enter your Dremel 3D45's IP address
3. (Optional) Adjust request timeout (default: 30s) and poll interval (default: 10s)
4. (Optional) Enable camera integration
5. Save settings
6. Use the **Test Connection** button to verify connectivity

### Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| Printer IP | *(empty)* | IP address of your Dremel 3D45 (required) |
| Request Timeout | 30s | HTTP request timeout for REST API calls |
| Poll Interval | 10s | How often to poll the printer for status updates |
| Camera Enabled | Off | Enable the Dremel's built-in camera in OctoPrint |
| Update Global Webcam | Off | Automatically configure OctoPrint's webcam URLs |

## Connecting

1. Go to the **Connection** panel in OctoPrint
2. Select **DREMEL3D45** from the Serial Port dropdown
3. Click **Connect**

No USB cable is required—the plugin communicates over your local network.

## Printing

### From OctoPrint (recommended)
Upload a `.gcode` file via OctoPrint's UI, then click **Print**. The plugin uploads the file to the Dremel and starts the print via REST API. Progress, temperature, and layer information are reported back to OctoPrint in real time.

### From a Slicer (e.g., Dremel 3D Slicer, Cura)
If your slicer sends jobs to OctoPrint via its REST API, the plugin automatically detects the local file print, suppresses the streamed GCode, uploads the file to the Dremel, and starts the print natively. A notification appears in OctoPrint's UI during this redirect process.

### From the Printer's Touchscreen

## Camera Setup

The Dremel 3D45 has a built-in camera accessible at:
- **Stream**: `http://<printer_ip>:10123/?action=stream`
- **Snapshot**: `http://<printer_ip>:10123/?action=snapshot`

To use it in OctoPrint:
1. Enable camera in plugin settings
2. Check **Update Global Webcam** to automatically configure OctoPrint's webcam settings
3. Or manually configure the URLs in OctoPrint's webcam settings

The plugin supports both the modern `classicwebcam` plugin and legacy OctoPrint webcam settings.

## Limitations

Due to the Dremel 3D45's REST API design:

| Feature | Status | Notes |
|---------|--------|-------|
| **Motion control** | ❌ Not supported | G0/G1/G28 are acknowledged but ignored—Dremel handles motion internally |
| **Position tracking** | ❌ Not available | M114 reports 0,0,0,0 (layer number available during prints) |
| **Fan control** | ❌ No-op | Dremel API doesn't expose fan control |
| **Temperature during printing** | ⚠️ Blocked | M104/M140 blocked during active print (allowed when paused) |
| **SD file listing** | ❌ Not available | Dremel API provides no file listing endpoint |
| **Streaming GCode** | 🔄 Auto-redirected | Local prints are intercepted and redirected to Dremel's upload-and-print workflow |

## Security Notice

⚠️ The Dremel 3D45's network interface has **no authentication**. Only use this on a trusted local network. The plugin's frontend escapes all user-supplied data to prevent XSS.

## Troubleshooting

### "Cannot connect to DREMEL3D45"
- Verify the printer's IP address in plugin settings
- Check that OctoPrint can reach the printer: `curl http://<printer_ip>/command -d GETPRINTERINFO`
- Ensure no firewall is blocking port 80
- Use the **Test Connection** button in settings to diagnose

### Temperature targets show wrong values
The Dremel API reports incorrect target temperatures when heating via REST commands. The plugin works around this by tracking locally-set targets.

### Print progress stuck at 0%
Ensure you're printing through OctoPrint (upload and print) rather than starting a print from the printer's touchscreen after it was already loaded.

### "Uploading to Dremel" notification appears
This is normal—when you print a local file, the plugin uploads it to the Dremel first. The notification disappears when the print starts.

## Development

```bash
# Set up development environment
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Run tests (116 tests)
python -m pytest tests/

# Run tests with verbose output
python -m pytest tests/ -v

# Test connection to a real printer
python test_connection.py <printer_ip>
```

## Architecture

The plugin uses OctoPrint's virtual serial factory hooks (same pattern as the bundled `virtual_printer` plugin):

```
OctoPrint ←→ DremelVirtualSerial ←→ dremel3dpy ←→ Dremel REST API
              (GCode translation)    (vendored)    (HTTP POST /command)
```

### OctoPrint Hooks

| Hook | Purpose |
|------|---------|
| `octoprint.comm.transport.serial.factory` | Creates `DremelVirtualSerial` when connecting to `DREMEL3D45` port |
| `octoprint.comm.transport.serial.additional_port_names` | Adds `DREMEL3D45` to the port dropdown |
| `octoprint.comm.protocol.gcode.queuing` | Intercepts local file prints and redirects to Dremel's native workflow |
| `octoprint.printer.estimation.remaining` | Provides print time estimate from Dremel's API |

### Key Files

| File | Purpose |
|------|---------|
| [octoprint_dremel3d45/\_\_init\_\_.py](octoprint_dremel3d45/__init__.py) | Plugin entry, hooks, settings, local print redirect |
| [octoprint_dremel3d45/virtual_serial.py](octoprint_dremel3d45/virtual_serial.py) | GCode↔REST translation layer |
| [octoprint_dremel3d45/vendor/dremel3dpy/](octoprint_dremel3d45/vendor/dremel3dpy/) | Vendored Dremel API library (avoids NumPy/OpenBLAS deps) |
| [octoprint_dremel3d45/static/js/dremel3d45.js](octoprint_dremel3d45/static/js/dremel3d45.js) | Knockout.js ViewModel for settings UI and notifications |
| [octoprint_dremel3d45/templates/dremel3d45_settings.jinja2](octoprint_dremel3d45/templates/dremel3d45_settings.jinja2) | Settings panel template |

### Plugin Mixins

`StartupPlugin` · `ShutdownPlugin` · `EventHandlerPlugin` · `SettingsPlugin` · `SimpleApiPlugin` · `TemplatePlugin` · `AssetPlugin`

## License

MIT License — See [LICENSE](LICENSE) for details.

## Credits

- [dremel3dpy](https://github.com/godely/dremel3dpy) — Dremel 3D printer Python library by Gustavo Stor
- [OctoPrint](https://octoprint.org/) — The snappy web interface for 3D printers

## Contributing

Pull requests welcome! Please ensure:
- All 116 tests pass (`python -m pytest tests/`)
- New GCode handlers have docstrings
- Code follows the existing style
