#!/bin/sh
# provision_virgin_board.sh — one-time bring-up of a factory-fresh STM32G474CB
# board for the detector3 firmware.
#
# WHY: the firmware and STM32G474CBTX_FLASH.ld assume SINGLE-BANK flash
# (DBANK=0): 124K contiguous code + 4K settings page at 0x0801F000. Factory
# G474s ship with DBANK=1 (dual-bank), where a 128K part maps only 64K at
# 0x08000000 and bank 2 at 0x08040000 (RM0440 p.96 Table 7 — see
# the manual is not in this repo — get RM0440 from st.com). Our ~85K image doesn't fit in 64K, so flashing a virgin
# board fails with "Operation exceeds memory limits" during erase.
#
# WHAT THIS DOES (in order, with checks between each step):
#   1. Connects over SWD (connect-under-reset) and reads the option bytes.
#      Aborts unless the chip is a G4 cat-3 (device ID 0x469) with RDP level 0.
#   2. If DBANK=1: switches to DBANK=0, then MASS ERASES the chip — required
#      after a DBANK change because the ECC word format differs (RM0440
#      pp.119-120). Asks for confirmation first; a virgin board has nothing
#      to lose. If DBANK is already 0 the switch AND the erase are skipped —
#      an already-provisioned board (e.g. the old bench board) keeps its
#      saved settings.
#   3. Flashes Debug/stm_detector3.elf with verify + reset, if it exists
#      (skip with --no-flash).
#
# AFTER FIRST BOOT: the settings page is empty, so the firmware runs
# compile-time defaults — re-tune (a/z s/x d/c f/v) and press S to save.
# Tuned values are recoverable from any recent study header (tx_pulse_us etc.).
#
# NEVER use CubeProgrammer's "factory default" option-bytes button on a
# provisioned board — it silently sets DBANK back to 1.
#
# Usage: ./provision_virgin_board.sh [-y] [--no-flash]
#   -y          skip the confirmation prompt
#   --no-flash  provision only; don't download firmware

set -u
cd "$(dirname "$0")" || exit 1

ASSUME_YES=0
DO_FLASH=1
for arg in "$@"; do
  case "$arg" in
    -y)         ASSUME_YES=1 ;;
    --no-flash) DO_FLASH=0 ;;
    *) echo "usage: $0 [-y] [--no-flash]" >&2; exit 2 ;;
  esac
done

die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

# ── Locate STM32_Programmer_CLI (CubeIDE plugin, else standalone, else PATH) ──
CLI=""
for c in /Applications/STM32CubeIDE.app/Contents/Eclipse/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.*/tools/bin/STM32_Programmer_CLI \
         /Applications/STMicroelectronics/STM32Cube/STM32CubeProgrammer/STM32CubeProgrammer.app/Contents/MacOs/bin/STM32_Programmer_CLI \
         /Applications/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI; do
  [ -x "$c" ] && CLI="$c"   # glob is version-sorted; last match = newest
done
[ -n "$CLI" ] || CLI=$(command -v STM32_Programmer_CLI 2>/dev/null || true)
[ -n "$CLI" ] || die "STM32_Programmer_CLI not found (is STM32CubeIDE installed?)"
echo "Using: $CLI"

CONNECT="-c port=SWD mode=UR"

# ── Step 1: connect, read option bytes, sanity-check the chip ────────────────
echo ""
echo "── Reading option bytes ─────────────────────────────────────────"
OB_OUT=$("$CLI" $CONNECT -ob displ 2>&1)
STATUS=$?
printf '%s\n' "$OB_OUT"
[ $STATUS -eq 0 ] || die "could not connect / read option bytes.
Check the ST-LINK USB connection (plug in directly, no hub) and retry."

printf '%s\n' "$OB_OUT" | grep -qi 'Device ID.*0x469' \
  || die "device ID is not 0x469 (STM32G4 cat-3) — wrong board attached? Refusing to continue."

printf '%s\n' "$OB_OUT" | grep -i 'RDP' | grep -q '0xAA' \
  || die "RDP is not level 0 (0xAA). This chip has read protection set — not a virgin board.
Resolve that manually before provisioning (regressing RDP has its own mass-erase semantics)."

DBANK_VAL=$(printf '%s\n' "$OB_OUT" | grep -m1 'DBANK' | grep -Eo '0x[01]' | head -1)
[ -n "$DBANK_VAL" ] || die "could not parse DBANK from option-byte output above."
echo ""
echo "DBANK = $DBANK_VAL"

# ── Step 2: switch to single-bank + mandatory mass erase (only if needed) ────
if [ "$DBANK_VAL" = "0x0" ]; then
  echo "Already single-bank (DBANK=0) — skipping option-byte write and mass erase."
  echo "(Any saved settings at 0x0801F000 are untouched.)"
else
  echo ""
  echo "This will set DBANK=0 and then MASS ERASE the entire chip"
  echo "(required after a DBANK change, RM0440 pp.119-120)."
  if [ $ASSUME_YES -ne 1 ]; then
    printf 'Type yes to continue: '
    read -r answer
    [ "$answer" = "yes" ] || die "aborted by user."
  fi

  echo ""
  echo "── Setting DBANK=0 ──────────────────────────────────────────────"
  "$CLI" $CONNECT -ob DBANK=0 || die "option-byte write failed."

  # The option-byte launch resets the chip; reconnect and verify it stuck.
  VERIFY_OUT=$("$CLI" $CONNECT -ob displ 2>&1) || die "could not reconnect after option-byte write."
  NEW_DBANK=$(printf '%s\n' "$VERIFY_OUT" | grep -m1 'DBANK' | grep -Eo '0x[01]' | head -1)
  [ "$NEW_DBANK" = "0x0" ] || die "DBANK still reads $NEW_DBANK after write — not proceeding to erase."
  echo "Verified: DBANK=0."

  echo ""
  echo "── Mass erasing ─────────────────────────────────────────────────"
  "$CLI" $CONNECT -e all || die "mass erase failed."
fi

# ── Step 3: flash firmware ────────────────────────────────────────────────────
if [ $DO_FLASH -eq 1 ]; then
  ELF=Debug/stm_detector3.elf
  if [ -f "$ELF" ]; then
    echo ""
    echo "── Flashing $ELF (verify + reset) ───────────────────────────────"
    "$CLI" $CONNECT -w "$ELF" -v -rst || die "flash failed."
  else
    echo ""
    echo "No $ELF found — build the Debug configuration, then flash from the"
    echo "IDE or rerun this script."
  fi
fi

echo ""
echo "Done. Board is provisioned (single-bank, DBANK=0)."
echo "Reminder: settings page is empty on a fresh chip — re-tune over serial"
echo "(a/z s/x d/c f/v) and press S to save. Never apply factory-default"
echo "option bytes to this board (it would set DBANK=1 again)."
