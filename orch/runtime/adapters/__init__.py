"""Adapters: one open CLI each. `make(backend)` finds the class; nothing is imported before it is needed."""
from __future__ import annotations

import importlib
from typing import Any

# backend name in the config -> (module, class); the classes' packages are in docs/redesign.md 4.2
ADAPTERS = {
    "claude": ("claude", "ClaudeAdapter"),
    "qoder": ("claude", "QoderAdapter"),
    "qwen": ("claude", "QwenAdapter"),
    "codex": ("codex", "CodexAdapter"),
    "opencode": ("opencode", "OpenCodeAdapter"),
    "agy": ("agy", "AgyAdapter"),
    "command": ("oneshot", "CommandAdapter"),
    "mock": ("mock", "MockAdapter"),
}


def adapter_class(backend: str) -> Any:
    if backend not in ADAPTERS:
        raise ValueError(f"unknown backend {backend!r}")
    module, name = ADAPTERS[backend]
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


def make(backend: str, clock: Any) -> Any:
    """A new, not yet started adapter. Every adapter class takes the clock as its only argument."""
    return adapter_class(backend)(clock)
