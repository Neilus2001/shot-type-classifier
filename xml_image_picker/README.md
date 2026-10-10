# pick_image.py — audition stills on a Premiere XML timeline

Only JPEGs (`.jpg` / `.jpeg`) are considered, both on the timeline and in the folder.

Wherever two or more different JPEGs are stacked at the same moment (e.g. a b-roll still on V2
and a cast-matched still on V7), it plays the movie with each one cut in, lets you pick one
(or a replacement from a folder), asks how long it should stay on screen, and writes the pick
to one track (V1 by default) in a **new** XML. Your original XML is never changed.

## Setup (once, on your Mac)
- Python 3 (you already have it: the FirstPass tools use it)
- ffmpeg: `brew install ffmpeg`
- Get the script: download `pick_image.py` from the `add-xml-image-picker` branch of
  `neilus2001/shot-type-classifier`, e.g. into `~/Downloads/Claude/FirstPass/`

## Run
```
cd ~/Downloads/Claude/FirstPass
python3 pick_image.py "/path/to/Eves_Bayou_XMLtest.xml" \
    --movie "/path/to/EvesBayou_reference.mp4" \
    --folder ~/Downloads/EvesBayou_stills
```
- `--movie`: File > Export > Media of the sequence, H.264, low resolution. This is what you
  see in the previews. Without it, previews are rebuilt from V1/A1 only (or black).
- `--folder`: replacement images. If the folder has `cast_labels.json` (written by the
  FirstPass face-naming step), the characters on screen are shown next to each still,
  and `f mozelle` filters the folder by character.

Output: `<input>_picked.xml`, saved after every pick. Import it with **File > Import**.

## Commands
| type | does |
|---|---|
| `p 1` / `p F12` | play the movie with candidate 1 / folder image 12 cut in |
| `pa` | play every candidate for this slot in a row |
| `f` / `f eve` | list the folder / only images matching a filename or character |
| `pf` | play every image from the last `f` listing |
| `s 1` / `s F12` | choose it → asks how long on screen (`4`, `4.5s`, `96f`, `00:00:04:00`, Enter = keep) → commit |
| `n` / `b` / `g 40` | next slot / back / jump to slot 40 |
| `q` | save and quit (you can resume later by running it on the `_picked.xml`) |

## Which track to write to
Picks overwrite that stretch of the target track and remove the other candidates.
Before you confirm, it tells you:
- **what it will cut** on the target track, and
- **what sits above** the target track and would cover the image.

For the Eve's Bayou test sequence: V1 is covered by footage on V3/V5 in 14 of 172 slots;
V7 is never covered and only cuts one Graphic. Use `--track 7` to write picks there instead.

## Other options
`--include-single` also stops at lone images (to swap in a folder image) ·
`--start 00:05:00:00 --end 00:06:00:00` only this window ·
`--preroll 3 --postroll 3` seconds of context · `--scale fit|fill|none` for folder images ·
`--keep-others` don't remove the unchosen candidates · `--out file.xml`.
