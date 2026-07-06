#!/usr/bin/env python3
"""Offline + mock-Moonraker simulation for iron scheduler / watcher / inject."""

from __future__ import annotations

import json
import os
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
        payload = json.loads(raw.decode())
        if self.path.endswith("/printer/gcode/script"):
            script = payload.get("script", "")
            self.printer.scripts.append(script)
            if "PRINT_END" in script:
                self.printer.homed_axes = "xyz"
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
    """Return inject events (object, layer, file_pos) watcher would fire."""
    events: list[tuple[str, int, int]] = []
    done: dict[str, set[int]] = {
        name: set(obj.get("done") or [])
        for name, obj in (schedule.get("objects") or {}).items()
    }

    for file_pos in sorted(file_positions):
        layer = iron_watcher.layer_from_file_position(cache, file_pos)
        ready: list[tuple[int, str, int]] = []
        for obj_name, obj_sched in (schedule.get("objects") or {}).items():
            for target in obj_sched.get("layers") or []:
                if target in done.get(obj_name, set()):
                    continue
                if layer < target:
                    continue
                trigger = iron_watcher.inject_after_byte(cache, obj_name, target)
                if trigger is not None and file_pos < trigger:
                    continue
                ready.append((trigger or 0, obj_name, target))
        if not ready:
            continue
        ready.sort(key=lambda item: item[0])
        _, obj_name, target = ready[0]
        key = (obj_name, target)
        if any(e[0] == obj_name and e[1] == target for e in events):
            continue
        events.append((obj_name, target, file_pos))
        done.setdefault(obj_name, set()).add(target)
    return events


def build_inject_script(
    cache: dict[str, Any], obj_name: str, layer: int
) -> tuple[str, bool]:
    """Mirror inject_iron.main script assembly without HTTP."""
    obj = cache["objects"][obj_name]
    snippet = obj["layers"][str(layer)]
    print_end_byte = cache.get("print_end_byte")
    print_end_margin = int(cache.get("print_end_margin") or 2000)
    trigger_byte = obj["inject_after_byte"][str(layer)]
    near = bool((obj.get("inject_near_print_end") or {}).get(str(layer)))
    if not near and print_end_byte is not None:
        near = int(print_end_byte) - int(trigger_byte) <= print_end_margin
    lines = [
        f"; IRON object={obj_name} layer={layer}",
        "SAVE_GCODE_STATE NAME=IRON_STATE",
    ]
    lines.extend(ln.strip() for ln in snippet.splitlines() if ln.strip())
    lines.append("RESTORE_GCODE_STATE NAME=IRON_STATE MOVE=0")
    if near:
        lines.append("PRINT_END")
    return "\n".join(lines) + "\n", near


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
            argv = [
                "inject_iron.py",
                "--file",
                "Cube 1_ASA_3m55s.gcode",
                "--object",
                object_name,
                "--layer",
                str(layer),
            ]
            old_argv = sys.argv
            sys.argv = argv
            try:
                inject_iron.main()
                return True, ""
            except SystemExit as exc:
                return False, str(exc)
            finally:
                sys.argv = old_argv

        ok1, err1 = run_inject("Cube_1_id_1_copy_0", 10)
        if ok1 and len(printer.scripts) == 1 and "PRINT_END" not in printer.scripts[0]:
            ok("mock inject id_1", "script sent, no PRINT_END")
        else:
            fail("mock inject id_1", err1 or f"scripts={len(printer.scripts)}")

        printer.file_position = 123450
        ok0, err0 = run_inject("Cube_1_id_0_copy_0", 10)
        if ok0:
            script = printer.scripts[-1]
            if "PRINT_END" in script and "RESTORE_GCODE_STATE" in script:
                ok("mock inject id_0", "iron + PRINT_END in one script")
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

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())