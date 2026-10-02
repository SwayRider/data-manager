#!/usr/bin/env bash
# Re-downloads the glyph ranges and sprites of protomaps/basemaps-assets into the configure blueprint's static folder.
set -euo pipefail
BASE=https://protomaps.github.io/basemaps-assets
DEST="$(dirname "$0")/../blueprints/configure/static/map-assets"
for font in "Noto Sans Regular" "Noto Sans Medium" "Noto Sans Italic"; do
  mkdir -p "$DEST/fonts/$font"
  for i in $(seq 0 255); do echo "$((i * 256))-$((i * 256 + 255))"; done |
    xargs -P 16 -I{} sh -c 'curl -sfL -o "$1/fonts/$2/$3.pbf" "'"$BASE"'/fonts/$(echo "$2" | sed "s/ /%20/g")/$3.pbf"' _ "$DEST" "$font" {}
done
mkdir -p "$DEST/sprites"
for flavor in light dark white black grayscale; do
  for ext in .json .png @2x.json @2x.png; do curl -sfL -o "$DEST/sprites/$flavor$ext" "$BASE/sprites/v4/$flavor$ext"; done
done
