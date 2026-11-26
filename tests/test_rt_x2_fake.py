"""A fake `codex app-server` for the X2 tests: WebSocket over a unix socket, JSON-RPC, scripted turns.

Run as `python3 test_rt_x2_fake.py app-server --listen unix://PATH ...` (the tests wrap it in a shell script).
What a turn does depends on the text of its message: JOB (a command that keeps running after the turn), CMD (a command
that ends inside the turn), LONG (the turn stays open until `x/finish`), FAIL (usage limit error), else a short answer.
Control methods for the tests: x/endJob {code}, x/finish, x/echo {text}. Events reach only connections that called
thread/start or thread/resume, as in the real server (docs/probes/codex.md).
"""
import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import uuid

LOCK = threading.RLock()
THREADS = {}  # id -> {"turns": [...], "times": {item id: completedAtMs}, "subs": set of Conn}
JOBS = {}  # item id -> {"thread", "turn", "process", "command"}
COUNTER = [0]


def now_ms():
    return int(time.time() * 1000)


def _frame(op, payload):
    n = len(payload)
    head = bytes([0x80 | op])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


class Conn:
    def __init__(self, sock):
        self.sock, self.buf, self.wlock = sock, b"", threading.Lock()

    def send(self, obj):
        with self.wlock:
            try:
                self.sock.sendall(_frame(1, json.dumps(obj).encode()))
            except OSError:
                pass

    def need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        while True:
            b1, b2 = self.need(2)
            op, n = b1 & 15, b2 & 127
            if n == 126:
                n = struct.unpack(">H", self.need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self.need(8))[0]
            key = self.need(4) if b2 & 0x80 else None
            data = self.need(n)
            if key:
                data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
            if op == 8:
                raise EOFError
            if op == 1:
                return json.loads(data)
            if op == 9:
                with self.wlock:
                    self.sock.sendall(_frame(10, data))


def notify(thread, method, **params):
    with LOCK:
        for c in list(THREADS[thread]["subs"]):
            c.send({"method": method, "params": {"threadId": thread, **params}})


def new_id(prefix):
    COUNTER[0] += 1
    return f"{prefix}-{COUNTER[0]}"


def add_item(thread, turn, item, done=True):
    turn["items"].append(item)
    notify(thread, "item/started", turnId=turn["id"], item=item)
    if done:
        complete_item(thread, turn, item)


def complete_item(thread, turn, item):
    THREADS[thread]["times"][item["id"]] = now_ms()
    notify(thread, "item/completed", turnId=turn["id"], item=item)


def user_item(text, client_id):
    return {"id": new_id("um"), "type": "userMessage", "clientId": client_id, "content": [{"type": "text", "text": text}]}


def find_turn(thread, turn_id):
    return next(t for t in THREADS[thread]["turns"] if t["id"] == turn_id)


def end_turn(thread, turn, status="completed", error=None):
    turn["status"], turn["completedAt"], turn["error"] = status, time.time(), error
    notify(thread, "turn/completed", turn={"id": turn["id"], "status": status, "error": error})
    notify(thread, "thread/status/changed", status={"type": "idle"})


def run_turn(thread, turn, text):
    with LOCK:
        if "JOB" in text:
            item = {"id": new_id("exec"), "type": "commandExecution", "command": "sh -c 'sleep 100'",
                    "processId": new_id("p"), "status": "inProgress"}
            JOBS[item["id"]] = {"thread": thread, "turn": turn["id"], "process": item["processId"], "item": item}
            add_item(thread, turn, item, done=False)
            add_item(thread, turn, {"id": new_id("am"), "type": "agentMessage", "phase": "final_answer",
                                    "text": "started the job"})
            end_turn(thread, turn)
        elif "CMD" in text:
            item = {"id": new_id("exec"), "type": "commandExecution", "command": "echo hi", "processId": new_id("p"),
                    "status": "inProgress"}
            add_item(thread, turn, item, done=False)
            item.update(status="completed", exitCode=0)
            complete_item(thread, turn, item)
            add_item(thread, turn, {"id": new_id("am"), "type": "agentMessage", "phase": "final_answer", "text": "ran it"})
            end_turn(thread, turn)
        elif "LONG" in text:
            add_item(thread, turn, {"id": new_id("rs"), "type": "reasoning"})
        elif "FAIL" in text:
            err = {"message": "usage limit reached", "codexErrorInfo": "usageLimitExceeded"}
            end_turn(thread, turn, "failed", err)
        else:
            add_item(thread, turn, {"id": new_id("am"), "type": "agentMessage", "phase": "commentary", "text": "hmm"})
            notify(thread, "item/agentMessage/delta", turnId=turn["id"], itemId="x", delta="o")
            add_item(thread, turn, {"id": new_id("am"), "type": "agentMessage", "phase": "final_answer",
                                    "text": "ok: " + text})
            end_turn(thread, turn)


def active_turn(thread):
    turns = THREADS[thread]["turns"]
    return turns[-1] if turns and turns[-1]["status"] == "inProgress" else None


def begin_turn(thread, items):
    turn = {"id": str(uuid.uuid4()), "status": "inProgress", "items": [], "startedAt": time.time(),
            "completedAt": None, "error": None}
    THREADS[thread]["turns"].append(turn)
    notify(thread, "thread/status/changed", status={"type": "active", "activeFlags": []})
    notify(thread, "turn/started", turn={"id": turn["id"], "status": "inProgress"})
    for it in items:
        add_item(thread, turn, it)
    return turn


class Error(Exception):
    def __init__(self, message):
        self.message = message


def handle(conn, method, p):
    with LOCK:
        if method == "initialize":
            return {"userAgent": "fake/0", "codexHome": "/nowhere"}
        if method == "x/echo":
            return {"text": p["text"]}
        if method == "model/list":
            return {"data": [
                {"id": "fake-a", "model": "fake-a", "displayName": "Fake A", "hidden": False,
                 "defaultReasoningEffort": "low",
                 "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
                {"id": "fake-h", "model": "fake-h", "hidden": True, "supportedReasoningEfforts": []}], "nextCursor": None}
        if method == "thread/start":
            tid = str(uuid.uuid4())
            THREADS[tid] = {"turns": [], "times": {}, "subs": {conn}, "params": p}
            return {"thread": {"id": tid}, "model": p.get("model") or "fake-model", "reasoningEffort": "low"}
        tid = p.get("threadId")
        if method == "x/endJob":
            for iid, j in list(JOBS.items()):
                turn = find_turn(j["thread"], j["turn"])
                j["item"].update(status="completed" if p.get("code", 0) == 0 else "failed", exitCode=p.get("code", 0))
                complete_item(j["thread"], turn, j["item"])
                JOBS.pop(iid)
            return {}
        if method == "x/finish":
            turn = active_turn(tid)
            add_item(tid, turn, {"id": new_id("am"), "type": "agentMessage", "phase": "final_answer", "text": "finished"})
            end_turn(tid, turn)
            return {}
        if tid not in THREADS:
            raise Error(f"no rollout found for thread id {tid}")
        th = THREADS[tid]
        if method == "thread/resume":
            th["subs"].add(conn)
            return {"thread": {"id": tid}, "model": "fake-model", "reasoningEffort": "low", "status": {"type": "idle"}}
        if method == "turn/start":
            text, cid = p["input"][0]["text"], p.get("clientUserMessageId")
            turn = active_turn(tid)
            if turn:
                add_item(tid, turn, user_item(text, cid))
                return {"turn": {"id": turn["id"]}}
            turn = begin_turn(tid, [user_item(text, cid)])
            threading.Timer(0.05, run_turn, (tid, turn, text)).start()
            return {"turn": {"id": turn["id"], "status": "inProgress"}}
        if method == "turn/steer":
            turn = active_turn(tid)
            if not turn:
                raise Error("no active turn to steer")
            if turn["id"] != p["expectedTurnId"]:
                raise Error(f"expected active turn id `{p['expectedTurnId']}` but found `{turn['id']}`")
            add_item(tid, turn, user_item(p["input"][0]["text"], p.get("clientUserMessageId")))
            return {"turnId": turn["id"]}
        if method == "thread/turns/list":
            turns = list(th["turns"])
            if p.get("sortDirection") == "desc":
                turns.reverse()
            start = int(p.get("cursor") or 0)
            limit = p.get("limit") or 20
            page = turns[start:start + limit]
            nxt = str(start + limit) if start + limit < len(turns) else None
            return {"data": json.loads(json.dumps(page)), "nextCursor": nxt}
        if method == "thread/items/list":
            turn = find_turn(tid, p["turnId"])
            return {"data": [{"turnId": turn["id"], "item": it, "completedAtMs": th["times"].get(it["id"])}
                             for it in turn["items"]], "nextCursor": None}
        if method == "thread/backgroundTerminals/list":
            return {"data": [{"itemId": i, "processId": j["process"], "command": j["item"]["command"]}
                             for i, j in JOBS.items() if j["thread"] == tid], "nextCursor": None}
        if method == "thread/backgroundTerminals/terminate":
            for iid, j in list(JOBS.items()):
                if j["process"] == p["processId"]:
                    j["item"].update(status="failed", exitCode=143)
                    complete_item(tid, find_turn(tid, j["turn"]), j["item"])
                    JOBS.pop(iid)
            return {}
        if method == "thread/compact/start":
            turn = begin_turn(tid, [])
            add_item(tid, turn, {"id": new_id("cc"), "type": "contextCompaction"})
            end_turn(tid, turn)
            return {}
        raise Error("unknown method " + method)


def serve(conn):
    try:
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.sock.recv(4096)
        conn.sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        conn.buf = buf.split(b"\r\n\r\n", 1)[1]
        while True:
            m = conn.recv()
            if "id" not in m:
                continue
            try:
                conn.send({"id": m["id"], "result": handle(conn, m["method"], m.get("params") or {})})
            except Error as e:
                conn.send({"id": m["id"], "error": {"code": -32600, "message": e.message}})
    except (EOFError, OSError):
        pass
    finally:
        with LOCK:
            for th in THREADS.values():
                th["subs"].discard(conn)
        conn.sock.close()


def main(argv):
    path = argv[argv.index("--listen") + 1].replace("unix://", "")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(8)
    while True:
        sock, _ = srv.accept()
        threading.Thread(target=serve, args=(Conn(sock),), daemon=True).start()


if __name__ == "__main__":
    main(sys.argv)
