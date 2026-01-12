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

try:
    import octoprint.plugin as octoprint_plugin
except Exception:
    octoprint_plugin = None

if TYPE_CHECKING:
    from octoprint.settings import Settings

_LOGGER = logging.getLogger("octoprint.plugins.dremel3d45")

__plugin_name__ = "Dremel 3D45"
__plugin_pythoncompat__ = ">=3.7,<4"
__plugin_version__ = "0.1.0"
__plugin_author__ = "Nick Betcher"
__plugin_author_email__ = "nick@nickbetcher.com"
__plugin_url__ = "https://www.nickbetcher.com/projects/octoprint_dremel3d45"


# Port name for connection dropdown
DREMEL_PORT_NAME = "DREMEL3D45"


if octoprint_plugin is not None:

    class Dremel3D45Plugin(
        octoprint_plugin.StartupPlugin,
        octoprint_plugin.ShutdownPlugin,
        octoprint_plugin.SettingsPlugin,
        octoprint_plugin.SimpleApiPlugin,
        octoprint_plugin.TemplatePlugin,
        octoprint_plugin.AssetPlugin,
    ):
        """OctoPrint plugin for Dremel 3D45 network control."""

        def __init__(self):
            super().__init__()
            self._virtual_serial = None

        # ---------------------------------------------------------------------
        # StartupPlugin
        # ---------------------------------------------------------------------

        def on_startup(self, host: str, port: int) -> None:
            _LOGGER.info("Dremel 3D45 plugin starting up")

        def on_after_startup(self) -> None:
            _LOGGER.info("Dremel 3D45 plugin ready")

            printer_ip = self._settings.get(["printer_ip"])
            if printer_ip:
                _LOGGER.info("Configured printer IP: %s", printer_ip)
                _LOGGER.info(
                    "To connect: Select port '%s' in the connection panel",
                    DREMEL_PORT_NAME,
                )
            else:
                _LOGGER.warning(
                    "No printer IP configured - please configure in settings"
                )

            if self._settings.get_boolean(["camera_enabled"]) and self._settings.get_boolean(
                ["camera_update_global"]
            ):
                self._configure_camera()

        # ---------------------------------------------------------------------
        # ShutdownPlugin
        # ---------------------------------------------------------------------

        def on_shutdown(self) -> None:
            _LOGGER.info("Dremel 3D45 plugin shutting down")
            if self._virtual_serial:
                try:
                    self._virtual_serial.close()
                except Exception:
                    pass
                self._virtual_serial = None

        # ---------------------------------------------------------------------
        # SettingsPlugin
        # ---------------------------------------------------------------------

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

        def on_settings_save(self, data: dict) -> None:
            old_ip = self._settings.get(["printer_ip"])

            # Let OctoPrint persist settings
            super().on_settings_save(data)

            new_ip = self._settings.get(["printer_ip"])
            if old_ip != new_ip:
                _LOGGER.info("Printer IP changed from %s to %s", old_ip, new_ip)

            if self._settings.get_boolean(["camera_enabled"]) and self._settings.get_boolean(
                ["camera_update_global"]
            ):
                self._configure_camera()

        def _configure_camera(self) -> None:
            """Configure OctoPrint's webcam settings for Dremel camera."""
            printer_ip = self._settings.get(["printer_ip"])
            if not printer_ip:
                return

            stream_url = self._settings.get(["camera_stream_url"])
            snapshot_url = self._settings.get(["camera_snapshot_url"])

            if not stream_url:
                stream_url = f"http://{printer_ip}:10123/?action=stream"
            if not snapshot_url:
                snapshot_url = f"http://{printer_ip}:10123/?action=snapshot"

            _LOGGER.info("Configuring global webcam settings: stream=%s", stream_url)

            try:
                from octoprint.settings import settings as octoprint_settings

                s = octoprint_settings()
                s.set(["webcam", "stream"], stream_url)
                s.set(["webcam", "snapshot"], snapshot_url)
                s.set(["webcam", "streamRatio"], "4:3")
                s.save()
            except Exception as e:
                _LOGGER.warning("Failed to update global webcam settings: %s", e)

        # ---------------------------------------------------------------------
        # TemplatePlugin
        # ---------------------------------------------------------------------

        def get_template_configs(self) -> list:
            return [
                {
                    "type": "settings",
                    "name": "Dremel 3D45",
                    "template": "dremel3d45_settings.jinja2",
                    "custom_bindings": True,
                }
            ]

        # ---------------------------------------------------------------------
        # AssetPlugin
        # ---------------------------------------------------------------------

        def get_assets(self) -> dict:
            return {
                "js": ["js/dremel3d45.js"],
                "css": [],
            }

        # ---------------------------------------------------------------------
        # SimpleApiPlugin
        # ---------------------------------------------------------------------

        def get_api_commands(self) -> dict:
            return {
                "clear_sd_index": [],
            }

        def on_api_get(self, request):  # noqa: ANN001
            """Return plugin status information for the settings UI."""
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
            if command != "clear_sd_index":
                return

            from flask import jsonify

            # Clear in-memory (if connected) and on-disk mapping
            if self._virtual_serial:
                try:
                    self._virtual_serial.clear_sd_index()
                except Exception:
                    pass

            try:
                import os

                data_folder = self.get_plugin_data_folder()
                index_file = os.path.join(data_folder, "sd_index.json")
                if os.path.exists(index_file):
                    os.remove(index_file)
            except Exception:
                pass

            return jsonify(ok=True)

        # ---------------------------------------------------------------------
        # Virtual Serial Factory Hook
        # ---------------------------------------------------------------------

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
            if port != DREMEL_PORT_NAME:
                return None

            if not self._settings.get(["printer_ip"]):
                _LOGGER.error(
                    "Cannot connect to %s: No printer IP configured", DREMEL_PORT_NAME
                )
                return None

            _LOGGER.info(
                "Creating Dremel virtual serial connection to %s",
                self._settings.get(["printer_ip"]),
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

            return self._virtual_serial

        def get_additional_port_names(self, *args, **kwargs) -> list:
            """
            Hook: octoprint.comm.transport.serial.additional_port_names

            Called to get additional port names to show in the connection dropdown.
            """
            # Only show the port if we have a printer IP configured
            if self._settings.get(["printer_ip"]):
                return [DREMEL_PORT_NAME]
            return []

        # ---------------------------------------------------------------------
        # SD Card Upload Hook
        # ---------------------------------------------------------------------

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
                sd_upload_failed(
                    filename, path, "Not connected to Dremel printer"
                )
                return

            _LOGGER.info("Uploading file to Dremel: %s", filename)
            sd_upload_started(filename, path)

            try:
                success = self._virtual_serial.upload_file(path, filename)
                if success:
                    sd_upload_succeeded(filename, path)
                else:
                    sd_upload_failed(filename, path, "Upload failed")
            except Exception as e:
                _LOGGER.exception("Upload error: %s", e)
                sd_upload_failed(filename, path, str(e))

    # -------------------------------------------------------------------------
    # Plugin Registration
    # -------------------------------------------------------------------------

    __plugin_implementation__ = Dremel3D45Plugin()

    __plugin_hooks__ = {
        # Virtual serial transport hooks (the proper way!)
        "octoprint.comm.transport.serial.factory": (
            __plugin_implementation__.virtual_serial_factory,
            1,  # Priority: run before default serial factory
        ),
        "octoprint.comm.transport.serial.additional_port_names": (
            __plugin_implementation__.get_additional_port_names,
        ),
        # SD card upload hook
        "octoprint.printer.sdcardupload": (
            __plugin_implementation__.sdcard_upload_hook,
        ),
    }

else:
    __plugin_implementation__ = None
    __plugin_hooks__ = {}

