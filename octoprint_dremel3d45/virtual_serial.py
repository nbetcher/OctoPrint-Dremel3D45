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
import os
import queue
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Optional, Tuple

from .vendor.dremel3dpy import Dremel3DPrinter, PRINT_COMMAND, default_request

if TYPE_CHECKING:
    from octoprint.settings import Settings

_LOGGER = logging.getLogger("octoprint.plugins.dremel3d45.virtual_serial")


class DremelVirtualSerial:
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
    - M24 (start/resume) -> Start or resume print
    - M25 (pause) -> Pause print
    - M27 (print status) -> Report print progress
    - M524 (abort) -> Cancel print
    - etc.
    """

    # When file size is unknown (external prints), use a synthetic size so
    # OctoPrint's progress tracking gets proportional byte counts.
    # Must be large enough to avoid OctoPrint's "current == total" end-of-print
    # check triggering prematurely, but the actual value is arbitrary since
    # progress is derived from the Dremel API percentage.
    _SYNTHETIC_FILE_SIZE = 1000000

    # TTL (seconds) for cached API data that changes rarely.
    # Printer info (firmware, serial, model) is effectively static.
    _PRINTER_INFO_TTL = 300  # 5 minutes
    # Extra status (max temps, storage, usage counter) changes rarely.
    _EXTRA_STATUS_TTL = 120  # 2 minutes

    # Dremel job phases that indicate an active print job.
    # Values from REST API: idle, preparing, building, completed, abort,
    #                       paused, pausing, resuming
    _ACTIVE_PHASES = frozenset({"preparing", "building", "paused", "pausing", "resuming"})

    # Dremel job phases that indicate a print has finished (success or cancel).
    _TERMINAL_PHASES = frozenset({"completed", "abort"})

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

        # Apply configured timeout to vendored library.
        # dremel3dpy imports REQUEST_TIMEOUT into its module namespace at import
        # time, so update BOTH the constants module and dremel3dpy module-level
        # variable to ensure all request sites see the new timeout.
        from .vendor import dremel3dpy as _dremel3dpy
        from .vendor.dremel3dpy.helpers import constants as _dremel_constants
        _dremel_constants.REQUEST_TIMEOUT = self._request_timeout
        _dremel3dpy.REQUEST_TIMEOUT = self._request_timeout

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
        self._selected_file_display: str = ""
        self._selected_file_remote: str = ""
        self._selected_file_size: int = 0
        self._printing = False
        self._paused = False
        self._was_printing = False  # Track previous state to detect print completion
        self._job_phase = "idle"  # Current Dremel API job phase
        self._last_job_phase = "idle"  # Previous job phase for transition detection
        self._completion_sent = False  # One-shot guard for completion messages
        self._last_announced_job_name = ""  # Last job name sent in File opened:
        self._progress = 0
        self._progress_from_host = False  # Guard: host-set M73 progress vs API
        self._elapsed_time = 0
        self._remaining_time = 0
        self._current_layer = 0
        self._total_layers = 0  # Total layers parsed from GCode at upload
        self._connection_errors = 0  # Track consecutive connection errors
        self._filament_type = ""  # Filament type from printer
        self._door_open = False  # Door sensor state
        self._fan_speed = 0  # Fan speed (read-only, can't control)

        # Timestamps for TTL-gated API refreshes
        self._printer_info_ts: float = 0.0
        self._extra_status_ts: float = 0.0

        # Auto-reporting controls (Marlin-style)
        self._autotemp_enabled = False
        self._autotemp_interval = 0
        self._last_autotemp_ts = 0.0

        # Read buffer for callers using read(size)
        self._read_buffer = bytearray()

        # Line number tracking for Marlin protocol
        self._current_line = 0
        self._expected_line: Optional[int] = None

        # Polling thread for status updates
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()
        self._poll_interval_active, self._poll_interval_idle = self._resolve_poll_intervals()
        # Backward-compatible alias used by older code paths and tests.
        self._poll_interval = self._poll_interval_active

        _LOGGER.debug(
            "Poll intervals: active=%ds, idle=%ds",
            self._poll_interval_active,
            self._poll_interval_idle,
        )

        # Lock for thread safety
        self._lock = threading.RLock()

        # Start communication
        self._start()

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

    def update_settings(self) -> None:
        """Re-read mutable settings from OctoPrint and apply them live.

        Called by the plugin's ``on_settings_save`` so that changed values
        (poll interval, request timeout) take effect without reconnecting.
        """
        new_timeout = self._settings.get_int(["request_timeout"]) or 30
        new_poll_active, new_poll_idle = self._resolve_poll_intervals()

        if new_timeout != self._request_timeout:
            _LOGGER.info("Request timeout changed: %ds → %ds", self._request_timeout, new_timeout)
            self._request_timeout = new_timeout
            from .vendor import dremel3dpy as _dremel3dpy
            from .vendor.dremel3dpy.helpers import constants as _dremel_constants
            _dremel_constants.REQUEST_TIMEOUT = new_timeout
            _dremel3dpy.REQUEST_TIMEOUT = new_timeout

        if (
            new_poll_active != self._poll_interval_active
            or new_poll_idle != self._poll_interval_idle
        ):
            _LOGGER.info(
                "Poll intervals changed: active %ds → %ds, idle %ds → %ds",
                self._poll_interval_active,
                new_poll_active,
                self._poll_interval_idle,
                new_poll_idle,
            )
            self._poll_interval_active = new_poll_active
            self._poll_interval_idle = new_poll_idle
            self._poll_interval = new_poll_active

    def _resolve_poll_intervals(self) -> Tuple[int, int]:
        """Resolve active/idle poll intervals from settings.

        Uses ``poll_interval_printing`` and ``poll_interval_idle`` when set,
        with ``poll_interval`` as a legacy fallback for backward compatibility.
        """
        legacy_poll = self._settings.get_int(["poll_interval"])
        if legacy_poll is None:
            legacy_poll = 10
        legacy_poll = max(1, int(legacy_poll))

        configured_active = self._settings.get_int(["poll_interval_printing"])
        if configured_active is None:
            configured_active = legacy_poll
        active_interval = max(1, int(configured_active))

        configured_idle = self._settings.get_int(["poll_interval_idle"])
        if configured_idle is None:
            configured_idle = max(active_interval, legacy_poll)
        idle_interval = max(1, int(configured_idle))

        if idle_interval < active_interval:
            _LOGGER.warning(
                "poll_interval_idle (%ds) cannot be lower than poll_interval_printing (%ds); "
                "clamping to %ds",
                idle_interval,
                active_interval,
                active_interval,
            )
            idle_interval = active_interval

        return active_interval, idle_interval

    def _current_poll_interval(self) -> int:
        """Return the currently applicable poll interval.

        Use a faster cadence during active prints and a slower cadence while idle.
        """
        if self._printing or self._paused:
            return self._poll_interval_active
        return self._poll_interval_idle

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
        # NOTE: We only send "start" here. OctoPrint will respond by
        # sending M110 (line number reset) and M115 (firmware info
        # request). Our M115 handler sends the FIRMWARE_NAME and Cap:
        # lines in response.  Sending capabilities here eagerly causes
        # race conditions with OctoPrint's M110 reset, leading to
        # "resend request" warnings.
        self._send("")  # Empty line
        self._send("start")

        # Try to connect to printer using dremel3dpy library
        try:
            _LOGGER.debug("Creating Dremel3DPrinter instance for host: %s", self._host)
            self._printer = Dremel3DPrinter(self._host)
            # Explicitly fetch printer info (constructor no longer auto-refreshes)
            self._printer.set_printer_info(refresh=True)
            self._printer_info_ts = time.time()
            self._connected = True
            _LOGGER.info("Connected to Dremel printer at %s", self._host)

            # Get firmware version from library
            firmware = self._printer.get_firmware_version() or "Unknown"
            _LOGGER.info("Printer firmware version: %s", firmware)

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
        if self._closed:
            return
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

    # Pre-compiled pattern for parenthetical GCode comments.
    _COMMENT_PAREN_RE: re.Pattern[str] = re.compile(r"\([^)]*\)")

    def _strip_comments(self, line: str) -> str:
        """Remove common GCode comment styles."""
        # Remove parenthetical comments
        line = self._COMMENT_PAREN_RE.sub("", line)
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
                self._send(
                    f"Error:checksum mismatch, Last Line: {self._current_line or 0}"
                )
                if line_number is not None:
                    self._send(f"Resend: {line_number}")
                return

        # Peek at the command *before* the sequence check so we can
        # recognise M110 (set line number).  Marlin exempts M110 from the
        # sequence check because its entire purpose is to reset the counter.
        peek_command = line_for_checksum
        if match:
            peek_command = peek_command[match.end():]
        peek_command = self._strip_comments(peek_command)
        is_m110 = peek_command.split()[0].upper() == "M110" if peek_command.split() else False

        # Line number sequencing (best-effort, only if host uses N-lines).
        # M110 is exempt — it resets the expected line counter.
        if line_number is not None:
            if is_m110:
                # M110 resets the line numbering; accept whatever N-value
                # the host provides and skip the sequence check.
                self._expected_line = line_number
            elif self._expected_line is None:
                self._expected_line = line_number
            elif line_number != self._expected_line:
                self._send(
                    f"Error:Line Number is not Last Line Number+1, Last Line: {self._current_line or 0}"
                )
                self._send(f"Resend: {self._expected_line}")
                return

        # Remove line number + checksum, then strip comments
        command = peek_command

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
        """Report temperatures (from poll cache)."""
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
        printer = self._printer
        if not printer:
            _LOGGER.warning("M115 requested but not connected")
            self._send("Error: Not connected")
            self._send("ok")
            return
        
        # Refresh printer info only if cache is stale (static data).
        now = time.time()
        if now - self._printer_info_ts > self._PRINTER_INFO_TTL:
            _LOGGER.debug("Refreshing printer info for M115 (cache stale)")
            try:
                printer.set_printer_info(refresh=True)
                self._printer_info_ts = now
            except Exception as e:
                _LOGGER.debug("set_printer_info refresh failed (using cache): %s", e)
        else:
            _LOGGER.debug("Using cached printer info for M115")
        
        machine = printer.get_title() or "Dremel 3D45"
        firmware = printer.get_firmware_version() or "Unknown"
        serial = printer.get_serial_number() or "Unknown"
        
        _LOGGER.debug(
            "Firmware info: machine=%s, firmware=%s, serial=%s",
            machine, firmware, serial,
        )
        
        # Include UUID for plugin compatibility
        self._send(
            f"FIRMWARE_NAME:Dremel3D45 {firmware}"
            f" PROTOCOL_VERSION:1.0"
            f" MACHINE_TYPE:{machine}"
            f" EXTRUDER_COUNT:1"
            f" UUID:{serial}"
        )
        self._send("Cap:AUTOREPORT_TEMP:1")
        self._send("Cap:AUTOREPORT_POS:0")
        self._send("Cap:EEPROM:0")
        self._send("Cap:VOLUMETRIC:0")
        self._send("Cap:THERMAL_PROTECTION:1")
        self._send("Cap:CHAMBER_TEMPERATURE:1")
        self._send("Cap:BUILD_PERCENT:1")
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
        """Report endstop status (simulated + door from poll cache)."""
        door_status = "TRIGGERED" if self._door_open else "open"

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



    def _gcode_M24(self, command: str) -> None:
        """Start/resume SD print."""
        printer = self._printer
        if not printer:
            _LOGGER.warning("M24: Cannot start print - not connected")
            self._send("Error: Not connected")
            self._send("ok")
            return
            
        if self._paused:
            # Resume using library method
            _LOGGER.info("Resuming paused print")
            printer.resume_print()
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
                # Don't set _printing here; poll thread will discover the
                # phase transition ("preparing"/"building") authoritatively.
                # Mark as announced so poll doesn't re-emit File opened.
                self._was_printing = True
                self._last_announced_job_name = self._selected_file_display
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
        printer = self._printer
        if self._printing and printer:
            _LOGGER.info("Pausing print")
            try:
                success = printer.pause_print()
                if success:
                    self._paused = True
                    _LOGGER.debug("Print paused successfully")
                else:
                    _LOGGER.warning("Pause command returned failure")
            except Exception as e:
                _LOGGER.error("Failed to pause print: %s", e)
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
        """Report SD print status."""
        if self._printing or self._paused:
            # Format: SD printing byte X/Y
            total = int(self._selected_file_size or self._SYNTHETIC_FILE_SIZE)
            printed = int((float(self._progress) / 100.0) * float(total))
            self._send(f"SD printing byte {printed}/{total}")
            _LOGGER.debug("SD status: progress=%.1f%%, layer=%d", self._progress, self._current_layer)
        else:
            _LOGGER.debug("SD status: not printing")
            self._send("Not SD printing")
        self._send("ok")

    def _gcode_M524(self, command: str) -> None:
        """Abort SD print (Marlin 2.0+)."""
        _LOGGER.info("Aborting print (M524)")
        printer = self._printer
        if printer:
            try:
                printer.stop_print()
                _LOGGER.debug("Stop command sent to printer")
            except Exception as e:
                _LOGGER.error("Failed to stop print on M524: %s", e)
                self._send(f"Error: {e}")
                self._send("ok")
                return
        with self._lock:
            self._printing = False
            self._paused = False
            self._selected_file_display = ""
            self._selected_file_remote = ""
            self._selected_file_size = 0
            # Sync phase-tracking state to prevent poll from re-triggering transitions
            self._was_printing = False
            self._job_phase = "idle"
            self._last_job_phase = "idle"
            self._completion_sent = True  # Suppress redundant completion from poll
            self._last_announced_job_name = ""
            self._progress_from_host = False
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
                self._send(f"Error: {e}")
                self._send("ok")
                return
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
                self._send(f"Error: {e}")
                self._send("ok")
                return
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
                self._send(f"Error: {e}")
                self._send("ok")
                return
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
                self._send(f"Error: {e}")
                self._send("ok")
                return
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
        printer = self._printer
        if printer:
            _LOGGER.info("Sending stop command to printer")
            try:
                printer.stop_print()
            except Exception as e:
                _LOGGER.error("Failed to stop print on M112: %s", e)
                self._send(f"Error: {e}")
                self._send("ok")
                return
        with self._lock:
            self._printing = False
            self._paused = False
            self._selected_file_display = ""
            self._selected_file_remote = ""
            self._selected_file_size = 0
            # Sync phase-tracking state to prevent poll from re-triggering transitions
            self._was_printing = False
            self._job_phase = "idle"
            self._last_job_phase = "idle"
            self._completion_sent = True  # Suppress redundant completion from poll
            self._last_announced_job_name = ""
            self._progress_from_host = False
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
        """Initialize SD card (no-op — no SD card support)."""
        self._send("ok")

    def _gcode_M22(self, command: str) -> None:
        """Release SD card (no-op)."""
        self._send("ok")

    def _gcode_M73(self, command: str) -> None:
        """Set build progress (best-effort). Format: M73 P<percent> [R<min>]

        Host-set progress is used until the next API refresh, which is
        authoritative for the Dremel printer.
        """
        match = re.search(r"P(\d+)", command)
        if match:
            try:
                self._progress = float(match.group(1))
                self._progress_from_host = True
            except Exception:
                pass
        self._send("ok")

    def _gcode_M532(self, command: str) -> None:
        """Report job progress with layer info (Prusa-style, from poll cache).
        
        Format: X:<percent> L:<layer>
        Some hosts (OctoPrint plugins) parse this for layer display.
        """
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
        if self._closed:
            return

        # Grab reference under lock to avoid race with close()
        printer = self._printer
        if not printer:
            return

        try:
            # Refresh all data sources from the Dremel API, matching the
            # pattern used by the Home Assistant integration which calls
            # printer.refresh().  We call each individually so a failure
            # in one (e.g. set_extra_status on HTTPS) doesn't block the
            # others from updating.
            try:
                printer.set_job_status(refresh=True)
            except Exception as e:
                _LOGGER.debug("set_job_status failed: %s", e)
                raise  # temperatures etc. depend on this; abort cycle

            # Printer info changes rarely; refresh it only when the TTL
            # expires.  Errors are non-fatal — cached data is used.
            now = time.time()
            if now - self._printer_info_ts > self._PRINTER_INFO_TTL:
                try:
                    printer.set_printer_info(refresh=True)
                    self._printer_info_ts = now
                except Exception as e:
                    _LOGGER.debug("set_printer_info failed (non-fatal): %s", e)

            # Extra status (max temps, storage) via HTTPS port 11134.
            # Also non-fatal and TTL-gated — cached or zero values are
            # acceptable.
            if now - self._extra_status_ts > self._EXTRA_STATUS_TTL:
                try:
                    printer.set_extra_status(refresh=True)
                    self._extra_status_ts = now
                except Exception as e:
                    _LOGGER.debug("set_extra_status failed (non-fatal): %s", e)

            with self._lock:
                # Get temperatures from API
                tool_actual = float(printer.get_temperature_type("extruder") or 0)
                bed_actual = float(printer.get_temperature_type("platform") or 0)
                chamber_actual = float(printer.get_temperature_type("chamber") or 0)

                tool_attrs = printer.get_temperature_attributes("extruder") or {}
                bed_attrs = printer.get_temperature_attributes("platform") or {}

                tool_target = float(tool_attrs.get("target_temp", 0) or 0)
                bed_target = float(bed_attrs.get("target_temp", 0) or 0)

                self._temps = {
                    "tool0": (tool_actual, tool_target),
                    "bed": (bed_actual, bed_target),
                    "chamber": (chamber_actual, 0),
                }

                # Get job phase directly from the Dremel API
                try:
                    phase = (printer.get_printing_status() or "idle").strip().lower()
                except Exception:
                    phase = "idle"

                self._last_job_phase = self._job_phase
                self._job_phase = phase

                # Derive printing/paused state from phase
                is_active = phase in self._ACTIVE_PHASES
                is_terminal = phase in self._TERMINAL_PHASES
                self._printing = phase in ("building", "preparing", "resuming")
                self._paused = phase in ("paused", "pausing")

                # Get job name for external print detection
                try:
                    job_name = (printer.get_job_name() or "").strip()
                except Exception:
                    job_name = ""

                was_active = self._was_printing

                # ----------------------------------------------------------
                # Transition: idle/terminal → active (print started)
                # ----------------------------------------------------------
                if is_active and not was_active:
                    _LOGGER.info(
                        "Print started (phase=%s): job=%s",
                        phase, job_name or "(unknown)",
                    )
                    self._completion_sent = False

                    # Sync job name into selected file tracking
                    if job_name:
                        self._selected_file_remote = job_name
                        if not self._selected_file_display:
                            self._selected_file_display = job_name
                    elif not self._selected_file_display:
                        self._selected_file_display = "unknown_job.gcode"
                        self._selected_file_remote = "unknown_job.gcode"

                    # Ensure we have a usable file size.
                    if not self._selected_file_size:
                        self._selected_file_size = self._SYNTHETIC_FILE_SIZE

                    # Tell OctoPrint a file is selected so it enters
                    # the SD printing state when it sees progress bytes.
                    file_display = self._selected_file_display or "unknown_job.gcode"
                    file_size = int(self._selected_file_size)
                    self._send(f"File opened: {file_display} Size: {file_size}")
                    self._send("File selected")
                    self._last_announced_job_name = file_display

                # ----------------------------------------------------------
                # Active → active: check for late job-name discovery
                # ----------------------------------------------------------
                elif is_active and was_active:
                    if (
                        job_name
                        and self._last_announced_job_name
                        and self._last_announced_job_name == "unknown_job.gcode"
                        and job_name != self._last_announced_job_name
                    ):
                        # Real job name appeared after we used a placeholder;
                        # re-announce so OctoPrint updates its file display.
                        _LOGGER.info(
                            "Late job name discovered: %s (was %s)",
                            job_name, self._last_announced_job_name,
                        )
                        self._selected_file_remote = job_name
                        self._selected_file_display = job_name
                        if not self._selected_file_size:
                            self._selected_file_size = self._SYNTHETIC_FILE_SIZE

                        file_display = self._selected_file_display
                        file_size = int(self._selected_file_size)
                        self._send(f"File opened: {file_display} Size: {file_size}")
                        self._send("File selected")
                        self._last_announced_job_name = file_display

                # ----------------------------------------------------------
                # Transition: active → terminal (completed / abort)
                # ----------------------------------------------------------
                elif is_terminal and was_active and not self._completion_sent:
                    _LOGGER.info("Print finished (phase=%s)", phase)
                    # Send final 100% progress so OctoPrint marks print done
                    total = int(self._selected_file_size or self._SYNTHETIC_FILE_SIZE)
                    self._send(f"SD printing byte {total}/{total}")
                    self._send("Not SD printing")
                    self._selected_file_display = ""
                    self._selected_file_remote = ""
                    self._selected_file_size = 0
                    self._last_announced_job_name = ""
                    self._total_layers = 0
                    self._progress_from_host = False
                    self._completion_sent = True

                # ----------------------------------------------------------
                # Terminal → idle: clear completion guard
                # ----------------------------------------------------------
                elif phase == "idle" and self._last_job_phase in self._TERMINAL_PHASES:
                    self._completion_sent = False

                self._was_printing = is_active

                api_progress = float(printer.get_printing_progress() or 0)
                if not self._progress_from_host:
                    self._progress = api_progress
                else:
                    # Host-set progress (via M73) takes precedence for one
                    # poll cycle; API is authoritative and resumes next time.
                    self._progress_from_host = False
                self._elapsed_time = int(printer.get_elapsed_time() or 0)
                self._remaining_time = int(printer.get_remaining_time() or 0)
                self._current_layer = int(printer.get_layer() or 0)

                # Capture additional sensor data
                try:
                    self._door_open = printer.is_door_open()
                except Exception:
                    pass
                try:
                    job_status_dict = printer.get_job_status() or {}
                    self._filament_type = str(job_status_dict.get("filament", "") or "").strip()
                    self._fan_speed = int(job_status_dict.get("fan_speed", 0) or 0)
                except Exception:
                    pass

                # Reset error counter on successful refresh
                self._connection_errors = 0

                # Best-effort: keep selected file in sync with the active job name
                if is_active and job_name:
                    self._selected_file_remote = job_name

        except Exception as e:
            self._connection_errors += 1
            if self._connection_errors <= 3:
                _LOGGER.warning("Error refreshing status (attempt %d): %s", self._connection_errors, e)
            elif self._connection_errors == 4:
                _LOGGER.error("Persistent connection errors - printer may be offline")
                # Prevent stale "printing" state when backend status can no
                # longer be refreshed for multiple cycles.
                with self._lock:
                    if self._was_printing or self._printing or self._paused:
                        self._send("Not SD printing")
                    self._printing = False
                    self._paused = False
                    self._was_printing = False
                    self._job_phase = "idle"
            # After 3 errors, only log at debug level to avoid log spam
            else:
                _LOGGER.debug("Error refreshing status: %s", e)

    # -------------------------------------------------------------------------
    # Status Polling
    # -------------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Background thread to poll printer status."""
        _LOGGER.info("Starting status polling thread")

        while True:
            interval = self._current_poll_interval()
            if self._poll_stop.wait(interval):
                break
            if self._closed:
                break
            try:
                self._refresh_status()

                now = time.time()
                is_active = self._printing or self._paused

                # Auto-report temperature (Marlin-style) if enabled, or if printing
                should_report_temp = is_active or self._autotemp_enabled
                if should_report_temp:
                    interval = self._autotemp_interval if self._autotemp_enabled else 0
                    if interval <= 0 or (now - self._last_autotemp_ts) >= float(interval):
                        t0 = self._temps.get("tool0", (0, 0))
                        bed = self._temps.get("bed", (0, 0))
                        chamber = self._temps.get("chamber", (0, 0))
                        self._send(
                            f"T:{t0[0]:.1f} /{t0[1]:.1f} B:{bed[0]:.1f} /{bed[1]:.1f}"
                            f" C:{chamber[0]:.1f} /{chamber[1]:.1f}"
                        )
                        self._last_autotemp_ts = now

                # -------------------------------------------------------
                # SD progress: always report during active prints so
                # OctoPrint transitions into "Printing from SD" even for
                # external (touchscreen) prints.  Also honour M27 auto-
                # report if enabled.
                # -------------------------------------------------------
                if is_active:
                    total = int(self._selected_file_size or self._SYNTHETIC_FILE_SIZE)
                    printed = int((float(self._progress) / 100.0) * float(total))
                    self._send(f"SD printing byte {printed}/{total}")

                    # M73 progress report — OctoPrint 1.9+ parses this
                    # from firmware output and uses it for the state panel
                    # "Printed", "Print Time Left", and "Approx Total
                    # Print Time" fields.
                    remaining_min = max(int(self._remaining_time / 60), 0)
                    self._send(
                        f"M73 P{int(self._progress)} R{remaining_min}"
                    )

                    # Proactive layer reporting for plugins like
                    # DisplayLayerProgress which parse //action:notification
                    # lines from serial output.
                    if self._current_layer > 0:
                        if self._total_layers > 0:
                            self._send(
                                f"//action:notification Layer {self._current_layer}/{self._total_layers}"
                            )
                        else:
                            self._send(
                                f"//action:notification Layer {self._current_layer}"
                            )

            except Exception as e:
                _LOGGER.debug("Poll error: %s", e)

        _LOGGER.info("Status polling thread stopped")

    # -------------------------------------------------------------------------
    # File Upload
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

        if self._closed:
            _LOGGER.error("Cannot upload: connection is closed")
            return False

        printer = self._printer
        if not printer:
            _LOGGER.error("Cannot upload: not connected to printer")
            return False

        if self._is_print_active():
            _LOGGER.error("Cannot upload: print in progress")
            return False
            
        try:
            try:
                file_size = int(os.path.getsize(local_path))
                _LOGGER.debug("File size: %d bytes", file_size)
            except Exception:
                file_size = 0
            
            # Stream file content to the printer to avoid high memory usage
            # on low-resource hosts (e.g., Raspberry Pi).
            _LOGGER.debug("Uploading file stream to printer: %s", local_path)
            with open(local_path, "rb") as f:
                uploaded_name = printer._upload_print(f)
            _LOGGER.info(
                "File uploaded successfully: %s -> %s (size=%d bytes)",
                local_path, uploaded_name, file_size,
            )

            display_name = remote_name or uploaded_name

            with self._lock:
                self._selected_file_display = display_name
                self._selected_file_remote = uploaded_name
                self._selected_file_size = file_size

            return True
            
        except Exception as e:
            _LOGGER.error("Upload failed: %s", e)
            _LOGGER.debug("Upload error details", exc_info=True)
            return False
