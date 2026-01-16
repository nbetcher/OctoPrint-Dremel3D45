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
"""

from __future__ import annotations

import logging
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
__plugin_version__ = "0.1.1"
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
        octoprint.plugin.SettingsPlugin,
        octoprint.plugin.SimpleApiPlugin,
        octoprint.plugin.TemplatePlugin,
        octoprint.plugin.AssetPlugin,
    ):
        """OctoPrint plugin for Dremel 3D45 network control."""

        def __init__(self):
            super().__init__()
            self._virtual_serial = None

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
                    "Settings: timeout=%ss, poll_interval=%ss, camera_enabled=%s",
                    self._settings.get(["request_timeout"]),
                    self._settings.get(["poll_interval"]),
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
        # SettingsPlugin
        # -------------------------------------------------------------------------

        def get_settings_defaults(self) -> dict:
            return {
                "printer_ip": "",
                "request_timeout": 30,
                "poll_interval": 10,
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
                    ["camera_enabled"],
                    ["camera_update_global"],
                    ["camera_stream_url"],
                    ["camera_snapshot_url"],
                ]
            }

        def on_settings_save(self, data: dict) -> dict:
            _LOGGER.info("on_settings_save called with data: %s", data)
            old_ip = self._settings.get(["printer_ip"])

            # Let OctoPrint persist settings and get the diff
            diff = octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

            new_ip = self._settings.get(["printer_ip"])
            _LOGGER.info("Settings saved: old_ip=%r, new_ip=%r", old_ip, new_ip)
            if old_ip != new_ip:
                _LOGGER.info("Printer IP changed from %s to %s", old_ip, new_ip)
                if self._virtual_serial:
                    _LOGGER.warning(
                        "Printer IP changed while connected - reconnect required for changes to take effect"
                    )
            _LOGGER.info(
                "Settings after save: timeout=%ss, poll_interval=%ss",
                self._settings.get(["request_timeout"]),
                self._settings.get(["poll_interval"]),
            )

            if self._settings.get_boolean(["camera_enabled"]) and self._settings.get_boolean(
                ["camera_update_global"]
            ):
                self._configure_camera()

            # Return the diff as expected by OctoPrint
            return diff

        def _configure_camera(self) -> None:
            """Configure OctoPrint's webcam settings for Dremel camera."""
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
                from octoprint.settings import settings as octoprint_settings

                s = octoprint_settings()
                s.set(["webcam", "stream"], stream_url)
                s.set(["webcam", "snapshot"], snapshot_url)
                s.set(["webcam", "streamRatio"], "4:3")
                s.save()
                _LOGGER.info("Global webcam settings updated successfully")
            except Exception as e:
                _LOGGER.warning("Failed to update global webcam settings: %s", e)

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
                "clear_sd_index": [],
            }

        def on_api_get(self, request):  # noqa: ANN001
            """Return plugin status information for the settings UI."""
            _LOGGER.debug("API GET request received")
            from flask import jsonify

            sd_index = {"count": 0, "items": []}
            sd_index_path = None

            try:
                sd_index_path = self.get_plugin_data_folder()
            except Exception:
                sd_index_path = None

            index_file = None
            if sd_index_path:
                try:
                    import os

                    index_file = os.path.join(sd_index_path, "sd_index.json")
                except Exception:
                    index_file = None

            if self._virtual_serial:
                try:
                    sd_index = self._virtual_serial.get_sd_index_snapshot()
                except Exception:
                    sd_index = {"count": 0, "items": []}
            elif index_file:
                # Best-effort load from disk when not connected
                try:
                    import json
                    import os

                    if os.path.exists(index_file):
                        with open(index_file, "r", encoding="utf-8") as f:
                            payload = json.load(f)
                        items = payload.get("items", {})
                        if isinstance(items, dict):
                            cleaned = []
                            for _, meta in items.items():
                                if isinstance(meta, dict):
                                    cleaned.append(meta)
                            cleaned.sort(key=lambda x: str(x.get("display") or "").lower())
                            sd_index = {"count": len(cleaned), "items": cleaned}
                except Exception:
                    sd_index = {"count": 0, "items": []}

            return jsonify(
                connected=bool(self._virtual_serial is not None),
                sd_index=sd_index,
            )

        def on_api_command(self, command: str, data):  # noqa: ANN001
            _LOGGER.debug("API command received: %s", command)
            if command != "clear_sd_index":
                _LOGGER.debug("Unknown API command: %s", command)
                return

            from flask import jsonify

            _LOGGER.info("Clearing SD file index")

            # Clear in-memory (if connected) and on-disk mapping
            if self._virtual_serial:
                try:
                    self._virtual_serial.clear_sd_index()
                    _LOGGER.debug("Cleared in-memory SD index")
                except Exception as e:
                    _LOGGER.warning("Failed to clear in-memory SD index: %s", e)

            try:
                import os

                data_folder = self.get_plugin_data_folder()
                index_file = os.path.join(data_folder, "sd_index.json")
                if os.path.exists(index_file):
                    os.remove(index_file)
                    _LOGGER.debug("Removed SD index file: %s", index_file)
            except Exception as e:
                _LOGGER.warning("Failed to remove SD index file: %s", e)

            _LOGGER.info("SD file index cleared successfully")
            return jsonify(ok=True)

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
            _LOGGER.info(
                "virtual_serial_factory hook called for port=%s (looking for %s)",
                port, DREMEL_PORT_NAME,
            )

            if port != DREMEL_PORT_NAME:
                _LOGGER.info("Serial factory: port %s is not our port, returning None", port)
                return None

            _LOGGER.info(
                "Serial factory called for %s (baudrate=%s, timeout=%s)",
                port, baudrate, read_timeout,
            )

            # Debug: log what settings we can see
            printer_ip = self._settings.get(["printer_ip"])
            _LOGGER.info(
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

            data_folder = None
            try:
                data_folder = self.get_plugin_data_folder()
            except Exception:
                data_folder = None

            self._virtual_serial = DremelVirtualSerial(
                settings=self._settings,
                read_timeout=float(read_timeout),
                data_folder=data_folder,
            )

            _LOGGER.info("Virtual serial connection created successfully")
            return self._virtual_serial

        def get_additional_port_names(self, *args, **kwargs) -> list:
            """
            Hook: octoprint.comm.transport.serial.additional_port_names

            Called to get additional port names to show in the connection dropdown.
            """
            # Always show the port - if IP is not configured, user will see
            # an error when they try to connect (handled in virtual_serial_factory)
            _LOGGER.debug("get_additional_port_names hook called - returning [%s]", DREMEL_PORT_NAME)
            return [DREMEL_PORT_NAME]

        # -------------------------------------------------------------------------
        # SD Card Upload Hook
        # -------------------------------------------------------------------------

        def sdcard_upload_hook(
            self,
            printer,
            filename: str,
            path: str,
            sd_upload_started,
            sd_upload_succeeded,
            sd_upload_failed,
            *args,
            **kwargs,
        ):
            """
            Hook: octoprint.printer.sdcardupload

            Called when OctoPrint wants to upload a file to the printer's SD card.
            We intercept this and upload via the Dremel REST API.
            """
            if not self._virtual_serial:
                _LOGGER.error("SD upload failed: not connected to Dremel printer")
                sd_upload_failed(
                    filename, path, "Not connected to Dremel printer"
                )
                return

            _LOGGER.info("Starting SD upload: %s -> %s", path, filename)
            _LOGGER.debug("Upload source path: %s", path)
            sd_upload_started(filename, path)

            try:
                success = self._virtual_serial.upload_file(path, filename)
                if success:
                    _LOGGER.info("SD upload succeeded: %s", filename)
                    sd_upload_succeeded(filename, path)
                else:
                    _LOGGER.error("SD upload failed (no exception): %s", filename)
                    sd_upload_failed(filename, path, "Upload failed")
            except Exception as e:
                _LOGGER.exception("SD upload error for %s: %s", filename, e)
                sd_upload_failed(filename, path, str(e))


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
        # SD card upload hook
        "octoprint.printer.sdcardupload": plugin.sdcard_upload_hook,
    }

    _LOGGER.info(
        "Plugin hooks registered: %s",
        list(__plugin_hooks__.keys()),
    )
