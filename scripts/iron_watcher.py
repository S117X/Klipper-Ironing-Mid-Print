#!/usr/bin/env python3
"""Compatibility shim — injection lives in iron_inject_trigger.py."""

from iron_inject_trigger import (  # noqa: F401
    get_current_layer,
    get_file_position,
    get_print_status,
    inject_after_byte,
    layer_from_file_position,
    load_cache,
    pending_inject_targets,
    poll_interval,
    schedule_has_pending_iron,
)

if __name__ == "__main__":
    from iron_inject_trigger import main

    raise SystemExit(main())