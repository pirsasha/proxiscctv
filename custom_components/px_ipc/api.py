"""Asynchronous client for PX IPC cameras (HeroSpeed / Longse platform).

The device speaks a JSON API whose authentication is a challenge-response dance,
not HTTP Basic. Verified against a live ``PX_IPC``:

1. ``POST /api/session/login-capabilities`` with the plain username returns a
   per-session ``challenge``, a per-boot ``salt`` and a ``sessionID``.
2. The client builds ``sha256(username + salt + base64(local_datetime) + password)``,
   turns that hex digest into raw bytes, appends the challenge and hashes again.
3. ``POST /api/session/login`` with that digest answers a ``sessionID`` cookie
   which every later call must carry.

Three details are easy to get wrong and are handled explicitly here:

* **Local time, not UTC.** The device validates the timestamp field; sending UTC
  fails whenever the camera is not on UTC.
* **``charset=utf-8`` is mandatory on writes.** Without it the device answers
  ``code: 0`` and silently ignores the change — reads are unaffected, which makes
  the bug look like "the integration does not save".
* **``code: 101`` means the session died** (it happens after every camera reboot).
  The client re-authenticates once and retries.

The event stream is a WebSocket of 32-byte headers + ``\\0``-terminated JSON +
an optional binary segment. Packet 6 is the license plate message and carries
``license_plate_list[].license_plate_num``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import aiohttp

from .const import (
    API_VERSION,
    DEVICE_INFO_PATH,
    EVENTS_WS_PATH,
    HEARTBEAT_PATH,
    IMAGE_LEGACY_PATH,
    IMAGE_PARAM_PATH,
    JSON_CONTENT_TYPE,
    LOGIN_CAPABILITIES_PATH,
    LOGIN_PATH,
    PACKET_HEADER_SIZE,
    PACKET_SIGNATURE,
    SNAPSHOT_PATH,
    SUBSCRIBED_PACKETS,
)

_LOGGER = logging.getLogger(__name__)

#: The session cookie name used by the device.
COOKIE_NAME = "sessionID"

#: Longest we wait for any single HTTP call.
DEFAULT_TIMEOUT = 15
#: The camera closes an idle WebSocket without warning; reconnect after this.
WS_RECONNECT_DELAY = 10


class PxIpcError(Exception):
    """Base class for every failure raised by this client."""


class PxIpcConnectionError(PxIpcError):
    """The device could not be reached."""


class PxIpcAuthError(PxIpcError):
    """The device rejected the credentials."""


class PxIpcResponseError(PxIpcError):
    """The device answered, but with an error code."""


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without a camera)
# --------------------------------------------------------------------------- #


def datetime_string(when: datetime | None = None) -> str:
    """Local time in the format the device expects: ``YYYY-MM-DDTHH:MM:SS``."""
    return (when or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")


def build_login_digest(
    username: str, salt: str, challenge: str, password: str, when: datetime | None = None
) -> str:
    """Return the ``password`` field for ``/api/session/login``.

    ``salt`` plays the role the vendor SDK calls ``LICENSE_KEY`` — the device
    hands it out itself, so no vendor interaction is required.
    """
    moment = datetime_string(when)
    date_b64 = base64.b64encode(moment.encode("ascii")).decode("ascii")
    first = hashlib.sha256(f"{username}{salt}{date_b64}{password}".encode()).hexdigest()
    # The device turns the hex digest into raw bytes before the second hash.
    second = hashlib.sha256(bytes.fromhex(first) + challenge.encode("ascii")).hexdigest()
    return second


def parse_packet(buffer: bytes) -> tuple[int, dict[str, Any], bytes, int] | None:
    """Decode one event packet.

    Returns ``(packet_type, json_payload, binary_segment, consumed)`` or ``None``
    when *buffer* does not yet hold a complete packet. An unknown packet type is
    still returned — the SDK requires clients to tolerate newer types.
    """
    if len(buffer) < PACKET_HEADER_SIZE:
        return None

    binary_len = int.from_bytes(buffer[0:4], "little")
    json_len = int.from_bytes(buffer[8:12], "little")
    packet_type = int.from_bytes(buffer[12:14], "little")
    signature = int.from_bytes(buffer[30:32], "little")

    total = PACKET_HEADER_SIZE + json_len + binary_len
    if len(buffer) < total:
        return None

    signature_ok = signature == PACKET_SIGNATURE
    raw_json = buffer[PACKET_HEADER_SIZE:PACKET_HEADER_SIZE + json_len].rstrip(b"\x00")
    try:
        payload = json.loads(raw_json.decode("utf-8")) if raw_json else {}
    except (UnicodeDecodeError, ValueError):
        payload = {"_unparsed": raw_json.decode("utf-8", "replace")[:200]}
    if not isinstance(payload, dict):
        payload = {"_value": payload}
    if not signature_ok:
        payload["_bad_signature"] = f"0x{signature:04x}"

    binary = buffer[PACKET_HEADER_SIZE + json_len:total]
    return packet_type, payload, binary, total


def plates_from_payload(payload: dict[str, Any]) -> list[str]:
    """Plate numbers carried by a packet, from either supported shape."""
    found: list[str] = []
    for entry in payload.get("license_plate_list") or []:
        if isinstance(entry, dict) and entry.get("license_plate_num"):
            found.append(str(entry["license_plate_num"]))
    for entry in payload.get("objects") or []:
        if not isinstance(entry, dict):
            continue
        info = entry.get("license_plate_info") or {}
        if isinstance(info, dict) and info.get("license_plate_num"):
            found.append(str(info["license_plate_num"]))
    return found


def event_types_from_payload(payload: dict[str, Any]) -> list[str]:
    """Event strings reported by an event packet."""
    found: list[str] = []
    for event in payload.get("events") or []:
        if isinstance(event, dict) and event.get("event_type"):
            found.append(str(event["event_type"]))
    if not found and payload.get("event_type"):
        found.append(str(payload["event_type"]))
    return found


@dataclass
class PxIpcEvent:
    """One decoded packet, in the shape the coordinator wants."""

    packet_type: int
    payload: dict[str, Any] = field(default_factory=dict)
    binary: bytes = b""
    received: float = field(default_factory=time.time)

    @property
    def plates(self) -> list[str]:
        return plates_from_payload(self.payload)

    @property
    def event_types(self) -> list[str]:
        return event_types_from_payload(self.payload)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class PxIpcClient:
    """Small async wrapper around the camera API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 80,
        use_ssl: bool = False,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._scheme = "https" if use_ssl else "http"
        self._username = username
        self._password = password
        self._cookie: str | None = None
        self._lock = asyncio.Lock()

    # -- addressing ----------------------------------------------------- #
    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self._host}:{self._port}"

    @property
    def rtsp_url(self) -> str:
        from .const import RTSP_MAIN

        return (
            f"rtsp://{self._username}:{self._password}@{self._host}:554{RTSP_MAIN}"
        )

    # -- low level ------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        headers = {
            "Api-Version": API_VERSION,
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # The device silently ignores a write whose body is not labelled
            # UTF-8, while reads keep working — hence an explicit header here.
            "Content-Type": JSON_CONTENT_TYPE,
        }
        if self._cookie:
            headers["Cookie"] = f"{COOKIE_NAME}={self._cookie}"
        return headers

    async def _raw_post(
        self, path: str, action: str, data: Any, *, timeout: int = DEFAULT_TIMEOUT
    ) -> dict[str, Any]:
        body = json.dumps({"action": action, "data": data})
        try:
            async with self._session.post(
                f"{self.base_url}{path}",
                data=body.encode("utf-8"),
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status != 200:
                    raise PxIpcResponseError(f"HTTP {response.status} for {path}")
                text = await response.text()
        except asyncio.TimeoutError as err:
            raise PxIpcConnectionError(f"timeout calling {path}") from err
        except aiohttp.ClientError as err:
            raise PxIpcConnectionError(f"{type(err).__name__} calling {path}") from err

        try:
            return json.loads(text)
        except ValueError as err:
            raise PxIpcResponseError(f"{path} returned non-JSON") from err

    async def async_login(self) -> None:
        """Authenticate and remember the session cookie."""
        async with self._lock:
            capabilities = await self._raw_post(
                LOGIN_CAPABILITIES_PATH, "get", {"username": self._username}
            )
            data = capabilities.get("data") or {}
            params = data.get("param") or {}
            salt = params.get("salt")
            challenge = params.get("challenge")
            session_id = data.get("sessionID")
            encryptions = data.get("encryptionType") or []
            if not (salt and challenge and session_id and encryptions):
                raise PxIpcAuthError("login-capabilities returned an unexpected payload")

            when = datetime.now()
            digest = build_login_digest(
                self._username, salt, challenge, self._password, when
            )
            result = await self._raw_post(
                LOGIN_PATH,
                "set",
                {
                    "username": self._username,
                    "loginEncryptionType": encryptions[0],
                    "password": digest,
                    "sessionID": session_id,
                    "datetime": datetime_string(when),
                },
            )
            if str(result.get("code")) != "0":
                raise PxIpcAuthError(f"login rejected (code={result.get('code')})")

            cookie = ((result.get("data") or {}).get("cookie") or "").split("=", 1)
            if len(cookie) != 2 or not cookie[1]:
                raise PxIpcAuthError("login succeeded but no session cookie came back")
            self._cookie = cookie[1]
            _LOGGER.debug("authenticated with %s", self._host)

    async def async_call(
        self, path: str, action: str = "get", data: Any = None, *, _retry: bool = True
    ) -> Any:
        """Call *path* and return its ``data``.

        ``code: 101`` means the session expired (every camera reboot does this),
        so the client logs in again and retries the call once.
        """
        if self._cookie is None:
            await self.async_login()

        result = await self._raw_post(path, action, data)
        code = str(result.get("code"))

        if code == "101" and _retry:
            _LOGGER.debug("session expired, re-authenticating")
            self._cookie = None
            await self.async_login()
            return await self.async_call(path, action, data, _retry=False)

        if code != "0":
            # 203 means "known endpoint, not supported by this firmware"; callers
            # that treat it as fatal would break on unrelated models, so it is
            # surfaced as a normal error and left to them.
            raise PxIpcResponseError(f"{path} answered code={code}")

        return result.get("data")

    # -- high level ----------------------------------------------------- #
    async def async_get_device_info(self) -> dict[str, Any]:
        return await self.async_call(DEVICE_INFO_PATH) or {}

    async def async_get_capability(self) -> dict[str, Any]:
        from .const import CAPABILITY_PATH

        return await self.async_call(CAPABILITY_PATH) or {}

    async def async_get_snapshot(self) -> bytes:
        """Return a JPEG frame taken right now."""
        payload = await self.async_call(SNAPSHOT_PATH)
        data = (payload or {}).get("pictureData")
        if not data:
            raise PxIpcResponseError("snapshot contained no pictureData")
        try:
            return base64.b64decode(data, validate=False)
        except (binascii.Error, ValueError) as err:
            raise PxIpcResponseError("snapshot was not valid base64") from err

    async def async_get_image_params(self) -> dict[str, Any]:
        return await self.async_call(IMAGE_PARAM_PATH) or {}

    async def async_get_legacy_image_params(self) -> dict[str, Any]:
        return await self.async_call(IMAGE_LEGACY_PATH) or {}

    async def async_set_wdr(self, level: int) -> None:
        """Set WDR.

        Only the legacy endpoint actually applies it: the modern one accepts the
        value, answers ``code: 0`` and keeps the old setting. Verified on the
        device, hence the full read-modify-write through the legacy path.
        """
        current = await self.async_get_legacy_image_params()
        current["enableWideDynamic"] = 1 if level > 0 else 0
        if level > 0:
            current["wideDynamicLevel"] = level
        await self.async_call(IMAGE_LEGACY_PATH, "set", current)

    async def async_set_day_night(self, mode: int) -> None:
        current = await self.async_get_image_params()
        current.setdefault("dayNightMode", {})["dayNightMode"] = mode
        await self.async_call(IMAGE_PARAM_PATH, "set", current)

    async def async_set_illuminator(self, mode: int) -> None:
        current = await self.async_get_image_params()
        current.setdefault("dayNightMode", {})["ledMode"] = mode
        await self.async_call(IMAGE_PARAM_PATH, "set", current)

    async def async_set_light_brightness(self, value: int) -> None:
        """Illuminator output level, 0-100 on the models seen so far."""
        current = await self.async_get_image_params()
        current.setdefault("dayNightMode", {})["lightBrightness"] = int(value)
        await self.async_call(IMAGE_PARAM_PATH, "set", current)

    async def async_heartbeat(self) -> None:
        """Keep the session alive; the SDK asks for this every 30s."""
        try:
            await self.async_call(
                HEARTBEAT_PATH, "set", {"cookie": f"{COOKIE_NAME}={self._cookie}"}
            )
        except PxIpcError as err:
            _LOGGER.debug("heartbeat failed: %s", err)

    # -- event stream --------------------------------------------------- #
    async def async_listen_events(
        self,
        on_event: Callable[[PxIpcEvent], None],
        stop: asyncio.Event,
        *,
        on_state: Callable[[bool], None] | None = None,
    ) -> None:
        """Subscribe to ``/events`` and push decoded packets to *on_event*.

        Runs until *stop* is set, reconnecting on any failure. The camera sends
        nothing while idle — it does not implement the heartbeats the SDK
        documents — so a quiet stream is normal, not a fault.
        """
        if self._cookie is None:
            await self.async_login()

        url = f"ws://{self._host}:{self._port}{EVENTS_WS_PATH}"
        while not stop.is_set():
            try:
                async with self._session.ws_connect(url, heartbeat=None, max_msg_size=0) as socket:
                    await socket.send_json(
                        {
                            "action": "start",
                            "data": {
                                "sessionID": self._cookie,
                                "subscription": {
                                    "packet_types": SUBSCRIBED_PACKETS,
                                    "event_image": True,
                                    "face_image": True,
                                    "license_plate_image": True,
                                },
                            },
                        }
                    )
                    if on_state:
                        on_state(True)
                    buffer = b""
                    while not stop.is_set():
                        message = await socket.receive(timeout=60)
                        if message.type is aiohttp.WSMsgType.BINARY:
                            buffer += message.data
                        elif message.type is aiohttp.WSMsgType.TEXT:
                            _LOGGER.debug("event stream said %s", message.data[:200])
                            continue
                        elif message.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
                        else:
                            continue

                        while True:
                            parsed = parse_packet(buffer)
                            if parsed is None:
                                break
                            packet_type, payload, binary, consumed = parsed
                            buffer = buffer[consumed:]
                            try:
                                on_event(
                                    PxIpcEvent(
                                        packet_type=packet_type,
                                        payload=payload,
                                        binary=binary,
                                    )
                                )
                            except Exception:  # pragma: no cover - consumer bug
                                _LOGGER.exception("event handler failed")
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the loop must survive anything
                _LOGGER.debug("event stream dropped: %s: %s", type(err).__name__, err)
            finally:
                if on_state:
                    on_state(False)

            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=WS_RECONNECT_DELAY)
            except asyncio.TimeoutError:
                pass

    async def async_test_connection(self) -> dict[str, Any]:
        """Log in and read device info; used by the config flow."""
        await self.async_login()
        # Touch a real endpoint so a wrong password cannot look like success.
        return await self.async_get_device_info()
