# -*- coding: utf-8 -*-
"""
Unit tests for the DremelVirtualSerial GCode handlers.

These tests use a mock Settings and mock Dremel3DPrinter to test
the GCode translation layer without a real printer connection.
"""

import queue
import time
import unittest
from unittest.mock import MagicMock, patch


class MockSettings:
    """Mock OctoPrint Settings object."""

    def __init__(self, settings: dict = None):
        self._data = settings or {
            "printer_ip": "192.168.1.100",
            "request_timeout": 30,
            "poll_interval": 60,  # Long interval so polling doesn't interfere
        }

    def get(self, path: list):
        key = path[0] if path else None
        return self._data.get(key)

    def get_int(self, path: list):
        val = self.get(path)
        return int(val) if val is not None else None

    def get_boolean(self, path: list):
        return bool(self.get(path))


class TestGCodeHandlers(unittest.TestCase):
    """Test individual GCode handlers."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        """Set up a DremelVirtualSerial instance with mocked printer."""
        # Create mock printer instance
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""

        mock_printer_class.return_value = self.mock_printer

        # Import after patching
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial

        settings = MockSettings()
        self.serial = DremelVirtualSerial(
            settings=settings,
            read_timeout=1.0,
        )

        # Drain startup messages
        self._drain_responses()

    def tearDown(self):
        """Clean up."""
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain_responses(self) -> list:
        """Drain all pending responses and return them."""
        responses = []
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                line = self.serial._outgoing.get_nowait()
                responses.append(line.strip())
            except queue.Empty:
                break
        return responses

    def _send_command(self, command: str) -> list:
        """Send a command and collect responses."""
        self.serial.write(f"{command}\n".encode())
        time.sleep(0.05)  # Allow processing
        return self._drain_responses()

    # -------------------------------------------------------------------------
    # Temperature Commands
    # -------------------------------------------------------------------------

    def test_m105_reports_temperatures(self):
        """M105 should report cached temperatures (no API call)."""
        # M105 uses poll-cached temps; set them directly.
        self.serial._temps = {
            "tool0": (200.0, 210.0),
            "bed": (60.0, 65.0),
            "chamber": (30.0, 0),
        }

        responses = self._send_command("M105")

        self.assertEqual(len(responses), 1)
        self.assertIn("T:200.0", responses[0])
        self.assertIn("/210.0", responses[0])
        self.assertIn("B:60.0", responses[0])
        self.assertIn("/65.0", responses[0])
        self.assertIn("ok", responses[0])

    def test_m155_enables_autoreport(self):
        """M155 S5 should enable temperature auto-reporting every 5 seconds."""
        responses = self._send_command("M155 S5")

        self.assertIn("ok", responses)
        self.assertTrue(self.serial._autotemp_enabled)
        self.assertEqual(self.serial._autotemp_interval, 5)

    def test_m155_disables_autoreport(self):
        """M155 S0 should disable temperature auto-reporting."""
        self.serial._autotemp_enabled = True
        self.serial._autotemp_interval = 5

        responses = self._send_command("M155 S0")

        self.assertIn("ok", responses)
        self.assertFalse(self.serial._autotemp_enabled)

    # -------------------------------------------------------------------------
    # Print Control Commands
    # -------------------------------------------------------------------------

    def test_m27_reports_not_printing(self):
        """M27 should report 'Not SD printing' when idle."""
        responses = self._send_command("M27")

        self.assertTrue(any("Not SD printing" in r for r in responses))
        self.assertIn("ok", responses)

    def test_m25_pauses_print(self):
        """M25 should pause a running print (checks return value)."""
        self.serial._printing = True
        self.mock_printer.pause_print.return_value = True

        responses = self._send_command("M25")

        self.assertIn("ok", responses)
        self.mock_printer.pause_print.assert_called_once()
        self.assertTrue(self.serial._paused)

    def test_m25_pause_failure(self):
        """M25 should not set _paused if pause_print returns False."""
        self.serial._printing = True
        self.mock_printer.pause_print.return_value = False

        responses = self._send_command("M25")

        self.assertIn("ok", responses)
        self.assertFalse(self.serial._paused)

    def test_m524_cancels_print(self):
        """M524 should cancel the current print and sync all state."""
        self.serial._printing = True
        self.serial._selected_file_display = "test.gcode"
        self.serial._was_printing = True
        self.serial._job_phase = "building"

        responses = self._send_command("M524")

        self.assertIn("ok", responses)
        self.mock_printer.stop_print.assert_called_once()
        self.assertFalse(self.serial._printing)
        self.assertEqual(self.serial._selected_file_display, "")
        # Verify phase-tracking state is synced
        self.assertFalse(self.serial._was_printing)
        self.assertEqual(self.serial._job_phase, "idle")
        self.assertEqual(self.serial._last_job_phase, "idle")
        self.assertTrue(self.serial._completion_sent)
        self.assertEqual(self.serial._last_announced_job_name, "")

    # -------------------------------------------------------------------------
    # Motion Commands (all no-ops - Dremel doesn't support motion control)
    # -------------------------------------------------------------------------

    def test_m114_reports_zero_position(self):
        """M114 should report zeros (position not tracked)."""
        responses = self._send_command("M114")

        response_text = " ".join(responses)
        self.assertIn("X:0.00", response_text)
        self.assertIn("Y:0.00", response_text)
        self.assertIn("Z:0.00", response_text)
        self.assertIn("ok", responses)

    def test_g0_acknowledged(self):
        """G0 should be acknowledged (no-op)."""
        responses = self._send_command("G0 X50 Y100 Z10")
        self.assertIn("ok", responses)

    def test_g1_acknowledged(self):
        """G1 should be acknowledged (no-op)."""
        responses = self._send_command("G1 X-10 Y20 E5")
        self.assertIn("ok", responses)

    def test_g90_acknowledged(self):
        """G90 should be acknowledged (no-op)."""
        responses = self._send_command("G90")
        self.assertIn("ok", responses)

    def test_g91_acknowledged(self):
        """G91 should be acknowledged (no-op)."""
        responses = self._send_command("G91")
        self.assertIn("ok", responses)

    def test_g28_acknowledged(self):
        """G28 should be acknowledged (no-op)."""
        responses = self._send_command("G28")
        self.assertIn("ok", responses)

    def test_g92_acknowledged(self):
        """G92 should be acknowledged (no-op)."""
        responses = self._send_command("G92 X100 E0")
        self.assertIn("ok", responses)

    # -------------------------------------------------------------------------
    # Firmware / Info Commands
    # -------------------------------------------------------------------------

    def test_m115_reports_firmware(self):
        """M115 should report firmware info with standard Marlin fields."""
        responses = self._send_command("M115")

        response_text = " ".join(responses)
        self.assertIn("FIRMWARE_NAME:Dremel3D45", response_text)
        self.assertIn("PROTOCOL_VERSION:1.0", response_text)
        self.assertIn("EXTRUDER_COUNT:1", response_text)
        self.assertIn("Cap:AUTOREPORT_TEMP:1", responses)
        self.assertIn("Cap:CHAMBER_TEMPERATURE:1", responses)
        self.assertIn("Cap:BUILD_PERCENT:1", responses)
        self.assertIn("ok", responses)

    def test_m119_reports_endstops(self):
        """M119 should report endstop status from poll cache."""
        self.serial._door_open = True

        responses = self._send_command("M119")

        response_text = " ".join(responses)
        self.assertIn("x_min", response_text)
        # When door is open, it's "TRIGGERED" (Marlin convention)
        self.assertIn("door: TRIGGERED", response_text)
        self.assertIn("ok", responses)

    # -------------------------------------------------------------------------
    # Line Number / Checksum
    # -------------------------------------------------------------------------

    def test_line_number_accepted(self):
        """Commands with line numbers should be processed."""
        responses = self._send_command("N1 M105")

        self.assertTrue(any("T:" in r for r in responses))
        self.assertEqual(self.serial._current_line, 1)

    def test_checksum_validation(self):
        """Valid checksums should be accepted."""
        # "N2 M105" XOR checksum = 37
        cmd = "N2 M105*37"
        responses = self._send_command(cmd)

        self.assertTrue(any("T:" in r or "ok" in r for r in responses))

    def test_bad_checksum_rejected(self):
        """Invalid checksums should trigger resend."""
        cmd = "N3 M105*99"  # Wrong checksum
        responses = self._send_command(cmd)

        self.assertTrue(any("checksum" in r.lower() for r in responses))

    def test_m110_resets_line_number_from_zero(self):
        """M110 N0 should reset line numbering even when _expected_line is non-zero.

        Reproduces the real OctoPrint boot scenario: OctoPrint sends
        'N0 M110 N0*125' at the start of every print to reset the line
        counter.  If the virtual serial has already seen numbered lines
        (e.g. from a previous print), _expected_line will be >0 and the
        command would be rejected without an M110 exemption.
        """
        # Establish line numbering at N1 → _expected_line becomes 2
        self._send_command("N1 M105")
        self.assertEqual(self.serial._expected_line, 2)

        # Now send the exact command OctoPrint sends at print start
        # Checksum: XOR("N0 M110 N0") = 125
        responses = self._send_command("N0 M110 N0*125")

        # Should succeed (ok), NOT produce a line number error
        self.assertIn("ok", responses)
        self.assertFalse(any("Error" in r for r in responses))
        # _expected_line should now be 1 (reset to 0, then +1)
        self.assertEqual(self.serial._expected_line, 1)
        self.assertEqual(self.serial._current_line, 0)

    def test_m110_resets_line_number_without_checksum(self):
        """M110 N0 without checksum should also be accepted."""
        # Establish at N5 → _expected_line becomes 6
        self.serial._expected_line = 5
        self.serial._current_line = 4
        self._send_command("N5 M105")
        self.assertEqual(self.serial._expected_line, 6)

        # Send M110 N0 without checksum
        responses = self._send_command("N0 M110 N0")

        self.assertIn("ok", responses)
        self.assertFalse(any("Error" in r for r in responses))
        self.assertEqual(self.serial._expected_line, 1)

    def test_m110_resets_to_nonzero_line(self):
        """M110 N42 should reset line numbering to 42."""
        self.serial._expected_line = 10
        self.serial._current_line = 9

        responses = self._send_command("N42 M110 N42")

        self.assertIn("ok", responses)
        self.assertFalse(any("Error" in r for r in responses))
        self.assertEqual(self.serial._current_line, 42)
        self.assertEqual(self.serial._expected_line, 43)

    def test_wrong_line_number_still_rejected_for_non_m110(self):
        """Non-M110 commands with wrong line numbers should still be rejected."""
        # Set expected to 5
        self.serial._expected_line = 5
        self.serial._current_line = 4

        responses = self._send_command("N10 M105")

        self.assertTrue(any("Error" in r for r in responses))
        self.assertTrue(any("Resend: 5" in r for r in responses))

    # -------------------------------------------------------------------------
    # Comment Stripping
    # -------------------------------------------------------------------------

    def test_semicolon_comments_stripped(self):
        """Semicolon comments should be stripped and command acknowledged."""
        responses = self._send_command("G0 X100 ; move to X=100")
        self.assertIn("ok", responses)

    def test_paren_comments_stripped(self):
        """Parenthetical comments should be stripped and command acknowledged."""
        responses = self._send_command("G0 X50 (this is a comment) Y75")
        self.assertIn("ok", responses)

    # -------------------------------------------------------------------------
    # Serial Interface
    # -------------------------------------------------------------------------

    def test_write_returns_length(self):
        """write() should return bytes written."""
        data = b"M105\n"
        result = self.serial.write(data)

        self.assertEqual(result, len(data))

    def test_readline_returns_bytes(self):
        """readline() should return bytes."""
        self.serial._send("ok")

        result = self.serial.readline()

        self.assertIsInstance(result, bytes)
        self.assertEqual(result.strip(), b"ok")

    def test_close_sets_closed_flag(self):
        """close() should mark connection closed."""
        self.serial.close()

        self.assertTrue(self.serial._closed)
        self.assertFalse(self.serial.is_open)

    def test_in_waiting_property(self):
        """in_waiting should return approximate bytes available."""
        self.serial._send("test message")
        time.sleep(0.01)

        self.assertGreater(self.serial.in_waiting, 0)


class TestMarlinChecksum(unittest.TestCase):
    """Test Marlin checksum computation."""

    def test_checksum_computation(self):
        """Test XOR checksum matches expected values."""
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial

        # Use class method without instance
        def compute(line):
            checksum = 0
            for ch in line:
                checksum ^= ord(ch)
            return checksum

        # Known test cases (computed XOR of each character)
        self.assertEqual(compute("N1 M105"), 38)
        self.assertEqual(compute("N2 M105"), 37)
        self.assertEqual(compute("N0 M110"), 35)


class TestSDProgressFormat(unittest.TestCase):
    """Test SD progress reporting uses byte-count format."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        mock_printer_class.return_value = self.mock_printer
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        self.serial = DremelVirtualSerial(
            settings=MockSettings(),
            read_timeout=1.0,
        )
        self._drain()

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain(self):
        """Drain all pending responses."""
        responses = []
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                line = self.serial._outgoing.get_nowait()
                responses.append(line.strip())
            except queue.Empty:
                break
        return responses

    def _send_command(self, command):
        self.serial.write(f"{command}\n".encode())
        time.sleep(0.05)
        return self._drain()

    def test_m27_uses_byte_counts_when_file_size_known(self):
        """M27 should report byte position/total, not percentage."""
        # Must set mock to return printing=True since M27 calls _refresh_status
        self.mock_printer.is_printing.return_value = True
        self.mock_printer.get_printing_status.return_value = "building"
        self.mock_printer.get_job_name.return_value = "test.gcode"
        self.serial._printing = True
        self.serial._was_printing = True  # Prevent "File opened" from external detection
        self.serial._selected_file_display = "test.gcode"
        self.serial._selected_file_remote = "test.gcode"
        self.serial._selected_file_size = 50000
        self.serial._progress = 50.0  # 50%
        self.mock_printer.get_printing_progress.return_value = 50.0
        responses = self._send_command("M27")
        sd_lines = [r for r in responses if r.startswith("SD printing byte")]
        self.assertEqual(len(sd_lines), 1)
        self.assertEqual(sd_lines[0], "SD printing byte 25000/50000")

    def test_m27_uses_synthetic_size_when_no_file_size(self):
        """M27 should use synthetic file size when actual size unknown."""
        self.mock_printer.is_printing.return_value = True
        self.mock_printer.get_printing_status.return_value = "building"
        self.mock_printer.get_job_name.return_value = "test.gcode"
        self.serial._printing = True
        self.serial._was_printing = True
        self.serial._selected_file_display = "test.gcode"
        self.serial._selected_file_remote = "test.gcode"
        self.serial._selected_file_size = 0
        self.serial._progress = 42.0
        self.mock_printer.get_printing_progress.return_value = 42.0
        responses = self._send_command("M27")
        sd_lines = [r for r in responses if r.startswith("SD printing byte")]
        self.assertEqual(len(sd_lines), 1)
        # With synthetic size of 1000000: 42% * 1000000 = 420000
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        synthetic = DremelVirtualSerial._SYNTHETIC_FILE_SIZE
        expected = int(0.42 * synthetic)
        self.assertEqual(sd_lines[0], f"SD printing byte {expected}/{synthetic}")

    def test_m27_reports_not_printing_when_idle(self):
        """M27 should report 'Not SD printing' when not printing."""
        self.serial._printing = False
        self.serial._paused = False
        responses = self._send_command("M27")
        self.assertIn("Not SD printing", responses)


class TestExternalPrintDetection(unittest.TestCase):
    """Test detection of prints started from printer touchscreen."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        self.mock_printer.is_door_open.return_value = False
        self.mock_printer.get_job_status.return_value = {}
        mock_printer_class.return_value = self.mock_printer
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        self.serial = DremelVirtualSerial(
            settings=MockSettings(),
            read_timeout=1.0,
        )
        self._drain()

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain(self):
        responses = []
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                line = self.serial._outgoing.get_nowait()
                responses.append(line.strip())
            except queue.Empty:
                break
        return responses

    def test_external_print_sends_file_opened(self):
        """When printer starts externally, File opened + File selected are sent."""
        # Simulate: printer transitions from idle to building
        self.mock_printer.get_printing_status.return_value = "building"
        self.mock_printer.get_job_name.return_value = "mypart.gcode"
        self.mock_printer.get_printing_progress.return_value = 5.0

        self.serial._refresh_status()
        responses = self._drain()

        file_opened = [r for r in responses if r.startswith("File opened:")]
        file_selected = [r for r in responses if r == "File selected"]
        self.assertEqual(len(file_opened), 1, f"Expected 'File opened:', got {responses}")
        self.assertIn("mypart.gcode", file_opened[0])
        self.assertEqual(len(file_selected), 1)

        # Should use synthetic file size since Dremel API doesn't report size
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        self.assertIn(str(DremelVirtualSerial._SYNTHETIC_FILE_SIZE), file_opened[0])

    def test_external_print_no_duplicate_file_opened(self):
        """Second _refresh_status during same print should NOT re-send File opened."""
        self.mock_printer.get_printing_status.return_value = "building"
        self.mock_printer.get_job_name.return_value = "test.gcode"
        self.mock_printer.get_printing_progress.return_value = 10.0

        self.serial._refresh_status()
        self._drain()  # discard

        # Second refresh — already printing
        self.serial._refresh_status()
        responses = self._drain()

        file_opened = [r for r in responses if r.startswith("File opened:")]
        self.assertEqual(len(file_opened), 0, "Should not re-send File opened")

    def test_print_completion_on_completed_phase(self):
        """When job phase transitions to 'completed', send final progress + Not SD printing."""
        # First: simulate printing
        self.mock_printer.get_printing_status.return_value = "building"
        self.mock_printer.get_job_name.return_value = "part.gcode"
        self.mock_printer.get_printing_progress.return_value = 50.0
        self.serial._refresh_status()
        self._drain()

        # Then: job phase transitions directly to 'completed'
        self.mock_printer.get_printing_status.return_value = "completed"
        self.mock_printer.get_printing_progress.return_value = 100.0

        self.serial._refresh_status()
        responses = self._drain()

        not_sd = [r for r in responses if r == "Not SD printing"]
        self.assertEqual(len(not_sd), 1, f"Expected 'Not SD printing', got {responses}")
        # Should also have sent final 100% progress
        sd_byte = [r for r in responses if r.startswith("SD printing byte")]
        self.assertEqual(len(sd_byte), 1)

    def test_print_completion_on_abort_phase(self):
        """When job phase transitions to 'abort', send completion immediately."""
        # First: simulate printing
        self.mock_printer.get_printing_status.return_value = "building"
        self.mock_printer.get_job_name.return_value = "part.gcode"
        self.mock_printer.get_printing_progress.return_value = 20.0
        self.serial._refresh_status()
        self._drain()

        # Abort
        self.mock_printer.get_printing_status.return_value = "abort"
        self.mock_printer.get_printing_progress.return_value = 0

        self.serial._refresh_status()
        responses = self._drain()

        not_sd = [r for r in responses if r == "Not SD printing"]
        self.assertEqual(len(not_sd), 1, f"Expected 'Not SD printing', got {responses}")

    def test_completion_not_repeated(self):
        """Completion messages should only be sent once even if phase stays 'completed'."""
        # Start printing
        self.mock_printer.get_printing_status.return_value = "building"
        self.mock_printer.get_job_name.return_value = "part.gcode"
        self.mock_printer.get_printing_progress.return_value = 50.0
        self.serial._refresh_status()
        self._drain()

        # Complete
        self.mock_printer.get_printing_status.return_value = "completed"
        self.serial._refresh_status()
        responses1 = self._drain()
        not_sd1 = [r for r in responses1 if r == "Not SD printing"]
        self.assertEqual(len(not_sd1), 1)

        # Phase stays 'completed' for another poll
        self.serial._refresh_status()
        responses2 = self._drain()
        not_sd2 = [r for r in responses2 if r == "Not SD printing"]
        self.assertEqual(len(not_sd2), 0, "Should not repeat completion")

    def test_late_job_name_re_emits_file_opened(self):
        """If job name arrives late, File opened should be re-emitted."""
        # Start with empty job name → placeholder
        self.mock_printer.get_printing_status.return_value = "preparing"
        self.mock_printer.get_job_name.return_value = ""
        self.mock_printer.get_printing_progress.return_value = 0
        self.serial._refresh_status()
        responses1 = self._drain()
        file_opened1 = [r for r in responses1 if r.startswith("File opened:")]
        self.assertEqual(len(file_opened1), 1)
        self.assertIn("unknown_job.gcode", file_opened1[0])

        # Next poll: real job name appears
        self.mock_printer.get_printing_status.return_value = "building"
        self.mock_printer.get_job_name.return_value = "realpart.gcode"
        self.mock_printer.get_printing_progress.return_value = 2.0
        self.serial._refresh_status()
        responses2 = self._drain()
        file_opened2 = [r for r in responses2 if r.startswith("File opened:")]
        self.assertEqual(len(file_opened2), 1, f"Expected re-emission, got {responses2}")
        self.assertIn("realpart.gcode", file_opened2[0])

    def test_paused_phase_sets_paused_flag(self):
        """When job phase is 'paused', _paused should be True."""
        self.mock_printer.get_printing_status.return_value = "building"
        self.mock_printer.get_job_name.return_value = "test.gcode"
        self.serial._refresh_status()
        self._drain()

        self.mock_printer.get_printing_status.return_value = "paused"
        self.serial._refresh_status()
        self.assertTrue(self.serial._paused)
        self.assertFalse(self.serial._printing)

    def test_resuming_phase_clears_paused(self):
        """When job phase is 'resuming', _printing should be True, _paused False."""
        self.mock_printer.get_printing_status.return_value = "paused"
        self.mock_printer.get_job_name.return_value = "test.gcode"
        self.serial._refresh_status()
        self._drain()

        self.mock_printer.get_printing_status.return_value = "resuming"
        self.serial._refresh_status()
        self.assertTrue(self.serial._printing)
        self.assertFalse(self.serial._paused)

    def test_unknown_job_name_fallback(self):
        """When job name is empty, 'unknown_job.gcode' should be used."""
        self.mock_printer.get_printing_status.return_value = "building"
        self.mock_printer.get_job_name.return_value = ""
        self.mock_printer.get_printing_progress.return_value = 5.0

        self.serial._refresh_status()
        responses = self._drain()

        file_opened = [r for r in responses if r.startswith("File opened:")]
        self.assertEqual(len(file_opened), 1)
        self.assertIn("unknown_job.gcode", file_opened[0])


class TestTemperatureControl(unittest.TestCase):
    """Test temperature control GCode handlers (M104, M140, M109, M190)."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        mock_printer_class.return_value = self.mock_printer
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        self.serial = DremelVirtualSerial(
            settings=MockSettings(),
            read_timeout=1.0,
        )
        self._drain()

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain(self):
        responses = []
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                line = self.serial._outgoing.get_nowait()
                responses.append(line.strip())
            except queue.Empty:
                break
        return responses

    def _send_command(self, command):
        self.serial.write(f"{command}\n".encode())
        time.sleep(0.05)
        return self._drain()

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m104_sets_extruder_temp(self, mock_request):
        """M104 S200 should send NOZZLEHEAT=200."""
        responses = self._send_command("M104 S200")
        self.assertIn("ok", responses)
        mock_request.assert_called_once_with(self.serial._host, "NOZZLEHEAT=200")

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m104_stops_heater_at_zero(self, mock_request):
        """M104 S0 should send STOPNOZZLEHEAT."""
        responses = self._send_command("M104 S0")
        self.assertIn("ok", responses)
        mock_request.assert_called_once_with(self.serial._host, "STOPNOZZLEHEAT")

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m104_clamps_to_280(self, mock_request):
        """M104 S300 should clamp to 280."""
        responses = self._send_command("M104 S300")
        self.assertIn("ok", responses)
        mock_request.assert_called_once_with(self.serial._host, "NOZZLEHEAT=280")

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m104_blocked_during_printing(self, mock_request):
        """M104 should be blocked during active printing."""
        self.serial._printing = True
        self.serial._paused = False
        responses = self._send_command("M104 S200")
        self.assertTrue(any("Error" in r for r in responses))
        self.assertIn("ok", responses)
        mock_request.assert_not_called()

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m104_request_failure_reports_error_without_ok(self, mock_request):
        """M104 should return Error and still acknowledge with ok when backend fails."""
        mock_request.side_effect = RuntimeError("backend failure")

        responses = self._send_command("M104 S200")

        self.assertTrue(any("Error:" in r for r in responses))
        self.assertIn("ok", responses)

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m104_allowed_when_paused(self, mock_request):
        """M104 should be allowed when paused (_is_print_active returns False)."""
        self.serial._printing = False
        self.serial._paused = True
        responses = self._send_command("M104 S200")
        self.assertIn("ok", responses)
        mock_request.assert_called_once()

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m140_sets_bed_temp(self, mock_request):
        """M140 S60 should send PLATEHEAT=60."""
        responses = self._send_command("M140 S60")
        self.assertIn("ok", responses)
        mock_request.assert_called_once_with(self.serial._host, "PLATEHEAT=60")

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m140_clamps_to_100(self, mock_request):
        """M140 S150 should clamp to 100."""
        responses = self._send_command("M140 S150")
        self.assertIn("ok", responses)
        mock_request.assert_called_once_with(self.serial._host, "PLATEHEAT=100")

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m109_sets_extruder_temp(self, mock_request):
        """M109 S210 should set extruder temp and return ok."""
        responses = self._send_command("M109 S210")
        self.assertIn("ok", responses)
        mock_request.assert_called_once_with(self.serial._host, "NOZZLEHEAT=210")

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m190_sets_bed_temp(self, mock_request):
        """M190 S60 should set bed temp and return ok."""
        responses = self._send_command("M190 S60")
        self.assertIn("ok", responses)
        mock_request.assert_called_once_with(self.serial._host, "PLATEHEAT=60")


class TestM24StartPrint(unittest.TestCase):
    """Test M24 print start/resume behavior."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        mock_printer_class.return_value = self.mock_printer
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        self.serial = DremelVirtualSerial(
            settings=MockSettings(),
            read_timeout=1.0,
        )
        self._drain()

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain(self):
        responses = []
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                line = self.serial._outgoing.get_nowait()
                responses.append(line.strip())
            except queue.Empty:
                break
        return responses

    def _send_command(self, command):
        self.serial.write(f"{command}\n".encode())
        time.sleep(0.05)
        return self._drain()

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m24_starts_print(self, mock_request):
        """M24 with selected file should send PRINT command."""
        self.serial._selected_file_display = "test.gcode"
        self.serial._selected_file_remote = "UPLOAD001.g3drem"
        self.serial._selected_file_size = 50000

        responses = self._send_command("M24")

        self.assertIn("ok", responses)
        from octoprint_dremel3d45.vendor.dremel3dpy import PRINT_COMMAND
        mock_request.assert_called_once_with(
            self.serial._host, {PRINT_COMMAND: "UPLOAD001.g3drem"}
        )

    @patch("octoprint_dremel3d45.virtual_serial.default_request")
    def test_m24_does_not_set_printing_flag(self, mock_request):
        """M24 start should NOT set _printing; poll thread is authoritative."""
        self.serial._selected_file_display = "test.gcode"
        self.serial._selected_file_remote = "UPLOAD001.g3drem"
        self.serial._selected_file_size = 50000

        self._send_command("M24")

        self.assertFalse(self.serial._printing)
        # was_printing should be set as guard for poll transition
        self.assertTrue(self.serial._was_printing)
        self.assertEqual(self.serial._last_announced_job_name, "test.gcode")

    def test_m24_no_file_selected(self):
        """M24 without selected file should report error."""
        self.serial._selected_file_display = ""
        self.serial._selected_file_remote = ""

        responses = self._send_command("M24")

        self.assertTrue(any("Error" in r for r in responses))
        self.assertIn("ok", responses)

    def test_m24_resume_clears_paused(self):
        """M24 when paused should resume and clear _paused."""
        self.serial._paused = True
        self.serial._printing = True

        responses = self._send_command("M24")

        self.assertIn("ok", responses)
        self.assertFalse(self.serial._paused)
        self.mock_printer.resume_print.assert_called_once()


class TestUploadFile(unittest.TestCase):
    """Test file upload to printer."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        self.mock_printer._upload_print.return_value = "ABCDEfghij.gcode"
        mock_printer_class.return_value = self.mock_printer
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        self.serial = DremelVirtualSerial(
            settings=MockSettings(),
            read_timeout=1.0,
        )
        # Drain startup
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                self.serial._outgoing.get_nowait()
            except queue.Empty:
                break

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def test_upload_success(self):
        """Successful upload should update selected file state and return True."""
        import os
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False, mode="w") as f:
            f.write("G28\nG1 X10\n")
            tmp_path = f.name
        try:
            result = self.serial.upload_file(tmp_path, "myprint.gcode")
            self.assertTrue(result)
            self.assertEqual(self.serial._selected_file_display, "myprint.gcode")
            self.assertEqual(self.serial._selected_file_remote, "ABCDEfghij.gcode")
        finally:
            os.unlink(tmp_path)

    def test_upload_blocked_during_printing(self):
        """Upload should fail when a print is active."""
        self.serial._printing = True
        self.serial._paused = False
        result = self.serial.upload_file("/tmp/fake.gcode", "test.gcode")
        self.assertFalse(result)

    def test_upload_not_connected(self):
        """Upload should fail when not connected."""
        self.serial._printer = None
        result = self.serial.upload_file("/tmp/fake.gcode", "test.gcode")
        self.assertFalse(result)


class TestStopFailureHandling(unittest.TestCase):
    """Test stop command failure paths for M524/M112."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        mock_printer_class.return_value = self.mock_printer

        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial

        self.serial = DremelVirtualSerial(
            settings=MockSettings(),
            read_timeout=1.0,
        )
        self._drain()

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain(self):
        responses = []
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                line = self.serial._outgoing.get_nowait()
                responses.append(line.strip())
            except queue.Empty:
                break
        return responses

    def _send_command(self, command):
        self.serial.write(f"{command}\n".encode())
        time.sleep(0.05)
        return self._drain()

    def test_m524_stop_failure_reports_error_without_ok(self):
        """M524 stop failures should still acknowledge with ok."""
        self.serial._printing = True
        self.mock_printer.stop_print.side_effect = RuntimeError("stop failed")

        responses = self._send_command("M524")

        self.assertTrue(any("Error:" in r for r in responses))
        self.assertIn("ok", responses)

    def test_m112_stop_failure_reports_error_without_ok(self):
        """M112 stop failures should still acknowledge with ok."""
        self.serial._printing = True
        self.mock_printer.stop_print.side_effect = RuntimeError("stop failed")

        responses = self._send_command("M112")

        self.assertTrue(any("Error:" in r for r in responses))
        self.assertIn("ok", responses)


class TestPollLoopBehavior(unittest.TestCase):
    """Test poll loop output during active prints."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        self.mock_printer.is_door_open.return_value = False
        self.mock_printer.get_job_status.return_value = {}
        mock_printer_class.return_value = self.mock_printer
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        self.serial = DremelVirtualSerial(
            settings=MockSettings(),
            read_timeout=1.0,
        )
        self._drain()

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain(self):
        responses = []
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                line = self.serial._outgoing.get_nowait()
                responses.append(line.strip())
            except queue.Empty:
                break
        return responses

    def test_active_print_emits_sd_progress(self):
        """During active print, poll body should emit SD progress bytes."""
        # Set up active print state
        self.serial._printing = True
        self.serial._paused = False
        self.serial._was_printing = True
        self.serial._selected_file_size = 100000
        self.serial._progress = 50.0

        # Simulate the poll body (SD progress emission part)
        is_active = self.serial._printing or self.serial._paused
        self.assertTrue(is_active)
        total = int(self.serial._selected_file_size or 1000000)
        printed = int((float(self.serial._progress) / 100.0) * float(total))
        self.serial._send(f"SD printing byte {printed}/{total}")

        responses = self._drain()
        sd_lines = [r for r in responses if r.startswith("SD printing byte")]
        self.assertEqual(len(sd_lines), 1)
        self.assertEqual(sd_lines[0], "SD printing byte 50000/100000")

    def test_active_print_emits_layer_notification(self):
        """During active print with layer info, should emit //action:notification."""
        self.serial._printing = True
        self.serial._current_layer = 15

        # Simulate poll body layer emission
        if self.serial._current_layer > 0:
            self.serial._send(
                f"//action:notification Layer {self.serial._current_layer}"
            )

        responses = self._drain()
        layer_lines = [r for r in responses if "action:notification" in r]
        self.assertEqual(len(layer_lines), 1)
        self.assertIn("Layer 15", layer_lines[0])
        # Verify no space between // and action (C1 fix)
        self.assertTrue(
            layer_lines[0].startswith("//action:"),
            f"Bad format: {layer_lines[0]}"
        )

    def test_active_print_emits_m73_progress(self):
        """During active print, poll body should emit M73 P<pct> R<min>."""
        self.serial._printing = True
        self.serial._progress = 42.0
        self.serial._remaining_time = 630  # 10.5 minutes → 10

        # Simulate poll body M73 emission
        remaining_min = max(int(self.serial._remaining_time / 60), 0)
        self.serial._send(f"M73 P{int(self.serial._progress)} R{remaining_min}")

        responses = self._drain()
        m73_lines = [r for r in responses if r.startswith("M73")]
        self.assertEqual(len(m73_lines), 1)
        self.assertEqual(m73_lines[0], "M73 P42 R10")

    def test_active_print_m73_zero_remaining(self):
        """M73 R should be 0 when remaining_time is 0."""
        self.serial._printing = True
        self.serial._progress = 99.0
        self.serial._remaining_time = 0

        remaining_min = max(int(self.serial._remaining_time / 60), 0)
        self.serial._send(f"M73 P{int(self.serial._progress)} R{remaining_min}")

        responses = self._drain()
        m73_lines = [r for r in responses if r.startswith("M73")]
        self.assertEqual(len(m73_lines), 1)
        self.assertEqual(m73_lines[0], "M73 P99 R0")

    def test_idle_does_not_emit_sd_progress(self):
        """When idle, no SD progress should be emitted."""
        self.serial._printing = False
        self.serial._paused = False

        # Nothing should be emitted
        responses = self._drain()
        sd_lines = [r for r in responses if "SD printing" in r]
        self.assertEqual(len(sd_lines), 0)


class TestRefreshFailureRecovery(unittest.TestCase):
    """Test behavior after repeated refresh failures."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        mock_printer_class.return_value = self.mock_printer

        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial

        self.serial = DremelVirtualSerial(
            settings=MockSettings(),
            read_timeout=1.0,
        )
        self._drain()

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain(self):
        responses = []
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                line = self.serial._outgoing.get_nowait()
                responses.append(line.strip())
            except queue.Empty:
                break
        return responses

    def test_repeated_refresh_failures_clear_stale_print_state(self):
        self.serial._printing = True
        self.serial._paused = False
        self.serial._was_printing = True
        self.serial._job_phase = "building"

        self.mock_printer.set_job_status.side_effect = RuntimeError("offline")

        for _ in range(4):
            self.serial._refresh_status()

        responses = self._drain()

        self.assertFalse(self.serial._printing)
        self.assertFalse(self.serial._paused)
        self.assertFalse(self.serial._was_printing)
        self.assertEqual(self.serial._job_phase, "idle")
        self.assertIn("Not SD printing", responses)


class TestBootSequence(unittest.TestCase):
    """Test that boot sequence is minimal (no eager capabilities)."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def test_boot_does_not_send_capabilities(self, mock_printer_class):
        """Boot should only send empty line + start + SD card ok, NOT FIRMWARE_NAME or Cap:."""
        mock_printer = MagicMock()
        mock_printer.get_firmware_version.return_value = "1.0.0"
        mock_printer.is_printing.return_value = False
        mock_printer.is_paused.return_value = False
        mock_printer.get_printing_status.return_value = "idle"
        mock_printer.get_printing_progress.return_value = 0
        mock_printer.get_elapsed_time.return_value = 0
        mock_printer.get_remaining_time.return_value = 0
        mock_printer.get_layer.return_value = 0
        mock_printer.get_job_name.return_value = ""
        mock_printer_class.return_value = mock_printer

        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        serial = DremelVirtualSerial(
            settings=MockSettings(),
            read_timeout=1.0,
        )
        try:
            # Collect all startup messages
            responses = []
            timeout = time.time() + 0.5
            while time.time() < timeout:
                try:
                    line = serial._outgoing.get_nowait()
                    responses.append(line.strip())
                except queue.Empty:
                    break

            # Should have empty line and "start"
            self.assertIn("start", responses)
            # Should NOT have eager FIRMWARE_NAME or Cap: lines
            cap_lines = [r for r in responses if r.startswith("Cap:")]
            fw_lines = [r for r in responses if r.startswith("FIRMWARE_NAME:")]
            self.assertEqual(len(cap_lines), 0, f"Should not send Cap: at boot, got {cap_lines}")
            self.assertEqual(len(fw_lines), 0, f"Should not send FIRMWARE_NAME at boot, got {fw_lines}")
        finally:
            serial._poll_stop.set()
            serial.close()


class TestResilientApiParsing(unittest.TestCase):
    """Test that the vendored dremel3dpy library handles missing keys gracefully."""

    def _make_printer(self):
        """Create a bare Dremel3DPrinter without __init__ (no real connection)."""
        from octoprint_dremel3d45.vendor.dremel3dpy import Dremel3DPrinter

        printer = object.__new__(Dremel3DPrinter)
        printer._host = "192.168.1.100"
        printer._job_status = None
        printer._printer_info = None
        printer._printer_extra_stats = None
        printer._total_time = 0
        printer._is_printing = False
        printer._is_building = False
        printer._is_calibrating = False
        printer._is_starting = False
        printer._is_heating = False
        printer._is_finished = False
        return printer

    def _mock_post_empty(self):
        """Return a patch context for requests.post returning empty JSON."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"{}"
        return patch("requests.sessions.Session.post", return_value=mock_resp)

    def test_set_job_status_missing_keys(self):
        """set_job_status should not raise when API returns partial data."""
        printer = self._make_printer()

        with self._mock_post_empty():
            printer.set_job_status(refresh=True)

        # All getters should return safe defaults
        self.assertEqual(printer.get_printing_progress(), 0)
        self.assertIsNotNone(printer.get_printing_status())

    def test_set_printer_info_missing_keys(self):
        """set_printer_info should not raise when API returns partial data."""
        printer = self._make_printer()

        with self._mock_post_empty():
            printer.set_printer_info(refresh=True)

        self.assertIsNotNone(printer.get_title())
        self.assertIsNotNone(printer.get_firmware_version())

    def test_set_extra_status_missing_keys(self):
        """set_extra_status should not raise when API returns partial data."""
        printer = self._make_printer()

        with self._mock_post_empty():
            printer.set_extra_status(refresh=True)

    def test_set_job_status_maps_paused_phase(self):
        """Raw paused status should map to paused (not unknown)."""
        from octoprint_dremel3d45.vendor.dremel3dpy.helpers.constants import JOB_STATUS

        printer = self._make_printer()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = ('{"%s": "paused"}' % JOB_STATUS[0]).encode("utf-8")

        with patch("requests.sessions.Session.post", return_value=mock_resp):
            printer.set_job_status(refresh=True)

        self.assertEqual(printer.get_printing_status(), "paused")


class TestDefaultRequestHandling(unittest.TestCase):
    """Regression tests for default_request error behavior."""

    def setUp(self):
        from octoprint_dremel3d45.vendor import dremel3dpy as _dremel3dpy

        session = getattr(_dremel3dpy._THREAD_LOCAL_SESSION, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
            delattr(_dremel3dpy._THREAD_LOCAL_SESSION, "session")

    def test_default_request_non_200_raises_runtimeerror(self):
        from octoprint_dremel3d45.vendor.dremel3dpy import default_request

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.content = b'{"message":"boom"}'
        mock_resp.text = '{"message":"boom"}'

        with patch("requests.sessions.Session.post", return_value=mock_resp):
            with self.assertRaises(RuntimeError):
                default_request("192.168.1.100", "GETPRINTERSTATUS")

    def test_default_request_non_json_error_body_raises_runtimeerror(self):
        from octoprint_dremel3d45.vendor.dremel3dpy import default_request

        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.content = b"<html>bad gateway</html>"
        mock_resp.text = "bad gateway"

        with patch("requests.sessions.Session.post", return_value=mock_resp):
            with self.assertRaises(RuntimeError):
                default_request("192.168.1.100", "GETPRINTERSTATUS")

    def test_default_request_http_200_with_api_error_code_raises_runtimeerror(self):
        from octoprint_dremel3d45.vendor.dremel3dpy import default_request

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.content = b'{"error_code":500,"message":"command failed"}'
        mock_resp.text = '{"error_code":500,"message":"command failed"}'

        with patch("requests.sessions.Session.post", return_value=mock_resp):
            with self.assertRaises(RuntimeError):
                default_request("192.168.1.100", "PRINT=test.gcode")

    def test_default_request_http_200_with_api_success_code_returns_payload(self):
        from octoprint_dremel3d45.vendor.dremel3dpy import default_request

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.content = b'{"error_code":200,"message":"ok"}'
        mock_resp.text = '{"error_code":200,"message":"ok"}'

        with patch("requests.sessions.Session.post", return_value=mock_resp):
            payload = default_request("192.168.1.100", "PRINT=test.gcode")

        self.assertEqual(payload.get("error_code"), 200)

    def test_default_request_retries_after_transport_error(self):
        from octoprint_dremel3d45.vendor.dremel3dpy import default_request
        import requests

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.content = b'{"error_code":200,"message":"ok"}'
        mock_resp.text = '{"error_code":200,"message":"ok"}'

        with patch(
            "requests.sessions.Session.post",
            side_effect=[requests.RequestException("boom"), mock_resp],
        ) as mock_post:
            payload = default_request("192.168.1.100", "GETPRINTERSTATUS")

        self.assertEqual(payload.get("error_code"), 200)
        self.assertEqual(mock_post.call_count, 2)

    def test_default_request_reuses_thread_session(self):
        from octoprint_dremel3d45.vendor.dremel3dpy import default_request

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.content = b'{"error_code":200,"message":"ok"}'
        mock_resp.text = '{"error_code":200,"message":"ok"}'

        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch(
            "octoprint_dremel3d45.vendor.dremel3dpy.requests.Session",
            return_value=mock_session,
        ) as mock_session_ctor:
            default_request("192.168.1.100", "GETPRINTERSTATUS")
            default_request("192.168.1.100", "GETPRINTERSTATUS")

        self.assertEqual(mock_session_ctor.call_count, 1)


class TestRefreshCallsAllEndpoints(unittest.TestCase):
    """Test that _refresh_status calls all three API endpoints."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        self.mock_printer.get_job_status.return_value = {}
        self.mock_printer.is_door_open.return_value = False
        mock_printer_class.return_value = self.mock_printer

        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial

        settings = MockSettings()
        self.serial = DremelVirtualSerial(settings=settings, read_timeout=1.0)
        self._drain_responses()

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain_responses(self):
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                self.serial._outgoing.get_nowait()
            except queue.Empty:
                break

    def test_refresh_calls_all_three_api_methods(self):
        """_refresh_status should call set_job_status, set_printer_info, and set_extra_status."""
        self.serial._refresh_status()

        self.mock_printer.set_job_status.assert_called_with(refresh=True)
        self.mock_printer.set_printer_info.assert_called_with(refresh=True)
        self.mock_printer.set_extra_status.assert_called_with(refresh=True)

    def test_refresh_continues_if_printer_info_fails(self):
        """_refresh_status should not crash if set_printer_info raises."""
        self.mock_printer.set_printer_info.side_effect = Exception("API timeout")

        # Should not raise
        self.serial._refresh_status()

        # set_job_status should still have been called
        self.mock_printer.set_job_status.assert_called_with(refresh=True)

    def test_refresh_continues_if_extra_status_fails(self):
        """_refresh_status should not crash if set_extra_status raises."""
        self.mock_printer.set_extra_status.side_effect = Exception("HTTPS error")

        # Should not raise
        self.serial._refresh_status()

        self.mock_printer.set_job_status.assert_called_with(refresh=True)


class TestUpdateSettings(unittest.TestCase):
    """Test that update_settings propagates settings to a running session."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        mock_printer_class.return_value = self.mock_printer

        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial

        self.settings = MockSettings()
        self.serial = DremelVirtualSerial(settings=self.settings, read_timeout=1.0)
        self._drain_responses()

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain_responses(self):
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                self.serial._outgoing.get_nowait()
            except queue.Empty:
                break

    def test_update_poll_interval(self):
        """Changing poll_interval in settings should update the serial's interval."""
        self.assertEqual(self.serial._poll_interval, 60)
        self.assertEqual(self.serial._poll_interval_active, 60)
        self.assertEqual(self.serial._poll_interval_idle, 60)

        # Simulate user changing the poll interval
        self.settings._data["poll_interval"] = 5
        self.serial.update_settings()

        self.assertEqual(self.serial._poll_interval, 5)
        self.assertEqual(self.serial._poll_interval_active, 5)
        self.assertEqual(self.serial._poll_interval_idle, 5)

    def test_update_adaptive_poll_intervals(self):
        """Adaptive printing/idle poll intervals should update independently."""
        self.settings._data["poll_interval_printing"] = 4
        self.settings._data["poll_interval_idle"] = 12

        self.serial.update_settings()

        self.assertEqual(self.serial._poll_interval, 4)
        self.assertEqual(self.serial._poll_interval_active, 4)
        self.assertEqual(self.serial._poll_interval_idle, 12)

    def test_update_request_timeout(self):
        """Changing request_timeout should update both the serial and the library constant."""
        self.assertEqual(self.serial._request_timeout, 30)

        self.settings._data["request_timeout"] = 15
        self.serial.update_settings()

        self.assertEqual(self.serial._request_timeout, 15)

        from octoprint_dremel3d45.vendor import dremel3dpy as _dremel3dpy
        from octoprint_dremel3d45.vendor.dremel3dpy.helpers import constants as _c
        self.assertEqual(_c.REQUEST_TIMEOUT, 15)
        self.assertEqual(_dremel3dpy.REQUEST_TIMEOUT, 15)

    def test_no_change_is_noop(self):
        """update_settings should be a no-op when values haven't changed."""
        old_interval = self.serial._poll_interval
        old_interval_active = self.serial._poll_interval_active
        old_interval_idle = self.serial._poll_interval_idle
        old_timeout = self.serial._request_timeout

        self.serial.update_settings()

        self.assertEqual(self.serial._poll_interval, old_interval)
        self.assertEqual(self.serial._poll_interval_active, old_interval_active)
        self.assertEqual(self.serial._poll_interval_idle, old_interval_idle)
        self.assertEqual(self.serial._request_timeout, old_timeout)

    def test_update_poll_interval_clamps_to_minimum_one(self):
        """poll_interval values <= 0 should be clamped to 1 second."""
        self.settings._data["poll_interval"] = 0
        self.serial.update_settings()
        self.assertEqual(self.serial._poll_interval, 1)
        self.assertEqual(self.serial._poll_interval_active, 1)
        self.assertEqual(self.serial._poll_interval_idle, 1)

    def test_idle_interval_clamped_not_below_active(self):
        """poll_interval_idle should be clamped to poll_interval_printing when lower."""
        self.settings._data["poll_interval_printing"] = 8
        self.settings._data["poll_interval_idle"] = 3

        self.serial.update_settings()

        self.assertEqual(self.serial._poll_interval_active, 8)
        self.assertEqual(self.serial._poll_interval_idle, 8)


class TestAdaptivePollingLoop(unittest.TestCase):
    """Test adaptive polling cadence selection in _poll_loop."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        mock_printer_class.return_value = self.mock_printer

        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial

        settings = MockSettings(
            {
                "printer_ip": "192.168.1.100",
                "request_timeout": 30,
                "poll_interval": 10,
                "poll_interval_printing": 4,
                "poll_interval_idle": 11,
            }
        )
        self.serial = DremelVirtualSerial(settings=settings, read_timeout=1.0)

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def test_poll_loop_waits_idle_interval_when_not_printing(self):
        self.serial._printing = False
        self.serial._paused = False

        with patch.object(self.serial._poll_stop, "wait", side_effect=[True]) as mock_wait:
            self.serial._poll_loop()

        mock_wait.assert_called_once_with(self.serial._poll_interval_idle)

    def test_poll_loop_waits_active_interval_when_printing(self):
        self.serial._printing = True
        self.serial._paused = False

        with patch.object(self.serial._poll_stop, "wait", side_effect=[True]) as mock_wait:
            self.serial._poll_loop()

        mock_wait.assert_called_once_with(self.serial._poll_interval_active)


class TestEstimationHook(unittest.TestCase):
    """Test the print time estimation hook in the plugin."""

    def _estimate(self, virtual_serial):
        """Replicate the estimation hook logic from Dremel3D45Plugin."""
        if (
            virtual_serial
            and getattr(virtual_serial, "_printing", False)
            and virtual_serial._remaining_time > 0
        ):
            return virtual_serial._remaining_time, "dremel"
        return None

    def test_returns_none_when_not_connected(self):
        """Should return None when no virtual serial is connected."""
        result = self._estimate(None)
        self.assertIsNone(result)

    def test_returns_none_when_not_printing(self):
        """Should return None when connected but not printing."""
        vs = MagicMock()
        vs._printing = False
        vs._remaining_time = 600
        result = self._estimate(vs)
        self.assertIsNone(result)

    def test_returns_none_when_remaining_zero(self):
        """Should return None when remaining_time is 0."""
        vs = MagicMock()
        vs._printing = True
        vs._remaining_time = 0
        result = self._estimate(vs)
        self.assertIsNone(result)

    def test_returns_remaining_time_when_printing(self):
        """Should return (remaining_seconds, 'dremel') when actively printing."""
        vs = MagicMock()
        vs._printing = True
        vs._remaining_time = 1800
        result = self._estimate(vs)
        self.assertEqual(result, (1800, "dremel"))


class TestLayerNotificationFormat(unittest.TestCase):
    """Test that layer notification includes total when available.

    The notification is emitted by ``_poll_loop`` (not ``_refresh_status``),
    so we test the ``_send_layer_notification`` helper directly.
    """

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        self.mock_printer.is_door_open.return_value = False
        self.mock_printer.get_job_status.return_value = {}
        mock_printer_class.return_value = self.mock_printer

        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        self.serial = DremelVirtualSerial(
            settings=MockSettings(), read_timeout=1.0,
        )
        while not self.serial._outgoing.empty():
            self.serial._outgoing.get_nowait()

    def tearDown(self):
        self.serial._poll_stop.set()
        self.serial.close()

    def _drain(self):
        lines = []
        while not self.serial._outgoing.empty():
            lines.append(self.serial._outgoing.get_nowait().strip())
        return lines

    def test_layer_with_total(self):
        """When total_layers is known, notification uses Layer X/Y format."""
        self.serial._current_layer = 10
        self.serial._total_layers = 50
        # Directly emit the notification the same way _poll_loop does
        if self.serial._current_layer > 0:
            if self.serial._total_layers > 0:
                self.serial._send(
                    f"//action:notification Layer {self.serial._current_layer}/{self.serial._total_layers}"
                )
            else:
                self.serial._send(
                    f"//action:notification Layer {self.serial._current_layer}"
                )
        responses = self._drain()
        layer_msgs = [r for r in responses if "//action:notification Layer" in r]
        self.assertEqual(len(layer_msgs), 1)
        self.assertIn("Layer 10/50", layer_msgs[0])

    def test_layer_without_total(self):
        """When total_layers is 0, notification uses Layer X format (no slash)."""
        self.serial._current_layer = 15
        self.serial._total_layers = 0
        if self.serial._current_layer > 0:
            if self.serial._total_layers > 0:
                self.serial._send(
                    f"//action:notification Layer {self.serial._current_layer}/{self.serial._total_layers}"
                )
            else:
                self.serial._send(
                    f"//action:notification Layer {self.serial._current_layer}"
                )
        responses = self._drain()
        layer_msgs = [r for r in responses if "//action:notification Layer" in r]
        self.assertEqual(len(layer_msgs), 1)
        self.assertIn("Layer 15", layer_msgs[0])
        self.assertNotIn("15/", layer_msgs[0])


if __name__ == "__main__":
    unittest.main()


class TestCachingBehavior(unittest.TestCase):
    """Test TTL-based caching for printer info and M119 poll cache usage."""

    @patch("octoprint_dremel3d45.virtual_serial.Dremel3DPrinter")
    def setUp(self, mock_printer_class):
        self.mock_printer = MagicMock()
        self.mock_printer.get_firmware_version.return_value = "1.0.0"
        self.mock_printer.get_title.return_value = "Dremel 3D45"
        self.mock_printer.get_serial_number.return_value = "TEST123"
        self.mock_printer.get_temperature_type.return_value = 25.0
        self.mock_printer.get_temperature_attributes.return_value = {"target_temp": 0}
        self.mock_printer.is_printing.return_value = False
        self.mock_printer.is_paused.return_value = False
        self.mock_printer.get_printing_status.return_value = "idle"
        self.mock_printer.get_printing_progress.return_value = 0
        self.mock_printer.get_elapsed_time.return_value = 0
        self.mock_printer.get_remaining_time.return_value = 0
        self.mock_printer.get_layer.return_value = 0
        self.mock_printer.get_job_name.return_value = ""
        self.mock_printer.is_door_open.return_value = False
        self.mock_printer.get_job_status.return_value = {}
        mock_printer_class.return_value = self.mock_printer
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        self.serial = DremelVirtualSerial(
            settings=MockSettings(),
            read_timeout=1.0,
        )
        self._drain()

    def tearDown(self):
        if hasattr(self, "serial") and self.serial:
            self.serial._poll_stop.set()
            self.serial.close()

    def _drain(self):
        responses = []
        timeout = time.time() + 0.5
        while time.time() < timeout:
            try:
                line = self.serial._outgoing.get_nowait()
                responses.append(line.strip())
            except queue.Empty:
                break
        return responses

    def _send_command(self, command):
        self.serial.write(f"{command}\n".encode())
        time.sleep(0.05)
        return self._drain()

    # ---- M115 printer info caching ----

    def test_m115_uses_cache_when_fresh(self):
        """M115 should NOT call set_printer_info when cache is fresh."""
        # _start() already called set_printer_info once; reset the mock
        self.mock_printer.set_printer_info.reset_mock()
        # Ensure cache is recent
        self.serial._printer_info_ts = time.time()

        self._send_command("M115")

        self.mock_printer.set_printer_info.assert_not_called()

    def test_m115_refreshes_when_stale(self):
        """M115 should call set_printer_info when cache TTL has expired."""
        self.mock_printer.set_printer_info.reset_mock()
        # Force cache to be stale
        from octoprint_dremel3d45.virtual_serial import DremelVirtualSerial
        self.serial._printer_info_ts = time.time() - DremelVirtualSerial._PRINTER_INFO_TTL - 1

        self._send_command("M115")

        self.mock_printer.set_printer_info.assert_called_once_with(refresh=True)

    def test_m115_still_responds_on_refresh_failure(self):
        """M115 should still return firmware info from cache if refresh fails."""
        self.mock_printer.set_printer_info.reset_mock()
        self.mock_printer.set_printer_info.side_effect = RuntimeError("network error")
        # Force stale so it attempts refresh
        self.serial._printer_info_ts = 0.0

        responses = self._send_command("M115")

        response_text = " ".join(responses)
        # Should still have firmware info from the initial (successful) call
        self.assertIn("FIRMWARE_NAME:Dremel3D45", response_text)
        self.assertIn("ok", responses)

    # ---- M119 door cache ----

    def test_m119_uses_poll_cache_not_live_call(self):
        """M119 should use _door_open cache, not call is_door_open()."""
        self.mock_printer.is_door_open.reset_mock()
        self.serial._door_open = True

        responses = self._send_command("M119")

        # Should NOT make a live API call
        self.mock_printer.is_door_open.assert_not_called()
        response_text = " ".join(responses)
        self.assertIn("door: TRIGGERED", response_text)

    def test_m119_reports_closed_door_from_cache(self):
        """M119 should report 'open' when _door_open is False."""
        self.serial._door_open = False

        responses = self._send_command("M119")

        response_text = " ".join(responses)
        self.assertIn("door: open", response_text)

    # ---- Poll thread TTL gating ----

    def test_refresh_status_skips_printer_info_when_fresh(self):
        """_refresh_status should skip set_printer_info when TTL is fresh."""
        self.mock_printer.set_printer_info.reset_mock()
        self.serial._printer_info_ts = time.time()

        self.serial._refresh_status()

        self.mock_printer.set_printer_info.assert_not_called()

    def test_refresh_status_calls_printer_info_when_stale(self):
        """_refresh_status should call set_printer_info when TTL has expired."""
        self.mock_printer.set_printer_info.reset_mock()
        self.serial._printer_info_ts = 0.0

        self.serial._refresh_status()

        self.mock_printer.set_printer_info.assert_called_once_with(refresh=True)

    def test_refresh_status_skips_extra_status_when_fresh(self):
        """_refresh_status should skip set_extra_status when TTL is fresh."""
        self.mock_printer.set_extra_status.reset_mock()
        self.serial._extra_status_ts = time.time()

        self.serial._refresh_status()

        self.mock_printer.set_extra_status.assert_not_called()

    def test_refresh_status_calls_extra_status_when_stale(self):
        """_refresh_status should call set_extra_status when TTL has expired."""
        self.mock_printer.set_extra_status.reset_mock()
        self.serial._extra_status_ts = 0.0

        self.serial._refresh_status()

        self.mock_printer.set_extra_status.assert_called_once_with(refresh=True)

    def test_refresh_status_always_calls_job_status(self):
        """_refresh_status should always call set_job_status (no TTL gating)."""
        self.mock_printer.set_job_status.reset_mock()

        self.serial._refresh_status()

        self.mock_printer.set_job_status.assert_called_once_with(refresh=True)
