#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
dremel3dpy by Gustavo Stor - A Dremel 3D Printer Python library.

https://github.com/godely/dremel3dpy

Published under the MIT license - See LICENSE file for more details.

This library supports the three Dremel models: 3D20, 3D40 and 3D45.

"Dremel" is a trademark owned by Bosch, see www.dremel.com for
more information. I am in no way affiliated with Dremel.
"""

import json
import logging
import os
import random
import re
import string
import threading
from typing import Any, Dict

import requests
import validators
from urllib.parse import urlunsplit

from .helpers.constants import (
    _LOGGER,
    AVAILABLE_STORAGE,
    CAMERA_PORT,
    CANCEL_COMMAND,
    CHAMBER_TEMPERATURE,
    COMMAND_PATH,
    COMMAND_PORT,
    CONF_API_VERSION,
    CONF_CONNECTION_TYPE,
    CONF_ETHERNET_CONNECTED,
    CONF_ETHERNET_IP,
    CONF_FIRMWARE_VERSION,
    CONF_HOST,
    CONF_MACHINE_TYPE,
    CONF_MODEL,
    CONF_SERIAL_NUMBER,
    CONF_TITLE,
    CONF_WIFI_CONNECTED,
    CONF_WIFI_IP,
    DOOR_OPEN,
    DREMEL_MANUFACTURER,
    ELAPSED_TIME,
    ERROR_CODE,
    ERROR_CODE,
    ESTIMATED_TOTAL_TIME,
    EXTRA_STATUS_PORT,
    EXTRUDER_TARGET_TEMPERATURE,
    EXTRUDER_TEMPERATURE,
    EXTRUDER_TEMPERATURE_RANGE,
    FAN_SPEED,
    FILAMENT,
    HOME_MESSAGE_PATH,
    JOB_NAME,
    JOB_STATUS,
    LAYER,
    NETWORK_BUILD,
    PAUSE_COMMAND,
    PLATFORM_TARGET_TEMPERATURE,
    PLATFORM_TEMPERATURE,
    PLATFORM_TEMPERATURE_RANGE,
    PRINT_COMMAND,
    PRINT_FILE_UPLOADS,
    PRINTER_INFO_COMMAND,
    PRINTER_STATUS_COMMAND,
    PROGRESS,
    REMAINING_TIME,
    REQUEST_TIMEOUT,
    RESUME_COMMAND,
    STATS_FILAMENT_USED,
    STATS_FILE_NAME,
    STATS_LAYER_HEIGHT,
    STATS_SOFTWARE,
    STATUS,
    USAGE_COUNTER,
)


_THREAD_LOCAL_SESSION = threading.local()


def _get_thread_session() -> requests.Session:
    """Return a per-thread HTTP session for connection reuse."""
    session = getattr(_THREAD_LOCAL_SESSION, "session", None)
    if session is None:
        session = requests.Session()
        _THREAD_LOCAL_SESSION.session = session
    return session


def _reset_thread_session() -> requests.Session:
    """Reset and return the current thread's HTTP session."""
    session = getattr(_THREAD_LOCAL_SESSION, "session", None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass

    session = requests.Session()
    _THREAD_LOCAL_SESSION.session = session
    return session


class Dremel3DPrinter:
    """Main Dremel 3D Printer class."""

    def __init__(self, host: str) -> None:
        """Init a Dremel 3D Printer instance"""
        self._host = host
        self._printer_info = None
        self._job_status = None
        self._printer_extra_stats = None
        self._total_time = 0
        self._is_printing = False
        self._is_building = False
        self._is_calibrating = False
        self._is_starting = False
        self._is_heating = False
        self._is_finished = False

    def set_printer_info(self, refresh=False):
        """Return attributes related to the printer."""
        if refresh or self._printer_info is None:
            try:
                printer_info = default_request(self._host, PRINTER_INFO_COMMAND)
            except RuntimeError as exc:
                self._printer_info = None
                raise exc
            else:
                title = None
                model = None
                machine_type = printer_info.get(CONF_MACHINE_TYPE, "Dremel 3D45")
                try:
                    title = re.search(
                        r"DREMEL [^\s+]+", machine_type
                    ).group(0)
                except Exception:
                    title = machine_type
                try:
                    model = re.search(
                        r"DREMEL ([^\s+]+)", machine_type
                    ).group(1)
                except Exception:
                    model = "3D45"
                is_eth = printer_info.get(CONF_ETHERNET_CONNECTED) == 1
                self._printer_info = {
                    CONF_HOST: self._host,
                    CONF_API_VERSION: printer_info.get(CONF_API_VERSION, ""),
                    CONF_CONNECTION_TYPE: "eth0" if is_eth else "wlan",
                    CONF_ETHERNET_IP: printer_info.get(CONF_ETHERNET_IP, "n-a")
                    if is_eth
                    else "n-a",
                    CONF_FIRMWARE_VERSION: printer_info.get(CONF_FIRMWARE_VERSION, "Unknown"),
                    CONF_MACHINE_TYPE: machine_type,
                    CONF_MODEL: model,
                    CONF_SERIAL_NUMBER: printer_info.get(CONF_SERIAL_NUMBER, "Unknown"),
                    CONF_TITLE: title,
                    CONF_WIFI_IP: printer_info.get(CONF_WIFI_IP, "n-a")
                    if printer_info.get(CONF_WIFI_CONNECTED) == 1
                    else "n-a",
                }

    def set_job_status(self, refresh=False):
        """Return stats related to the printer and the printing job."""
        if refresh or self._job_status is None:
            try:
                last_printing_status = (
                    self.get_printing_status()
                    if self._job_status is not None
                    else "idle"
                )
                job_status = default_request(self._host, PRINTER_STATUS_COMMAND)
                raw_job_name = job_status.get(JOB_NAME[0], "")
                job_name_match = re.search(
                    r"(.*?)(\.[^\.]*)?$", raw_job_name
                )
                job_name = job_name_match.group(1) if job_name_match else raw_job_name
            except RuntimeError as exc:
                self._job_status = None
                raise exc
            else:
                mapped_status = {
                    "": "idle",
                    "abort": "abort",
                    "building": "building",
                    "completed": "completed",
                    "paused": "paused",
                    "pausing": "pausing",
                    "preparing": "preparing",
                    "resuming": "resuming",
                    "!pausing": "paused",
                    "!resuming": "resuming",
                }
                # Use .get() with sensible defaults for ALL fields so that
                # a missing key in the API response does not crash the
                # entire method with a KeyError.
                raw_job_status = job_status.get(JOB_STATUS[0], "")
                self._job_status = {
                    DOOR_OPEN[1]: job_status.get(DOOR_OPEN[0], 0),
                    CHAMBER_TEMPERATURE[1]: job_status.get(CHAMBER_TEMPERATURE[0], 0),
                    ELAPSED_TIME[1]: job_status.get(ELAPSED_TIME[0], 0),
                    REMAINING_TIME[1]: job_status.get(REMAINING_TIME[0], 0),
                    ESTIMATED_TOTAL_TIME[1]: job_status.get(ESTIMATED_TOTAL_TIME[0], 0),
                    EXTRUDER_TEMPERATURE[1]: job_status.get(EXTRUDER_TEMPERATURE[0], 0),
                    EXTRUDER_TARGET_TEMPERATURE[1]: job_status.get(
                        EXTRUDER_TARGET_TEMPERATURE[0], 0
                    ),
                    FAN_SPEED[1]: job_status.get(FAN_SPEED[0], 0),
                    FILAMENT[1]: job_status.get(FILAMENT[0], ""),
                    JOB_STATUS[1]: mapped_status.get(raw_job_status, "unknown"),
                    JOB_NAME[1]: job_name,
                    LAYER[1]: job_status.get(LAYER[0], 0),
                    NETWORK_BUILD[1]: job_status.get(NETWORK_BUILD[0], 0),
                    PLATFORM_TARGET_TEMPERATURE[1]: job_status.get(
                        PLATFORM_TARGET_TEMPERATURE[0], 0
                    ),
                    PLATFORM_TEMPERATURE[1]: job_status.get(PLATFORM_TEMPERATURE[0], 0),
                    PROGRESS[1]: job_status.get(PROGRESS[0], 0),
                    STATUS[1]: job_status.get(STATUS[0], ""),
                }
                current_printing_status = self.get_printing_status()
                if last_printing_status != current_printing_status:
                    self._is_printing = False
                    self._is_building = False
                    self._is_calibrating = False
                    self._is_starting = False
                    self._is_heating = False
                    self._is_finished = False
                    if current_printing_status == "building":
                        self._is_printing = True
                        self._is_building = True
                    elif (
                        current_printing_status == "resuming"
                        or current_printing_status == "paused"
                        or current_printing_status == "pausing"
                    ):
                        self._is_printing = True
                    elif current_printing_status == "preparing":
                        self._is_printing = True
                        if last_printing_status == "completed":
                            self._is_heating = True
                        else:
                            self._is_calibrating = True
                    elif (
                        last_printing_status == "completed"
                        and current_printing_status == "idle"
                    ):
                        self._is_printing = True
                        self._is_starting = True
                    elif (
                        last_printing_status == "preparing"
                        and current_printing_status == "idle"
                    ):
                        self._is_printing = True
                        self._is_heating = True
                    elif (
                        last_printing_status == "building"
                        and current_printing_status == "completed"
                    ):
                        self._is_finished = True
                    info_msg = f"Printer changed its phase from {last_printing_status} to {current_printing_status}."
                    _LOGGER.info(info_msg)
                    last_printing_status = current_printing_status
            # Patch fix the total time. Sometimes when in a printing job this API can
            # keep returning a total time of 0 but an actual estimated remaining time.
            # Every time we call this API, if total_time is still not set, we check to
            # see if the API returned a correct value for the estimated total time and
            # use that as source of truth. Otherwise, we check to see if we the API
            # returned at least a non-zero value for remaining time. The first time this
            # happens we get the value of remaining time and use it as total_time and
            # do not change it again.
            if self._total_time == 0:
                total_times = [0]
                if (total_time := self._job_status[ESTIMATED_TOTAL_TIME[1]]) > 0:
                    total_times += [total_time]
                if (total_time := self._job_status[REMAINING_TIME[1]]) > 0:
                    total_times += [self._job_status[REMAINING_TIME[1]]]
                    if (elapsed_time := self._job_status[ELAPSED_TIME[1]]) > 0:
                        total_times += [total_time + elapsed_time]
                if (total_time := max(total_times)) > self._total_time:
                    self._total_time = total_time

    def set_extra_status(self, refresh=False):
        """Return extra status that we grab from the Dremel webpage API."""
        if refresh or self._printer_extra_stats is None:
            try:
                extra_status = default_request(
                    self._host,
                    scheme="https",
                    port=EXTRA_STATUS_PORT,
                    path=HOME_MESSAGE_PATH,
                )
            except RuntimeError as exc:
                self._printer_extra_stats = None
                raise exc
            else:
                max_platform_temperature = 0
                max_extruder_temperature = 0
                try:
                    m = re.search(
                        r"0-(\d+)", extra_status.get(PLATFORM_TEMPERATURE_RANGE[0], "")
                    )
                    if m:
                        max_platform_temperature = m.group(1)
                except Exception:
                    pass
                try:
                    m = re.search(
                        r"0-(\d+)", extra_status.get(EXTRUDER_TEMPERATURE_RANGE[0], "")
                    )
                    if m:
                        max_extruder_temperature = m.group(1)
                except Exception:
                    pass
                self._printer_extra_stats = {
                    AVAILABLE_STORAGE[1]: extra_status.get(AVAILABLE_STORAGE[0], ""),
                    EXTRUDER_TEMPERATURE_RANGE[1]: max_extruder_temperature,
                    PLATFORM_TEMPERATURE_RANGE[1]: max_platform_temperature,
                    USAGE_COUNTER[1]: extra_status.get(USAGE_COUNTER[0], 0),
                }

    def refresh(self) -> None:
        """Do a full refresh of all API calls."""
        try:
            self.set_printer_info(refresh=True)
            self.set_job_status(refresh=True)
            self.set_extra_status(refresh=True)
        except RuntimeError as exc:
            _LOGGER.exception(str(exc))

    def get_printer_info(self) -> Dict[str, Any]:
        return (self._printer_info or {}) | (self._printer_extra_stats or {})

    def get_job_status(self) -> Dict[str, Any]:
        return self._job_status or {}

    def get_manufacturer(self) -> str:
        return DREMEL_MANUFACTURER

    def get_model(self) -> str:
        return self.get_printer_info().get(CONF_MODEL)

    def get_title(self) -> str:
        return self.get_printer_info().get(CONF_TITLE)

    def get_firmware_version(self) -> str:
        return self.get_printer_info().get(CONF_FIRMWARE_VERSION)

    def get_job_name(self) -> str:
        return self.get_job_status().get(JOB_NAME[1])

    def get_remaining_time(self) -> int:
        return self.get_job_status().get(REMAINING_TIME[1])

    def get_elapsed_time(self) -> int:
        return self.get_job_status().get(ELAPSED_TIME[1])

    def get_total_time(self) -> int:
        return self.get_elapsed_time() + self.get_remaining_time()

    def get_filament(self) -> str:
        return self.get_job_status().get(FILAMENT[1])

    def get_layer(self) -> int:
        """Return the current print layer number."""
        return self.get_job_status().get(LAYER[1], 0)

    def is_busy(self) -> bool:
        return self.get_job_status().get(STATUS[1]) == "busy"

    def is_ready(self) -> bool:
        return self.get_job_status().get(STATUS[1]) == "ready"

    def is_printing(self) -> bool:
        return self._is_printing

    def is_finished(self) -> bool:
        return self._is_finished

    def is_heating(self) -> bool:
        return self._is_heating

    def is_calibrating(self) -> bool:
        return self._is_calibrating

    def is_starting(self) -> bool:
        return self._is_starting

    def is_not_printing(self) -> bool:
        return not self.is_printing()

    def is_completed(self) -> bool:
        return self._is_finished

    def is_paused(self) -> bool:
        return self.get_printing_status() == "paused"

    def is_pausing(self) -> bool:
        return self.get_printing_status() == "pausing"

    def is_aborted(self) -> bool:
        return self.get_printing_status() == "aborted"

    def is_running(self) -> bool:
        return self.is_printing() and not self.is_paused() and not self.is_pausing()

    def is_building(self) -> bool:
        return (
            self._is_building
            and self.get_total_time() > 0
            # This function is a maybe because there were times the initial calls to the API failed
            # and the target temperature was always zero. A better solution in the future is use code
            # that we already created to check if the platform/extruder temperatures are not moving.
            and self.are_temperatures_maybe_within_target_range()
            # Maybe change it to: self._is_building or (self._is_idle and self.get_total_temp() > 0 and self.are_temp...)
        )

    def is_door_open(self) -> bool:
        return self.get_job_status().get(DOOR_OPEN[1]) == 1

    def get_stream_url(self) -> str:
        return f"http://{self._host}:{CAMERA_PORT}/?action=stream"

    def get_snapshot_url(self) -> str:
        return f"http://{self._host}:{CAMERA_PORT}/?action=snapshot"

    def get_serial_number(self) -> str:
        return self.get_printer_info().get(CONF_SERIAL_NUMBER)

    def get_printing_status(self) -> str:
        return self.get_job_status().get(JOB_STATUS[1])

    def get_printing_progress(self) -> float:
        return self.get_job_status().get(PROGRESS[1])

    def get_temperature_type(self, temp_type: str) -> int:
        return self.get_job_status().get(f"{temp_type}_temperature")

    def get_temperature_attributes(self, temp_type: str) -> Dict[str, int]:
        max_temp_raw = self.get_printer_info().get(f"{temp_type}_max_temperature")
        return {
            "target_temp": self.get_job_status().get(f"{temp_type}_target_temperature"),
            "max_temp": int(max_temp_raw) if max_temp_raw is not None else 0,
        }

    def is_maybe_temperature_within_target_range(self, temp_type) -> bool:
        temperature = self.get_temperature_type(temp_type)
        target_temperature = self.get_temperature_attributes(temp_type)["target_temp"]
        if target_temperature == 0:
            return True
        return temperature in range(target_temperature - 2, target_temperature + 3)

    def are_temperatures_maybe_within_target_range(self) -> bool:
        return all(
            [
                self.is_maybe_temperature_within_target_range(temp_type)
                for temp_type in ["platform", "extruder"]
            ]
        )

    def _upload_print(self, file) -> str:
        try:
            filename = (
                "".join(random.choice(string.ascii_letters) for i in range(10))
                + ".gcode"
            )
            response = requests.post(
                f"http://{self._host}{PRINT_FILE_UPLOADS}",
                files={"print_file": (filename, file)},
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as exc:  # pylint: disable=broad-except
            raise exc
        if response.status_code != 200:
            raise RuntimeError(f"Upload failed with status code {response.status_code}")

        return filename

    def _get_print_stats(self, filename: str, data: str) -> Dict[str, str]:
        filament_used = (
            f"{match.group(1)}m"
            if (match := re.search("Filament used: ([0-9.]+)", data)) is not None
            else ""
        )
        layer_height = (
            f"{match.group(1)}mm"
            if (match := re.search("Layer height: ([0-9.]+)", data)) is not None
            else ""
        )
        software = (
            match.group(1)
            if (match := re.search("Generated with (.+)", data)) is not None
            else ""
        )
        return {
            STATS_FILAMENT_USED: filament_used,
            STATS_FILE_NAME: filename,
            STATS_LAYER_HEIGHT: layer_height,
            STATS_SOFTWARE: software,
        }

    def start_print_from_file(self, filepath: str) -> Dict[str, str]:
        """
        Uploads a file to the printer, so it can start a print job. This file is local.
        """
        if (
            filepath is not None
            and os.path.isfile(filepath)
            and filepath.lower().endswith(".gcode")
        ):
            file = open(filepath, "rb")
            data = file.read().decode("utf-8")
        else:
            raise RuntimeError(
                "File path must be defined and point to a valid .gcode file."
            )
        filename = self._upload_print(data)
        try:
            default_request(self._host, {PRINT_COMMAND: filename})
            return self._get_print_stats(filename, data)
        except RuntimeError as exc:
            _LOGGER.exception(str(exc))

    def start_print_from_url(self, url: str) -> Dict[str, str]:
        """
        Uploads a file to the printer, so it can start a print job. This file is fetched from an URL.
        """
        if url is not None:
            try:
                if validators.url(url) is True:
                    request = requests.get(url, timeout=REQUEST_TIMEOUT)
                elif validators.url(f"https://{url}") is True:
                    try:
                        request = requests.get(
                            f"https://{url}", timeout=REQUEST_TIMEOUT
                        )
                    except requests.exceptions.SSLError:
                        request = requests.get(f"http://{url}", timeout=REQUEST_TIMEOUT)
                else:
                    raise RuntimeError("Invalid URL format")
                if request.status_code != 200:
                    raise RuntimeError(
                        f"URL returned status code {request.status_code}"
                    )
                file = request.content
                data = file.decode("utf-8")
            except requests.exceptions.ConnectionError as exc:
                raise exc
            except Exception as exc:  # pylint: disable=broad-except
                raise exc
        else:
            raise RuntimeError("URL must be defined and be a valid gcode file")
        filename = self._upload_print(data)
        try:
            default_request(self._host, {PRINT_COMMAND: filename})
            return self._get_print_stats(filename, data)
        except RuntimeError as exc:
            _LOGGER.exception(str(exc))

    def resume_print(self) -> Dict[str, Any]:
        """Resumes a print job."""
        return default_request(self._host, RESUME_COMMAND)[ERROR_CODE] == 200

    def pause_print(self) -> Dict[str, Any]:
        """Pauses a print job."""
        return default_request(self._host, PAUSE_COMMAND)[ERROR_CODE] == 200

    def stop_print(self) -> Dict[str, Any]:
        """Stops a print job."""
        return default_request(self._host, CANCEL_COMMAND)[ERROR_CODE] == 200


def default_request(
    host, command="", scheme="http", port=COMMAND_PORT, path=COMMAND_PATH
) -> Dict[str, Any]:
    """Performs a default request to the Dremel 3D Printer APIs."""
    netloc = f"{host}:{port}" if port else str(host)
    url = urlunsplit((scheme, netloc, path, "", ""))

    try:
        # IMPORTANT: Dremel printer HTTPS endpoints commonly use invalid/expired
        # certificates in the field. DO NOT enable certificate verification for
        # printer API calls, or connectivity will fail for many real devices.
        response = _get_thread_session().post(
            url, data=command, timeout=REQUEST_TIMEOUT, verify=False
        )
    except requests.RequestException as exc:
        # A cached keep-alive socket can go stale between polls; retry once
        # with a fresh session for the current thread.
        _LOGGER.debug("Request failed for %s, retrying once with fresh session: %s", url, exc)
        try:
            response = _reset_thread_session().post(
                url, data=command, timeout=REQUEST_TIMEOUT, verify=False
            )
        except requests.RequestException as retry_exc:
            raise RuntimeError(f"Request failed for {url}: {retry_exc}") from retry_exc

    response_json: Dict[str, Any] = {}
    try:
        payload = response.content.decode("utf-8")
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            response_json = parsed
    except Exception:
        response_json = {}

    if response.status_code != 200:
        message = response_json.get("message") or response.text[:200]
        raise RuntimeError(
            f"HTTP {response.status_code} from {url} (content-type={response.headers.get('Content-Type')}): {message}"
        )

    # Dremel command endpoints commonly return HTTP 200 with an API-level
    # error payload. Treat non-200 API error_code values as failures so callers
    # do not silently report success.
    api_error_code = response_json.get(ERROR_CODE)
    if api_error_code is not None:
        try:
            code = int(api_error_code)
        except (TypeError, ValueError):
            code = None
        if code is None or code != 200:
            message = response_json.get("message") or "Unknown API error"
            raise RuntimeError(
                f"Dremel API error from {url}: error_code={api_error_code}, message={message}"
            )

    return response_json
