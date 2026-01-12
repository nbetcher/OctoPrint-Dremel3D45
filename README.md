# OctoPrint Dremel 3D45 Plugin

An OctoPrint plugin that enables network-based control of Dremel 3D45 printers via REST API, presenting as a **virtual serial connection**.

## Overview

The Dremel 3D45 printer has no USB-serial support—all communication happens over HTTP. This plugin creates a virtual serial port that translates standard Marlin GCode commands into Dremel REST API calls, allowing OctoPrint to control your printer over the network.

## Features

### Core Functionality
- **Virtual serial port** - Shows as "DREMEL3D45" in OctoPrint's connection dropdown
- **Temperature monitoring** - Real-time extruder, bed, and chamber temperatures
- **Temperature control** - Set nozzle and bed temperatures via M104/M140/M109/M190 (blocked during active printing)
- **Print control** - Start, pause, resume, and cancel prints
- **Progress tracking** - Print progress, elapsed time, remaining time, current layer
- **File upload** - Upload GCode files to the printer via OctoPrint's interface
- **Webcam integration** - Use the Dremel's built-in camera in OctoPrint

### GCode Command Categories

| Category | Commands | Notes |
|----------|----------|-------|
| **Temperature (read)** | M105 | Query current/target temps |
| **Temperature (write)** | M104, M109, M140, M155, M190 | Set temps via REST API; **blocked during active printing** (allowed when paused) |
| **Print Control** | M23, M24, M25, M27, M524 | Start/pause/resume/cancel; M24 resumes when paused |
| **Information** | M31, M73, M75-M77, M114, M115, M117-M119, M532 | Query status, progress, layer |
| **Motion** | G0, G1, G4, G10, G11, G28, G29, G90-G92 | **Acknowledged only** - Dremel doesn't support motion control via GCode |
| **Configuration** | M82, M83, M92, M106, M107, M110, M201-M205, M220, M221, M301, M304, M400-M420, M500-M503, M851 | Acknowledged for compatibility |
| **Miscellaneous** | M0, M1, M17, M18, M84, M108, M112, M211, M600, M862, M999, T0, T1 | Various no-ops and pause triggers |

## Installation

### Prerequisites

- OctoPrint 1.5.0 or newer
- Python 3.7+
- Dremel 3D45 printer on the same network

### Install from Source

```bash
# Clone the repository
git clone https://github.com/yourusername/octoprint-dremel3d45.git
cd octoprint-dremel3d45

# Install the local patched dremel3dpy library first
pip install -e ./dremel3dpy-local

# Install the plugin
pip install -e .

# Restart OctoPrint
sudo systemctl restart octoprint
```

### Install via OctoPrint Plugin Manager

*(Coming soon - once published to PyPI)*

## Configuration

1. Go to **Settings → Dremel 3D45** in OctoPrint
2. Enter your Dremel 3D45's IP address
3. (Optional) Enable camera integration
4. Save settings

## Connecting

1. Go to the **Connection** panel in OctoPrint
2. Select **DREMEL3D45** from the Serial Port dropdown
3. Click **Connect**

No USB cable is required—the plugin communicates over your local network.

## Camera Setup

The Dremel 3D45 has a built-in camera accessible at:
- **Stream**: `http://<printer_ip>:10123/?action=stream`
- **Snapshot**: `http://<printer_ip>:10123/?action=snapshot`

To use it in OctoPrint:
1. Enable camera in plugin settings
2. Check "Update Global Webcam" to automatically configure OctoPrint's webcam settings
3. Or manually configure the URLs in OctoPrint's webcam settings

## Limitations

Due to the Dremel 3D45's REST API design:

| Feature | Status | Notes |
|---------|--------|-------|
| **Motion control** | ❌ Not supported | G0/G1/G28 are acknowledged but ignored—Dremel handles motion internally |
| **Position tracking** | ❌ Not available | M114 reports 0,0,0,0 (layer number available during prints) |
| **Fan control** | ❌ No-op | Dremel API doesn't support M106/M107 |
| **Temperature control during printing** | ⚠️ Blocked | M104/M140 blocked during active print (allowed when paused) |
| **SD file listing** | ⚠️ Session-scoped | Plugin tracks files uploaded via OctoPrint only |
| **Streaming GCode** | ❌ Not supported | Printer only runs pre-uploaded files |

## SD Index

The Dremel API doesn't provide a file listing endpoint. The plugin maintains its own index of files uploaded through OctoPrint, mapping display names to the printer's internal upload names.

You can view and clear this index in **Settings → Dremel 3D45 → SD Index**.

## Security Notice

⚠️ The Dremel 3D45's network interface has **no authentication**. Only use this on a trusted local network.

## Troubleshooting

### "Cannot connect to DREMEL3D45"
- Verify the printer's IP address in settings
- Check that OctoPrint can reach the printer: `curl http://<printer_ip>/command -d GETPRINTERINFO`
- Ensure no firewall is blocking port 80

### Temperature targets show wrong values
The Dremel API reports incorrect target temperatures when heating via REST commands. The plugin works around this by tracking locally-set targets.

### Print progress stuck at 0%
Ensure you're using the plugin's SD card functionality (upload via OctoPrint) rather than printing from the printer's touchscreen.

## Development

```bash
# Set up development environment
python -m venv .venv
source .venv/bin/activate
pip install -e ./dremel3dpy-local
pip install -e .

# Run tests
python -m pytest tests/

# Test connection to printer
python test_connection.py <printer_ip>
```

## Architecture

The plugin uses OctoPrint's virtual serial factory hooks (same pattern as the bundled `virtual_printer` plugin):

```
OctoPrint ←→ DremelVirtualSerial ←→ dremel3dpy ←→ Dremel REST API
              (GCode translation)    (library)     (HTTP POST /command)
```

Key files:
- `octoprint_dremel3d45/__init__.py` - Plugin entry, hooks, settings
- `octoprint_dremel3d45/virtual_serial.py` - GCode↔REST translation layer

## License

MIT License - See [LICENSE](LICENSE) for details.

## Credits

- [dremel3dpy](https://github.com/godely/dremel3dpy) - Dremel 3D printer Python library by Gustavo Stor
- [OctoPrint](https://octoprint.org/) - The snappy web interface for 3D printers

## Contributing

Pull requests welcome! Please ensure:
- Code follows the existing style
- New GCode handlers have docstrings
- Tests pass
