"""Guard against shadowing Home Assistant's own attributes.

``DataUpdateCoordinator`` keeps private state on ``self`` — ``_listeners``,
``data``, ``update_interval`` and friends. Assigning one of those names in a
subclass silently replaces Home Assistant's bookkeeping with ours, and the
failure surfaces much later and somewhere unrelated: clobbering ``_listeners``
made ``async_update_listeners()`` die with

    AttributeError: 'list' object has no attribute 'values'

on the first refresh, while the config flow had already succeeded, so it looked
like a network problem rather than a naming mistake.

The reserved names are read out of the installed Home Assistant at test time
rather than hard-coded, so a new internal attribute in a future release is
covered automatically.
"""

from __future__ import annotations

import ast
from pathlib import Path

from homeassistant.helpers import update_coordinator

COORDINATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "px_ipc"
    / "coordinator.py"
)


def _assignments(node: ast.AST, class_name: str | None = None) -> set[str]:
    """Names assigned to ``self.<name>`` inside *class_name* (or module-wide)."""
    names: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth_class: str | None = None

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            previous, self.depth_class = self.depth_class, node.name
            self.generic_visit(node)
            self.depth_class = previous

        def _record(self, target: ast.AST) -> None:
            if (
                class_name is not None
                and self.depth_class != class_name
            ):
                return
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                names.add(target.attr)

        def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
            for target in node.targets:
                self._record(target)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
            self._record(node.target)
            self.generic_visit(node)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
            self._record(node.target)
            self.generic_visit(node)

    Visitor().visit(node)
    return names


def test_coordinator_does_not_shadow_data_update_coordinator_attributes():
    ha_source = Path(update_coordinator.__file__).read_text(encoding="utf-8")
    reserved = _assignments(ast.parse(ha_source), "DataUpdateCoordinator")

    # Sanity-check the extraction: an empty set would make the test vacuous.
    assert {"_listeners", "data", "hass"} <= reserved, sorted(reserved)

    ours = _assignments(ast.parse(COORDINATOR_PATH.read_text(encoding="utf-8")), "PxIpcCoordinator")
    clashes = sorted(ours & reserved)

    assert clashes == [], (
        "PxIpcCoordinator assigns names that DataUpdateCoordinator owns: "
        f"{clashes}. Rename them — Home Assistant keeps its own state there."
    )


def test_event_subscribers_use_the_renamed_attribute():
    """The subscribers must live somewhere other than ``_listeners``."""
    tree = ast.parse(COORDINATOR_PATH.read_text(encoding="utf-8"))
    ours = _assignments(tree, "PxIpcCoordinator")

    assert "_event_listeners" in ours
    assert "_listeners" not in ours
