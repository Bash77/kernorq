from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.tools import inspect_project_workspace


class DuplicateToolError(Exception):
    """Raised when registering a tool with a name that already exists."""


class ToolNotFoundError(Exception):
    """Raised when requesting a tool that is not registered."""


class ToolRegistry:
    """Registry managing callable tools available to the deterministic executor."""

    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, func: Callable[..., Any]) -> None:
        """Registers a named callable tool.

        Raises:
            DuplicateToolError: If a tool with the same name is already registered.
            ValueError: If tool name or func is invalid.
        """
        if not name or not isinstance(name, str):
            raise ValueError("Tool name must be a non-empty string")
        if not callable(func):
            raise ValueError("Tool must be callable")
        if name in self._tools:
            raise DuplicateToolError(f"Tool '{name}' is already registered")
        self._tools[name] = func

    def get(self, name: str) -> Callable[..., Any]:
        """Retrieves a registered tool by name.

        Raises:
            ToolNotFoundError: If the tool is not found.
        """
        if name not in self._tools:
            raise ToolNotFoundError(f"Tool '{name}' not found in registry")
        return self._tools[name]

    def has(self, name: str) -> bool:
        """Returns True if the tool name is registered."""
        return name in self._tools

    def list_tools(self) -> list[str]:
        """Returns a list of all registered tool names."""
        return sorted(self._tools.keys())


def create_default_tool_registry() -> ToolRegistry:
    """Creates a ToolRegistry pre-populated with standard project tools."""
    registry = ToolRegistry()
    registry.register("inspect_project_workspace", inspect_project_workspace)
    return registry


__all__ = [
    "ToolRegistry",
    "DuplicateToolError",
    "ToolNotFoundError",
    "create_default_tool_registry",
]
