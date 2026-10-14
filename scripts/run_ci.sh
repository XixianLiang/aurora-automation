#!/usr/bin/env bash
set -euo pipefail

serial="${ANDROID_SERIAL:-emulator-5554}"
apk_dir="${APK_DIR:-.ci-apks}"
output="${COLLECTION_OUTPUT:-artifacts/ci}"
mkdir -p "$output"
adb -s "$serial" logcat -v threadtime > "$output/logcat-live.txt" 2>&1 &
logcat_pid=$!

collect_debug() {
  rc=$?
  trap - EXIT
  kill "$logcat_pid" 2>/dev/null || true
  sudo dmesg --ctime > "$output/host-kernel.txt" 2>&1 || true
  if [ -d /tmp/android-runner ]; then
    cp -R /tmp/android-runner "$output/emulator-crash" || true
  fi
  timeout 15s adb -s "$serial" logcat -d > "$output/logcat.txt" 2>&1 || true
  timeout 15s adb -s "$serial" shell dumpsys activity activities > "$output/activities.txt" 2>&1 || true
  timeout 15s adb -s "$serial" shell dumpsys location > "$output/location-service.txt" 2>&1 || true
  free -m > "$output/host-memory.txt" 2>&1 || true
  ps -eo pid,ppid,rss,comm > "$output/host-processes.txt" 2>&1 || true
  timeout 15s adb -s "$serial" shell getprop > "$output/device-properties.txt" 2>&1 || true
  exit "$rc"
}
trap collect_debug EXIT

adb -s "$serial" wait-for-device
run_with_recovery() {
  stage="$1"
  shift
  stage_deadline=$((SECONDS + 720))
  for attempt in 1 2 3; do
    remaining=$((stage_deadline - SECONDS))
    if [ "$remaining" -le 0 ]; then
      return 1
    fi
    if timeout --signal=TERM --kill-after=15s "${remaining}s" "$@"; then
      return 0
    fi
    if [ -d "$output/$stage" ]; then
      mv "$output/$stage" "$output/$stage-attempt-$attempt"
    fi
    if [ "$attempt" = 3 ]; then
      return 1
    fi
    echo "$stage attempt $attempt failed; reconnecting emulator before retry"
    sleep 10
    timeout 60s adb -s "$serial" wait-for-device || true
  done
}

run_with_recovery setup timeout --signal=TERM --kill-after=15s 12m uv run python scripts/prepare_emulator.py --serial "$serial" --output "$output/setup"
uv run python install_aurora.py --serial "$serial" --source "$apk_dir/aurora-apks.zip" --fresh --report "$output/aurora-install.json"
uv run python install_aurora.py --serial "$serial" --package com.windyty.android --source "$apk_dir/windy-apks.zip" --fresh --report "$output/windy-install.json"
run_with_recovery collection timeout --signal=TERM --kill-after=15s 8m uv run python main.py --serial "$serial" --location "${LOCATION:-Мурманск}" --aurora-gps "${LATITUDE:-68.9707}" "${LONGITUDE:-33.0827}" --timeout 90 --output "$output/collection"
