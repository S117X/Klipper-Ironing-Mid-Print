#!/usr/bin/env python3
"""Index, cache, and schedule per-object ironing for active Klipper prints."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PRINTER_DATA = Path(os.environ.get("PRINTER_DATA", "/home/x/printer_data"))
GCODE_DIR = PRINTER_DATA / "gcodes"
CACHE_DIR = PRINTER_DATA / "iron_cache"
MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")
CACHE_VERSION = 9
PRINT_END_MARGIN = 2000  # bytes: inject this close to PRINT_END runs PRINT_END after iron
MIN_INJECT_MARGIN = 64  # bytes: refuse inject if sdcard is already this close to file end
FAST_POLL_GAP = 8000  # bytes: tighten watcher polling when this close to a trigger
EOF_POLL_GAP = 2000  # bytes: ultra-fast poll when SD is this close to PRINT_END
FAST_POLL_INTERVAL = 0.08
ULTRA_POLL_INTERVAL = 0.02

CHANGE_LAYER_RE = re.compile(r"^;\s*(?:CHANGE_LAYER|LAYER_CHANGE)\b", re.I)
Z_HEIGHT_RE = re.compile(r"^;\s*(?:Z_HEIGHT|Z):\s*([\d.]+)", re.I)
LAYER_HEIGHT_RE = re.compile(r"^;\s*LAYER_HEIGHT:\s*([\d.]+)", re.I)
FEATURE_RE = re.compile(r"^;\s*(?:FEATURE:\s*|TYPE:)(.+)", re.I)
TOTAL_LAYERS_RE = re.compile(r"^;\s*total layer number:\s*(\d+)", re.I)
META_RE = re.compile(r"^;\s*(\w+)\s*=\s*(.+)")
IRON_WIDTH_RE = re.compile(r"^;\s*WIDTH:\s*([\d.]+)", re.I)
IRON_HEIGHT_RE = re.compile(r"^;\s*HEIGHT:\s*([\d.]+)", re.I)
EXCLUDE_DEFINE_RE = re.compile(
    r"^EXCLUDE_OBJECT_DEFINE\s+NAME=(?P<name>\S+)"
    r"(?:\s+CENTER=(?P<center>[\d.,]+))?"
    r"(?:\s+POLYGON=(?P<polygon>\[\[.+\]\]))?",
    re.I,
)
EXCLUDE_START_RE = re.compile(r"^EXCLUDE_OBJECT_START\s+NAME=(?P<name>\S+)", re.I)
EXCLUDE_END_RE = re.compile(r"^EXCLUDE_OBJECT_END\s+NAME=(?P<name>\S+)", re.I)
STOP_PRINTING_RE = re.compile(r"^;\s*stop printing object\b", re.I)
PRINT_END_LINE_RE = re.compile(r"^PRINT_END\b", re.I)
G1_CMD_RE = re.compile(r"^G1\b", re.I)
AXIS_RE = {axis: re.compile(rf"\b{axis}([\d.+-]+)", re.I) for axis in "XYZEF"}


def moonraker_get(path: str) -> dict[str, Any]:
    req = urllib.request.Request(f"{MOONRAKER_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def moonraker_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{MOONRAKER_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def cache_path_for(gcode_file: Path) -> Path:
    return CACHE_DIR / f"{gcode_file.name}.json"


def resolve_object_name(cache: dict[str, Any], name: str) -> str | None:
    """Match Klipper exclude_object names to gcode cache keys (case may differ)."""
    objects = cache.get("objects", {})
    if name in objects:
        return name
    folded = {k.casefold(): k for k in objects}
    return folded.get(name.casefold())


def schedule_path_for(gcode_file: Path) -> Path:
    return CACHE_DIR / f"{gcode_file.name}.schedule.json"


def watcher_lock_path(gcode_file: Path) -> Path:
    return CACHE_DIR / f"{gcode_file.name}.watcher.lock"


def session_guard_lock_path(gcode_file: Path) -> Path:
    return CACHE_DIR / f"{gcode_file.name}.session.lock"


def trigger_lock_path(gcode_file: Path) -> Path:
    return CACHE_DIR / f"{gcode_file.name}.trigger.lock"


SCHEDULE_VERSION = 2


def normalize_schedule(data: dict[str, Any]) -> dict[str, Any]:
    """Support legacy single-object schedules and multi-object v2 format."""
    if data.get("objects"):
        return data
    obj = data.get("object")
    if not obj:
        return {**data, "version": SCHEDULE_VERSION, "objects": {}}
    return {
        "file": data.get("file"),
        "version": SCHEDULE_VERSION,
        "objects": {
            obj: {
                "mode": data.get("mode", "all_top"),
                "layers": list(data.get("layers") or []),
                "done": list(data.get("done") or []),
            }
        },
        "active": bool(data.get("active", True)),
        "print_duration_at_schedule": data.get("print_duration_at_schedule"),
        "gcode_mtime": data.get("gcode_mtime"),
    }


def resolve_schedule_object(
    schedule: dict[str, Any], name: str
) -> tuple[str | None, dict[str, Any] | None]:
    objects = normalize_schedule(schedule).get("objects") or {}
    if name in objects:
        return name, objects[name]
    folded = {k.casefold(): k for k in objects}
    key = folded.get(name.casefold())
    if key:
        return key, objects[key]
    return None, None


def object_schedule_pending(schedule: dict[str, Any], obj_name: str) -> bool:
    if not schedule.get("active"):
        return False
    _, obj_sched = resolve_schedule_object(schedule, obj_name)
    if not obj_sched:
        return False
    layers = obj_sched.get("layers") or []
    done = set(obj_sched.get("done") or [])
    return any(layer not in done for layer in layers)


def schedule_all_complete(schedule: dict[str, Any]) -> bool:
    norm = normalize_schedule(schedule)
    objects = norm.get("objects") or {}
    if not objects:
        return True
    for obj_sched in objects.values():
        layers = obj_sched.get("layers") or []
        done = set(obj_sched.get("done") or [])
        if any(layer not in done for layer in layers):
            return False
    return True


def _process_running(lock_path: Path) -> bool:
    if not lock_path.is_file():
        return False
    try:
        pid = int(lock_path.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        lock_path.unlink(missing_ok=True)
        return False


def watcher_is_running(gcode_file: Path) -> bool:
    return _process_running(session_guard_lock_path(gcode_file)) or _process_running(
        trigger_lock_path(gcode_file)
    )


def _spawn_daemon(script_name: str, gcode_file: Path) -> None:
    script = Path(__file__).with_name(script_name)
    subprocess.Popen(
        [
            sys.executable,
            str(script),
            "--file",
            str(gcode_file),
            "--schedule",
            str(schedule_path_for(gcode_file)),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def start_session_guard(gcode_file: Path) -> None:
    lock = session_guard_lock_path(gcode_file)
    if _process_running(lock):
        return
    _spawn_daemon("iron_session_guard.py", gcode_file)


def start_inject_trigger(gcode_file: Path) -> None:
    lock = trigger_lock_path(gcode_file)
    if _process_running(lock):
        return
    _spawn_daemon("iron_inject_trigger.py", gcode_file)


def start_watcher(gcode_file: Path) -> None:
    """Start session guard (cleanup) + inject trigger (fires at byte triggers)."""
    start_session_guard(gcode_file)
    start_inject_trigger(gcode_file)


TERMINAL_PRINT_STATES = frozenset({"complete", "cancelled", "error", "standby"})


def watched_gcode_name(snapshot: dict[str, Any]) -> str:
    ps = snapshot.get("print_stats") or {}
    vsd = snapshot.get("virtual_sdcard") or {}
    for raw in (ps.get("filename"), vsd.get("file_path")):
        if raw:
            return Path(str(raw)).name
    return ""


def print_job_active(snapshot: dict[str, Any]) -> bool:
    ps = snapshot.get("print_stats") or {}
    vsd = snapshot.get("virtual_sdcard") or {}
    state = str(ps.get("state") or "")
    if state in ("printing", "paused"):
        return True
    return bool(vsd.get("is_active"))


def print_job_finished(
    snapshot: dict[str, Any],
    gcode_file: Path,
    *,
    seen_printing: bool,
) -> tuple[bool, str]:
    """Return whether the watched print job is over and why."""
    if not seen_printing:
        return False, ""

    ps = snapshot.get("print_stats") or {}
    vsd = snapshot.get("virtual_sdcard") or {}
    state = str(ps.get("state") or "")
    message = str(ps.get("message") or "").strip()
    active = print_job_active(snapshot)
    current = watched_gcode_name(snapshot)

    if current and current != gcode_file.name:
        return True, f"different_file={current}"

    if state in ("complete", "cancelled", "error"):
        suffix = f" msg={message}" if message else ""
        return True, f"state={state}{suffix}"

    if state == "standby" and not active:
        suffix = f" msg={message}" if message else ""
        return True, f"state=standby{suffix}"

    if not active:
        progress = float(vsd.get("progress") or 0)
        file_pos = int(vsd.get("file_position") or 0)
        file_size = int(vsd.get("file_size") or 0)
        if file_size > 0 and (progress >= 0.995 or file_pos >= file_size - 32):
            return True, "sdcard_complete"
        if file_pos == 0 and not current:
            return True, "job_cleared"

    return False, ""


def cleanup_watcher_artifacts(
    gcode_file: Path,
    schedule_path: Path | None = None,
) -> None:
    cleanup_session_artifacts(gcode_file, schedule_path)


def cleanup_session_artifacts(
    gcode_file: Path,
    schedule_path: Path | None = None,
) -> None:
    path = schedule_path or schedule_path_for(gcode_file)
    path.unlink(missing_ok=True)
    session_guard_lock_path(gcode_file).unlink(missing_ok=True)
    trigger_lock_path(gcode_file).unlink(missing_ok=True)
    watcher_lock_path(gcode_file).unlink(missing_ok=True)


def _apply_meta_kv(meta: dict[str, Any], key: str, value: str) -> None:
    if key == "top_shell_layers":
        meta["top_shell_layers"] = int(float(value))
    elif key in ("ironing_flow", "support_ironing_flow"):
        meta["ironing_flow"] = float(value.rstrip("%")) / 100.0
    elif key == "ironing_speed":
        meta["ironing_speed"] = float(value.split()[0])
    elif key == "ironing_spacing":
        meta["ironing_spacing"] = float(value.split()[0])
    elif key == "ironing_pattern":
        meta["ironing_pattern"] = value
    elif key == "ironing_angle":
        meta["ironing_angle"] = float(value.split()[0])
    elif key == "ironing_inset":
        meta["ironing_inset"] = float(value.split()[0])
    elif key == "ironing_expansion":
        meta["ironing_expansion"] = float(value.split()[0])
    elif key == "ironing_fan_speed":
        meta["ironing_fan_speed"] = float(value.split()[0])
    elif key == "layer_height":
        meta["layer_height"] = float(value)
    elif key == "filament_diameter":
        meta["filament_diameter"] = float(value)
    elif key == "line_width":
        meta["line_width"] = float(value)


def parse_metadata(lines: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "top_shell_layers": 4,
        "ironing_flow": 0.10,
        "ironing_speed": 30.0,
        "ironing_spacing": 0.15,
        "ironing_pattern": "rectilinear",
        "ironing_angle": 0.0,
        "ironing_inset": 0.0,
        "ironing_expansion": 0.0,
        "ironing_fan_speed": -1.0,
        "layer_height": 0.2,
        "filament_diameter": 1.75,
        "line_width": 0.4,
        "iron_line_width": 0.40161,
        "iron_line_height": 0.0075,
        "iron_velocity_limit": "SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=2500",
        "iron_travel_feed": 3600.0,
        "iron_prime_feed": 1800.0,
    }
    in_config = False
    for line in lines:
        stripped = line.strip()
        if "; CONFIG_BLOCK_START" in stripped:
            in_config = True
            continue
        if not in_config:
            continue
        if "; CONFIG_BLOCK_END" in stripped:
            break
        if not stripped.startswith(";"):
            continue
        m = META_RE.match(stripped)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        _apply_meta_kv(meta, key, value)
    meta["iron_travel_feed"] = float(meta["ironing_speed"]) * 60.0
    return meta


def short_label(name: str) -> str:
    base = name.split(".")[0].replace("_", " ")
    return base[:28] + ("…" if len(base) > 28 else "")


def parse_g1_xy(line: str) -> dict[str, float] | None:
    """Parse Orca/Bambu-style G1 moves (X/Y required; Z optional)."""
    coords = parse_g1_line(line)
    if not coords or "x" not in coords or "y" not in coords:
        return None
    return coords


def parse_g1_line(line: str) -> dict[str, float] | None:
    stripped = line.strip()
    if not G1_CMD_RE.match(stripped):
        return None
    coords: dict[str, float] = {}
    for axis, pattern in AXIS_RE.items():
        match = pattern.search(stripped)
        if match:
            coords[axis.lower()] = float(match.group(1))
    return coords or None


def format_g1_move(coords: dict[str, float]) -> str:
    parts = ["G1"]
    for axis in "XYZEF":
        key = axis.lower()
        if key in coords:
            val = coords[key]
            if axis == "E":
                e_str = f"{val:.5f}"
                parts.append(f"E{e_str.lstrip('0')}" if abs(val) < 0.01 else f"E{e_str}")
            elif axis == "F":
                parts.append(f"F{val:.0f}")
            else:
                parts.append(f"{axis}{val:.3f}")
    return " ".join(parts)


def iron_snippet_has_moves(snippet: str) -> bool:
    for line in snippet.splitlines():
        if parse_g1_xy(line):
            return True
    return False


def cache_needs_rebuild(cache: dict[str, Any]) -> bool:
    if int(cache.get("cache_version") or 0) < CACHE_VERSION:
        return True
    for obj in cache.get("objects", {}).values():
        if obj.get("layers") and not obj.get("inject_after_byte"):
            return True
        for snippet in obj.get("layers", {}).values():
            if snippet and not iron_snippet_has_moves(snippet):
                return True
            if snippet and "; --- generated iron pass ---" in snippet:
                return True
        for source in obj.get("layer_sources", {}).values():
            if source == "synth":
                return True
    return False


def parse_polygon(raw: str | None) -> list[list[float]] | None:
    if not raw:
        return None
    try:
        points = json.loads(raw.replace(" ", ""))
    except json.JSONDecodeError:
        return None
    if not isinstance(points, list):
        return None
    out: list[list[float]] = []
    for pt in points:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        out.append([float(pt[0]), float(pt[1])])
    return out or None


def polygon_bbox(polygon: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def iron_surface_rect(
    polygon: list[list[float]], inset: float
) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = polygon_bbox(polygon)
    margin = 0.275 + inset
    return xmin + margin, ymin + margin, xmax - margin, ymax - margin


def e_per_mm(
    line_width: float, line_height: float, filament_diameter: float, flow: float
) -> float:
    fil_area = math.pi * (filament_diameter / 2.0) ** 2
    return (line_width * line_height / fil_area) * flow


def extract_iron_approach_lines(recent: list[str]) -> list[str]:
    """Orca approach block immediately before ;TYPE:Ironing (Z lift → prime → accel)."""
    lines = [
        s.strip()
        for s in recent
        if s.strip()
        and not s.strip().startswith(";")
        and not s.strip().startswith("M73")
    ]
    end_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=2500"):
            end_idx = i
            break
    if end_idx is None:
        return []

    start_idx = end_idx
    for i in range(end_idx, -1, -1):
        s = lines[i]
        if "E-" in s and s.startswith("G1"):
            start_idx = i + 1
            break
        if s.startswith("G1 Z") and "F" in s:
            start_idx = i
            break
    return lines[start_idx : end_idx + 1]


def apply_iron_approach_feeds(meta: dict[str, Any], approach: list[str]) -> None:
    for line in approach:
        if line.startswith("G1 Z") and "F" in line:
            m = re.search(r"\bF([\d.]+)", line, re.I)
            if m:
                meta["iron_travel_feed"] = float(m.group(1))
        if line.startswith("G1 E") and "F" in line:
            m = re.search(r"\bF([\d.]+)", line, re.I)
            if m:
                meta["iron_prime_feed"] = float(m.group(1))


def clean_native_iron_lines(lines: list[str]) -> list[str]:
    """Keep slicer ironing moves verbatim; stop at end-of-iron retract."""
    cleaned: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith(";"):
            continue
        if stripped.startswith("M73"):
            continue
        if "WIPE_" in stripped:
            break
        if stripped.startswith("EXCLUDE_OBJECT") or stripped.startswith("M106"):
            break
        if "E-" in stripped and stripped.startswith("G1"):
            break
        cleaned.append(stripped)
    while cleaned and cleaned[-1].startswith("G1 F") and "X" not in cleaned[-1]:
        cleaned.pop()
    while cleaned and "E-" in cleaned[-1]:
        cleaned.pop()
    return cleaned


def native_iron_header_lines(lines: list[str]) -> list[str]:
    """Orca ;TYPE:Ironing header comments from native feature."""
    header: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith(";TYPE:Ironing") or stripped.startswith(";WIDTH:") or stripped.startswith(
            ";HEIGHT:"
        ):
            header.append(stripped)
    return header


def iron_approach_preamble(
    polygon: list[list[float]],
    z: float,
    line_width: float,
    line_height: float,
    inset: float,
    meta: dict[str, Any] | None = None,
    native_header: list[str] | None = None,
) -> list[str]:
    meta = meta or {}
    travel = float(meta.get("iron_travel_feed") or 3600.0)
    prime = float(meta.get("iron_prime_feed") or 1800.0)
    vel = str(
        meta.get("iron_velocity_limit")
        or "SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=2500"
    )
    ix0, iy0, ix1, iy1 = iron_surface_rect(polygon, inset)
    z_lift = z + 0.4
    corner_x = ix1
    corner_y = iy0 - 0.067
    header = native_header or [
        ";TYPE:Ironing",
        f";WIDTH:{line_width:.5f}",
        f";HEIGHT:{line_height:.5f}",
    ]
    return [
        "; --- slicer-style iron approach ---",
        "G90",
        f"G1 Z{z_lift:.2f} F{travel:.0f}",
        f"G1 X{ix0:.3f} Y{(iy0 + iy1) / 2:.3f} Z{z_lift:.2f}",
        f"G1 X{ix0:.3f} Y{iy1:.3f}",
        f"G1 X{ix1:.3f} Y{iy1:.3f}",
        f"G1 X{ix1:.3f} Y{iy0:.3f}",
        f"G1 X{corner_x:.3f} Y{corner_y:.3f}",
        f"G1 Z{z:.2f}",
        f"G1 E.8 F{prime:.0f}",
        vel,
        *header,
    ]


def translate_gcode_lines(
    lines: list[str],
    dx: float,
    dy: float,
) -> list[str]:
    out: list[str] = []
    for raw in lines:
        if raw.startswith("SET_VELOCITY_LIMIT"):
            out.append(raw)
            continue
        coords = parse_g1_line(raw)
        if not coords:
            continue
        if "x" in coords:
            coords["x"] += dx
        if "y" in coords:
            coords["y"] += dy
        out.append(format_g1_move(coords))
    return out


def translate_iron_template(
    template_moves: list[str],
    src_rect: tuple[float, float, float, float],
    polygon: list[list[float]],
    z: float,
    meta: dict[str, Any],
    approach_lines: list[str] | None = None,
    header_lines: list[str] | None = None,
) -> list[str]:
    """Shift a native Orca ironing path to another same-size object."""
    inset = float(meta.get("ironing_inset", 0.0))
    line_width = float(meta.get("iron_line_width", meta["line_width"]))
    line_height = float(meta.get("iron_line_height", 0.0075))
    dst_rect = iron_surface_rect(polygon, inset)
    dx = dst_rect[0] - src_rect[0]
    dy = dst_rect[1] - src_rect[1]

    moves = translate_gcode_lines(template_moves, dx, dy)
    if not moves:
        return []

    header = header_lines or [
        ";TYPE:Ironing",
        f";WIDTH:{line_width:.5f}",
        f";HEIGHT:{line_height:.5f}",
    ]
    if approach_lines:
        approach = translate_gcode_lines(approach_lines, dx, dy)
        return [
            "; --- orca slicer iron (matched) ---",
            "G90",
            *approach,
            *header,
            *moves,
        ]
    return [
        "; --- orca template iron (translated) ---",
        *iron_approach_preamble(
            polygon, z, line_width, line_height, inset, meta=meta
        ),
        *moves,
    ]


def generate_rectilinear_iron(
    polygon: list[list[float]],
    z: float,
    meta: dict[str, Any],
    template: (
        tuple[
            list[str],
            tuple[float, float, float, float],
            list[str],
            list[str],
        ]
        | None
    ) = None,
) -> list[str]:
    if template:
        template_moves, src_rect, approach_lines, header_lines = template
        translated = translate_iron_template(
            template_moves,
            src_rect,
            polygon,
            z,
            meta,
            approach_lines=approach_lines or None,
            header_lines=header_lines or None,
        )
        if translated:
            return translated

    spacing = float(meta["ironing_spacing"])
    speed = float(meta["ironing_speed"])
    flow = float(meta["ironing_flow"])
    inset = float(meta.get("ironing_inset", 0.0))
    line_width = float(meta.get("iron_line_width", meta["line_width"]))
    line_height = float(meta.get("iron_line_height", 0.0075))
    filament_diameter = float(meta["filament_diameter"])
    feed = speed * 60.0
    e_mm = e_per_mm(line_width, line_height, filament_diameter, flow)

    ix0, iy0, ix1, iy1 = iron_surface_rect(polygon, inset)
    moves: list[str] = []
    x = ix1
    while x >= ix0 - 1e-6:
        dist = iy1 - iy0
        moves.append(f"G1 X{x:.3f} Y{iy0:.3f} F{feed:.0f}")
        moves.append(f"G1 X{x:.3f} Y{iy1:.3f} E{dist * e_mm:.5f}")
        x -= spacing

    if not moves:
        return []
    return [
        "; --- rectilinear iron fallback ---",
        *iron_approach_preamble(
            polygon, z, line_width, line_height, inset, meta=meta
        ),
        *moves,
    ]


def wrap_slicer_iron(
    native_lines: list[str],
    polygon: list[list[float]],
    z: float,
    meta: dict[str, Any],
    approach_lines: list[str] | None = None,
) -> list[str]:
    cleaned = clean_native_iron_lines(native_lines)
    if not cleaned:
        return []
    line_width = float(meta.get("iron_line_width", meta["line_width"]))
    line_height = float(meta.get("iron_line_height", 0.0075))
    inset = float(meta.get("ironing_inset", 0.0))
    header = native_iron_header_lines(native_lines)
    if approach_lines:
        return [
            "; --- orca slicer iron (native) ---",
            "G90",
            *approach_lines,
            *header,
            *cleaned,
        ]
    return [
        *iron_approach_preamble(
            polygon,
            z,
            line_width,
            line_height,
            inset,
            meta=meta,
            native_header=header,
        ),
        *cleaned,
    ]


def gcode_mtime(gcode_file: Path) -> float:
    try:
        return gcode_file.stat().st_mtime
    except OSError:
        return 0.0


def cache_is_stale(gcode_file: Path, cache_path: Path) -> bool:
    if not cache_path.is_file():
        return True
    try:
        return cache_path.stat().st_mtime < gcode_mtime(gcode_file)
    except OSError:
        return True


def drop_stale_schedule(
    gcode_file: Path, print_duration: float, print_state: str = ""
) -> None:
    """Remove schedule files left from a prior job or an older gcode upload."""
    path = schedule_path_for(gcode_file)
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return
    if data.get("file") != gcode_file.name:
        path.unlink(missing_ok=True)
        return
    if print_state in ("complete", "standby", "cancelled"):
        path.unlink(missing_ok=True)
        return

    gcode_mt = gcode_mtime(gcode_file)
    sched_gcode_mt = float(data.get("gcode_mtime") or 0)
    if sched_gcode_mt and gcode_mt > sched_gcode_mt + 0.5:
        path.unlink(missing_ok=True)
        return

    sched_at = data.get("print_duration_at_schedule")
    if sched_at is not None and print_duration + 5 < float(sched_at):
        path.unlink(missing_ok=True)
        return


def load_schedule(
    gcode_file: Path, print_duration: float, print_state: str = ""
) -> dict[str, Any] | None:
    drop_stale_schedule(gcode_file, print_duration, print_state)
    path = schedule_path_for(gcode_file)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if data.get("file") != gcode_file.name:
        return None
    if print_state in ("complete", "standby", "cancelled"):
        return None
    sched_at = data.get("print_duration_at_schedule")
    if sched_at is not None and print_duration + 5 < float(sched_at):
        return None
    return data


def layer_from_file_position(cache: dict[str, Any], file_position: int) -> int:
    offsets = cache.get("layer_byte_offsets") or {}
    best = 0
    for layer_s, pos in offsets.items():
        if int(pos) <= file_position:
            best = max(best, int(layer_s))
    return best


def index_gcode(gcode_file: Path) -> dict[str, Any]:
    text = gcode_file.read_text(errors="replace").splitlines()
    meta = parse_metadata(text)

    objects: dict[str, dict[str, Any]] = {}
    current_object: str | None = None
    layer_num = 0
    layer_z = 0.0
    layer_height = float(meta.get("layer_height", 0.2))
    total_layers = 0
    layer_byte_offsets: dict[str, int] = {}
    layer_z_by_num: dict[int, float] = {}
    file_offset = 0

    feature = None
    feature_lines: list[str] = []
    object_features: dict[str, dict[str, dict[str, list[str]]]] = {}
    object_layer_end_byte: dict[str, dict[str, int]] = {}
    object_top_surface_end_byte: dict[str, dict[str, int]] = {}
    object_had_top_surface: dict[str, bool] = {}
    object_recent_gcode: dict[str, list[str]] = {}
    object_iron_approach: dict[str, dict[str, list[str]]] = {}
    print_end_byte: int | None = None

    def flush_feature() -> None:
        nonlocal feature, feature_lines
        if not feature or not current_object:
            feature = None
            feature_lines = []
            return
        obj_bucket = object_features.setdefault(current_object, {})
        layer_bucket = obj_bucket.setdefault(str(layer_num), {})
        layer_bucket.setdefault(feature, []).extend(feature_lines)
        feature = None
        feature_lines = []

    for line in text:
        stripped = line.strip()
        line_byte_len = len(line) + 1

        define = EXCLUDE_DEFINE_RE.match(stripped)
        if define:
            name = define.group("name")
            center = None
            if define.group("center"):
                center = [
                    float(v) for v in define.group("center").split(",") if v.strip()
                ]
            polygon = parse_polygon(define.group("polygon"))
            objects.setdefault(
                name,
                {
                    "name": name,
                    "label": short_label(name),
                    "center": center,
                    "polygon": polygon,
                    "layers": {},
                    "has_slicer_iron": False,
                    "layer_sources": {},
                },
            )
            if polygon and not objects[name].get("polygon"):
                objects[name]["polygon"] = polygon
            file_offset += line_byte_len
            continue

        start = EXCLUDE_START_RE.match(stripped)
        if start:
            flush_feature()
            current_object = start.group("name")
            object_had_top_surface[current_object] = False
            objects.setdefault(
                current_object,
                {
                    "name": current_object,
                    "label": short_label(current_object),
                    "center": None,
                    "layers": {},
                },
            )
            file_offset += line_byte_len
            continue

        if PRINT_END_LINE_RE.match(stripped):
            print_end_byte = file_offset

        if (
            current_object
            and layer_num > 0
            and object_had_top_surface.get(current_object)
            and STOP_PRINTING_RE.match(stripped)
        ):
            # Inject after the last top-surface extrusion, before wipe/end gcode.
            object_top_surface_end_byte.setdefault(current_object, {})[
                str(layer_num)
            ] = file_offset + line_byte_len

        end = EXCLUDE_END_RE.match(stripped)
        if end:
            flush_feature()
            ended_obj = end.group("name")
            if layer_num > 0:
                end_byte = file_offset + line_byte_len
                object_layer_end_byte.setdefault(ended_obj, {})[
                    str(layer_num)
                ] = end_byte
                if object_had_top_surface.get(ended_obj):
                    top_bucket = object_top_surface_end_byte.setdefault(
                        ended_obj, {}
                    )
                    top_bucket.setdefault(str(layer_num), end_byte)
            object_had_top_surface.pop(ended_obj, None)
            current_object = None
            file_offset += line_byte_len
            continue

        total_match = TOTAL_LAYERS_RE.match(stripped)
        if total_match:
            total_layers = int(total_match.group(1))
            file_offset += line_byte_len
            continue

        if CHANGE_LAYER_RE.match(stripped):
            flush_feature()
            layer_num += 1
            total_layers = max(total_layers, layer_num)
            layer_byte_offsets[str(layer_num)] = file_offset
            file_offset += line_byte_len
            continue

        z_match = Z_HEIGHT_RE.match(stripped)
        if z_match:
            layer_z = float(z_match.group(1))
            if layer_num > 0:
                layer_z_by_num[layer_num] = layer_z
            file_offset += line_byte_len
            continue

        lh_match = LAYER_HEIGHT_RE.match(stripped)
        if lh_match:
            layer_height = float(lh_match.group(1))
            file_offset += line_byte_len
            continue

        feat_match = FEATURE_RE.match(stripped)
        if feat_match:
            flush_feature()
            feature = feat_match.group(1).strip()
            if current_object and feature.casefold() == "top surface":
                object_had_top_surface[current_object] = True
            if (
                current_object
                and layer_num > 0
                and feature.casefold() == "ironing"
            ):
                approach = extract_iron_approach_lines(
                    object_recent_gcode.get(current_object, [])
                )
                if approach:
                    object_iron_approach.setdefault(current_object, {})[
                        str(layer_num)
                    ] = approach
            file_offset += line_byte_len
            continue

        if current_object and stripped and not stripped.startswith(";"):
            bucket = object_recent_gcode.setdefault(current_object, [])
            bucket.append(stripped)
            if len(bucket) > 28:
                object_recent_gcode[current_object] = bucket[-28:]

        if feature == "Ironing":
            w_match = IRON_WIDTH_RE.match(stripped)
            if w_match:
                meta["iron_line_width"] = float(w_match.group(1))
            h_match = IRON_HEIGHT_RE.match(stripped)
            if h_match:
                meta["iron_line_height"] = float(h_match.group(1))

        if feature and stripped and not stripped.startswith(";"):
            feature_lines.append(stripped)

        file_offset += line_byte_len

    flush_feature()

    top_n = int(meta["top_shell_layers"])
    top_layer_start = max(1, total_layers - top_n + 1)
    top_layer_z = layer_z_by_num.get(
        total_layers, total_layers * layer_height
    )

    iron_template: (
        tuple[
            list[str],
            tuple[float, float, float, float],
            list[str],
            list[str],
        ]
        | None
    ) = None
    template_source: str | None = None
    template_layer: str | None = None
    template_inset = float(meta.get("ironing_inset", 0.0))
    for obj_name, obj in objects.items():
        polygon = obj.get("polygon")
        if not polygon:
            continue
        for layer_key, feats in object_features.get(obj_name, {}).items():
            native = feats.get("Ironing", [])
            if not native:
                continue
            header = native_iron_header_lines(native)
            for hdr in header:
                if hdr.startswith(";WIDTH:"):
                    meta["iron_line_width"] = float(hdr.split(":", 1)[1])
                elif hdr.startswith(";HEIGHT:"):
                    meta["iron_line_height"] = float(hdr.split(":", 1)[1])
            approach = object_iron_approach.get(obj_name, {}).get(layer_key, [])
            apply_iron_approach_feeds(meta, approach)
            for raw in native:
                s = raw.strip()
                if s.startswith("SET_VELOCITY_LIMIT"):
                    meta["iron_velocity_limit"] = s
            for raw in approach:
                s = raw.strip()
                if s.startswith("SET_VELOCITY_LIMIT"):
                    meta["iron_velocity_limit"] = s
            cleaned = clean_native_iron_lines(native)
            if cleaned:
                iron_template = (
                    cleaned,
                    iron_surface_rect(polygon, template_inset),
                    approach,
                    header,
                )
                template_source = obj_name
                template_layer = layer_key
                break
        if iron_template:
            break

    for obj_name, obj in objects.items():
        obj_layers: dict[str, str] = {}
        layer_sources: dict[str, str] = {}
        polygon = obj.get("polygon")
        has_slicer = False

        for layer_key, feats in object_features.get(obj_name, {}).items():
            layer_i = int(layer_key)
            native = feats.get("Ironing", [])
            if not native:
                continue
            has_slicer = True
            z = layer_z_by_num.get(layer_i, top_layer_z)
            approach = object_iron_approach.get(obj_name, {}).get(layer_key, [])
            if approach:
                apply_iron_approach_feeds(meta, approach)
            if polygon:
                iron_lines = wrap_slicer_iron(
                    native, polygon, z, meta, approach_lines=approach or None
                )
            else:
                iron_lines = clean_native_iron_lines(native)
            snippet = "\n".join(iron_lines)
            if iron_snippet_has_moves(snippet):
                obj_layers[layer_key] = snippet
                layer_sources[layer_key] = "slicer"

        if polygon and str(total_layers) not in obj_layers:
            iron_lines = generate_rectilinear_iron(
                polygon, top_layer_z, meta, iron_template
            )
            snippet = "\n".join(iron_lines)
            if iron_snippet_has_moves(snippet):
                obj_layers[str(total_layers)] = snippet
                layer_sources[str(total_layers)] = (
                    "template" if iron_template else "synth"
                )

        inject_after_byte: dict[str, int] = {}
        inject_near_print_end: dict[str, bool] = {}
        for layer_key in obj_layers:
            if layer_key in object_top_surface_end_byte.get(obj_name, {}):
                inject_after_byte[layer_key] = object_top_surface_end_byte[
                    obj_name
                ][layer_key]
            elif layer_key in object_layer_end_byte.get(obj_name, {}):
                inject_after_byte[layer_key] = object_layer_end_byte[obj_name][
                    layer_key
                ]
            if print_end_byte is not None and layer_key in inject_after_byte:
                margin = print_end_byte - inject_after_byte[layer_key]
                inject_near_print_end[layer_key] = margin <= PRINT_END_MARGIN

        obj["layers"] = obj_layers
        obj["layer_sources"] = layer_sources
        obj["has_slicer_iron"] = has_slicer
        obj["inject_after_byte"] = inject_after_byte
        if inject_near_print_end:
            obj["inject_near_print_end"] = inject_near_print_end

    ironing_settings = {
        "flow": meta["ironing_flow"],
        "speed": meta["ironing_speed"],
        "spacing": meta["ironing_spacing"],
        "pattern": meta["ironing_pattern"],
        "angle": meta.get("ironing_angle"),
        "inset": meta.get("ironing_inset"),
        "expansion": meta.get("ironing_expansion"),
        "fan_speed": meta.get("ironing_fan_speed"),
        "line_width": meta.get("iron_line_width"),
        "line_height": meta.get("iron_line_height"),
        "travel_feed": meta.get("iron_travel_feed"),
        "prime_feed": meta.get("iron_prime_feed"),
        "velocity_limit": meta.get("iron_velocity_limit"),
        "template_source": template_source,
        "template_layer": template_layer,
        "uses_native_approach": bool(
            iron_template and iron_template[2]
        ),
    }

    cache = {
        "file": gcode_file.name,
        "cache_version": CACHE_VERSION,
        "print_end_byte": print_end_byte,
        "print_end_margin": PRINT_END_MARGIN,
        "min_inject_margin": MIN_INJECT_MARGIN,
        "total_layers": total_layers,
        "layer_height": layer_height,
        "layer_byte_offsets": layer_byte_offsets,
        "layer_z_by_num": {str(k): v for k, v in layer_z_by_num.items()},
        "top_shell_layers": top_n,
        "top_layer_start": top_layer_start,
        "ironing_settings": ironing_settings,
        "objects": objects,
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path_for(gcode_file).write_text(json.dumps(cache, indent=2))
    return cache


def resolve_gcode_file(name: str) -> Path:
    path = Path(name)
    if path.is_file():
        return path
    candidate = GCODE_DIR / name
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"G-code file not found: {name}")


def get_print_state() -> dict[str, Any]:
    try:
        resp = moonraker_get(
            "/printer/objects/query?print_stats&toolhead&exclude_object&virtual_sdcard&gcode_move"
        )
        return resp.get("result", {}).get("status", {})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {}


def current_layer(cache: dict[str, Any], status: dict[str, Any]) -> int:
    print_stats = status.get("print_stats", {})
    info = print_stats.get("info", {})
    cur = int(info.get("current_layer") or 0)
    if cur > 0:
        return cur

    vsd = status.get("virtual_sdcard", {})
    fp = int(vsd.get("file_position") or 0)
    if cache.get("layer_byte_offsets") and fp > 0:
        state = str(print_stats.get("state") or "")
        if state in ("printing", "paused") or vsd.get("is_active"):
            return layer_from_file_position(cache, fp)

    return 0


def report_enable_result(payload: dict[str, Any]) -> None:
    try:
        moonraker_post(
            "/server/database/item",
            {"namespace": "iron_scheduler", "key": "last_enable", "value": payload},
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        pass


def layers_for_mode(cache: dict[str, Any], mode: str) -> list[int]:
    total = int(cache["total_layers"])
    start = int(cache["top_layer_start"])
    layers = list(range(start, total + 1))
    if mode == "topmost":
        return [total] if total >= start else []
    return layers


def cmd_index(args: argparse.Namespace) -> int:
    gcode_file = resolve_gcode_file(args.file)
    cache = index_gcode(gcode_file)
    print(json.dumps({"ok": True, "objects": list(cache["objects"]), "total_layers": cache["total_layers"]}))
    return 0


def ensure_indexed(gcode_file: Path) -> dict[str, Any]:
    cache_path = cache_path_for(gcode_file)
    if cache_is_stale(gcode_file, cache_path):
        return index_gcode(gcode_file)
    if cache_path.is_file():
        cache = json.loads(cache_path.read_text())
        if cache_needs_rebuild(cache):
            return index_gcode(gcode_file)
        return cache
    return index_gcode(gcode_file)


def cmd_enable(args: argparse.Namespace) -> int:
    gcode_file = resolve_gcode_file(args.file)
    cache = ensure_indexed(gcode_file)

    status = get_print_state()
    print_stats = status.get("print_stats", {})
    print_duration = float(print_stats.get("print_duration") or 0)
    print_state = str(print_stats.get("state") or "")
    existing = load_schedule(gcode_file, print_duration, print_state)
    canonical = resolve_object_name(cache, args.object)
    if not canonical:
        result = {
            "ok": False,
            "error": f"Object not found: {args.object}",
            "known": list(cache["objects"]),
        }
        report_enable_result(result)
        print(json.dumps(result))
        return 1

    obj = cache["objects"][canonical]
    if obj.get("has_slicer_iron"):
        result = {
            "ok": False,
            "error": (
                "Object already has slicer ironing in this file — Orca will iron "
                "it during the normal print. Only schedule the other object(s)."
            ),
        }
        report_enable_result(result)
        print(json.dumps(result))
        return 1

    if not obj.get("layers"):
        result = {"ok": False, "error": "No ironable top layers for this object"}
        report_enable_result(result)
        print(json.dumps(result))
        return 1

    if existing and object_schedule_pending(existing, canonical):
        result = {
            "ok": False,
            "error": f"Iron already scheduled for {canonical}",
        }
        report_enable_result(result)
        print(json.dumps(result))
        return 1

    cur = current_layer(cache, status)
    target_layers = [
        layer
        for layer in layers_for_mode(cache, args.mode)
        if layer >= cur and str(layer) in obj["layers"]
    ]
    if not target_layers:
        result = {"ok": False, "error": "No remaining top layers to iron for this object"}
        report_enable_result(result)
        print(json.dumps(result))
        return 1

    if existing:
        schedule = normalize_schedule(existing)
    else:
        schedule = {
            "file": gcode_file.name,
            "version": SCHEDULE_VERSION,
            "objects": {},
            "active": True,
            "print_duration_at_schedule": print_duration,
            "gcode_mtime": gcode_mtime(gcode_file),
        }

    schedule["objects"][canonical] = {
        "mode": args.mode,
        "layers": target_layers,
        "done": [],
    }
    schedule["active"] = True
    schedule.setdefault("print_duration_at_schedule", print_duration)
    schedule.setdefault("gcode_mtime", gcode_mtime(gcode_file))
    sched_path = schedule_path_for(gcode_file)
    sched_path.write_text(json.dumps(schedule, indent=2))

    start_watcher(gcode_file)

    try:
        from iron_inject_trigger import attempt_inject_for_schedule

        attempt_inject_for_schedule(gcode_file, sched_path)
    except ImportError:
        pass

    result = {
        "ok": True,
        "scheduled_layers": target_layers,
        "object": canonical,
    }
    report_enable_result(result)
    print(json.dumps(result))
    return 0


def cmd_preprocess(args: argparse.Namespace) -> int:
    gcode_file = resolve_gcode_file(args.file)
    index_gcode(gcode_file)
    lines = gcode_file.read_text(errors="replace").splitlines()
    if any("SET_PRINT_STATS_INFO CURRENT_LAYER" in line for line in lines):
        print(json.dumps({"ok": True, "skipped": "already_preprocessed"}))
        return 0

    out: list[str] = []
    layer_num = 0
    total_layers = 0
    for line in lines:
        stripped = line.strip()
        total_match = TOTAL_LAYERS_RE.match(stripped)
        if total_match:
            total_layers = int(total_match.group(1))
            break
    if total_layers <= 0:
        total_layers = sum(1 for line in lines if CHANGE_LAYER_RE.match(line.strip()))

    for line in lines:
        out.append(line)
        if CHANGE_LAYER_RE.match(line.strip()):
            layer_num += 1
            out.append(f"SET_PRINT_STATS_INFO TOTAL_LAYER={total_layers}")
            out.append(f"SET_PRINT_STATS_INFO CURRENT_LAYER={layer_num}")
            out.append("_IRON_LAYER_HOOK")

    gcode_file.write_text("\n".join(out) + "\n")
    print(json.dumps({"ok": True, "layers": total_layers}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Iron scheduler")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index")
    p_index.add_argument("--file", required=True)
    p_index.set_defaults(func=cmd_index)

    p_enable = sub.add_parser("enable")
    p_enable.add_argument("--file", required=True)
    p_enable.add_argument("--object", required=True)
    p_enable.add_argument("--mode", choices=["topmost", "all_top"], default="topmost")
    p_enable.set_defaults(func=cmd_enable)

    p_preprocess = sub.add_parser("preprocess")
    p_preprocess.add_argument("--file", required=True)
    p_preprocess.set_defaults(func=cmd_preprocess)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())