#!/bin/sh
# Single-source PoC: regenerate control + git remotes from one 'trees' file.
# Terse: one line per step, plus any warnings, plus the .git/config snippet.
# All generated files are written into a created output subdirectory.
#
# Usage: demo.sh SRC [OUTDIR]
#   SRC    directory containing 'control' + 'git-config' to seed from.
#   OUTDIR where generated files go (created if needed; default: ./out).
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY="python3 $SCRIPT_DIR/treegen.py"
TAB=$(printf '\t')

# Source dir (must hold 'control' + 'git-config'); resolved to absolute.
SRC="${1:-}"
if [ -z "$SRC" ] || [ ! -d "$SRC" ]; then
  echo "usage: $0 SRC [OUTDIR]   (SRC holds 'control' + 'git-config')" >&2
  exit 1
fi
SRC=$(CDPATH= cd -- "$SRC" && pwd) || exit 1
if [ ! -f "$SRC/control" ] || [ ! -f "$SRC/git-config" ]; then
  echo "$0: $SRC has no 'control' + 'git-config'" >&2
  exit 1
fi

# Output subdirectory: created here, all generated files land inside it.
OUT="${2:-$SCRIPT_DIR/out}"
mkdir -p "$OUT" || exit 1
OUT=$(CDPATH= cd -- "$OUT" && pwd)
echo "output dir: $OUT"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

$PY extract "$SRC/control" "$SRC/git-config" "$OUT" 2>&1

$PY gen-control "$OUT/trees" > "$OUT/control.gen"
nd=$(diff "$SRC/control" "$OUT/control.gen" | grep -cE '^<')
if [ "$nd" = 0 ]; then
  echo "gen-control: byte-identical"
else
  sed "s/ *$TAB/$TAB/g" "$SRC/control"     > "$TMP/o.norm"
  sed "s/ *$TAB/$TAB/g" "$OUT/control.gen" > "$TMP/g.norm"
  if diff -q "$TMP/o.norm" "$TMP/g.norm" >/dev/null; then
    echo "gen-control: OK (cleaned $nd stray-whitespace line(s))"
  else
    echo "gen-control: WARN $nd line(s) differ beyond whitespace"
    diff "$SRC/control" "$OUT/control.gen" | grep -E '^[<>]'
  fi
fi

$PY gen-remotes "$OUT/trees" > "$OUT/trees.remotes"
echo "gen-remotes: $(grep -c '^\[remote' "$OUT/trees.remotes") remotes -> $OUT/trees.remotes"

$PY gen-static "$SRC/git-config" > "$OUT/git-config.static"
echo "gen-static: -> $OUT/git-config.static"

$PY validate "$OUT/trees"

# Sanity-check the include wiring in a throwaway repo.
R="$TMP/repo"; mkdir -p "$R"; (cd "$R" && git init -q)
cp "$OUT/git-config.static" "$OUT/trees.remotes" "$R/"
{ echo '[include]'; echo "${TAB}path = $R/git-config.static"; echo "${TAB}path = $R/trees.remotes"; } >> "$R/.git/config"
first=$(grep -m1 '^\[remote' "$OUT/trees.remotes" | sed -E 's/.*"(.*)".*/\1/')
if [ -n "$(git -C "$R" config --get "remote.$first.url")" ] && \
   [ -n "$(git -C "$R" config --get rerere.enabled)" ]; then
  echo "include: OK (remotes + plumbing resolve via .git/config)"
else
  echo "include: WARN resolution failed"
fi

echo
echo "Add to your next tree's .git/config (fresh bootstrap only -- else duplicates):"
echo "[include]"
printf '\tpath = %s\n' "$OUT/git-config.static"
printf '\tpath = %s\n' "$OUT/trees.remotes"
