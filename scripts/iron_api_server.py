#!/usr/bin/env python3
"""Small HTTP API for iron enable — bypasses Klipper gcode queue during prints."""

from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).with_name("iron_scheduler.py")
HOST = "127.0.0.1"
PORT = 8765


class IronApiHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"result": {"ok": True}})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/enable":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return

        filename = body.get("file") or body.get("filename")
        obj = body.get("object")
        mode = body.get("mode", "topmost")
        if not filename or not obj:
            self._send_json(400, {"error": "file and object required"})
            return

        cmd = [
            sys.executable,
            str(SCRIPT),
            "enable",
            "--file",
            str(filename),
            "--object",
            str(obj),
            "--mode",
            str(mode),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": "iron enable timed out"})
            return

        stdout = (proc.stdout or "").strip()
        result = None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if result is None:
            result = {
                "ok": False,
                "error": proc.stderr.strip() or stdout or f"exit {proc.returncode}",
            }
        code = 200 if result.get("ok") else 400
        self._send_json(code, {"result": result})

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> int:
    server = HTTPServer((HOST, PORT), IronApiHandler)
    print(f"iron_api_server listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())