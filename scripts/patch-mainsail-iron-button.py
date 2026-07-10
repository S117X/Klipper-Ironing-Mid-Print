#!/usr/bin/env python3
"""Add Iron Object toolbar button to Mainsail status panel (in-page bed-map dialog).

UI layout goal
--------------
Exclude Object and Iron Object must sit **side by side** on the status toolbar
(like a "hat" of buttons), not collapsed behind Mainsail's multi-function
dropdown arrow.

Mainsail hides toolbar entries when multiFunctionButton is true (more than one
multi-function menu item during a print). Adding Iron to that menu triggers the
dropdown. This patch:
  1. Adds Iron next to Exclude on the main toolbar
  2. Keeps Exclude + Iron visible even when multi-function would activate
  3. Leaves Iron out of the multi-function dropdown (or strips it if present)
  4. Forces multiFunctionButton off so the arrow menu does not swallow them
"""

from __future__ import annotations

import shutil
from pathlib import Path

MAINSAIL_ASSETS = Path("/home/x/mainsail/assets")
BUNDLE_GLOB = "index-*.js"
OPEN_DIALOG = "btnIronObject(){window.openIronObjectDialog&&window.openIronObjectDialog()}"
REDIRECT = 'btnIronObject(){window.location.href="/iron-picker/"}'

# Stock: collapse toolbar into dropdown when >1 multi-function items.
MULTIFUNC_ON = (
    'get multiFunctionButton(){return["paused","printing"].includes(this.printer_state)'
    "?this.multiFunctionMenuButtonsFiltered.length>1:!1}"
)
# Always show full toolbar buttons (Exclude + Iron side by side).
MULTIFUNC_OFF = "get multiFunctionButton(){return!1}"

# Iron entry as it was first injected (hides when multiFunctionButton is true).
IRON_TOOLBAR_OLD = (
    '{text:"Iron Object",color:"primary",icon:va,loadingName:"ironObjectButton",'
    "status:()=>this.multiFunctionButton||this.printing_objects.length<1?!1:"
    '["paused","printing"].includes(this.printer_state),click:this.btnIronObject},'
)
# Always-visible Iron (ignore multi-function collapse).
IRON_TOOLBAR_NEW = (
    '{text:"Iron Object",color:"primary",icon:va,loadingName:"ironObjectButton",'
    "status:()=>this.printing_objects.length<1?!1:"
    '["paused","printing"].includes(this.printer_state),click:this.btnIronObject},'
)

# Exclude: stock / patched variants that hide under multi-function.
EXCLUDE_TOOLBAR_MF = (
    '{text:this.$t("Panels.StatusPanel.ExcludeObject.ExcludeObject"),color:"warning",icon:n1,'
    'loadingName:"excludeObjectButton",status:()=>this.multiFunctionButton||this.printing_objects.length<2?!1:'
    '["paused","printing"].includes(this.printer_state),click:this.btnExcludeObject},'
)
EXCLUDE_TOOLBAR_MF_LEN1 = (
    '{text:this.$t("Panels.StatusPanel.ExcludeObject.ExcludeObject"),color:"warning",icon:n1,'
    'loadingName:"excludeObjectButton",status:()=>this.multiFunctionButton||this.printing_objects.length<1?!1:'
    '["paused","printing"].includes(this.printer_state),click:this.btnExcludeObject},'
)
EXCLUDE_TOOLBAR_SIDE = (
    '{text:this.$t("Panels.StatusPanel.ExcludeObject.ExcludeObject"),color:"warning",icon:n1,'
    'loadingName:"excludeObjectButton",status:()=>this.printing_objects.length<1?!1:'
    '["paused","printing"].includes(this.printer_state),click:this.btnExcludeObject},'
)

# Iron inside multi-function dropdown (causes the arrow menu) — remove it.
IRON_MENU = (
    '{text:"Iron Object",loadingName:"ironObjectButton",icon:va,status:()=>this.printing_objects.length>0,'
    'disabled:()=>["paused","printing"].includes(this.printer_state),click:this.btnIronObject},'
)


def find_bundle() -> Path:
    matches = sorted(
        p for p in MAINSAIL_ASSETS.glob(BUNDLE_GLOB) if ".bak" not in p.name
    )
    if not matches:
        raise SystemExit(f"No Mainsail bundle found in {MAINSAIL_ASSETS}")
    return matches[-1]


def patch(data: str) -> str:
    changed = False

    # --- method: open in-page dialog ---
    if REDIRECT in data:
        data = data.replace(REDIRECT, OPEN_DIALOG)
        print("Updated Iron Object button: redirect -> in-page dialog")
        changed = True
    elif OPEN_DIALOG not in data:
        old_method = "btnExcludeObject(){this.boolShowObjects=!0}btnPauseAtLayer()"
        new_method = (
            "btnExcludeObject(){this.boolShowObjects=!0}"
            f"{OPEN_DIALOG}"
            "btnPauseAtLayer()"
        )
        if old_method not in data:
            raise SystemExit("Could not find btnExcludeObject hook in Mainsail bundle")
        data = data.replace(old_method, new_method, 1)
        print("Added btnIronObject() method")
        changed = True
    else:
        print("btnIronObject already opens in-page dialog")

    # --- toolbar: ensure Iron sits next to Exclude ---
    if IRON_TOOLBAR_NEW in data:
        print("Iron toolbar button already side-by-side (always visible)")
    elif IRON_TOOLBAR_OLD in data:
        data = data.replace(IRON_TOOLBAR_OLD, IRON_TOOLBAR_NEW, 1)
        print("Updated Iron toolbar: always visible (no multi-function hide)")
        changed = True
    elif "loadingName:\"ironObjectButton\"" not in data:
        # Fresh install: insert after Exclude entry (stock length<2 form).
        stock_exclude = (
            '{text:this.$t("Panels.StatusPanel.ExcludeObject.ExcludeObject"),color:"warning",icon:n1,'
            'loadingName:"excludeObjectButton",status:()=>this.multiFunctionButton||this.printing_objects.length<2?!1:'
            '["paused","printing"].includes(this.printer_state),click:this.btnExcludeObject},'
            '{text:this.$t("Panels.StatusPanel.PauseAtLayer.PauseAtLayer")'
        )
        with_iron = (
            EXCLUDE_TOOLBAR_SIDE
            + IRON_TOOLBAR_NEW
            + '{text:this.$t("Panels.StatusPanel.PauseAtLayer.PauseAtLayer")'
        )
        if stock_exclude not in data:
            # try already-partial length<1 form without iron
            stock_exclude = (
                '{text:this.$t("Panels.StatusPanel.ExcludeObject.ExcludeObject"),color:"warning",icon:n1,'
                'loadingName:"excludeObjectButton",status:()=>this.multiFunctionButton||this.printing_objects.length<1?!1:'
                '["paused","printing"].includes(this.printer_state),click:this.btnExcludeObject},'
                '{text:this.$t("Panels.StatusPanel.PauseAtLayer.PauseAtLayer")'
            )
        if stock_exclude not in data:
            raise SystemExit("Could not find toolbar Exclude Object entry in Mainsail bundle")
        data = data.replace(stock_exclude, with_iron, 1)
        print("Inserted Iron Object toolbar button beside Exclude")
        changed = True
    else:
        print("Iron toolbar entry present (custom); leaving structure")

    # --- Exclude toolbar: never hide behind multi-function arrow ---
    if EXCLUDE_TOOLBAR_MF in data:
        data = data.replace(EXCLUDE_TOOLBAR_MF, EXCLUDE_TOOLBAR_SIDE, 1)
        print("Updated Exclude toolbar: always visible beside Iron")
        changed = True
    elif EXCLUDE_TOOLBAR_MF_LEN1 in data:
        data = data.replace(EXCLUDE_TOOLBAR_MF_LEN1, EXCLUDE_TOOLBAR_SIDE, 1)
        print("Updated Exclude toolbar: always visible beside Iron")
        changed = True
    elif EXCLUDE_TOOLBAR_SIDE in data:
        print("Exclude toolbar already always-visible")
    else:
        print("WARNING: Exclude toolbar pattern not matched (may already be custom)")

    # --- strip Iron from multi-function dropdown (arrow menu) ---
    if IRON_MENU in data:
        data = data.replace(IRON_MENU, "", 1)
        print("Removed Iron Object from multi-function dropdown menu")
        changed = True
    else:
        print("Iron not in multi-function menu (good)")

    # --- disable multi-function collapse so buttons stay side by side ---
    if MULTIFUNC_OFF in data:
        print("multiFunctionButton already forced off")
    elif MULTIFUNC_ON in data:
        data = data.replace(MULTIFUNC_ON, MULTIFUNC_OFF, 1)
        print("Disabled multi-function dropdown (toolbar buttons stay side by side)")
        changed = True
    else:
        print("WARNING: multiFunctionButton getter not found")

    if not changed:
        print("No bundle changes needed")
    return data


def main() -> int:
    bundle = find_bundle()
    backup = bundle.with_suffix(bundle.suffix + ".bak-iron")
    text = bundle.read_text(encoding="utf-8")
    patched = patch(text)
    if patched != text:
        if not backup.exists():
            shutil.copy2(bundle, backup)
            print(f"Backup: {backup}")
        bundle.write_text(patched, encoding="utf-8")
        print(f"Wrote: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
