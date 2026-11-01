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
# Evidence (logs, installed metainfo, screenshot) goes to $RUNNER_TEMP/smoke. No credentials are
# used and the environment is never printed. Nothing weakens the Flatpak or Electron sandbox: the
# app is started with a plain `flatpak run`.
set -euo pipefail

APP=io.tether.QVAC.Workbench
OUT="${RUNNER_TEMP:-/tmp}/smoke"
LAUNCH_SECONDS=120
STABLE_SECONDS=5

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

# Runs inside dbus-run-session, so the app and its portals get a private session bus.
cleanup() {
  flatpak kill "$APP" > /dev/null 2>&1 || true
  local pid
  for pid in "${app_pid:-}" "${openbox_pid:-}" "${xvfb_pid:-}"; do
    [ -z "$pid" ] || kill "$pid" 2> /dev/null || true
  done
  sleep 1
  for pid in "${app_pid:-}" "${openbox_pid:-}" "${xvfb_pid:-}"; do
    [ -z "$pid" ] || kill -KILL "$pid" 2> /dev/null || true
  done
}

# A visible top-level window of a plausible size, so tiny helper windows do not count.
find_window() {
  local ids id geometry width height
  mapfile -t ids < <(xdotool search --onlyvisible --name '.' 2> /dev/null || true)
  for id in "${ids[@]}"; do
    geometry="$(xdotool getwindowgeometry --shell "$id" 2> /dev/null)" || continue
    width="$(printf '%s\n' "$geometry" | sed -n 's/^WIDTH=//p')"
    height="$(printf '%s\n' "$geometry" | sed -n 's/^HEIGHT=//p')"
    if [ "${width:-0}" -ge 300 ] && [ "${height:-0}" -ge 200 ]; then
      echo "$id"
      return 0
    fi
  done
  return 1
}

app_failed() {
  local status=0
  wait "$app_pid" || status=$?
  echo "error: $1 (exit status $status); last log lines:" >&2
  tail -n 40 "$OUT/app.log" >&2 || true
  exit 1
}

launch() {
  export DISPLAY=:99
  trap cleanup EXIT
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

  local window=""
  while [ "$SECONDS" -lt "$LAUNCH_SECONDS" ]; do
    kill -0 "$app_pid" 2> /dev/null || app_failed "the app exited before showing a window"
    window="$(find_window || true)"
    [ -z "$window" ] || break
    sleep 2
  done
  [ -n "$window" ] || app_failed "no app window within ${LAUNCH_SECONDS}s"

  sleep "$STABLE_SECONDS"
  kill -0 "$app_pid" 2> /dev/null || app_failed "the app crashed right after showing its window"
  xdotool getwindowname "$window" | tr -cd '[:print:]' > "$OUT/window-name.txt"
  scrot "$OUT/screenshot.png"
  [ -s "$OUT/screenshot.png" ] || die "no screenshot was captured"
  echo "window shown and app still running after ${STABLE_SECONDS}s"
}

summary() {
  local window_name launched=$1 source_text="$SMOKE_SOURCE"
  [ "$SMOKE_SOURCE" != test ] || source_text="test (build $SMOKE_TEST_BUILD_ID)"
  window_name="$(tr -d '|' < "$OUT/window-name.txt" 2> /dev/null || true)"
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

  launch_status=0
  dbus-run-session -- bash "${BASH_SOURCE[0]}" launch || launch_status=$?
  if [ "$launch_status" -eq 0 ]; then summary passed; else summary "failed (status $launch_status)"; fi
  [ "$version_ok" = true ] && [ "$launch_status" -eq 0 ] || exit 1
}

case "${1:-run}" in
  run) main ;;
  launch) launch ;;
  *) die "usage: $0 [run|launch]" ;;
esac
