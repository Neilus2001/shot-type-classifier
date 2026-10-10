#!/bin/bash
# Double-click: opens the Image Picker on the Eve's Bayou test sequence with everything pre-filled.
cd "$(dirname "$0")"

MOVIE="$HOME/Downloads/EvesBayou_ref.mp4"
FOLDER="$HOME/Downloads/EvesBayou_stills"
TRACK=7
XML="$HOME/Downloads/Eves_Bayou_XMLtest.xml"

if [ ! -f "$XML" ]; then
  echo "Couldn't find $XML"
  read -r -p "Drag the Eve's Bayou XML into this window, then press Return: " XML
  XML="$(printf '%s' "$XML" | sed -e 's/[[:space:]]*$//' -e 's/^[[:space:]]*//' -e "s/^'\(.*\)'$/\1/" -e 's/\\\(.\)/\1/g')"
fi
[ -f "$MOVIE" ] || echo "Note: reference movie not found at $MOVIE (you'll see images only, no movie or sound)."
[ -d "$FOLDER" ] || echo "Note: stills folder not found at $FOLDER."

ARGS=("$XML" --track "$TRACK")
[ -f "$MOVIE" ] && ARGS+=(--movie "$MOVIE")
[ -d "$FOLDER" ] && ARGS+=(--folder "$FOLDER")

python3 pick_gui.py "${ARGS[@]}"
echo
read -r -p "Press Return to close this window."
