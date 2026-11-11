"""Minimal MCP stdio client used to smoke-test ignou-tools."""
import json
import subprocess
import sys
import os

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")


class C:
    def __init__(self):
        self.p = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.id = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.id += 1
            msg["id"] = self.id
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server closed: " + self.p.stderr.read()[:800])
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    def init(self):
        r = self.send("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "1"},
        })
        self.send("notifications/initialized", {}, notify=True)
        return r

    def call(self, name, args):
        r = self.send("tools/call", {"name": name, "arguments": args})
        if "error" in r:
            return "ERROR " + json.dumps(r["error"])[:300]
        out = r["result"].get("content", [])
        return "".join(c.get("text", "") for c in out)

    def close(self):
        self.p.terminate()
