#!/usr/bin/env python3
"""Add Iron Object toolbar button to Mainsail status panel (in-page bed-map dialog)."""

from __future__ import annotations

import shutil
from pathlib import Path

MAINSAIL_ASSETS = Path("/home/x/mainsail/assets")
BUNDLE_GLOB = "index-*.js"
OPEN_DIALOG = "btnIronObject(){window.openIronObjectDialog&&window.openIronObjectDialog()}"
REDIRECT = 'btnIronObject(){window.location.href="/iron-picker/"}'


def find_bundle() -> Path:
    matches = sorted(MAINSAIL_ASSETS.glob(BUNDLE_GLOB))
    if not matches:
        raise SystemExit(f"No Mainsail bundle found in {MAINSAIL_ASSETS}")
    return matches[-1]


def patch(data: str) -> str:
    changed = False

    if OPEN_DIALOG in data:
        print("Mainsail bundle already opens in-page Iron Object dialog")
        return data

    if REDIRECT in data:
        data = data.replace(REDIRECT, OPEN_DIALOG)
        print("Updated Iron Object button: redirect -> in-page dialog")
        changed = True
    else:
        old_method = "btnExcludeObject(){this.boolShowObjects=!0}btnPauseAtLayer()"
        new_method = (
            'btnExcludeObject(){this.boolShowObjects=!0}'
            f"{OPEN_DIALOG}"
            "btnPauseAtLayer()"
        )
        if old_method not in data:
            raise SystemExit("Could not find btnExcludeObject hook in Mainsail bundle")
        data = data.replace(old_method, new_method, 1)
        changed = True

        old_toolbar = (
            '{text:this.$t("Panels.StatusPanel.ExcludeObject.ExcludeObject"),color:"warning",icon:n1,'
            'loadingName:"excludeObjectButton",status:()=>this.multiFunctionButton||this.printing_objects.length<2?!1:'
            '["paused","printing"].includes(this.printer_state),click:this.btnExcludeObject},'
            '{text:this.$t("Panels.StatusPanel.PauseAtLayer.PauseAtLayer")'
        )
        new_toolbar = (
            '{text:this.$t("Panels.StatusPanel.ExcludeObject.ExcludeObject"),color:"warning",icon:n1,'
            'loadingName:"excludeObjectButton",status:()=>this.multiFunctionButton||this.printing_objects.length<2?!1:'
            '["paused","printing"].includes(this.printer_state),click:this.btnExcludeObject},'
            '{text:"Iron Object",color:"primary",icon:va,loadingName:"ironObjectButton",'
            "status:()=>this.multiFunctionButton||this.printing_objects.length<1?!1:"
            '["paused","printing"].includes(this.printer_state),click:this.btnIronObject},'
            '{text:this.$t("Panels.StatusPanel.PauseAtLayer.PauseAtLayer")'
        )
        if old_toolbar not in data:
            raise SystemExit("Could not find toolbar Exclude Object entry in Mainsail bundle")
        data = data.replace(old_toolbar, new_toolbar, 1)

        old_menu = (
            '{text:this.$t("Panels.StatusPanel.ExcludeObject.ExcludeObject"),loadingName:"excludeObjectButton",icon:n1,'
            "status:()=>this.printing_objects.length>1,disabled:()=>"
            '["paused","printing"].includes(this.printer_state),click:this.btnExcludeObject},'
            '{text:this.$t("Panels.StatusPanel.PauseAtLayer.PauseAtLayer")'
        )
        new_menu = (
            '{text:this.$t("Panels.StatusPanel.ExcludeObject.ExcludeObject"),loadingName:"excludeObjectButton",icon:n1,'
            "status:()=>this.printing_objects.length>1,disabled:()=>"
            '["paused","printing"].includes(this.printer_state),click:this.btnExcludeObject},'
            '{text:"Iron Object",loadingName:"ironObjectButton",icon:va,status:()=>this.printing_objects.length>0,'
            'disabled:()=>["paused","printing"].includes(this.printer_state),click:this.btnIronObject},'
            '{text:this.$t("Panels.StatusPanel.PauseAtLayer.PauseAtLayer")'
        )
        if old_menu not in data:
            raise SystemExit("Could not find multi-function menu entry in Mainsail bundle")
        data = data.replace(old_menu, new_menu, 1)
        print("Patched Mainsail bundle with Iron Object dialog button")

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
        bundle.write_text(patched, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())