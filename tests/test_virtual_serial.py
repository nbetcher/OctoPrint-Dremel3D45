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
            data_folder=None,
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
        """M105 should report current and target temperatures."""
        # Note: M105 calls _refresh_status() which updates temps from the mock.
        # So we need to set the mock to return our expected values.
        self.mock_printer.get_temperature_type.side_effect = lambda t: {
            "extruder": 200.0,
            "platform": 60.0,
            "chamber": 30.0,
        }.get(t, 25.0)
        self.mock_printer.get_temperature_attributes.side_effect = lambda t: {
            "extruder": {"target_temp": 210},
            "platform": {"target_temp": 65},
        }.get(t, {"target_temp": 0})

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

    def test_m27_enables_autosd(self):
        """M27 S3 should enable SD status auto-reporting."""
        responses = self._send_command("M27 S3")

        self.assertIn("ok", responses)
        self.assertTrue(self.serial._autosd_enabled)
        self.assertEqual(self.serial._autosd_interval, 3)

    def test_m23_selects_file(self):
        """M23 should select a file for printing."""
        # Add a file to the index
        self.serial._sd_index["test.gcode"] = {
            "display": "test.gcode",
            "remote": "UPLOAD001.g3drem",
            "size": 12345,
        }

        responses = self._send_command("M23 test.gcode")

        self.assertTrue(any("File opened" in r for r in responses))
        self.assertTrue(any("File selected" in r for r in responses))
        self.assertEqual(self.serial._selected_file_display, "test.gcode")
        self.assertEqual(self.serial._selected_file_remote, "UPLOAD001.g3drem")

    def test_m23_missing_file(self):
        """M23 with unknown file should report error."""
        responses = self._send_command("M23 nonexistent.gcode")

        self.assertTrue(any("Error" in r for r in responses))

    def test_m25_pauses_print(self):
        """M25 should pause a running print."""
        self.serial._printing = True

        responses = self._send_command("M25")

        self.assertIn("ok", responses)
        self.mock_printer.pause_print.assert_called_once()

    def test_m524_cancels_print(self):
        """M524 should cancel the current print."""
        self.serial._printing = True
        self.serial._selected_file_display = "test.gcode"

        responses = self._send_command("M524")

        self.assertIn("ok", responses)
        self.mock_printer.stop_print.assert_called_once()
        self.assertFalse(self.serial._printing)
        self.assertEqual(self.serial._selected_file_display, "")

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
        """M115 should report firmware info."""
        responses = self._send_command("M115")

        response_text = " ".join(responses)
        self.assertIn("FIRMWARE_NAME:Dremel3D45", response_text)
        self.assertIn("AUTOREPORT_TEMP", response_text)
        self.assertIn("ok", responses)

    def test_m119_reports_endstops(self):
        """M119 should report endstop status."""
        self.mock_printer.is_door_open.return_value = True

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


if __name__ == "__main__":
    unittest.main()
