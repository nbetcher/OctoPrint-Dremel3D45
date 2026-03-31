# -*- coding: utf-8 -*-
"""
Unit tests for the local print redirect mechanism.

Tests the gcode_queuing_hook and _do_redirect_local_print flow that
intercepts local file prints and redirects them to the Dremel's native
upload-and-print REST workflow.
"""

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock


# OctoPrint is not available in the test environment.  We need a lightweight
# stub so the plugin module can be imported.
import sys

_octoprint_stub = MagicMock()

# Each mixin base class must be a distinct real class so the plugin
# class can inherit from all of them without "duplicate base class" errors.
_octoprint_stub.plugin.StartupPlugin = type("StartupPlugin", (), {})
_octoprint_stub.plugin.ShutdownPlugin = type("ShutdownPlugin", (), {})
_octoprint_stub.plugin.EventHandlerPlugin = type("EventHandlerPlugin", (), {})
_octoprint_stub.plugin.SettingsPlugin = type("SettingsPlugin", (), {"on_settings_save": lambda self, data: {}})
_octoprint_stub.plugin.SimpleApiPlugin = type("SimpleApiPlugin", (), {})
_octoprint_stub.plugin.TemplatePlugin = type("TemplatePlugin", (), {})
_octoprint_stub.plugin.AssetPlugin = type("AssetPlugin", (), {})

sys.modules.setdefault("octoprint", _octoprint_stub)
sys.modules.setdefault("octoprint.plugin", _octoprint_stub.plugin)
sys.modules.setdefault("octoprint.settings", MagicMock())

# Flask is not installed in the test environment; provide minimal stub.
_flask_stub = MagicMock()
_flask_stub.jsonify = lambda **kwargs: kwargs
sys.modules.setdefault("flask", _flask_stub)

# Now we can import the plugin
from octoprint_dremel3d45 import Dremel3D45Plugin  # noqa: E402


class TestGcodeQueuingHook(unittest.TestCase):
    """Test the gcode_queuing_hook interception logic."""

    def setUp(self):
        self.plugin = Dremel3D45Plugin()
        self.plugin._virtual_serial = MagicMock()
        self.plugin._virtual_serial._closed = False
        self.plugin._plugin_manager = MagicMock()
        self.plugin._identifier = "dremel3d45"

    def tearDown(self):
        # Ensure the redirect flag is cleared so the sleep(5) thread
        # doesn't leak between tests.
        self.plugin._local_print_redirecting = False

    # -- Passthrough cases --------------------------------------------------

    def test_passthrough_when_no_virtual_serial(self):
        """Commands should pass through when not connected to the Dremel."""
        self.plugin._virtual_serial = None
        result = self.plugin.gcode_queuing_hook(
            MagicMock(), "queuing", "G28", None, "G28", tags={"source:file"},
        )
        self.assertIsNone(result)

    def test_passthrough_for_terminal_commands(self):
        """Terminal-sourced commands should never be suppressed."""
        result = self.plugin.gcode_queuing_hook(
            MagicMock(), "queuing", "M104 S200", None, "M104",
            tags={"source:terminal"},
        )
        self.assertIsNone(result)

    def test_passthrough_when_no_tags(self):
        """Commands with no tags should pass through."""
        result = self.plugin.gcode_queuing_hook(
            MagicMock(), "queuing", "G28", None, "G28", tags=None,
        )
        self.assertIsNone(result)

    def test_passthrough_for_empty_tags(self):
        """Commands with empty tag set should pass through."""
        result = self.plugin.gcode_queuing_hook(
            MagicMock(), "queuing", "G28", None, "G28", tags=set(),
        )
        self.assertIsNone(result)

    # -- Suppression cases --------------------------------------------------

    def test_suppresses_file_sourced_commands(self):
        """File-sourced commands should be suppressed (returned as (None,))."""
        comm = MagicMock()
        comm._currentFile = None  # No file info available

        result = self.plugin.gcode_queuing_hook(
            comm, "queuing", "G1 X10 Y20", None, "G1",
            tags={"source:file", "fileline:1"},
        )
        self.assertEqual(result, (None,))

    def test_suppresses_m104_from_file(self):
        """M104 from file should be suppressed — prevents unwanted preheat."""
        comm = MagicMock()
        comm._currentFile = None

        result = self.plugin.gcode_queuing_hook(
            comm, "queuing", "M104 S250", None, "M104",
            tags={"source:file"},
        )
        self.assertEqual(result, (None,))

    def test_suppresses_m140_from_file(self):
        """M140 from file should be suppressed — prevents unwanted bed heat."""
        comm = MagicMock()
        comm._currentFile = None

        result = self.plugin.gcode_queuing_hook(
            comm, "queuing", "M140 S60", None, "M140",
            tags={"source:file"},
        )
        self.assertEqual(result, (None,))

    def test_suppresses_subsequent_file_commands(self):
        """All subsequent file commands should also be suppressed."""
        comm = MagicMock()
        comm._currentFile = None

        # First call starts the redirect
        self.plugin.gcode_queuing_hook(
            comm, "queuing", "M110 N0", None, "M110", tags={"source:file"},
        )
        # Second call should also be suppressed
        result = self.plugin.gcode_queuing_hook(
            comm, "queuing", "G28", None, "G28", tags={"source:file"},
        )
        self.assertEqual(result, (None,))

    def test_suppresses_afterprint_scripts_during_redirect(self):
        """Script commands should be suppressed while redirect is active."""
        self.plugin._local_print_redirecting = True

        result = self.plugin.gcode_queuing_hook(
            MagicMock(), "queuing", "M104 S0", None, "M104",
            tags={"source:script:afterPrintDone"},
        )
        self.assertEqual(result, (None,))

    def test_allows_scripts_when_not_redirecting(self):
        """Script commands should pass through normally when not redirecting."""
        self.plugin._local_print_redirecting = False

        result = self.plugin.gcode_queuing_hook(
            MagicMock(), "queuing", "M104 S0", None, "M104",
            tags={"source:script:afterPrintDone"},
        )
        self.assertIsNone(result)

    # -- Redirect thread startup --------------------------------------------

    @patch("octoprint_dremel3d45.threading.Thread")
    def test_starts_redirect_thread_on_first_file_command(self, mock_thread_cls):
        """First file command should spawn the redirect background thread."""
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        comm = MagicMock()
        current_file = MagicMock()
        current_file.getFilename.return_value = "/uploads/test.gcode"
        comm._currentFile = current_file

        self.plugin.gcode_queuing_hook(
            comm, "queuing", "M110 N0", None, "M110", tags={"source:file"},
        )

        mock_thread_cls.assert_called_once()
        call_kwargs = mock_thread_cls.call_args
        self.assertEqual(call_kwargs.kwargs["target"], self.plugin._do_redirect_local_print)
        self.assertEqual(call_kwargs.kwargs["args"], ("/uploads/test.gcode", "test.gcode"))
        mock_thread.start.assert_called_once()

    @patch("octoprint_dremel3d45.threading.Thread")
    def test_does_not_start_second_redirect_thread(self, mock_thread_cls):
        """Once the redirect is active, no second thread should be spawned."""
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        comm = MagicMock()
        comm._currentFile = None

        # First call starts thread
        self.plugin.gcode_queuing_hook(
            comm, "queuing", "M110 N0", None, "M110", tags={"source:file"},
        )
        # Second call should NOT start another thread
        self.plugin.gcode_queuing_hook(
            comm, "queuing", "G28", None, "G28", tags={"source:file"},
        )

        self.assertEqual(mock_thread_cls.call_count, 1)

    @patch("octoprint_dremel3d45.threading.Thread")
    def test_handles_missing_currentFile(self, mock_thread_cls):
        """Redirect should start even if _currentFile is not available."""
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        comm = MagicMock(spec=[])  # No _currentFile attribute

        self.plugin.gcode_queuing_hook(
            comm, "queuing", "M110 N0", None, "M110", tags={"source:file"},
        )

        mock_thread_cls.assert_called_once()
        call_args = mock_thread_cls.call_args.kwargs["args"]
        self.assertIsNone(call_args[0])  # file_path
        self.assertIsNone(call_args[1])  # file_name


class TestDoRedirectLocalPrint(unittest.TestCase):
    """Test the _do_redirect_local_print background logic."""

    def setUp(self):
        self.plugin = Dremel3D45Plugin()
        self.plugin._plugin_manager = MagicMock()
        self.plugin._identifier = "dremel3d45"

        # Create a temp gcode file
        self.tmp = tempfile.NamedTemporaryFile(
            suffix=".gcode", delete=False, mode="w",
        )
        self.tmp.write("; test gcode\nG28\nG1 X10\n")
        self.tmp.close()

        # Mock virtual serial
        self.mock_vs = MagicMock()
        self.mock_vs._closed = False
        self.mock_vs._lock = threading.RLock()
        self.mock_vs._selected_file_remote = "uploaded.g3drem"
        self.mock_vs._selected_file_display = "test.gcode"
        self.mock_vs._was_printing = False
        self.mock_vs._last_announced_job_name = ""
        self.mock_vs.upload_file.return_value = True
        self.mock_vs._host = "192.168.1.100"
        self.plugin._virtual_serial = self.mock_vs

    def tearDown(self):
        self.plugin._local_print_redirecting = False
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    @patch("octoprint_dremel3d45.time.sleep")
    @patch("octoprint_dremel3d45.Dremel3D45Plugin._notify_redirect")
    def test_successful_redirect(self, mock_notify, mock_sleep):
        """Happy path: upload succeeds and print starts."""
        # default_request is imported locally inside _do_redirect_local_print,
        # so patch it in the vendor module.
        with patch(
            "octoprint_dremel3d45.vendor.dremel3dpy.default_request"
        ) as mock_request:
            self.plugin._local_print_redirecting = True
            self.plugin._do_redirect_local_print(self.tmp.name, "test.gcode")

        # Verify upload was called
        self.mock_vs.upload_file.assert_called_once_with(
            self.tmp.name, "test.gcode",
        )

        # Verify print was started via REST
        mock_request.assert_called_once()

        # Verify poll state was populated
        self.assertTrue(self.mock_vs._was_printing)
        self.assertEqual(self.mock_vs._last_announced_job_name, "test.gcode")

        # Verify notifications
        notify_calls = [c.args for c in mock_notify.call_args_list]
        self.assertIn(("uploading", "test.gcode"), notify_calls)
        self.assertIn(("success", "test.gcode"), notify_calls)

    @patch("octoprint_dremel3d45.time.sleep")
    @patch("octoprint_dremel3d45.Dremel3D45Plugin._notify_redirect")
    def test_redirect_file_not_found(self, mock_notify, mock_sleep):
        """Redirect should fail gracefully when file doesn't exist."""
        self.plugin._local_print_redirecting = True
        self.plugin._do_redirect_local_print("/nonexistent/file.gcode", "file.gcode")

        self.mock_vs.upload_file.assert_not_called()
        mock_notify.assert_called_with("error", "file.gcode", "File not found")

    @patch("octoprint_dremel3d45.time.sleep")
    @patch("octoprint_dremel3d45.Dremel3D45Plugin._notify_redirect")
    def test_redirect_not_connected(self, mock_notify, mock_sleep):
        """Redirect should fail gracefully when not connected."""
        self.plugin._virtual_serial = None
        self.plugin._local_print_redirecting = True
        self.plugin._do_redirect_local_print(self.tmp.name, "test.gcode")

        mock_notify.assert_called_with(
            "error", "test.gcode", "Not connected to printer",
        )

    @patch("octoprint_dremel3d45.time.sleep")
    @patch("octoprint_dremel3d45.Dremel3D45Plugin._notify_redirect")
    def test_redirect_upload_fails(self, mock_notify, mock_sleep):
        """Redirect should fail gracefully when upload fails."""
        self.mock_vs.upload_file.return_value = False
        self.plugin._local_print_redirecting = True
        self.plugin._do_redirect_local_print(self.tmp.name, "test.gcode")

        notify_calls = [c.args for c in mock_notify.call_args_list]
        self.assertIn(("error", "test.gcode", "Upload failed"), notify_calls)

    @patch("octoprint_dremel3d45.time.sleep")
    @patch("octoprint_dremel3d45.Dremel3D45Plugin._notify_redirect")
    def test_redirect_no_remote_name(self, mock_notify, mock_sleep):
        """Redirect should fail if upload returns no remote name."""
        self.mock_vs._selected_file_remote = None
        self.plugin._local_print_redirecting = True
        self.plugin._do_redirect_local_print(self.tmp.name, "test.gcode")

        notify_calls = [c.args for c in mock_notify.call_args_list]
        self.assertIn(
            ("error", "test.gcode", "Internal upload error"), notify_calls,
        )

    @patch("octoprint_dremel3d45.time.sleep")
    def test_redirect_clears_flag_in_finally(self, mock_sleep):
        """The redirecting flag should always be cleared in finally."""
        self.plugin._local_print_redirecting = True
        # Run with a missing file so it exits early
        self.plugin._do_redirect_local_print("/no/such/file", "x.gcode")
        self.assertFalse(self.plugin._local_print_redirecting)

    @patch("octoprint_dremel3d45.time.sleep")
    def test_redirect_clears_flag_on_exception(self, mock_sleep):
        """The redirecting flag should be cleared even on unexpected errors."""
        self.mock_vs.upload_file.side_effect = RuntimeError("boom")
        self.plugin._local_print_redirecting = True
        self.plugin._do_redirect_local_print(self.tmp.name, "test.gcode")
        self.assertFalse(self.plugin._local_print_redirecting)

    @patch("octoprint_dremel3d45.time.sleep")
    def test_redirect_vs_closed(self, mock_sleep):
        """Should fail gracefully when virtual serial is closed."""
        self.mock_vs._closed = True
        self.plugin._local_print_redirecting = True
        self.plugin._do_redirect_local_print(self.tmp.name, "test.gcode")
        self.mock_vs.upload_file.assert_not_called()


class TestOnEvent(unittest.TestCase):
    """Test the EventHandlerPlugin.on_event method."""

    def setUp(self):
        self.plugin = Dremel3D45Plugin()
        self.plugin._virtual_serial = MagicMock()

    def test_ignores_non_print_events(self):
        """Non-print events should be ignored."""
        # Should not raise
        self.plugin.on_event("Connected", {})
        self.plugin.on_event("Disconnected", {})
        self.plugin.on_event("FileAdded", {"origin": "local"})

    def test_ignores_sd_print_started(self):
        """SD-origin PrintStarted should not trigger redirect logging."""
        # Should not raise or set any flags
        self.plugin.on_event("PrintStarted", {"origin": "sdcard", "name": "test.gcode"})

    def test_logs_local_print_started(self):
        """Local-origin PrintStarted should be logged (redirect is via hook)."""
        # Should not raise — just logs
        self.plugin.on_event("PrintStarted", {"origin": "local", "name": "test.gcode"})

    def test_skips_when_not_connected(self):
        """Should do nothing when virtual serial is not active."""
        self.plugin._virtual_serial = None
        # Should not raise
        self.plugin.on_event("PrintStarted", {"origin": "local", "name": "test.gcode"})


class TestNotifyRedirect(unittest.TestCase):
    """Test the _notify_redirect helper."""

    def setUp(self):
        self.plugin = Dremel3D45Plugin()
        self.plugin._plugin_manager = MagicMock()
        self.plugin._identifier = "dremel3d45"

    def test_sends_plugin_message(self):
        self.plugin._notify_redirect("uploading", "test.gcode")
        self.plugin._plugin_manager.send_plugin_message.assert_called_once_with(
            "dremel3d45",
            {
                "type": "local_print_redirect",
                "status": "uploading",
                "filename": "test.gcode",
                "message": "",
            },
        )

    def test_handles_missing_plugin_manager(self):
        """Should not raise if _plugin_manager is not set."""
        self.plugin._plugin_manager = None
        # Should not raise
        self.plugin._notify_redirect("error", "test.gcode", "boom")


class TestHandleTestConnection(unittest.TestCase):
    """Test connection test API behavior and live-session reuse."""

    def setUp(self):
        self.plugin = Dremel3D45Plugin()
        self.plugin._settings = MagicMock()
        self._settings_data = {
            "printer_ip": "192.168.1.50",
            "request_timeout": 30,
        }
        self.plugin._settings.get.side_effect = lambda path: self._settings_data.get(path[0])
        self.plugin._settings.get_int.side_effect = lambda path: int(self._settings_data.get(path[0]))

    @patch("flask.jsonify", side_effect=lambda **kwargs: kwargs)
    @patch("octoprint_dremel3d45.vendor.dremel3dpy.Dremel3DPrinter")
    def test_uses_live_virtual_session_when_healthy(self, mock_printer_cls, mock_jsonify):
        live_printer = MagicMock()
        live_printer.get_firmware_version.return_value = "3.0"
        live_printer.get_title.return_value = "Dremel 3D45"
        live_printer.get_serial_number.return_value = "LIVE123"

        self.plugin._virtual_serial = MagicMock(
            _closed=False,
            _host="192.168.1.50",
            _connected=True,
            _connection_errors=0,
            _printer=live_printer,
        )

        result = self.plugin._handle_test_connection()

        mock_printer_cls.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "live-session")
        self.assertEqual(result["serial"], "LIVE123")

    @patch("flask.jsonify", side_effect=lambda **kwargs: kwargs)
    @patch("octoprint_dremel3d45.vendor.dremel3dpy.Dremel3DPrinter")
    def test_falls_back_to_fresh_probe_when_no_active_session(self, mock_printer_cls, mock_jsonify):
        fresh_printer = MagicMock()
        fresh_printer.get_firmware_version.return_value = "3.0"
        fresh_printer.get_title.return_value = "Dremel 3D45"
        fresh_printer.get_serial_number.return_value = "PROBE123"
        mock_printer_cls.return_value = fresh_printer

        self.plugin._virtual_serial = None

        result = self.plugin._handle_test_connection()

        mock_printer_cls.assert_called_once_with("192.168.1.50")
        fresh_printer.set_printer_info.assert_called_once_with(refresh=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "fresh-probe")
        self.assertEqual(result["serial"], "PROBE123")

    @patch("flask.jsonify", side_effect=lambda **kwargs: kwargs)
    @patch("octoprint_dremel3d45.vendor.dremel3dpy.Dremel3DPrinter")
    def test_falls_back_to_fresh_probe_when_session_has_errors(self, mock_printer_cls, mock_jsonify):
        fresh_printer = MagicMock()
        fresh_printer.get_firmware_version.return_value = "3.0"
        fresh_printer.get_title.return_value = "Dremel 3D45"
        fresh_printer.get_serial_number.return_value = "PROBE456"
        mock_printer_cls.return_value = fresh_printer

        self.plugin._virtual_serial = MagicMock(
            _closed=False,
            _host="192.168.1.50",
            _connected=True,
            _connection_errors=2,
            _printer=MagicMock(),
        )

        result = self.plugin._handle_test_connection()

        mock_printer_cls.assert_called_once_with("192.168.1.50")
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "fresh-probe")


if __name__ == "__main__":
    unittest.main()
