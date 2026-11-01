#!/usr/bin/env bash
# Temporary smoke test of the published QV.AC Flatpak: install it with the Flatpak CLI, then launch
# it under Xvfb and wait for its window. No login and no model download. Run by the manual
# "smoke" job of .github/workflows/beta-updater-tests.yml on an ubuntu-22.04 runner.
#
# Inputs arrive only through the environment and are validated strictly (never interpolated into
# a command string, never passed to eval):
#   SMOKE_SOURCE            beta: install //beta from the Flathub beta remote
#                           test: install a Flathub test build
#   SMOKE_TEST_BUILD_ID     digits only; required when SMOKE_SOURCE is test
#   SMOKE_EXPECTED_VERSION  version the installed metainfo must report, e.g. 0.21.0-rc.4.254
#
# The launch passes only for a visible window of the app itself: its WM_CLASS must be the
# StartupWMClass of the installed desktop file. An error dialog titled "Error", a fatal
# main-process error in the app log, an exit, or a window that is gone after the stabilization
# period each fail the run. Nothing ever waits on a live app: failures report diagnostics (window
# tree, WM_CLASS and title of every visible window, a screenshot, the log tail) and the bounded
# cleanup stops it. Process ids are not used to find the window: they are namespaced inside the
# Flatpak sandbox.
#
# Evidence (logs, installed metainfo, window diagnostics, screenshot) goes to $RUNNER_TEMP/smoke.
# No credentials are used and the environment is never printed. Nothing weakens the Flatpak or
# Electron sandbox: the app is started with a plain `flatpak run`.
set -euo pipefail

APP=io.tether.QVAC.Workbench
OUT="${RUNNER_TEMP:-/tmp}/smoke"
LAUNCH_SECONDS=120          # how long the app may take to show its window
STABLE_SECONDS=5            # the window must still be there this long afterwards
LAUNCH_BACKSTOP_SECONDS=300 # outer bound for the whole launch phase, diagnostics and cleanup included

die() {
  echo "error: $*" >&2
  exit 1
}

validate() {
  local version_re='^[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+\.[0-9]+)?$'
  local build_re='^[0-9]{1,12}$'
  case "${SMOKE_SOURCE:-}" in beta | test) ;; *) die "SMOKE_SOURCE must be beta or test" ;; esac
  [[ "${SMOKE_EXPECTED_VERSION:-}" =~ $version_re ]] ||
    die "SMOKE_EXPECTED_VERSION must look like 0.21.0 or 0.21.0-rc.4.254"
  if [ "$SMOKE_SOURCE" = test ] || [ -n "${SMOKE_TEST_BUILD_ID:-}" ]; then
    [[ "${SMOKE_TEST_BUILD_ID:-}" =~ $build_re ]] ||
      die "SMOKE_TEST_BUILD_ID must be 1 to 12 digits (and is required when SMOKE_SOURCE is test)"
  fi
}

install_app() {
  flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
  flatpak remote-add --user --if-not-exists flathub-beta https://flathub.org/beta-repo/flathub-beta.flatpakrepo
  flatpak remotes --user --columns=name,url > "$OUT/remotes.txt"
  if [ "$SMOKE_SOURCE" = beta ]; then
    timeout 900 flatpak install --user --noninteractive --assumeyes flathub-beta "$APP//beta"
  else
    timeout 900 flatpak install --user --noninteractive --assumeyes \
      "https://dl.flathub.org/build-repo/${SMOKE_TEST_BUILD_ID}/${APP}.flatpakref"
  fi
}

record_evidence() {
  flatpak --version > "$OUT/flatpak-version.txt"
  flatpak info --user "$APP" > "$OUT/flatpak-info.txt"
  flatpak list --user --app --columns=application,branch,origin,installation > "$OUT/flatpak-list.txt"
  ref="$(flatpak info --user --show-ref "$APP")"
  commit="$(flatpak info --user --show-commit "$APP")"
  origin="$(flatpak info --user --show-origin "$APP")"
  branch="${ref##*/}"
  metainfo="$(flatpak info --user --show-location "$APP")/files/share/metainfo/$APP.metainfo.xml"
  [ -f "$metainfo" ] || die "the installed package has no metainfo at files/share/metainfo"
  cp "$metainfo" "$OUT/installed.metainfo.xml"
  release="$(grep -m1 '<release ' "$metainfo" || true)"
  printf '%s\n' "$release" > "$OUT/release-line.txt"
  version="$(printf '%s\n' "$release" | sed -n 's/.*version="\([^"]*\)".*/\1/p')"
  release_type="$(printf '%s\n' "$release" | sed -n 's/.*type="\([^"]*\)".*/\1/p')"
  if [ "$version" = "$SMOKE_EXPECTED_VERSION" ]; then
    version_ok=true
  else
    version_ok=false
    echo "error: the installed metainfo reports version '$(printf '%s' "$version" | tr -cd '[:print:]')', expected $SMOKE_EXPECTED_VERSION" >&2
  fi
  echo "ref=$ref branch=$branch commit=$commit origin=$origin version=$version type=$release_type"
}

# The window class the app declares for itself, read from the installed desktop file. Exactly one
# StartupWMClass made of plain class-name characters is required, so it is safe to use in a regex
# once its dots are escaped.
installed_wm_class() {
  local desktop value plain_re='^[A-Za-z0-9._-]+$'
  desktop="$(flatpak info --user --show-location "$APP")/files/share/applications/$APP.desktop"
  [ -f "$desktop" ] || die "the installed package has no desktop file at files/share/applications"
  value="$(sed -n 's/^StartupWMClass=//p' "$desktop" | tr -d '\r')"
  [[ "$value" =~ $plain_re ]] || die "the desktop file needs exactly one plain StartupWMClass"
  printf '%s' "$value"
}

# Everything below that touches the display is bounded with `timeout`, and nothing waits on the
# app, so a hung app or display cannot stall the failure path.
cleanup() {
  timeout 15 flatpak kill "$APP" > /dev/null 2>&1 || true
  local pid
  for pid in "${app_pid:-}" "${openbox_pid:-}" "${xvfb_pid:-}"; do
    [ -z "$pid" ] || kill "$pid" 2> /dev/null || true
  done
  sleep 1
  for pid in "${app_pid:-}" "${openbox_pid:-}" "${xvfb_pid:-}"; do
    [ -z "$pid" ] || kill -KILL "$pid" 2> /dev/null || true
  done
}

# Visible windows of the app itself (anchored, escaped WM_CLASS) of a plausible size, so tiny
# helper windows do not count.
find_window() {
  local ids id geometry width height
  mapfile -t ids < <(timeout 10 xdotool search --onlyvisible --class "$class_re" 2> /dev/null || true)
  for id in "${ids[@]}"; do
    geometry="$(timeout 10 xdotool getwindowgeometry --shell "$id" 2> /dev/null)" || continue
    width="$(printf '%s\n' "$geometry" | sed -n 's/^WIDTH=//p')"
    height="$(printf '%s\n' "$geometry" | sed -n 's/^HEIGHT=//p')"
    if [ "${width:-0}" -ge 300 ] && [ "${height:-0}" -ge 200 ]; then
      echo "$id"
      return 0
    fi
  done
  return 1
}

# The window tree and the WM_CLASS, title and geometry of every visible named window, so a class
# mismatch can be told apart from a real startup error. $1 is success or failure.
capture_windows() {
  local ids id
  {
    echo "expected WM_CLASS: ${wm_class:-unknown}"
    echo "--- xwininfo -root -tree"
    timeout 10 xwininfo -root -tree || true
    mapfile -t ids < <(timeout 10 xdotool search --onlyvisible --name '.' 2> /dev/null || true)
    for id in "${ids[@]}"; do
      echo "--- window $id"
      timeout 10 xprop -id "$id" WM_CLASS WM_NAME _NET_WM_NAME || true
      timeout 10 xdotool getwindowgeometry "$id" || true
    done
  } > "$OUT/windows-$1.txt" 2>&1
  if [ "$1" = failure ]; then
    timeout 10 scrot "$OUT/failure-screenshot.png" > /dev/null 2>&1 || true
  fi
}

# Reports and exits; it never waits on a live app, the EXIT trap's cleanup stops it.
fail_launch() {
  echo "error: $1" >&2
  capture_windows failure
  if kill -0 "$app_pid" 2> /dev/null; then
    echo "the app is still running; the cleanup stops it" >&2
  else
    local status=0
    wait "$app_pid" || status=$?
    echo "the app exited with status $status" >&2
  fi
  echo "last app log lines:" >&2
  tail -n 40 "$OUT/app.log" >&2 || true
  exit 1
}

# $1 says when, for the message: the app must be alive, its log free of fatal main-process
# errors, and no error dialog may be showing (Electron's uncaught-exception box is titled Error
# and keeps the process alive).
check_health() {
  kill -0 "$app_pid" 2> /dev/null || fail_launch "the app exited $1"
  if grep -qF -e 'A JavaScript error occurred in the main process' \
    -e 'Uncaught Exception' -e 'App threw an error during load' "$OUT/app.log" 2> /dev/null; then
    fail_launch "the app log reports a fatal main-process error ($1)"
  fi
  if timeout 10 xdotool search --onlyvisible --name '^Error$' > /dev/null 2>&1; then
    fail_launch "an error dialog is showing ($1)"
  fi
}

launch() {
  export DISPLAY=:99
  trap cleanup EXIT
  wm_class="$(installed_wm_class)"
  printf '%s\n' "$wm_class" > "$OUT/startup-wm-class.txt"
  # The class is plain characters only, so the dot is the one regex metacharacter to escape.
  class_re="^$(printf '%s' "$wm_class" | sed 's/\./\\./g')\$"

  Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp > "$OUT/xvfb.log" 2>&1 &
  xvfb_pid=$!
  for _ in $(seq 1 20); do
    xdotool getdisplaygeometry > /dev/null 2>&1 && break
    sleep 0.5
  done
  xdotool getdisplaygeometry > /dev/null 2>&1 || die "Xvfb did not start"
  openbox > "$OUT/openbox.log" 2>&1 &
  openbox_pid=$!

  flatpak run --user "$APP" > "$OUT/app.log" 2>&1 &
  app_pid=$!

  local window="" deadline=$((SECONDS + LAUNCH_SECONDS))
  while [ "$SECONDS" -lt "$deadline" ]; do
    check_health "before showing its window"
    window="$(find_window || true)"
    [ -z "$window" ] || break
    sleep 2
  done
  [ -n "$window" ] || fail_launch "no visible window of class $wm_class within ${LAUNCH_SECONDS}s"

  sleep "$STABLE_SECONDS"
  check_health "during the ${STABLE_SECONDS}s stabilization"
  window="$(find_window || true)"
  [ -n "$window" ] || fail_launch "the app window of class $wm_class is gone after ${STABLE_SECONDS}s (closed, hidden or replaced)"

  timeout 10 xdotool getwindowname "$window" 2> /dev/null | tr -cd '[:print:]' > "$OUT/window-name.txt" || true
  capture_windows success
  timeout 10 scrot "$OUT/screenshot.png" || true
  [ -s "$OUT/screenshot.png" ] || fail_launch "no screenshot was captured"
  echo "app window of class $wm_class shown and still there after ${STABLE_SECONDS}s"
}

summary() {
  local window_name wm launched=$1 source_text="$SMOKE_SOURCE"
  [ "$SMOKE_SOURCE" != test ] || source_text="test (build $SMOKE_TEST_BUILD_ID)"
  window_name="$(tr -d '|' < "$OUT/window-name.txt" 2> /dev/null || true)"
  wm="$(tr -d '|' < "$OUT/startup-wm-class.txt" 2> /dev/null || true)"
  {
    echo "### QV.AC Flatpak smoke test"
    echo
    echo "| Item | Value |"
    echo "| --- | --- |"
    echo "| Source | ${source_text} |"
    echo "| Ref | ${ref} |"
    echo "| Branch | ${branch} |"
    echo "| Commit | ${commit} |"
    echo "| Origin | ${origin} |"
    echo "| Installed version | ${version} (expected ${SMOKE_EXPECTED_VERSION}, match: ${version_ok}) |"
    echo "| Release type | ${release_type} |"
    echo "| Startup WM class | ${wm:-unknown} |"
    echo "| Window | ${window_name:-none} |"
    echo "| Launch | ${launched} |"
  } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
}

main() {
  validate
  mkdir -p "$OUT"
  # `flatpak run` needs a private runtime directory; some runners do not provide one.
  if [ -z "${XDG_RUNTIME_DIR:-}" ] || [ ! -d "$XDG_RUNTIME_DIR" ] || [ ! -w "$XDG_RUNTIME_DIR" ]; then
    XDG_RUNTIME_DIR="$(mktemp -d)"
    chmod 700 "$XDG_RUNTIME_DIR"
    export XDG_RUNTIME_DIR
  fi
  install_app 2>&1 | tee "$OUT/install.log"
  record_evidence

  # The launch phase has its own deadlines; this outer bound only guarantees that it ends, and
  # `timeout` stops its whole process group.
  launch_status=0
  timeout --kill-after=15 "$LAUNCH_BACKSTOP_SECONDS" \
    dbus-run-session -- bash "${BASH_SOURCE[0]}" launch || launch_status=$?
  timeout 15 flatpak kill "$APP" > /dev/null 2>&1 || true
  if [ "$launch_status" -eq 0 ]; then
    summary passed
  elif [ "$launch_status" -eq 124 ] || [ "$launch_status" -eq 137 ]; then
    summary "failed (status $launch_status: the ${LAUNCH_BACKSTOP_SECONDS}s backstop stopped it)"
  else
    summary "failed (status $launch_status)"
  fi
  [ "$version_ok" = true ] && [ "$launch_status" -eq 0 ] || exit 1
}

case "${1:-run}" in
  run) main ;;
  launch) launch ;;
  *) die "usage: $0 [run|launch]" ;;
esac
