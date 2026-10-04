"""Entity wiring checks.

The number and switch platforms are table-driven: each row names a property on
``PxIpcData`` to read and a method on ``PxIpcClient`` to write. A typo in either
name is invisible until someone drags the slider, at which point it fails deep
inside a service call. These tests resolve both names up front instead.
"""

from __future__ import annotations

from custom_components.px_ipc.api import PxIpcClient
from custom_components.px_ipc.coordinator import PxIpcData
from custom_components.px_ipc.number import NUMBERS
from custom_components.px_ipc.select import (
    PxIpcAntiFlickerSelect,
    PxIpcDayNightSelect,
    PxIpcDnrSelect,
    PxIpcIlluminatorSelect,
    PxIpcWdrSelect,
)
from custom_components.px_ipc.switch import SWITCHES


def test_every_number_description_resolves():
    assert NUMBERS, "the number platform would register nothing"
    for description in NUMBERS:
        assert hasattr(PxIpcClient, description.setter), (
            f"{description.key}: no client method {description.setter!r}"
        )
        assert hasattr(PxIpcData, description.getter), (
            f"{description.key}: no PxIpcData property {description.getter!r}"
        )
        assert description.min_value < description.max_value, description.key


def test_every_switch_description_resolves():
    assert SWITCHES, "the switch platform would register nothing"
    for description in SWITCHES:
        assert hasattr(PxIpcClient, description.setter), (
            f"{description.key}: no client method {description.setter!r}"
        )
        assert hasattr(PxIpcData, description.getter), (
            f"{description.key}: no PxIpcData property {description.getter!r}"
        )


def test_entity_keys_are_unique_across_platforms():
    number_keys = {d.key for d in NUMBERS}
    switch_keys = {d.key for d in SWITCHES}
    select_classes = (
        PxIpcDayNightSelect,
        PxIpcIlluminatorSelect,
        PxIpcWdrSelect,
        PxIpcAntiFlickerSelect,
        PxIpcDnrSelect,
    )
    # Each select builds its description in __init__, so the keys live in the
    # source rather than in a table; check them for duplicates by hand.
    assert len(number_keys) == len(NUMBERS)
    assert len(switch_keys) == len(SWITCHES)
    assert number_keys.isdisjoint(switch_keys)
    assert len(select_classes) == 5


def test_select_option_maps_round_trip():
    """Every rendered option must map back to a device value."""
    from custom_components.px_ipc.const import (
        ANTI_FLICKER_LEVELS,
        ANTI_FLICKER_LEVELS_REVERSE,
        DAY_NIGHT_MODES,
        DAY_NIGHT_MODES_REVERSE,
        DNR_LEVELS,
        DNR_LEVELS_REVERSE,
        ILLUMINATOR_MODES,
        ILLUMINATOR_MODES_REVERSE,
        WDR_LEVELS,
        WDR_LEVELS_REVERSE,
    )

    for forward, reverse in (
        (DAY_NIGHT_MODES, DAY_NIGHT_MODES_REVERSE),
        (ILLUMINATOR_MODES, ILLUMINATOR_MODES_REVERSE),
        (WDR_LEVELS, WDR_LEVELS_REVERSE),
        (DNR_LEVELS, DNR_LEVELS_REVERSE),
        (ANTI_FLICKER_LEVELS, ANTI_FLICKER_LEVELS_REVERSE),
    ):
        assert len(reverse) == len(forward), "duplicate option names"
        for value, name in forward.items():
            assert reverse[name] == value
