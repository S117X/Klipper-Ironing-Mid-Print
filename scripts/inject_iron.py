#!/usr/bin/env python3
"""Stream cached per-object iron gcode to Klipper via Moonraker.

Mid-print multi-object (critical)
---------------------------------
After ironing the first object, do **not** M24 when more objects remain.
Instead use --splice-rest: while SD is still paused, send
  [remaining gcode until PRINT_END) + [remaining irons] + PRINT_END
as one script, then SDCARD_RESET_FILE.

That is the only reliable way to beat a ~40-byte gap between the last top
and PRINT_END without cold ironing.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

PRINTER_DATA = Path(os.environ.get("PRINTER_DATA", "/home/x/printer_data"))
GCODE_DIR = PRINTER_DATA / "gcodes"
CACHE_DIR = PRINTER_DATA / "iron_cache"
MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")
IRON_SCRIPT_TIMEOUT = 600
SEIZE_TIMEOUT = 15
MIN_EXTRUDE_TEMP_C = 170.0
MAX_TEMP_BELOW_TARGET_C = 25.0


def moonraker_get(path: str) -> dict:
    req = urllib.request.Request(f"{MOONRAKER_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


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


def query_status() -> dict:
    try:
        resp = moonraker_get(
            "/printer/objects/query?print_stats&extruder&virtual_sdcard"
        )
        return resp.get("result", {}).get("status", {}) or {}
    except (urllib.error.URLError, json.JSONDecodeError, KeyError):
        return {}


def print_state(status: dict | None = None) -> str:
    status = status if status is not None else query_status()
    return str((status.get("print_stats") or {}).get("state") or "")


def file_position(status: dict | None = None) -> int:
    status = status if status is not None else query_status()
    try:
        return int((status.get("virtual_sdcard") or {}).get("file_position") or 0)
    except (TypeError, ValueError):
        return 0


def heaters_ok(status: dict | None = None) -> tuple[bool, str]:
    """Strict: require active heat target — blocks post-PRINT_END cold iron."""
    status = status if status is not None else query_status()
    ext = status.get("extruder") or {}
    try:
        temp = float(ext.get("temperature") or 0.0)
        target = float(ext.get("target") or 0.0)
    except (TypeError, ValueError):
        return False, "extruder temp unreadable"

    if target < MIN_EXTRUDE_TEMP_C:
        return False, f"heater target off/low temp={temp:.1f} target={target:.1f}"

    if temp < MIN_EXTRUDE_TEMP_C:
        return False, f"extruder cold temp={temp:.1f} (need >={MIN_EXTRUDE_TEMP_C})"

    if temp < target - MAX_TEMP_BELOW_TARGET_C:
        return (
            False,
            f"extruder too far below target temp={temp:.1f} target={target:.1f}",
        )

    if not bool(ext.get("can_extrude")):
        return False, f"cannot extrude temp={temp:.1f} target={target:.1f}"

    return True, f"ok temp={temp:.1f} target={target:.1f}"


def resolve_object(cache: dict, name: str) -> dict | None:
    objects = cache.get("objects") or {}
    if name in objects:
        return objects[name]
    folded = {k.casefold(): k for k in objects}
    key = folded.get(name.casefold())
    return objects.get(key) if key else None


def iron_snippet(cache: dict, object_name: str, layer: int) -> str:
    obj = resolve_object(cache, object_name)
    if not obj:
        raise SystemExit(f"Object not in cache: {object_name}")
    snippet = (obj.get("layers") or {}).get(str(layer))
    if not snippet:
        raise SystemExit(f"No iron gcode for {object_name} layer {layer}")
    return snippet


def build_iron_body(object_name: str, layer: int, snippet: str) -> list[str]:
    lines = [
        f"; IRON object={object_name} layer={layer}",
        "SAVE_GCODE_STATE NAME=IRON_STATE",
    ]
    for line in snippet.splitlines():
        line = line.strip()
        if line:
            lines.append(line)
    lines.append("RESTORE_GCODE_STATE NAME=IRON_STATE MOVE=0")
    return lines


def guard_or_die(status: dict | None = None) -> dict:
    status = status if status is not None else query_status()
    state = print_state(status)
    if state in ("complete", "cancelled", "standby", "error"):
        raise SystemExit(f"Refusing iron inject: print state is {state or 'unknown'}")
    if state not in ("printing", "paused"):
        # Allow paused (M25) mid-print; reject idle/complete.
        if state:
            raise SystemExit(f"Refusing iron inject: print state is {state}")
    ok, reason = heaters_ok(status)
    if not ok:
        raise SystemExit(f"Refusing iron inject: {reason}")
    return status


def seize_sd() -> None:
    moonraker_script("M25\n", timeout=SEIZE_TIMEOUT)


def resume_sd() -> None:
    moonraker_script("M24\n", timeout=SEIZE_TIMEOUT)


def resolve_gcode_path(name: str) -> Path:
    p = Path(name)
    if p.is_file():
        return p
    cand = GCODE_DIR / Path(name).name
    if cand.is_file():
        return cand
    raise SystemExit(f"G-code not found: {name}")


def find_print_end_byte(data: bytes) -> int | None:
    pos = 0
    for line in data.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == b"PRINT_END" or stripped.startswith(b"PRINT_END "):
            return pos
        pos += len(line)
    return None


def splice_rest_and_finish(
    gcode_file: Path,
    cache: dict,
    pairs: list[tuple[str, int]],
    *,
    from_byte: int | None = None,
    print_end: int | None = None,
) -> None:
    """SD must already be paused. Print remaining file body, iron pairs, PRINT_END."""
    status = guard_or_die()
    data = gcode_file.read_bytes()
    end = print_end if print_end is not None else find_print_end_byte(data)
    if end is None:
        end = len(data)
    pos = file_position(status) if from_byte is None else int(from_byte)
    # If we somehow already passed PRINT_END, refuse (would only cold-iron).
    if pos >= end:
        raise SystemExit(
            f"Refusing splice: file_pos={pos} already at/past PRINT_END={end}"
        )

    chunk = data[pos:end]
    rest_lines: list[str] = []
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        s = line.strip()
        if s == "PRINT_END" or s.startswith("PRINT_END "):
            break
        rest_lines.append(line)

    out: list[str] = [
        f"; IRON_SPLICE from_byte={pos} print_end={end} objects={pairs!r}",
    ]
    out.extend(rest_lines)
    out.append("; --- midprint iron remaining objects (before PRINT_END) ---")
    for obj_name, layer in pairs:
        snip = iron_snippet(cache, obj_name, layer)
        out.extend(build_iron_body(obj_name, layer, snip))
    out.append("PRINT_END")

    script = "\n".join(out) + "\n"
    # Re-check heaters immediately before long script.
    guard_or_die()
    moonraker_script(script, timeout=IRON_SCRIPT_TIMEOUT)

    # Drop the paused SD job so M24 cannot resume into the old tail.
    try:
        moonraker_script("SDCARD_RESET_FILE\n", timeout=SEIZE_TIMEOUT)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        pass


def run_sd_mode(sd: str, body_lines: list[str]) -> None:
    if sd == "seize":
        seize_sd()
        return
    if sd == "resume":
        resume_sd()
        return

    if sd in ("full", "hold"):
        seize_sd()
        guard_or_die()

    if body_lines:
        moonraker_script("\n".join(body_lines) + "\n")

    if sd == "full":
        resume_sd()


def parse_pairs(batch: str, object_name: str | None, layer: int | None) -> list[tuple[str, int]]:
    if batch:
        try:
            raw = json.loads(batch)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid --batch JSON: {exc}") from exc
        pairs: list[tuple[str, int]] = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise SystemExit(f"Bad batch item: {item!r}")
            pairs.append((str(item[0]), int(item[1])))
        if not pairs:
            raise SystemExit("Empty --batch")
        return pairs
    if not object_name or layer is None:
        raise SystemExit("--object and --layer required (or --batch)")
    return [(object_name, layer)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="", help="G-code filename")
    parser.add_argument("--object", help="Object name")
    parser.add_argument("--layer", type=int, help="Layer number")
    parser.add_argument(
        "--sd",
        choices=["full", "hold", "none", "resume", "seize"],
        default="full",
        help="SD pause/resume wrapping (default full)",
    )
    parser.add_argument("--batch", default="", help="JSON list of [object, layer]")
    parser.add_argument(
        "--splice-rest",
        action="store_true",
        help="While SD paused: run file remainder + irons + PRINT_END, then reset SD",
    )
    parser.add_argument(
        "--from-byte",
        type=int,
        default=-1,
        help="Override splice start byte (default: current file_position)",
    )
    parser.add_argument(
        "--print-end-byte",
        type=int,
        default=-1,
        help="Override PRINT_END byte for splice",
    )
    args = parser.parse_args()

    if args.sd == "seize":
        state = print_state()
        if state in ("complete", "cancelled", "standby", "error"):
            raise SystemExit(f"Refusing seize: print state is {state}")
        seize_sd()
        return 0

    if args.sd == "resume":
        resume_sd()
        return 0

    if not args.file:
        raise SystemExit("--file required")

    cache_path = CACHE_DIR / f"{Path(args.file).name}.json"
    if not cache_path.is_file():
        raise SystemExit(f"Cache missing: {cache_path}")
    cache = json.loads(cache_path.read_text())
    gcode_path = resolve_gcode_path(args.file)
    pairs = parse_pairs(args.batch, args.object, args.layer)

    if args.splice_rest:
        splice_rest_and_finish(
            gcode_path,
            cache,
            pairs,
            from_byte=None if args.from_byte < 0 else args.from_byte,
            print_end=None if args.print_end_byte < 0 else args.print_end_byte,
        )
        return 0

    # Hard ban: never start a normal inject if SD already at/past PRINT_END.
    status = query_status()
    pe = cache.get("print_end_byte")
    fp = file_position(status)
    if pe is not None and fp >= int(pe):
        raise SystemExit(
            f"Refusing iron inject: file_pos={fp} >= print_end={pe} (would be cold)"
        )

    guard_or_die(status)

    body: list[str] = []
    for obj_name, layer in pairs:
        snip = iron_snippet(cache, obj_name, layer)
        body.extend(build_iron_body(obj_name, layer, snip))
    run_sd_mode(args.sd, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
