# Moonraker API for mid-print iron scheduling (bypasses Klipper gcode queue)
#
# Copyright (C) 2026
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common import RequestType

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest

PRINTER_DATA = Path(os.environ.get("PRINTER_DATA", "/home/x/printer_data"))
CACHE_DIR = PRINTER_DATA / "iron_cache"
SCRIPT = PRINTER_DATA / "scripts" / "iron_scheduler.py"
MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://127.0.0.1:7125")


def load_component(config: ConfigHelper) -> IronEnableComponent:
    return IronEnableComponent(config)


class IronEnableComponent:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.server.register_endpoint(
            "/server/iron/enable", RequestType.POST, self._handle_enable
        )
        self.server.register_endpoint(
            "/server/iron/health", RequestType.GET, self._handle_health
        )
        self.server.register_endpoint(
            "/server/iron/schedule", RequestType.GET, self._handle_schedule
        )
        self.server.register_endpoint(
            "/server/iron/cache", RequestType.GET, self._handle_cache
        )
        logging.info("Iron enable API loaded at /server/iron/enable")

    async def _handle_health(self, web_request: WebRequest) -> dict[str, bool]:
        return {"ok": True}

    async def _handle_cache(self, web_request: WebRequest) -> dict[str, Any]:
        args = web_request.get_args()
        filename = args.get("file") or args.get("filename")
        if not filename:
            raise self.server.error("file required", 400)
        return await asyncio.to_thread(_read_cache, str(filename))

    async def _handle_schedule(self, web_request: WebRequest) -> dict[str, Any]:
        args = web_request.get_args()
        filename = args.get("file") or args.get("filename")
        if not filename:
            raise self.server.error("file required", 400)
        return await asyncio.to_thread(_read_schedule, str(filename))

    async def _handle_enable(self, web_request: WebRequest) -> dict[str, Any]:
        args = web_request.get_args()
        filename = args.get("file") or args.get("filename")
        obj = args.get("object")
        mode = args.get("mode", "topmost")
        if not filename or not obj:
            raise self.server.error("file and object required", 400)

        result = await asyncio.to_thread(
            _run_enable, str(filename), str(obj), str(mode)
        )
        if not result.get("ok"):
            raise self.server.error(result.get("error", "iron enable failed"), 400)
        return result


def _gcode_path(filename: str) -> Path | None:
    name = Path(filename).name
    for candidate in (PRINTER_DATA / "gcodes" / name, Path(filename)):
        if candidate.is_file():
            return candidate
    return None


def _ensure_indexed(filename: str) -> None:
    gcode = _gcode_path(filename)
    if not gcode:
        return
    cache_path = CACHE_DIR / f"{Path(filename).name}.json"
    try:
        stale = (
            not cache_path.is_file()
            or cache_path.stat().st_mtime < gcode.stat().st_mtime
        )
    except OSError:
        stale = True
    if not stale:
        return
    subprocess.run(
        [sys.executable, str(SCRIPT), "index", "--file", gcode.name],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(PRINTER_DATA),
    )


def _drop_stale_schedule_file(
    name: str, data: dict[str, Any], print_duration: float, print_state: str
) -> bool:
    path = CACHE_DIR / f"{name}.schedule.json"
    if print_state in ("complete", "standby", "cancelled"):
        path.unlink(missing_ok=True)
        return True
    gcode = _gcode_path(name)
    if gcode:
        try:
            gcode_mt = gcode.stat().st_mtime
            sched_gcode_mt = float(data.get("gcode_mtime") or 0)
            if sched_gcode_mt and gcode_mt > sched_gcode_mt + 0.5:
                path.unlink(missing_ok=True)
                return True
        except OSError:
            pass
    sched_at = data.get("print_duration_at_schedule")
    if sched_at is not None and print_duration + 5 < float(sched_at):
        path.unlink(missing_ok=True)
        return True
    return False


def _read_cache(filename: str) -> dict[str, Any]:
    _ensure_indexed(filename)
    name = Path(filename).name
    path = CACHE_DIR / f"{name}.json"
    if not path.is_file():
        return {"ok": False, "error": "not indexed"}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid cache"}
    objects: dict[str, Any] = {}
    for obj_name, obj in data.get("objects", {}).items():
        objects[obj_name] = {
            "has_slicer_iron": bool(obj.get("has_slicer_iron")),
            "layers": sorted(obj.get("layers", {}).keys(), key=int),
            "layer_sources": obj.get("layer_sources", {}),
        }
    return {
        "ok": True,
        "objects": objects,
        "ironing_settings": data.get("ironing_settings", {}),
        "total_layers": data.get("total_layers"),
    }


def _read_schedule(filename: str) -> dict[str, Any]:
    name = Path(filename).name
    path = CACHE_DIR / f"{name}.schedule.json"
    if not path.is_file():
        return {"ok": True, "schedule": None}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return {"ok": True, "schedule": None}
    if data.get("file") != name:
        path.unlink(missing_ok=True)
        return {"ok": True, "schedule": None}

    stats = _current_print_stats()
    print_duration = float(stats.get("print_duration") or 0)
    print_state = str(stats.get("state") or "")
    if _drop_stale_schedule_file(name, data, print_duration, print_state):
        return {"ok": True, "schedule": None}
    return {"ok": True, "schedule": data}


def _current_print_stats() -> dict[str, Any]:
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            f"{MOONRAKER_URL}/printer/objects/query?print_stats", method="GET"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
        return payload.get("result", {}).get("status", {}).get("print_stats", {})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return {}


def _run_enable(filename: str, obj: str, mode: str) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(SCRIPT),
        "enable",
        "--file",
        filename,
        "--object",
        obj,
        "--mode",
        mode,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "iron enable timed out"}

    stdout = (proc.stdout or "").strip()
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {
        "ok": False,
        "error": (proc.stderr or stdout or f"exit {proc.returncode}").strip(),
    }