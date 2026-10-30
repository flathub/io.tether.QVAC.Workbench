#!/usr/bin/env bash
# Helpers for .github/workflows/beta-updater.yml. Version discovery, download,
# checksums, commits and pull requests are all done by the official
# flatpak-external-data-checker; nothing here selects or hashes a release.
#
#   prepare-beta.sh ready URL
#       Prints ready=true (HTTP 200) or ready=false (HTTP 404) for the first
#       published beta archive. Any other status, or a transport error, fails.
#   prepare-beta.sh bootstrap MASTER_DIR BETA_DIR MANIFEST
#       Prepares the beta working tree for the checker (see README.md).
set -euo pipefail

fail() {
  echo "error: $*" >&2
  exit 1
}

ready() {
  local url=$1 status
  status="$(curl --silent --show-error --head --location --max-redirs 3 \
    --proto '=http,https' --proto-redir '=http,https' \
    --connect-timeout 15 --max-time 60 \
    --output /dev/null --write-out '%{http_code}' "$url")" ||
    fail "could not query $url"
  case "$status" in
    200) echo "ready=true" ;;
    404)
      echo "no beta archive is published at $url yet" >&2
      echo "ready=false"
      ;;
    *) fail "unexpected HTTP $status from $url" ;;
  esac
}

bootstrap() {
  local master=$1 beta=$2 name=$3
  local source="$master/$name" target="$beta/$name" flathub="$beta/flathub.json"
  local root tmp src

  case "$name" in */* | "") fail "manifest must be a plain file name" ;; esac
  [ -f "$source" ] || fail "missing production manifest $source"

  if [ -f "$target" ] && grep -q '^ *x-checker-data:' "$target"; then
    # Already bootstrapped (and merged): the beta recipe belongs to its humans.
    grep -qF 'beta-$version/flatpak/' "$target" ||
      fail "$target has x-checker-data without the staging beta-\$version/flatpak/ template; fix it by hand"
    echo "beta already has staging checker data; its recipe is left alone" >&2
  else
    # Seed from the human production recipe, changing only the two checker lines.
    # The checker commits this only together with a real update to a staged archive.
    root="$(awk '/^          url: / { print $2; n++ } END { exit n != 1 }' "$source")" ||
      fail "$source needs exactly one x-checker-data url"
    case "$root" in */) ;; *) fail "x-checker-data url must end with /: $root" ;; esac
    tmp="$(mktemp "${TMPDIR:-/tmp}/beta-updater.XXXXXX")"
    BETA_PATTERN='href="\./beta-(\d+\.\d+\.\d+-rc\.\d+\.\d+)/"' \
      BETA_TEMPLATE="${root}beta-\$version/flatpak/QV.AC-x64-flatpak.tar.gz" \
      awk '
        /^          version-pattern: / { print "          version-pattern: " ENVIRON["BETA_PATTERN"]; p++; next }
        /^          url-template: /    { print "          url-template: " ENVIRON["BETA_TEMPLATE"]; t++; next }
        { print }
        END { exit p != 1 || t != 1 }
      ' "$source" > "$tmp" ||
      { rm -f "$tmp"; fail "$source needs exactly one x-checker-data version-pattern and url-template"; }
    chmod 644 "$tmp"
    mv "$tmp" "$target"
    echo "beta recipe seeded from $source; only the checker config differs" >&2
  fi

  # The checker reads automerge-flathubbot-prs from the working tree, so beta never auto-merges.
  src="$flathub"
  [ -f "$flathub" ] || src="$master/flathub.json"
  if [ "$src" != "$flathub" ] || ! jq -e '."automerge-flathubbot-prs" == false' "$src" > /dev/null 2>&1; then
    tmp="$(mktemp "${TMPDIR:-/tmp}/beta-updater.XXXXXX")"
    jq --indent 2 '. + {"automerge-flathubbot-prs": false}' "$src" > "$tmp" ||
      { rm -f "$tmp"; fail "$src is not a JSON object"; }
    chmod 644 "$tmp"
    mv "$tmp" "$flathub"
    echo "flathub.json: automerge-flathubbot-prs is false" >&2
  fi

  # The checker commits with `git commit -a`, which skips files git does not track yet.
  if [ -e "$beta/.git" ]; then
    git -C "$beta" add --intent-to-add -- "$name" flathub.json
  fi
}

case "${1:-}" in
  ready)
    [ "$#" -eq 2 ] || fail "usage: $0 ready URL"
    ready "$2"
    ;;
  bootstrap)
    [ "$#" -eq 4 ] || fail "usage: $0 bootstrap MASTER_DIR BETA_DIR MANIFEST"
    bootstrap "$2" "$3" "$4"
    ;;
  *) fail "usage: $0 ready URL | bootstrap MASTER_DIR BETA_DIR MANIFEST" ;;
esac
