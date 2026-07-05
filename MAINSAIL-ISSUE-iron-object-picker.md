# GitHub Issue Draft — Mainsail

**Copy everything below the line into a new issue at:**  
https://github.com/mainsail-crew/mainsail/issues/new

---

## Feature request: Native “Iron Object” bed-map picker (mid-print per-object ironing)

### Summary

Add first-class UI support for **scheduling ironing on remaining top layers of a single object mid-print**, using the same **2D bed-map interaction model as Exclude Objects** — not a text list or macro prompt.

This is for multi-object prints where the user decides *during the print* that one part needs ironing on its remaining top surface layers (OrcaSlicer / SuperSlicer “label objects” + Klipper `[exclude_object]`).

---

### Problem

Today, Mainsail has excellent **Exclude Object** UX: status-panel button → modal → bed map + object list → confirm.

There is **no equivalent for ironing one object**:

- Klipper macros can schedule work, but Mainsail only exposes them as **text buttons / console commands**.
- `printer.gcode.script` **queues behind the active SD print** and can hang for minutes — unsuitable for responsive UI during printing.
- Users expect to **click the object on the bed**, pick a mode, and get immediate feedback — same as exclude.

We prototyped an external overlay (`/iron-picker/`) and a Moonraker API (`/server/iron/enable`) to prove the flow. It works, but patching the Mainsail bundle and maintaining a parallel UI is fragile and not sustainable.

---

### Proposed UX (match Exclude Objects)

**Entry point**

- Status panel button: **Iron Object** (visible while `printing` / `paused`, when `exclude_object.objects.length > 0`)
- Same overflow / multi-function menu behavior as Exclude Object when multiple actions compete for space

**Dialog**

- Modal (~900px desktop, fullscreen mobile) — same shell as `StatusPanelExcludeObjectDialog`
- **Left / main panel:** 2D bed map from `exclude_object` polygons + `toolhead.axis_min/max` (reuse map component logic)
- Click object → secondary step or inline panel: **Top Surface Only** vs **All Top Layers**
- Close / cancel without leaving Mainsail
- No navigation to a separate page

**Feedback**

- Immediate success or error in-dialog (not “request sent — check console”)
- Clear errors, e.g.:
  - object has no ironable top layers in gcode index
  - print not active
  - object excluded

---

### Backend contract (Moonraker / Klipper — can be core or companion)

Mainsail should **not** call `printer.gcode.script` for this during an active print.

Recommended API (names flexible):

```
POST /server/iron/enable
{
  "file": "<print_stats.filename>",
  "object": "<exclude_object name>",
  "mode": "topmost" | "all_top"
}

→ { "ok": true, "object": "...", "scheduled_layers": [122,123,124,125] }
→ { "ok": false, "error": "..." }   // HTTP 400
```

Implementation notes from our prototype (for whoever builds the server side):

| Topic | Detail |
|--------|--------|
| Object names | Klipper `exclude_object` names may differ in **case** from gcode `EXCLUDE_OBJECT_DEFINE` — server must match case-insensitively |
| Layer tracking | Many slicer gcodes lack `SET_PRINT_STATS_INFO` / `current_layer` — layer detection may need **virtual_sdcard.file_position** + pre-indexed layer byte offsets, with Z fallback |
| Injection | Remaining top-layer iron moves are injected via Moonraker `gcode/script` at layer boundaries (watcher), not queued through the UI path |
| Index | Per-file cache: object → layer → iron gcode snippets, derived from top-shell layers + infill geometry |
| Permissions | Same auth as other `/server/*` endpoints |

Klipper requirements (document in issue / docs):

- `[exclude_object]`
- Slicer: **Label objects** enabled
- Optional: `gcode_shell_command` or pure Moonraker implementation

---

### What we validated externally (reference only)

Custom stack on Klipper + Moonraker + Mainsail (not part of Mainsail core today):

- `iron_scheduler.py` — index gcode, build cache, schedule layers
- `iron_watcher.py` — detect layer, call inject
- `inject_iron.py` — stream iron snippets mid-print
- Moonraker component `iron_enable` → `/server/iron/enable` (fast, does not block on Klipper queue)
- Bed-map overlay mimicking exclude dialog (polygons, grid, axis, legend)

**Limitations of the prototype** (why we want native Mainsail):

- Mainsail bundle patch for status-panel button breaks on every update
- Separate CSS/JS overlay duplicates exclude map logic
- Second HTTP stack / service wiring is easy to misconfigure (502 → Safari JSON parse errors)
- No integration with Mainsail state store / i18n / theming

---

### ETA / remaining time — **needs design (included in scope discussion)**

**Open question:** When ironing is scheduled mid-print, **total remaining time increases**, but we are **not sure** the current Mainsail ETA accounts for injected gcode.

Observed behavior today:

- Mainsail ETA / progress typically reflects **original gcode file** analysis (slicer metadata, Moonraker analysis, layer count, etc.)
- Mid-print injected iron passes add **extra motion + extrusion time** per affected layer(s)
- Our prototype **does not** update ETA, `print_stats`, or any Mainsail progress model after scheduling

**Requested Mainsail behavior (to define / implement):**

1. After successful `iron/enable`, **adjust displayed ETA** (or show “+N min ironing”) based on:
   - scheduled layer count × estimated iron pass duration from index (length, feedrate, flow), or
   - Moonraker analysis re-run on pending injected segments, or
   - explicit `additional_duration` returned by `/server/iron/enable`
2. If exact ETA adjustment is too heavy for v1, show a **non-blocking notice**: “Iron scheduled — expect additional time on layers X–Y”
3. Document whether this hooks into existing **Analysis / History / Progress** components

**Acceptance criteria for ETA (when implemented):**

- [ ] Scheduling iron on layers 122–125 visibly increases remaining time (or shows additive notice) before those layers complete
- [ ] ETA does not **decrease** or stay unchanged when significant iron time is added
- [ ] Completing iron layers does not confuse progress % (define expected behavior)

---

### Suggested Mainsail implementation approach

1. **UI:** New `StatusPanelIronObjectDialog` mirroring exclude dialog structure:
   - `StatusPanelIronObjectDialogMap` (reuse / extract shared map component with exclude)
   - `StatusPanelIronObjectDialogActions` (mode buttons + status text)
2. **Store:** Subscribe to `exclude_object`, `toolhead`, `print_stats` (already available)
3. **API:** `$socket` or HTTP POST to `/server/iron/enable` (not `printer.gcode.script`)
4. **i18n:** `Panels.StatusPanel.IronObject.*` strings
5. **Settings (optional):** Toggle status-panel button like other actions

---

### Acceptance criteria (MVP)

- [ ] **Iron Object** button in status panel during print when labeled objects exist
- [ ] Modal bed-map picker; click object to select
- [ ] **Top Surface Only** and **All Top Layers** modes
- [ ] API response drives in-dialog success/error within ~1s
- [ ] Works with uppercase `exclude_object` names from Klipper vs mixed-case gcode define names
- [ ] No full-page redirect; no dependency on editing minified bundle
- [ ] Document Klipper/Moonraker companion requirements (or ship as optional Moonraker component)

### Acceptance criteria (ETA — follow-up / same epic)

- [ ] Remaining time reflects or annotates added ironing duration (see ETA section)
- [ ] UX decision documented if exact ETA is deferred

---

### Environment tested

- Mainsail (stable channel), Moonraker, Klipper
- OrcaSlicer gcode with `EXCLUDE_OBJECT_DEFINE` / `EXCLUDE_OBJECT_START`
- Multi-object plate (housing + small parts)
- Board: Orange Pi 5 Plus (aarch64) — performance should not block UI path

---

### Why this fits Mainsail

Exclude Objects proved users understand **bed-map selection** mid-print. Per-object ironing is the same mental model: pick the part on the plate, confirm action, keep printing. Native integration avoids brittle overlays and makes the feature maintainable across releases.

---

### Labels to suggest

`enhancement`, `status-panel`, `discussion` (for ETA design)