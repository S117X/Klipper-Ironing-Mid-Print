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
IRON_LINE_BATCH = 12
IRON_SCRIPT_TIMEOUT = 60


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


def stream_iron_lines(snippet: str, object_name: str, layer: int) -> None:
    moves = [line.strip() for line in snippet.splitlines() if line.strip()]
    moonraker_script(
        f"; IRON object={object_name} layer={layer}\n"
        "SAVE_GCODE_STATE NAME=IRON_STATE"
    )
    batch: list[str] = []
    for line in moves:
        batch.append(line)
        if len(batch) >= IRON_LINE_BATCH:
            moonraker_script("\n".join(batch))
            batch = []
            state = print_state()
            if state in ("complete", "cancelled", "standby", "error"):
                raise SystemExit(f"Print ended during iron ({state})")
    if batch:
        moonraker_script("\n".join(batch))
    moonraker_script("RESTORE_GCODE_STATE NAME=IRON_STATE MOVE=1")


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

    stream_iron_lines(snippet, args.object, args.layer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())