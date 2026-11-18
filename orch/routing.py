"""Message routing: where each `<message to="…">` of a reply goes, and the record of what became of it.

Every reply is scanned, whatever produced it: a round turn, a board chat, the manager's interactive terminal
session (through `orch/hooks/terminal_relay.py`), or a turn orch started only to deliver a message.

The ledger (`state["messages"]`) holds one entry per message:

    sent        recorded and on its way: waiting for the recipient's next prompt
    held        not delivered, with a reason: an address the sender may not use, the recipient's session open in a
                terminal (it goes out when the terminal closes), or held by design (`by_design`: e.g. the critic's
                checklist, which the worker never sees)
    delivered   seen in the recipient's actual prompt (`turn`, `how`: prompt / cut in / live)
    superseded  replaced by a newer message from the same sender before it was delivered (`by`)

Delivery is checked against the prompt the recipient was really given, never assumed from having queued it.

Supersede rule: a message an agent sends down to the agent it directs (manager -> critic or worker, critic ->
worker) replaces that sender's older messages to the same recipient that are still waiting, unless it is marked
`additive="yes"`. Messages in the same reply never replace each other; notes up (critic -> manager) and messages
from people are always additive. Only waiting messages orch can still take back (an inbox note, a queued check-in
update, a relayed note) are replaced; one a phase of the run carries anyway is delivered as usual.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import tags as T
from .recorder import now_iso

# whom each role may write to with <message to="…"> (besides to="you", its own user)
MAY_ADDRESS = {"manager": ("verifier", "worker"), "verifier": ("worker", "manager"), "worker": ()}
# sender role -> recipient roles it directs (its messages to them supersede its older waiting ones)
DIRECTS = {"manager": ("verifier", "worker"), "verifier": ("worker",)}
MAX_HOPS = 2  # a message written in a turn that a message started counts one hop more; past this it waits


def flat(text: str) -> str:
    """Text for matching: lower case, whitespace collapsed."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def carries(hay: str, body: str) -> bool:
    """Does `hay` (flattened) carry the message `body`? orch may wrap a message, or drop its headings and checklist
    before passing it on, so it counts when most of its substantial lines are there (or, for a short message, all
    of it)."""
    lines = [flat(ln.strip().lstrip("-*•>").strip()) for ln in (body or "").splitlines()]
    lines = [ln for ln in lines if len(ln) >= 12 and not ln.startswith("#")]
    if not lines:
        whole = flat(body)
        return bool(whole) and whole in hay
    return sum(ln in hay for ln in lines) >= max(1, -(-len(lines) * 3 // 5))


def additive(attrs: Dict[str, str]) -> bool:
    """<message to="…" additive="yes">: sent in addition to what is still waiting, not in place of it."""
    return T._norm(attrs.get("additive", "")) in ("yes", "true", "1")


def split_reply(text: str, sender: Any, agents: List[Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The <message> blocks of a reply that leave the conversation it was written in, by the sender's role view:
    ([{target, body, urgent, additive, i}], [the bad address of each block that can't go]). `to="you"` (the
    sender's own user) stays where it was written and is in neither list. A worker addresses no one."""
    allowed = MAY_ADDRESS.get(getattr(sender, "role", ""), ())
    out, bad = [], []
    for i, (attrs, body) in enumerate(T.blocks(text or "", "message")):
        to = (attrs.get("to") or "").strip()
        if T.addressed(attrs, "you"):
            continue
        target = T.recipient(to, agents)
        if target is None or target.name == sender.name or target.role not in allowed or not (body or "").strip():
            bad.append(f'<message to="{to}">' + ("" if (body or "").strip() else " (empty)"))
            continue
        out.append({"target": target, "body": body, "urgent": T.urgent(attrs), "additive": additive(attrs), "i": i})
    return out, bad


class Ledger:
    """The message ledger in a run's state (`state["messages"]`)."""

    def __init__(self, state: Dict[str, Any]):
        self.st = state

    @property
    def rows(self) -> List[Dict[str, Any]]:
        return self.st.setdefault("messages", [])

    def get(self, mid: str) -> Optional[Dict[str, Any]]:
        return next((m for m in self.rows if m["id"] == mid), None)

    def add(self, frm: str, to: str, body: str, text: str = "", **kw: Any) -> Dict[str, Any]:
        m = {"id": f"M{len(self.rows) + 1}", "from": frm, "to": to, "body": body, "text": text or body,
             "source": "", "i": None, "via": "round", "purpose": "", "urgent": False, "additive": False, "hops": 1,
             "activate": False, "carrier": {}, "status": "sent", "reason": "", "by_design": False,
             "created_at": now_iso()}
        m.update(kw)
        self.rows.append(m)
        return m

    def mark(self, m: Dict[str, Any], status: str, **kw: Any) -> None:
        m.update(status=status, updated_at=now_iso(), **kw)

    def waiting(self, to: str) -> List[Dict[str, Any]]:
        return [m for m in self.rows if m["to"] == to and m["status"] == "sent"]

    def by_carrier(self, key: str, value: Any) -> List[Dict[str, Any]]:
        return [m for m in self.rows if (m.get("carrier") or {}).get(key) == value]

    def seen(self, to: str, prompt: str, turn: str, how: str = "prompt") -> List[Dict[str, Any]]:
        """Mark every waiting message to `to` that this prompt (or cut-in note) carries as delivered."""
        hay, out = flat(prompt), []
        for m in self.waiting(to):
            if carries(hay, m.get("text") or m.get("body") or ""):
                self.mark(m, "delivered", turn=turn, how=how, delivered_at=now_iso())
                out.append(m)
        return out

    def older_to_replace(self, new: Dict[str, Any], roles: Dict[str, str]) -> List[Dict[str, Any]]:
        """Waiting messages `new` replaces (the supersede rule above); roles: agent name -> role."""
        if new.get("additive") or roles.get(new["to"]) not in DIRECTS.get(roles.get(new["from"], ""), ()):
            return []
        return [m for m in self.rows if m is not new and m["from"] == new["from"] and m["to"] == new["to"]
                and m["status"] in ("sent", "held") and not m.get("by_design") and m.get("source") != new.get("source")
                and any(k in (m.get("carrier") or {}) for k in ("inbox", "wm", "fwd"))]
