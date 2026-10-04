"""Constants for the PX IPC (HeroSpeed / Longse platform) integration.

Everything here was verified against a live ``PX_IPC`` running
``KL8_1ND_BVD5L1A0T1Q0_K300036141_V2.0.12.260306_R1``. Where a value is
firmware-specific it is called out in a comment, because guessing these silently
produces "the integration is broken" bug reports.
"""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "px_ipc"
MANUFACTURER: Final = "HeroSpeed"

#: Platforms this integration sets up.
PLATFORMS: Final = [
    Platform.CAMERA,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
]

#: Event-driven binary sensors, keyed by the stable name event payloads are
#: normalised to (see EVENT_TYPES / HTTP_EVENT_TYPES).
EVENT_SENSORS: Final = {
    "motion": "Motion",
    "intrusion": "Region intrusion",
    "illegal_parking": "Illegal parking",
    "line_crossing": "Line crossing",
    "region_entrance": "Region entry",
    "region_exiting": "Region exit",
    "loitering": "Loitering",
    "people_gathering": "People gathering",
    "face_detection": "Face detection",
    "video_tampering": "Video tampering",
    "audio_detection": "Audio detection",
    "license_plate": "License plate",
    "scene_change": "Scene change",
    "item_abandoned": "Abandoned object",
    "item_moved": "Object removed",
    "alarm_input": "Alarm input",
    "video_loss": "Video loss",
}

#: Sensors that stay on for a while after a trigger. The camera reports only the
#: moment of detection, so the integration has to expire them itself — otherwise
#: every automation would have to reset the state by hand.
LATCHED_SENSORS: Final = frozenset(
    {
        "motion",
        "intrusion",
        "illegal_parking",
        "line_crossing",
        "region_entrance",
        "region_exiting",
        "loitering",
        "people_gathering",
        "license_plate",
    }
)

# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

DEFAULT_USERNAME: Final = "admin"
DEFAULT_PORT: Final = 80
DEFAULT_SCAN_INTERVAL: Final = 60

#: The device rejects writes whose body is not labelled as UTF-8. Reads work
#: either way, writes are silently ignored without the charset — verified.
JSON_CONTENT_TYPE: Final = "application/json; charset=utf-8"

#: Sent on every call. The device also expects it on writes.
API_VERSION: Final = "v4.0.0"

LOGIN_CAPABILITIES_PATH: Final = "/api/session/login-capabilities"
LOGIN_PATH: Final = "/api/session/login"
LOGOUT_PATH: Final = "/api/session/logout"
HEARTBEAT_PATH: Final = "/api/session/heart-beat"

DEVICE_INFO_PATH: Final = "/api/system/device-info"
CAPABILITY_PATH: Final = "/api/system/capability"
SNAPSHOT_PATH: Final = "/api/picture/snapshot"

#: Two image endpoints, two different variable sets. WDR only responds on the
#: legacy one — the modern one accepts the write, answers code 0 and changes
#: nothing. Verified on the device, so we keep both.
IMAGE_PARAM_PATH: Final = "/api/image/image-param"
IMAGE_LEGACY_PATH: Final = "/api/image/image"

DAY_NIGHT_PATH: Final = "/api/event/face-detect"  # reserved; see select.py notes
MOTION_PATH: Final = "/api/event/motion"
INTRUSION_PATH: Final = "/api/event/intrusion"
ILLEGAL_PARKING_PATH: Final = "/api/event/illegal-parking"
LICENSE_PLATE_PATH: Final = "/api/event/license-plate"

ALARM_SERVER_PATH: Final = "/api/network/alarm-server"

#: Real-time event stream. The camera is an IPC, so ``channel`` is omitted.
EVENTS_WS_PATH: Final = "/events"

#: RTSP path template. The device serves ``/h264/ch1/{main,sub}/av_stream``;
#: Hikvision-style ``/Streaming/Channels/…`` answers 401 for Basic auth.
RTSP_MAIN: Final = "/h264/ch1/main/av_stream"
RTSP_SUB: Final = "/h264/ch1/sub/av_stream"

# --------------------------------------------------------------------------- #
# Device enums (from /api/image/image-param)
# --------------------------------------------------------------------------- #

DAY_NIGHT_MODES: Final = {0: "auto", 1: "color", 2: "black_and_white", 3: "schedule"}
DAY_NIGHT_MODES_REVERSE: Final = {v: k for k, v in DAY_NIGHT_MODES.items()}

ILLUMINATOR_MODES: Final = {0: "warm_light", 1: "infrared", 2: "intelligent"}
ILLUMINATOR_MODES_REVERSE: Final = {v: k for k, v in ILLUMINATOR_MODES.items()}

WDR_LEVELS: Final = {0: "off", 1: "low", 2: "medium", 3: "high"}
WDR_LEVELS_REVERSE: Final = {v: k for k, v in WDR_LEVELS.items()}

#: Noise reduction, same scale shape as WDR.
DNR_LEVELS: Final = {0: "off", 1: "low", 2: "medium", 3: "high"}
DNR_LEVELS_REVERSE: Final = {v: k for k, v in DNR_LEVELS.items()}

#: Anti-flicker: off, then grades 1-10. Visible in the vendor UI as a bare
#: list of grades, so the names here mirror it rather than inventing meaning.
ANTI_FLICKER_LEVELS: Final = {0: "off", **{n: f"grade_{n}" for n in range(1, 11)}}
ANTI_FLICKER_LEVELS_REVERSE: Final = {v: k for k, v in ANTI_FLICKER_LEVELS.items()}

# --------------------------------------------------------------------------- #
# Event WebSocket protocol (vendor SDK 7.14.10)
# --------------------------------------------------------------------------- #

#: 32-byte global header, little-endian, signature 0xa5a5 at offset 30.
PACKET_HEADER_SIZE: Final = 32
PACKET_SIGNATURE: Final = 0xA5A5

PKT_HEARTBEAT: Final = 1
PKT_EVENT: Final = 2
PKT_FACE: Final = 3
PKT_LINE_CROSS_COUNT: Final = 4
PKT_SMART_OBJECT: Final = 5
PKT_LICENSE_PLATE: Final = 6
PKT_HEATMAP: Final = 7
PKT_EPTZ: Final = 8
PKT_CONFIG: Final = 9
PKT_OBJECT_INFO: Final = 10

PACKET_NAMES: Final = {
    PKT_HEARTBEAT: "heartbeat",
    PKT_EVENT: "event",
    PKT_FACE: "face",
    PKT_LINE_CROSS_COUNT: "line_crossing_count",
    PKT_SMART_OBJECT: "smart_object",
    PKT_LICENSE_PLATE: "license_plate",
    PKT_HEATMAP: "heatmap",
    PKT_EPTZ: "eptz",
    PKT_CONFIG: "config",
    PKT_OBJECT_INFO: "object_info",
}

#: Which packet types we ask for. Heartbeats are always delivered.
SUBSCRIBED_PACKETS: Final = [
    PKT_EVENT,
    PKT_FACE,
    PKT_LINE_CROSS_COUNT,
    PKT_SMART_OBJECT,
    PKT_LICENSE_PLATE,
    PKT_HEATMAP,
    PKT_OBJECT_INFO,
]

#: Event strings from the SDK's "Event Type Strings" table, mapped to a stable
#: entity key. Unknown strings are surfaced as-is in the "last event" sensor
#: rather than dropped.
EVENT_TYPES: Final = {
    "motion": "motion",
    "video_tempering": "video_tampering",
    "audio_detection": "audio_detection",
    "intrusion": "intrusion",
    "region_entrance": "region_entrance",
    "region_exiting": "region_exiting",
    "line_crossing": "line_crossing",
    "line_crossing_count": "line_crossing_count",
    "loitering": "loitering",
    "people_gathering": "people_gathering",
    "face_detection": "face_detection",
    "plate_detection": "license_plate",
    "illegal_parking": "illegal_parking",
    "face_matched": "face_matched",
    "face_stranger": "face_stranger",
    "scene_change": "scene_change",
    "item_abandoned": "item_abandoned",
    "item_moved": "item_moved",
    "fire_detection": "fire_detection",
    "alarm_input": "alarm_input",
    "video_loss": "video_loss",
    "disk_loss": "disk_loss",
    "disk_full": "disk_full",
    "disk_abnormality": "disk_abnormality",
    "net_disconnect": "net_disconnect",
    "ip_conflict": "ip_conflict",
}

#: The camera's own HTTP alarm notification uses these eventType strings; they
#: differ from the WebSocket ones above.
HTTP_EVENT_TYPES: Final = {
    "motionDetection": "motion",
    "videoTampering": "video_tampering",
    "audioInDetection": "audio_detection",
    "intrusionDetection": "intrusion",
    "regionEntranceDetection": "region_entrance",
    "regionExitingDetection": "region_exiting",
    "lineCrossDetection": "line_crossing",
    "loiteringDetection": "loitering",
    "peopleGatherDetection": "people_gathering",
    "faceDetection": "face_detection",
    "illegalParkingDetection": "illegal_parking",
    "crossBorderDetection": "line_crossing",
}

#: How long an event-driven binary sensor stays "on" after the last trigger.
#: The camera emits a single "active" alert per occurrence, so something has to
#: expire it; a short window keeps automations usable without a reset call.
EVENT_HOLD_SECONDS: Final = 30

CONF_HOST: Final = "host"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_PORT: Final = "port"
CONF_STREAM: Final = "stream"
CONF_VERIFY_SSL: Final = "verify_ssl"

STREAM_MAIN: Final = "main"
STREAM_SUB: Final = "sub"
