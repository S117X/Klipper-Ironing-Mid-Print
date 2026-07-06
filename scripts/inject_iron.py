#!/usr/bin/env python3
"""Stream cached per-object iron gcode to Klipper via Moonraker."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

PRINTER_DATA = Path(os.environ.get("PRINTER_DATA", "/home/x/printer_data"))
CACHE_DIR = PRINTER_DATA / "iron_cache"
MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")
IRON_SCRIPT_TIMEOUT = 300


def moonraker_get(path: str) -> dict:
    req = urllib.request.Request(f"{MOONRAKER_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def moonraker_post(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        f"{MOONRAKER_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
        return json.loads(body) if body else {}


def print_snapshot() -> dict:
    try:
        resp = moonraker_get("/printer/objects/query?print_stats&virtual_sdcard")
        return resp.get("result", {}).get("status", {})
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        return {}


def print_state() -> str:
    return str(print_snapshot().get("print_stats", {}).get("state") or "")


def moonraker_script(script: str) -> None:
    payload = json.dumps({"script": script}).encode()
    req = urllib.request.Request(
        f"{MOONRAKER_URL}/printer/gcode/script",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=IRON_SCRIPT_TIMEOUT) as resp:
        resp.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="G-code filename")
    parser.add_argument("--object", required=True)
    parser.add_argument("--layer", type=int, required=True)
    args = parser.parse_args()

    state = print_state()
    if state in ("complete", "cancelled", "standby", "error"):
        raise SystemExit(f"Refusing iron inject: print state is {state or 'unknown'}")

    cache_path = CACHE_DIR / f"{Path(args.file).name}.json"
    if not cache_path.is_file():
        raise SystemExit(f"Cache missing: {cache_path}")

    cache = json.loads(cache_path.read_text())
    objects = cache.get("objects", {})
    obj = objects.get(args.object)
    if not obj:
        folded = {k.casefold(): k for k in objects}
        key = folded.get(args.object.casefold())
        if key:
            obj = objects[key]
    if not obj:
        raise SystemExit(f"Object not in cache: {args.object}")

    snippet = obj.get("layers", {}).get(str(args.layer))
    if not snippet:
        raise SystemExit(f"No iron gcode for layer {args.layer}")

    paused_for_iron = False
    if state == "printing":
        moonraker_post("/printer/print/pause")
        paused_for_iron = True

    script_lines = [
        f"; IRON object={args.object} layer={args.layer}",
        "SAVE_GCODE_STATE NAME=IRON_STATE",
    ]
    for line in snippet.splitlines():
        line = line.strip()
        if line:
            script_lines.append(line)
    script_lines.append("RESTORE_GCODE_STATE NAME=IRON_STATE MOVE=1")

    try:
        moonraker_script("\n".join(script_lines) + "\n")
    finally:
        if paused_for_iron:
            after = print_state()
            if after in ("printing", "paused"):
                try:
                    moonraker_post("/printer/print/resume")
                except urllib.error.HTTPError:
                    pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())