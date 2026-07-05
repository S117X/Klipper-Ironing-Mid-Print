# Resume: Iron Scheduler install (run ON THE PRINTER)

**Cursor session name suggestion:** `iron-scheduler printer install`

**When resumed on the printer, paste this to the agent:**

> Resume iron scheduler install. We are SSH'd on the printer now (Linux, `/home/x/printer_data`). Run `install-iron-scheduler.sh`, verify files, do NOT restart Klipper unless I say so.

---

## What was prepared (may need sync from Mac first)

If you edited on Mac, sync `Desktop/printer_data` to the Pi `~/printer_data` before running install.

Files that must exist on the printer:

```
~/printer_data/
├── install-iron-scheduler.sh
├── scripts/iron_scheduler.py
├── scripts/inject_iron.py
├── scripts/iron_watcher.py
├── config/iron_scheduler.cfg
└── config/printer.cfg   (needs [include iron_scheduler.cfg] + [respond])
```

---

## Install on printer (you run manually)

```bash
cd ~/printer_data
chmod +x install-iron-scheduler.sh scripts/*.py
PRINTER_DATA=~/printer_data ./install-iron-scheduler.sh
```

Verify:

```bash
grep iron_scheduler config/printer.cfg
grep '^\[respond\]' config/printer.cfg
ls -la scripts/iron_*.py scripts/inject_iron.py config/iron_scheduler.cfg
```

---

## After install (manual)

1. Mainsail → Machine → Macros → status panel button: **Iron Object** → `IRON_MENU`
2. When not printing: `FIRMWARE_RESTART`

---

## Requires

- `[exclude_object]` in printer.cfg (already there)
- OrcaSlicer: Label objects enabled
- Mainsail v2.9+ (macro prompts)