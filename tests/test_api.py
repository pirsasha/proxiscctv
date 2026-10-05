"""Tests for the parts where a silent mistake costs the most.

The login digest and the packet parser are pure functions, so they can be pinned
against known-good values. The reference digest below was produced by an
independent implementation (PowerShell + .NET SHA256) and then verified to be
accepted by a real camera, which is what makes it a useful oracle: a change that
breaks the hash chain cannot pass by agreeing with itself.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from custom_components.px_ipc.api import (
    build_login_digest,
    datetime_string,
    event_types_from_payload,
    normalise_host,
    parse_packet,
    plates_from_payload,
)
from custom_components.px_ipc.const import (
    PACKET_HEADER_SIZE,
    PACKET_SIGNATURE,
    PKT_EVENT,
    PKT_LICENSE_PLATE,
)

# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #

REFERENCE_MOMENT = datetime(2026, 10, 3, 19, 9, 31)
REFERENCE_SALT = "6e8ed8f8d2bd5f5bc163a459933ced80"
REFERENCE_CHALLENGE = "7128d837567da28c51cce3569db2bfe5"
REFERENCE_DIGEST = "d97e85b5775b5865a571e62fba347ebb99e642380d1d8fee914aff127778258b"


def test_datetime_string_uses_local_time_without_timezone():
    """The device validates this string, so a stray offset would break login."""
    assert datetime_string(REFERENCE_MOMENT) == "2026-10-03T19:09:31"


def test_login_digest_matches_the_reference_vector():
    assert (
        build_login_digest(
            "admin", REFERENCE_SALT, REFERENCE_CHALLENGE, "admin", REFERENCE_MOMENT
        )
        == REFERENCE_DIGEST
    )


def test_login_digest_changes_with_every_input():
    """A digest that ignores an input would still log in — and hide a typo."""
    baseline = build_login_digest(
        "admin", REFERENCE_SALT, REFERENCE_CHALLENGE, "admin", REFERENCE_MOMENT
    )
    variants = [
        build_login_digest("root", REFERENCE_SALT, REFERENCE_CHALLENGE, "admin", REFERENCE_MOMENT),
        build_login_digest("admin", "00" * 16, REFERENCE_CHALLENGE, "admin", REFERENCE_MOMENT),
        build_login_digest("admin", REFERENCE_SALT, "ab" * 16, "admin", REFERENCE_MOMENT),
        build_login_digest("admin", REFERENCE_SALT, REFERENCE_CHALLENGE, "hunter2", REFERENCE_MOMENT),
        build_login_digest(
            "admin", REFERENCE_SALT, REFERENCE_CHALLENGE, "admin",
            datetime(2026, 10, 3, 19, 9, 32),
        ),
    ]
    for variant in variants:
        assert variant != baseline
    assert all(len(v) == 64 for v in variants)  # lowercase hex sha256


# --------------------------------------------------------------------------- #
# Address parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("192.168.2.223", ("192.168.2.223", None)),
        (" 192.168.2.223 ", ("192.168.2.223", None)),
        # The mistake that produces "cannot connect" for no visible reason:
        # a port in the host field yields http://host:80:80 otherwise.
        ("192.168.2.223:80", ("192.168.2.223", 80)),
        ("192.168.2.223:8080", ("192.168.2.223", 8080)),
        ("http://192.168.2.223", ("192.168.2.223", None)),
        ("http://192.168.2.223:8080/", ("192.168.2.223", 8080)),
        ("https://camera.local", ("camera.local", None)),
        ("camera.local/path", ("camera.local", None)),
        # A non-numeric suffix must not be mistaken for a port.
        ("camera.local:http", ("camera.local:http", None)),
        # Bare IPv6 stays intact; a bracketed literal is unpacked.
        ("fe80::1", ("fe80::1", None)),
        ("[fe80::1]:8080", ("fe80::1", 8080)),
        ("", ("", None)),
    ],
)
def test_normalise_host_accepts_what_people_actually_type(typed, expected):
    assert normalise_host(typed) == expected


# --------------------------------------------------------------------------- #
# Packet framing
# --------------------------------------------------------------------------- #


def build_packet(
    packet_type: int,
    payload: dict,
    *,
    binary: bytes = b"",
    signature: int = PACKET_SIGNATURE,
) -> bytes:
    """Frame one packet the way the camera actually does.

    Layout confirmed against live traffic on 2026-10-04: the signature opens the
    header, and the field between ``json_len`` and ``bin_len`` is reserved and
    always zero. The earlier builder put those fields in the order the vendor
    SDK happens to list them, which is not the order they appear on the wire —
    and that is exactly what hid every plate packet.
    """
    body = json.dumps(payload).encode("utf-8") + b"\x00"
    return b"".join(
        [
            signature.to_bytes(2, "little"),
            packet_type.to_bytes(2, "little"),
            len(body).to_bytes(4, "little"),
            (0).to_bytes(4, "little"),  # reserved, always zero
            len(binary).to_bytes(4, "little"),
            b"\x00" * 16,
            body,
            binary,
        ]
    )


def test_real_captured_header_matches_the_documented_offsets():
    """A header captured from the camera on 2026-10-04, verbatim.

    Pinned so a future refactor cannot quietly drift back to the layout that
    made every packet look corrupt: this is an event packet carrying a 226 KB
    image, and its length fields sit where the parser now reads them.
    """
    header = bytes.fromhex(
        "a5a502003c020000000000007d730300" "00000000000000000000000000000000"
    )
    assert len(header) == PACKET_HEADER_SIZE

    assert int.from_bytes(header[0:2], "little") == PACKET_SIGNATURE
    assert int.from_bytes(header[2:4], "little") == PKT_EVENT
    assert int.from_bytes(header[4:8], "little") == 572
    assert int.from_bytes(header[8:12], "little") == 0
    assert int.from_bytes(header[12:16], "little") == 226173
    # 32 + 572 + 226173 is the size the camera actually sent.
    assert PACKET_HEADER_SIZE + 572 + 226173 == 226777


def test_license_plate_packet_is_decoded():
    packet = build_packet(
        PKT_LICENSE_PLATE, {"license_plate_list": [{"license_plate_num": "A123BC777"}]}
    )

    parsed = parse_packet(packet)

    assert parsed is not None
    packet_type, payload, binary, consumed = parsed
    assert packet_type == PKT_LICENSE_PLATE
    assert plates_from_payload(payload) == ["A123BC777"]
    assert binary == b""
    assert consumed == len(packet)


def test_binary_segment_is_split_out_of_the_packet():
    """The plate thumbnail rides in the binary segment, not inside the JSON."""
    binary = b"\xff\xd8thumbnail\xff\xd9"
    packet = build_packet(PKT_LICENSE_PLATE, {"license_plate_list": []}, binary=binary)

    parsed = parse_packet(packet)

    assert parsed is not None
    assert parsed[2] == binary
    assert parsed[3] == len(packet)


def test_incomplete_packet_returns_none_until_it_arrives():
    packet = build_packet(PKT_LICENSE_PLATE, {"license_plate_list": []})

    assert parse_packet(packet[: PACKET_HEADER_SIZE - 1]) is None
    assert parse_packet(packet[:-1]) is None
    assert parse_packet(packet) is not None


def test_two_packets_in_one_buffer_are_consumed_one_at_a_time():
    """TCP gives no message boundaries; the parser must report the exact length."""
    first = build_packet(2, {"events": [{"event_type": "intrusion"}]})
    second = build_packet(PKT_LICENSE_PLATE, {"license_plate_list": []})
    buffer = first + second

    parsed = parse_packet(buffer)
    assert parsed is not None
    _, _, _, consumed = parsed
    assert consumed == len(first)

    remainder = parse_packet(buffer[consumed:])
    assert remainder is not None
    assert remainder[0] == PKT_LICENSE_PLATE


def test_bad_signature_is_reported_not_dropped():
    """The SDK says to discard these; flagging beats silently trusting garbage."""
    packet = build_packet(2, {"events": []}, signature=0x0000)

    parsed = parse_packet(packet)

    assert parsed is not None
    assert parsed[1]["_bad_signature"] == "0x0000"


def test_unknown_packet_type_is_returned_for_forward_compatibility():
    packet = build_packet(99, {"anything": True})

    parsed = parse_packet(packet)

    assert parsed is not None
    assert parsed[0] == 99


def test_malformed_json_does_not_lose_the_packet():
    body = b"{not json\x00"
    packet = b"".join(
        [
            PACKET_SIGNATURE.to_bytes(2, "little"),
            (2).to_bytes(2, "little"),
            len(body).to_bytes(4, "little"),
            (0).to_bytes(4, "little"),  # reserved
            (0).to_bytes(4, "little"),  # no binary segment
            b"\x00" * 16,
            body,
        ]
    )

    parsed = parse_packet(packet)

    assert parsed is not None
    assert "_unparsed" in parsed[1]


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #


def test_plates_are_read_from_both_documented_shapes():
    """Packet 6 uses a plate list; generic object packets nest it per object."""
    assert plates_from_payload(
        {"license_plate_list": [{"license_plate_num": "A123BC777"}]}
    ) == ["A123BC777"]

    assert plates_from_payload(
        {
            "objects": [
                {"obj_type": "vehicle", "license_plate_info": {"license_plate_num": "X999YY777"}},
                {"obj_type": "person"},
            ]
        }
    ) == ["X999YY777"]


def test_plates_are_empty_when_the_camera_said_nothing():
    assert plates_from_payload({}) == []
    assert plates_from_payload({"objects": [{"license_plate_info": {}}]}) == []


def test_event_types_come_from_the_event_list():
    payload = {"events": [{"event_type": "intrusion"}, {"event_type": "illegal_parking"}]}
    assert event_types_from_payload(payload) == ["intrusion", "illegal_parking"]


def test_event_type_falls_back_to_the_top_level_field():
    assert event_types_from_payload({"event_type": "motion"}) == ["motion"]
    assert event_types_from_payload({}) == []
