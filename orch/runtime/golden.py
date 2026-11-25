"""The golden board (docs/redesign.md PRM-5): reading the file, and telling the manager when it names the layers above
the critic. The manager keeps the board; the critic reads it by path. Workers never get it. Pure: `read_board` is given
the file's text by the engine. Which reminder lines are shown, and the size warning, belong to the reminders (H), which
read `chars` and `words` from the snapshot.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import List, Optional, Sequence

from .. import boards as B
from . import contracts as C
from .contracts import GoldenBoard, Snapshot, TextKey as K, TextRef

NAME = "golden"
MANAGER_ONLY = ("manager", "orch", "real user")  # words that name the layer above the critic


def read_board(path: str, text: Optional[str], last: GoldenBoard, names: Sequence[str]) -> GoldenBoard:
    """The board as agents read it (no comments, no title). `digest` and `chars` follow that body, so a change to a
    comment is no change. `words`: manager-only words in it, case-insensitive, whole words (the leak check, which now
    also covers the file the critic reads). `names`: the names the agents above the critic are known by."""
    body = B.golden_body(text or "")
    wanted = list(MANAGER_ONLY) + [n for n in names if n and len(n) > 2 and n.lower() != "the"]
    found = sorted({w for w in wanted if re.search(rf"(?i)(?<![\w.-]){re.escape(w)}(?![\w-])", body)})
    return GoldenBoard(path=path, digest=B.digest(body) if body else "", chars=len(body), words=found,
                       leak_told=last.leak_told)


class _Module:
    name = NAME

    def decide(self, snap: Snapshot, event: C.Event, prior: Sequence[C.Action] = ()) -> List[C.Action]:
        g, mgr = snap.golden, snap.manager
        if mgr is None or not mgr.is_open:
            return []
        if g.words and g.leak_told != g.digest:  # told once for each version of the board
            msg = C.Message(C.new_id(NAME, event), C.ORCH, mgr.id, text=TextRef(K.GOLDEN_LEAK, {
                "path": g.path, "words": list(g.words)}), needs_answer=False, trigger=C.Trigger.ORCH)
            return [C.Tell(msg), C.SetGolden(replace(g, leak_told=g.digest))]
        if not g.words and g.leak_told:  # fixed: a later leak is told again
            return [C.SetGolden(replace(g, leak_told=""))]
        return []


def module() -> _Module:
    return _Module()
