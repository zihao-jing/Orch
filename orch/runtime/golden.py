"""The golden board (docs/redesign.md PRM-5): reading the file. The manager keeps the board; the critic is never
pointed to it, and workers never get it. Pure: `read_board` is given the file's text by the engine. Which reminder
lines are shown, and the size warning, belong to the reminders (H), which read `chars` from the snapshot.
"""
from __future__ import annotations

from typing import Optional

from .. import boards as B
from .contracts import GoldenBoard


def read_board(path: str, text: Optional[str], last: GoldenBoard) -> GoldenBoard:
    """The board as agents read it (no comments, no title). `digest` and `chars` follow that body, so a change to a
    comment is no change. `last` is the board read before; nothing of it is kept."""
    body = B.golden_body(text or "")
    return GoldenBoard(path=path, digest=B.digest(body) if body else "", chars=len(body))
