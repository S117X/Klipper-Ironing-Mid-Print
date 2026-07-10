#!/usr/bin/env python3
"""Mid-print iron controller.

Failure we fix (from live log 2026-07-09 22:48–22:57)
------------------------------------------------------
1) Iron object A @ ~109932 (OK, hot)
2) M24 resume → SD runs ~18s through rest of file
3) file_pos jumps to PRINT_END (122209); only ~42 bytes after last top
4) Iron object B after PRINT_END → cold / park moves

Fix
---
* Iron first ready object with M25 + iron and **no M24** when more pending.
* Then **splice**: remaining gcode until PRINT_END + remaining irons + PRINT_END
  in one Moonraker script while SD stays paused; SDCARD_RESET_FILE after.
* Never inject if file_pos >= PRINT_END or heater target is off.
"""

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
    cleanup_watcher_artifacts,
    normalize_schedule,
    print_job_active,
    print_job_finished,
    schedule_all_complete,
    watcher_lock_path,
)

NEAR_TRIGGER_BYTES = 16384
POLL_IDLE = 0.75
POLL_NEAR = 0.05
POLL_CRITICAL = 0.02


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
            "/printer/objects/query?print_stats&virtual_sdcard&gcode_move&extruder"
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
        if cache.get("layer_byte_offsets") and fp > 0 and print_job_active(status):
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


def heaters_alive(status: dict) -> bool:
    ext = status.get("extruder") or {}
    try:
        temp = float(ext.get("temperature") or 0.0)
        target = float(ext.get("target") or 0.0)
    except (TypeError, ValueError):
        return False
    if target < 170.0:
        return False
    if temp < 170.0:
        return False
    return bool(ext.get("can_extrude")) or temp >= target - 25.0


def load_cache(gcode_file: Path) -> dict:
    cache_path = CACHE_DIR / f"{gcode_file.name}.json"
    if not cache_path.is_file():
        return {}
    return json.loads(cache_path.read_text())


def inject_after_byte(cache: dict, object_name: str, layer: int) -> int | None:
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


def print_end_byte(cache: dict, gcode_file: Path) -> int | None:
    raw = cache.get("print_end_byte")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    candidates = [gcode_file]
    if not gcode_file.is_file():
        candidates.append(PRINTER_DATA / "gcodes" / gcode_file.name)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        pos = 0
        for line in data.splitlines(keepends=True):
            stripped = line.strip()
            if stripped == b"PRINT_END" or stripped.startswith(b"PRINT_END "):
                return pos
            pos += len(line)
    return None


def pending_work(schedule: dict, cache: dict) -> list[tuple[int, str, int]]:
    items: list[tuple[int, str, int]] = []
    for obj_name, obj_sched in (schedule.get("objects") or {}).items():
        done = set(obj_sched.get("done") or [])
        for target in obj_sched.get("layers") or []:
            if target in done:
                continue
            tb = inject_after_byte(cache, obj_name, int(target))
            items.append((tb if tb is not None else 0, obj_name, int(target)))
    items.sort(key=lambda x: (x[0], x[1], x[2]))
    return items


def classify_ready(
    pending: list[tuple[int, str, int]],
    *,
    layer: int,
    file_pos: int,
) -> tuple[list[tuple[int, str, int]], list[tuple[int, str, int]]]:
    ready: list[tuple[int, str, int]] = []
    waiting: list[tuple[int, str, int]] = []
    for trigger_byte, obj_name, target in pending:
        if trigger_byte and file_pos < trigger_byte:
            waiting.append((trigger_byte, obj_name, target))
            continue
        if not trigger_byte and layer < target:
            waiting.append((trigger_byte, obj_name, target))
            continue
        ready.append((trigger_byte, obj_name, target))
    return ready, waiting


def mark_done(schedule: dict, pairs: list[tuple[str, int]]) -> None:
    for obj_name, target in pairs:
        obj_sched = schedule["objects"][obj_name]
        done = list(obj_sched.get("done") or [])
        if target not in done:
            done.append(target)
        obj_sched["done"] = done
        schedule["objects"][obj_name] = obj_sched


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
    end_byte = print_end_byte(cache, gcode_file)

    def log(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        try:
            with log_path.open("a") as fh:
                fh.write(line)
        except OSError:
            pass

    def stop_watching(reason: str) -> None:
        cleanup_watcher_artifacts(gcode_file, schedule_path)
        log(f"controller exit: {reason} file={gcode_file.name}")

    def run_cmd(cmd: list[str]) -> tuple[int, str]:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        err = (proc.stderr or proc.stdout or "").strip()
        if len(err) > 600:
            err = err[:600] + "..."
        return proc.returncode, err

    def iron_one_hold(obj_name: str, layer: int) -> tuple[int, str]:
        """M25 + iron, leave SD paused (no M24)."""
        return run_cmd(
            [
                sys.executable,
                str(inject),
                "--file",
                str(gcode_file),
                "--object",
                obj_name,
                "--layer",
                str(layer),
                "--sd",
                "hold",
            ]
        )

    def iron_one_full(obj_name: str, layer: int) -> tuple[int, str]:
        """M25 + iron + M24 (last / only object)."""
        return run_cmd(
            [
                sys.executable,
                str(inject),
                "--file",
                str(gcode_file),
                "--object",
                obj_name,
                "--layer",
                str(layer),
                "--sd",
                "full",
            ]
        )

    def splice_remaining(pairs: list[tuple[str, int]], from_byte: int) -> tuple[int, str]:
        cmd = [
            sys.executable,
            str(inject),
            "--file",
            str(gcode_file),
            "--batch",
            json.dumps([[o, ly] for o, ly in pairs]),
            "--splice-rest",
            "--from-byte",
            str(from_byte),
        ]
        if end_byte is not None:
            cmd.extend(["--print-end-byte", str(end_byte)])
        return run_cmd(cmd)

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
        f"controller started file={gcode_file.name} "
        f"objects={list((initial.get('objects') or {}).keys())} "
        f"n={len(initial.get('objects') or {})} policy=asap+splice-rest "
        f"print_end_byte={end_byte}"
    )

    seen_printing = False
    for _ in range(120):
        status = get_print_status()
        if print_job_active(status):
            seen_printing = True
            break
        finished, reason = print_job_finished(status, gcode_file, seen_printing=False)
        if finished:
            stop_watching(f"before_start {reason}")
            return 1
        time.sleep(0.5)
    else:
        stop_watching("print never started")
        return 1

    last_logged_layer = -1
    waiting_logged: set[tuple[str, int]] = set()
    failed_logged: set[tuple[str, int]] = set()

    try:
        while True:
            status = get_print_status()
            if print_job_active(status):
                seen_printing = True

            finished, reason = print_job_finished(
                status, gcode_file, seen_printing=seen_printing
            )
            if finished:
                stop_watching(reason)
                break

            if not schedule_path.is_file():
                log(f"controller exit: schedule removed file={gcode_file.name}")
                break

            try:
                schedule = normalize_schedule(json.loads(schedule_path.read_text()))
            except (json.JSONDecodeError, OSError):
                stop_watching("schedule unreadable")
                break

            if not schedule.get("active"):
                time.sleep(POLL_IDLE)
                continue

            layer = get_current_layer(cache, status)
            file_pos = get_file_position(status)
            if layer != last_logged_layer and layer > 0:
                log(
                    f"layer watch file={gcode_file.name} layer={layer} "
                    f"file_pos={file_pos}"
                )
                last_logged_layer = layer

            # Hard stop: past PRINT_END with work left — do NOT cold iron.
            if end_byte is not None and file_pos >= end_byte:
                pending = pending_work(schedule, cache)
                if pending:
                    log(
                        f"ABORT past PRINT_END file_pos={file_pos} end={end_byte} "
                        f"pending={[(o, ly) for _, o, ly in pending]} — no cold iron"
                    )
                    stop_watching("past_print_end_abort")
                    return 1

            pending = pending_work(schedule, cache)
            if not pending:
                if schedule_all_complete(schedule):
                    schedule["active"] = False
                    schedule_path.write_text(json.dumps(schedule, indent=2))
                    log(f"all iron complete file={gcode_file.name}")
                time.sleep(POLL_IDLE)
                continue

            ready, waiting = classify_ready(
                pending, layer=layer, file_pos=file_pos
            )

            for trigger_byte, obj_name, target in waiting:
                key = (obj_name, target)
                if key not in waiting_logged:
                    log(
                        f"waiting top surface file={gcode_file.name} "
                        f"object={obj_name} layer={target} "
                        f"file_pos={file_pos} need_byte={trigger_byte}"
                    )
                    waiting_logged.add(key)

            next_trigger = min((t for t, _, _ in waiting if t), default=None)
            dist_trig = (next_trigger - file_pos) if next_trigger else 10**9
            dist_end = (end_byte - file_pos) if end_byte and file_pos else 10**9
            poll_sec = POLL_IDLE
            if dist_trig < NEAR_TRIGGER_BYTES or dist_end < NEAR_TRIGGER_BYTES:
                poll_sec = POLL_NEAR
            if dist_trig < 4096 or dist_end < 4096:
                poll_sec = POLL_CRITICAL

            if not ready:
                time.sleep(poll_sec)
                continue

            # Fire earliest ready object only.
            ready = [r for r in ready if (r[1], r[2]) not in failed_logged]
            if not ready:
                time.sleep(poll_sec)
                continue

            trigger_byte, obj_name, target = ready[0]
            pairs_one = [(obj_name, target)]
            more_after = len(pending) > 1

            status = get_print_status()
            finished, reason = print_job_finished(
                status, gcode_file, seen_printing=seen_printing
            )
            if finished:
                stop_watching(f"before_inject {reason}")
                return 0

            if not heaters_alive(status):
                log(
                    f"ABORT heaters dead before inject object={obj_name} "
                    f"file_pos={get_file_position(status)}"
                )
                stop_watching("heaters_off_before_inject")
                return 1

            if end_byte is not None and get_file_position(status) >= end_byte:
                log(
                    f"ABORT at/past PRINT_END before inject object={obj_name} "
                    f"file_pos={get_file_position(status)} end={end_byte}"
                )
                stop_watching("past_print_end_before_inject")
                return 1

            if more_after:
                # Keep SD paused after this iron; splice the rest of the job.
                log(
                    f"inject HOLD (more pending) file={gcode_file.name} "
                    f"items={pairs_one} file_pos={file_pos} layer={layer}"
                )
                rc, err = iron_one_hold(obj_name, target)
                if rc != 0:
                    failed_logged.add((obj_name, target))
                    log(f"inject failed items={pairs_one}: {err}")
                    time.sleep(poll_sec)
                    continue

                mark_done(schedule, pairs_one)
                schedule_path.write_text(json.dumps(schedule, indent=2))
                log(f"inject ok (held) items={pairs_one}")

                # Still-paused position should be ~trigger of first object.
                status = get_print_status()
                held_pos = get_file_position(status)
                rest = pending_work(schedule, cache)
                rest_pairs = [(o, ly) for _, o, ly in rest]
                if not rest_pairs:
                    # Nothing else — just resume.
                    log("no remaining pairs after hold; M24 resume")
                    run_cmd(
                        [
                            sys.executable,
                            str(inject),
                            "--sd",
                            "resume",
                            "--file",
                            str(gcode_file),
                        ]
                    )
                else:
                    if not heaters_alive(status):
                        log("ABORT heaters dead before splice — no cold iron")
                        stop_watching("heaters_off_before_splice")
                        return 1
                    if end_byte is not None and held_pos >= end_byte:
                        log(
                            f"ABORT held_pos={held_pos} >= PRINT_END={end_byte} "
                            "before splice — no cold iron"
                        )
                        stop_watching("past_print_end_before_splice")
                        return 1

                    log(
                        f"SPLICE rest+iron+PRINT_END from_byte={held_pos} "
                        f"print_end={end_byte} remaining={rest_pairs}"
                    )
                    rc2, err2 = splice_remaining(rest_pairs, held_pos)
                    if rc2 != 0:
                        log(f"splice failed: {err2}")
                        # Do not M24 into PRINT_END tail — leave paused / abort.
                        stop_watching("splice_failed")
                        return 1

                    mark_done(schedule, rest_pairs)
                    schedule["active"] = False
                    schedule_path.write_text(json.dumps(schedule, indent=2))
                    log(f"splice ok remaining={rest_pairs}")
                    stop_watching("splice_complete")
                    return 0

            else:
                # Last / only object: normal M25/iron/M24.
                log(
                    f"inject FULL (last) file={gcode_file.name} "
                    f"items={pairs_one} file_pos={file_pos} layer={layer}"
                )
                rc, err = iron_one_full(obj_name, target)
                if rc != 0:
                    failed_logged.add((obj_name, target))
                    log(f"inject failed items={pairs_one}: {err}")
                else:
                    mark_done(schedule, pairs_one)
                    schedule_path.write_text(json.dumps(schedule, indent=2))
                    log(f"inject ok items={pairs_one}")
                    if schedule_all_complete(schedule):
                        schedule["active"] = False
                        schedule_path.write_text(json.dumps(schedule, indent=2))
                        log(f"all iron complete file={gcode_file.name}")
                        stop_watching("all_complete")
                        return 0

                finished, reason = print_job_finished(
                    get_print_status(), gcode_file, seen_printing=seen_printing
                )
                if finished:
                    stop_watching(f"after_inject {reason}")
                    return 0

            # Loop immediately after work.
            continue
    finally:
        lock_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
