# pick_image.py — audition stills on a Premiere XML timeline

Only JPEGs (`.jpg` / `.jpeg`) are considered, both on the timeline and in the folder.

Wherever two or more different JPEGs are stacked at the same moment (e.g. a b-roll still on V2
and a cast-matched still on V7), it plays the movie with each one cut in, lets you pick one
(or a replacement from a folder), asks how long it should stay on screen, and writes the pick
to one track (V1 by default) in a **new** XML. Your original XML is never changed.

## Setup (once, on your Mac)
- Download the three files from the `add-xml-image-picker` branch into one folder
  (e.g. `~/Downloads/Claude/FirstPass/ImagePicker/`): `pick_gui.py`, `pick_image.py`, `Pick Images.command`
- Optional but recommended: `brew install ffmpeg` (faster thumbnails)
- First run only: right-click `Pick Images.command` > Open > Open (macOS asks once because it was downloaded)

## Run (visual version)
1. In Premiere, export the sequence: File > Export > Media, **H.264 .mp4**, small/720p is fine.
   This is what you'll see and hear in the picker.
2. Double-click **Pick Images.command** and drag in, when asked: the XML, the .mp4, the
   stills folder, and type the track for picks (7 for the Eve's Bayou test sequence).
3. Your browser opens the picker:
   - **left**: every place where 2+ different .jpg images compete
   - **middle**: the movie with sound, playing 3s before → the image → 3s after
   - **right**: the candidates on the timeline, plus your folder (search by file name or character)
   - click an image to watch it in context, set **On screen for**, click **Use this image**
4. Each pick saves `<xml name>_picked.xml` immediately. **Undo last pick** works. When done,
   close the browser tab, press Ctrl+C in the Terminal window, and import the `_picked.xml`
   in Premiere (File > Import).

### Decided places, coming back later, inserting new images
- Decided places stay in the left list with a green ✓ and the image you chose. Click one to
  re-watch it or change your mind ("Change to this image"). "Next undecided" jumps ahead;
  "Hide decided places" shortens the list.
- Close it any time. Run it again on the same XML and it **resumes** from `_picked.xml`,
  ✓ marks included (they're kept in `<name>_picked.picks.json`). `--fresh` starts over.
- **＋ Insert an image** puts an image where there was none: scrub the movie, press
  "Use playhead" (or type a timecode), click an image, set how long, "Use this image".
- **Images that aren't in your folder**: drag .jpg files onto the page or click
  "Add image file…". They're copied into a `Picker added images` folder next to your XML,
  so Premiere can find them.

Keyboard: N next undecided · Space play/pause · 1–9 pick a timeline image · Enter use it · ↑/↓ previous/next place · / search.

Command line instead of double-click:
```
python3 pick_gui.py timeline.xml --movie ref.mp4 --folder ~/Downloads/EvesBayou_stills --track 7
```
(`pick_image.py` is the older terminal-only version; the visual one uses it under the hood.)

## Which track to write to
Picks overwrite that stretch of the target track and remove the other candidates.
Before you confirm, it shows (in red under the button):
- **what it will cut** on the target track, and
- **what sits above** the target track and would cover the image.

For the Eve's Bayou test sequence: V1 is covered by footage on V3/V5 in 14 of 172 slots;
V7 is never covered and only cuts one Graphic. Pick the track in the top-right menu (or `--track 7`).

## Other options
`--include-single` also stops at lone images (to swap in a folder image) ·
`--start 00:05:00:00 --end 00:06:00:00` only this window ·
`--preroll 3 --postroll 3` seconds of context · `--scale fit|fill|none` for folder images ·
`--keep-others` don't remove the unchosen candidates · `--out file.xml`.
