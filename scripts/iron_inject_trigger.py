#!/usr/bin/env python3
"""Fire scheduled iron injects at gcode trigger bytes — not a session guard."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PRINTER_DATA = Path(os.environ.get("PRINTER_DATA", "/home/x/printer_data"))
CACHE_DIR = PRINTER_DATA / "iron_cache"
MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")
SCRIPT_DIR = Path(__file__).resolve().parent
TRIGGER_SPIN_GAP = 2000
SPIN_INTERVAL = 0.005

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import inject_iron  # noqa: E402

from iron_scheduler import (  # noqa: E402
    EOF_POLL_GAP,
    FAST_POLL_GAP,
    FAST_POLL_INTERVAL,
    ULTRA_POLL_INTERVAL,
    normalize_schedule,
    print_job_active,
    print_job_finished,
    schedule_all_complete,
    trigger_lock_path,
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

        if print_job_active(status):
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


def pending_inject_targets(
    cache: dict,
    schedule: dict,
    layer: int,
    file_pos: int,
    failed_logged: set[tuple[str, int]],
) -> tuple[list[tuple[int, str, int]], list[tuple[int, str, int]]]:
    """Return (waiting_for_trigger, ready_now) sorted by gcode byte order."""
    waiting: list[tuple[int, str, int]] = []
    ready: list[tuple[int, str, int]] = []
    layer_pending: list[tuple[int, str, int]] = []

    for obj_name, obj_sched in (schedule.get("objects") or {}).items():
        done = set(obj_sched.get("done") or [])
        for target in obj_sched.get("layers") or []:
            if target in done or layer < target:
                continue
            wait_key = (obj_name, target)
            if wait_key in failed_logged:
                continue
            trigger_byte = inject_after_byte(cache, obj_name, target)
            if trigger_byte is None:
                entry = (0, obj_name, target)
                layer_pending.append(entry)
                continue
            entry = (int(trigger_byte), obj_name, target)
            layer_pending.append(entry)
            if file_pos < int(trigger_byte):
                waiting.append(entry)

    if not layer_pending:
        return waiting, ready

    if len(layer_pending) > 1:
        if all(file_pos >= entry[0] for entry in layer_pending):
            ready = sorted(layer_pending, key=lambda item: item[0])
        else:
            waiting = sorted(layer_pending, key=lambda item: item[0])
        return waiting, ready

    entry = layer_pending[0]
    if file_pos >= entry[0]:
        ready = [entry]
    else:
        waiting = [entry]
    return waiting, ready


def last_pending_trigger(waiting: list[tuple[int, str, int]]) -> int:
    triggers = [entry[0] for entry in waiting if entry[0] > 0]
    return max(triggers) if triggers else 0


def schedule_has_pending_iron(schedule: dict, layer: int) -> bool:
    for obj_sched in (schedule.get("objects") or {}).values():
        done = set(obj_sched.get("done") or [])
        for target in obj_sched.get("layers") or []:
            if target not in done and layer >= target:
                return True
    return False


def poll_interval(
    schedule: dict,
    layer: int,
    *,
    cache: dict | None = None,
    file_pos: int = 0,
    waiting: list[tuple[int, str, int]] | None = None,
) -> float:
    if schedule_has_pending_iron(schedule, layer):
        if cache and waiting:
            pe = cache.get("print_end_byte")
            if pe is not None and int(pe) - file_pos <= EOF_POLL_GAP:
                return ULTRA_POLL_INTERVAL
            next_byte = min((entry[0] for entry in waiting if entry[0] > 0), default=0)
            if next_byte and next_byte - file_pos <= FAST_POLL_GAP:
                return ULTRA_POLL_INTERVAL
        return FAST_POLL_INTERVAL
    return 1.5


def log_line(log_path: Path, msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with log_path.open("a") as fh:
            fh.write(line)
    except OSError:
        pass


def attempt_inject_for_schedule(
    gcode_file: Path,
    schedule_path: Path,
    *,
    log_path: Path | None = None,
) -> bool:
    """Inject immediately if trigger bytes already passed. Returns True if fired."""
    log = log_path or (CACHE_DIR / "iron_watcher.log")
    if not schedule_path.is_file():
        return False
    try:
        schedule = normalize_schedule(json.loads(schedule_path.read_text()))
    except (json.JSONDecodeError, OSError):
        return False
    if not schedule.get("active"):
        return False

    cache = load_cache(gcode_file)
    if not cache:
        return False

    status = get_print_status()
    if not print_job_active(status):
        return False

    layer = get_current_layer(cache, status)
    file_pos = get_file_position(status)
    waiting, ready = pending_inject_targets(cache, schedule, layer, file_pos, set())
    if not ready:
        return False

    fired = _fire_ready_batch(
        gcode_file,
        schedule_path,
        cache,
        schedule,
        ready,
        layer,
        file_pos,
        failed_logged=set(),
        log_path=log,
    )
    if fired:
        log_line(log, f"inject immediate file={gcode_file.name} file_pos={file_pos}")
    return fired


def _fire_ready_batch(
    gcode_file: Path,
    schedule_path: Path,
    cache: dict,
    schedule: dict,
    ready: list[tuple[int, str, int]],
    layer: int,
    file_pos: int,
    *,
    failed_logged: set[tuple[str, int]],
    log_path: Path,
) -> bool:
    status = get_print_status()
    if not print_job_active(status):
        return False

    batch: list[tuple[int, str, int]] = []
    for trigger_byte, obj_name, target in ready:
        wait_key = (obj_name, target)
        if wait_key in failed_logged:
            continue
        obj_sched = schedule.get("objects", {}).get(obj_name) or {}
        if target in (obj_sched.get("done") or []):
            continue
        if target in (obj_sched.get("launching") or []):
            continue
        batch.append((trigger_byte, obj_name, target))

    if not batch:
        return False

    claimed: list[tuple[int, str, int]] = []
    for entry in batch:
        _, obj_name, target = entry
        if _claim_inject_target(schedule_path, obj_name, target):
            claimed.append(entry)
        else:
            for _, n, t in claimed:
                _clear_launching(schedule_path, n, t)
            return False

    names = [f"{name}:L{target}" for _, name, target in claimed]
    first_trigger = claimed[0][0]
    last_trigger = claimed[-1][0]
    multi = len(claimed) > 1
    pre_hold = multi
    if not multi:
        _, name, target = claimed[0]
        obj = cache.get("objects", {}).get(name) or {}
        tb = (obj.get("inject_after_byte") or {}).get(str(target))
        pre_hold = inject_iron.object_near_print_end(cache, obj, target, tb)

    log_line(
        log_path,
        f"inject chain {gcode_file.name} objects={names} "
        f"(detected_layer={layer} file_pos={file_pos} "
        f"first_trigger={first_trigger} last_trigger={last_trigger})",
    )

    rc = 0
    err = ""
    try:
        inject_iron.inject_chain(
            gcode_file.name,
            [(name, target) for _, name, target in claimed],
            trigger_byte=first_trigger,
            last_trigger_byte=last_trigger,
            pre_hold_sd=pre_hold,
        )
    except SystemExit as exc:
        rc = int(exc.code) if isinstance(exc.code, int) else 1
        err = str(exc)

    for _, obj_name, target in claimed:
        _mark_inject_done(
            schedule_path,
            gcode_file,
            (obj_name, target),
            obj_name,
            target,
            rc,
            err,
            failed_logged,
            log_path,
        )

    try:
        schedule = normalize_schedule(json.loads(schedule_path.read_text()))
    except (json.JSONDecodeError, OSError):
        return rc == 0
    if schedule_all_complete(schedule):
        schedule["active"] = False
        schedule_path.write_text(json.dumps(schedule, indent=2))
    return rc == 0


def _claim_inject_target(schedule_path: Path, obj_name: str, target: int) -> bool:
    try:
        schedule = normalize_schedule(json.loads(schedule_path.read_text()))
    except (json.JSONDecodeError, OSError):
        return False
    obj_sched = schedule.get("objects", {}).get(obj_name)
    if not obj_sched:
        return False
    done = set(obj_sched.get("done") or [])
    launching = set(obj_sched.get("launching") or [])
    if target in done or target in launching:
        return False
    launching.add(target)
    obj_sched["launching"] = sorted(launching)
    schedule["objects"][obj_name] = obj_sched
    schedule_path.write_text(json.dumps(schedule, indent=2))
    return True


def _clear_launching(schedule_path: Path, obj_name: str, target: int) -> None:
    try:
        schedule = normalize_schedule(json.loads(schedule_path.read_text()))
    except (json.JSONDecodeError, OSError):
        return
    obj_sched = schedule.get("objects", {}).get(obj_name)
    if not obj_sched:
        return
    obj_sched["launching"] = [
        layer for layer in (obj_sched.get("launching") or []) if layer != target
    ]
    schedule["objects"][obj_name] = obj_sched
    schedule_path.write_text(json.dumps(schedule, indent=2))


def _mark_inject_done(
    schedule_path: Path,
    gcode_file: Path,
    wait_key: tuple[str, int],
    obj_name: str,
    target: int,
    rc: int,
    err: str,
    failed_logged: set[tuple[str, int]],
    log_path: Path,
) -> None:
    _clear_launching(schedule_path, obj_name, target)

    if rc != 0:
        if len(err) > 400:
            err = err[:400] + "..."
        failed_logged.add(wait_key)
        log_line(log_path, f"inject failed object={obj_name} layer={target}: {err}")
        return

    if str(get_print_status().get("print_stats", {}).get("state") or "") == "complete":
        failed_logged.add(wait_key)
        log_line(
            log_path,
            f"inject suspect object={obj_name} layer={target}: print already complete",
        )
        return

    try:
        schedule = normalize_schedule(json.loads(schedule_path.read_text()))
    except (json.JSONDecodeError, OSError):
        return
    obj_sched = schedule.get("objects", {}).get(obj_name)
    if not obj_sched:
        return
    done = list(obj_sched.get("done") or [])
    if target not in done:
        done.append(target)
    obj_sched["done"] = done
    schedule["objects"][obj_name] = obj_sched
    schedule_path.write_text(json.dumps(schedule, indent=2))
    log_line(log_path, f"inject ok object={obj_name} layer={target}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--schedule", required=True)
    args = parser.parse_args()

    schedule_path = Path(args.schedule)
    gcode_file = Path(args.file)
    log_path = CACHE_DIR / "iron_watcher.log"
    lock_path = trigger_lock_path(gcode_file)

    def log(msg: str) -> None:
        log_line(log_path, msg)

    try:
        lock_path.write_text(str(os.getpid()))
    except OSError:
        pass

    try:
        initial = normalize_schedule(json.loads(schedule_path.read_text()))
    except (json.JSONDecodeError, OSError):
        initial = {"objects": {}}

    log(
        f"trigger started file={gcode_file.name} "
        f"objects={list((initial.get('objects') or {}).keys())}"
    )

    failed_logged: set[tuple[str, int]] = set()
    waiting_logged: set[tuple[str, int]] = set()
    last_logged_layer = -1

    try:
        while True:
            if not schedule_path.is_file():
                log(f"trigger exit: schedule removed file={gcode_file.name}")
                break

            try:
                schedule = normalize_schedule(json.loads(schedule_path.read_text()))
            except (json.JSONDecodeError, OSError):
                log("trigger exit: schedule unreadable")
                break

            if not schedule.get("active"):
                log(f"trigger exit: schedule inactive file={gcode_file.name}")
                break

            cache = load_cache(gcode_file)
            if not cache:
                time.sleep(0.5)
                continue

            status = get_print_status()
            if not print_job_active(status):
                log("trigger exit: print not active")
                break

            layer = get_current_layer(cache, status)
            file_pos = get_file_position(status)
            pe = int(cache.get("print_end_byte") or 0)

            if layer != last_logged_layer and layer > 0:
                log(
                    f"layer watch file={gcode_file.name} layer={layer} "
                    f"file_pos={file_pos}"
                )
                last_logged_layer = layer

            waiting, ready = pending_inject_targets(
                cache, schedule, layer, file_pos, failed_logged
            )

            for trigger_byte, obj_name, target in waiting:
                wait_key = (obj_name, target)
                if wait_key not in waiting_logged:
                    log(
                        f"waiting top surface file={gcode_file.name} "
                        f"object={obj_name} layer={target} "
                        f"file_pos={file_pos} need_byte={trigger_byte}"
                    )
                    waiting_logged.add(wait_key)

            if ready:
                if _fire_ready_batch(
                    gcode_file,
                    schedule_path,
                    cache,
                    schedule,
                    ready,
                    layer,
                    file_pos,
                    failed_logged=failed_logged,
                    log_path=log_path,
                ):
                    log(f"trigger exit: inject done file={gcode_file.name}")
                    return 0
                log(f"trigger exit: inject failed file={gcode_file.name}")
                return 1

            if pe and file_pos >= pe:
                log(
                    f"trigger exit: missed PRINT_END file_pos={file_pos} "
                    f"print_end={pe}"
                )
                return 1

            spin_target = last_pending_trigger(waiting)
            if spin_target and file_pos >= spin_target - TRIGGER_SPIN_GAP:
                log(
                    f"spin wait file={gcode_file.name} file_pos={file_pos} "
                    f"need_byte={spin_target}"
                )
                while print_job_active(get_print_status()):
                    status = get_print_status()
                    file_pos = get_file_position(status)
                    if pe and file_pos >= pe:
                        log(f"spin abort at PRINT_END file_pos={file_pos}")
                        break
                    try:
                        schedule = normalize_schedule(
                            json.loads(schedule_path.read_text())
                        )
                    except (json.JSONDecodeError, OSError):
                        break
                    if not schedule.get("active"):
                        break
                    layer = get_current_layer(cache, status)
                    _, ready = pending_inject_targets(
                        cache, schedule, layer, file_pos, failed_logged
                    )
                    if ready:
                        if _fire_ready_batch(
                            gcode_file,
                            schedule_path,
                            cache,
                            schedule,
                            ready,
                            layer,
                            file_pos,
                            failed_logged=failed_logged,
                            log_path=log_path,
                        ):
                            log(f"trigger exit: inject done (spin) file={gcode_file.name}")
                            return 0
                        return 1
                    time.sleep(SPIN_INTERVAL)
                return 1

            time.sleep(
                poll_interval(
                    schedule,
                    layer,
                    cache=cache,
                    file_pos=file_pos,
                    waiting=waiting,
                )
            )
    finally:
        lock_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())