#!/usr/bin/env python3
"""
pick_gui.py - visual version of pick_image.py. Opens in your web browser.

  python3 pick_gui.py timeline.xml --movie reference.mp4 --folder ~/Downloads/EvesBayou_stills

- Left: every place on the timeline where 2+ different .jpg images compete.
- Middle: the movie (with sound) playing around that spot, with the image you
  clicked cut in at the right moment and for the duration you set.
- Click a candidate or a folder image to watch it in context, set how long it
  stays on screen, press "Use this image". A new XML (<input>_picked.xml) is
  saved after every pick; the original XML is never changed.

Runs entirely on your computer (http://127.0.0.1). Needs only Python 3;
ffmpeg makes thumbnails faster but isn't required on a Mac.
"""

import argparse
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


class App:
    def __init__(self, args):
        self.args = args
        self.xml = args.xml
        self.out = args.out or os.path.splitext(args.xml)[0] + "_picked.xml"
        if os.path.abspath(self.out) == os.path.abspath(self.xml):
            core.die("--out must be different from the input XML.")
        self.tl = core.Timeline(args.xml, args.track)
        self.folder = core.list_folder(args.folder)
        self.cast = core.load_cast(args.folder)
        self.reg, self.rev = {}, {}               # image id <-> path (only these are served)
        self.undo = []                            # serialized XML before each pick
        self.log = []                             # picks made this session
        self.lock = threading.RLock()
        self.thumbdir = tempfile.mkdtemp(prefix="pick_gui_thumbs_")
        self.rescan()

    # ---- data ------------------------------------------------------------ #
    def img_id(self, path):
        if path not in self.rev:
            i = str(len(self.reg))
            self.reg[i], self.rev[path] = path, i
        return self.rev[path]

    def who(self, path):
        return self.cast.get(os.path.basename(path or "").lower(), [])

    def rescan(self):
        tl, min_len = self.tl, int(round(MIN_SLOT_SECONDS * self.tl.fps))
        self.slots = [sl for sl in tl.find_slots()
                      if sl["end"] - sl["start"] >= min_len
                      and (self.args.include_single or len(tl._paths(sl)) > 1)]

    def options(self, slot):
        opts, seen = [], set()
        for c in slot["candidates"]:             # top track first, one entry per image file
            p = self.tl.clip_path(c["clip"])
            if p in seen:
                continue
            seen.add(p)
            opts.append({"kind": "timeline", "clip": c["clip"], "path": p, "track": c["track"] + 1,
                         "disabled": core.text(c["clip"], "enabled", "TRUE") != "TRUE"})
        return opts

    def image_json(self, path, **extra):
        d = {"id": self.img_id(path), "name": os.path.basename(path or "?"),
             "path": path, "cast": self.who(path), "missing": not (path and os.path.exists(path))}
        d.update(extra)
        return d

    def state(self):
        tl = self.tl
        slots = []
        for i, sl in enumerate(self.slots):
            slots.append({
                "i": i, "start": sl["start"], "end": sl["end"],
                "tc": tl.tc(sl["start"]), "secs": round(tl.secs(sl["end"] - sl["start"]), 2),
                "candidates": [self.image_json(o["path"], ref=k, track=o["track"], disabled=o["disabled"])
                               for k, o in enumerate(self.options(sl))],
            })
        return {
            "sequence": core.text(tl.seq, "name", "?"), "fps": tl.fps, "timebase": tl.timebase,
            "width": tl.width, "height": tl.height,
            "tracks": len(tl.video_tracks), "target": tl.target + 1,
            "movie": bool(self.args.movie), "movieName": os.path.basename(self.args.movie or ""),
            "offset": self.args.movie_offset, "preroll": self.args.preroll, "postroll": self.args.postroll,
            "out": self.out, "outName": os.path.basename(self.out),
            "slots": slots, "picks": self.log, "canUndo": bool(self.undo),
            "folder": [self.image_json(p, ref=k) for k, p in enumerate(self.folder)],
            "folderName": os.path.basename(os.path.normpath(self.args.folder)) if self.args.folder else "",
            "castCount": len(self.cast),
        }

    def resolve_choice(self, body):
        slot = self.slots[int(body["slot"])]
        ref = int(body["ref"])
        if body["kind"] == "folder":
            p = self.folder[ref]
            return slot, {"kind": "folder", "path": p, "label": os.path.basename(p)}
        o = self.options(slot)[ref]
        return slot, {"kind": "timeline", "clip": o["clip"], "path": o["path"], "label": os.path.basename(o["path"])}

    def check(self, body):
        tl = self.tl
        slot, choice = self.resolve_choice(body)
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
        return {"ok": True, "frames": dur, "secs": tl.secs(dur),
                "range": f"{tl.tc(s)} → {tl.tc(s + dur)}", "track": f"V{tl.target + 1}",
                "longer": max(0, dur - cur),
                "cut": tl.would_cut(s, s + dur, ignore), "cover": tl.covering(s, s + dur, ignore)}

    def commit(self, body):
        with self.lock:
            chk = self.check(body)
            if not chk["ok"]:
                return chk
            slot, choice = self.resolve_choice(body)
            self.undo.append(core.ET.tostring(self.tl.root, encoding="unicode"))
            del self.undo[:-10]
            self.tl.commit(slot, choice, chk["frames"], remove_others=not self.args.keep_others,
                           scale_mode=self.args.scale)
            self.tl.write(self.out)
            self.log.append({"tc": self.tl.tc(slot["start"]), "name": choice["label"],
                             "secs": round(chk["secs"], 2), "track": chk["track"]})
            done_start = slot["start"]
            self.rescan()
            nxt = next((k for k, sl in enumerate(self.slots) if sl["start"] > done_start), len(self.slots) - 1)
            return {"ok": True, "next": max(nxt, 0), "state": self.state()}

    def do_undo(self):
        with self.lock:
            if not self.undo:
                return {"ok": False, "error": "Nothing to undo."}
            xml = self.undo.pop()
            tmp = os.path.join(self.thumbdir, "undo.xml")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(xml)
            target = self.tl.target + 1
            self.tl = core.Timeline(tmp, target)
            self.tl.write(self.out)
            last = self.log.pop() if self.log else None
            self.rescan()
            back = 0
            if last:
                back = next((k for k, sl in enumerate(self.slots) if self.tl.tc(sl["start"]) == last["tc"]), 0)
            return {"ok": True, "next": back, "state": self.state()}

    def set_track(self, body):
        with self.lock:
            n = int(body["track"])
            if 1 <= n <= len(self.tl.video_tracks):
                self.tl.target = n - 1
            return {"ok": True, "state": self.state()}

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
            body = json.loads(self.rfile.read(n) or b"{}")
            path = urlparse(self.path).path
            try:
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
.app{display:grid;grid-template-columns:270px 1fr;grid-template-rows:auto 1fr;height:100vh}
header{grid-column:1/3;display:flex;gap:14px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
header h1{font-size:15px;margin:0;font-weight:600}
header .seq{color:var(--mute);font-size:13px;max-width:420px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
header .sp{flex:1}
.saved{color:var(--mute);font-size:12px}
.saved b{color:var(--ok);font-weight:500}
aside{border-right:1px solid var(--line);overflow:auto;background:var(--panel)}
aside .head{padding:10px 12px;color:var(--mute);font-size:12px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel)}
.slot{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid #262626;cursor:pointer}
.slot:hover{background:#232323}
.slot.on{background:#2e2717;box-shadow:inset 3px 0 var(--accent)}
.slot .n{color:var(--mute);width:28px;font-variant-numeric:tabular-nums;font-size:12px}
.slot .tc{font-variant-numeric:tabular-nums;font-size:12px}
.slot .th{display:flex;gap:3px;margin-left:auto}
.slot .th img{width:38px;height:22px;object-fit:cover;border-radius:2px;background:#000}
main{overflow:auto;padding:14px 18px 40px;display:grid;grid-template-columns:minmax(420px,1.15fr) minmax(320px,1fr);gap:22px;align-items:start}
main>.empty{grid-column:1/-1}
.left{position:sticky;top:0}
.right h2:first-child{margin-top:4px}
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
.card .miss{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--warn);font-size:12px;aspect-ratio:16/9}
.folderbar{display:flex;gap:10px;align-items:center;margin-bottom:8px}
.folderbar input{flex:1}
.fgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.fgrid .card .meta{padding:4px 6px;font-size:11px}
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
@media (max-width:1000px){main{grid-template-columns:1fr}.left{position:static}}
@media (max-width:800px){.app{grid-template-columns:1fr;grid-template-rows:auto 200px 1fr}header{grid-column:1}aside{border-right:0;border-bottom:1px solid var(--line)}}
</style></head><body>
<div class="app">
  <header>
    <h1>Image Picker</h1><span class="seq" id="seq"></span><span class="sp"></span>
    <label class="note">Picks go on <select id="track"></select></label>
    <button id="undo" disabled>Undo last pick</button>
    <span class="saved" id="saved"></span>
  </header>
  <aside><div class="head" id="slotHead"></div><div id="slots"></div></aside>
  <main id="main"></main>
</div>
<div class="toast" id="toast"></div>
<script>
let S=null, cur=0, sel=null, chk=null, interacted=false, loop=false, query="";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function api(path,body){const r=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});return r.json();}
function toast(t){const e=$("#toast");e.textContent=t;e.classList.add("show");setTimeout(()=>e.classList.remove("show"),1600);}
const secs=f=>f/S.fps;
function fmt(t){t=Math.max(0,t);const m=Math.floor(t/60),s=(t%60).toFixed(1);return `${m}:${s.padStart(4,"0")}`;}

async function load(){S=await api("/api/state");renderHeader();renderSlots();renderMain();}
function renderHeader(){
  $("#seq").textContent=S.sequence;
  $("#track").innerHTML=Array.from({length:S.tracks},(_,i)=>`<option value="${i+1}" ${i+1==S.target?"selected":""}>V${i+1}</option>`).join("");
  $("#undo").disabled=!S.canUndo;
  $("#saved").innerHTML=S.picks.length?`<b>${S.picks.length} pick${S.picks.length>1?"s":""}</b> saved to ${esc(S.outName)}`:`Saves to ${esc(S.outName)}`;
  $("#slotHead").textContent=`${S.slots.length} places to decide`;
}
function renderSlots(){
  $("#slots").innerHTML=S.slots.map(s=>`<div class="slot ${s.i==cur?"on":""}" data-i="${s.i}">
    <span class="n">${s.i+1}</span><span><div class="tc">${s.tc}</div><div class="note">${s.secs}s</div></span>
    <span class="th">${s.candidates.slice(0,3).map(c=>`<img loading="lazy" src="/thumb/${c.id}">`).join("")}</span></div>`).join("");
  document.querySelectorAll(".slot").forEach(e=>e.onclick=()=>go(+e.dataset.i));
  const on=$(".slot.on"); if(on) on.scrollIntoView({block:"nearest"});
}
function card(c,kind,key){
  const castTxt=c.cast.length?`<div class="cast">${esc(c.cast.join(", "))}</div>`:"";
  const on=sel&&sel.kind==kind&&sel.ref==c.ref?"on":"";
  return `<div class="card ${on}" data-kind="${kind}" data-ref="${c.ref}" title="${esc(c.path)}">
    ${key?`<span class="k">${key}</span>`:""}${c.track?`<span class="tr">V${c.track}${c.disabled?" · off":""}</span>`:""}
    ${c.missing?`<div class="miss">file not found</div>`:`<img loading="lazy" src="/thumb/${c.id}">`}
    <div class="meta"><div class="nm">${esc(c.name)}</div>${castTxt}</div></div>`;
}
function folderMatches(){
  const q=query.trim().toLowerCase();
  return S.folder.filter(f=>!q||f.name.toLowerCase().includes(q)||f.cast.join(" ").toLowerCase().includes(q));
}
function renderMain(){
  const m=$("#main");
  if(!S.slots.length){m.innerHTML=`<div class="empty">Nothing left to decide.<br>${S.picks.length?`Import <b>${esc(S.outName)}</b> into Premiere (File › Import).`:"No places with 2+ different .jpg images were found."}</div>`;return;}
  cur=Math.min(cur,S.slots.length-1);
  const s=S.slots[cur];
  m.innerHTML=`
  <div class="left">
  <div class="stage" id="stage">
    ${S.movie?`<video id="vid" src="/movie" preload="auto" playsinline></video>`:""}
    <img class="over" id="over">
    <span class="badge" id="badge"></span>
    ${S.movie?"":`<div class="nomovie">No reference movie loaded, so you see the image only. Start with --movie to watch and hear it in context.</div>`}
  </div>
  <div class="transport">
    <button class="primary" id="play">▶ Play in context</button>
    <button id="loopBtn">Loop: off</button>
    <div class="bar" id="bar"><div class="img" id="barImg"></div><div class="ph" id="ph"></div></div>
    <span class="time" id="time"></span>
  </div>
  <div class="note">Place ${cur+1} of ${S.slots.length} · ${s.tc} · ${s.secs}s on the timeline · plays ${S.preroll}s before and ${S.postroll}s after</div>
  <div class="decide" id="decide"></div>
  <div class="keys"><kbd>Space</kbd> play/pause · <kbd>1</kbd>–<kbd>9</kbd> pick a timeline image · <kbd>Enter</kbd> use selected · <kbd>↑</kbd>/<kbd>↓</kbd> previous/next place · <kbd>/</kbd> search folder</div>
  </div>
  <div class="right">
  <h2>On the timeline</h2>
  <div class="cards" id="cands">${s.candidates.map((c,k)=>card(c,"timeline",k+1)).join("")}</div>
  ${S.folder.length?`<h2>Replace from folder ${esc(S.folderName)}</h2>
  <div class="folderbar"><input id="q" placeholder="Search by file name${S.castCount?" or character (e.g. Mozelle)":""}" value="${esc(query)}"><span class="note" id="fcount"></span></div>
  <div class="fgrid" id="fgrid"></div>`:""}
  </div>`;
  document.querySelectorAll("#cands .card").forEach(bindCard);
  if(S.folder.length){$("#q").oninput=e=>{query=e.target.value;renderFolder();};renderFolder();}
  $("#play").onclick=()=>{interacted=true;playFromStart();};
  $("#loopBtn").onclick=()=>{loop=!loop;$("#loopBtn").textContent="Loop: "+(loop?"on":"off");};
  $("#bar").onclick=e=>{const v=$("#vid");if(!v)return;const r=e.currentTarget.getBoundingClientRect();const w=win();v.currentTime=w.a+(w.b-w.a)*(e.clientX-r.left)/r.width;};
  if(!sel||sel.slot!==cur) select("timeline",0,false); else {renderDecide();runCheck();}
}
function renderFolder(){
  const list=folderMatches();
  $("#fcount").textContent=`${list.length} of ${S.folder.length}`;
  $("#fgrid").innerHTML=list.map(c=>card(c,"folder",null)).join("");
  document.querySelectorAll("#fgrid .card").forEach(bindCard);
}
function bindCard(e){e.onclick=()=>{interacted=true;select(e.dataset.kind,+e.dataset.ref,true);};}
function selImage(){const s=S.slots[cur];return sel.kind=="folder"?S.folder[sel.ref]:s.candidates[sel.ref];}
function select(kind,ref,play){
  const s=S.slots[cur];
  if(kind=="timeline"&&!s.candidates[ref])return;
  sel={kind,ref,slot:cur,dur:""};chk=null;
  document.querySelectorAll(".card").forEach(c=>c.classList.toggle("on",c.dataset.kind==kind&&+c.dataset.ref==ref));
  const img=selImage();
  $("#over").src=img.missing?"":`/img/${img.id}`;
  $("#badge").textContent=img.name;
  renderDecide();runCheck();
  if(play&&S.movie)playFromStart();
}
function renderDecide(){
  const img=selImage(), s=S.slots[cur];
  $("#decide").innerHTML=`
   <div class="sel">${img.missing?"":`<img src="/thumb/${img.id}">`}<div><div>${esc(img.name)}</div>
     <div class="note">${img.cast.length?esc(img.cast.join(", ")):(sel.kind=="folder"?"from folder":"V"+img.track)}</div></div></div>
   <div><label for="dur">On screen for (seconds, 96f, or timecode)</label>
     <input class="dur" id="dur" placeholder="${s.secs}" value="${esc(sel.dur)}"></div>
   <div class="note" id="range"></div>
   <button class="primary" id="use" ${img.missing?"disabled":""}>Use this image ⏎</button>
   <button id="skip">Skip ↓</button>
   <div class="warn" id="warn" style="flex-basis:100%"></div>`;
  let t=null;
  $("#dur").oninput=e=>{sel.dur=e.target.value;clearTimeout(t);t=setTimeout(runCheck,250);};
  $("#dur").onkeydown=e=>{if(e.key=="Enter"){e.preventDefault();commit();}};
  $("#use").onclick=commit;
  $("#skip").onclick=()=>go(cur+1);
}
async function runCheck(){
  const r=await api("/api/check",{slot:cur,kind:sel.kind,ref:sel.ref,duration:sel.dur});
  chk=r.ok?r:null;
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
  const img=selImage(); if(img.missing)return;
  $("#use").disabled=true;$("#use").textContent="Saving…";
  const r=await api("/api/commit",{slot:cur,kind:sel.kind,ref:sel.ref,duration:sel.dur});
  if(!r.ok){$("#warn").textContent=r.error;$("#use").disabled=false;$("#use").textContent="Use this image ⏎";return;}
  toast(`Saved: ${img.name}`);
  S=r.state;cur=r.next;sel=null;renderHeader();renderSlots();renderMain();
}
function go(i){if(!S.slots.length)return;cur=Math.max(0,Math.min(S.slots.length-1,i));sel=null;stop();renderSlots();renderMain();}

// ---- playback: movie plays, the selected image is shown over it for its duration ----
function imgSpan(){const s=S.slots[cur];const a=secs(s.start)+S.offset;const d=chk?chk.secs:secs(s.end-s.start);return {a,b:a+d};}
function win(){const sp=imgSpan();return {a:Math.max(0,sp.a-S.preroll),b:sp.b+S.postroll};}
function playFromStart(){const v=$("#vid");if(!v)return;v.currentTime=win().a;v.play().catch(()=>{});}
function stop(){const v=$("#vid");if(v)v.pause();}
function drawBar(){
  const w=win(),sp=imgSpan(),L=w.b-w.a,bi=$("#barImg");
  if(!bi)return;bi.style.left=((sp.a-w.a)/L*100)+"%";bi.style.width=((sp.b-sp.a)/L*100)+"%";
}
function tick(){
  if(S&&S.slots.length&&sel){
    const v=$("#vid"),over=$("#over"),badge=$("#badge");
    if(over){
      if(!v){over.style.visibility="visible";}
      else{
        const t=v.currentTime,sp=imgSpan(),w=win(),inside=t>=sp.a&&t<sp.b;
        over.style.visibility=inside?"visible":"hidden";badge.style.visibility=inside?"visible":"hidden";
        if(!v.paused&&t>=w.b){if(loop)v.currentTime=w.a;else v.pause();}
        const ph=$("#ph");if(ph)ph.style.left=Math.min(100,Math.max(0,(t-w.a)/(w.b-w.a)*100))+"%";
        const tm=$("#time");if(tm)tm.textContent=`${fmt(t-sp.a)} from image start`;
        const pb=$("#play");if(pb)pb.textContent=v.paused?"▶ Play in context":"❚❚ Pause";
      }
    }
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

document.addEventListener("keydown",e=>{
  if(e.target.tagName=="INPUT"){if(e.key=="Escape")e.target.blur();return;}
  if(e.target.tagName=="SELECT")return;
  const v=$("#vid");
  if(e.key==" "){e.preventDefault();interacted=true;if(!v)return;
    if(v.paused){const w=win();if(v.currentTime<w.a||v.currentTime>=w.b)v.currentTime=w.a;v.play();}else v.pause();}
  else if(/^[1-9]$/.test(e.key)){interacted=true;select("timeline",+e.key-1,true);}
  else if(e.key=="Enter"){e.preventDefault();commit();}
  else if(e.key=="ArrowDown"||e.key=="ArrowRight"){e.preventDefault();go(cur+1);}
  else if(e.key=="ArrowUp"||e.key=="ArrowLeft"){e.preventDefault();go(cur-1);}
  else if(e.key=="/"){const q=$("#q");if(q){e.preventDefault();q.focus();}}
});
$("#undo").onclick=async()=>{const r=await api("/api/undo",{});if(r.ok){S=r.state;cur=r.next;sel=null;renderHeader();renderSlots();renderMain();toast("Undone");}};
$("#track").onchange=async e=>{const r=await api("/api/track",{track:+e.target.value});S=r.state;renderHeader();if(sel)runCheck();};
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
    print(f"{len(app.slots)} places to decide. Picks go to {app.out}")

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
            print(f"\n{len(app.log)} pick(s) saved to {app.out}. Import it in Premiere: File > Import.")


if __name__ == "__main__":
    main()
