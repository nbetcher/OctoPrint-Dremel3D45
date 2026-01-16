"""Constants for the Dremel 3D Printer (3D20, 3D40, 3D45) integration."""

import logging
import signal

_LOGGER = logging.getLogger(__name__)

MAJOR_VERSION = 2
MINOR_VERSION = 1
PATCH_VERSION = "1"

__version__ = f"{MAJOR_VERSION}.{MINOR_VERSION}.{PATCH_VERSION}"

PROJECT_NAME = "dremel3dpy"
PROJECT_PACKAGE_NAME = "dremel3dpy"
PROJECT_AUTHOR = "Gustavo Stor"
PROJECT_EMAIL = "gus@storhub.io"
PROJECT_COPYRIGHT = f" 2022, {PROJECT_AUTHOR}"
PROJECT_LICENSE = "MIT"
PROJECT_URL = "https://github.com/godely/dremel3dpy"
PROJECT_DESCRIPTION = "A Dremel 3D Printer Python Library running on Python 3"
PROJECT_LONG_DESCRIPTION = (
    "API for grabbing 3D Printer statistics "
    "and remote controlling a 3D20, 3D40 or "
    "3D45 model for operations such as resuming "
    "a print, pausing, canceling or starting a "
    "new print from either a file path or a URL."
)
PROJECT_CLASSIFIERS = [
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 3.9",
    "Topic :: Home Automation",
]
PROJECT_GITHUB_USERNAME = "godely"
PROJECT_GITHUB_REPOSITORY = "dremel3dpy"
PROJECT_KEYWORDS = ["python", "dremel", "3d", "printer", "3d-printer"]

PYPI_URL = f"https://pypi.python.org/pypi/{PROJECT_PACKAGE_NAME}"

SERVICE_PRINT_JOB = "print_job"
SERVICE_PAUSE_JOB = "pause_job"
SERVICE_RESUME_JOB = "resume_job"
SERVICE_STOP_JOB = "stop_job"
ATTR_FILEPATH = "file_path"
ATTR_URL = "url"
ATTR_DEVICE_ID = "device_id"

COMMAND_PORT = 80
CAMERA_PORT = 10123
EXTRA_STATUS_PORT = 11134

EXIT_SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)

COMMAND_PATH = "/command"
HOME_MESSAGE_PATH = "/getHomeMessage"
PRINT_FILE_UPLOADS = "/print_file_uploads"

PRINTER_STATUS_COMMAND = "GETPRINTERSTATUS"
PRINTER_INFO_COMMAND = "GETPRINTERINFO"
PRINT_COMMAND = "PRINT"
RESUME_COMMAND = "RESUME"
PAUSE_COMMAND = "PAUSE"
CANCEL_COMMAND = "CANCEL"

STATS_FILE_NAME = "file_name"
STATS_FILAMENT_USED = "filament_used"
STATS_LAYER_HEIGHT = "layer_height"
STATS_SOFTWARE = "software"

EVENT_DATA_NEW_PRINT_STATS = "dremel_3d_printer_new_print_stats"

REQUEST_TIMEOUT = 30
REFRESH_API_INTERVAL = 5
REFRESH_SLEEP_SECONDS = 1

DEFAULT_FPS = 5
DEFAULT_INITIAL_PREPARING_PERIOD = 20 * 60
DEFAULT_POLLING_INTERVAL_UNTIL_START = 5
DEFAULT_UPDATE_JOB_STATUS_INTERVAL = 1
DEFAULT_SCALE_PERCENT = 1
DEFAULT_MAX_SIZE_MB = 10.0
DEFAULT_TOTAL_DURATION = 15
DEFAULT_FINAL_GRACE_PERIOD = 30
DEFAULT_ADDITIONAL_OUTPUT_SIZE_PERCENTAGE = 0.2

FRAME_SCALING_FACTOR = 1
FRAME_RECTANGLE_MARGIN = 10
FRAME_RECTANGLE_PADDING = 10
FRAME_TEXT_VERTICAL_PADDING = 5
FRAME_RECTANGLE_OPACITY = 0.7
FRAME_RECTANGLE_COLOR = (255, 255, 255)
FRAME_TEXT_FONT_SCALE = 0.6
FRAME_TEXT_THICKNESS = 2
FRAME_TEXT_COLOR = (0, 0, 0)

DREMEL_MANUFACTURER = "Dremel"

MESSAGE = "message"
ERROR_CODE = "error_code"

CONF_HOST = "host"
CONF_TITLE = "title"
CONF_MODEL = "model"
CONF_SERIAL_NUMBER = "SN"
CONF_API_VERSION = "api_version"
CONF_FIRMWARE_VERSION = "firmware_version"
CONF_MACHINE_TYPE = "machine_type"
CONF_WIFI_IP = "wifi_ip"
CONF_WIFI_CONNECTED = "wifi_connected"
CONF_ETHERNET_IP = "ethernet_ip"
CONF_ETHERNET_CONNECTED = "ethernet_connected"
CONF_CONNECTION_TYPE = "connection_type"

ELAPSED_TIME = ["elaspedtime", "elapsed_time"]
ESTIMATED_TOTAL_TIME = ["totalTime", "estimated_total_time"]
REMAINING_TIME = ["remaining", "remaining_time"]
PROGRESS = ["progress", "progress"]
STATUS = ["status", "current_status"]
DOOR_OPEN = ["door_open", "door_open"]
FILAMENT = ["filament_type ", "filament"]
FAN_SPEED = ["fanSpeed", "fan_speed"]
CHAMBER_TEMPERATURE = ["chamber_temperature", "chamber_temperature"]
PLATFORM_TEMPERATURE = ["platform_temperature", "platform_temperature"]
PLATFORM_TARGET_TEMPERATURE = [
    "buildPlate_target_temperature",
    "platform_target_temperature",
]
EXTRUDER_TEMPERATURE = ["temperature", "extruder_temperature"]
EXTRUDER_TARGET_TEMPERATURE = [
    "extruder_target_temperature",
    "extruder_target_temperature",
]
JOB_STATUS = [
    "jobstatus",
    "job_status",
]
JOB_NAME = ["jobname", "job_name"]
NETWORK_BUILD = ["networkBuild", "network_build"]
LAYER = ["layer", "layer"]

USAGE_COUNTER = ["UsageCounter", "hours_used"]
AVAILABLE_STORAGE = ["PrintererAvailabelStorage", "available_storage"]
PLATFORM_TEMPERATURE_RANGE = ["PrinterBedMessage", "platform_max_temperature"]
EXTRUDER_TEMPERATURE_RANGE = ["PrinterNozzleMessage", "extruder_max_temperature"]
