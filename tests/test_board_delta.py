"""The board's /api/state deltas: a browser that applies each reply ends up with exactly the full payload."""

import gzip
import json
import random
import tempfile
import unittest
import urllib.request
from pathlib import Path

from orch.board import Board, StateDeltas, serve


def apply(snap, ver, j):
    """What board.html's applyState does; returns (snap, ver) or None when the reply is not built on ours."""
    if "base" in j and (j["base"] != ver or snap is None):
        return None
    out = {}
    for k in j["keys"]:
        if k in j.get("set", {}):
            out[k] = j["set"][k]
        elif k in j.get("patch", {}):
            items = []
            for op in j["patch"][k]:
                if isinstance(op, list):
                    items.extend(snap[k][op[0]:op[0] + op[1]])
                else:
                    items.append(op["v"])
            out[k] = items
        else:
            out[k] = snap[k]
    return out, j["ver"]


class StateDeltasTest(unittest.TestCase):
    def test_round_trips_through_random_edits(self):
        rng = random.Random(7)
        d = StateDeltas()
        data = {"status": "running", "round": 1, "turns": [{"id": i, "s": "done"} for i in range(50)],
                "messages": [f"m{i}" for i in range(30)], "live": {"t1": "x"}}
        snap, ver = None, ""
        for step in range(200):
            data = json.loads(json.dumps(data))
            data["round"] = step
            r = rng.random()
            if r < 0.3:
                data["turns"].append({"id": 1000 + step, "s": "running"})
            elif r < 0.5 and data["turns"]:
                data["turns"][rng.randrange(len(data["turns"]))]["s"] = f"edit{step}"
            elif r < 0.6 and data["turns"]:
                del data["turns"][rng.randrange(len(data["turns"]))]
            elif r < 0.7:
                data["messages"].insert(rng.randrange(len(data["messages"]) + 1), f"new{step}")
            elif r < 0.8:
                data.pop("live", None) if "live" in data else data.__setitem__("live", {"t": step})
            elif r < 0.85:
                data["turns"] = list(reversed(data["turns"]))
            j = json.loads(d.reply(data, ver))
            got = apply(snap, ver, j)
            self.assertIsNotNone(got)
            snap, ver = got
            self.assertEqual(snap, data)
            self.assertEqual(list(snap), list(data))  # key order too

    def test_small_change_sends_little(self):
        d = StateDeltas()
        data = {"turns": [{"id": i, "text": "x" * 500} for i in range(400)], "round": 1}
        full = json.loads(d.reply(data, ""))
        data["turns"][-1]["text"] = "changed"
        data["round"] = 2
        txt = d.reply(data, full["ver"])
        self.assertLess(len(txt), 2000)
        self.assertEqual(apply(full["set"], full["ver"], json.loads(txt))[0], data)

    def test_unchanged_and_unknown_base(self):
        d = StateDeltas()
        data = {"a": [1, 2], "b": "x"}
        v = json.loads(d.reply(data, ""))["ver"]
        same = json.loads(d.reply(data, v))
        self.assertEqual((same["set"], same["patch"], same["ver"]), ({}, {}, v))
        fresh = json.loads(d.reply(data, "not-a-version"))
        self.assertNotIn("base", fresh)
        self.assertEqual(fresh["set"], data)


class BoardHttpTest(unittest.TestCase):
    def test_since_and_gzip(self):
        with tempfile.TemporaryDirectory() as tmp:
            rd = Path(tmp)
            (rd / "state.json").write_text(json.dumps({"status": "running", "goal": "g" * 3000}), encoding="utf-8")
            srv, url = serve({"m": Board("m", lambda: rd)}, port=18765)
            try:
                plain = json.loads(urllib.request.urlopen(url + "api/state").read())
                self.assertEqual(plain["status"], "running")  # no since: the full payload as before
                req = urllib.request.Request(url + "api/state?since=", headers={"Accept-Encoding": "gzip"})
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
                    j = json.loads(gzip.decompress(r.read()))
                self.assertEqual(j["set"], plain)
                again = json.loads(urllib.request.urlopen(url + "api/state?since=" + j["ver"]).read())
                self.assertEqual((again["base"], again["set"], again["patch"]), (j["ver"], {}, {}))
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
