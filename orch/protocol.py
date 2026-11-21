"""Versioned managed-mode communication rules, recorded with each turn."""
from . import tags as T

VERSION = "upper-default-v1"
LEGACY = "turn-dependent-v0"
DESCRIPTIONS = {
    LEGACY: "Untagged critic review/instruction text goes to the reviewed worker; requested reports and "
            "check-ins go to the manager. Explicit messages follow their addresses. FAIL can continue work "
            "using untagged review feedback. This is the historical protocol, not upper-default-v1.",
    VERSION: "Untagged text always goes upward: worker → critic, critic → manager, manager → human. "
             "Explicit messages go only to the addressed recipient allowed by the sender's role view. "
             "Worker turns require an explicit message or assignment; PASS/FAIL alone never dispatches a worker. "
             "Nonurgent critic progress records wait for the manager's next turn. Questions, pauses, urgent "
             "messages and explicit background-job waits retain their control semantics.",
}


def upward(text, names=()):
    """Plain text and explicitly upward messages, excluding other recipients and control blocks."""
    parts = [T.outside(text or "")]
    parts += [body for attrs, body in T.blocks(text or "", "message")
              if T.addressed(attrs, "you", *names)]
    return "\n\n".join(p for p in parts if p.strip()).strip()
