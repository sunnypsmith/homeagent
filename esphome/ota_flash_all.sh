#!/bin/sh
# Compile and OTA-flash all Atom Echo devices in sequence.
# Run:  cd esphome && sh ota_flash_all.sh

cd "$(dirname "$0")" || exit 1

PASSED=0
FAILED=0
SKIPPED=0
FAILURES=""

flash_device() {
  config="$1"
  device="$2"

  echo ""
  echo "========================================"
  echo "  Flashing: $config -> $device"
  echo "========================================"

  if [ ! -f "$config" ]; then
    echo "  SKIP: $config not found"
    SKIPPED=$((SKIPPED + 1))
    return
  fi

  esphome compile "$config"
  if esphome upload "$config" --device "$device"; then
    echo "  OK: $config"
    PASSED=$((PASSED + 1))
  else
    echo "  FAILED: $config"
    FAILED=$((FAILED + 1))
    FAILURES="$FAILURES $config"
  fi
}

#                config                              current IP
flash_device atom-echo-voice-office.yaml             10.1.2.158
flash_device atom-echo-bedroom.yaml                  10.1.2.131
flash_device atom-echo-kitchen.yaml                  10.1.2.145
flash_device atom-echo-dining.yaml                   10.1.2.111
flash_device atom-echo-tilly.yaml                    10.1.2.192
flash_device atom-echo-twins.yaml                    10.1.2.207
flash_device atom-echo-theater.yaml                  10.1.2.228
flash_device atom-echo-closet.yaml                   10.1.2.144

echo ""
echo "========================================"
echo "  Summary: $PASSED passed, $FAILED failed, $SKIPPED skipped"
if [ -n "$FAILURES" ]; then
  echo "  Failed:$FAILURES"
fi
echo "========================================"
