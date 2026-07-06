#!/usr/bin/env python3
"""Clear iron schedules when the print file/session changes — no injection."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

PRINTER_DATA = Path(os.environ.get("PRINTER_DATA", "/home/x/printer_data"))
CACHE_DIR = PRINTER_DATA / "iron_cache"
MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")
SCRIPT_DIR = Path(__file__).resolve().parent

import sys

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from iron_scheduler import (  # noqa: E402
    cleanup_session_artifacts,
    normalize_schedule,
    print_job_active,
    print_job_finished,
    session_guard_lock_path,
)


def moonraker_get(path: str) -> dict:
    req = urllib.request.Request(f"{MOONRAKER_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def get_print_status() -> dict:
    try:
        resp = moonraker_get(
            "/printer/objects/query?print_stats&virtual_sdcard&gcode_move"
        )
        return resp["result"]["status"]
    except (KeyError, urllib.error.URLError, json.JSONDecodeError):
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--schedule", required=True)
    args = parser.parse_args()

    gcode_file = Path(args.file)
    schedule_path = Path(args.schedule)
    log_path = CACHE_DIR / "iron_watcher.log"
    lock_path = session_guard_lock_path(gcode_file)

    def log(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        try:
            with log_path.open("a") as fh:
                fh.write(line)
        except OSError:
            pass

    try:
        lock_path.write_text(str(os.getpid()))
    except OSError:
        pass

    log(f"session guard started file={gcode_file.name}")

    seen_printing = False
    try:
        while True:
            status = get_print_status()
            if print_job_active(status):
                seen_printing = True

            finished, reason = print_job_finished(
                status, gcode_file, seen_printing=seen_printing
            )
            if finished:
                cleanup_session_artifacts(gcode_file, schedule_path)
                log(f"session guard exit: {reason} file={gcode_file.name}")
                return 0

            if not schedule_path.is_file():
                log(f"session guard exit: schedule removed file={gcode_file.name}")
                return 0

            try:
                schedule = normalize_schedule(json.loads(schedule_path.read_text()))
            except (json.JSONDecodeError, OSError):
                cleanup_session_artifacts(gcode_file, schedule_path)
                log("session guard exit: schedule unreadable")
                return 0

            if not schedule.get("active"):
                log(f"session guard exit: schedule inactive file={gcode_file.name}")
                return 0

            time.sleep(1.0)
    finally:
        lock_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())