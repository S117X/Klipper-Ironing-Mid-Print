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
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from iron_scheduler import (  # noqa: E402
    normalize_schedule,
    schedule_all_complete,
    watcher_lock_path,
)


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


def get_print_status() -> dict:
    try:
        resp = moonraker_get(
            "/printer/objects/query?print_stats&virtual_sdcard&gcode_move"
        )
        return resp["result"]["status"]
    except (KeyError, urllib.error.URLError, json.JSONDecodeError):
        return {}


def get_current_layer(cache: dict, status: dict | None = None) -> int:
    try:
        status = status or get_print_status()
        print_stats = status.get("print_stats", {})
        info = print_stats.get("info", {})
        cur = int(info.get("current_layer") or 0)
        if cur > 0:
            return cur

        vsd = status.get("virtual_sdcard", {})
        fp = int(vsd.get("file_position") or 0)
        if not cache.get("layer_byte_offsets") or fp <= 0:
            return 0

        state = str(print_stats.get("state") or "")
        if state in ("printing", "paused") or vsd.get("is_active"):
            return layer_from_file_position(cache, fp)
    except (KeyError, TypeError, ValueError):
        return 0
    return 0


def get_file_position(status: dict | None = None) -> int:
    try:
        status = status or get_print_status()
        return int(status.get("virtual_sdcard", {}).get("file_position") or 0)
    except (KeyError, TypeError, ValueError):
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


def inject_after_byte(cache: dict, object_name: str, layer: int) -> int | None:
    """Byte offset in gcode file after the object's top surface (preferred) or block."""
    objects = cache.get("objects", {})
    obj = objects.get(object_name)
    if not obj:
        folded = {k.casefold(): k for k in objects}
        key = folded.get(object_name.casefold())
        if key:
            obj = objects[key]
    if not obj:
        return None

    offsets = obj.get("inject_after_byte") or {}
    if str(layer) in offsets:
        return int(offsets[str(layer)])

    layer_offsets = cache.get("layer_byte_offsets") or {}
    if str(layer) in layer_offsets:
        return int(layer_offsets[str(layer)])
    return None


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

    lock_path = watcher_lock_path(gcode_file)
    try:
        lock_path.write_text(str(os.getpid()))
    except OSError:
        pass

    try:
        initial = normalize_schedule(json.loads(schedule_path.read_text()))
    except (json.JSONDecodeError, OSError):
        initial = {"objects": {}}

    log(
        f"watcher started file={gcode_file.name} "
        f"objects={list((initial.get('objects') or {}).keys())}"
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
        lock_path.unlink(missing_ok=True)
        return 1

    last_logged_layer = -1
    stale_layer_polls = 0
    waiting_logged: set[tuple[str, int]] = set()
    try:
        while True:
            state = get_print_state()
            if state not in ("printing", "paused"):
                if schedule_path.is_file():
                    schedule_path.unlink(missing_ok=True)
                log(f"watcher exit: print ended state={state} file={gcode_file.name}")
                break

            schedule = normalize_schedule(json.loads(schedule_path.read_text()))
            if not schedule.get("active"):
                time.sleep(1.5)
                continue

            status = get_print_status()
            layer = get_current_layer(cache, status)
            file_pos = get_file_position(status)
            if layer != last_logged_layer and layer > 0:
                log(
                    f"layer watch file={gcode_file.name} layer={layer} "
                    f"file_pos={file_pos}"
                )
                last_logged_layer = layer
                stale_layer_polls = 0
            elif layer <= 0:
                stale_layer_polls += 1
                if stale_layer_polls in (10, 40, 80):
                    vsd = status.get("virtual_sdcard", {})
                    ps = status.get("print_stats", {})
                    log(
                        f"layer detect stuck file={gcode_file.name} "
                        f"state={ps.get('state')} is_active={vsd.get('is_active')} "
                        f"file_pos={vsd.get('file_position')}"
                    )

            changed = False
            for obj_name, obj_sched in (schedule.get("objects") or {}).items():
                done = list(obj_sched.get("done") or [])
                for target in obj_sched.get("layers") or []:
                    if target in done:
                        continue
                    if layer < target:
                        continue

                    trigger_byte = inject_after_byte(cache, obj_name, target)
                    wait_key = (obj_name, target)
                    if trigger_byte is not None and file_pos < trigger_byte:
                        if wait_key not in waiting_logged:
                            log(
                                f"waiting top surface file={gcode_file.name} "
                                f"object={obj_name} layer={target} "
                                f"file_pos={file_pos} need_byte={trigger_byte}"
                            )
                            waiting_logged.add(wait_key)
                        continue

                    log(
                        f"inject {gcode_file.name} object={obj_name} layer={target} "
                        f"(detected_layer={layer} file_pos={file_pos} "
                        f"trigger_byte={trigger_byte})"
                    )
                    proc = subprocess.run(
                        [
                            sys.executable,
                            str(inject),
                            "--file",
                            str(gcode_file),
                            "--object",
                            obj_name,
                            "--layer",
                            str(target),
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if proc.returncode == 0:
                        done.append(target)
                        obj_sched["done"] = done
                        schedule["objects"][obj_name] = obj_sched
                        changed = True
                        waiting_logged.discard(wait_key)
                        log(f"inject ok object={obj_name} layer={target}")
                    else:
                        err = (proc.stderr or proc.stdout or "unknown error").strip()
                        log(f"inject failed object={obj_name} layer={target}: {err}")

            if changed:
                schedule_path.write_text(json.dumps(schedule, indent=2))

            if schedule_all_complete(schedule):
                schedule["active"] = False
                schedule_path.write_text(json.dumps(schedule, indent=2))

            time.sleep(1.5)
    finally:
        lock_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())