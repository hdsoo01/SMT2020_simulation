"""Explicit tool registration while preserving subclass dispatch for evaluators."""
from __future__ import annotations

from copy import deepcopy
from functools import partial


class ToolRegistry:
    def __init__(self):
        self._entries = {}

    @classmethod
    def from_specs(cls, specs):
        registry = cls()
        for spec in specs:
            registry.register(spec)
        return registry

    def register(self, spec: dict, handler=None):
        """handler: method name, or callable(toolbox, **args). Returns self.

        Registration is explicit Python code; configuration cannot import code.
        A method name is resolved on the actual instance, including subclasses.
        """
        name = spec.get("name")
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError("tool name must be an identifier")
        if name in self._entries:
            raise ValueError(f"duplicate tool: {name}")
        if not isinstance(spec.get("parameters"), dict):
            raise ValueError(f"{name}: parameters must be a JSON schema object")
        handler = name if handler is None else handler
        if not isinstance(handler, str) and not callable(handler):
            raise TypeError("handler must be a method name or callable")
        self._entries[name] = (deepcopy(spec), handler)
        return self

    def specs(self) -> list:
        return [deepcopy(spec) for spec, _ in self._entries.values()]

    def clone(self):
        other = type(self)()
        for spec, handler in self._entries.values():
            other.register(spec, handler)
        return other

    def resolve(self, toolbox, name):
        entry = self._entries.get(name)
        if entry is None:
            return None
        handler = entry[1]
        return getattr(toolbox, handler) if isinstance(handler, str) else partial(handler, toolbox)
