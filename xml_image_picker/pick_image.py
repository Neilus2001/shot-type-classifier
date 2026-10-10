#!/usr/bin/env python3
"""
pick_image.py - audition still images on a Premiere Pro XML timeline and
commit the winner to V1.

Workflow
  1. Finds "slots": places on the timeline where one or more still images sit
     (typically alternates stacked on V2, V3, ... above V1).
  2. For each slot it plays the surrounding part of the movie with each
     candidate image cut in, so you can judge it in context.
  3. You pick a candidate, or pick a replacement from a folder of images.
  4. It asks how long the image should stay on screen.
  5. It overwrites that stretch of V1 with the chosen image, removes the other
     candidates from the slot, and writes a NEW xml (the original is untouched).

Requirements: Python 3.8+, and ffmpeg / ffprobe / ffplay on your PATH.

Usage
  python3 pick_image.py timeline.xml --movie reference_export.mp4 --folder ./alt_images

  --movie   A quick export of the whole sequence (File > Export > Media, any
            small H.264). Strongly recommended: the preview is cut from this.
            Without it the script tries to rebuild the preview from the V1/A1
            source media referenced in the XML, and falls back to black.
  --folder  Folder of extra images you can pick from if the candidates
            on the timeline don't work.

Premiere: File > Export > Final Cut Pro XML to get the xml, and
File > Import the *_picked.xml it writes.
"""

import argparse
import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse

# Only JPEGs are treated as candidates, on the timeline and in the folder.
IMAGE_EXTS = {".jpg", ".jpeg"}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def die(msg):
    print(f"\nERROR: {msg}")
    sys.exit(1)


def text(el, path, default=None):
    if el is None:
        return default
    found = el.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def set_text(el, tag, value):
    child = el.find(tag)
    if child is None:
        child = ET.SubElement(el, tag)
    child.text = str(value)


def pathurl_to_path(url):
    """file://localhost/Volumes/x/y%20z.png -> /Volumes/x/y z.png (also handles Windows drives)."""
    if not url:
        return None
    if not url.startswith("file:"):
        return url
    p = unquote(urlparse(url).path)
    # Windows: file://localhost/C%3a/foo -> /C:/foo -> C:/foo
    if re.match(r"^/[A-Za-z]:", p):
        p = p[1:]
    return os.path.normpath(p)  # Premiere sometimes writes "Downloads//folder"


def path_to_pathurl(path):
    path = os.path.abspath(path).replace("\\", "/")
    if re.match(r"^[A-Za-z]:", path):
        path = "/" + path
    return "file://localhost" + quote(path, safe="/:")


def is_image_path(p):
    return bool(p) and os.path.splitext(p)[1].lower() in IMAGE_EXTS


def require_tools():
    for tool in ("ffmpeg", "ffprobe", "ffplay"):
        if shutil.which(tool) is None:
            die(f"'{tool}' not found on PATH. Install ffmpeg (https://ffmpeg.org) and try again.")


def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def image_size(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path])
    m = re.search(r"(\d+)x(\d+)", r.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def has_audio(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path])
    return bool(r.stdout.strip())


# --------------------------------------------------------------------------- #
# Timeline model
# --------------------------------------------------------------------------- #

class Timeline:
    def __init__(self, xml_path, target_track=1):
        self.xml_path = xml_path
        self.tree = ET.parse(xml_path)
        self.root = self.tree.getroot()
        if self.root.tag != "xmeml":
            die("This doesn't look like a Premiere/Final Cut XML (root is not <xmeml>).")

        self.seq = self.root.find(".//sequence")
        if self.seq is None:
            die("No <sequence> found in the XML.")

        tb = float(text(self.seq, "rate/timebase", "24"))
        ntsc = text(self.seq, "rate/ntsc", "FALSE").upper() == "TRUE"
        self.timebase = int(round(tb))
        self.ntsc = ntsc
        self.fps = tb * 1000 / 1001 if ntsc else tb

        fmt = self.seq.find("media/video/format/samplecharacteristics")
        self.width = int(text(fmt, "width", "1920"))
        self.height = int(text(fmt, "height", "1080"))

        self.video_tracks = self.seq.findall("media/video/track")
        self.audio_tracks = self.seq.findall("media/audio/track")
        if not self.video_tracks:
            die("Sequence has no video tracks.")
        if not 1 <= target_track <= len(self.video_tracks):
            die(f"--track {target_track}: the sequence only has V1-V{len(self.video_tracks)}.")
        self.target = target_track - 1           # where picks are written (V1 by default)

        # Full <file> definitions by id (Premiere writes the full one once, then <file id="x"/>).
        self.files = {}
        for f in self.root.iter("file"):
            fid = f.get("id")
            if fid and len(f) > 0 and fid not in self.files:
                self.files[fid] = copy.deepcopy(f)

        self._uid = 0

    # ---- time ------------------------------------------------------------ #
    def tc(self, frames):
        frames = int(round(frames))
        fb = self.timebase
        h, rem = divmod(frames, fb * 3600)
        m, rem = divmod(rem, fb * 60)
        s, f = divmod(rem, fb)
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

    def secs(self, frames):
        return frames / self.fps

    def parse_duration(self, s):
        """'4' / '4s' / '4.5' -> seconds, '96f' -> frames, '00:00:04:00' -> timecode. Returns frames."""
        s = s.strip().lower()
        if re.fullmatch(r"\d+f", s):
            return int(s[:-1])
        if re.fullmatch(r"\d+:\d+:\d+[:;]\d+", s):
            h, m, sec, f = [int(x) for x in re.split(r"[:;]", s)]
            return ((h * 3600 + m * 60 + sec) * self.timebase) + f
        if re.fullmatch(r"\d+(\.\d+)?s?", s):
            return int(round(float(s.rstrip("s")) * self.fps))
        raise ValueError(s)

    # ---- clips ----------------------------------------------------------- #
    def file_of(self, clip):
        f = clip.find("file")
        if f is None:
            return None
        return self.files.get(f.get("id"), f)

    def clip_path(self, clip):
        return pathurl_to_path(text(self.file_of(clip), "pathurl"))

    def clip_name(self, clip):
        return text(clip, "name") or os.path.basename(self.clip_path(clip) or "?")

    def is_still(self, clip):
        """A candidate image: a JPEG still. Other stills (PNG, titles, ...) are left alone."""
        return is_image_path(self.clip_path(clip))

    def is_any_still(self, clip):
        return self.is_still(clip) or text(clip, "stillframe", "").upper() == "TRUE"

    def resolve(self, track):
        """Return [(clip, start, end)] for a track, filling in -1 values next to transitions."""
        items = list(track)
        out = []
        for i, el in enumerate(items):
            if el.tag != "clipitem":
                continue
            st, en = int(text(el, "start", "-1")), int(text(el, "end", "-1"))
            if st == -1:
                prev = next((p for p in reversed(items[:i]) if p.tag == "transitionitem"), None)
                st = int(text(prev, "start", "0")) if prev is not None else 0
                if prev is not None and text(prev, "alignment") == "center":
                    st = (int(text(prev, "start")) + int(text(prev, "end"))) // 2
            if en == -1:
                nxt = next((n for n in items[i + 1:] if n.tag == "transitionitem"), None)
                en = int(text(nxt, "end", "0")) if nxt is not None else st
                if nxt is not None and text(nxt, "alignment") == "center":
                    en = (int(text(nxt, "start")) + int(text(nxt, "end"))) // 2
            out.append((el, st, en))
        return out

    def new_id(self, prefix):
        existing = {e.get("id") for e in self.root.iter() if e.get("id")}
        while True:
            self._uid += 1
            cand = f"{prefix}-picked-{self._uid}"
            if cand not in existing:
                return cand

    # ---- slots ----------------------------------------------------------- #
    def find_slots(self, window=None):
        """Group still-image clips that overlap in time (across all video tracks) into slots."""
        stills = []
        for ti, track in enumerate(self.video_tracks):
            for clip, st, en in self.resolve(track):
                if en > st and self.is_still(clip):
                    stills.append({"clip": clip, "track": ti, "start": st, "end": en})

        if window:
            ws, we = window
            group = [s for s in stills if s["start"] < we and s["end"] > ws]
            if not group:
                return []
            return [self._slot(group)]

        stills.sort(key=lambda s: s["start"])
        slots, cur, cur_end = [], [], None
        for s in stills:
            if cur and s["start"] < cur_end:
                cur.append(s)
                cur_end = max(cur_end, s["end"])
            else:
                if cur:
                    slots.append(self._slot(cur))
                cur, cur_end = [s], s["end"]
        if cur:
            slots.append(self._slot(cur))
        return self._merge_pieces(slots)

    def _merge_pieces(self, slots):
        """A shot cut into back-to-back pieces (same images each time) is one decision, not several."""
        merged = []
        for sl in slots:
            if merged and merged[-1]["end"] == sl["start"] and self._paths(merged[-1]) == self._paths(sl):
                merged[-1] = self._slot(merged[-1]["candidates"] + sl["candidates"])
            else:
                merged.append(sl)
        return merged

    def _paths(self, slot):
        return {self.clip_path(c["clip"]) for c in slot["candidates"]}

    def would_cut(self, start, end, ignore):
        """Clips already on the target track in [start, end) that the pick will cut or remove."""
        return [f"V{self.target + 1} '{self.clip_name(clip)}'"
                for clip, st, en in self.resolve(self.video_tracks[self.target])
                if st < end and en > start and clip not in ignore]

    def covering(self, start, end, ignore):
        """Non-candidate video above the target track in [start, end) that would hide the pick."""
        found = []
        for ti, track in enumerate(self.video_tracks[self.target + 1:], self.target + 2):
            for clip, st, en in self.resolve(track):
                if st < end and en > start and clip not in ignore and text(clip, "enabled", "TRUE") == "TRUE":
                    name = self.clip_name(clip)
                    if "adjustment layer" not in name.lower():
                        found.append(f"V{ti} '{name}'")
        return found

    def _slot(self, group):
        group.sort(key=lambda s: (-s["track"], s["start"]))  # top track first (what's visible)
        return {"start": min(s["start"] for s in group),
                "end": max(s["end"] for s in group),
                "candidates": group}

    # ---- the edit -------------------------------------------------------- #
    def commit(self, slot, choice, duration, remove_others=True, scale_mode="fit"):
        """Overwrite the target track (V1 by default) from slot start for `duration` frames."""
        s = slot["start"]
        e = s + duration
        v1 = self.video_tracks[self.target]

        # 1. Build the new clipitem.
        if choice["kind"] == "timeline":
            new = copy.deepcopy(choice["clip"])
            fid = new.find("file").get("id")
            # Always carry the full <file> definition; the original holder may be deleted.
            if fid in self.files:
                new.remove(new.find("file"))
                new.insert(self._file_index(new), copy.deepcopy(self.files[fid]))
        else:
            template = slot["candidates"][0]["clip"]
            new = copy.deepcopy(template)
            for tag in ("file", "masterclipid", "link", "filter"):
                for child in new.findall(tag):
                    new.remove(child)
            new.insert(self._file_index(new), self._make_file(choice["path"], template))
            set_text(new, "name", os.path.basename(choice["path"]))
            f = self._scale_filter(choice["path"], scale_mode)
            if f is not None:
                new.append(f)

        new.set("id", self.new_id("clipitem"))
        for child in new.findall("link"):
            new.remove(child)
        in_pt = int(text(new, "in", "0"))
        if in_pt < 0:
            in_pt = 0
        set_text(new, "start", s)
        set_text(new, "end", e)
        set_text(new, "in", in_pt)
        set_text(new, "out", in_pt + duration)
        file_dur = int(text(self.files.get(new.find("file").get("id"), new.find("file")), "duration", "0") or 0)
        if file_dur and file_dur < in_pt + duration:
            # Stills can be any length; make sure Premiere doesn't think we ran off the media.
            for f in [new.find("file")] + [self.files.get(new.find("file").get("id"))]:
                if f is not None and f.find("duration") is not None:
                    set_text(f, "duration", in_pt + duration + 1)
            set_text(new, "duration", in_pt + duration + 1)

        # 2. Remove the other candidates (and the chosen one's old position).
        removed_ids = set()
        for c in slot["candidates"]:
            if remove_others or (choice["kind"] == "timeline" and c["clip"] is choice["clip"]):
                track = self.video_tracks[c["track"]]
                if c["clip"] in list(track):
                    removed_ids.add(c["clip"].get("id"))
                    track.remove(c["clip"])

        # 3. Overwrite [s, e) on the target track.
        for clip, st, en in self.resolve(v1):
            if en <= s or st >= e:
                continue
            cin = int(text(clip, "in", "0"))
            if st >= s and en <= e:                      # fully covered -> remove
                removed_ids.add(clip.get("id"))
                v1.remove(clip)
            elif st < s and en > e:                      # spans the slot -> split
                right = copy.deepcopy(clip)
                right.set("id", self.new_id("clipitem"))
                for child in right.findall("link"):
                    right.remove(child)
                self._dedupe_file(right)
                nested = right.find("sequence")
                if nested is not None:
                    for child in list(nested):
                        nested.remove(child)
                set_text(right, "start", e)
                set_text(right, "end", en)
                set_text(right, "in", cin + (e - st))
                set_text(clip, "start", st)
                set_text(clip, "end", s)
                set_text(clip, "out", cin + (s - st))
                v1.insert(list(v1).index(clip) + 1, right)
            elif st < s:                                 # tail overlaps -> trim end
                set_text(clip, "start", st)
                set_text(clip, "end", s)
                set_text(clip, "out", cin + (s - st))
            else:                                        # head overlaps -> trim start
                set_text(clip, "start", e)
                set_text(clip, "end", en)
                set_text(clip, "in", cin + (e - st))

        for t in list(v1.findall("transitionitem")):
            ts, te = int(text(t, "start", "0")), int(text(t, "end", "0"))
            if ts < e and te > s:
                v1.remove(t)

        # 4. Insert the new clip in time order.
        idx = len([c for c in v1 if c.tag not in ("clipitem", "transitionitem", "generatoritem")])
        for i, el in enumerate(list(v1)):
            if el.tag in ("clipitem", "transitionitem", "generatoritem"):
                st = int(text(el, "start", "-1"))
                if st == -1 or st < s:
                    idx = i + 1
        v1.insert(idx, new)

        # 5. Clean dangling <link>s and make sure every file id is defined once, in full.
        self._drop_links(removed_ids)
        self._normalize_files()

    def _file_index(self, clip):
        for i, child in enumerate(list(clip)):
            if child.tag in ("sourcetrack", "filter", "link", "labels", "comments", "logginginfo"):
                return i
        return len(clip)

    def _dedupe_file(self, clip):
        f = clip.find("file")
        if f is not None and len(f) > 0:
            for child in list(f):
                f.remove(child)

    def _make_file(self, path, template_clip):
        fid = self.new_id("file")
        tmpl_file = self.file_of(template_clip)
        f = copy.deepcopy(tmpl_file) if tmpl_file is not None else ET.Element("file")
        f.set("id", fid)
        for child in list(f):
            if child.tag not in ("rate", "duration", "timecode", "media"):
                f.remove(child)
        ET.SubElement(f, "name").text = os.path.basename(path)
        ET.SubElement(f, "pathurl").text = path_to_pathurl(path)
        if f.find("rate") is None:
            r = ET.SubElement(f, "rate")
            ET.SubElement(r, "timebase").text = str(self.timebase)
            ET.SubElement(r, "ntsc").text = "TRUE" if self.ntsc else "FALSE"
        if f.find("duration") is None:
            ET.SubElement(f, "duration").text = str(self.timebase * 3600)
        size = image_size(path)
        media = f.find("media")
        if media is None:
            media = ET.SubElement(f, "media")
        for a in media.findall("audio"):
            media.remove(a)
        video = media.find("video")
        if video is None:
            video = ET.SubElement(media, "video")
        sc = video.find("samplecharacteristics")
        if sc is None:
            sc = ET.SubElement(video, "samplecharacteristics")
        if size:
            set_text(sc, "width", size[0])
            set_text(sc, "height", size[1])
        # reorder so name/pathurl come first like Premiere writes them
        kids = list(f)
        for k in kids:
            f.remove(k)
        order = {"name": 0, "pathurl": 1, "rate": 2, "duration": 3, "timecode": 4, "media": 5}
        for k in sorted(kids, key=lambda k: order.get(k.tag, 9)):
            f.append(k)
        self.files[fid] = copy.deepcopy(f)
        return f

    def _scale_filter(self, path, mode):
        if mode == "none":
            return None
        size = image_size(path)
        if not size:
            return None
        w, h = size
        sx, sy = self.width / w, self.height / h
        scale = (min(sx, sy) if mode == "fit" else max(sx, sy)) * 100
        if abs(scale - 100) < 0.01:
            return None
        flt = ET.Element("filter")
        eff = ET.SubElement(flt, "effect")
        ET.SubElement(eff, "name").text = "Basic Motion"
        ET.SubElement(eff, "effectid").text = "basic"
        ET.SubElement(eff, "effectcategory").text = "motion"
        ET.SubElement(eff, "effecttype").text = "motion"
        ET.SubElement(eff, "mediatype").text = "video"
        p = ET.SubElement(eff, "parameter", {"authoringApp": "PremierePro"})
        ET.SubElement(p, "parameterid").text = "scale"
        ET.SubElement(p, "name").text = "Scale"
        ET.SubElement(p, "valuemin").text = "0"
        ET.SubElement(p, "valuemax").text = "1000"
        ET.SubElement(p, "value").text = f"{scale:.2f}"
        return flt

    def _drop_links(self, removed_ids):
        if not removed_ids:
            return
        for clip in self.root.iter("clipitem"):
            for link in list(clip.findall("link")):
                if text(link, "linkclipref") in removed_ids:
                    clip.remove(link)

    def _normalize_files(self):
        """First reference to each file id gets the full definition; the rest become <file id=.../>."""
        seen = set()
        for clip in self.root.iter("clipitem"):
            f = clip.find("file")
            if f is None:
                continue
            fid = f.get("id")
            for child in list(f):
                f.remove(child)
            if fid not in seen and fid in self.files:
                for child in self.files[fid]:
                    f.append(copy.deepcopy(child))
                seen.add(fid)

    # ---- output ---------------------------------------------------------- #
    def write(self, out_path):
        if hasattr(ET, "indent"):
            ET.indent(self.tree, space="\t")
        body = ET.tostring(self.root, encoding="unicode")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
            fh.write(body)
            fh.write("\n")


# --------------------------------------------------------------------------- #
# Preview rendering
# --------------------------------------------------------------------------- #

class Previewer:
    def __init__(self, tl, movie, movie_offset, preroll, postroll, workdir):
        self.tl = tl
        self.movie = movie
        self.movie_offset = movie_offset
        self.preroll = preroll
        self.postroll = postroll
        self.workdir = workdir
        self.movie_has_audio = has_audio(movie) if movie else False
        self._n = 0
        self._bg_cache = {}

    def _fit(self, label):
        W, H = 960, int(round(960 * self.tl.height / self.tl.width / 2) * 2)
        return W, H, (f"[{label}]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={self.tl.fps:.6f}")

    def background(self, start_f, end_f):
        """Movie around the slot (without any candidate on top). Cached per window."""
        key = (start_f, end_f)
        if key in self._bg_cache:
            return self._bg_cache[key]
        self._n += 1
        out = os.path.join(self.workdir, f"bg_{self._n}.mp4")
        t0 = self.tl.secs(start_f)
        dur = self.tl.secs(end_f - start_f)
        W, H, _ = self._fit("x")

        ok = False
        if self.movie:
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{t0 + self.movie_offset:.3f}", "-t", f"{dur:.3f}",
                   "-i", self.movie,
                   "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.tl.fps:.6f}",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p"]
            cmd += (["-c:a", "aac", "-ar", "48000", "-ac", "2"] if self.movie_has_audio else ["-an"])
            ok = run(cmd + [out]).returncode == 0
        if not ok:
            ok = self._rebuild_from_xml(start_f, end_f, out)
        if not ok:
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=black:s={W}x{H}:r={self.tl.fps:.6f}",
                 "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{dur:.3f}",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", out])
        self._bg_cache[key] = out
        return out

    def _rebuild_from_xml(self, start_f, end_f, out):
        """Best effort: V1 non-still clips + A1 clips from the source media referenced in the XML."""
        W, H, _ = self._fit("x")
        fps = self.tl.fps
        inputs, vparts, aparts = [], [], []

        def segments(track, want_still):
            segs, cursor = [], start_f
            for clip, st, en in sorted(self.tl.resolve(track), key=lambda c: c[1]):
                if en <= start_f or st >= end_f:
                    continue
                if want_still is False and self.tl.is_any_still(clip):
                    continue
                p = self.tl.clip_path(clip)
                if not p or not os.path.exists(p):
                    continue
                a, b = max(st, start_f), min(en, end_f)
                if a > cursor:
                    segs.append((None, None, a - cursor))
                cin = int(text(clip, "in", "0"))
                segs.append((p, cin + (a - st), b - a))
                cursor = b
            if cursor < end_f:
                segs.append((None, None, end_f - cursor))
            return segs

        vsegs = segments(self.tl.video_tracks[0], False)
        asegs = segments(self.tl.audio_tracks[0], None) if self.tl.audio_tracks else [(None, None, end_f - start_f)]
        if all(p is None for p, _, _ in vsegs) and all(p is None for p, _, _ in asegs):
            return False

        fc = []
        for i, (p, src_in, n) in enumerate(vsegs):
            d = n / fps
            if p:
                inputs += ["-ss", f"{src_in / fps:.3f}", "-t", f"{d:.3f}", "-i", p]
                fc.append(f"[{len(inputs) // 6 - 1}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.6f},trim=duration={d:.3f}[v{i}]")
            else:
                fc.append(f"color=black:s={W}x{H}:r={fps:.6f}:d={d:.3f},setsar=1[v{i}]")
            vparts.append(f"[v{i}]")
        for i, (p, src_in, n) in enumerate(asegs):
            d = n / fps
            if p and has_audio(p):
                inputs += ["-ss", f"{src_in / fps:.3f}", "-t", f"{d:.3f}", "-i", p]
                fc.append(f"[{len(inputs) // 6 - 1}:a]aresample=48000,aformat=channel_layouts=stereo,"
                          f"apad,atrim=duration={d:.3f}[a{i}]")
            else:
                fc.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.3f}[a{i}]")
            aparts.append(f"[a{i}]")
        fc.append(f"{''.join(vparts)}concat=n={len(vparts)}:v=1:a=0[vout]")
        fc.append(f"{''.join(aparts)}concat=n={len(aparts)}:v=0:a=1[aout]")
        cmd = ["ffmpeg", "-y", "-v", "error"] + inputs + [
            "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", out]
        return run(cmd).returncode == 0

    def render(self, slot, image_path, duration):
        """Background window with `image_path` cut in from slot start for `duration` frames."""
        s = slot["start"]
        win_start = max(0, s - int(round(self.preroll * self.tl.fps)))
        win_end = max(slot["end"], s + duration) + int(round(self.postroll * self.tl.fps))
        bg = self.background(win_start, win_end)

        t_in = self.tl.secs(s - win_start)
        t_out = t_in + self.tl.secs(duration)
        _, _, fit = self._fit("1:v")
        self._n += 1
        out = os.path.join(self.workdir, f"preview_{self._n}.mp4")
        fc = (f"{fit}[img];[0:v][img]overlay=0:0:enable='between(t,{t_in:.3f},{t_out:.3f})'[v]")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", bg, "-loop", "1", "-i", image_path,
               "-filter_complex", fc, "-map", "[v]", "-map", "0:a?", "-shortest",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "copy", out]
        r = run(cmd)
        if r.returncode != 0:
            print(f"  (couldn't render preview: {r.stderr.strip()[-300:]})")
            return None
        return out

    def play(self, path, title):
        subprocess.run(["ffplay", "-autoexit", "-loglevel", "error", "-window_title", title,
                        "-x", "960", path])


# --------------------------------------------------------------------------- #
# Interactive session
# --------------------------------------------------------------------------- #

def list_folder(folder):
    if not folder:
        return []
    if not os.path.isdir(folder):
        print(f"  (folder not found: {folder})")
        return []
    return sorted(os.path.join(folder, f) for f in os.listdir(folder)
                  if is_image_path(f) and not f.startswith("."))


def load_cast(folder):
    """Character names per still from the face-naming step (<stills>/cast_labels.json), keyed by filename."""
    if not folder:
        return {}
    p = os.path.join(folder, "cast_labels.json")
    if not os.path.exists(p):
        return {}
    try:
        import json
        with open(p, encoding="utf-8") as fh:
            stills = json.load(fh).get("stills", {})
    except (ValueError, OSError) as e:
        print(f"  (couldn't read {p}: {e})")
        return {}
    cast = {}
    for fname, people in stills.items():
        names = [x["name"] if isinstance(x, dict) else str(x) for x in people]
        cast[os.path.basename(fname).lower()] = names
    return cast


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        sys.exit(0)


class Session:
    def __init__(self, tl, pv, args):
        self.tl, self.pv, self.args = tl, pv, args
        self.folder_imgs = list_folder(args.folder)
        self.cast = load_cast(args.folder)
        self.shown_folder = []        # folder images from the last `f` listing, for `pf`

    def who(self, path):
        names = self.cast.get(os.path.basename(path or "").lower())
        return f"  — {', '.join(names)}" if names else ""

    # ---- one slot ------------------------------------------------------- #
    def options_for(self, slot):
        opts, seen = [], set()
        for c in slot["candidates"]:              # top track first; one entry per image file
            p = self.tl.clip_path(c["clip"])
            if p in seen:
                continue
            seen.add(p)
            off = "" if text(c["clip"], "enabled", "TRUE") == "TRUE" else " [disabled]"
            opts.append({"kind": "timeline", "clip": c["clip"], "path": p,
                         "label": f"{os.path.basename(p)}  [V{c['track'] + 1}]{off}"})
        return opts

    def show(self, n, total, slot, opts):
        tl = self.tl
        s, e = slot["start"], slot["end"]
        print(f"\n=== Slot {n} of {total}: {tl.tc(s)} -> {tl.tc(e)}  ({tl.secs(e - s):.2f}s) ===")
        for i, o in enumerate(opts, 1):
            missing = "" if o["path"] and os.path.exists(o["path"]) else "   (FILE NOT FOUND)"
            print(f"  {i:>3}. {o['label']}{self.who(o['path'])}{missing}")
        if self.folder_imgs:
            labelled = sum(1 for p in self.folder_imgs if self.who(p))
            extra = f", {labelled} with cast names" if self.cast else ""
            print(f"  + folder: {len(self.folder_imgs)} images{extra}.  f = list all,  f eve = filter by name/character")
        print("  p 2 / p F3 preview   pa preview all   s 2 / s F3 choose   n next   b back   g 12 go to slot   q save & quit")

    def list_folder_matches(self, query):
        q = (query or "").lower()
        hits = [(i, p) for i, p in enumerate(self.folder_imgs, 1)
                if not q or q in os.path.basename(p).lower() or q in self.who(p).lower()]
        self.shown_folder = [p for _, p in hits]
        if not hits:
            print("  No folder images match.")
        for i, p in hits:
            print(f"  F{i:<4} {os.path.basename(p)}{self.who(p)}")

    def get(self, token, opts):
        token = token.upper()
        pool, idx = (self.folder_imgs, token[1:]) if token.startswith("F") else (opts, token)
        if not idx.isdigit() or not (1 <= int(idx) <= len(pool)):
            return None
        o = pool[int(idx) - 1]
        if token.startswith("F"):
            return {"kind": "folder", "path": o, "label": os.path.basename(o)}
        return o

    def preview(self, slot, o, dur):
        if not o["path"] or not os.path.exists(o["path"]):
            print(f"  Can't preview {o['label']}: image file not found on this machine.")
            return
        print(f"  Rendering {o['label']} ...")
        out = self.pv.render(slot, o["path"], dur)
        if out:
            print("  Playing (q or Esc closes the window).")
            self.pv.play(out, o["label"])

    def ask_duration(self, o, cur):
        tl = self.tl
        while True:
            ans = ask(f"  How long should '{o['label']}' stay on screen? "
                      f"[Enter = {tl.secs(cur):.2f}s]  (4, 4.5s, 96f or 00:00:04:00): ")
            if not ans:
                return cur
            try:
                d = tl.parse_duration(ans)
                if d > 0:
                    return d
            except ValueError:
                pass
            print("  Didn't understand that duration.")

    def run_slot(self, n, total, slot):
        """Returns 'done', 'next', 'back', 'quit' or ('goto', index)."""
        tl = self.tl
        opts = self.options_for(slot)
        cur = slot["end"] - slot["start"]
        self.show(n, total, slot, opts)
        while True:
            cmd = ask("> ").split(None, 1)
            if not cmd:
                continue
            op, arg = cmd[0].lower(), (cmd[1].strip() if len(cmd) > 1 else None)
            if op == "q":
                return "quit"
            if op in ("n", "k"):
                return "next"
            if op == "b":
                return "back"
            if op == "g" and arg and arg.isdigit():
                return ("goto", int(arg) - 1)
            if op == "l":
                self.show(n, total, slot, opts)
            elif op == "f":
                self.list_folder_matches(arg)
            elif op == "pa":
                for o in opts:
                    self.preview(slot, o, cur)
            elif op == "pf":
                pool = self.shown_folder or self.folder_imgs
                if len(pool) > 10 and ask(f"  Preview all {len(pool)}? Use `f <name>` to narrow first. [y/N] ").lower() != "y":
                    continue
                for p in pool:
                    self.preview(slot, {"path": p, "label": os.path.basename(p)}, cur)
            elif op == "p" and arg:
                o = self.get(arg, opts)
                self.preview(slot, o, cur) if o else print("  No such option.")
            elif op == "s" and arg:
                o = self.get(arg, opts)
                if not o:
                    print("  No such option.")
                    continue
                dur = self.ask_duration(o, cur)
                s = slot["start"]
                vt = f"V{tl.target + 1}"
                print(f"  -> {tl.secs(dur):.2f}s ({dur} frames), {tl.tc(s)} to {tl.tc(s + dur)} on {vt}")
                if dur > cur:
                    print(f"  Note: longer than the slot; overwrites {dur - cur} more frames of {vt}.")
                cover = tl.covering(s, s + dur, {c["clip"] for c in slot["candidates"]})
                if cover:
                    print(f"  HEADS UP: these are above {vt} here and will cover the image (see --track):")
                    for c in cover[:6]:
                        print(f"     {c}")
                cut = tl.would_cut(s, s + dur, {c["clip"] for c in slot["candidates"]})
                if cut:
                    print(f"  This will cut/shorten what's already on {vt} here:")
                    for c in cut[:6]:
                        print(f"     {c}")
                if ask("  Preview the final result first? [y/N] ").lower() == "y":
                    self.preview(slot, o, dur)
                if ask(f"  Commit to {vt}? [Y/n] ").lower() == "n":
                    self.show(n, total, slot, opts)
                    continue
                tl.commit(slot, o, dur, remove_others=not self.args.keep_others, scale_mode=self.args.scale)
                return "done"
            else:
                print("  Unknown command (l lists the options again).")


def parse_time_arg(tl, s):
    try:
        return tl.parse_duration(s)
    except ValueError:
        die(f"Can't parse time '{s}'. Use seconds (12.5), frames (300f) or timecode (00:00:12:12).")


def main():
    ap = argparse.ArgumentParser(description="Audition still images on a Premiere XML timeline and put the winner on V1.")
    ap.add_argument("xml", help="Premiere Pro / FCP7 XML exported from your sequence")
    ap.add_argument("--movie", help="Reference export of the sequence (mp4/mov) used for previews")
    ap.add_argument("--movie-offset", type=float, default=0.0,
                    help="Seconds into the sequence where the reference movie starts (if you exported a range)")
    ap.add_argument("--folder", help="Folder of alternate images (cast_labels.json in it is used for character names)")
    ap.add_argument("--out", help="Output XML path (default: <input>_picked.xml)")
    ap.add_argument("--start", help="Only look at images in this window: start (seconds, 300f, or timecode)")
    ap.add_argument("--end", help="Window end (use with --start)")
    ap.add_argument("--preroll", type=float, default=3.0, help="Seconds of movie to play before the image (default 3)")
    ap.add_argument("--postroll", type=float, default=3.0, help="Seconds to play after the image (default 3)")
    ap.add_argument("--scale", choices=["fit", "fill", "none"], default="fit",
                    help="How to scale a folder image to the frame (default fit)")
    ap.add_argument("--track", type=int, default=1,
                    help="Video track the picks are written to (default 1 = V1). Use a track above your footage "
                         "if footage on higher tracks would cover V1.")
    ap.add_argument("--include-single", action="store_true",
                    help="Also stop at images with no alternate on the timeline (to swap in a folder image)")
    ap.add_argument("--keep-others", action="store_true",
                    help="Leave the unchosen candidates on their tracks instead of removing them")
    args = ap.parse_args()

    require_tools()
    if not os.path.exists(args.xml):
        die(f"XML not found: {args.xml}")
    if args.movie and not os.path.exists(args.movie):
        die(f"Movie not found: {args.movie}")

    tl = Timeline(args.xml, args.track)
    out_path = args.out or os.path.splitext(args.xml)[0] + "_picked.xml"
    if os.path.abspath(out_path) == os.path.abspath(args.xml):
        die("--out must be different from the input XML (the original is never overwritten).")

    window = None
    if args.start or args.end:
        if not (args.start and args.end):
            die("Use --start and --end together.")
        window = (parse_time_arg(tl, args.start), parse_time_arg(tl, args.end))

    print(f"Sequence: {text(tl.seq, 'name', '?')}  {tl.width}x{tl.height} @ {tl.fps:.3f} fps, "
          f"{len(tl.video_tracks)} video tracks. Picks go on V{args.track}.")
    if not args.movie:
        print("No --movie given: previews are rebuilt from V1/A1 source media (or black if not found).\n"
              "For accurate previews export the sequence (any small H.264) and pass it with --movie.")

    min_len = int(round(0.25 * tl.fps))

    def scan():
        """Slots worth a decision: not a sliver, and (unless --include-single) more than one image."""
        return [sl for sl in tl.find_slots(window)
                if sl["end"] - sl["start"] >= min_len and (args.include_single or len(tl._paths(sl)) > 1)]

    slots = scan()
    if not slots:
        die("No .jpg choices found on the timeline" + (" in that window" if window else "")
            + ("." if args.include_single else ". (Use --include-single to also see lone images.)"))

    with tempfile.TemporaryDirectory(prefix="pick_image_") as work:
        pv = Previewer(tl, args.movie, args.movie_offset, args.preroll, args.postroll, work)
        sess = Session(tl, pv, args)
        if sess.cast:
            print(f"Cast labels loaded for {len(sess.cast)} stills.")
        print(f"{len(slots)} slot(s) to decide" + ("." if args.include_single else
              " (places with 2+ different .jpg images; --include-single adds lone images)."))
        changed, i = 0, 0
        while 0 <= i < len(slots):
            res = sess.run_slot(i + 1, len(slots), slots[i])
            if res == "done":
                changed += 1
                tl.write(out_path)
                print(f"  Saved -> {out_path}")
                done_start = slots[i]["start"]
                slots = scan()                    # the edit changes the timeline; re-scan
                i = next((k for k, sl in enumerate(slots) if sl["start"] > done_start), len(slots))
            elif res == "quit":
                break
            elif res == "back":
                i = max(0, i - 1)
            elif isinstance(res, tuple):
                i = min(max(0, res[1]), len(slots) - 1)
            else:
                i += 1

    if changed:
        print(f"\nDone: {changed} pick(s). Import into Premiere: File > Import > {out_path}")
    else:
        print("\nNo changes made.")


if __name__ == "__main__":
    main()
