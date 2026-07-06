#!/bin/bash
# Install mid-print per-object iron scheduler. Does NOT restart Klipper.
set -euo pipefail

PRINTER_DATA="${PRINTER_DATA:-$HOME/printer_data}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing to: $PRINTER_DATA"

mkdir -p "$PRINTER_DATA/scripts" "$PRINTER_DATA/iron_cache" "$PRINTER_DATA/config"

install_file() {
  local src="$1" dest="$2"
  if [ "$(readlink -f "$src" 2>/dev/null || realpath "$src")" = "$(readlink -f "$dest" 2>/dev/null || realpath "$dest")" ]; then
    return 0
  fi
  cp -f "$src" "$dest"
}

install_file "$SCRIPT_DIR/scripts/iron_scheduler.py" "$PRINTER_DATA/scripts/iron_scheduler.py"
install_file "$SCRIPT_DIR/scripts/inject_iron.py" "$PRINTER_DATA/scripts/inject_iron.py"
install_file "$SCRIPT_DIR/scripts/iron_watcher.py" "$PRINTER_DATA/scripts/iron_watcher.py"
install_file "$SCRIPT_DIR/scripts/iron_inject_trigger.py" "$PRINTER_DATA/scripts/iron_inject_trigger.py"
install_file "$SCRIPT_DIR/scripts/iron_session_guard.py" "$PRINTER_DATA/scripts/iron_session_guard.py"
install_file "$SCRIPT_DIR/config/iron_scheduler.cfg" "$PRINTER_DATA/config/iron_scheduler.cfg"
install_file "$SCRIPT_DIR/scripts/patch-mainsail-iron-button.py" "$PRINTER_DATA/scripts/patch-mainsail-iron-button.py"
install_file "$SCRIPT_DIR/scripts/iron_api_server.py" "$PRINTER_DATA/scripts/iron_api_server.py"

chmod +x "$PRINTER_DATA/scripts/iron_scheduler.py" \
         "$PRINTER_DATA/scripts/inject_iron.py" \
         "$PRINTER_DATA/scripts/iron_watcher.py" \
         "$PRINTER_DATA/scripts/iron_inject_trigger.py" \
         "$PRINTER_DATA/scripts/iron_session_guard.py" \
         "$PRINTER_DATA/scripts/patch-mainsail-iron-button.py" \
         "$PRINTER_DATA/scripts/iron_api_server.py"

MOONRAKER_COMP="/home/x/moonraker/moonraker/components/iron_enable.py"
if [ -f "$SCRIPT_DIR/scripts/iron_enable_moonraker_component.py" ]; then
  cp -f "$SCRIPT_DIR/scripts/iron_enable_moonraker_component.py" "$MOONRAKER_COMP"
  echo "Installed Moonraker iron API component"
fi
if ! grep -q '^\[iron_enable\]' "$PRINTER_DATA/config/moonraker.conf" 2>/dev/null; then
  echo '[iron_enable]' >> "$PRINTER_DATA/config/moonraker.conf"
  echo "Added [iron_enable] to moonraker.conf — restart moonraker"
fi

MAINSAIL_ROOT="${MAINSAIL_ROOT:-$HOME/mainsail}"
if [ -d "$MAINSAIL_ROOT" ] && [ -d "$SCRIPT_DIR/iron-picker" ]; then
  mkdir -p "$MAINSAIL_ROOT/iron-picker"
  cp -f "$SCRIPT_DIR/iron-picker/"* "$MAINSAIL_ROOT/iron-picker/"
  python3 "$PRINTER_DATA/scripts/patch-mainsail-iron-button.py" || true
  echo "Installed bed-map picker at $MAINSAIL_ROOT/iron-picker/"
fi

PRINTER_CFG="$PRINTER_DATA/config/printer.cfg"
if ! grep -q 'iron_scheduler.cfg' "$PRINTER_CFG" 2>/dev/null; then
  # Insert after KAMP include if present, else after first include block
  if grep -q 'KAMP_Settings.cfg' "$PRINTER_CFG"; then
    sed -i.bak '/\[include KAMP_Settings.cfg\]/a\
[include iron_scheduler.cfg]\
\
[respond]
' "$PRINTER_CFG"
  else
    echo '[include iron_scheduler.cfg]' >> "$PRINTER_CFG"
    echo '' >> "$PRINTER_CFG"
    echo '[respond]' >> "$PRINTER_CFG"
  fi
  echo "Updated printer.cfg"
else
  echo "printer.cfg already includes iron_scheduler.cfg"
fi

if ! grep -q '^\[respond\]' "$PRINTER_CFG" 2>/dev/null; then
  echo '[respond]' >> "$PRINTER_CFG"
fi

echo ""
echo "Done. Next steps (manual):"
echo "  1. FIRMWARE_RESTART  (when not printing)"
echo "  2. While printing: Mainsail status panel → Iron Object"
echo "     (bed-map picker, same objects as Exclude Objects)"
echo "     Direct URL: http://<printer-ip>/iron-picker/"
echo ""
echo "Requires: OrcaSlicer label objects enabled (same as exclude objects)."