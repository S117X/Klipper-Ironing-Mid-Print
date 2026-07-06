#!/usr/bin/env python3
"""Watch print layer changes and inject scheduled per-object ironing."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
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

import inject_iron  # noqa: E402

from iron_scheduler import (  # noqa: E402
    EOF_POLL_GAP,
    FAST_POLL_GAP,
    FAST_POLL_INTERVAL,
    ULTRA_POLL_INTERVAL,
    cleanup_watcher_artifacts,
    normalize_schedule,
    print_job_active,
    print_job_finished,
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

    # Multi-object same layer: wait until every trigger has passed, then
    # inject all cubes in one Klipper script (prevents SD hitting PRINT_END
    # while the first cube is still ironing).
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
    inflight: set[tuple[str, int]],
    *,
    cache: dict | None = None,
    file_pos: int = 0,
    waiting: list[tuple[int, str, int]] | None = None,
) -> float:
    """Fast poll while injects are outstanding or approaching triggers/EOF."""
    if inflight or schedule_has_pending_iron(schedule, layer):
        if cache and waiting:
            pe = cache.get("print_end_byte")
            if pe is not None and int(pe) - file_pos <= EOF_POLL_GAP:
                return ULTRA_POLL_INTERVAL
            next_byte = min((entry[0] for entry in waiting if entry[0] > 0), default=0)
            if next_byte and next_byte - file_pos <= FAST_POLL_GAP:
                return ULTRA_POLL_INTERVAL
        return FAST_POLL_INTERVAL
    return 1.5


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--schedule", required=True)
    args = parser.parse_args()

    schedule_path = Path(args.schedule)
    gcode_file = Path(args.file)
    log_path = CACHE_DIR / "iron_watcher.log"

    def log(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        try:
            with log_path.open("a") as fh:
                fh.write(line)
        except OSError:
            pass

    def stop_watching(reason: str) -> None:
        cleanup_watcher_artifacts(gcode_file, schedule_path)
        log(f"watcher exit: {reason} file={gcode_file.name}")

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
        f"objects={list((initial.get('objects') or {}).keys())} "
        f"mode=async_immediate_inject"
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
    stale_layer_polls = 0
    waiting_logged: set[tuple[str, int]] = set()
    failed_logged: set[tuple[str, int]] = set()
    inflight: set[tuple[str, int]] = set()
    launched: set[tuple[str, int]] = set()
    session_done: set[tuple[str, int]] = set()
    inflight_lock = threading.Lock()
    schedule_lock = threading.Lock()

    def claim_inject_target(obj_name: str, target: int) -> bool:
        """Persist launching on disk so duplicate watchers cannot double-inject."""
        wait_key = (obj_name, target)
        with schedule_lock:
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
        with inflight_lock:
            launched.add(wait_key)
            inflight.add(wait_key)
        return True

    def mark_inject_done(
        wait_key: tuple[str, int],
        obj_name: str,
        target: int,
        rc: int,
        err: str,
    ) -> None:
        with schedule_lock:
            try:
                schedule = normalize_schedule(json.loads(schedule_path.read_text()))
            except (json.JSONDecodeError, OSError):
                schedule = None
            if schedule is not None:
                obj_sched = schedule.get("objects", {}).get(obj_name)
                if obj_sched:
                    launching = [
                        layer
                        for layer in (obj_sched.get("launching") or [])
                        if layer != target
                    ]
                    obj_sched["launching"] = launching
                    schedule["objects"][obj_name] = obj_sched
                    schedule_path.write_text(json.dumps(schedule, indent=2))

        with inflight_lock:
            inflight.discard(wait_key)

        if rc != 0:
            if len(err) > 400:
                err = err[:400] + "..."
            failed_logged.add(wait_key)
            log(f"inject failed object={obj_name} layer={target}: {err}")
            return

        status = get_print_status()
        ps = status.get("print_stats", {})
        if str(ps.get("state") or "") == "complete":
            failed_logged.add(wait_key)
            log(
                f"inject suspect object={obj_name} layer={target}: "
                "print already complete"
            )
            return

        with schedule_lock:
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
            obj_sched["launching"] = [
                layer
                for layer in (obj_sched.get("launching") or [])
                if layer != target
            ]
            schedule["objects"][obj_name] = obj_sched
            schedule_path.write_text(json.dumps(schedule, indent=2))
        with inflight_lock:
            session_done.add(wait_key)
        waiting_logged.discard(wait_key)
        log(f"inject ok object={obj_name} layer={target}")

    def run_chain_inject(
        batch: list[tuple[int, str, int]],
        layer: int,
        file_pos: int,
        *,
        sync: bool,
    ) -> None:
        """One Klipper script for all ready objects — avoids SD racing to PRINT_END."""
        names = [f"{name}:L{target}" for _, name, target in batch]
        first_trigger = batch[0][0]
        last_trigger = batch[-1][0]
        multi = len(batch) > 1

        def worker() -> None:
            rc = 0
            err = ""
            try:
                inject_iron.inject_chain(
                    gcode_file.name,
                    [(name, target) for _, name, target in batch],
                    trigger_byte=first_trigger,
                    last_trigger_byte=last_trigger,
                    pre_hold_sd=multi,
                )
            except SystemExit as exc:
                rc = int(exc.code) if isinstance(exc.code, int) else 1
                err = str(exc)
            for _, obj_name, target in batch:
                mark_inject_done((obj_name, target), obj_name, target, rc, err)

        log(
            f"inject chain {gcode_file.name} objects={names} "
            f"(detected_layer={layer} file_pos={file_pos} "
            f"first_trigger={first_trigger} last_trigger={last_trigger} "
            f"sync={sync})"
        )
        if sync:
            worker()
        else:
            threading.Thread(
                target=worker, daemon=True, name=f"iron-chain-{batch[0][1]}"
            ).start()

    try:
        while True:
            status = get_print_status()
            if print_job_active(status):
                seen_printing = True

            if not schedule_path.is_file():
                log(f"watcher exit: schedule removed file={gcode_file.name}")
                break

            try:
                schedule = normalize_schedule(json.loads(schedule_path.read_text()))
            except (json.JSONDecodeError, OSError):
                stop_watching("schedule unreadable")
                break

            if not schedule.get("active"):
                time.sleep(1.5)
                continue

            cache = load_cache(gcode_file)
            if not cache:
                time.sleep(1.5)
                continue

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

            launched_this_loop = False
            # Fire inject as soon as file_pos passes each object's trigger.
            # Non-blocking: SD keeps advancing while Klipper runs iron, so the
            # watcher must keep polling and queue the next object immediately.
            if ready:
                status = get_print_status()
                finished, reason = print_job_finished(
                    status, gcode_file, seen_printing=seen_printing
                )
                if finished and not inflight:
                    stop_watching(f"before_inject {reason}")
                    return 0

                if not print_job_active(status):
                    for _, obj_name, target in ready:
                        wait_key = (obj_name, target)
                        log(
                            f"inject skipped object={obj_name} layer={target}: "
                            "print not active"
                        )
                        failed_logged.add(wait_key)
                else:
                    file_pos = get_file_position(status)
                    batch: list[tuple[int, str, int]] = []
                    for trigger_byte, obj_name, target in ready:
                        wait_key = (obj_name, target)
                        if wait_key in failed_logged or wait_key in session_done:
                            continue
                        obj_sched = schedule.get("objects", {}).get(obj_name) or {}
                        if target in (obj_sched.get("done") or []):
                            with inflight_lock:
                                session_done.add(wait_key)
                            continue
                        if target in (obj_sched.get("launching") or []):
                            continue
                        with inflight_lock:
                            if wait_key in inflight or wait_key in launched:
                                continue
                        batch.append((trigger_byte, obj_name, target))

                    if batch:
                        claimed: list[tuple[int, str, int]] = []
                        for entry in batch:
                            _, obj_name, target = entry
                            if claim_inject_target(obj_name, target):
                                claimed.append(entry)
                            else:
                                for _, n, t in claimed:
                                    with schedule_lock:
                                        try:
                                            sched = normalize_schedule(
                                                json.loads(
                                                    schedule_path.read_text()
                                                )
                                            )
                                            o = sched.get("objects", {}).get(n)
                                            if o:
                                                o["launching"] = [
                                                    ly
                                                    for ly in (
                                                        o.get("launching") or []
                                                    )
                                                    if ly != t
                                                ]
                                                sched["objects"][n] = o
                                                schedule_path.write_text(
                                                    json.dumps(sched, indent=2)
                                                )
                                        except (json.JSONDecodeError, OSError):
                                            pass
                                claimed = []
                                break
                        if claimed:
                            run_chain_inject(
                                claimed,
                                layer,
                                file_pos,
                                sync=len(claimed) > 1,
                            )
                            launched_this_loop = True

            finished, reason = print_job_finished(
                status, gcode_file, seen_printing=seen_printing
            )
            if finished and not inflight:
                stop_watching(reason)
                break

            with schedule_lock:
                try:
                    schedule = normalize_schedule(
                        json.loads(schedule_path.read_text())
                    )
                except (json.JSONDecodeError, OSError):
                    schedule = {"objects": {}}

            if schedule_all_complete(schedule) and not inflight:
                schedule["active"] = False
                schedule_path.write_text(json.dumps(schedule, indent=2))

            finished, reason = print_job_finished(
                get_print_status(), gcode_file, seen_printing=seen_printing
            )
            if finished and "complete" in reason and not inflight:
                for _ in range(45):
                    try:
                        resp = moonraker_get("/printer/objects/query?toolhead")
                        th = resp.get("result", {}).get("status", {}).get(
                            "toolhead", {}
                        )
                        homed = str(th.get("homed_axes") or "")
                        if "x" in homed and "y" in homed:
                            break
                    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
                        pass
                    time.sleep(1.0)
                stop_watching(f"after_inject {reason}")
                return 0
            if finished and not inflight:
                stop_watching(f"after_inject {reason}")
                return 0

            if launched_this_loop or inflight:
                continue

            time.sleep(
                poll_interval(
                    schedule,
                    layer,
                    inflight,
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