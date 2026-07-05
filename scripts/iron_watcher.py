#!/usr/bin/env python3
"""Watch print layer changes and inject scheduled per-object ironing."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PRINTER_DATA = Path(os.environ.get("PRINTER_DATA", "/home/x/printer_data"))
CACHE_DIR = PRINTER_DATA / "iron_cache"
MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")


def moonraker_get(path: str) -> dict:
    req = urllib.request.Request(f"{MOONRAKER_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def layer_from_file_position(cache: dict, file_position: int) -> int:
    offsets = cache.get("layer_byte_offsets") or {}
    best = 0
    for layer_s, pos in offsets.items():
        if int(pos) <= file_position:
            best = max(best, int(layer_s))
    return best


def get_current_layer(cache: dict) -> int:
    try:
        resp = moonraker_get(
            "/printer/objects/query?print_stats&virtual_sdcard&gcode_move"
        )
        status = resp["result"]["status"]
        print_stats = status.get("print_stats", {})
        info = print_stats.get("info", {})
        cur = int(info.get("current_layer") or 0)
        if cur > 0:
            return cur

        vsd = status.get("virtual_sdcard", {})
        fp = int(vsd.get("file_position") or 0)
        if not cache.get("layer_byte_offsets") or fp <= 0:
            return 0

        # Do not require is_active — some hosts keep it false while printing.
        state = str(print_stats.get("state") or "")
        if state in ("printing", "paused") or vsd.get("is_active"):
            return layer_from_file_position(cache, fp)
    except (KeyError, urllib.error.URLError, json.JSONDecodeError, TypeError, ValueError):
        return 0
    return 0


def get_print_state() -> str:
    try:
        resp = moonraker_get("/printer/objects/query?print_stats")
        return resp["result"]["status"]["print_stats"].get("state", "")
    except (KeyError, urllib.error.URLError, json.JSONDecodeError):
        return ""


def load_cache(gcode_file: Path) -> dict:
    cache_path = CACHE_DIR / f"{gcode_file.name}.json"
    if not cache_path.is_file():
        return {}
    return json.loads(cache_path.read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--schedule", required=True)
    args = parser.parse_args()

    schedule_path = Path(args.schedule)
    gcode_file = Path(args.file)
    inject = Path(__file__).with_name("inject_iron.py")
    cache = load_cache(gcode_file)
    log_path = CACHE_DIR / "iron_watcher.log"

    def log(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        try:
            with log_path.open("a") as fh:
                fh.write(line)
        except OSError:
            pass

    try:
        initial = json.loads(schedule_path.read_text())
    except (json.JSONDecodeError, OSError):
        initial = {}

    log(
        f"watcher started file={gcode_file.name} object={initial.get('object')} "
        f"layers={initial.get('layers')}"
    )

    # Wait for print to start (enable may arrive before Klipper reports printing).
    for _ in range(120):
        state = get_print_state()
        if state in ("printing", "paused"):
            break
        time.sleep(0.5)
    else:
        log(f"watcher exit: print never started for {gcode_file.name}")
        schedule_path.unlink(missing_ok=True)
        return 1

    last_logged_layer = -1
    stale_layer_polls = 0
    while True:
        state = get_print_state()
        if state not in ("printing", "paused"):
            if schedule_path.is_file():
                schedule_path.unlink(missing_ok=True)
            log(f"watcher exit: print ended state={state} file={gcode_file.name}")
            break

        schedule = json.loads(schedule_path.read_text())
        if not schedule.get("active"):
            time.sleep(1.5)
            continue

        layer = get_current_layer(cache)
        if layer != last_logged_layer and layer > 0:
            log(f"layer watch file={gcode_file.name} layer={layer}")
            last_logged_layer = layer
            stale_layer_polls = 0
        elif layer <= 0:
            stale_layer_polls += 1
            if stale_layer_polls in (10, 40, 80):
                try:
                    resp = moonraker_get(
                        "/printer/objects/query?print_stats&virtual_sdcard"
                    )
                    st = resp["result"]["status"]
                    vsd = st.get("virtual_sdcard", {})
                    ps = st.get("print_stats", {})
                    log(
                        f"layer detect stuck file={gcode_file.name} "
                        f"state={ps.get('state')} is_active={vsd.get('is_active')} "
                        f"file_pos={vsd.get('file_position')}"
                    )
                except (urllib.error.URLError, json.JSONDecodeError, KeyError):
                    pass
        injected = False
        for target in schedule.get("layers", []):
            if target in schedule.get("done", []):
                continue
            if layer < target:
                continue
            log(
                f"inject {gcode_file.name} object={schedule['object']} "
                f"layer={target} (detected_layer={layer})"
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(inject),
                    "--file",
                    str(gcode_file),
                    "--object",
                    schedule["object"],
                    "--layer",
                    str(target),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                schedule.setdefault("done", []).append(target)
                schedule_path.write_text(json.dumps(schedule, indent=2))
                injected = True
                log(f"inject ok layer={target}")
            else:
                err = (proc.stderr or proc.stdout or "unknown error").strip()
                log(f"inject failed layer={target}: {err}")

        if len(schedule.get("done", [])) >= len(schedule.get("layers", [])):
            schedule["active"] = False
            schedule_path.write_text(json.dumps(schedule, indent=2))

        time.sleep(1.5)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())