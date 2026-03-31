# OctoPrint Dremel 3D45 Plugin — AI Agent Instructions

## Project Overview

OctoPrint plugin that bridges OctoPrint's serial/GCode world to the Dremel 3D45's HTTP REST API via a **virtual serial transport**. The Dremel has no USB-serial interface — all communication is HTTP POST to `http://<ip>/command`.

## Quick Reference

```bash
# Dev environment
cd /home/nbetcher/octoprint-dremel3d45
source .venv/bin/activate
pip install -e .          # Install in development mode

# Run tests
pytest                    # Unit tests (mocked printer)
python test_connection.py <printer_ip>  # Integration test against real printer
```

## Key Files

| File | Purpose |
|------|---------|
| `octoprint_dremel3d45/__init__.py` | Plugin entry, hooks, settings, local-print redirect |
| `octoprint_dremel3d45/virtual_serial.py` | **Core**: GCode↔REST translation, poll thread, state machine |
| `octoprint_dremel3d45/vendor/dremel3dpy/__init__.py` | Vendored REST client library (all HTTP calls) |
| `octoprint_dremel3d45/vendor/dremel3dpy/helpers/constants.py` | API field names, ports, timeouts |
| `tests/test_virtual_serial.py` | Unit tests with mocked `Dremel3DPrinter` |
| `ha-dremel-int/` | Home Assistant integration — reference for `dremel3dpy` usage patterns |

## Architecture: Virtual Serial Bridge

```
OctoPrint comm layer ──write(GCode)──► DremelVirtualSerial ──HTTP POST──► Dremel REST API
                      ◄──readline()──  (queue.Queue)       ◄──JSON────── (port 80/11134)
```

### Hook Registration (`__init__.py`)

| Hook | Purpose |
|------|---------|
| `serial.factory` | Returns `DremelVirtualSerial` when port is `DREMEL3D45` |
| `serial.additional_port_names` | Advertises `DREMEL3D45` in connection dropdown (only when IP configured) |
| `gcode.queuing` | Intercepts local file prints → uploads to Dremel + starts via REST |
| `estimation.remaining` | Feeds Dremel API remaining-time to OctoPrint's estimator |

### Threading Model

There are **three thread contexts** to be aware of:

1. **OctoPrint comm thread** — calls `write()` and `readline()` on the serial object. This is the main GCode processing path.
2. **Poll thread** (`dremel3d45.poll`, daemon) — runs `_refresh_status()` every `poll_interval` seconds. Updates cached temps, job phase, progress. Emits auto-report lines and SD progress.
3. **Local print redirect thread** (`DremelLocalPrintRedirect`, daemon) — spawned by `gcode_queuing_hook` for upload+start of local files.

**Shared state** between threads is guarded by `self._lock` (an `RLock`). The outgoing `queue.Queue` is thread-safe by design.

---

## GCode Handler System

### Dispatch Pattern

`_process_command()` dispatches via `getattr(self, f"_gcode_{cmd}")`. Unknown commands get a silent `ok`.

### Handler Categories

**Every handler MUST call `self._send("ok")` (or an error + ok) before returning.** OctoPrint's comm layer waits for the acknowledgement; a missing `ok` will stall the comm timeout and eventually disconnect.

#### 1. REST-Backed Handlers (make HTTP calls)

These handlers issue **synchronous** `requests.post()` calls to the Dremel. They block the OctoPrint comm thread for the duration of the HTTP round-trip.

| Handler | REST call | Notes |
|---------|-----------|-------|
| `M115` | `GETPRINTERINFO` (via `set_printer_info`) | Connection handshake — sends FIRMWARE_NAME + Cap: lines |
| `M104` | `NOZZLEHEAT=<T>` or `STOPNOZZLEHEAT` | Extruder temp; clamped 0–280; blocked during active print |
| `M109` | Same as M104 | Sets temp but does NOT block (OctoPrint will poll M105) |
| `M140` | `PLATEHEAT=<T>` or `STOPPLATEHEAT` | Bed temp; clamped 0–100; blocked during active print |
| `M190` | Same as M140 | Sets temp but does NOT block |
| `M24` | `PRINT=<filename>` or `RESUME` | Start or resume print |
| `M25` | `PAUSE` | Pause print |
| `M524` | `CANCEL` | Abort print |
| `M112` | `CANCEL` | Emergency stop |
| `M119` | `is_door_open()` (reads cached job status) | Refreshes door state |

**Concurrency concern:** These synchronous HTTP calls hold up `write()` → `_process_command()`, which runs on OctoPrint's comm thread. The comm thread cannot send or receive other commands until the handler returns. The Dremel's REST API typically responds in 100–500ms, so this is acceptable for command frequency. However:

- If the printer is unreachable (network timeout), the `REQUEST_TIMEOUT` (default 30s, configurable) will block the comm thread for up to that duration. OctoPrint may report a communication timeout.
- The `default_request()` function in the vendored library uses `requests.post()` with `timeout=REQUEST_TIMEOUT` — there is no async path.

#### 2. Cache-Only Handlers (no HTTP, read from poll cache)

These respond instantly from state cached by the poll thread.

| Handler | Data source |
|---------|-------------|
| `M105` | `self._temps` dict |
| `M27` | `self._progress`, `self._selected_file_size` |
| `M114` | Static/layer from `self._current_layer` |
| `M31` | `self._elapsed_time` |
| `M73` | Writes to `self._progress` (host-set, overridden by next poll) |
| `M532` | `self._progress`, `self._current_layer` |

#### 3. No-Op Handlers (acknowledge only)

Motion, stepper, fan, feedrate, and hardware commands the Dremel doesn't support: `G0`, `G1`, `G28`, `G90`, `G91`, `M17`, `M18`, `M82`, `M83`, `M84`, `M106`, `M107`, `M220`, `M221`, `M400`, `G4`, `G92`, etc.

These exist so OctoPrint (and slicer-generated GCode) don't produce "unknown command" noise.

### Adding a New Handler

```python
def _gcode_M999(self, command: str) -> None:
    """Restart after fault (no-op)."""
    self._send("ok")
```

For REST-backed handlers, follow the M104 pattern: parse args with `re.search`, call `default_request()` in a try/except, send error + ok on failure.

---

## REST API Call Patterns

### The `default_request()` Function

All Dremel HTTP communication funnels through `vendor/dremel3dpy/__init__.py:default_request()`:

```python
default_request(host, command, scheme="http", port=80, path="/command")
```

- Uses `requests.post()` with form-encoded body
- Timeout: `REQUEST_TIMEOUT` (module-level global, patched at runtime)
- SSL verification disabled (Dremel HTTPS certs are commonly invalid)
- Returns parsed JSON dict
- Raises `RuntimeError` on HTTP or API-level errors

### Dremel API Endpoints Used

| Endpoint | Method | Used for |
|----------|--------|----------|
| `POST /command` body=`GETPRINTERINFO` | Status | Firmware version, serial, model |
| `POST /command` body=`GETPRINTERSTATUS` | Polling | Temps, progress, job phase, door, filament |
| `POST /command` body=`PRINT=<name>` | Action | Start print from uploaded file |
| `POST /command` body=`PAUSE` | Action | Pause current print |
| `POST /command` body=`RESUME` | Action | Resume paused print |
| `POST /command` body=`CANCEL` | Action | Cancel/stop print |
| `POST /command` body=`NOZZLEHEAT=<T>` | Action | Set extruder temperature |
| `POST /command` body=`STOPNOZZLEHEAT` | Action | Turn off extruder heater |
| `POST /command` body=`PLATEHEAT=<T>` | Action | Set bed temperature |
| `POST /command` body=`STOPPLATEHEAT` | Action | Turn off bed heater |
| `POST /print_file_uploads` | Upload | Multipart file upload |
| `GET https://<ip>:11134/getHomeMessage` | Extra | Max temps, storage, usage counter |

### Concurrency & Deadlock Prevention

1. **Poll thread vs comm thread**: Both access `self._printer` and shared state. `self._lock` (RLock) guards state mutations. The poll thread holds the lock briefly for state snapshots; the comm thread holds it for state reads/writes in handlers. Neither thread calls the other's blocking operations while holding the lock.

2. **No lock held during HTTP calls**: REST calls in handlers (`default_request()`, `printer.pause_print()`, etc.) happen **outside** the lock scope. The lock is only acquired to read/write local state before or after the network call.

3. **Queue-based decoupling**: Responses go into `self._outgoing` (a `queue.Queue`), which is inherently thread-safe. The poll thread and comm thread both call `self._send()` freely.

4. **Graceful degradation on network errors**: `_refresh_status()` catches exceptions and increments `_connection_errors`. After 4 consecutive failures, the printer is treated as offline and stale printing state is cleared to avoid OctoPrint getting stuck in a phantom "printing" state.

### Efficiency Observations

- **Poll thread makes 1–3 HTTP calls per cycle**: `set_job_status` (required), `set_printer_info` (non-fatal), `set_extra_status` (non-fatal, HTTPS port 11134). If any fails, the others still proceed.
- **M105 is free**: Temperature reports come from the poll cache — zero network calls.
- **M115 forces a synchronous refresh**: Only happens once during handshake.
- **M104/M140/M109/M190 each make one HTTP call**: Synchronous on the comm thread.
- **The Dremel REST API is single-threaded**: Concurrent requests may serialize or fail. The poll interval (default 10s) keeps background polling sparse enough to avoid conflicts with user-triggered commands.

---

## State Machine: Print Lifecycle

The poll thread drives state transitions by reading `job_phase` from the Dremel API:

```
idle ──► preparing ──► building ──► completed ──► idle
                         │    ▲
                         ▼    │
                       pausing ──► paused ──► resuming
                                      │
                                      ▼
                                    abort ──► idle
```

Key transitions in `_refresh_status()`:
- **idle/terminal → active**: Emits `File opened:` + `File selected` to OctoPrint
- **active → terminal (completed/abort)**: Emits `SD printing byte total/total` + `Not SD printing`
- **Late job name discovery**: Re-announces `File opened:` if placeholder was used

### Local Print Redirect

OctoPrint streams GCode line-by-line for local files. The Dremel **cannot** execute streamed GCode. The `gcode_queuing_hook` intercepts:
1. First `source:file` command sets `_local_print_redirecting = True`
2. All subsequent file commands return `(None,)` (suppressed)
3. Background thread uploads file via `upload_file()` → starts via `PRINT=<name>`
4. Script commands (`source:script`) are also suppressed during redirect to prevent temperature interference
5. After 5s grace period, flag is cleared

---

## Protocol Compatibility

The virtual transport emulates enough Marlin serial protocol for OctoPrint's comm layer:

| Feature | Implementation |
|---------|---------------|
| `M110 N0` hello + reset | Resets line counter; exempt from sequence check |
| Line numbers `N123 cmd*45` | XOR checksum validation; `Resend:` on mismatch |
| `M115` firmware identification | `FIRMWARE_NAME:Dremel3D45`, `Cap:AUTOREPORT_TEMP:1`, etc. |
| `M155 S<n>` auto-report temp | Poll thread sends periodic temp lines (no `ok` prefix) |
| SD progress `SD printing byte X/Y` | Poll thread during active prints; uses synthetic 1MB file size |
| `M73 P<pct> R<min>` | Poll thread emits for OctoPrint 1.9+ progress display |
| `// action:notification Layer N` | Poll thread for DisplayLayerProgress plugin compat |
| Emergency `M112` | Bypasses queue, sends CANCEL to Dremel |
| `start` banner | Sent on connection; capabilities sent in response to M115 only |

### Connection Handshake Sequence

1. Plugin sends `\n` + `start`
2. OctoPrint sends `M110 N0` → plugin responds `ok`
3. OctoPrint sends `M115` → plugin responds with `FIRMWARE_NAME:...` + capabilities + `ok`
4. OctoPrint sends `M21` (SD init) → plugin responds `ok`
5. OctoPrint sends `M105` → plugin responds from cache `ok T:... B:...`
6. Connection established → OctoPrint enters Operational state

---

## Dremel API Field Mappings

The raw REST API uses non-standard names. The `dremel3dpy` library normalizes them:

| Dremel API Field | Normalized Name | Meaning |
|------------------|-----------------|---------|
| `temperature` | `extruder_temperature` | Extruder current temp |
| `platform_temperature` | `platform_temperature` | Bed current temp |
| `extruder_target_temperature` | `extruder_target_temperature` | Extruder target |
| `buildPlate_target_temperature` | `platform_target_temperature` | Bed target |
| `elaspedtime` (typo in API!) | `elapsed_time` | Elapsed print time |
| `jobstatus` | `job_status` | Current job phase |
| `jobname` | `job_name` | Current job filename |
| `SN` | via `get_serial_number()` | Printer serial number |

---

## Key Constraints

- **No motion control**: G0/G1/G28 are no-ops. The Dremel cannot execute streamed GCode movements.
- **Temperature control only when idle/paused**: M104/M140 are blocked during active prints (`_is_print_active()` guard).
- **No SD file listing**: M20 is not implemented. The plugin tracks files uploaded via OctoPrint in session state.
- **Single-extruder only**: `EXTRUDER_COUNT:1` in M115.
- **Camera on port 10123**: `http://<ip>:10123/?action=stream` and `?action=snapshot`.
- **HTTPS port 11134**: Extra status (max temps, storage). Uses invalid/expired certificates — SSL verification is disabled.
- **dremel3dpy is vendored**: Lives in `octoprint_dremel3d45/vendor/dremel3dpy/`. Do NOT use PyPI `dremel3dpy`. The vendored copy has local patches.
- **`REQUEST_TIMEOUT` is a module-level global**: Must be patched in **both** `dremel3dpy` and `dremel3dpy.helpers.constants` at runtime to take effect.

---

## Testing

Tests use `unittest.mock` to mock `Dremel3DPrinter`. The mock is patched at import time in `setUp()`.

```python
# Run all tests
pytest

# Run specific test
pytest tests/test_virtual_serial.py -k "test_M105"

# Integration test (requires real printer)
python test_connection.py 192.168.1.xxx
```

When testing GCode handlers:
- Use `_send_command(cmd)` helper which calls `write()` + drains responses
- Drain startup messages in `setUp()` before sending test commands
- Mock printer methods on `self.mock_printer` to control return values

## Reference Implementations

- **OctoPrint's bundled `virtual_printer`**: Canonical reference for the serial transport contract.
- **`ha-dremel-int/`**: Home Assistant integration for same printer — useful for sensor mappings and `dremel3dpy` patterns.
- **Skills**: `octoprint-skill` (references/virtual-printer.md, references/dremel-rest-interface.md) and `3d-printer-gcode-reference` SKILL.md for authoritative OctoPrint serial protocol behavior.
