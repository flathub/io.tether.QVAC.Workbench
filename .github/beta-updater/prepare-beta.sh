#!/usr/bin/env bash
# First-run preparation of the beta working tree for the official flatpak-external-data-checker.
# It runs inside the pinned checker image (bash, awk, jq and git are there), so the host needs
# nothing. The trusted master tree is only read; files in the beta tree are data and are never
# executed. Version discovery, download, checksums, commits and pull requests are all left to
# the official checker.
#
#   prepare-beta.sh TRUSTED_DIR BETA_DIR MANIFEST
#
# Beta without x-checker-data (today's obsolete recipe): the beta manifest becomes the
# human-authored production manifest with only its checker's version-pattern and url-template
# switched to staging discovery, and flathub.json gets "automerge-flathubbot-prs": true. The
# source url and sha512 stay production's real ones until the checker verifies a staged RC. All of
# this is only an edit of the working tree: the checker commits it, on a new branch, together
# with a real update and never alone. Beta that already has staging checker data: nothing is
# touched, so human edits to its recipe, comments and flathub.json survive.
set -euo pipefail

fail() {
  echo "error: $*" >&2
  exit 1
}

[ "$#" -eq 3 ] || fail "usage: $0 TRUSTED_DIR BETA_DIR MANIFEST"
trusted=$1
beta=$2
name=$3
case "$name" in */* | "") fail "manifest must be a plain file name" ;; esac
production="$trusted/$name"
target="$beta/$name"
flathub="$beta/flathub.json"
[ -f "$production" ] || fail "missing production manifest $production"
[ -d "$beta" ] || fail "missing beta checkout $beta"

if [ -f "$target" ] && grep -q '^ *x-checker-data:' "$target"; then
  grep -qF 'beta-$version/flatpak/' "$target" ||
    fail "$target has x-checker-data without the staging beta-\$version/flatpak/ template; fix it by hand"
  echo "beta already has staging checker data; nothing to prepare" >&2
  exit 0
fi

root="$(awk '/^          url: / { print $2; n++ } END { exit n != 1 }' "$production")" ||
  fail "$production needs exactly one x-checker-data url"
case "$root" in */) ;; *) fail "the x-checker-data url must end with /: $root" ;; esac

# Compute both results first, so a refusal leaves the beta tree untouched.
manifest_tmp="$(mktemp)"
flathub_tmp="$(mktemp)"
trap 'rm -f "$manifest_tmp" "$flathub_tmp"' EXIT

BETA_PATTERN='href="\./beta-(\d+\.\d+\.\d+-rc\.\d+\.\d+)/"' \
  BETA_TEMPLATE="${root}beta-\$version/flatpak/QV.AC-x64-flatpak.tar.gz" \
  awk '
    /^          version-pattern: / { print "          version-pattern: " ENVIRON["BETA_PATTERN"]; p++; next }
    /^          url-template: /    { print "          url-template: " ENVIRON["BETA_TEMPLATE"]; t++; next }
    { print }
    END { exit p != 1 || t != 1 }
  ' "$production" > "$manifest_tmp" ||
  fail "$production needs exactly one x-checker-data version-pattern and url-template"

current="$flathub"
[ -f "$current" ] || current="$trusted/flathub.json"
jq --indent 2 '. + {"automerge-flathubbot-prs": true}' "$current" > "$flathub_tmp" ||
  fail "$current is not a JSON object"

chmod 644 "$manifest_tmp" "$flathub_tmp"
mv "$manifest_tmp" "$target"
mv "$flathub_tmp" "$flathub"

# The checker commits with `git commit -a`, which skips files git does not track yet.
if [ -e "$beta/.git" ]; then
  git -c "safe.directory=$(cd "$beta" && pwd -P)" -C "$beta" add --intent-to-add -- "$name" flathub.json
fi
echo "beta prepared from $production: staging discovery, automerge-flathubbot-prs true" >&2
