#!/bin/bash
# Double-click to start the Image Picker. Drag files from Finder into this window when asked.
cd "$(dirname "$0")"

# Finder drops paths like /Users/me/Eve\'s\ Bayou/file.xml (escaped, trailing space) or in quotes.
clean() {
  local p="$1"
  p="$(printf '%s' "$p" | sed -e 's/[[:space:]]*$//' -e 's/^[[:space:]]*//')"
  case "$p" in \'*\') p="${p:1:${#p}-2}" ;; \"*\") p="${p:1:${#p}-2}" ;; *) p="$(printf '%s' "$p" | sed -e 's/\\\(.\)/\1/g')" ;; esac
  printf '%s' "$p"
}

echo "=== Image Picker ==="
echo
read -r -p "1) Drag your Premiere XML here, then press Return: " XML
XML=$(clean "$XML")
read -r -p "2) Drag the reference movie (.mp4 export of the sequence) here, or just press Return to skip: " MOVIE
MOVIE=$(clean "$MOVIE")
read -r -p "3) Drag the folder of replacement images here, or just press Return to skip: " FOLDER
FOLDER=$(clean "$FOLDER")
read -r -p "4) Which video track should picks go on? [1]: " TRACK
TRACK=${TRACK:-1}

ARGS=("$XML" --track "$TRACK")
[ -n "$MOVIE" ] && ARGS+=(--movie "$MOVIE")
[ -n "$FOLDER" ] && ARGS+=(--folder "$FOLDER")

echo
python3 pick_gui.py "${ARGS[@]}"
echo
read -r -p "Press Return to close this window."
