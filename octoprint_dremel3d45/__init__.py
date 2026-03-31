# -*- coding: utf-8 -*-
"""
OctoPrint Dremel 3D45 Plugin.

Provides network-based control of Dremel 3D45 printers via REST API,
presenting as a virtual serial connection to OctoPrint.

This plugin follows OctoPrint's plugin guidelines and uses the standard
virtual serial transport pattern (like the bundled virtual_printer plugin).

Hooks used:
    - octoprint.comm.transport.serial.factory
    - octoprint.comm.transport.serial.additional_port_names
    - octoprint.comm.protocol.gcode.queuing
    - octoprint.printer.estimation.remaining

Note: The Dremel 3D45 has no real SD card support. Print progress is reported
using Marlin-compatible ``SD printing byte X/Y`` messages so that OctoPrint's
progress tracking works normally.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

# OctoPrint may not be available during testing
try:
    import octoprint.plugin
    _OCTOPRINT_AVAILABLE = True
except ImportError:
    _OCTOPRINT_AVAILABLE = False
    octoprint = None  # type: ignore

if TYPE_CHECKING:
    from octoprint.settings import Settings

_LOGGER = logging.getLogger("octoprint.plugins.dremel3d45")

__plugin_name__ = "Dremel 3D45"
__plugin_pythoncompat__ = ">=3.7,<4"
__plugin_version__ = "1.0.0"
__plugin_author__ = "Nick Betcher"
__plugin_author_email__ = "nick@nickbetcher.com"
__plugin_url__ = "https://www.nickbetcher.com/projects/octoprint_dremel3d45"


# Port name for connection dropdown
DREMEL_PORT_NAME = "DREMEL3D45"


# Define plugin class only if OctoPrint is available
if _OCTOPRINT_AVAILABLE:
    class Dremel3D45Plugin(
        octoprint.plugin.StartupPlugin,
        octoprint.plugin.ShutdownPlugin,
        octoprint.plugin.EventHandlerPlugin,
        octoprint.plugin.SettingsPlugin,
        octoprint.plugin.SimpleApiPlugin,
        octoprint.plugin.TemplatePlugin,
        octoprint.plugin.AssetPlugin,
    ):
        """OctoPrint plugin for Dremel 3D45 network control."""

        def __init__(self):
            super().__init__()
            self._virtual_serial = None
            self._local_print_redirecting = False

        # -------------------------------------------------------------------------
        # StartupPlugin
        # -------------------------------------------------------------------------

        def on_startup(self, host: str, port: int) -> None:
            _LOGGER.info("Dremel 3D45 plugin starting up (OctoPrint host=%s:%s)", host, port)
            _LOGGER.debug("Plugin version: %s", __plugin_version__)

        def on_after_startup(self) -> None:
            _LOGGER.info("Dremel 3D45 plugin ready")

            printer_ip = self._settings.get(["printer_ip"])
            if printer_ip:
                _LOGGER.info("Configured printer IP: %s", printer_ip)
                _LOGGER.info(
                    "To connect: Select port '%s' in the connection panel",
                    DREMEL_PORT_NAME,
                )
                _LOGGER.debug(
                    "Settings: timeout=%ss, poll_interval_printing=%ss, poll_interval_idle=%ss, camera_enabled=%s",
                    self._settings.get(["request_timeout"]),
                    self._settings.get(["poll_interval_printing"]),
                    self._settings.get(["poll_interval_idle"]),
                    self._settings.get_boolean(["camera_enabled"]),
                )
            else:
                _LOGGER.warning(
                    "No printer IP configured - please configure in settings"
                )

            if self._settings.get_boolean(["camera_enabled"]) and self._settings.get_boolean(
                ["camera_update_global"]
            ):
                self._configure_camera()

        # -------------------------------------------------------------------------
        # ShutdownPlugin
        # -------------------------------------------------------------------------

        def on_shutdown(self) -> None:
            _LOGGER.info("Dremel 3D45 plugin shutting down")
            if self._virtual_serial:
                _LOGGER.debug("Closing active virtual serial connection")
                try:
                    self._virtual_serial.close()
                    _LOGGER.debug("Virtual serial connection closed successfully")
                except Exception as e:
                    _LOGGER.warning("Error closing virtual serial connection: %s", e)
                self._virtual_serial = None
            _LOGGER.info("Dremel 3D45 plugin shutdown complete")

        # -------------------------------------------------------------------------
        # EventHandlerPlugin
        # -------------------------------------------------------------------------

        def on_event(self, event, payload):
            """React to OctoPrint events.

            Detects local file prints (which the Dremel cannot execute via
            streamed GCode) and logs the redirect.  The actual interception
            and suppression happens in ``gcode_queuing_hook``.
            """
            if not self._virtual_serial:
                return

            if event == "PrintStarted" and payload.get("origin") == "local":
                _LOGGER.info(
                    "Local file print detected: %s — will be redirected to Dremel",
                    payload.get("name", "unknown"),
                )

        # -------------------------------------------------------------------------
        # SettingsPlugin
        # -------------------------------------------------------------------------

        def get_settings_defaults(self) -> dict:
            return {
                "printer_ip": "",
                "request_timeout": 30,
                # Legacy single-interval setting retained for compatibility.
                "poll_interval": 10,
                # Adaptive polling: faster while printing, slower while idle.
                "poll_interval_printing": 5,
                "poll_interval_idle": 15,
                "camera_enabled": False,
                "camera_update_global": False,
                "camera_stream_url": "",
                "camera_snapshot_url": "",
            }

        def get_settings_restricted_paths(self) -> dict:
            return {
                "admin": [
                    ["printer_ip"],
                    ["request_timeout"],
                    ["poll_interval"],
                    ["poll_interval_printing"],
                    ["poll_interval_idle"],
                    ["camera_enabled"],
                    ["camera_update_global"],
                    ["camera_stream_url"],
                    ["camera_snapshot_url"],
                ]
            }

        def on_settings_save(self, data: dict) -> dict:
            _LOGGER.debug("on_settings_save called with data: %s", data)
            old_ip = self._settings.get(["printer_ip"])

            # Let OctoPrint persist settings and get the diff
            diff = octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

            new_ip = self._settings.get(["printer_ip"])
            _LOGGER.debug("Settings saved: old_ip=%r, new_ip=%r", old_ip, new_ip)
            if old_ip != new_ip:
                _LOGGER.info("Printer IP changed from %s to %s", old_ip, new_ip)
                if self._virtual_serial:
                    _LOGGER.info(
                        "Closing existing connection due to IP change"
                    )
                    try:
                        self._virtual_serial.close()
                    except Exception as e:
                        _LOGGER.warning("Error closing virtual serial on IP change: %s", e)
                    self._virtual_serial = None
            _LOGGER.debug(
                "Settings after save: timeout=%ss, poll_interval_printing=%ss, poll_interval_idle=%ss",
                self._settings.get(["request_timeout"]),
                self._settings.get(["poll_interval_printing"]),
                self._settings.get(["poll_interval_idle"]),
            )

            # Propagate mutable settings to the running virtual serial session
            if self._virtual_serial:
                try:
                    self._virtual_serial.update_settings()
                except Exception as e:
                    _LOGGER.warning("Failed to propagate settings to virtual serial: %s", e)

            if self._settings.get_boolean(["camera_enabled"]) and self._settings.get_boolean(
                ["camera_update_global"]
            ):
                self._configure_camera()

            # Return the diff as expected by OctoPrint
            return diff

        def _configure_camera(self) -> None:
            """Configure OctoPrint's webcam settings for Dremel camera.

            Supports both the modern classicwebcam plugin (OctoPrint 1.9+)
            and the legacy top-level webcam settings path for older versions.
            """
            printer_ip = self._settings.get(["printer_ip"])
            if not printer_ip:
                _LOGGER.debug("Cannot configure camera: no printer IP set")
                return

            stream_url = self._settings.get(["camera_stream_url"])
            snapshot_url = self._settings.get(["camera_snapshot_url"])

            if not stream_url:
                stream_url = f"http://{printer_ip}:10123/?action=stream"
                _LOGGER.debug("Using default stream URL: %s", stream_url)
            if not snapshot_url:
                snapshot_url = f"http://{printer_ip}:10123/?action=snapshot"
                _LOGGER.debug("Using default snapshot URL: %s", snapshot_url)

            _LOGGER.info("Configuring global webcam settings: stream=%s, snapshot=%s", stream_url, snapshot_url)

            try:
                # OctoPrint 1.9+ moved webcam settings into the classicwebcam plugin
                webcam_plugin = self._plugin_manager.get_plugin_info("classicwebcam", require_enabled=True)
                if webcam_plugin and webcam_plugin.implementation:
                    impl = webcam_plugin.implementation
                    impl._settings.set(["stream"], stream_url)
                    impl._settings.set(["snapshot"], snapshot_url)
                    impl._settings.set(["streamRatio"], "4:3")
                    impl._settings.save()
                    _LOGGER.info("Updated classicwebcam plugin settings")
                else:
                    # Fallback: legacy OctoPrint webcam path (< 1.9)
                    from octoprint.settings import settings as octoprint_settings

                    s = octoprint_settings()
                    s.set(["webcam", "stream"], stream_url)
                    s.set(["webcam", "snapshot"], snapshot_url)
                    s.set(["webcam", "streamRatio"], "4:3")
                    s.save()
                    _LOGGER.info("Updated legacy webcam settings (classicwebcam plugin not available)")
            except Exception as e:
                _LOGGER.warning("Failed to update webcam settings: %s", e)

        def _handle_test_connection(self):
            """Test connection to Dremel printer and return status."""
            from flask import jsonify

            printer_ip = self._settings.get(["printer_ip"])
            if not printer_ip:
                return jsonify(ok=False, error="No printer IP configured")

            _LOGGER.info("Testing connection to %s", printer_ip)
            try:
                from .vendor import dremel3dpy as _dremel3dpy
                from .vendor.dremel3dpy import Dremel3DPrinter
                from .vendor.dremel3dpy.helpers import constants as _c

                timeout = self._settings.get_int(["request_timeout"]) or 30
                _c.REQUEST_TIMEOUT = timeout
                _dremel3dpy.REQUEST_TIMEOUT = timeout

                # Prefer a healthy active session to avoid a redundant probe.
                printer = None
                source = "fresh-probe"
                vs = self._virtual_serial
                if (
                    vs
                    and not getattr(vs, "_closed", True)
                    and getattr(vs, "_host", "") == printer_ip
                    and getattr(vs, "_connected", False)
                    and getattr(vs, "_connection_errors", 0) == 0
                    and getattr(vs, "_printer", None) is not None
                ):
                    printer = vs._printer
                    source = "live-session"

                if printer is None:
                    printer = Dremel3DPrinter(printer_ip)
                    printer.set_printer_info(refresh=True)

                fw = printer.get_firmware_version() or "Unknown"
                model = printer.get_title() or "Unknown"
                sn = printer.get_serial_number() or "Unknown"
                _LOGGER.info(
                    "Connection test succeeded (%s): %s (fw=%s)",
                    source,
                    model,
                    fw,
                )
                return jsonify(ok=True, firmware=fw, model=model, serial=sn, source=source)
            except Exception as e:
                _LOGGER.warning("Connection test failed: %s", e)
                return jsonify(ok=False, error=str(e))

        # -------------------------------------------------------------------------
        # TemplatePlugin
        # -------------------------------------------------------------------------

        def get_template_configs(self) -> list:
            return [
                {
                    "type": "settings",
                    "name": "Dremel 3D45",
                    "template": "dremel3d45_settings.jinja2",
                    "custom_bindings": False,
                }
            ]

        # -------------------------------------------------------------------------
        # AssetPlugin
        # -------------------------------------------------------------------------

        def get_assets(self) -> dict:
            return {
                "js": ["js/dremel3d45.js"],
                "css": [],
            }

        # -------------------------------------------------------------------------
        # SimpleApiPlugin
        # -------------------------------------------------------------------------

        def is_api_protected(self):  # noqa: ANN001
            """Explicitly declare API protection status (OctoPrint 1.11.2+).

            The plugin API is intended for authenticated UI usage.
            """
            return True

        def get_api_commands(self) -> dict:
            return {
                "test_connection": [],
            }

        def on_api_get(self, request):  # noqa: ANN001
            """Return plugin status information for the settings UI."""
            _LOGGER.debug("API GET request received")
            from flask import jsonify

            connected = False
            if self._virtual_serial:
                try:
                    connected = bool(self._virtual_serial.is_open)
                except Exception:
                    connected = False

            return jsonify(
                connected=connected,
            )

        def on_api_command(self, command: str, data):  # noqa: ANN001
            _LOGGER.debug("API command received: %s", command)

            if command == "test_connection":
                return self._handle_test_connection()

            _LOGGER.debug("Unknown API command: %s", command)

        # -------------------------------------------------------------------------
        # Print Time Estimation Hook
        # -------------------------------------------------------------------------

        def estimate_remaining_print_time(
            self,
            origin,
            filename,
            progress,
            printTime,
            cleanedPrintTime,
            statisticalTotalPrintTime,
            statisticalTotalPrintTimeType,
        ):
            """
            Hook: octoprint.printer.estimation.remaining

            Provides the Dremel API's remaining-time estimate directly to
            OctoPrint's print time estimator.  This supplements the M73
            progress report we send over the serial line.
            """
            if (
                self._virtual_serial
                and getattr(self._virtual_serial, "_printing", False)
                and self._virtual_serial._remaining_time > 0
            ):
                return self._virtual_serial._remaining_time, "dremel"
            return None

        # -------------------------------------------------------------------------
        # Virtual Serial Factory Hook
        # -------------------------------------------------------------------------

        def virtual_serial_factory(
            self,
            comm_instance,
            port: str,
            baudrate: int,
            read_timeout: float,
        ):
            """
            Hook: octoprint.comm.transport.serial.factory

            Called when OctoPrint tries to open a serial connection.
            If port is DREMEL3D45, return our virtual serial object.
            """
            _LOGGER.debug(
                "virtual_serial_factory hook called for port=%s (looking for %s)",
                port, DREMEL_PORT_NAME,
            )

            if port != DREMEL_PORT_NAME:
                _LOGGER.debug("Serial factory: port %s is not our port, returning None", port)
                return None

            _LOGGER.debug(
                "Serial factory called for %s (baudrate=%s, timeout=%s)",
                port, baudrate, read_timeout,
            )

            # Debug: log what settings we can see
            printer_ip = self._settings.get(["printer_ip"])
            _LOGGER.debug(
                "Settings check: printer_ip=%r, _settings type=%s",
                printer_ip, type(self._settings).__name__,
            )

            if not printer_ip:
                _LOGGER.error(
                    "Cannot connect to %s: No printer IP configured", DREMEL_PORT_NAME
                )
                return None

            _LOGGER.info(
                "Creating Dremel virtual serial connection to %s",
                printer_ip,
            )

            from .virtual_serial import DremelVirtualSerial

            self._virtual_serial = DremelVirtualSerial(
                settings=self._settings,
                read_timeout=float(read_timeout),
            )

            _LOGGER.info("Virtual serial connection created successfully")
            return self._virtual_serial

        def get_additional_port_names(self, *args, **kwargs) -> list:
            """
            Hook: octoprint.comm.transport.serial.additional_port_names

            Called to get additional port names to show in the connection dropdown.
            """
            # Only advertise the virtual port when an IP is configured.
            # This avoids OctoPrint autodetect repeatedly trying/"failing"
            # a synthetic serial port before the plugin is configured.
            printer_ip = (self._settings.get(["printer_ip"]) or "").strip()
            if not printer_ip:
                _LOGGER.debug(
                    "get_additional_port_names hook called - no printer IP configured, returning []"
                )
                return []

            _LOGGER.debug(
                "get_additional_port_names hook called - returning [%s]",
                DREMEL_PORT_NAME,
            )
            return [DREMEL_PORT_NAME]

        # -------------------------------------------------------------------------
        # GCode Queuing Hook — Local Print Interception
        # -------------------------------------------------------------------------

        def gcode_queuing_hook(
            self,
            comm_instance,
            phase,
            cmd,
            cmd_type,
            gcode,
            subcode=None,
            tags=None,
            *args,
            **kwargs,
        ):
            """
            Hook: octoprint.comm.protocol.gcode.queuing

            The Dremel 3D45 cannot execute streamed GCode — it can only
            print files uploaded to it via REST API.  When OctoPrint
            streams a local file, we suppress every command and redirect
            to the Dremel's native upload-and-print workflow in a
            background thread.
            """
            if not self._virtual_serial:
                return None

            if tags is None:
                tags = set()

            # During a redirect, also suppress after-print script commands
            # (e.g. afterPrintDone sends M104 S0 / M140 S0 cooldown that
            # would interfere with the Dremel's own preheat cycle).
            if self._local_print_redirecting:
                if any(t.startswith("source:script") for t in tags):
                    return (None,)

            # Only intercept file-sourced commands
            if "source:file" not in tags:
                return None

            # First file command: kick off the redirect
            if not self._local_print_redirecting:
                self._local_print_redirecting = True

                # Extract the file path from OctoPrint's comm layer
                file_path = None
                try:
                    current_file = getattr(comm_instance, "_currentFile", None)
                    if current_file and hasattr(current_file, "getFilename"):
                        file_path = current_file.getFilename()
                except Exception:
                    _LOGGER.debug(
                        "Could not read file path from comm instance",
                        exc_info=True,
                    )

                file_name = os.path.basename(file_path) if file_path else None
                _LOGGER.info(
                    "Intercepting local file print — redirecting to Dremel: %s",
                    file_name or "(unknown)",
                )

                threading.Thread(
                    target=self._do_redirect_local_print,
                    args=(file_path, file_name),
                    daemon=True,
                    name="DremelLocalPrintRedirect",
                ).start()

            # Suppress every file-sourced command
            return (None,)

        # ------------------------------------------------------------------ #
        #  Background redirect logic                                          #
        # ------------------------------------------------------------------ #

        def _do_redirect_local_print(self, file_path, file_name):
            """Upload a local file to the Dremel and start printing.

            Runs in a background thread.  ``gcode_queuing_hook`` suppresses
            all streamed commands while this is in progress.
            """
            try:
                if not file_path or not os.path.isfile(file_path):
                    _LOGGER.error(
                        "Cannot redirect local print: file not found (%s)",
                        file_path,
                    )
                    self._notify_redirect("error", file_name, "File not found")
                    return

                vs = self._virtual_serial
                if not vs or getattr(vs, "_closed", True):
                    _LOGGER.error("Cannot redirect local print: not connected")
                    self._notify_redirect(
                        "error", file_name, "Not connected to printer"
                    )
                    return

                # Notify the frontend that upload is beginning
                self._notify_redirect("uploading", file_name)

                # Upload via the virtual serial upload helper
                if not vs.upload_file(file_path, file_name):
                    _LOGGER.error("Dremel upload failed for %s", file_name)
                    self._notify_redirect("error", file_name, "Upload failed")
                    return

                # Retrieve the remote name assigned by the Dremel
                with vs._lock:
                    remote_name = vs._selected_file_remote
                    display_name = vs._selected_file_display

                if not remote_name:
                    _LOGGER.error("Upload OK but no remote name available")
                    self._notify_redirect(
                        "error", file_name, "Internal upload error"
                    )
                    return

                # Start the print via the Dremel REST API
                _LOGGER.info(
                    "Starting Dremel print: %s (remote=%s)",
                    display_name,
                    remote_name,
                )
                from .vendor.dremel3dpy import PRINT_COMMAND, default_request

                default_request(vs._host, {PRINT_COMMAND: remote_name})

                # Pre-populate poll-thread state so it doesn't re-announce
                with vs._lock:
                    vs._was_printing = True
                    vs._last_announced_job_name = display_name

                _LOGGER.info(
                    "Local print redirected to Dremel successfully: %s",
                    display_name,
                )
                self._notify_redirect("success", display_name)

            except Exception:
                _LOGGER.exception("Local print redirect failed")
                self._notify_redirect(
                    "error", file_name, "Unexpected error during redirect"
                )
            finally:
                # Keep the flag active long enough for OctoPrint's
                # afterPrintDone script to be suppressed, then clear.
                time.sleep(5)
                self._local_print_redirecting = False

        def _notify_redirect(self, status, filename=None, message=None):
            """Push a redirect status notification to the frontend."""
            try:
                self._plugin_manager.send_plugin_message(
                    self._identifier,
                    {
                        "type": "local_print_redirect",
                        "status": status,
                        "filename": filename or "",
                        "message": message or "",
                    },
                )
            except Exception:
                pass




# -----------------------------------------------------------------------------
# Plugin Registration (using __plugin_load__ pattern like Virtual Printer)
# -----------------------------------------------------------------------------

def __plugin_load__():
    global __plugin_implementation__
    global __plugin_hooks__

    if not _OCTOPRINT_AVAILABLE:
        __plugin_implementation__ = None
        __plugin_hooks__ = {}
        return

    plugin = Dremel3D45Plugin()
    __plugin_implementation__ = plugin

    __plugin_hooks__ = {
        # Virtual serial transport hooks
        "octoprint.comm.transport.serial.factory": (
            plugin.virtual_serial_factory,
            1,  # Priority: run before default serial factory
        ),
        "octoprint.comm.transport.serial.additional_port_names": plugin.get_additional_port_names,
        # GCode queuing hook — intercept local file prints
        "octoprint.comm.protocol.gcode.queuing": plugin.gcode_queuing_hook,
        # Print time estimation from Dremel API
        "octoprint.printer.estimation.remaining": plugin.estimate_remaining_print_time,
    }

    _LOGGER.info(
        "Plugin hooks registered: %s",
        list(__plugin_hooks__.keys()),
    )
