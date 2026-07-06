#!/usr/bin/env python3
"""Stream cached per-object iron gcode to Klipper via Moonraker."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

PRINTER_DATA = Path(os.environ.get("PRINTER_DATA", "/home/x/printer_data"))
CACHE_DIR = PRINTER_DATA / "iron_cache"
MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")
IRON_SCRIPT_TIMEOUT = 300
LIVE_LOG = CACHE_DIR / "iron_live.log"


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


def toolhead_xy() -> tuple[float, float] | None:
    try:
        resp = moonraker_get("/printer/objects/query?toolhead")
        pos = (
            resp.get("result", {})
            .get("status", {})
            .get("toolhead", {})
            .get("position")
        )
        if pos and len(pos) >= 2:
            return float(pos[0]), float(pos[1])
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return None


def moonraker_script(script: str, *, timeout: float = IRON_SCRIPT_TIMEOUT) -> None:
    payload = json.dumps({"script": script}).encode()
    req = urllib.request.Request(
        f"{MOONRAKER_URL}/printer/gcode/script",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def pause_sd() -> None:
    """Pause virtual SD immediately — must run before SD reaches PRINT_END."""
    moonraker_script("M25", timeout=10)


def iron_move_stats(snippet: str) -> dict[str, float | int]:
    moves = 0
    iron_feed = 0.0
    y_vals: list[float] = []
    for line in snippet.splitlines():
        stripped = line.strip()
        if not stripped.startswith("G1"):
            continue
        moves += 1
        m = re.search(r"\bF([\d.]+)", stripped, re.I)
        if m:
            iron_feed = max(iron_feed, float(m.group(1)))
        ym = re.search(r"\bY([\d.+-]+)", stripped, re.I)
        if ym:
            y_vals.append(float(ym.group(1)))
    y_span = (max(y_vals) - min(y_vals)) if y_vals else 0.0
    est_s = 0.0
    if iron_feed > 0 and y_span > 0 and moves > 2:
        mm_s = iron_feed / 60.0
        est_s = (moves / 2.0) * (y_span / mm_s)
    return {
        "moves": moves,
        "feed": iron_feed,
        "y_span": y_span,
        "est_seconds": est_s,
    }


def live_log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with LIVE_LOG.open("a") as fh:
            fh.write(line)
    except OSError:
        pass


def resolve_cache_object(cache: dict, object_name: str) -> dict | None:
    objects = cache.get("objects", {})
    obj = objects.get(object_name)
    if obj:
        return obj
    folded = {k.casefold(): k for k in objects}
    key = folded.get(object_name.casefold())
    if key:
        return objects[key]
    return None


def object_near_print_end(
    cache: dict, obj: dict, layer: int, trigger_byte: int | None
) -> bool:
    print_end_byte = cache.get("print_end_byte")
    print_end_margin = int(cache.get("print_end_margin") or 2000)
    near = bool((obj.get("inject_near_print_end") or {}).get(str(layer)))
    if not near and print_end_byte is not None and trigger_byte is not None:
        near = int(print_end_byte) - int(trigger_byte) <= print_end_margin
    return near


def build_iron_script(
    cache: dict,
    items: list[tuple[str, int]],
    *,
    hold_sd: bool = False,
) -> tuple[str, bool]:
    """One uninterrupted script for all scheduled objects (print order).

    Never appends PRINT_END — the gcode file already contains it. Injecting
    PRINT_END early is what turns the case light off while iron is still running.
    """
    script_lines: list[str] = []
    near_print_end = False
    if hold_sd:
        script_lines.append("M25")
    script_lines.append("SAVE_GCODE_STATE NAME=IRON_STATE")

    for object_name, layer in items:
        obj = resolve_cache_object(cache, object_name)
        if not obj:
            raise SystemExit(f"Object not in cache: {object_name}")
        snippet = obj.get("layers", {}).get(str(layer))
        if not snippet:
            raise SystemExit(f"No iron gcode for {object_name} layer {layer}")

        trigger_byte = (obj.get("inject_after_byte") or {}).get(str(layer))
        if object_near_print_end(cache, obj, layer, trigger_byte):
            near_print_end = True

        script_lines.append(f"; IRON object={object_name} layer={layer}")
        for line in snippet.splitlines():
            line = line.strip()
            if line:
                script_lines.append(line)

    script_lines.append("RESTORE_GCODE_STATE NAME=IRON_STATE MOVE=0")
    if hold_sd:
        script_lines.append("M24")
    return "\n".join(script_lines) + "\n", near_print_end


def inject_chain(
    gcode_file: str,
    items: list[tuple[str, int]],
    trigger_byte: int | None = None,
    hold_sd: bool = False,
    *,
    last_trigger_byte: int | None = None,
    pre_hold_sd: bool = False,
) -> int:
    """Inject all objects in one gcode/script — no pause, no gap between irons."""
    if not items:
        return 0

    state = print_state()
    if state in ("complete", "cancelled", "standby", "error"):
        raise SystemExit(f"Refusing iron inject: print state is {state or 'unknown'}")

    cache_path = CACHE_DIR / f"{Path(gcode_file).name}.json"
    if not cache_path.is_file():
        raise SystemExit(f"Cache missing: {cache_path}")

    cache = json.loads(cache_path.read_text())
    print_end_byte = cache.get("print_end_byte")
    min_margin = int(cache.get("min_inject_margin") or 64)
    settings = cache.get("ironing_settings") or {}

    names = [f"{name}:L{layer}" for name, layer in items]
    xy = toolhead_xy()
    if hold_sd or any(
        object_near_print_end(
            cache,
            resolve_cache_object(cache, name) or {},
            layer,
            (resolve_cache_object(cache, name) or {})
            .get("inject_after_byte", {})
            .get(str(layer)),
        )
        for name, layer in items
    ):
        hold_sd = True

    margin_byte = last_trigger_byte if last_trigger_byte is not None else trigger_byte
    file_pos = file_position()
    if print_end_byte is not None:
        pe = int(print_end_byte)
        room_now = pe - file_pos
        room = room_now
        if margin_byte is not None:
            room_at_trigger = pe - int(margin_byte)
            if room_at_trigger >= min_margin:
                room = room_at_trigger
            elif room_now < min_margin:
                live_log(
                    f"INJECT_LATE file_pos={file_pos} trigger={margin_byte} "
                    f"print_end={pe} margin={room_at_trigger}"
                )
        if hold_sd:
            if room < min_margin:
                raise SystemExit(
                    f"Refusing iron inject: only {room} bytes before PRINT_END at {pe} "
                    f"(trigger={margin_byte})"
                )
        else:
            if file_pos >= pe:
                raise SystemExit(
                    f"Refusing iron inject: file_pos={file_pos} already at/past "
                    f"PRINT_END at {pe} (SD end macro likely ran)"
                )
            if room < min_margin:
                raise SystemExit(
                    f"Refusing iron inject: file_pos={file_pos} only {room_now} bytes "
                    f"before PRINT_END at {pe}"
                )

    if pre_hold_sd and hold_sd and state == "printing":
        try:
            pause_sd()
            live_log(f"PRE_HOLD file={gcode_file} objects={names} file_pos={file_pos}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            live_log(f"PRE_HOLD_FAIL file={gcode_file} err={exc}")

    script, near_print_end = build_iron_script(cache, items, hold_sd=hold_sd)
    live_log(
        f"CHAIN_START file={gcode_file} objects={names} file_pos={file_pos} "
        f"near_eof={near_print_end} hold_sd={hold_sd} toolhead={xy} "
        f"settings={settings} script_bytes={len(script)}"
    )

    t0 = time.monotonic()
    moonraker_script(script)
    elapsed = time.monotonic() - t0
    live_log(
        f"CHAIN_DONE file={gcode_file} objects={names} elapsed={elapsed:.1f}s "
        f"toolhead_after={toolhead_xy()} state={print_state()}"
    )
    return 0


def inject_object(
    gcode_file: str,
    object_name: str,
    layer: int,
    trigger_byte: int | None = None,
    hold_sd: bool = False,
) -> int:
    return inject_chain(
        gcode_file,
        [(object_name, layer)],
        trigger_byte=trigger_byte,
        hold_sd=hold_sd,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, help="G-code filename")
    parser.add_argument("--object", required=True)
    parser.add_argument("--layer", type=int, required=True)
    args = parser.parse_args()
    return inject_object(args.file, args.object, args.layer)


if __name__ == "__main__":
    raise SystemExit(main())