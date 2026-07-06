#!/usr/bin/env python3
"""Offline + mock-Moonraker simulation for iron scheduler / watcher / inject."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import inject_iron  # noqa: E402
import iron_scheduler  # noqa: E402
import iron_watcher  # noqa: E402

# Bytes of gcode the virtual_sdcard reads per second while iron runs on toolhead.
SDCARD_BYTES_PER_SEC = 120.0
# Typical iron inject duration observed on printer (rear cube, ~97s).
IRON_INJECT_DURATION_SEC = 97.0

GCODE_DIR = Path(os.environ.get("PRINTER_DATA", "/home/x/printer_data")) / "gcodes"

PASS = 0
FAIL = 0


def ok(name: str, detail: str = "") -> None:
    global PASS
    PASS += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  PASS  {name}{suffix}")


def fail(name: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  FAIL  {name}{suffix}")


@dataclass
class MockPrinter:
    state: str = "printing"
    file_position: int = 0
    file_size: int = 0
    filename: str = ""
    homed_axes: str = "xyz"
    scripts: list[str] = field(default_factory=list)
    is_active: bool = True

    def query_print_stats(self) -> dict[str, Any]:
        return {
            "result": {
                "status": {
                    "print_stats": {
                        "state": self.state,
                        "filename": self.filename,
                        "print_duration": 120.0,
                        "info": {"current_layer": 10},
                    },
                    "virtual_sdcard": {
                        "file_position": self.file_position,
                        "file_size": self.file_size,
                        "is_active": self.is_active,
                        "progress": (
                            self.file_position / self.file_size
                            if self.file_size
                            else 0.0
                        ),
                    },
                    "gcode_move": {},
                }
            }
        }

    def query_toolhead(self) -> dict[str, Any]:
        return {
            "result": {
                "status": {
                    "toolhead": {"homed_axes": self.homed_axes},
                }
            }
        }


class _MoonrakerHandler(BaseHTTPRequestHandler):
    printer: MockPrinter

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        body: dict[str, Any]
        if "print_stats" in self.path and "virtual_sdcard" in self.path:
            body = self.printer.query_print_stats()
        elif "print_stats" in self.path:
            body = {
                "result": {
                    "status": {
                        "print_stats": self.printer.query_print_stats()["result"][
                            "status"
                        ]["print_stats"]
                    }
                }
            }
        elif "virtual_sdcard" in self.path:
            body = {
                "result": {
                    "status": {
                        "virtual_sdcard": self.printer.query_print_stats()["result"][
                            "status"
                        ]["virtual_sdcard"]
                    }
                }
            }
        elif "toolhead" in self.path:
            body = self.printer.query_toolhead()
        else:
            body = {"result": {"status": {}}}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode()) if raw else {}
        if self.path.endswith("/printer/print/pause"):
            if self.printer.state == "printing":
                self.printer.state = "paused"
        elif self.path.endswith("/printer/print/resume"):
            if self.printer.state == "paused":
                self.printer.state = "printing"
        elif self.path.endswith("/printer/gcode/script"):
            script = payload.get("script", "")
            self.printer.scripts.append(script)
            if "PRINT_END" in script:
                self.printer.homed_axes = "xyz"
                self.printer.state = "complete"
        data = json.dumps({"result": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_mock_moonraker(printer: MockPrinter) -> tuple[ThreadingHTTPServer, str]:
    handler = type("Handler", (_MoonrakerHandler,), {"printer": printer})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def patch_env(cache_dir: Path, moonraker_url: str) -> dict[str, str]:
    return {
        "PRINTER_DATA": str(cache_dir.parent),
        "MOONRAKER_URL": moonraker_url,
    }


def simulate_watcher_inject_plan(
    cache: dict[str, Any],
    schedule: dict[str, Any],
    file_positions: list[int],
) -> list[tuple[str, int, int]]:
    """Return immediate per-object inject events (object, layer, file_pos)."""
    events: list[tuple[str, int, int]] = []
    sim_schedule = json.loads(json.dumps(schedule))

    for file_pos in sorted(file_positions):
        layer = iron_watcher.layer_from_file_position(cache, file_pos)
        waiting, ready = iron_watcher.pending_inject_targets(
            cache, sim_schedule, layer, file_pos, set()
        )
        if not ready:
            continue
        trigger_byte, obj_name, target = ready[0]
        obj_sched = sim_schedule["objects"][obj_name]
        done = set(obj_sched.get("done") or [])
        if target in done:
            continue
        events.append((obj_name, target, file_pos))
        done.add(target)
        obj_sched["done"] = sorted(done)
    return events


def build_inject_script(
    cache: dict[str, Any], obj_name: str, layer: int
) -> tuple[str, bool]:
    """Mirror single-object inject script assembly without HTTP."""
    return inject_iron.build_iron_script(cache, [(obj_name, layer)])


def build_chain_script(
    cache: dict[str, Any], items: list[tuple[str, int]]
) -> tuple[str, bool]:
    return inject_iron.build_iron_script(cache, items)


@dataclass
class ToolheadState:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    e: float = 0.0
    absolute: bool = True
    feed: float = 3000.0

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


def parse_g1_axes(line: str) -> dict[str, float]:
    coords: dict[str, float] = {}
    for axis in "XYZEF":
        m = re.search(rf"\b{axis}([\d.+-]+)", line, re.I)
        if m:
            coords[axis.lower()] = float(m.group(1))
    return coords


def simulate_toolhead_path(script: str, start: ToolheadState | None = None) -> list[dict[str, Any]]:
    """Replay injected gcode and return each XY move with coordinates."""
    th = start or ToolheadState()
    path: list[dict[str, Any]] = []
    for raw in script.splitlines():
        line = raw.strip()
        if line == "G90":
            th.absolute = True
            continue
        if line == "G91":
            th.absolute = False
            continue
        if not line.startswith("G1"):
            continue
        coords = parse_g1_axes(line)
        if "f" in coords:
            th.feed = coords["f"]
        prev = (th.x, th.y, th.z)
        for axis in "xyz":
            if axis in coords:
                if th.absolute:
                    setattr(th, axis, coords[axis])
                else:
                    setattr(th, axis, getattr(th, axis) + coords[axis])
        if "e" in coords:
            th.e += coords["e"]
        if "x" in coords or "y" in coords:
            path.append(
                {
                    "line": line,
                    "x": th.x,
                    "y": th.y,
                    "z": th.z,
                    "feed": th.feed,
                    "from": prev,
                }
            )
    return path


def polygon_contains(polygon: list[list[float]], x: float, y: float, pad: float = 1.0) -> bool:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return (min(xs) - pad) <= x <= (max(xs) + pad) and (min(ys) - pad) <= y <= (max(ys) + pad)


def iron_path_stays_on_object(
    cache: dict[str, Any], obj_name: str, layer: int
) -> tuple[bool, str]:
    obj = cache["objects"][obj_name]
    polygon = obj["polygon"]
    script, _ = build_inject_script(cache, obj_name, layer)
    path = simulate_toolhead_path(script)
    iron_started = False
    iron_moves: list[dict[str, Any]] = []
    for raw in script.splitlines():
        if ";TYPE:Ironing" in raw:
            iron_started = True
            continue
        if not iron_started:
            continue
        line = raw.strip()
        if line.startswith("G1") and ("X" in line.upper() or "Y" in line.upper()):
            for pt in path:
                if pt["line"] == line:
                    iron_moves.append(pt)
                    break
    if not iron_moves:
        iron_moves = path[-30:] if len(path) > 30 else path
    for pt in iron_moves:
        if not polygon_contains(polygon, pt["x"], pt["y"], pad=0.5):
            return False, (
                f"move {pt['line']} -> ({pt['x']:.3f},{pt['y']:.3f}) outside {obj_name}"
            )
    center = obj.get("center") or [0, 0]
    return True, f"{len(iron_moves)} iron moves, center~({center[0]:.1f},{center[1]:.1f})"


def simulate_blocking_watcher_race(
    cache: dict[str, Any],
    schedule: dict[str, Any],
    *,
    start_pos: int,
    iron_duration_sec: float,
    sdcard_bps: float,
) -> list[tuple[str, int, int, str]]:
    """Model old synchronous watcher: inject blocks while SD advances."""
    events: list[tuple[str, int, int, str]] = []
    sim_schedule = json.loads(json.dumps(schedule))
    file_pos = start_pos
    time_s = 0.0

    while file_pos < int(cache["print_end_byte"]) + 50:
        layer = iron_watcher.layer_from_file_position(cache, file_pos)
        waiting, ready = iron_watcher.pending_inject_targets(
            cache, sim_schedule, layer, file_pos, set()
        )
        if ready:
            trigger_byte, obj_name, target = ready[0]
            obj_sched = sim_schedule["objects"][obj_name]
            done = set(obj_sched.get("done") or [])
            if target not in done:
                events.append((obj_name, target, file_pos, "inject_start"))
                time_s += iron_duration_sec
                file_pos += int(iron_duration_sec * sdcard_bps)
                events.append((obj_name, target, file_pos, "inject_end"))
                done.add(target)
                obj_sched["done"] = sorted(done)
                continue
        if waiting:
            next_byte = waiting[0][0]
            if file_pos < next_byte:
                jump = min(next_byte - file_pos, int(sdcard_bps * 0.08))
                file_pos += max(jump, 1)
                time_s += 0.08
            else:
                file_pos += int(sdcard_bps * 0.08)
                time_s += 0.08
        else:
            file_pos += int(sdcard_bps * 0.08)
            time_s += 0.08
        if time_s > 300:
            break
    return events


def simulate_async_watcher(
    cache: dict[str, Any],
    schedule: dict[str, Any],
    *,
    start_pos: int,
    iron_duration_sec: float,
    sdcard_bps: float,
) -> list[tuple[str, int, int, str]]:
    """Model async watcher: poll continues during in-flight iron."""
    events: list[tuple[str, int, int, str]] = []
    sim_schedule = json.loads(json.dumps(schedule))
    file_pos = start_pos
    time_s = 0.0
    inflight: dict[tuple[str, int], float] = {}

    while time_s < 300 and file_pos < int(cache["print_end_byte"]) + 50:
        done_keys = {
            (n, t)
            for n, s in sim_schedule["objects"].items()
            for t in (s.get("done") or [])
        }
        finished = [
            k
            for k, end_t in list(inflight.items())
            if time_s >= end_t
        ]
        for key in finished:
            obj_name, target = key
            events.append((obj_name, target, file_pos, "inject_end"))
            obj_sched = sim_schedule["objects"][obj_name]
            done = set(obj_sched.get("done") or [])
            done.add(target)
            obj_sched["done"] = sorted(done)
            del inflight[key]

        layer = iron_watcher.layer_from_file_position(cache, file_pos)
        waiting, ready = iron_watcher.pending_inject_targets(
            cache, sim_schedule, layer, file_pos, set()
        )
        for trigger_byte, obj_name, target in ready:
            key = (obj_name, target)
            if key in inflight or key in done_keys:
                continue
            inflight[key] = time_s + iron_duration_sec
            events.append((obj_name, target, file_pos, "inject_start"))

        if waiting and not inflight:
            next_byte = waiting[0][0]
            if file_pos < next_byte:
                file_pos += max(min(next_byte - file_pos, int(sdcard_bps * 0.08)), 1)
        else:
            file_pos += int(sdcard_bps * 0.08)
        time_s += 0.08

        if schedule_all_pending_done(sim_schedule) and not inflight:
            break
    return events


def schedule_all_pending_done(schedule: dict[str, Any]) -> bool:
    for obj_sched in schedule.get("objects", {}).values():
        layers = obj_sched.get("layers") or []
        done = set(obj_sched.get("done") or [])
        if any(layer not in done for layer in layers):
            return False
    return True


def inject_would_succeed(cache: dict[str, Any], file_pos: int) -> bool:
    pe = cache.get("print_end_byte")
    margin = int(cache.get("min_inject_margin") or 64)
    if pe is None:
        return True
    return int(pe) - file_pos >= margin


def test_indexer(gcode_path: Path, cache_dir: Path) -> dict[str, Any]:
    iron_scheduler.CACHE_DIR = cache_dir
    cache = iron_scheduler.index_gcode(gcode_path)
    ver = cache.get("cache_version")
    pe = cache.get("print_end_byte")
    if ver != iron_scheduler.CACHE_VERSION:
        fail(f"index {gcode_path.name}", f"cache_version={ver}")
    else:
        ok(f"index {gcode_path.name}", f"v{ver} print_end={pe}")
    for name, obj in cache.get("objects", {}).items():
        inject = obj.get("inject_after_byte") or {}
        for layer, byte in inject.items():
            if pe is not None and byte >= pe:
                fail(f"{gcode_path.name} {name} L{layer}", f"inject {byte} >= PRINT_END {pe}")
            elif pe is not None:
                margin = pe - byte
                ok(
                    f"{gcode_path.name} {name} L{layer} trigger",
                    f"byte={byte} margin={margin}",
                )
    return cache


def test_3m55s_dual_object_flow(cache: dict[str, Any]) -> None:
    schedule = {
        "file": "Cube 1_ASA_3m55s.gcode",
        "version": 2,
        "active": True,
        "objects": {
            "Cube_1_id_0_copy_0": {"mode": "topmost", "layers": [10], "done": []},
            "Cube_1_id_1_copy_0": {"mode": "topmost", "layers": [10], "done": []},
        },
    }
    pe = int(cache["print_end_byte"])
    id0 = cache["objects"]["Cube_1_id_0_copy_0"]["inject_after_byte"]["10"]
    id1 = cache["objects"]["Cube_1_id_1_copy_0"]["inject_after_byte"]["10"]
    positions = [id1 - 50, id1 + 10, id0 - 50, id0 + 10, pe - 10]
    events = simulate_watcher_inject_plan(cache, schedule, positions)

    if len(events) != 2:
        fail("3m55s inject count", f"got {events}")
    else:
        ok("3m55s inject count", "2 objects")

    if events[0][0] != "Cube_1_id_1_copy_0":
        fail("3m55s inject order", f"first={events[0][0]} expected id_1")
    else:
        ok("3m55s inject order", "back-right (id_1) before front-left (id_0)")

    if len(events) < 2:
        fail("3m55s inject timing", f"need 2 events, got {events}")
    elif events[0][2] >= events[1][2]:
        fail("3m55s inject timing", f"rear must fire before front: {events}")
    else:
        ok(
            "3m55s inject timing",
            f"rear@{events[0][2]} then front@{events[1][2]}",
        )

    _, near0 = build_inject_script(cache, "Cube_1_id_0_copy_0", 10)
    _, near1 = build_inject_script(cache, "Cube_1_id_1_copy_0", 10)
    if not near0:
        fail("3m55s id_0 near_print_end", "should append PRINT_END")
    else:
        ok("3m55s id_0 near_print_end", "PRINT_END will run after iron")
    if near1:
        fail("3m55s id_1 near_print_end", "should NOT append PRINT_END")
    else:
        ok("3m55s id_1 near_print_end", "no PRINT_END (enough margin)")

    if pe - id0 > iron_scheduler.PRINT_END_MARGIN:
        fail("3m55s id_0 margin flag", f"margin={pe - id0}")
    else:
        ok("3m55s id_0 margin", f"{pe - id0} bytes before PRINT_END")


def test_byte_walk_handoff(cache: dict[str, Any]) -> None:
    """Simulate sdcard advancing byte-by-byte; front must inject before EOF."""
    schedule = {
        "file": "Cube 1_ASA_3m55s.gcode",
        "version": 2,
        "active": True,
        "objects": {
            "Cube_1_id_0_copy_0": {"mode": "topmost", "layers": [10], "done": []},
            "Cube_1_id_1_copy_0": {"mode": "topmost", "layers": [10], "done": []},
        },
    }
    id0 = int(cache["objects"]["Cube_1_id_0_copy_0"]["inject_after_byte"]["10"])
    id1 = int(cache["objects"]["Cube_1_id_1_copy_0"]["inject_after_byte"]["10"])
    pe = int(cache["print_end_byte"])
    min_margin = int(cache.get("min_inject_margin") or 64)

    positions = list(range(id1 - 200, pe + 50, 25))
    events = simulate_watcher_inject_plan(cache, schedule, positions)

    rear = [e for e in events if e[0] == "Cube_1_id_1_copy_0"]
    front = [e for e in events if e[0] == "Cube_1_id_0_copy_0"]
    if not rear or not front:
        fail("byte-walk handoff", f"missing events: {events}")
        return

    rear_pos, front_pos = rear[0][2], front[0][2]
    if rear_pos < id1:
        fail("byte-walk rear", f"rear@{rear_pos} before trigger {id1}")
    elif front_pos < id0:
        fail("byte-walk front", f"front@{front_pos} before trigger {id0}")
    elif front_pos >= pe - min_margin:
        fail(
            "byte-walk front EOF",
            f"front@{front_pos} too late (need <{pe - min_margin})",
        )
    elif rear_pos >= front_pos:
        fail("byte-walk order", f"rear@{rear_pos} not before front@{front_pos}")
    else:
        ok(
            "byte-walk handoff",
            f"rear@{rear_pos} front@{front_pos} margin={pe - front_pos}",
        )


def test_mock_inject_http(cache_dir: Path, cache: dict[str, Any]) -> None:
    printer = MockPrinter(
        state="printing",
        file_position=111600,
        file_size=int(cache["print_end_byte"]),
        filename="Cube 1_ASA_3m55s.gcode",
    )
    server, url = start_mock_moonraker(printer)
    old_env = {
        "PRINTER_DATA": os.environ.get("PRINTER_DATA"),
        "MOONRAKER_URL": os.environ.get("MOONRAKER_URL"),
    }
    try:
        os.environ["PRINTER_DATA"] = str(cache_dir.parent)
        os.environ["MOONRAKER_URL"] = url
        iron_scheduler.CACHE_DIR = cache_dir
        inject_iron.MOONRAKER_URL = url
        inject_iron.CACHE_DIR = cache_dir
        inject_iron.PRINTER_DATA = cache_dir.parent

        def run_inject(object_name: str, layer: int) -> tuple[bool, str]:
            try:
                inject_iron.inject_object("Cube 1_ASA_3m55s.gcode", object_name, layer)
                return True, ""
            except SystemExit as exc:
                return False, str(exc)

        ok1, err1 = run_inject("Cube_1_id_1_copy_0", 10)
        if ok1 and len(printer.scripts) == 1 and "PRINT_END" not in printer.scripts[0]:
            ok("mock inject id_1", "rear iron only, no PRINT_END")
        else:
            fail("mock inject id_1", err1 or f"scripts={len(printer.scripts)}")

        printer.file_position = 123450
        ok0, err0 = run_inject("Cube_1_id_0_copy_0", 10)
        if ok0:
            script = printer.scripts[-1]
            if "PRINT_END" in script and "IRON object=Cube_1_id_0_copy_0" in script:
                ok("mock inject id_0", "front iron + PRINT_END")
            else:
                fail("mock inject id_0", "missing PRINT_END tail")
        else:
            fail("mock inject id_0", err0)

        printer.file_position = int(cache["print_end_byte"]) - 50
        printer.state = "printing"
        late_ok, late_err = run_inject("Cube_1_id_0_copy_0", 10)
        if not late_ok and "Refusing iron inject" in late_err:
            ok("mock inject refuse late", late_err.split("\n")[0][:60])
        else:
            fail("mock inject refuse late", late_err or "should have refused")

        printer.state = "complete"
        done_ok, done_err = run_inject("Cube_1_id_0_copy_0", 10)
        if not done_ok and "complete" in done_err:
            ok("mock inject refuse complete", "state=complete blocked")
        else:
            fail("mock inject refuse complete", done_err or "should have refused")
    finally:
        server.shutdown()
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def replay_full_print_injects(
    gcode_path: Path, cache: dict[str, Any], object_names: list[str]
) -> None:
    """Walk every layer-10+ byte offset; verify inject fires once per object in order."""
    data = gcode_path.read_bytes()
    pe = int(cache["print_end_byte"])
    schedule = {
        "file": gcode_path.name,
        "version": 2,
        "active": True,
        "objects": {
            name: {
                "mode": "topmost",
                "layers": [cache["total_layers"]],
                "done": [],
            }
            for name in object_names
        },
    }
    # Sample byte positions: every 500 bytes from first inject trigger - 1000 to PRINT_END
    triggers = []
    for name in object_names:
        tb = cache["objects"][name]["inject_after_byte"][str(cache["total_layers"])]
        triggers.append(int(tb))
    start = max(0, min(triggers) - 1000)
    positions = list(range(start, pe, 500)) + [pe - 100, pe - 50, pe - 10]
    events = simulate_watcher_inject_plan(cache, schedule, positions)
    expected = len(object_names)
    if len(events) != expected:
        fail(f"replay {gcode_path.name}", f"events={events}")
        return
    ok(f"replay {gcode_path.name}", f"{expected} injects at bytes {[e[2] for e in events]}")
    sorted_triggers = sorted(
        (cache["objects"][n]["inject_after_byte"][str(cache["total_layers"])], n)
        for n in object_names
    )
    for i, (_, name) in enumerate(sorted_triggers):
        if events[i][0] != name:
            fail(f"replay order {gcode_path.name}", f"got {events[i][0]} want {name}")
            return
    ok(f"replay order {gcode_path.name}", "trigger order matches sorted bytes")


def test_blocking_vs_async_race(cache_3m: dict[str, Any]) -> None:
    """Prove blocking inject misses front object; async fires at correct byte."""
    schedule = {
        "file": "Cube 1_ASA_3m55s.gcode",
        "version": 2,
        "active": True,
        "objects": {
            "Cube_1_id_0_copy_0": {"mode": "topmost", "layers": [10], "done": []},
            "Cube_1_id_1_copy_0": {"mode": "topmost", "layers": [10], "done": []},
        },
    }
    id1 = int(cache_3m["objects"]["Cube_1_id_1_copy_0"]["inject_after_byte"]["10"])
    start = id1 + 400

    blocking = simulate_blocking_watcher_race(
        cache_3m,
        schedule,
        start_pos=start,
        iron_duration_sec=IRON_INJECT_DURATION_SEC,
        sdcard_bps=SDCARD_BYTES_PER_SEC,
    )
    rear_block = [
        e for e in blocking if e[0] == "Cube_1_id_1_copy_0" and e[3] == "inject_start"
    ]
    front_block = [
        e for e in blocking if e[0] == "Cube_1_id_0_copy_0" and e[3] == "inject_start"
    ]
    if rear_block:
        ok("blocking race rear", f"rear@{rear_block[0][2]}")
    else:
        fail("blocking race rear", "no rear inject")

    if front_block and not inject_would_succeed(cache_3m, front_block[0][2]):
        ok(
            "blocking race reproduces bug",
            f"front@{front_block[0][2]} too late (margin={int(cache_3m['print_end_byte']) - front_block[0][2]})",
        )
    elif not front_block:
        ok("blocking race reproduces bug", "front inject never started")
    else:
        fail("blocking race bug", f"front@{front_block[0][2]} unexpectedly ok")

    async_events = simulate_async_watcher(
        cache_3m,
        json.loads(json.dumps(schedule)),
        start_pos=start,
        iron_duration_sec=IRON_INJECT_DURATION_SEC,
        sdcard_bps=SDCARD_BYTES_PER_SEC,
    )
    front_async = [
        e for e in async_events if e[0] == "Cube_1_id_0_copy_0" and e[3] == "inject_start"
    ]
    if not front_async:
        fail("async race front", "front never injected")
    elif not inject_would_succeed(cache_3m, front_async[0][2]):
        fail("async race front", f"front@{front_async[0][2]} too late")
    else:
        ok(
            "async race front",
            f"inject@{front_async[0][2]} margin={int(cache_3m['print_end_byte']) - front_async[0][2]}",
        )


def test_toolhead_coords_per_object(cache: dict[str, Any], objects: list[str], layer: int) -> None:
    for name in objects:
        ok_path, detail = iron_path_stays_on_object(cache, name, layer)
        if ok_path:
            script, _ = build_inject_script(cache, name, layer)
            path = simulate_toolhead_path(script)
            if path:
                ok(f"toolhead {name}", f"{detail} last=({path[-1]['x']:.2f},{path[-1]['y']:.2f})")
            else:
                fail(f"toolhead {name}", "no moves")
        else:
            fail(f"toolhead {name}", detail)


def test_mixed_slicer_newtest(cache: dict[str, Any]) -> None:
    """One cube has native Orca iron; inject only the other with translated template."""
    layer = int(cache["total_layers"])
    slicer_obj = "Cube_1_id_0_copy_0"
    inject_obj = "Cube_1_id_1_copy_0"
    if not cache["objects"][slicer_obj].get("has_slicer_iron"):
        fail("newtest slicer flag", f"{slicer_obj} should have slicer iron")
        return
    if cache["objects"][inject_obj].get("has_slicer_iron"):
        fail("newtest inject flag", f"{inject_obj} should not have slicer iron")
        return
    settings = cache.get("ironing_settings") or {}
    if settings.get("template_source") != slicer_obj:
        fail("newtest template_source", f"got {settings.get('template_source')}")
        return
    ok("newtest template_source", slicer_obj)
    if settings.get("speed") != 30.0 or settings.get("spacing") != 0.15:
        fail("newtest slicer settings", f"speed={settings.get('speed')} spacing={settings.get('spacing')}")
        return
    ok("newtest slicer settings", "30 mm/s, 0.15 spacing from CONFIG block")

    inject_src = cache["objects"][inject_obj]["layer_sources"][str(layer)]
    if inject_src != "template":
        fail("newtest inject source", f"want template got {inject_src}")
        return
    ok("newtest inject source", "orca template translated from slicer cube")

    if not settings.get("uses_native_approach"):
        fail("newtest native approach", "inject should copy slicer approach path")
        return
    ok("newtest native approach", "orca approach copied from front cube")

    script, near_eof = build_inject_script(cache, inject_obj, layer)
    if "F3600" not in script or "F1800" not in script:
        fail("newtest inject script feeds", "missing native travel F3600 or iron F1800")
        return
    if "SET_VELOCITY_LIMIT ACCEL=5000" not in script:
        fail("newtest inject script feeds", "missing native iron velocity limit")
        return
    if "X109.536" in script or "slicer-style iron approach" in script:
        fail("newtest inject script path", "still using synthetic approach")
        return
    if "X179.536" not in script and "X179.275" in script:
        fail("newtest inject script path", "expected translated native approach coords")
        return
    ok("newtest inject script feeds", "native travel F3600 + iron F1800 + accel")
    if near_eof:
        fail("newtest inject margin", "rear inject should not be near PRINT_END")
        return
    ok("newtest inject margin", "rear trigger has room — no PRINT_END append")

    schedule = {
        "file": "NewTest.gcode",
        "version": 2,
        "active": True,
        "objects": {
            inject_obj: {"mode": "topmost", "layers": [layer], "done": []},
        },
    }
    trigger = int(cache["objects"][inject_obj]["inject_after_byte"][str(layer)])
    events = simulate_async_watcher(
        cache,
        schedule,
        start_pos=trigger - 200,
        iron_duration_sec=45.0,
        sdcard_bps=SDCARD_BYTES_PER_SEC,
    )
    starts = [e for e in events if e[3] == "inject_start"]
    if len(starts) != 1 or starts[0][0] != inject_obj:
        fail("newtest single inject", f"got {starts}")
        return
    ok("newtest single inject", f"one inject@{starts[0][2]}")


def test_3cube_inject_flow(cache: dict[str, Any], gcode_name: str) -> None:
    layer = int(cache["total_layers"])
    objects = ["Cube_id_2_copy_0", "Cube_id_1_copy_0", "Cube_id_0_copy_0"]
    schedule = {
        "file": gcode_name,
        "version": 2,
        "active": True,
        "objects": {
            name: {"mode": "topmost", "layers": [layer], "done": []}
            for name in objects
        },
    }
    triggers = sorted(
        int(cache["objects"][n]["inject_after_byte"][str(layer)]) for n in objects
    )
    start = triggers[0] - 500
    events = simulate_async_watcher(
        cache,
        schedule,
        start_pos=start,
        iron_duration_sec=45.0,
        sdcard_bps=SDCARD_BYTES_PER_SEC,
    )
    starts = [e for e in events if e[3] == "inject_start"]
    if len(starts) != 3:
        fail("3-cube inject count", f"got {starts}")
        return
    ok("3-cube inject count", "all 3 cubes scheduled")

    for i, name in enumerate(
        sorted(objects, key=lambda n: cache["objects"][n]["inject_after_byte"][str(layer)])
    ):
        if starts[i][0] != name:
            fail("3-cube order", f"want {name} got {starts[i][0]}")
            return
    ok("3-cube inject order", "byte-sorted rear->front")

    for obj_name, _, file_pos, _ in starts:
        if not inject_would_succeed(cache, file_pos):
            fail(f"3-cube margin {obj_name}", f"inject@{file_pos} too late")
            return
    ok("3-cube inject margins", "all injects before PRINT_END guard")

    test_toolhead_coords_per_object(cache, objects, layer)


def test_byte_regression_vs_old_bug(cache: dict[str, Any]) -> None:
    """Old bug used EXCLUDE_OBJECT_END; new must be earlier with margin."""
    pe = int(cache["print_end_byte"])
    id0 = int(cache["objects"]["Cube_1_id_0_copy_0"]["inject_after_byte"]["10"])
    old_bug_byte = 123579
    if id0 >= old_bug_byte:
        fail("regression id_0 byte", f"still {id0} >= old {old_bug_byte}")
    else:
        ok("regression id_0 byte", f"{id0} < old {old_bug_byte}")
    if pe - id0 < 64:
        fail("regression id_0 time margin", f"only {pe - id0} bytes — tight")
    else:
        ok("regression id_0 time margin", f"{pe - id0} bytes before PRINT_END")


def main() -> int:
    global PASS, FAIL
    print("=" * 60)
    print("IRON BACKEND SIMULATION")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="iron-sim-") as tmp:
        base = Path(tmp)
        cache_dir = base / "iron_cache"
        cache_dir.mkdir(parents=True)
        gcode_copy = base / "gcodes"
        gcode_copy.mkdir()
        shutil.copy2(GCODE_DIR / "Cube 1_ASA_3m55s.gcode", gcode_copy)
        shutil.copy2(GCODE_DIR / "Cube 1_ASA_5m10s.gcode", gcode_copy)
        shutil.copy2(GCODE_DIR / "Cube 4_ASA_4m23s.gcode", gcode_copy)
        shutil.copy2(GCODE_DIR / "NewTest.gcode", gcode_copy)

        iron_scheduler.GCODE_DIR = gcode_copy
        iron_scheduler.CACHE_DIR = cache_dir

        print("\n[1] Indexer tests")
        cache_3m = test_indexer(gcode_copy / "Cube 1_ASA_3m55s.gcode", cache_dir)
        test_indexer(gcode_copy / "Cube 1_ASA_5m10s.gcode", cache_dir)

        print("\n[2] Watcher inject plan (3m55s dual cube)")
        test_3m55s_dual_object_flow(cache_3m)

        cache_5m_path = cache_dir / "Cube 1_ASA_5m10s.gcode.json"
        cache_5m = json.loads(cache_5m_path.read_text())

        print("\n[3] Full byte-replay inject simulation")
        replay_full_print_injects(
            gcode_copy / "Cube 1_ASA_3m55s.gcode",
            cache_3m,
            ["Cube_1_id_0_copy_0", "Cube_1_id_1_copy_0"],
        )
        replay_full_print_injects(
            gcode_copy / "Cube 1_ASA_5m10s.gcode",
            cache_5m,
            ["Cube_1_id_0_copy_0", "Cube_1_id_1_copy_0"],
        )

        print("\n[4] Byte regression vs last failed print")
        test_byte_regression_vs_old_bug(cache_3m)
        id1_5m = int(
            cache_5m["objects"]["Cube_1_id_1_copy_0"]["inject_after_byte"]["15"]
        )
        if id1_5m < 174524:
            ok("regression 5m10s id_1 byte", f"{id1_5m} < old 174524")
        else:
            fail("regression 5m10s id_1 byte", f"still {id1_5m}")

        print("\n[5] Mock Moonraker HTTP inject")
        test_mock_inject_http(cache_dir, cache_3m)

        print("\n[6] Script content checks")
        script0, near0 = build_inject_script(cache_3m, "Cube_1_id_0_copy_0", 10)
        script1, near1 = build_inject_script(cache_3m, "Cube_1_id_1_copy_0", 10)
        if "X109.275" in script0 and near0 and script0.strip().endswith("PRINT_END"):
            ok("script id_0 content", "front-left coords + PRINT_END tail")
        else:
            fail("script id_0 content")
        if "X179.275" in script1 and not near1:
            ok("script id_1 content", "back-right coords, no PRINT_END")
        else:
            fail("script id_1 content")
        if "MOVE=0" in script0 and "MOVE=1" not in script0:
            ok("script RESTORE MOVE=0", "no head jump between objects")
        else:
            fail("script RESTORE MOVE=0")

        print("\n[7] Byte-walk handoff (rear done -> front before EOF)")
        test_byte_walk_handoff(cache_3m)

        print("\n[8] SD race: blocking vs async watcher (real failure mode)")
        test_blocking_vs_async_race(cache_3m)

        print("\n[9] Three-cube environment (Cube 4, 3 objects)")
        cache_4 = test_indexer(gcode_copy / "Cube 4_ASA_4m23s.gcode", cache_dir)
        test_3cube_inject_flow(cache_4, "Cube 4_ASA_4m23s.gcode")

        print("\n[10] Toolhead paths — dual cube 3m55s")
        test_toolhead_coords_per_object(
            cache_3m,
            ["Cube_1_id_1_copy_0", "Cube_1_id_0_copy_0"],
            10,
        )

        print("\n[11] Mixed slicer + inject (NewTest.gcode)")
        cache_new = test_indexer(gcode_copy / "NewTest.gcode", cache_dir)
        test_mixed_slicer_newtest(cache_new)

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())