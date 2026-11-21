"""Explicitly migrate old scripted work instructions to the managed message protocol.

Used only at fixture declaration sites that intend to assign work. New routing tests supply raw replies
and never use this helper, so they exercise missing tags and idle behavior unchanged.
"""
from orch import tags as T


def worker_instruction(text):
    if not isinstance(text, str):
        return text
    # A completion report, parallel proposal, or already-tagged instruction has no implicit assignment.
    if T.value(text, 'verdict') == 'PASS' or T.blocks(text, 'workers'):
        return text
    plain = T.outside(text).strip()
    if not plain:
        return text
    controls = '\n'.join(text[a:b] for _, _, _, a, b in T.parse(text))
    return '<message to="{worker}">' + plain + '</message>\n' + controls
