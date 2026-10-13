#!/usr/bin/env bash
set -euo pipefail

serial="${ANDROID_SERIAL:-emulator-5554}"
apk_dir="${APK_DIR:-.ci-apks}"
output="${COLLECTION_OUTPUT:-artifacts/ci}"
mkdir -p "$output"

collect_debug() {
  rc=$?
  trap - EXIT
  timeout 15s adb -s "$serial" logcat -d > "$output/logcat.txt" 2>&1 || true
  timeout 15s adb -s "$serial" shell dumpsys activity activities > "$output/activities.txt" 2>&1 || true
  timeout 15s adb -s "$serial" shell getprop > "$output/device-properties.txt" 2>&1 || true
  exit "$rc"
}
trap collect_debug EXIT

adb -s "$serial" wait-for-device
uv run python scripts/prepare_emulator.py --serial "$serial" --output "$output/setup"
uv run python install_aurora.py --serial "$serial" --source "$apk_dir/aurora-apks.zip" --fresh --report "$output/aurora-install.json"
uv run python install_aurora.py --serial "$serial" --package com.windyty.android --source "$apk_dir/windy-apks.zip" --fresh --report "$output/windy-install.json"
timeout --signal=TERM --kill-after=15s 8m uv run python main.py --serial "$serial" --location "${LOCATION:-Мурманск}" --aurora-gps "${LATITUDE:-68.9707}" "${LONGITUDE:-33.0827}" --timeout 90 --output "$output/collection"
