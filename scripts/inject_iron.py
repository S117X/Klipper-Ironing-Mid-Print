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


def print_state() -> str:
    try:
        resp = moonraker_get("/printer/objects/query?print_stats")
        return str(
            resp.get("result", {})
            .get("status", {})
            .get("print_stats", {})
            .get("state")
            or ""
        )
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        return ""


def file_position() -> int:
    try:
        resp = moonraker_get("/printer/objects/query?virtual_sdcard")
        return int(
            resp.get("result", {})
            .get("status", {})
            .get("virtual_sdcard", {})
            .get("file_position")
            or 0
        )
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return 0


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

    print_end_byte = cache.get("print_end_byte")
    min_margin = int(cache.get("min_inject_margin") or 128)
    print_end_margin = int(cache.get("print_end_margin") or 2000)
    trigger_byte = (obj.get("inject_after_byte") or {}).get(str(args.layer))
    file_pos = file_position()

    if print_end_byte is not None:
        room = int(print_end_byte) - file_pos
        if room < min_margin:
            raise SystemExit(
                f"Refusing iron inject: file_pos={file_pos} only {room} bytes "
                f"before PRINT_END at {print_end_byte}"
            )

    near_print_end = bool(
        (obj.get("inject_near_print_end") or {}).get(str(args.layer))
    )
    if not near_print_end and print_end_byte is not None and trigger_byte is not None:
        near_print_end = int(print_end_byte) - int(trigger_byte) <= print_end_margin

    script_lines = [
        f"; IRON object={args.object} layer={args.layer}",
        "SAVE_GCODE_STATE NAME=IRON_STATE",
    ]
    for line in snippet.splitlines():
        line = line.strip()
        if line:
            script_lines.append(line)
    # MOVE=0: restore coords/feedrate but do NOT jump back to the saved
    # toolhead position (MOVE=1 caused back-and-forth between objects).
    script_lines.append("RESTORE_GCODE_STATE NAME=IRON_STATE MOVE=0")
    if near_print_end:
        script_lines.append("PRINT_END")
    moonraker_script("\n".join(script_lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())