#!/usr/bin/env python3
"""
pick_gui.py - visual version of pick_image.py. Opens in your web browser.

  python3 pick_gui.py timeline.xml --movie reference.mp4 --folder ~/Downloads/EvesBayou_stills

- Left: every place on the timeline where 2+ different .jpg images compete.
  Places you've decided stay in the list with a check mark and the image you chose.
- Middle: the movie (with sound) playing around that spot, with the image you
  clicked cut in at the right moment and for the duration you set.
- Right: the candidates, plus your folder. Drag in any other .jpg to add it.
- "+ Insert an image" puts an image anywhere, even where there was none.

Every pick saves <input>_picked.xml (the original XML is never changed) and a
<input>_picked.picks.json log. Run it again on the same XML and it resumes
where you left off, check marks included.

Runs entirely on your computer (http://127.0.0.1). Needs only Python 3;
ffmpeg makes thumbnails faster but isn't required on a Mac.
"""

import argparse
import copy
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pick_image as core  # noqa: E402

MIN_SLOT_SECONDS = 0.25
NEW_PLACE_SECONDS = 3.0
ADDED_DIR = "Picker added images"


class App:
    def __init__(self, args):
        self.args = args
        self.out = args.out or os.path.splitext(args.xml)[0] + "_picked.xml"
        if os.path.abspath(self.out) == os.path.abspath(args.xml):
            core.die("--out must be different from the input XML.")
        self.log_path = os.path.splitext(self.out)[0] + ".picks.json"
        self.added_dir = os.path.join(os.path.dirname(os.path.abspath(self.out)), ADDED_DIR)

        self.log = []                             # decided places: start/end/path/track/candidates
        source = args.xml
        if os.path.exists(self.out) and not args.fresh:
            source = self.out
            try:
                with open(self.log_path, encoding="utf-8") as fh:
                    self.log = json.load(fh)
            except (OSError, ValueError):
                self.log = []
            print(f"Resuming from {os.path.basename(self.out)} ({len(self.log)} decided place(s) so far). "
                  f"Use --fresh to start over from the original.")
        self.tl = core.Timeline(source, args.track)

        self.folder = core.list_folder(args.folder)
        self.added = core.list_folder(self.added_dir) if os.path.isdir(self.added_dir) else []
        self.cast = core.load_cast(args.folder)
        self.reg, self.rev = {}, {}               # image id <-> path (only these are served)
        self.undo = []                            # (xml, log) before each pick
        self.lock = threading.RLock()
        self.thumbdir = tempfile.mkdtemp(prefix="pick_gui_thumbs_")
        self.rescan()

    # ---- data ------------------------------------------------------------ #
    @property
    def pool(self):
        """Images you can pick from outside the timeline: the folder plus anything you added."""
        return self.folder + [p for p in self.added if p not in self.folder]

    def img_id(self, path):
        if path not in self.rev:
            i = str(len(self.reg))
            self.reg[i], self.rev[path] = path, i
        return self.rev[path]

    def who(self, path):
        return self.cast.get(os.path.basename(path or "").lower(), [])

    def image_json(self, path, **extra):
        d = {"id": self.img_id(path), "name": os.path.basename(path or "?"),
             "path": path, "cast": self.who(path), "missing": not (path and os.path.exists(path))}
        d.update(extra)
        return d

    def rescan(self):
        tl, min_len = self.tl, int(round(MIN_SLOT_SECONDS * self.tl.fps))
        decided = {e["start"] for e in self.log}
        slots = [sl for sl in tl.find_slots()
                 if sl["end"] - sl["start"] >= min_len and sl["start"] not in decided
                 and (self.args.include_single or len(tl._paths(sl)) > 1)]
        places = [{"kind": "open", "start": sl["start"], "end": sl["end"], "slot": sl} for sl in slots]
        places += [{"kind": "done", "start": e["start"], "end": e["end"], "log": j} for j, e in enumerate(self.log)]
        places.sort(key=lambda p: (p["start"], p["kind"] != "done"))
        self.places = places

    def done_slot(self, entry):
        """The decided image as it sits on its track now, shaped like a slot so it can be re-picked."""
        ti = entry["track"] - 1
        track = self.tl.video_tracks[ti]
        cands = [{"clip": c, "track": ti, "start": st, "end": en}
                 for c, st, en in self.tl.resolve(track)
                 if st < entry["end"] and en > entry["start"] and self.tl.clip_path(c) == entry["path"]]
        return {"start": entry["start"], "end": entry["end"], "candidates": cands}

    def options(self, place):
        """What you can choose at a place: [{'kind': 'timeline', clip...} | {'kind': 'path', path}]."""
        if place["kind"] == "open":
            opts, seen = [], set()
            for c in place["slot"]["candidates"]:     # top track first, one entry per image file
                p = self.tl.clip_path(c["clip"])
                if p in seen:
                    continue
                seen.add(p)
                opts.append({"kind": "timeline", "clip": c["clip"], "path": p, "track": c["track"] + 1,
                             "disabled": core.text(c["clip"], "enabled", "TRUE") != "TRUE"})
            return opts
        if place["kind"] == "done":
            e = self.log[place["log"]]
            sl = self.done_slot(e)
            opts = [{"kind": "timeline", "clip": c["clip"], "path": e["path"], "track": c["track"] + 1,
                     "current": True} for c in sl["candidates"][:1]]
            opts += [{"kind": "path", "path": p} for p in e.get("candidates", []) if p != e["path"]]
            return opts
        return []

    def state(self):
        tl = self.tl
        places = []
        for i, pl in enumerate(self.places):
            d = {"i": i, "kind": pl["kind"], "start": pl["start"], "end": pl["end"],
                 "tc": tl.tc(pl["start"]), "secs": round(tl.secs(pl["end"] - pl["start"]), 2),
                 "candidates": [self.image_json(o["path"], ref=k, track=o.get("track"),
                                                disabled=o.get("disabled", False), current=o.get("current", False))
                                for k, o in enumerate(self.options(pl))]}
            if pl["kind"] == "done":
                e = self.log[pl["log"]]
                d["chosen"] = self.image_json(e["path"])
                d["track"] = e["track"]
                d["inserted"] = not e.get("candidates")
            places.append(d)
        return {
            "sequence": core.text(tl.seq, "name", "?"), "fps": tl.fps, "timebase": tl.timebase,
            "tracks": len(tl.video_tracks), "target": tl.target + 1,
            "movie": bool(self.args.movie), "offset": self.args.movie_offset,
            "preroll": self.args.preroll, "postroll": self.args.postroll,
            "outName": os.path.basename(self.out), "addedDir": self.added_dir,
            "places": places, "decided": len(self.log), "canUndo": bool(self.undo),
            "folder": [self.image_json(p, ref=k, added=p in self.added and p not in self.folder)
                       for k, p in enumerate(self.pool)],
            "folderName": os.path.basename(os.path.normpath(self.args.folder)) if self.args.folder else "",
            "castCount": len(self.cast),
        }

    # ---- choosing --------------------------------------------------------- #
    def resolve(self, body):
        """-> (place, slot, choice). body has either place=<index> or at=<time> for a new insert."""
        tl = self.tl
        if body.get("at") is not None:
            start = tl.parse_duration(str(body["at"]))
            place = {"kind": "new", "start": start, "end": start + int(round(NEW_PLACE_SECONDS * tl.fps))}
            slot = {"start": place["start"], "end": place["end"], "candidates": []}
        else:
            place = self.places[int(body["place"])]
            slot = place["slot"] if place["kind"] == "open" else self.done_slot(self.log[place["log"]])
        ref = int(body["ref"])
        if body["kind"] == "folder":
            p = self.pool[ref]
            return place, slot, {"kind": "folder", "path": p, "label": os.path.basename(p)}
        o = self.options(place)[ref]
        if o["kind"] == "timeline":
            return place, slot, {"kind": "timeline", "clip": o["clip"], "path": o["path"],
                                 "label": os.path.basename(o["path"])}
        return place, slot, {"kind": "folder", "path": o["path"], "label": os.path.basename(o["path"])}

    def check(self, body):
        tl = self.tl
        try:
            place, slot, choice = self.resolve(body)
        except ValueError:
            return {"ok": False, "error": "Couldn't read that time. Use seconds (754.5), frames (18090f) "
                                          "or timecode (00:12:34:12)."}
        cur = slot["end"] - slot["start"]
        txt = str(body.get("duration", "")).strip()
        try:
            dur = tl.parse_duration(txt) if txt else cur
            if dur <= 0:
                raise ValueError
        except ValueError:
            return {"ok": False, "error": "Use seconds (4 or 4.5), frames (96f) or timecode (00:00:04:00)."}
        s = slot["start"]
        ignore = {c["clip"] for c in slot["candidates"]}
        return {"ok": True, "start": s, "startSecs": tl.secs(s), "frames": dur, "secs": tl.secs(dur),
                "range": f"{tl.tc(s)} → {tl.tc(s + dur)}", "track": f"V{tl.target + 1}",
                "longer": max(0, dur - cur) if place["kind"] != "new" else 0,
                "cut": tl.would_cut(s, s + dur, ignore), "cover": tl.covering(s, s + dur, ignore)}

    def commit(self, body):
        with self.lock:
            chk = self.check(body)
            if not chk["ok"]:
                return chk
            place, slot, choice = self.resolve(body)
            offered = [o["path"] for o in self.options(place)]     # before the edit removes them
            if not choice["path"] or not os.path.exists(choice["path"]):
                return {"ok": False, "error": "That image file isn't on this computer."}
            self.undo.append((core.ET.tostring(self.tl.root, encoding="unicode"), copy.deepcopy(self.log)))
            del self.undo[:-10]
            self.tl.commit(slot, choice, chk["frames"], remove_others=not self.args.keep_others,
                           scale_mode=self.args.scale)
            self.tl.write(self.out)

            s = slot["start"]
            entry = {"start": s, "end": s + chk["frames"], "tc": self.tl.tc(s), "path": choice["path"],
                     "name": choice["label"], "track": self.tl.target + 1}
            if place["kind"] == "done":
                old = self.log[place["log"]]
                entry["candidates"] = old.get("candidates", [])
                self.log[place["log"]] = entry
            else:
                entry["candidates"] = offered
                self.log.append(entry)
            self.save_log()
            self.rescan()
            nxt = next((k for k, pl in enumerate(self.places) if pl["kind"] == "open" and pl["start"] > s),
                       next((k for k, pl in enumerate(self.places) if pl["start"] == s), 0))
            return {"ok": True, "next": nxt, "state": self.state()}

    def save_log(self):
        with open(self.log_path, "w", encoding="utf-8") as fh:
            json.dump(self.log, fh, indent=1, ensure_ascii=False)

    def do_undo(self):
        with self.lock:
            if not self.undo:
                return {"ok": False, "error": "Nothing to undo."}
            xml, log = self.undo.pop()
            tmp = os.path.join(self.thumbdir, "undo.xml")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(xml)
            self.tl = core.Timeline(tmp, self.tl.target + 1)
            self.tl.write(self.out)
            self.log = log
            self.save_log()
            self.rescan()
            return {"ok": True, "state": self.state()}

    def set_track(self, body):
        with self.lock:
            n = int(body["track"])
            if 1 <= n <= len(self.tl.video_tracks):
                self.tl.target = n - 1
            return {"ok": True, "state": self.state()}

    def upload(self, name, data):
        name = os.path.basename(name or "image.jpg")
        if os.path.splitext(name)[1].lower() not in core.IMAGE_EXTS or not data.startswith(b"\xff\xd8"):
            return {"ok": False, "error": f"{name}: only .jpg images can be added."}
        with self.lock:
            os.makedirs(self.added_dir, exist_ok=True)
            base, ext = os.path.splitext(name)
            dest, n = os.path.join(self.added_dir, name), 1
            while os.path.exists(dest):
                with open(dest, "rb") as fh:
                    if fh.read() == data:          # same file added twice: reuse it
                        break
                n += 1
                dest = os.path.join(self.added_dir, f"{base} ({n}){ext}")
            else:
                with open(dest, "wb") as fh:
                    fh.write(data)
            if dest not in self.added:
                self.added.append(dest)
            return {"ok": True, "ref": self.pool.index(dest), "state": self.state()}

    def thumb(self, iid):
        path = self.reg.get(iid)
        if not path or not os.path.exists(path):
            return None
        out = os.path.join(self.thumbdir, f"{iid}.jpg")
        if not os.path.exists(out):
            r = core.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", "scale=480:-2", "-frames:v", "1",
                          "-q:v", "4", out])
            if r.returncode != 0:
                r = core.run(["sips", "-Z", "480", "-s", "format", "jpeg", path, "--out", out])
            if r.returncode != 0 or not os.path.exists(out):
                return path
        return out


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_json(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_file(self, path, ctype=None):
            size = os.path.getsize(path)
            ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                else:
                    start = size - int(b)
                end = min(end, size - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = fh.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path == "/":
                data = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/state":
                with app.lock:
                    self.send_json(app.state())
            elif path == "/movie" and app.args.movie:
                self.send_file(app.args.movie)
            elif path.startswith("/img/"):
                p = app.reg.get(path[5:])
                self.send_file(p) if p and os.path.exists(p) else self.send_error(404)
            elif path.startswith("/thumb/"):
                p = app.thumb(path[7:])
                self.send_file(p, "image/jpeg") if p else self.send_error(404)
            else:
                self.send_error(404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            path = urlparse(self.path).path
            try:
                if path == "/api/upload":
                    self.send_json(app.upload(unquote(self.headers.get("X-Filename", "")), raw))
                    return
                body = json.loads(raw or b"{}")
                if path == "/api/check":
                    with app.lock:
                        self.send_json(app.check(body))
                elif path == "/api/commit":
                    self.send_json(app.commit(body))
                elif path == "/api/undo":
                    self.send_json(app.do_undo())
                elif path == "/api/track":
                    self.send_json(app.set_track(body))
                else:
                    self.send_error(404)
            except (KeyError, IndexError, ValueError) as e:
                self.send_json({"ok": False, "error": f"Bad request: {e}"}, 400)
    return H


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Image Picker</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#141414;--panel:#1d1d1d;--panel2:#252525;--line:#333;--ink:#ececec;--mute:#9a9a9a;
      --accent:#e0a33a;--accent-ink:#1a1306;--ok:#5fb37a;--warn:#e07a5f}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
button,input,select{font:inherit;color:inherit}
button{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:7px 12px;cursor:pointer}
button:hover{border-color:#555}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
button:disabled{opacity:.4;cursor:default}
input,select{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
.app{display:grid;grid-template-columns:280px 1fr;grid-template-rows:auto 1fr;height:100vh}
header{grid-column:1/3;display:flex;gap:14px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
header h1{font-size:15px;margin:0;font-weight:600}
header .seq{color:var(--mute);font-size:13px;max-width:420px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
header .sp{flex:1}
.saved{color:var(--mute);font-size:12px}
.saved b{color:var(--ok);font-weight:500}
aside{border-right:1px solid var(--line);overflow:auto;background:var(--panel)}
aside .head{padding:10px 12px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel);z-index:1}
aside .head .count{color:var(--mute);font-size:12px;margin-bottom:6px}
aside .head .prog{height:4px;background:var(--panel2);border-radius:2px;overflow:hidden;margin-bottom:8px}
aside .head .prog div{height:100%;background:var(--ok)}
aside .head .row{display:flex;gap:6px;align-items:center}
aside .head .row button{flex:1;padding:6px 8px;font-size:12px}
aside .head label{color:var(--mute);font-size:12px;display:flex;gap:6px;align-items:center;margin-top:8px;cursor:pointer}
.slot{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid #262626;cursor:pointer}
.slot:hover{background:#232323}
.slot.on{background:#2e2717;box-shadow:inset 3px 0 var(--accent)}
.slot .n{color:var(--mute);width:28px;font-variant-numeric:tabular-nums;font-size:12px}
.slot .tc{font-variant-numeric:tabular-nums;font-size:12px}
.slot .th{display:flex;gap:3px;margin-left:auto}
.slot .th img{width:38px;height:22px;object-fit:cover;border-radius:2px;background:#000}
.slot.done .n{color:var(--ok);font-weight:700}
.slot.done .tc{color:var(--mute)}
.slot.done .who{color:var(--ok);font-size:11px;max-width:120px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.slot.done .th img{width:48px;height:27px;outline:2px solid var(--ok);outline-offset:-2px}
.slot.newrow{color:var(--accent);font-size:13px}
main{overflow:auto;padding:14px 18px 40px;display:grid;grid-template-columns:minmax(420px,1.15fr) minmax(320px,1fr);gap:22px;align-items:start}
main>.empty{grid-column:1/-1}
.left{position:sticky;top:0}
.right h2:first-child{margin-top:4px}
.status{display:inline-block;font-size:12px;padding:2px 8px;border-radius:10px;margin-bottom:8px}
.status.done{background:#1d3324;color:var(--ok)}
.status.open{background:#3a2e14;color:var(--accent)}
.status.new{background:#24303d;color:#8cb8e6}
.stage{position:relative;width:100%;aspect-ratio:16/9;background:#000;border-radius:8px;overflow:hidden}
.stage video,.stage .over{position:absolute;inset:0;width:100%;height:100%}
.stage .over{object-fit:contain;background:#000;visibility:hidden}
.stage .nomovie{position:absolute;inset:0;display:flex;align-items:flex-end;justify-content:center;padding:12px;color:var(--mute);font-size:12px;pointer-events:none}
.badge{position:absolute;top:10px;left:10px;background:rgba(0,0,0,.65);padding:3px 8px;border-radius:4px;font-size:12px;visibility:hidden}
.transport{display:flex;gap:10px;align-items:center;margin:10px 0 4px;flex-wrap:wrap}
.transport .time{font-variant-numeric:tabular-nums;color:var(--mute);font-size:13px}
.bar{position:relative;flex:1;min-width:200px;height:8px;background:var(--panel2);border-radius:4px;cursor:pointer}
.bar .img{position:absolute;top:0;bottom:0;background:#6b5420;border-radius:4px}
.bar .ph{position:absolute;top:-3px;width:2px;height:14px;background:var(--ink)}
.atrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0 4px}
.atrow input{width:140px;font-variant-numeric:tabular-nums}
h2{font-size:13px;font-weight:600;color:var(--mute);text-transform:uppercase;letter-spacing:.04em;margin:18px 0 8px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.card{background:var(--panel);border:2px solid transparent;border-radius:8px;overflow:hidden;cursor:pointer;position:relative}
.card:hover{border-color:#444}
.card.on{border-color:var(--accent)}
.card img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#000}
.card .meta{padding:6px 8px;font-size:12px}
.card .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .cast{color:var(--accent);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .k{position:absolute;top:6px;left:6px;background:rgba(0,0,0,.7);border-radius:4px;padding:1px 6px;font-size:12px}
.card .tr{position:absolute;top:6px;right:6px;background:rgba(0,0,0,.7);border-radius:4px;padding:1px 6px;font-size:11px;color:var(--mute)}
.card .tr.cur{color:var(--ok)}
.card .miss{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--warn);font-size:12px;aspect-ratio:16/9}
.folderbar{display:flex;gap:10px;align-items:center;margin-bottom:8px;flex-wrap:wrap}
.folderbar input{flex:1;min-width:160px}
.fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.fgrid .card .meta{padding:4px 6px;font-size:11px}
.drop{border:2px dashed var(--line);border-radius:8px;padding:10px;text-align:center;color:var(--mute);font-size:12px;margin-bottom:10px}
.drop.over{border-color:var(--accent);color:var(--accent)}
.decide{margin-top:14px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.decide .sel{display:flex;gap:10px;align-items:center;min-width:220px;flex:1}
.decide .sel img{width:96px;aspect-ratio:16/9;object-fit:cover;border-radius:4px;background:#000}
.decide label{color:var(--mute);font-size:12px;display:block}
.decide input.dur{width:110px}
.warn{color:var(--warn);font-size:12px}
.note{color:var(--mute);font-size:12px}
.empty{padding:60px 20px;text-align:center;color:var(--mute)}
.keys{color:var(--mute);font-size:11px;margin-top:10px}
.keys kbd{background:var(--panel2);border:1px solid var(--line);border-radius:3px;padding:0 4px;font:inherit}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--ok);color:#06150b;padding:8px 14px;border-radius:6px;font-weight:600;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.show{opacity:1}
.toast.bad{background:var(--warn)}
@media (max-width:1000px){main{grid-template-columns:1fr}.left{position:static}}
@media (max-width:800px){.app{grid-template-columns:1fr;grid-template-rows:auto 220px 1fr}header{grid-column:1}aside{border-right:0;border-bottom:1px solid var(--line)}}
</style></head><body>
<div class="app">
  <header>
    <h1>Image Picker</h1><span class="seq" id="seq"></span><span class="sp"></span>
    <label class="note">Picks go on <select id="track"></select></label>
    <button id="undo" disabled>Undo last pick</button>
    <span class="saved" id="saved"></span>
  </header>
  <aside>
    <div class="head">
      <div class="count" id="count"></div>
      <div class="prog"><div id="progbar"></div></div>
      <div class="row"><button id="nextOpen">Next undecided ↓</button><button id="newBtn">＋ Insert an image</button></div>
      <label><input type="checkbox" id="hideDone"> Hide decided places</label>
    </div>
    <div id="slots"></div>
  </aside>
  <main id="main"></main>
</div>
<div class="toast" id="toast"></div>
<input type="file" id="filepick" accept=".jpg,.jpeg,image/jpeg" multiple hidden>
<script>
// cur: index into S.places, or "new" while inserting an image at a new place
let S=null, cur=0, sel=null, chk=null, loop=false, query="", hideDone=false, newAt="";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function api(path,body){const r=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});return r.json();}
function toast(t,bad){const e=$("#toast");e.textContent=t;e.classList.toggle("bad",!!bad);e.classList.add("show");setTimeout(()=>e.classList.remove("show"),1800);}
const secs=f=>f/S.fps;
function fmt(t){const neg=t<0;t=Math.abs(t);const m=Math.floor(t/60),s=(t%60).toFixed(1);return `${neg?"-":""}${m}:${s.padStart(4,"0")}`;}
function tcOf(sec){const tb=S.timebase,f=Math.round(sec*S.fps),h=Math.floor(f/(tb*3600)),m=Math.floor(f/(tb*60))%60,s=Math.floor(f/tb)%60,fr=f%tb;
  return [h,m,s,fr].map(x=>String(x).padStart(2,"0")).join(":");}
const isNew=()=>cur==="new";
const place=()=>isNew()?null:S.places[cur];
const firstOpen=(from=0)=>{for(let k=Math.max(0,from);k<S.places.length;k++)if(S.places[k].kind=="open")return k;
  for(let k=0;k<S.places.length;k++)if(S.places[k].kind=="open")return k;return -1;};

async function load(){S=await api("/api/state");const f=firstOpen();cur=f>=0?f:(S.places.length?0:"new");refresh();}
function refresh(){renderHeader();renderSlots();renderMain();}
function renderHeader(){
  $("#seq").textContent=S.sequence;
  $("#track").innerHTML=Array.from({length:S.tracks},(_,i)=>`<option value="${i+1}" ${i+1==S.target?"selected":""}>V${i+1}</option>`).join("");
  $("#undo").disabled=!S.canUndo;
  $("#saved").innerHTML=S.decided?`<b>${S.decided} decided</b> · saved to ${esc(S.outName)}`:`Saves to ${esc(S.outName)}`;
  const total=S.places.length, open=S.places.filter(p=>p.kind=="open").length;
  $("#count").textContent=`${total-open} of ${total} places decided${open?` · ${open} to go`:" · all done"}`;
  $("#progbar").style.width=(total?(total-open)/total*100:0)+"%";
}
function renderSlots(){
  const rows=[];
  if(isNew())rows.push(`<div class="slot newrow on">＋ New image at ${esc(newAt||"…")}</div>`);
  S.places.forEach(p=>{
    if(hideDone&&p.kind=="done"&&p.i!==cur)return;
    const on=p.i===cur?"on":"";
    if(p.kind=="done"){
      rows.push(`<div class="slot done ${on}" data-i="${p.i}" title="Decided: ${esc(p.chosen.name)}">
        <span class="n">✓</span><span><div class="tc">${p.tc}</div><div class="who">${p.inserted?"＋ ":""}${esc(p.chosen.name)}</div></span>
        <span class="th"><img loading="lazy" src="/thumb/${p.chosen.id}"></span></div>`);
    }else{
      rows.push(`<div class="slot ${on}" data-i="${p.i}">
        <span class="n">${p.i+1}</span><span><div class="tc">${p.tc}</div><div class="note">${p.secs}s</div></span>
        <span class="th">${p.candidates.slice(0,3).map(c=>`<img loading="lazy" src="/thumb/${c.id}">`).join("")}</span></div>`);
    }
  });
  $("#slots").innerHTML=rows.join("");
  document.querySelectorAll(".slot[data-i]").forEach(e=>e.onclick=()=>go(+e.dataset.i));
  const on=$(".slot.on"); if(on) on.scrollIntoView({block:"nearest"});
}
function card(c,kind,key){
  const castTxt=c.cast.length?`<div class="cast">${esc(c.cast.join(", "))}</div>`:"";
  const on=sel&&sel.kind==kind&&sel.ref==c.ref?"on":"";
  const tag=c.current?`<span class="tr cur">✓ current</span>`:c.track?`<span class="tr">V${c.track}${c.disabled?" · off":""}</span>`:c.added?`<span class="tr">added</span>`:"";
  return `<div class="card ${on}" data-kind="${kind}" data-ref="${c.ref}" title="${esc(c.path)}">
    ${key?`<span class="k">${key}</span>`:""}${tag}
    ${c.missing?`<div class="miss">file not found</div>`:`<img loading="lazy" src="/thumb/${c.id}">`}
    <div class="meta"><div class="nm">${esc(c.name)}</div>${castTxt}</div></div>`;
}
function folderMatches(){
  const q=query.trim().toLowerCase();
  return S.folder.filter(f=>!q||f.name.toLowerCase().includes(q)||f.cast.join(" ").toLowerCase().includes(q));
}
function renderMain(){
  const m=$("#main");
  if(!isNew()&&!S.places.length){m.innerHTML=`<div class="empty">No places with 2+ different .jpg images were found.<br><br><button id="emptyNew">＋ Insert an image</button></div>`;$("#emptyNew").onclick=startNew;return;}
  if(!isNew())cur=Math.min(cur,S.places.length-1);
  const p=place();
  let status;
  if(isNew())status=`<span class="status new">New: insert an image where there was none</span>`;
  else if(p.kind=="done")status=`<span class="status done">✓ Decided: ${esc(p.chosen.name)} on V${p.track}</span> <span class="note">Choose another image to change it.</span>`;
  else status=`<span class="status open">To decide · place ${p.i+1} of ${S.places.length}</span>`;
  const info=isNew()?"":`<div class="note">${p.tc} · ${p.secs}s on the timeline · plays ${S.preroll}s before and ${S.postroll}s after</div>`;
  const atRow=isNew()?`<div class="atrow"><label class="note" for="at">Image starts at</label>
      <input id="at" value="${esc(newAt)}" placeholder="00:00:00:00"><button id="usePh">Use playhead</button>
      <span class="note">Scrub the movie, then “Use playhead”. Or type a timecode.</span></div>`:"";
  const cands=isNew()?"":`<h2>${p.kind=="done"?"Options at this place":"On the timeline"}</h2>
    <div class="cards" id="cands">${p.candidates.length?p.candidates.map((c,k)=>card(c,"cand",k+1)).join(""):`<div class="note">No other options were recorded here. Use the images below.</div>`}</div>`;
  m.innerHTML=`
  <div class="left">
  ${status}
  <div class="stage" id="stage">
    ${S.movie?`<video id="vid" src="/movie" preload="auto" playsinline ${isNew()?"controls":""}></video>`:""}
    <img class="over" id="over">
    <span class="badge" id="badge"></span>
    ${S.movie?"":`<div class="nomovie">No reference movie loaded, so you see the image only. Start with --movie to watch and hear it in context.</div>`}
  </div>
  ${atRow}
  <div class="transport">
    <button class="primary" id="play">▶ Play in context</button>
    <button id="loopBtn">Loop: ${loop?"on":"off"}</button>
    <div class="bar" id="bar"><div class="img" id="barImg"></div><div class="ph" id="ph"></div></div>
    <span class="time" id="time"></span>
  </div>
  ${info}
  <div class="decide" id="decide"></div>
  <div class="keys"><kbd>Space</kbd> play/pause · <kbd>1</kbd>–<kbd>9</kbd> pick an option · <kbd>Enter</kbd> use selected · <kbd>↑</kbd>/<kbd>↓</kbd> previous/next place · <kbd>N</kbd> next undecided · <kbd>/</kbd> search</div>
  </div>
  <div class="right">
  ${cands}
  <h2>${S.folderName?`Replace from folder ${esc(S.folderName)}`:"Other images"}</h2>
  <div class="drop" id="drop">Drag .jpg images here to add ones that aren't in the list · <button id="addBtn">Add image file…</button></div>
  <div class="folderbar"><input id="q" placeholder="Search by file name${S.castCount?" or character (e.g. Mozelle)":""}" value="${esc(query)}"><span class="note" id="fcount"></span></div>
  <div class="fgrid" id="fgrid"></div>
  </div>`;
  document.querySelectorAll("#cands .card").forEach(bindCard);
  $("#q").oninput=e=>{query=e.target.value;renderFolder();};renderFolder();
  bindDrop();
  $("#play").onclick=playFromStart;
  $("#loopBtn").onclick=()=>{loop=!loop;$("#loopBtn").textContent="Loop: "+(loop?"on":"off");};
  $("#bar").onclick=e=>{const v=$("#vid");if(!v||!chk)return;const r=e.currentTarget.getBoundingClientRect();const w=win();v.currentTime=w.a+(w.b-w.a)*(e.clientX-r.left)/r.width;};
  if(isNew()){
    let t=null;
    $("#at").oninput=e=>{newAt=e.target.value;clearTimeout(t);t=setTimeout(()=>{renderSlots();if(sel)runCheck();},300);};
    $("#usePh").onclick=()=>{const v=$("#vid");if(!v)return;newAt=tcOf(Math.max(0,v.currentTime-S.offset));$("#at").value=newAt;renderSlots();if(sel)runCheck();};
  }
  if(sel&&sel.place===cur){renderDecide();runCheck();}
  else if(!isNew()&&p.candidates.length)select("cand",0,false);
  else{sel=null;chk=null;renderDecide();}
}
function renderFolder(){
  const list=folderMatches();
  $("#fcount").textContent=`${list.length} of ${S.folder.length}`;
  $("#fgrid").innerHTML=list.map(c=>card(c,"folder",null)).join("")||`<div class="note">No images yet. Drag .jpg files into the box above.</div>`;
  document.querySelectorAll("#fgrid .card").forEach(bindCard);
}
function bindCard(e){e.onclick=()=>select(e.dataset.kind,+e.dataset.ref,true);}
function bindDrop(){
  const d=$("#drop");
  $("#addBtn").onclick=()=>$("#filepick").click();
  d.ondragover=e=>{e.preventDefault();d.classList.add("over");};
  d.ondragleave=()=>d.classList.remove("over");
  d.ondrop=e=>{e.preventDefault();d.classList.remove("over");upload(e.dataTransfer.files);};
}
async function upload(files){
  let last=null;
  for(const f of files){
    const r=await fetch("/api/upload",{method:"POST",headers:{"X-Filename":encodeURIComponent(f.name)},body:f}).then(r=>r.json());
    if(!r.ok){toast(r.error,true);continue;}
    S=r.state;last=r.ref;
  }
  if(last!==null){query="";const keep=sel;renderMain();sel=keep;select("folder",last,true);toast("Added to the list");}
}
function selImage(){if(!sel)return null;return sel.kind=="folder"?S.folder[sel.ref]:place().candidates[sel.ref];}
function select(kind,ref,play){
  if(kind=="cand"&&(isNew()||!place().candidates[ref]))return;
  if(kind=="folder"&&!S.folder[ref])return;
  sel={kind,ref,place:cur,dur:sel&&sel.place===cur?sel.dur:""};chk=null;
  document.querySelectorAll(".card").forEach(c=>c.classList.toggle("on",c.dataset.kind==kind&&+c.dataset.ref==ref));
  const img=selImage();
  $("#over").src=img.missing?"":`/img/${img.id}`;
  $("#badge").textContent=img.name;
  renderDecide();runCheck().then(()=>{if(play&&S.movie)playFromStart();});
}
function renderDecide(){
  const d=$("#decide");
  if(!sel){d.innerHTML=`<div class="note">${isNew()?"Set where the image starts, then click an image on the right.":"Click an image on the right."}</div>`;return;}
  const img=selImage(), p=place();
  const ph=isNew()?"3":p.secs;
  d.innerHTML=`
   <div class="sel">${img.missing?"":`<img src="/thumb/${img.id}">`}<div><div>${esc(img.name)}</div>
     <div class="note">${img.cast.length?esc(img.cast.join(", ")):(img.current?"current choice":img.track?"V"+img.track:"from folder")}</div></div></div>
   <div><label for="dur">On screen for (seconds, 96f, or timecode)</label>
     <input class="dur" id="dur" placeholder="${ph}" value="${esc(sel.dur)}"></div>
   <div class="note" id="range"></div>
   <button class="primary" id="use" ${img.missing?"disabled":""}>${p&&p.kind=="done"?"Change to this image ⏎":"Use this image ⏎"}</button>
   ${isNew()?`<button id="cancelNew">Cancel</button>`:`<button id="skip">${p.kind=="done"?"Next ↓":"Skip ↓"}</button>`}
   <div class="warn" id="warn" style="flex-basis:100%"></div>`;
  let t=null;
  $("#dur").oninput=e=>{sel.dur=e.target.value;clearTimeout(t);t=setTimeout(runCheck,250);};
  $("#dur").onkeydown=e=>{if(e.key=="Enter"){e.preventDefault();commit();}};
  $("#use").onclick=commit;
  if(isNew())$("#cancelNew").onclick=()=>{const f=firstOpen();go(f>=0?f:0);};
  else $("#skip").onclick=()=>go(cur+1);
}
function req(){const b={kind:sel.kind,ref:sel.ref,duration:sel.dur};if(isNew())b.at=newAt;else b.place=cur;return b;}
async function runCheck(){
  if(!sel)return;
  if(isNew()&&!newAt.trim()){chk=null;$("#range").textContent="";$("#warn").textContent="Set where the image starts (scrub and press “Use playhead”, or type a timecode).";return;}
  const r=await api("/api/check",req());
  chk=r.ok?r:null;
  if(!$("#range"))return;
  if(!r.ok){$("#range").textContent="";$("#warn").textContent=r.error;return;}
  $("#range").textContent=`${r.range} on ${r.track} (${r.secs.toFixed(2)}s)`;
  const w=[];
  if(r.longer)w.push(`Longer than this place on the timeline: it will also overwrite ${r.longer} frames after it.`);
  if(r.cut.length)w.push(`Will cut what's already on ${r.track} here: ${r.cut.slice(0,4).join(", ")}${r.cut.length>4?"…":""}`);
  if(r.cover.length)w.push(`Will be hidden under: ${r.cover.slice(0,4).join(", ")}${r.cover.length>4?"…":""}. Choose a higher track at the top of the page.`);
  $("#warn").innerHTML=w.map(esc).join("<br>");
  drawBar();
}
async function commit(){
  if(!sel)return;
  if(!chk)await runCheck();          // clicked before the check came back: finish it first
  if(!chk)return;
  const img=selImage(); if(img.missing)return;
  $("#use").disabled=true;$("#use").textContent="Saving…";
  const r=await api("/api/commit",req());
  if(!r.ok){$("#warn").textContent=r.error;$("#use").disabled=false;$("#use").textContent="Use this image ⏎";return;}
  toast(`Saved: ${img.name}`);
  S=r.state;cur=r.next;sel=null;newAt="";stop();refresh();
}
function go(i){if(!S.places.length)return;cur=Math.max(0,Math.min(S.places.length-1,i));sel=null;stop();renderSlots();renderMain();}
function startNew(){const v=$("#vid");const t=v?Math.max(0,v.currentTime-S.offset):0;cur="new";sel=null;chk=null;newAt=tcOf(t);stop();renderSlots();renderMain();}

// ---- playback: movie plays, the selected image is shown over it for its duration ----
function imgSpan(){
  if(!chk){const p=place();if(!p)return null;return {a:secs(p.start)+S.offset,b:secs(p.end)+S.offset};}
  return {a:chk.startSecs+S.offset,b:chk.startSecs+S.offset+chk.secs};
}
function win(){const sp=imgSpan();return {a:Math.max(0,sp.a-S.preroll),b:sp.b+S.postroll};}
function playFromStart(){const v=$("#vid");if(!v||!imgSpan())return;v.currentTime=win().a;v.play().catch(()=>{});}
function stop(){const v=$("#vid");if(v)v.pause();}
function drawBar(){
  const sp=imgSpan(),bi=$("#barImg");if(!sp||!bi)return;
  const w=win(),L=w.b-w.a;bi.style.left=((sp.a-w.a)/L*100)+"%";bi.style.width=((sp.b-sp.a)/L*100)+"%";
}
function tick(){
  if(S){
    const v=$("#vid"),over=$("#over"),badge=$("#badge"),sp=sel?imgSpan():null;
    if(over){
      if(!sp){over.style.visibility="hidden";if(badge)badge.style.visibility="hidden";}
      else if(!v){over.style.visibility="visible";}
      else{
        const t=v.currentTime,w=win(),inside=t>=sp.a&&t<sp.b;
        over.style.visibility=inside?"visible":"hidden";badge.style.visibility=inside?"visible":"hidden";
        if(!v.paused&&!isNew()&&t>=w.b){if(loop)v.currentTime=w.a;else v.pause();}
        if(!v.paused&&isNew()&&loop&&t>=w.b)v.currentTime=w.a;
        const ph=$("#ph");if(ph)ph.style.left=Math.min(100,Math.max(0,(t-w.a)/(w.b-w.a)*100))+"%";
        const tm=$("#time");if(tm)tm.textContent=`${fmt(t-sp.a)} from image start`;
      }
      const pb=$("#play");if(pb&&v)pb.textContent=v.paused?"▶ Play in context":"❚❚ Pause";
    }
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

document.addEventListener("keydown",e=>{
  if(e.target.tagName=="INPUT"){if(e.key=="Escape")e.target.blur();return;}
  if(e.target.tagName=="SELECT")return;
  const v=$("#vid");
  if(e.key==" "){e.preventDefault();if(!v)return;
    if(v.paused){const sp=imgSpan();if(sp){const w=win();if(v.currentTime<w.a||v.currentTime>=w.b)v.currentTime=w.a;}v.play();}else v.pause();}
  else if(/^[1-9]$/.test(e.key)){select("cand",+e.key-1,true);}
  else if(e.key=="Enter"){e.preventDefault();commit();}
  else if(e.key=="ArrowDown"||e.key=="ArrowRight"){e.preventDefault();go(isNew()?0:cur+1);}
  else if(e.key=="ArrowUp"||e.key=="ArrowLeft"){e.preventDefault();go(isNew()?0:cur-1);}
  else if(e.key=="n"||e.key=="N"){const f=firstOpen(isNew()?0:cur+1);if(f>=0)go(f);}
  else if(e.key=="/"){const q=$("#q");if(q){e.preventDefault();q.focus();}}
});
$("#undo").onclick=async()=>{const r=await api("/api/undo",{});if(r.ok){S=r.state;const f=firstOpen();cur=f>=0?f:0;sel=null;refresh();toast("Undone");}};
$("#track").onchange=async e=>{const r=await api("/api/track",{track:+e.target.value});S=r.state;renderHeader();if(sel)runCheck();};
$("#nextOpen").onclick=()=>{const f=firstOpen(isNew()?0:cur+1);if(f>=0)go(f);else toast("Everything is decided");};
$("#newBtn").onclick=startNew;
$("#hideDone").onchange=e=>{hideDone=e.target.checked;renderSlots();};
$("#filepick").onchange=e=>{upload(e.target.files);e.target.value="";};
load();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="Visual image picker for a Premiere XML timeline (opens in your browser).")
    ap.add_argument("xml", help="Premiere Pro / FCP7 XML exported from your sequence")
    ap.add_argument("--movie", help="Reference export of the sequence (H.264 .mp4) - what you see and hear in previews")
    ap.add_argument("--movie-offset", type=float, default=0.0,
                    help="Seconds into the sequence where the reference movie starts (if you exported a range)")
    ap.add_argument("--folder", help="Folder of replacement images (cast_labels.json in it adds character names)")
    ap.add_argument("--out", help="Output XML (default: <input>_picked.xml)")
    ap.add_argument("--fresh", action="store_true",
                    help="Start over from the original XML even if a _picked.xml from an earlier pass exists")
    ap.add_argument("--track", type=int, default=1, help="Video track picks are written to (changeable in the window)")
    ap.add_argument("--preroll", type=float, default=3.0, help="Seconds played before the image (default 3)")
    ap.add_argument("--postroll", type=float, default=3.0, help="Seconds played after the image (default 3)")
    ap.add_argument("--scale", choices=["fit", "fill", "none"], default="fit", help="Scaling for folder images")
    ap.add_argument("--include-single", action="store_true", help="Also list places with only one image")
    ap.add_argument("--keep-others", action="store_true", help="Don't remove the unchosen candidates")
    ap.add_argument("--port", type=int, default=7656)
    ap.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically")
    args = ap.parse_args()

    for label, p in (("XML", args.xml), ("Movie", args.movie), ("Folder", args.folder)):
        if p and not os.path.exists(p):
            core.die(f"{label} not found: {p}")
    if args.movie and os.path.splitext(args.movie)[1].lower() not in (".mp4", ".m4v", ".mov", ".webm"):
        print("Note: browsers play H.264 .mp4 best. If the movie doesn't play, re-export as H.264 .mp4.")

    print("Reading the timeline ...")
    app = App(args)
    open_n = sum(1 for p in app.places if p["kind"] == "open")
    print(f"{len(app.places)} places, {open_n} still to decide. Picks go to {app.out}")

    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
            break
        except OSError:
            continue
    if server is None:
        core.die("Couldn't find a free port.")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"\nOpen {url} in your browser (Chrome or Safari). Leave this window open while you work;"
          f"\npress Ctrl+C here when you're done.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(app.thumbdir, ignore_errors=True)
        if app.log:
            print(f"\n{len(app.log)} decided place(s) saved to {app.out}. Import it in Premiere: File > Import.")


if __name__ == "__main__":
    main()
