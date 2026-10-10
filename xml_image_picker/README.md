# pick_image.py — audition stills on a Premiere XML timeline

Only JPEGs (`.jpg` / `.jpeg`) are considered, both on the timeline and in the folder.

Plays each alternate image cut into the movie, lets you pick one (or one from a folder),
asks how long it should stay on screen, and writes it to V1 in a new XML.

## Setup (once)
- Python 3.8+
- ffmpeg (includes ffprobe and ffplay). Mac: `brew install ffmpeg`. Windows: `winget install ffmpeg`

## Use
1. In Premiere: **File > Export > Final Cut Pro XML** → `timeline.xml`
2. In Premiere: **File > Export > Media**, H.264, low resolution is fine → `reference.mp4`
   (for previews; see note below)
3. Run:
   ```
   python3 pick_image.py timeline.xml --movie reference.mp4 --folder ./alt_images
   ```
4. In Premiere: **File > Import** → `timeline_picked.xml`. Your original XML is never changed.

## Commands in the picker
| type | does |
|---|---|
| `p 2` / `p F3` | play the movie with timeline candidate 2 / folder image 3 cut in |
| `pa` / `pf` | play all timeline candidates / all folder images, one after another |
| `s 2` / `s F3` | choose it → asks duration (`4`, `4.5s`, `96f`, `00:00:04:00`, Enter = keep) → optional final preview → commit |
| `k` / `l` / `q` | skip slot / list again / save and quit |

## How it reads your timeline
- A **slot** is any stretch where still images overlap, across all video tracks
  (e.g. three alternates stacked on V2–V4). Each slot is offered in turn.
- If your alternates are laid out differently (e.g. one after another), use
  `--start 00:01:10:00 --end 00:01:20:00`: every image in that window becomes a candidate.
- Committing **overwrites** V1 from the slot start for the chosen duration (splits/trims
  what's there, no ripple — audio stays in sync), and removes the other candidates
  (`--keep-others` to leave them).
- Folder images are scaled to fit the frame (`--scale fill` to crop-fill, `--scale none` to leave at 100%).

## Options
`--preroll 3 --postroll 3` seconds of context around the image ·
`--movie-offset 60` if your reference export starts 60s into the sequence ·
`--out file.xml` output path.

Without `--movie`, previews are rebuilt from the V1 video and A1 audio media the XML points to
(ignores titles, effects, other tracks), or black if the media isn't found. The reference
export is what makes the preview match what you actually see.
