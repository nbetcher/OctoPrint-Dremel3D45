# -*- coding: utf-8 -*-
"""
Dremel 3D45 Virtual Serial Transport.

This module provides a virtual serial port that translates standard Marlin GCode
commands into Dremel 3D45 REST API calls, following OctoPrint's plugin guidelines.

The pattern is based on OctoPrint's bundled virtual_printer plugin.

Usage:
    OctoPrint connects to port "DREMEL3D45" and communicates via standard GCode.
    This class translates those commands to REST API calls and returns
    Marlin-compatible responses.

Hooks used:
    - octoprint.comm.transport.serial.additional_port_names
    - octoprint.comm.transport.serial.factory
"""

from __future__ import annotations

import logging
import json
import os
import queue
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

from dremel3dpy import Dremel3DPrinter, PRINT_COMMAND, default_request

if TYPE_CHECKING:
    from octoprint.settings import Settings

_LOGGER = logging.getLogger("octoprint.plugins.dremel3d45.virtual_serial")


class DremelVirtualSerial:
    _SD_INDEX_SCHEMA_VERSION = 1

    """
    Virtual serial port for Dremel 3D45 printer.

    Implements the serial-like interface expected by OctoPrint's MachineCom:
    - readline() -> bytes
    - write(data: bytes) -> int
    - close()
    - timeout, port, baudrate properties

    GCode commands are translated to REST API calls:
    - M105 (temps) -> GET status, return temps
    - M115 (firmware) -> GET printer info
    - M20 (list SD) -> List files on printer
    - M23 (select file) -> Select file for printing
    - M24 (start/resume) -> Start or resume print
    - M25 (pause) -> Pause print
    - M27 (SD status) -> Report print progress
    - M524 (abort) -> Cancel print
    - etc.
    """

    # Dremel status -> state string
    STATUS_MAP = {
        "ready": "operational",
        "building": "printing", 
        "paused": "paused",
        "completed": "operational",
        "cancelling": "cancelling",
        "error": "error",
        "busy": "busy",
        "offline": "offline",
    }

    def __init__(
        self,
        settings: "Settings",
        read_timeout: float = 5.0,
        write_timeout: float = 10.0,
        data_folder: Optional[str] = None,
    ):
        self._settings = settings
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

        _LOGGER.debug(
            "Initializing DremelVirtualSerial (timeout=%.1fs, write_timeout=%.1fs)",
            read_timeout, write_timeout,
        )

        self._closed = False

        # Get printer settings
        self._host = settings.get(["printer_ip"]) or ""
        self._request_timeout = settings.get_int(["request_timeout"]) or 30

        _LOGGER.debug("Printer host: %s, request timeout: %ds", self._host, self._request_timeout)

        # Response queue - OctoPrint reads from here
        self._outgoing: queue.Queue[str] = queue.Queue()

        # Best-effort byte count of queued outgoing responses (for in_waiting)
        self._outgoing_bytes = 0

        # Dremel API client (from dremel3dpy library)
        self._printer: Optional[Dremel3DPrinter] = None

        # Local state cache
        self._connected = False
        self._temps = {"tool0": (0.0, 0.0), "bed": (0.0, 0.0), "chamber": (0.0, 0.0)}
        # SD file index is session-scoped (Dremel API has no file listing we can query via dremel3dpy)
        # display_name -> {display, remote, size}
        self._sd_index: dict[str, dict] = {}
        self._sd_index_path: Optional[str] = None
        self._selected_file_display: str = ""
        self._selected_file_remote: str = ""
        self._selected_file_size: int = 0
        self._sd_files: list[dict] = []
        self._printing = False
        self._paused = False
        self._was_printing = False  # Track previous state to detect print completion
        self._progress = 0
        self._elapsed_time = 0
        self._remaining_time = 0
        self._current_layer = 0
        self._connection_errors = 0  # Track consecutive connection errors
        self._filament_type = ""  # Filament type from printer
        self._door_open = False  # Door sensor state
        self._fan_speed = 0  # Fan speed (read-only, can't control)

        # Auto-reporting controls (Marlin-style)
        self._autotemp_enabled = False
        self._autotemp_interval = 0
        self._last_autotemp_ts = 0.0
        self._autosd_enabled = False
        self._autosd_interval = 0
        self._last_autosd_ts = 0.0

        # Read buffer for callers using read(size)
        self._read_buffer = bytearray()

        # Line number tracking for Marlin protocol
        self._current_line = 0
        self._expected_line: Optional[int] = None

        # Polling thread for status updates
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()
        self._poll_interval = settings.get_int(["poll_interval"]) or 10

        _LOGGER.debug("Poll interval: %ds", self._poll_interval)

        # Lock for thread safety
        self._lock = threading.RLock()

        # Optional persisted SD index path
        if data_folder:
            _LOGGER.debug("Data folder provided: %s", data_folder)
            try:
                os.makedirs(data_folder, exist_ok=True)
                self._sd_index_path = os.path.join(data_folder, "sd_index.json")
                _LOGGER.debug("SD index path: %s", self._sd_index_path)
            except Exception as e:
                _LOGGER.warning("Failed to initialize data folder %r: %s", data_folder, e)
                self._sd_index_path = None
        else:
            _LOGGER.debug("No data folder provided - SD index will not be persisted")

        # Load persisted SD index (best-effort)
        self._load_sd_index()

        # Start communication
        self._start()

    def _load_sd_index(self) -> None:
        """Load the persisted SD index from disk (best-effort)."""
        path = self._sd_index_path
        if not path:
            _LOGGER.debug("No SD index path configured - skipping load")
            return

        _LOGGER.debug("Loading SD index from %s", path)

        try:
            if not os.path.exists(path):
                _LOGGER.debug("SD index file does not exist")
                return

            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)

            version = int(payload.get("schema_version", 0) or 0)
            if version != self._SD_INDEX_SCHEMA_VERSION:
                _LOGGER.warning(
                    "Unsupported sd_index schema_version=%s (expected %s); ignoring",
                    version,
                    self._SD_INDEX_SCHEMA_VERSION,
                )
                return

            items = payload.get("items", {})
            if not isinstance(items, dict):
                _LOGGER.warning("Invalid sd_index format; ignoring")
                return

            cleaned: dict[str, dict] = {}
            for display, meta in items.items():
                if not isinstance(meta, dict):
                    continue
                disp = str(meta.get("display") or display)
                remote = str(meta.get("remote") or "")
                if not remote:
                    continue
                try:
                    size = int(meta.get("size") or 0)
                except Exception:
                    size = 0
                cleaned[disp] = {"display": disp, "remote": remote, "size": size}

            with self._lock:
                self._sd_index = cleaned

            if cleaned:
                _LOGGER.info("Loaded %d SD index entries", len(cleaned))

        except Exception as e:
            _LOGGER.warning("Failed to load sd_index from %s: %s", path, e)

    def _save_sd_index(self) -> None:
        """Persist the SD index to disk (best-effort)."""
        path = self._sd_index_path
        if not path:
            _LOGGER.debug("No SD index path configured - skipping save")
            return

        _LOGGER.debug("Saving SD index to %s", path)

        try:
            with self._lock:
                items = dict(self._sd_index)

            _LOGGER.debug("Saving %d SD index entries", len(items))

            payload = {
                "schema_version": self._SD_INDEX_SCHEMA_VERSION,
                "updated_at": int(time.time()),
                "items": items,
            }

            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp_path, path)
            _LOGGER.debug("SD index saved successfully")
        except Exception as e:
            _LOGGER.warning("Failed to save sd_index to %s: %s", path, e)

    # -------------------------------------------------------------------------
    # Serial-like interface (required by OctoPrint)
    # -------------------------------------------------------------------------

    @property
    def timeout(self) -> float:
        return self._read_timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        _LOGGER.debug("Read timeout changed: %.1fs -> %.1fs", self._read_timeout, value)
        self._read_timeout = value

    @property
    def write_timeout(self) -> float:
        return self._write_timeout

    @write_timeout.setter
    def write_timeout(self, value: float) -> None:
        _LOGGER.debug("Write timeout changed: %.1fs -> %.1fs", self._write_timeout, value)
        self._write_timeout = value

    @property
    def port(self) -> str:
        return "DREMEL3D45"

    @property
    def baudrate(self) -> int:
        return 115200  # Fake baudrate

    def readline(self) -> bytes:
        """
        Read a line from the virtual serial port.

        Returns Marlin-compatible response strings as bytes.
        Blocks up to self.timeout seconds.
        """
        try:
            line = self._outgoing.get(timeout=self._read_timeout)
            with self._lock:
                self._outgoing_bytes = max(0, self._outgoing_bytes - len(line))
            _LOGGER.debug(">>> %s", line.strip())
            return line.encode("utf-8")
        except queue.Empty:
            return b""

    def read(self, size: int = 1) -> bytes:
        """pyserial compatibility: read up to size bytes."""
        if size <= 0:
            return b""

        # Satisfy from buffer first
        if self._read_buffer:
            chunk = bytes(self._read_buffer[:size])
            del self._read_buffer[:size]
            return chunk

        line = self.readline()
        if not line:
            return b""

        self._read_buffer.extend(line)
        chunk = bytes(self._read_buffer[:size])
        del self._read_buffer[:size]
        return chunk

    @property
    def in_waiting(self) -> int:
        """Approximate number of bytes queued for reading (pyserial compatibility)."""
        with self._lock:
            return int(self._outgoing_bytes)

    def inWaiting(self) -> int:  # noqa: N802
        """pyserial legacy alias for in_waiting."""
        return self.in_waiting

    @property
    def is_open(self) -> bool:
        """pyserial compatibility."""
        return not self._closed

    def isOpen(self) -> bool:  # noqa: N802
        """pyserial legacy alias for is_open."""
        return self.is_open

    def flush(self) -> None:
        """pyserial compatibility (no-op for this transport)."""
        return

    def reset_input_buffer(self) -> None:
        """Clear queued outgoing responses (pyserial compatibility)."""
        cleared = 0
        with self._lock:
            while True:
                try:
                    self._outgoing.get_nowait()
                    cleared += 1
                except queue.Empty:
                    break
            self._outgoing_bytes = 0
        if cleared > 0:
            _LOGGER.debug("Input buffer reset - cleared %d queued responses", cleared)

    def reset_output_buffer(self) -> None:
        """pyserial compatibility (no-op; writes are processed immediately)."""
        return

    def flushInput(self) -> None:  # noqa: N802
        """pyserial legacy alias for reset_input_buffer."""
        self.reset_input_buffer()

    def flushOutput(self) -> None:  # noqa: N802
        """pyserial legacy alias for reset_output_buffer."""
        self.reset_output_buffer()

    def write(self, data: bytes) -> int:
        """
        Write data (GCode command) to the virtual serial port.

        Parses the GCode and queues appropriate responses.
        """
        if not data:
            return 0

        # Decode
        try:
            payload = data.decode("utf-8", errors="replace")
        except Exception:
            return len(data)

        # Some writers can bundle multiple lines in a single write()
        for raw_line in payload.splitlines():
            line = raw_line.strip("\r\n")
            if not line:
                continue

            _LOGGER.debug("<<< %s", line)
            self._process_raw_line(line)

        return len(data)

    def close(self) -> None:
        """Close the virtual serial connection."""
        _LOGGER.info("Closing Dremel virtual serial connection")
        self._closed = True
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            _LOGGER.debug("Waiting for poll thread to stop...")
            self._poll_thread.join(timeout=5.0)
            if self._poll_thread.is_alive():
                _LOGGER.warning("Poll thread did not stop within timeout")
            else:
                _LOGGER.debug("Poll thread stopped")
        self._connected = False
        self._printer = None
        self._read_buffer.clear()
        self.reset_input_buffer()
        _LOGGER.info("Virtual serial connection closed")

    # -------------------------------------------------------------------------
    # Startup / Connection
    # -------------------------------------------------------------------------

    def _start(self) -> None:
        """Initialize the virtual serial port."""
        _LOGGER.info("Starting Dremel virtual serial for host: %s", self._host)

        if not self._host:
            self._send("Error: No printer IP configured")
            return

        # Queue initial startup messages (Marlin boot sequence)
        self._send("")  # Empty line
        self._send("start")
        self._send("Dremel 3D45 Virtual Serial")

        # Try to connect to printer using dremel3dpy library
        try:
            _LOGGER.debug("Creating Dremel3DPrinter instance for host: %s", self._host)
            self._printer = Dremel3DPrinter(self._host)
            self._connected = True
            _LOGGER.info("Connected to Dremel printer at %s", self._host)

            # Get firmware version from library
            firmware = self._printer.get_firmware_version() or "Unknown"
            _LOGGER.info("Printer firmware version: %s", firmware)

            # Send capability report
            self._send(f"FIRMWARE_NAME:Dremel3D45 FIRMWARE_VERSION:{firmware}")
            self._send("Cap:AUTOREPORT_TEMP:1")
            self._send("Cap:AUTOREPORT_SD_STATUS:1")
            self._send("ok")

            # Start polling thread
            _LOGGER.debug("Starting poll thread (interval=%ds)", self._poll_interval)
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                name="dremel3d45.poll",
                daemon=True,
            )
            self._poll_thread.start()
            _LOGGER.debug("Poll thread started")

        except Exception as e:
            _LOGGER.error("Failed to connect to printer at %s: %s", self._host, e)
            _LOGGER.debug("Connection error details", exc_info=True)
            self._send(f"Error: Connection failed - {e}")

    def _send(self, line: str) -> None:
        """Queue a response line to be read by OctoPrint."""
        payload = line + "\n"
        with self._lock:
            self._outgoing_bytes += len(payload)
        self._outgoing.put(payload)

    # -------------------------------------------------------------------------
    # Command Processing
    # -------------------------------------------------------------------------

    def _compute_marlin_checksum(self, line: str) -> int:
        """Compute Marlin XOR checksum over the given line (everything before '*')."""
        checksum = 0
        for ch in line:
            checksum ^= ord(ch)
        return checksum

    def _strip_comments(self, line: str) -> str:
        """Remove common GCode comment styles."""
        # Remove parenthetical comments
        line = re.sub(r"\([^)]*\)", "", line)
        # Remove ';' comments
        if ";" in line:
            line = line.split(";", 1)[0]
        return line.strip()

    def _is_print_active(self) -> bool:
        """Check if a print is actively running (not paused).
        
        Use this to guard operations that should not occur during printing.
        """
        return self._printing and not self._paused

    def _process_raw_line(self, raw_line: str) -> None:
        """Process a raw line as received over the virtual serial connection."""
        if not raw_line:
            return

        raw_line = raw_line.strip()
        if not raw_line:
            return

        # Handle emergency cancel (Ctrl-X) - ignore but acknowledge
        if raw_line == "\x18":
            self._send("ok")
            return

        # Strip comments early (but keep line number/checksum area intact)
        # NOTE: comments may appear after checksum; stripping later could break checksum.
        # We only strip comments from the command portion after checksum/line parsing.

        # Checksum validation
        line_for_checksum = raw_line
        provided_checksum: Optional[int] = None
        if "*" in raw_line:
            prefix, suffix = raw_line.split("*", 1)
            line_for_checksum = prefix
            try:
                provided_checksum = int(suffix.strip())
            except Exception:
                provided_checksum = None

        # Parse line number (optional)
        line_number: Optional[int] = None
        match = re.match(r"^N(\d+)\s+", line_for_checksum)
        if match:
            try:
                line_number = int(match.group(1))
            except Exception:
                line_number = None

        if provided_checksum is not None:
            computed = self._compute_marlin_checksum(line_for_checksum)
            if computed != provided_checksum:
                _LOGGER.warning(
                    "Checksum mismatch: got=%s computed=%s line=%r",
                    provided_checksum,
                    computed,
                    raw_line,
                )
                self._send("Error:checksum mismatch")
                if line_number is not None:
                    self._send(f"Resend:{line_number}")
                return

        # Line number sequencing (best-effort, only if host uses N-lines)
        if line_number is not None:
            if self._expected_line is None:
                self._expected_line = line_number
            if line_number != self._expected_line:
                self._send("Error:Line Number is not Last Line Number+1")
                self._send(f"Resend:{self._expected_line}")
                return

        # Remove line number + checksum, then strip comments
        command = line_for_checksum
        if match:
            command = command[match.end():]
        command = self._strip_comments(command)

        # Track the most recent line number seen (best-effort)
        if line_number is not None:
            self._current_line = line_number
            self._expected_line = line_number + 1

        self._process_command(command)

    def _process_command(self, command: str) -> None:
        """Process a GCode command and queue appropriate response."""
        if not command:
            self._send("ok")
            return

        # Parse command code
        cmd = command.split()[0].upper() if command.split() else ""

        # Dispatch to handler
        handler = getattr(self, f"_gcode_{cmd}", None)
        if handler:
            _LOGGER.debug("Dispatching command %s to handler", cmd)
            try:
                handler(command)
            except Exception as e:
                _LOGGER.exception("Error handling %s: %s", cmd, e)
                self._send(f"Error: {e}")
                self._send("ok")
        else:
            # Unknown command - just acknowledge
            _LOGGER.debug("Unknown/unsupported command (acknowledged): %s", command)
            self._send("ok")

    # -------------------------------------------------------------------------
    # GCode Handlers
    # -------------------------------------------------------------------------

    def _gcode_M105(self, command: str) -> None:
        """Report temperatures."""
        self._refresh_status()
        t0 = self._temps.get("tool0", (0, 0))
        bed = self._temps.get("bed", (0, 0))
        chamber = self._temps.get("chamber", (0, 0))
        
        _LOGGER.debug(
            "Temperature report: extruder=%.1f/%.1f, bed=%.1f/%.1f, chamber=%.1f",
            t0[0], t0[1], bed[0], bed[1], chamber[0],
        )
        
        # Marlin-ish format: ok T:.. /.. B:.. /.. (extras tolerated)
        self._send(
            f"ok T:{t0[0]:.1f} /{t0[1]:.1f} B:{bed[0]:.1f} /{bed[1]:.1f} C:{chamber[0]:.1f} /{chamber[1]:.1f}"
        )

    def _gcode_M115(self, command: str) -> None:
        """Report firmware info."""
        if not self._printer:
            _LOGGER.warning("M115 requested but not connected")
            self._send("Error: Not connected")
            self._send("ok")
            return
        
        # Refresh printer info
        _LOGGER.debug("Refreshing printer info for M115")
        self._printer.set_printer_info(refresh=True)
        
        machine = self._printer.get_title() or "Dremel 3D45"
        firmware = self._printer.get_firmware_version() or "Unknown"
        serial = self._printer.get_serial_number() or "Unknown"
        
        _LOGGER.debug(
            "Firmware info: machine=%s, firmware=%s, serial=%s",
            machine, firmware, serial,
        )
        
        # Include UUID for plugin compatibility
        self._send(f"FIRMWARE_NAME:Dremel3D45 MACHINE_TYPE:{machine} FIRMWARE_VERSION:{firmware} SERIAL:{serial} UUID:{serial}")
        self._send("Cap:AUTOREPORT_TEMP:1")
        self._send("Cap:AUTOREPORT_SD_STATUS:1")
        self._send("Cap:EEPROM:0")
        self._send("Cap:VOLUMETRIC:0")
        self._send("Cap:THERMAL_PROTECTION:0")
        self._send("Cap:EMERGENCY_PARSER:0")
        self._send("ok")

    def _gcode_M114(self, command: str) -> None:
        """Report current position.
        
        Note: Position is not tracked - Dremel doesn't support motion control.
        During printing, we report the current layer from the API.
        """
        if self._printing or self._paused:
            # Only layer info is available from the Dremel API
            self._send(f"X:0.00 Y:0.00 Z:0.00 E:0.00 Layer:{self._current_layer}")
        else:
            self._send("X:0.00 Y:0.00 Z:0.00 E:0.00")
        self._send("ok")

    def _gcode_M119(self, command: str) -> None:
        """Report endstop status (simulated + door from Dremel API)."""
        door_status = "TRIGGERED" if self._door_open else "open"
        
        # Refresh door state from API
        if self._printer:
            try:
                self._door_open = self._printer.is_door_open()
                door_status = "TRIGGERED" if self._door_open else "open"
            except Exception:
                pass

        self._send("Reporting endstop status")
        self._send("x_min: open")
        self._send("y_min: open")
        self._send("z_min: open")
        self._send(f"door: {door_status}")
        # Report filament as sensor (some plugins check this)
        if self._filament_type:
            self._send(f"filament: {self._filament_type}")
        self._send("ok")

    def _gcode_M108(self, command: str) -> None:
        """Break out of a wait (no-op)."""
        self._send("ok")

    def _gcode_M20(self, command: str) -> None:
        """List SD card files."""
        # NOTE: dremel3dpy determines there is no reliable way to list files on the 3D45 
        # via the user-facing API. Therefore we rely on our session-scoped/persisted index 
        # (self._sd_index) of files we have uploaded ourselves.
        self._fetch_sd_files()
        
        _LOGGER.debug("M20: Listing %d files in SD index", len(self._sd_files))
        
        self._send("Begin file list")
        for f in self._sd_files:
            name = f.get("name", "unknown.gcode")
            size = f.get("size", 0)
            self._send(f"{name} {size}")
        self._send("End file list")
        self._send("ok")

    def _gcode_M23(self, command: str) -> None:
        """Select SD file for printing. Format: M23 filename.gcode"""
        if self._is_print_active():
            _LOGGER.warning("M23: Cannot select file while printing")
            self._send("Error: Cannot select file while printing")
            self._send("ok")
            return

        parts = command.split(maxsplit=1)
        if len(parts) < 2:
            _LOGGER.warning("M23: No file specified")
            self._send("Error: No file specified")
            self._send("ok")
            return

        filename = parts[1].strip()
        _LOGGER.debug("M23: Attempting to select file: %s", filename)
        resolved = self._resolve_sd_filename(filename)
        if not resolved:
            _LOGGER.warning("M23: File not found in SD index: %s", filename)
            self._send("Error: File not found")
            self._send("ok")
            return

        display_name, remote_name, file_size = resolved
        self._selected_file_display = display_name
        self._selected_file_remote = remote_name
        self._selected_file_size = int(file_size or 0)

        _LOGGER.info(
            "Selected file: %s (remote=%s, size=%d)",
            display_name, remote_name, file_size,
        )

        self._send(f"File opened: {display_name} Size: {file_size}")
        self._send("File selected")
        self._send("ok")

    def _gcode_M24(self, command: str) -> None:
        """Start/resume SD print."""
        if not self._printer:
            _LOGGER.warning("M24: Cannot start print - not connected")
            self._send("Error: Not connected")
            self._send("ok")
            return
            
        if self._paused:
            # Resume using library method
            _LOGGER.info("Resuming paused print")
            self._printer.resume_print()
            self._paused = False
            self._printing = True
            _LOGGER.debug("Print resumed successfully")
            self._send("ok")
        elif self._is_print_active():
            # Already printing - don't start a new job
            _LOGGER.warning("M24: Print already in progress")
            self._send("Error: Print already in progress")
            self._send("ok")
        elif self._selected_file_remote:
            # Start a print from an already-uploaded file (remote filename)
            # NOTE: dremel3dpy does not expose a public method to start a print 
            # from a remote filename (only from local file via upload), so we 
            # use the internal default_request helper and PRINT_COMMAND constant.
            _LOGGER.info(
                "Starting print: %s (remote=%s)",
                self._selected_file_display, self._selected_file_remote,
            )
            try:
                default_request(self._host, {PRINT_COMMAND: self._selected_file_remote})
                self._printing = True
                self._paused = False
                _LOGGER.info("Print started successfully")
                self._send("ok")
            except Exception as e:
                _LOGGER.error("Failed to start print: %s", e)
                self._send(f"Error: {e}")
                self._send("ok")
        else:
            _LOGGER.warning("M24: No file selected for printing")
            self._send("Error: No file selected")
            self._send("ok")

    def _gcode_M25(self, command: str) -> None:
        """Pause SD print."""
        if self._printing and self._printer:
            _LOGGER.info("Pausing print")
            self._printer.pause_print()
            self._paused = True
            _LOGGER.debug("Print paused successfully")
        else:
            _LOGGER.debug("M25: Not printing - nothing to pause")
        self._send("ok")

    def _gcode_M600(self, command: str) -> None:
        """Filament change (treated as pause for compatibility)."""
        self._gcode_M25(command)

    def _gcode_M0(self, command: str) -> None:
        """Unconditional stop / pause (treated as pause for compatibility)."""
        self._gcode_M25(command)

    def _gcode_M1(self, command: str) -> None:
        """Sleep / conditional stop (treated as pause for compatibility)."""
        self._gcode_M25(command)

    def _gcode_M27(self, command: str) -> None:
        """Report SD print status (and optionally configure auto-report)."""
        # Marlin: M27 S<sec> enables SD status auto-reporting
        match = re.search(r"S(\d+)", command)
        if match:
            interval = int(match.group(1))
            self._autosd_enabled = interval > 0
            self._autosd_interval = interval
            self._last_autosd_ts = 0.0
            _LOGGER.debug(
                "Auto-report SD status %s (interval=%ds)",
                "enabled" if self._autosd_enabled else "disabled",
                interval,
            )
            self._send("ok")
            return

        self._refresh_status()
        
        if self._printing or self._paused:
            # Format: SD printing byte X/Y
            total = int(self._selected_file_size or 0)
            if total > 0:
                printed = int((float(self._progress) / 100.0) * float(total))
                self._send(f"SD printing byte {printed}/{total}")
            else:
                printed = int(self._progress)
                self._send(f"SD printing byte {printed}/100")
            _LOGGER.debug("SD status: progress=%.1f%%, layer=%d", self._progress, self._current_layer)
        else:
            _LOGGER.debug("SD status: not printing")
            self._send("Not SD printing")
        self._send("ok")

    def _gcode_M26(self, command: str) -> None:
        """Set SD position (no-op).

        Some senders issue M26 before M24; we don't support random access.
        """
        self._send("ok")

    def _gcode_M524(self, command: str) -> None:
        """Abort SD print (Marlin 2.0+)."""
        _LOGGER.info("Aborting print (M524)")
        if self._printer:
            self._printer.stop_print()
            _LOGGER.debug("Stop command sent to printer")
        self._printing = False
        self._paused = False
        self._selected_file_display = ""
        self._selected_file_remote = ""
        self._selected_file_size = 0
        _LOGGER.info("Print aborted - state reset")
        self._send("ok")

    def _gcode_M155(self, command: str) -> None:
        """Set auto-report temperature interval. Format: M155 S<seconds>"""
        match = re.search(r"S(\d+)", command)
        if match:
            interval = int(match.group(1))
            self._autotemp_enabled = interval > 0
            self._autotemp_interval = interval
            self._last_autotemp_ts = 0.0
            _LOGGER.debug(
                "Auto-report temperature %s (interval=%ds)",
                "enabled" if self._autotemp_enabled else "disabled",
                interval,
            )
        self._send("ok")

    def _gcode_M104(self, command: str) -> None:
        """Set extruder temperature. Format: M104 S<temp>
        
        Uses Dremel REST API: NOZZLEHEAT=nnn or STOPNOZZLEHEAT
        Max temp for 3D45: 280°C
        
        Blocked during active printing (but allowed when paused).
        """
        if self._is_print_active():
            _LOGGER.warning("M104: Blocked - cannot change temperature while printing")
            self._send("Error: Cannot change temperature while printing")
            self._send("ok")
            return

        match = re.search(r"S(\d+)", command)
        if match:
            target = int(float(match.group(1)))
            # Clamp to safe range
            original_target = target
            target = max(0, min(280, target))
            if target != original_target:
                _LOGGER.warning(
                    "M104: Target %d clamped to safe range (0-280): %d",
                    original_target, target,
                )
            
            _LOGGER.info("Setting extruder temperature to %d°C", target)
            try:
                if target == 0:
                    _LOGGER.debug("Sending STOPNOZZLEHEAT command")
                    default_request(self._host, "STOPNOZZLEHEAT")
                else:
                    _LOGGER.debug("Sending NOZZLEHEAT=%d command", target)
                    default_request(self._host, f"NOZZLEHEAT={target}")
                self._temps["tool0"] = (self._temps["tool0"][0], float(target))
                _LOGGER.debug("Extruder temperature target set successfully")
            except Exception as e:
                _LOGGER.error("Failed to set nozzle temperature: %s", e)
        self._send("ok")

    def _gcode_M140(self, command: str) -> None:
        """Set bed temperature. Format: M140 S<temp>
        
        Uses Dremel REST API: PLATEHEAT=nnn or STOPPLATEHEAT
        Max temp for 3D45: 100°C
        
        Blocked during active printing (but allowed when paused).
        """
        if self._is_print_active():
            _LOGGER.warning("M140: Blocked - cannot change temperature while printing")
            self._send("Error: Cannot change temperature while printing")
            self._send("ok")
            return

        match = re.search(r"S(\d+)", command)
        if match:
            target = int(float(match.group(1)))
            # Clamp to safe range
            original_target = target
            target = max(0, min(100, target))
            if target != original_target:
                _LOGGER.warning(
                    "M140: Target %d clamped to safe range (0-100): %d",
                    original_target, target,
                )
            
            _LOGGER.info("Setting bed temperature to %d°C", target)
            try:
                if target == 0:
                    _LOGGER.debug("Sending STOPPLATEHEAT command")
                    default_request(self._host, "STOPPLATEHEAT")
                else:
                    _LOGGER.debug("Sending PLATEHEAT=%d command", target)
                    default_request(self._host, f"PLATEHEAT={target}")
                self._temps["bed"] = (self._temps["bed"][0], float(target))
                _LOGGER.debug("Bed temperature target set successfully")
            except Exception as e:
                _LOGGER.error("Failed to set bed temperature: %s", e)
        self._send("ok")

    def _gcode_M109(self, command: str) -> None:
        """Set extruder temp and wait. Format: M109 S<temp> or M109 R<temp>
        
        Sets temperature via REST API and waits for it to reach target.
        Note: We don't actually block here (would freeze OctoPrint), but we
        set the temp and OctoPrint will poll M105 to track progress.
        
        Blocked during active printing (but allowed when paused).
        """
        if self._is_print_active():
            _LOGGER.warning("M109: Blocked - cannot change temperature while printing")
            self._send("Error: Cannot change temperature while printing")
            self._send("ok")
            return

        # M109 supports both S (heat and wait) and R (heat/cool and wait)
        match = re.search(r"[SR](\d+)", command)
        if match:
            target = int(float(match.group(1)))
            target = max(0, min(280, target))
            
            _LOGGER.info("Setting extruder temperature to %d°C (and wait)", target)
            try:
                if target == 0:
                    default_request(self._host, "STOPNOZZLEHEAT")
                else:
                    default_request(self._host, f"NOZZLEHEAT={target}")
                self._temps["tool0"] = (self._temps["tool0"][0], float(target))
                _LOGGER.debug("Extruder temperature target set - OctoPrint will wait for target")
            except Exception as e:
                _LOGGER.error("Failed to set nozzle temperature: %s", e)
        self._send("ok")

    def _gcode_M190(self, command: str) -> None:
        """Set bed temp and wait. Format: M190 S<temp> or M190 R<temp>
        
        Sets temperature via REST API. OctoPrint will poll M105 to track.
        
        Blocked during active printing (but allowed when paused).
        """
        if self._is_print_active():
            _LOGGER.warning("M190: Blocked - cannot change temperature while printing")
            self._send("Error: Cannot change temperature while printing")
            self._send("ok")
            return

        match = re.search(r"[SR](\d+)", command)
        if match:
            target = int(float(match.group(1)))
            target = max(0, min(100, target))
            
            _LOGGER.info("Setting bed temperature to %d°C (and wait)", target)
            try:
                if target == 0:
                    default_request(self._host, "STOPPLATEHEAT")
                else:
                    default_request(self._host, f"PLATEHEAT={target}")
                self._temps["bed"] = (self._temps["bed"][0], float(target))
                _LOGGER.debug("Bed temperature target set - OctoPrint will wait for target")
            except Exception as e:
                _LOGGER.error("Failed to set bed temperature: %s", e)
        self._send("ok")

    def _gcode_M106(self, command: str) -> None:
        """Set fan speed. Format: M106 S<speed>"""
        # Dremel doesn't support fan control - just acknowledge to maintain compatibility
        self._send("ok")

    def _gcode_M107(self, command: str) -> None:
        """Fan off."""
        self._send("ok")

    def _gcode_M110(self, command: str) -> None:
        """Set line number. Format: M110 N<line> or M110 (reset to 0)"""
        match = re.search(r"N(\d+)", command)
        if match:
            try:
                new_line = int(match.group(1))
                _LOGGER.debug("M110: Setting line number to %d", new_line)
                self._current_line = new_line
            except Exception:
                _LOGGER.debug("M110: Resetting line number to 0 (parse error)")
                self._current_line = 0
        else:
            # M110 without N resets to 0 per Marlin behavior
            _LOGGER.debug("M110: Resetting line number to 0")
            self._current_line = 0
        self._expected_line = self._current_line + 1
        self._send("ok")

    def _gcode_G90(self, command: str) -> None:
        """Set to Absolute Positioning (no-op - motion not supported)."""
        self._send("ok")

    def _gcode_G91(self, command: str) -> None:
        """Set to Relative Positioning (no-op - motion not supported)."""
        self._send("ok")

    def _gcode_M82(self, command: str) -> None:
        """Set Extruder to Absolute Positioning (no-op - motion not supported)."""
        self._send("ok")

    def _gcode_M83(self, command: str) -> None:
        """Set Extruder to Relative Positioning (no-op - motion not supported)."""
        self._send("ok")

    def _gcode_G28(self, command: str) -> None:
        """Home axes (no-op - Dremel doesn't support motion control via GCode)."""
        # Dremel handles homing internally; we just acknowledge
        self._send("ok")

    def _gcode_G0(self, command: str) -> None:
        """Rapid move (no-op - Dremel doesn't support motion control via GCode)."""
        self._send("ok")

    def _gcode_G1(self, command: str) -> None:
        """Linear move (no-op - Dremel doesn't support motion control via GCode)."""
        self._send("ok")

    def _gcode_M400(self, command: str) -> None:
        """Wait for moves to finish."""
        self._send("ok")

    def _gcode_M112(self, command: str) -> None:
        """Emergency stop."""
        _LOGGER.critical("EMERGENCY STOP requested (M112)!")
        if self._printer:
            _LOGGER.info("Sending stop command to printer")
            self._printer.stop_print()
        self._printing = False
        self._paused = False
        _LOGGER.info("Emergency stop executed - print state reset")
        self._send("ok")

    def _gcode_M503(self, command: str) -> None:
        """Report settings (simulated)."""
        self._send("echo:; Steps per unit:")
        self._send("echo:  M92 X80.00 Y80.00 Z400.00 E93.00")
        self._send("ok")

    def _gcode_M220(self, command: str) -> None:
        """Set feedrate percentage. Format: M220 S<percent>"""
        # Can't control this on Dremel, just acknowledge
        self._send("ok")

    def _gcode_M221(self, command: str) -> None:
        """Set flow percentage. Format: M221 S<percent>"""
        # Can't control this on Dremel, just acknowledge
        self._send("ok")

    def _gcode_G92(self, command: str) -> None:
        """Set current position (no-op - position not tracked)."""
        self._send("ok")

    def _gcode_G4(self, command: str) -> None:
        """Dwell (ignored, but acknowledged)."""
        self._send("ok")

    def _gcode_M17(self, command: str) -> None:
        """Enable steppers (no-op)."""
        self._send("ok")

    def _gcode_M18(self, command: str) -> None:
        """Disable steppers (no-op)."""
        self._send("ok")

    def _gcode_M84(self, command: str) -> None:
        """Disable steppers (no-op)."""
        self._send("ok")

    def _gcode_M21(self, command: str) -> None:
        """Initialize SD card (no-op)."""
        self._send("ok")

    def _gcode_M22(self, command: str) -> None:
        """Release SD card (no-op)."""
        self._send("ok")

    def _gcode_M32(self, command: str) -> None:
        """Select and start SD print. Format: M32 <filename>"""
        if self._is_print_active():
            _LOGGER.warning("M32: Cannot start new print while printing")
            self._send("Error: Cannot start new print while printing")
            self._send("ok")
            return

        parts = command.split(maxsplit=1)
        if len(parts) < 2:
            _LOGGER.warning("M32: No file specified")
            self._send("Error: No file specified")
            self._send("ok")
            return

        filename = parts[1].strip()
        _LOGGER.debug("M32: Attempting to select and start: %s", filename)
        resolved = self._resolve_sd_filename(filename)
        if not resolved:
            _LOGGER.warning("M32: File not found: %s", filename)
            self._send("Error: File not found")
            return

        display_name, remote_name, file_size = resolved
        self._selected_file_display = display_name
        self._selected_file_remote = remote_name
        self._selected_file_size = int(file_size or 0)

        _LOGGER.info(
            "M32: Selecting and starting print: %s (remote=%s)",
            display_name, remote_name,
        )

        # Delegate to M24 which actually starts/resumes the print.
        self._gcode_M24("M24")

    def _gcode_M73(self, command: str) -> None:
        """Set build progress (best-effort). Format: M73 P<percent> [R<min>]"""
        match = re.search(r"P(\d+)", command)
        if match:
            try:
                self._progress = float(match.group(1))
            except Exception:
                pass
        self._send("ok")

    def _gcode_M532(self, command: str) -> None:
        """Report job progress with layer info (Prusa-style).
        
        Format: X:<percent> L:<layer>
        Some hosts (OctoPrint plugins) parse this for layer display.
        """
        self._refresh_status()
        self._send(f"X:{self._progress:.1f} L:{self._current_layer}")
        self._send("ok")

    def _gcode_M75(self, command: str) -> None:
        """Start print job timer (no-op)."""
        self._send("ok")

    def _gcode_M76(self, command: str) -> None:
        """Pause print job timer (no-op)."""
        self._send("ok")

    def _gcode_M77(self, command: str) -> None:
        """Stop print job timer (no-op)."""
        self._send("ok")

    def _gcode_M31(self, command: str) -> None:
        """Report elapsed print time (best-effort)."""
        seconds = int(self._elapsed_time or 0)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        self._send(f"echo:Print time: {h:02d}:{m:02d}:{s:02d}")
        self._send("ok")

    def _gcode_M117(self, command: str) -> None:
        """Display message (acknowledge)."""
        self._send("ok")

    def _gcode_M118(self, command: str) -> None:
        """Serial/host message (echo back for compatibility)."""
        # Typical format: M118 <message>
        parts = command.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            self._send(f"echo:{parts[1].strip()}")
        self._send("ok")

    def _gcode_M999(self, command: str) -> None:
        """Restart after fault (no-op)."""
        self._send("ok")

    def _gcode_T0(self, command: str) -> None:
        """Select tool 0 (single-extruder printers treat as no-op)."""
        self._send("ok")

    def _gcode_T1(self, command: str) -> None:
        """Select tool 1 (not supported; acknowledge)."""
        self._send("ok")

    def _gcode_M500(self, command: str) -> None:
        """Store settings (no-op)."""
        self._send("ok")

    def _gcode_M501(self, command: str) -> None:
        """Load settings (no-op)."""
        self._send("ok")

    def _gcode_M502(self, command: str) -> None:
        """Factory reset settings (no-op)."""
        self._send("ok")

    def _gcode_M211(self, command: str) -> None:
        """Software endstops (no-op)."""
        self._send("ok")

    def _gcode_G29(self, command: str) -> None:
        """Auto bed leveling (not supported - Dremel has internal leveling)."""
        if self._is_print_active():
            self._send("Error: Cannot level while printing")
            self._send("ok")
            return
        # Dremel handles leveling internally via touchscreen
        self._send("echo:Bed leveling not available via GCode")
        self._send("ok")

    def _gcode_M420(self, command: str) -> None:
        """Bed leveling state (no-op)."""
        self._send("ok")

    def _gcode_M851(self, command: str) -> None:
        """Z probe offset (no-op)."""
        self._send("ok")

    def _gcode_G10(self, command: str) -> None:
        """Firmware retract (no-op - Dremel doesn't support motion control)."""
        self._send("ok")

    def _gcode_G11(self, command: str) -> None:
        """Firmware unretract (no-op - Dremel doesn't support motion control)."""
        self._send("ok")

    def _gcode_M92(self, command: str) -> None:
        """Set/report steps per unit (simulated report)."""
        # Just report fake values for compatibility
        self._send("echo: M92 X80.00 Y80.00 Z400.00 E93.00")
        self._send("ok")

    def _gcode_M201(self, command: str) -> None:
        """Set max acceleration (no-op)."""
        self._send("ok")

    def _gcode_M203(self, command: str) -> None:
        """Set max feedrate (no-op)."""
        self._send("ok")

    def _gcode_M204(self, command: str) -> None:
        """Set acceleration (no-op)."""
        self._send("ok")

    def _gcode_M205(self, command: str) -> None:
        """Set jerk limits (no-op)."""
        self._send("ok")

    def _gcode_M301(self, command: str) -> None:
        """Set hotend PID (no-op)."""
        self._send("ok")

    def _gcode_M304(self, command: str) -> None:
        """Set bed PID (no-op)."""
        self._send("ok")

    def _gcode_M862(self, command: str) -> None:
        """Printer model check (Prusa-style, no-op)."""
        self._send("ok")

    # -------------------------------------------------------------------------
    # Dremel API Communication (via dremel3dpy library)
    # -------------------------------------------------------------------------

    def _refresh_status(self) -> None:
        """Refresh printer status from Dremel API via library."""
        # Grab reference under lock to avoid race with close()
        printer = self._printer
        if not printer:
            return
            
        try:
            # Refresh job status from library (makes ONE API call internally)
            printer.set_job_status(refresh=True)
            
            with self._lock:
                # Get actual temperatures using library methods
                tool_actual = float(printer.get_temperature_type("extruder") or 0)
                bed_actual = float(printer.get_temperature_type("platform") or 0)
                chamber_actual = float(printer.get_temperature_type("chamber") or 0)
                
                # For target temps: ALWAYS prefer our locally-set values since the Dremel API
                # reports bogus target temps (e.g., 270 when we set 100).
                # Only fall back to API values if we haven't set targets ourselves.
                tool_attrs = printer.get_temperature_attributes("extruder") or {}
                bed_attrs = printer.get_temperature_attributes("platform") or {}
                
                api_tool_target = float(tool_attrs.get("target_temp", 0) or 0)
                api_bed_target = float(bed_attrs.get("target_temp", 0) or 0)
                
                current_tool_target = self._temps.get("tool0", (0, 0))[1]
                current_bed_target = self._temps.get("bed", (0, 0))[1]
                
                # Prefer local target; fall back to API only during firmware-initiated prints
                tool_target = current_tool_target if current_tool_target > 0 else api_tool_target
                bed_target = current_bed_target if current_bed_target > 0 else api_bed_target
                
                self._temps = {
                    "tool0": (tool_actual, tool_target),
                    "bed": (bed_actual, bed_target),
                    "chamber": (chamber_actual, 0),
                }
                
                # Parse print status using library methods
                was_active = self._printing or self._paused
                self._printing = printer.is_printing()
                self._paused = printer.is_paused()
                is_active = self._printing or self._paused
                
                # Detect print completion: was printing/paused, now idle
                if self._was_printing and not is_active:
                    _LOGGER.info("Print completed - resetting temperature targets")
                    # Reset locally-tracked temp targets since print is done
                    self._temps = {
                        "tool0": (tool_actual, 0.0),
                        "bed": (bed_actual, 0.0),
                        "chamber": (chamber_actual, 0),
                    }
                    # Clear selected file
                    self._selected_file_display = ""
                    self._selected_file_remote = ""
                    self._selected_file_size = 0
                
                self._was_printing = is_active
                
                self._progress = float(printer.get_printing_progress() or 0)
                self._elapsed_time = int(printer.get_elapsed_time() or 0)
                self._remaining_time = int(printer.get_remaining_time() or 0)
                self._current_layer = int(printer.get_layer() or 0)
                
                # Capture additional sensor data
                try:
                    self._door_open = printer.is_door_open()
                except Exception:
                    pass
                try:
                    job_status = printer.get_job_status() or {}
                    self._filament_type = str(job_status.get("filament", "") or "").strip()
                    self._fan_speed = int(job_status.get("fan_speed", 0) or 0)
                except Exception:
                    pass
                
                # Reset error counter on successful refresh
                self._connection_errors = 0

                # Best-effort: keep selected file in sync with the active job name
                try:
                    job_name = (printer.get_job_name() or "").strip()
                except Exception:
                    job_name = ""
                if (self._printing or self._paused) and job_name:
                    self._selected_file_remote = job_name
                    # If we previously uploaded this via OctoPrint, map back to display name
                    display_name = ""
                    for disp, meta in self._sd_index.items():
                        if (meta.get("remote") or "").lower() == job_name.lower():
                            display_name = meta.get("display") or disp
                            self._selected_file_size = int(meta.get("size") or 0)
                            break
                    if display_name:
                        self._selected_file_display = display_name
                
        except Exception as e:
            self._connection_errors += 1
            if self._connection_errors <= 3:
                _LOGGER.warning("Error refreshing status (attempt %d): %s", self._connection_errors, e)
            elif self._connection_errors == 4:
                _LOGGER.error("Persistent connection errors - printer may be offline")
            # After 3 errors, only log at debug level to avoid log spam
            else:
                _LOGGER.debug("Error refreshing status: %s", e)

    def _fetch_sd_files(self) -> None:
        """Fetch file list from Dremel (if available)."""
        # Dremel doesn't expose a reliable file listing via dremel3dpy.
        # We therefore keep a session-scoped index of files uploaded via OctoPrint.
        with self._lock:
            self._sd_files = []
            for meta in self._sd_index.values():
                self._sd_files.append(
                    {"name": meta.get("display") or meta.get("remote") or "unknown.gcode", "size": int(meta.get("size") or 0)}
                )

            # If we have a selected job that isn't in the index, still surface it for compatibility
            if self._selected_file_remote:
                selected_display = self._selected_file_display or self._selected_file_remote
                if not any(f.get("name", "").lower() == selected_display.lower() for f in self._sd_files):
                    self._sd_files.append({"name": selected_display, "size": int(self._selected_file_size or 0)})

    def _resolve_sd_filename(self, filename: str) -> Optional[tuple[str, str, int]]:
        """Resolve an SD filename from display name to remote name.

        Returns (display_name, remote_name, size) or None.
        """
        name = (filename or "").strip()
        if not name:
            return None

        # Exact match on display name
        for disp, meta in self._sd_index.items():
            if disp.lower() == name.lower() or (meta.get("display") or "").lower() == name.lower():
                return (meta.get("display") or disp, meta.get("remote") or disp, int(meta.get("size") or 0))

        # Allow selecting by remote name if the host has it
        for disp, meta in self._sd_index.items():
            if (meta.get("remote") or "").lower() == name.lower():
                return (meta.get("display") or disp, meta.get("remote") or name, int(meta.get("size") or 0))

        # Fall back to whatever is currently selected (useful if firmware reports a job name)
        if self._selected_file_remote and self._selected_file_remote.lower() == name.lower():
            return (self._selected_file_display or name, self._selected_file_remote, int(self._selected_file_size or 0))

        return None

    # -------------------------------------------------------------------------
    # Status Polling
    # -------------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Background thread to poll printer status."""
        _LOGGER.info("Starting status polling thread")
        
        while not self._poll_stop.wait(self._poll_interval):
            try:
                self._refresh_status()

                now = time.time()

                # Auto-report temperature (Marlin-style) if enabled, or if printing
                should_report_temp = (self._printing or self._paused) or self._autotemp_enabled
                if should_report_temp:
                    interval = self._autotemp_interval if self._autotemp_enabled else 0
                    if interval <= 0 or (now - self._last_autotemp_ts) >= float(interval):
                        t0 = self._temps.get("tool0", (0, 0))
                        bed = self._temps.get("bed", (0, 0))
                        self._send(
                            f"T:{t0[0]:.1f} /{t0[1]:.1f} B:{bed[0]:.1f} /{bed[1]:.1f}"
                        )
                        self._last_autotemp_ts = now

                # Auto-report SD status if enabled
                if self._autosd_enabled and self._autosd_interval > 0:
                    if (now - self._last_autosd_ts) >= float(self._autosd_interval):
                        printed = int(self._progress)
                        self._send(f"SD printing byte {printed}/100")
                        self._last_autosd_ts = now
                    
            except Exception as e:
                _LOGGER.debug("Poll error: %s", e)

        _LOGGER.info("Status polling thread stopped")

    # -------------------------------------------------------------------------
    # File Upload (for OctoPrint's upload to SD feature)
    # -------------------------------------------------------------------------

    def upload_file(self, local_path: str, remote_name: str) -> bool:
        """
        Upload a file to the Dremel printer.

        Args:
            local_path: Path to local gcode file
            remote_name: Filename to use on printer

        Returns:
            True if upload succeeded
        """
        _LOGGER.info("Upload requested: %s -> %s", local_path, remote_name)
        
        if not self._printer:
            _LOGGER.error("Cannot upload: not connected to printer")
            return False

        if self._is_print_active():
            _LOGGER.error("Cannot upload: print in progress")
            return False
            
        try:
            _LOGGER.debug("Reading file content from: %s", local_path)
            # Use the library's start_print_from_file which uploads and starts
            # For upload-only, we'd need to use the internal API
            # The library's _upload_print method accepts file content
            with open(local_path, "r", encoding="utf-8", errors="replace") as f:
                file_content = f.read()

            try:
                file_size = int(os.path.getsize(local_path))
                _LOGGER.debug("File size: %d bytes", file_size)
            except Exception:
                file_size = 0
            
            # Use the library's internal upload method
            _LOGGER.debug("Uploading file content to printer...")
            uploaded_name = self._printer._upload_print(file_content)
            _LOGGER.info(
                "File uploaded successfully: %s -> %s (size=%d bytes)",
                local_path, uploaded_name, file_size,
            )

            display_name = remote_name or uploaded_name

            with self._lock:
                self._sd_index[display_name] = {
                    "display": display_name,
                    "remote": uploaded_name,
                    "size": file_size,
                }
                _LOGGER.debug(
                    "Added to SD index: display=%s, remote=%s",
                    display_name, uploaded_name,
                )

                # Store the uploaded name for later use
                self._selected_file_display = display_name
                self._selected_file_remote = uploaded_name
                self._selected_file_size = file_size

            self._save_sd_index()
            return True
            
        except Exception as e:
            _LOGGER.error("Upload failed: %s", e)
            _LOGGER.debug("Upload error details", exc_info=True)
            return False

    # -------------------------------------------------------------------------
    # SD index management helpers (used by plugin API/UI)
    # -------------------------------------------------------------------------

    def clear_sd_index(self) -> None:
        """Clear the persisted SD index (best-effort)."""
        _LOGGER.info("Clearing SD file index")
        with self._lock:
            count = len(self._sd_index)
            self._sd_index = {}
            self._sd_files = []
            # Keep current selection intact; it's potentially the active job.
        _LOGGER.debug("Cleared %d entries from SD index", count)
        self._save_sd_index()

    def get_sd_index_snapshot(self) -> dict:
        """Return a snapshot of the SD index for UI display."""
        with self._lock:
            items = list(self._sd_index.values())
        # Stable ordering for UI
        items.sort(key=lambda x: (str(x.get("display") or "").lower()))
        return {
            "count": len(items),
            "items": items,
        }