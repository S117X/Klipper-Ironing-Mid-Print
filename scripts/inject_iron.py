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


def moonraker_script(script: str) -> None:
    payload = json.dumps({"script": script}).encode()
    req = urllib.request.Request(
        f"{MOONRAKER_URL}/printer/gcode/script",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="G-code filename")
    parser.add_argument("--object", required=True)
    parser.add_argument("--layer", type=int, required=True)
    args = parser.parse_args()

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

    script_lines = [
        f"; IRON object={args.object} layer={args.layer}",
        "SAVE_GCODE_STATE NAME=IRON_STATE",
    ]
    for line in snippet.splitlines():
        line = line.strip()
        if line:
            script_lines.append(line)
    script_lines.append("RESTORE_GCODE_STATE NAME=IRON_STATE MOVE=1")
    moonraker_script("\n".join(script_lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())