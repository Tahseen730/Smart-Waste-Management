import html
import json
import re
from functools import lru_cache

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Smart Waste City Simulator", page_icon="♻️", layout="wide")

# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------
DEPOT = (0, 0)
# Every bin sits beside a road (x or y is a road line), so the truck can reach it by road.
BINS = [
    {"rfid": "BIN-001", "location": "Market Road", "x": 5, "y": 3, "fill": 92},
    {"rfid": "BIN-002", "location": "College Gate", "x": 10, "y": 13, "fill": 48},
    {"rfid": "BIN-003", "location": "Bus Stand", "x": 15, "y": 2, "fill": 86},
    {"rfid": "BIN-004", "location": "City Park", "x": 5, "y": 12, "fill": 35},
    {"rfid": "BIN-005", "location": "Hospital Lane", "x": 15, "y": 8, "fill": 79},
    {"rfid": "BIN-006", "location": "Residential Block", "x": 10, "y": 7, "fill": 67},
    {"rfid": "BIN-007", "location": "Shopping Complex", "x": 13, "y": 0, "fill": 96},
    {"rfid": "BIN-008", "location": "Railway Colony", "x": 2, "y": 15, "fill": 81},
]

# Animation settings (milliseconds). The whole run is ONE timeline built here and played
# by the browser, so the page never has to talk to Python while the truck is driving.
MS_PER_GRID_UNIT = 230   # truck speed (lower = faster)
MIN_LEG_MS = 700         # shortest drive between two stops
COLLECT_MS = 1800        # time spent emptying one bin
INTRO_MS = 1500          # camera swoops in behind the truck before it moves

# ----------------------------------------------------------------------------
# Road network: a grid of roads. Buildings fill the blocks BETWEEN the roads.
# ----------------------------------------------------------------------------
XS = [0, 5, 10, 15]        # vertical roads (x = constant)
YS = [0, 5, 10, 15]        # horizontal roads (y = constant)
ROAD_HALF, WALK_HALF = 0.5, 0.78
WORLD_LO, WORLD_HI = -3, 18
BIN_OFFSET = 0.66          # bins stand on the pavement, this far from the road centre line
CANVAS_H = 660             # height of the 3D map in pixels


def _attach(p):
    """Intersections a point on a road connects to, with the road distance to each."""
    opts = []
    if p[0] in XS:
        lo = max(y for y in YS if y <= p[1]); hi = min(y for y in YS if y >= p[1])
        opts += [((p[0], lo), p[1] - lo), ((p[0], hi), hi - p[1])]
    if p[1] in YS:
        lo = max(x for x in XS if x <= p[0]); hi = min(x for x in XS if x >= p[0])
        opts += [((lo, p[1]), p[0] - lo), ((hi, p[1]), hi - p[0])]
    return opts


def road_length(path):
    return sum(abs(q[0] - p[0]) + abs(q[1] - p[1]) for p, q in zip(path, path[1:]))


@lru_cache(maxsize=None)
def road_path(a, b):
    """Shortest drive from a to b that stays on the roads (list of corner points)."""
    if a == b:
        return (a,)
    for axis, cross in ((0, YS), (1, XS)):
        other = 1 - axis
        if a[axis] == b[axis] and a[axis] in (XS if axis == 0 else YS):
            lo, hi = sorted((a[other], b[other]))
            if not any(lo < r < hi for r in cross):
                return (a, b)
    best = None
    for na, _ in _attach(a):
        for nb, _ in _attach(b):
            pts = [a, na, (nb[0], na[1]), nb, b]
            path = tuple(p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1])
            length = road_length(path)
            if best is None or length < best[0]:
                best = (length, path)
    return best[1]


def distance(a, b):
    """Road distance (not straight-line): the truck must follow the roads."""
    return road_length(road_path(tuple(a), tuple(b)))


def nearest_neighbour_route(selected_bins):
    remaining = selected_bins.copy()
    current = DEPOT
    route = []
    total = 0.0
    while remaining:
        nxt = min(remaining, key=lambda item: distance(current, (item["x"], item["y"])))
        total += distance(current, (nxt["x"], nxt["y"]))
        route.append(nxt)
        current = (nxt["x"], nxt["y"])
        remaining.remove(nxt)
    if route:
        total += distance(current, DEPOT)
    return route, total


def fixed_order_distance(selected_bins):
    current, total = DEPOT, 0.0
    for item in selected_bins:
        point = (item["x"], item["y"])
        total += distance(current, point)
        current = point
    if selected_bins:
        total += distance(current, DEPOT)
    return total


def safe_id(rfid):
    return re.sub(r"[^A-Za-z0-9_-]", "-", rfid)


def bin_color(fill):
    """Red (>=75) -> yellow (50-74) -> green (<50). Same rule is used in the browser."""
    return "#ef4444" if fill >= 75 else ("#eab308" if fill >= 50 else "#22c55e")


def bin_ground(item):
    """Bins stand on the pavement, just beside the road point where the truck stops."""
    if item["x"] in XS:
        return item["x"] + BIN_OFFSET, item["y"]
    return item["x"], item["y"] + BIN_OFFSET


def full_route_points(route):
    pts, cur = [DEPOT], DEPOT
    for item in route:
        nxt = (item["x"], item["y"])
        pts += list(road_path(cur, nxt)[1:])
        cur = nxt
    if route:
        pts += list(road_path(cur, DEPOT)[1:])
    return pts


def build_timeline(route):
    """Return (segments, total_ms). Every drive (along roads) and every collection is one segment."""
    segments, t, cur = [], 0.0, DEPOT

    def drive(path, label):
        nonlocal t
        pts = [list(p) for p in path]
        if len(pts) < 2:                      # same place twice: stand still for a moment
            pts = [pts[0], pts[0]]
        dur = max(MIN_LEG_MS, road_length(path) * MS_PER_GRID_UNIT)
        segments.append({"type": "move", "t0": round(t), "t1": round(t + dur), "pts": pts, "label": label})
        t += dur

    for item in route:
        nxt = (item["x"], item["y"])
        drive(road_path(cur, nxt), f'Driving to {item["rfid"]} · {item["location"]}')
        bx, by = bin_ground(item)
        segments.append({"type": "collect", "t0": round(t), "t1": round(t + COLLECT_MS),
                         "id": safe_id(item["rfid"]), "name": item["rfid"], "fill": int(item["fill"]),
                         "bx": bx, "by": by, "tx": nxt[0], "ty": nxt[1]})
        t += COLLECT_MS
        cur = nxt
    if route:
        drive(road_path(cur, DEPOT), "Returning to depot")
    return segments, round(t)


# ----------------------------------------------------------------------------
# The 3D map (three.js). Everything - camera, truck, Start/Stop buttons - lives inside it.
# ----------------------------------------------------------------------------
CSS = """
<style>
  html,body{margin:0;background:transparent;font-family:Arial,sans-serif}
  .wrap{background:linear-gradient(135deg,#0f172a,#1e3a5f);padding:10px;border-radius:18px;border:1px solid #334155;box-shadow:0 8px 24px rgba(15,23,42,.35)}
  .top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;padding:4px 10px 10px}
  .top b{font:900 17px 'Arial Black',Arial;color:#fbbf24;letter-spacing:1px;text-shadow:0 2px 0 #000}
  .top span{font-size:12px;color:#94a3b8}
  #stage{position:relative;border-radius:12px;overflow:hidden;background:#8fd3ff}
  #stage:fullscreen{width:100vw;height:100vh;border-radius:0}
  #gl{width:100%}
  #labels{position:absolute;inset:0;pointer-events:none;overflow:hidden;font-family:'Arial Black',Arial,sans-serif}
  .bl,.bn{position:absolute;left:0;top:0;will-change:transform;display:none}
  .bl{background:#0b1220ee;border:2px solid #fbbf24;border-radius:9px;padding:3px 8px;text-align:center;min-width:64px}
  .bl b{display:block;font-size:9px;color:#cbd5e1}
  .bl .pc{display:block;font-size:17px;color:#fde047;-webkit-text-stroke:.5px #000;line-height:1.1}
  .bl i{display:block;height:5px;background:#334155;border-radius:3px;overflow:hidden}
  .bl u{display:block;height:100%}
  .bn{font-size:10px;font-weight:900;color:#fff;text-shadow:0 0 3px #000,0 0 3px #000,0 1px 0 #000;letter-spacing:1px;white-space:nowrap}
  #mission{position:absolute;left:10px;top:10px;display:flex;gap:8px;flex-wrap:wrap}
  #mission button{font:900 14px 'Arial Black',Arial;color:#fff;border:2px solid #0b1220;border-radius:12px;padding:10px 16px;cursor:pointer;box-shadow:0 3px 0 #0b1220,0 6px 14px rgba(0,0,0,.35);background:#334155}
  #mission button:active:not(:disabled){transform:translateY(2px);box-shadow:0 1px 0 #0b1220}
  #mission .go{background:#16a34a}
  #mission .stop{background:#dc2626}
  #mission button:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}
  #ctl{position:absolute;right:8px;top:8px;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;max-width:46%}
  #ctl button{font:900 11px 'Arial Black',Arial;color:#fff;background:#0b1220cc;border:2px solid #475569;border-radius:8px;padding:5px 9px;cursor:pointer}
  #ctl button[data-on="1"]{border-color:#fbbf24;color:#fde047}
  #sim-status{position:absolute;left:50%;bottom:12px;transform:translateX(-50%);max-width:92%;text-align:center;font:900 14px 'Arial Black',Arial;color:#fde047;background:#0b1220cc;border:2px solid #334155;border-radius:12px;padding:7px 16px;text-shadow:0 2px 0 #000;pointer-events:none}
  .legend{font:12px Arial;color:#cbd5e1;padding:8px 10px 2px}
  .hud{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:8px 6px 4px;font-family:'Arial Black',Arial,sans-serif}
  .chip{background:#0b1220;border:2px solid #334155;border-radius:10px;padding:6px 9px;transition:box-shadow .2s}
  .chip[data-active="1"]{border-color:#fbbf24;box-shadow:0 0 14px #fbbf24}
  .chip .n{font-size:10px;color:#cbd5e1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .chip .p{font-size:22px;font-weight:900;line-height:1.15;-webkit-text-stroke:.6px #000}
  .chip .bar{height:7px;background:#334155;border-radius:4px;overflow:hidden}
  .chip .bar i{display:block;height:100%}
  @media(max-width:640px){.hud{grid-template-columns:repeat(2,1fr)}#ctl{max-width:60%}}
</style>
"""

BODY = """
<div class="wrap">
  <div class="top"><b>🌍 OPEN WORLD • COLLECTION MISSION</b><span>Illustrative city • not GPS/real roads</span></div>
  <div id="stage">
    <div id="gl"></div><div id="labels"></div>
    <div id="mission">
      <button id="btn-start" class="go">▶ Start collection</button>
      <button id="btn-stop" class="stop" disabled>⏹ Stop</button>
      <button id="btn-replay" disabled>↻ Replay</button>
    </div>
    <div id="ctl">
      <button id="btn-auto">⟳ Auto-rotate</button><button id="btn-follow" data-on="0">🎥 Follow truck</button>
      <button id="btn-top">⬆ Top view</button><button id="btn-reset">↺ Reset view</button><button id="btn-fs">⛶ Fullscreen</button><button id="btn-snd" data-on="1">🔊 Sound</button>
    </div>
    <div id="sim-status">Loading 3D city…</div>
  </div>
  <div class="legend">
    <span style="color:#22c55e">●</span> Below 50% &nbsp; <span style="color:#eab308">●</span> 50–74% &nbsp;
    <span style="color:#ef4444">●</span> 75% or above &nbsp; <span style="color:#38bdf8">●</span> Optimized route (follows the roads)
    &nbsp;•&nbsp; Drag: look around &nbsp; Right-drag: move &nbsp; Scroll/pinch: zoom &nbsp; Space: start/stop
  </div>
  <div class="hud">__HUD__</div>
</div>
"""

THREE_JS = r"""
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
(() => {
  const D = __DATA__;
  const $ = id => document.getElementById(id);
  const stage = $('stage');
  const fail = m => { stage.innerHTML = '<div style="color:#fde047;padding:30px;font:700 15px Arial">' + m + '</div>'; };
  if (!window.THREE) { fail('The 3D view needs an internet connection to load the 3D engine (three.js). Check your connection and reload the page.'); return; }
  const T = THREE;
  let renderer;
  try { renderer = new T.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' }); }
  catch (err) { fail('Your browser could not start WebGL (3D graphics). Try Chrome or Edge and make sure hardware acceleration is on.'); return; }

  const gl = $('gl'), labelsEl = $('labels');
  const fsOn = () => !!(document.fullscreenElement || document.webkitFullscreenElement);
  const W = () => gl.clientWidth || 900;
  const Hh = () => fsOn() ? (window.innerHeight || 700) : D.canvasH;
  const DPR = window.devicePixelRatio || 1;
  // Quality levels. The game drops a level automatically if the frame rate is poor.
  const QUAL = [{ pr: 0.7, shadow: false }, { pr: 1.0, shadow: true }, { pr: Math.min(DPR, 1.5), shadow: true }];
  let q = 2;
  renderer.setPixelRatio(QUAL[q].pr);
  renderer.setSize(W(), Hh());
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = T.PCFShadowMap; renderer.shadowMap.autoUpdate = false;
  gl.appendChild(renderer.domElement);
  const cv = renderer.domElement; cv.style.touchAction = 'none'; cv.style.display = 'block'; cv.tabIndex = 0;

  const C = D.cx, lo = D.lo, hi = D.hi, span = hi - lo;
  const SKY = 0x8fd3ff;
  const scene = new T.Scene(); scene.background = new T.Color(SKY); scene.fog = new T.Fog(SKY, 95, 230);
  const camera = new T.PerspectiveCamera(50, W() / Hh(), 0.5, 500);
  scene.add(new T.HemisphereLight(0xcfe9ff, 0x4a6b3a, 0.9));
  const sun = new T.DirectionalLight(0xfff0cc, 0.85); sun.position.set(C + 20, 32, C + 8); sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  Object.assign(sun.shadow.camera, { left: -19, right: 19, top: 19, bottom: -19, near: 1, far: 90 });
  sun.shadow.camera.updateProjectionMatrix();
  sun.target.position.set(C, 0, C); scene.add(sun.target); scene.add(sun);

  // ---- small helpers (geometry is shared so the scene stays light) ----
  const lam = (c, o) => new T.MeshLambertMaterial(Object.assign({ color: c }, o || {}));
  const basic = (c, o) => new T.MeshBasicMaterial(Object.assign({ color: c }, o || {}));
  const clamp01 = v => Math.max(0, Math.min(1, v));
  const ease = t => 0.5 - Math.cos(Math.PI * t) / 2;
  const wrapPi = d => { while (d > Math.PI) d -= 2 * Math.PI; while (d < -Math.PI) d += 2 * Math.PI; return d; };
  const colorFor = v => v >= 75 ? '#ef4444' : (v >= 50 ? '#eab308' : '#22c55e');
  let seed = 7; const rnd = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  const geoCache = {};
  const boxGeo = (w, h, d) => { const k = w + '|' + h + '|' + d; return geoCache[k] || (geoCache[k] = new T.BoxGeometry(w, h, d)); };
  function box(w, h, d, mat, x, y, z, shadow) {
    const m = new T.Mesh(boxGeo(w, h, d), mat); m.position.set(x, y, z);
    m.castShadow = shadow !== false; m.receiveShadow = true; scene.add(m); return m;
  }
  function plane(w, d, mat, x, y, z) {
    const m = new T.Mesh(new T.PlaneGeometry(w, d), mat); m.rotation.x = -Math.PI / 2; m.position.set(x, y, z);
    m.receiveShadow = true; scene.add(m); return m;
  }
  const M4 = new T.Matrix4(), QT = new T.Quaternion(), PV = new T.Vector3(), SV = new T.Vector3(), EU = new T.Euler();
  function instanced(geo, mat, items, cast) {   // many copies of one shape = a single draw call
    if (!items.length) return null;
    const im = new T.InstancedMesh(geo, mat, items.length);
    items.forEach((it, i) => { QT.setFromEuler(EU.set(0, it.ry || 0, 0)); PV.set(it.x, it.y, it.z); SV.set(it.sx, it.sy, it.sz); M4.compose(PV, QT, SV); im.setMatrixAt(i, M4); });
    im.instanceMatrix.needsUpdate = true; im.frustumCulled = false; im.castShadow = !!cast; scene.add(im); return im;
  }

  // ---- open world: ground, hills, forest, clouds ----
  plane(600, 600, lam(0x5fae5a), C, -0.45, C);
  box(span + 1, 0.45, span + 1, lam(0x7a5b3f), C, -0.225, C, false);
  plane(span, span, lam(0x78c46a), C, 0.005, C);
  for (let i = 0; i < 14; i++) {
    const a = rnd() * Math.PI * 2, r = 80 + rnd() * 30, rad = 8 + rnd() * 10, h = 6 + rnd() * 10;
    const hill = new T.Mesh(new T.ConeGeometry(rad, h, 7), lam(i % 3 ? 0x4d8f58 : 0x6b7f8f));
    hill.position.set(C + Math.cos(a) * r, h / 2 - 0.4, C + Math.sin(a) * r); scene.add(hill);
  }
  const TG = { trunk: new T.CylinderGeometry(0.07, 0.1, 0.6, 6), pine: new T.ConeGeometry(0.42, 1.2, 7), round: new T.SphereGeometry(0.46, 8, 6) };
  const TM = { trunk: lam(0x78350f), pine: lam(0x15803d), round: lam(0x22a447) };
  function plantTrees(list, cast) {
    const trunks = [], pines = [], rounds = [];
    list.forEach(t => {
      const x = t[0], z = t[1], s = t[2] * 1.5, pine = rnd() > 0.5;
      trunks.push({ x, y: 0.3 * s, z, sx: s, sy: s, sz: s });
      (pine ? pines : rounds).push({ x, y: (pine ? 1.15 : 0.95) * s, z, sx: s, sy: s, sz: s });
    });
    instanced(TG.trunk, TM.trunk, trunks, cast); instanced(TG.pine, TM.pine, pines, cast); instanced(TG.round, TM.round, rounds, cast);
  }
  const forest = [];
  for (let i = 0; i < 170; i++) {
    const x = C + (rnd() - 0.5) * 140, z = C + (rnd() - 0.5) * 140;
    if (x > lo - 3 && x < hi + 3 && z > lo - 3 && z < hi + 3) continue;
    forest.push([x, z, 0.8 + rnd() * 0.9]);
  }
  plantTrees(forest, false);
  const clouds = [], cloudGeo = new T.SphereGeometry(1, 8, 6), cloudMat = basic(0xffffff, { transparent: true, opacity: 0.88 });
  for (let i = 0; i < 9; i++) {
    const g = new T.Group();
    for (let k = 0; k < 4; k++) { const s = new T.Mesh(cloudGeo, cloudMat); const sc = 1.4 + rnd(); s.scale.set(sc, sc * 0.6, sc); s.position.set(k * 1.9, rnd() * 0.6, rnd()); g.add(s); }
    g.position.set(C + (rnd() - 0.5) * 120, 17 + rnd() * 8, C + (rnd() - 0.5) * 120); scene.add(g); clouds.push(g);
  }

  // ---- roads ----
  const asphalt = lam(0x3b4250), walk = lam(0xd6d3d1), dashMat = basic(0xfacc15);
  D.xs.forEach(x => { plane(2 * D.walk, span, walk, x, 0.02, C); plane(2 * D.half, span, asphalt, x, 0.04, C); });
  D.ys.forEach(y => { plane(span, 2 * D.walk, walk, C, 0.02, y); plane(span, 2 * D.half, asphalt, C, 0.041, y); });
  const dashes = [];
  for (let k = 0; k < D.xs.length - 1; k++) for (let n = 0; n < 5; n++) {
    const a = D.xs[k] + 0.95 + n * 0.78;
    D.xs.forEach(x => { dashes.push({ x, y: 0.06, z: D.ys[k] + 0.95 + n * 0.78, sx: 0.08, sy: 0.01, sz: 0.45 }); });
    D.ys.forEach(y => { dashes.push({ x: a, y: 0.062, z: y, sx: 0.45, sy: 0.01, sz: 0.08 }); });
  }
  instanced(boxGeo(1, 1, 1), dashMat, dashes, false);

  // ---- buildings (between the roads) with glowing windows ----
  function winTex(emi) {
    const c = document.createElement('canvas'); c.width = 64; c.height = 64; const g = c.getContext('2d');
    g.fillStyle = emi ? '#000' : '#e5e7eb'; g.fillRect(0, 0, 64, 64);
    for (let r = 0; r < 2; r++) for (let k = 0; k < 2; k++) { g.fillStyle = emi ? ((r + k) % 2 ? '#fde68a' : '#7dd3fc') : '#1e293b'; g.fillRect(6 + k * 30, 8 + r * 30, 22, 20); }
    const t = new T.CanvasTexture(c); t.wrapS = t.wrapT = T.RepeatWrapping; return t;
  }
  const winMap = winTex(false), winEmi = winTex(true);
  const edgeMat = new T.LineBasicMaterial({ color: 0x0b1220 });
  const tags = [];
  D.buildings.forEach(b => {
    const w = b.x1 - b.x0, d = b.y1 - b.y0, h = b.h / 13, cx = (b.x0 + b.x1) / 2, cz = (b.y0 + b.y1) / 2;
    const side = a => { const m = winMap.clone(), e = winEmi.clone();
      [m, e].forEach(t => { t.needsUpdate = true; t.repeat.set(Math.max(1, Math.round(a / 0.9)), Math.max(1, Math.round(h / 0.9))); });
      return lam(b.color, { map: m, emissive: 0xffffff, emissiveMap: e, emissiveIntensity: 0.6 }); };
    const sx = side(d), sz = side(w), roof = lam(new T.Color(b.color).multiplyScalar(0.75));
    const mesh = new T.Mesh(boxGeo(w, h, d), [sx, sx, roof, roof, sz, sz]);
    mesh.position.set(cx, h / 2, cz); mesh.castShadow = true; mesh.receiveShadow = true;
    mesh.add(new T.LineSegments(new T.EdgesGeometry(mesh.geometry), edgeMat));
    scene.add(mesh);
    box(0.6, 0.2, 0.6, lam(0x94a3b8), cx - w * 0.2, h + 0.1, cz + d * 0.15);
    if (b.name === 'HOSPITAL') { box(1.1, 0.04, 0.3, basic(0xef4444), cx, h + 0.03, cz, false); box(0.3, 0.04, 1.1, basic(0xef4444), cx, h + 0.031, cz, false); }
    tags.push({ x: cx, y: h + 0.7, z: cz, text: b.name });
  });
  D.parks.forEach(p => {
    plane(p[2] - p[0], p[3] - p[1], lam(0x8fd96f), (p[0] + p[2]) / 2, 0.03, (p[1] + p[3]) / 2);
    const pond = new T.Mesh(new T.CircleGeometry(0.8, 24), lam(0x38bdf8)); pond.rotation.x = -Math.PI / 2;
    pond.position.set((p[0] + p[2]) / 2, 0.045, (p[1] + p[3]) / 2); scene.add(pond);
  });
  plantTrees(D.trees, true);
  { // street lamps: one draw call per part
    const poles = [], bulbs = [];
    D.lamps.forEach(l => { poles.push({ x: l[0], y: 0.78, z: l[1], sx: 1, sy: 1.3, sz: 1 }); bulbs.push({ x: l[0], y: 1.65, z: l[1], sx: 1.3, sy: 1.3, sz: 1.3 }); });
    instanced(new T.CylinderGeometry(0.03, 0.04, 1.2, 6), lam(0x94a3b8), poles, false);
    instanced(new T.SphereGeometry(0.1, 8, 6), basic(0xfef08a), bulbs, false);
    instanced(new T.SphereGeometry(0.22, 8, 6), basic(0xfde047, { transparent: true, opacity: 0.25 }), bulbs, false);
  }
  { const dp = D.depot; box(dp.w, 1.4, dp.d, lam(0x3b82f6), dp.x, 0.7, dp.z); box(0.04, 0.9, 1.3, lam(0xcbd5e1), dp.x + dp.w / 2 + 0.02, 0.45, dp.z, false); tags.push({ x: dp.x, y: 1.9, z: dp.z, text: 'DEPOT' }); }

  // ---- route line (follows the roads) ----
  const routeMat = basic(0x38bdf8);
  for (let i = 1; i < D.route.length; i++) {
    const a = D.route[i - 1], b = D.route[i], len = Math.abs(b[0] - a[0]) + Math.abs(b[1] - a[1]);
    if (!len) continue;
    const horiz = a[1] === b[1];
    box(horiz ? len + 0.14 : 0.14, 0.02, horiz ? 0.14 : len + 0.14, routeMat, (a[0] + b[0]) / 2, 0.09, (a[1] + b[1]) / 2, false);
  }

  // ---- bins ----
  const bins = {}, BS = 1.4, lidMat = lam(0x475569);
  D.bins.forEach(b => {
    const mat = lam(b.color);
    const body = box(0.36 * BS, 0.55 * BS, 0.36 * BS, mat, b.gx, 0.275 * BS, b.gy);
    box(0.42 * BS, 0.1 * BS, 0.42 * BS, lidMat, b.gx, 0.6 * BS, b.gy);
    body.add(new T.LineSegments(new T.EdgesGeometry(body.geometry), edgeMat));
    bins[b.id] = { mat, b };
    const el = document.createElement('div'); el.className = 'bl';
    el.innerHTML = '<b>' + b.name + '</b><span class="pc" id="fill-' + b.id + '">' + b.fill + '%</span><i><u id="bar-' + b.id + '" style="width:' + b.fill + '%;background:' + b.color + '"></u></i>';
    labelsEl.appendChild(el); tags.push({ x: b.gx, y: 1.75, z: b.gy, el, isBin: true });
  });
  tags.forEach(t => { if (!t.el) { const el = document.createElement('div'); el.className = 'bn'; el.textContent = t.text; labelsEl.appendChild(el); t.el = el; } });

  // ---- truck ----
  const truck = new T.Group(), wheels = [];
  const tb = (w, h, d, mat, x, y, z) => { const m = new T.Mesh(boxGeo(w, h, d), mat); m.position.set(x, y, z); m.castShadow = true; truck.add(m); return m; };
  tb(1.5, 0.16, 0.6, lam(0x1f2937), 0, 0.3, 0);
  const cont = tb(1.0, 0.78, 0.7, lam(0x22c55e), -0.22, 0.78, 0);
  tb(0.52, 0.56, 0.62, lam(0xff7a1a), 0.55, 0.58, 0);
  tb(0.05, 0.26, 0.5, basic(0xbae6fd), 0.82, 0.7, 0);
  tb(0.04, 0.1, 0.12, basic(0xfef08a), 0.82, 0.38, 0.22); tb(0.04, 0.1, 0.12, basic(0xfef08a), 0.82, 0.38, -0.22);
  const beaconMat = basic(0xef4444); tb(0.12, 0.1, 0.12, beaconMat, 0.55, 0.92, 0);
  cont.add(new T.LineSegments(new T.EdgesGeometry(cont.geometry), edgeMat));
  const wheelGeo = new T.CylinderGeometry(0.17, 0.17, 0.12, 12), wheelMat = lam(0x111827);
  [[-0.5, 0.33], [-0.5, -0.33], [0.5, 0.33], [0.5, -0.33]].forEach(p => {
    const wh = new T.Mesh(wheelGeo, wheelMat); wh.rotation.x = Math.PI / 2; wh.position.set(p[0], 0.17, p[1]); wh.castShadow = true; truck.add(wh); wheels.push(wh);
  });
  const glow = new T.Mesh(new T.CircleGeometry(1.0, 24), basic(0xfb923c, { transparent: true, opacity: 0.4 })); glow.rotation.x = -Math.PI / 2; glow.position.y = 0.05; truck.add(glow);
  truck.scale.set(1.2, 1.2, 1.2); truck.position.set(D.start[0], 0.03, D.start[1]); scene.add(truck);

  // ---- collection effect ----
  const ring = new T.Mesh(new T.RingGeometry(0.45, 0.58, 32), basic(0x22ff88, { transparent: true, opacity: 0, side: T.DoubleSide })); ring.rotation.x = -Math.PI / 2; scene.add(ring);
  const beam = new T.Mesh(new T.CylinderGeometry(0.35, 0.35, 3.4, 16, 1, true), basic(0x22ff88, { transparent: true, opacity: 0, side: T.DoubleSide })); scene.add(beam);
  const parts = []; for (let k = 0; k < 8; k++) { const s = new T.Mesh(new T.SphereGeometry(0.09, 6, 5), basic(k % 2 ? 0x92400e : 0x84cc16)); s.visible = false; scene.add(s); parts.push(s); }

  // ---- timeline playback: the picture is a pure function of the run clock `e` ----
  const segs = D.segs, total = D.total, hasRoute = segs.length > 0;
  segs.forEach(s => { if (s.type !== 'move') return; s.cum = [0];
    for (let i = 1; i < s.pts.length; i++) s.cum.push(s.cum[i - 1] + Math.hypot(s.pts[i][0] - s.pts[i - 1][0], s.pts[i][1] - s.pts[i - 1][1]));
    s.len = s.cum[s.cum.length - 1]; });
  function along(s, d) { let i = 1; while (i < s.pts.length - 1 && s.cum[i] < d) i++;
    const a = s.pts[i - 1], b = s.pts[i], u = clamp01((d - s.cum[i - 1]) / ((s.cum[i] - s.cum[i - 1]) || 1));
    return { x: a[0] + (b[0] - a[0]) * u, y: a[1] + (b[1] - a[1]) * u, dx: b[0] - a[0], dy: b[1] - a[1] }; }
  const statusEl = $('sim-status'); let lastText = null;
  const setStatus = t => { if (t !== lastText) { statusEl.textContent = t; lastText = t; } };
  const live = {}; segs.filter(s => s.type === 'collect').forEach(s => { live[s.id] = { shown: null, active: null }; });
  var AC, state = 'idle', spd = 0, speedNow = 0, collectP = -1, sfx;
  let ang = 0, tx0 = D.start[0], tz0 = D.start[1], wheelRot = 0, firstUpdate = true, frNow = 1;

  // state: idle -> intro (camera swoops in) -> running <-> paused -> done
  let e = 0, introT = 0, introDone = false;

  function update(e) {
    let x = D.start[0], z = D.start[1], text = null, dir = null, inMove = false;
    for (const s of segs) {
      if (e < s.t0) break;
      if (s.type === 'move') { const p = e >= s.t1 ? 1 : ease(clamp01((e - s.t0) / (s.t1 - s.t0))); const r = along(s, p * s.len);
        x = r.x; z = r.y; if (r.dx || r.dy) dir = [r.dx, r.dy]; if (e < s.t1) { inMove = true; text = s.label; } }
      else { x = s.tx; z = s.ty; }
    }
    let fxOn = false; collectP = -1;
    for (const s of segs) {
      if (s.type !== 'collect') continue;
      const p = clamp01((e - s.t0) / (s.t1 - s.t0)), val = e <= s.t0 ? s.fill : (p >= 1 ? 0 : Math.round(s.fill * (1 - p))), L = live[s.id], bn = bins[s.id];
      if (L.shown !== val) { L.shown = val; const col = colorFor(val);
        $('fill-' + s.id).textContent = val + '%'; $('bar-' + s.id).setAttribute('style', 'width:' + val + '%;background:' + col);
        $('hud-pct-' + s.id).textContent = val + '%'; $('hud-pct-' + s.id).setAttribute('style', 'color:' + col);
        $('hud-bar-' + s.id).setAttribute('style', 'width:' + val + '%;background:' + col); bn.mat.color.set(col); }
      const act = (e > s.t0 && p < 1) ? '1' : '0'; if (L.active !== act) { const was = L.active; L.active = act; $('chip-' + s.id).setAttribute('data-active', act); if (state === 'running' && was !== null) { if (act === '1') sfx.thud(); else if (was === '1' && e > s.t0) sfx.ding(); } }
      if (act === '1') { fxOn = true; collectP = p; text = 'Collecting waste at ' + s.name + ' — ' + val + '%';
        const qq = (p * 2) % 1; ring.position.set(s.bx, 0.1, s.by); ring.scale.set(1 + 3 * qq, 1 + 3 * qq, 1); ring.material.opacity = 0.8 * (1 - qq);
        beam.position.set(s.bx, 1.7, s.by); beam.material.opacity = 0.22 + 0.1 * Math.sin(p * 30);
        const edge = Math.min(1, p * 8, (1 - p) * 8);
        parts.forEach((m, k) => { const ph = (p * 3 + k / parts.length) % 1; m.visible = edge > 0.05;
          m.position.set(s.bx + (s.tx - s.bx) * ph, 0.9 + 0.6 * ph + Math.sin(Math.PI * ph) * 1.4, s.by + (s.ty - s.by) * ph); }); }
    }
    if (!fxOn) { ring.material.opacity = 0; beam.material.opacity = 0; parts.forEach(m => { m.visible = false; }); }
    const moving = state === 'running' && inMove;
    const dist = Math.hypot(x - tx0, z - tz0); wheelRot += dist * 4; speedNow = dist / Math.max(0.001, frNow / 60); tx0 = x; tz0 = z;
    wheels.forEach(w => { w.rotation.y = -wheelRot; });
    if (dir) { const target = Math.atan2(-dir[1], dir[0]); ang += wrapPi(target - ang) * (firstUpdate ? 1 : Math.min(1, 1 - Math.pow(1 - 0.2, frNow))); }
    truck.rotation.y = ang; truck.position.set(x, 0.03 + (moving ? Math.abs(Math.sin(e / 90)) * 0.012 : 0), z); firstUpdate = false;
    return text;
  }

  // ---- free camera: drag = rotate, right-drag/shift = pan, wheel/pinch = zoom ----
  const HOME = { theta: 0.8, phi: 1.0, r: 38, tx: C, tz: C };
  const goal = Object.assign({}, HOME), cur = Object.assign({}, HOME);
  let auto = true, follow = false, lastDrag = 0, camFrom = null, camTo = null;
  try { const s = JSON.parse(localStorage.getItem('wasteCam3d2')); if (s && s.r) { Object.assign(goal, s); Object.assign(cur, s); auto = false; } } catch (err) {}
  const save = () => { if (follow) return; try { localStorage.setItem('wasteCam3d2', JSON.stringify(goal)); } catch (err) {} };
  const setOn = (id, on) => { const b = $(id); if (b) b.setAttribute('data-on', on ? '1' : '0'); };
  const touched = () => { auto = false; setOn('btn-auto', false); };
  const clampCam = () => { goal.phi = Math.max(0.12, Math.min(1.5, goal.phi)); goal.r = Math.max(5, Math.min(90, goal.r));
    goal.tx = Math.max(-10, Math.min(25, goal.tx)); goal.tz = Math.max(-10, Math.min(25, goal.tz)); };
  const ptrs = new Map();
  cv.addEventListener('contextmenu', ev => ev.preventDefault());
  cv.addEventListener('pointerdown', ev => { cv.setPointerCapture(ev.pointerId); ptrs.set(ev.pointerId, { x: ev.clientX, y: ev.clientY, b: ev.button }); touched(); cv.focus(); });
  const up = ev => { ptrs.delete(ev.pointerId); save(); };
  cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', up);
  cv.addEventListener('pointermove', ev => {
    const p = ptrs.get(ev.pointerId); if (!p) return;
    const dx = ev.clientX - p.x, dy = ev.clientY - p.y;
    if (ptrs.size === 1) {
      if (p.b === 2 || ev.shiftKey) { const s = goal.r * 0.0018; follow = false; setOn('btn-follow', false);
        goal.tx += -Math.cos(goal.theta) * dx * s + -Math.sin(goal.theta) * dy * s; goal.tz += Math.sin(goal.theta) * dx * s + -Math.cos(goal.theta) * dy * s; }
      else { goal.theta -= dx * 0.008; goal.phi -= dy * 0.006; lastDrag = performance.now(); }
    } else if (ptrs.size === 2) {
      const o = [...ptrs.entries()].find(k => k[0] !== ev.pointerId)[1];
      const before = Math.hypot(p.x - o.x, p.y - o.y), after = Math.hypot(ev.clientX - o.x, ev.clientY - o.y);
      goal.r *= before / (after || 1);
    }
    p.x = ev.clientX; p.y = ev.clientY; clampCam();
  });
  cv.addEventListener('wheel', ev => { ev.preventDefault(); touched(); goal.r *= Math.exp(ev.deltaY * 0.001); clampCam(); save(); }, { passive: false });
  const btn = (id, fn) => { const b = $(id); if (b) b.addEventListener('click', fn); };
  btn('btn-reset', () => { Object.assign(goal, HOME); follow = false; setOn('btn-follow', false); save(); });
  btn('btn-top', () => { goal.phi = 0.12; touched(); });
  btn('btn-follow', () => { follow = !follow; lastDrag = 0; touched(); setOn('btn-follow', follow); });
  btn('btn-auto', () => { auto = !auto; setOn('btn-auto', auto); });
  btn('btn-fs', () => { try { if (fsOn()) (document.exitFullscreen || document.webkitExitFullscreen).call(document); else (stage.requestFullscreen || stage.webkitRequestFullscreen).call(stage); } catch (err) {} });
  setOn('btn-auto', auto); setOn('btn-follow', follow);


  // ---- sound: everything is synthesised with the Web Audio API (no audio files needed) ----
  AC = null; let master = null, eng = null, suck = null, muted = false; spd = 0; speedNow = 0; collectP = -1;
  function audioInit() {
    try {
      if (!AC) {
        AC = new (window.AudioContext || window.webkitAudioContext)();
        master = AC.createGain(); master.gain.value = muted ? 0 : 1.0;
        const comp = AC.createDynamicsCompressor(); master.connect(comp); comp.connect(AC.destination);
        // truck engine: two low oscillators through a low-pass filter
        const f1 = AC.createOscillator(), f2 = AC.createOscillator(), lp = AC.createBiquadFilter(), g = AC.createGain();
        f1.type = 'sawtooth'; f2.type = 'square'; f1.frequency.value = 42; f2.frequency.value = 63;
        lp.type = 'lowpass'; lp.frequency.value = 250; g.gain.value = 0;
        f1.connect(lp); f2.connect(lp); lp.connect(g); g.connect(master); f1.start(); f2.start();
        eng = { f1, f2, lp, g };
        // waste being sucked into the truck: looping noise through a moving band-pass filter
        const buf = AC.createBuffer(1, AC.sampleRate * 2, AC.sampleRate), data = buf.getChannelData(0);
        for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
        const src = AC.createBufferSource(); src.buffer = buf; src.loop = true;
        const bp = AC.createBiquadFilter(), sg = AC.createGain(); bp.type = 'bandpass'; bp.Q.value = 2.5; bp.frequency.value = 600; sg.gain.value = 0;
        src.connect(bp); bp.connect(sg); sg.connect(master); src.start();
        suck = { bp, g: sg };
      }
      if (AC.state === 'suspended') AC.resume();
    } catch (err) { AC = null; }
  }
  function tone(f, dur, type, vol, when, slideTo) {
    if (!AC || muted) return;
    const t = AC.currentTime + (when || 0), o = AC.createOscillator(), g = AC.createGain();
    o.type = type || 'sine'; o.frequency.setValueAtTime(f, t);
    if (slideTo) o.frequency.exponentialRampToValueAtTime(slideTo, t + dur);
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(Math.min(0.9, (vol || 0.2) * 2), t + 0.012); g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g); g.connect(master); o.start(t); o.stop(t + dur + 0.05);
  }
  sfx = {
    click: () => tone(660, 0.07, 'square', 0.08),
    horn: () => { tone(330, 0.35, 'sawtooth', 0.12); tone(415, 0.35, 'sawtooth', 0.1); },
    thud: () => tone(140, 0.18, 'sine', 0.3, 0, 50),
    ding: () => { tone(988, 0.22, 'sine', 0.2); tone(1319, 0.35, 'sine', 0.16, 0.12); },
    done: () => [523, 659, 784, 1047].forEach((f, i) => tone(f, 0.4, 'triangle', 0.2, i * 0.14)),
  };
  function audioFrame() {
    if (!AC) return;
    const t = AC.currentTime, run = state === 'running', sp = run ? Math.min(spd, 6) : 0;
    eng.f1.frequency.setTargetAtTime(42 + sp * 10, t, 0.1); eng.f2.frequency.setTargetAtTime(63 + sp * 15, t, 0.1);
    eng.lp.frequency.setTargetAtTime(250 + sp * 120, t, 0.1);
    eng.g.gain.setTargetAtTime(run ? 0.26 + 0.12 * Math.min(1, sp / 4) : (state === 'intro' ? 0.12 : 0), t, 0.15);
    suck.g.gain.setTargetAtTime(run && collectP >= 0 ? 0.4 : 0, t, 0.08);
    if (collectP >= 0) suck.bp.frequency.setTargetAtTime(500 + collectP * 1500, t, 0.05);
  }
  btn('btn-snd', () => { audioInit(); muted = !muted; if (master) master.gain.setTargetAtTime(muted ? 0 : 1.0, AC.currentTime, 0.02); setOn('btn-snd', !muted); $('btn-snd').textContent = muted ? '🔇 Muted' : '🔊 Sound'; if (!muted) sfx.click(); });

  // ---- Start / Stop / Replay (inside the map) ----
  const bStart = $('btn-start'), bStop = $('btn-stop'), bReplay = $('btn-replay');
  function ui() {
    bStart.disabled = !hasRoute || state === 'intro' || state === 'running';
    bStart.textContent = state === 'paused' ? '▶ Resume' : (state === 'done' ? '↻ Play again' : '▶ Start collection');
    bStop.disabled = !(state === 'intro' || state === 'running');
    bReplay.disabled = !hasRoute || state === 'idle';
  }
  function beginIntro() {            // swoop the camera in behind the truck, then drive
    auto = false; follow = true; setOn('btn-auto', false); setOn('btn-follow', true);
    camFrom = Object.assign({}, cur);
    const th = Math.atan2(-Math.cos(ang), Math.sin(ang));
    camTo = { theta: camFrom.theta + wrapPi(th - camFrom.theta), phi: 1.0, r: 13, tx: truck.position.x, tz: truck.position.z };
    introT = 0; introDone = false; state = 'intro'; ui();
  }
  function startRun() {
    audioInit();
    if (!hasRoute || state === 'intro' || state === 'running') return;
    if (state === 'paused' && introDone) { state = 'running'; ui(); return; }
    if (state === 'done') e = 0;
    beginIntro();
  }
  function stopRun() { if (state === 'intro' || state === 'running') { state = 'paused'; ui(); sfx.click(); } }
  function replayRun() { if (!hasRoute) return; audioInit(); e = 0; update(0); beginIntro(); }
  bStart.addEventListener('click', startRun); bStop.addEventListener('click', stopRun); bReplay.addEventListener('click', replayRun);
  window.addEventListener('keydown', ev => { if (ev.code === 'Space') { ev.preventDefault(); (state === 'intro' || state === 'running') ? stopRun() : startRun(); } });
  ui();

  const statusFor = t => {
    if (state === 'idle') return hasRoute ? 'Ready — press ▶ Start collection' : 'No bins reach the threshold — the truck stays at the depot';
    if (state === 'intro') return 'Camera moving to the truck…';
    if (state === 'paused') return '⏸ Truck stopped — press ▶ Resume to continue';
    if (state === 'done') return '✅ Collection complete — truck returned to depot';
    return t || 'Collection in progress';
  };

  const v = new T.Vector3();
  function placeLabels() {
    const w = W(), h = Hh();
    for (const t of tags) {
      v.set(t.x, t.y, t.z).project(camera);
      const vis = v.z < 1 && Math.abs(v.x) < 1.1 && Math.abs(v.y) < 1.1 && (t.isBin || cur.r < 48);
      if (vis !== t.vis) { t.vis = vis; t.el.style.display = vis ? 'block' : 'none'; }
      if (vis) t.el.style.transform = 'translate(' + ((v.x * 0.5 + 0.5) * w).toFixed(1) + 'px,' + ((-v.y * 0.5 + 0.5) * h).toFixed(1) + 'px) translate(-50%,-100%)';
    }
  }
  function resize() { const w = W(), h = Hh(); renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix(); }
  window.addEventListener('resize', resize);
  document.addEventListener('fullscreenchange', resize); document.addEventListener('webkitfullscreenchange', resize);

  // ---- adaptive quality: if the frame rate is poor, drop resolution / shadows automatically ----
  function applyQuality() { const c = QUAL[q]; renderer.setPixelRatio(c.pr); renderer.setSize(W(), Hh()); sun.castShadow = c.shadow; }
  let fpsT = 0, fpsN = 0, warm = 3, frameNo = 0;
  function fpsTick(dt) {
    fpsT += dt; fpsN++;
    if (fpsT < 1.5) return;
    const fps = fpsN / fpsT; fpsT = 0; fpsN = 0;
    if (warm > 0) { warm -= 1.5; return; }
    if (fps < 34 && q > 0) { q--; applyQuality(); warm = 3; }
  }

  let lastNow = performance.now();
  function loop(now) {
    requestAnimationFrame(loop);
    const dt = Math.min(0.1, Math.max(0.001, (now - lastNow) / 1000)); lastNow = now;
    frNow = dt * 60;
    const fr = f => 1 - Math.pow(1 - f, frNow);   // same camera feel on 60 Hz and 120 Hz screens
    fpsTick(dt);
    clouds.forEach((c, i) => { c.position.x += (0.004 + i * 0.0005) * frNow; if (c.position.x > C + 70) c.position.x = C - 70; });
    beaconMat.color.set(Math.floor(now / 300) % 2 ? 0xef4444 : 0x7f1d1d);

    // The run clock only advances by real frame time (capped), so a slow frame slows the whole
    // game a little instead of letting the truck run ahead of the camera.
    if (state === 'running') { e = Math.min(total, e + dt * 1000); if (e >= total) { state = 'done'; ui(); sfx.done(); } }
    if (state === 'intro') { introT += dt * 1000; if (introT >= D.intro) { introDone = true; state = 'running'; ui(); sfx.horn(); } }
    const text = hasRoute ? update(e) : null;
    spd += (speedNow - spd) * Math.min(1, 0.15 * frNow); audioFrame();
    setStatus(statusFor(text));

    if (auto) goal.theta += 0.0035 * frNow;
    if (follow) {
      goal.tx = truck.position.x; goal.tz = truck.position.z;
      if (state === 'running' && now - lastDrag > 2500) {   // swing round to stay behind the truck
        const want = Math.atan2(-Math.cos(ang), Math.sin(ang)); goal.theta += wrapPi(want - goal.theta) * fr(0.05);
      }
    }
    if (state === 'intro') {
      const u = ease(clamp01(introT / D.intro));
      camTo.tx = truck.position.x; camTo.tz = truck.position.z;
      ['theta', 'phi', 'r', 'tx', 'tz'].forEach(k => { cur[k] = camFrom[k] + (camTo[k] - camFrom[k]) * u; goal[k] = cur[k]; });
    } else {
      // the camera target is locked exactly onto the truck; only the viewing angle is smoothed
      ['theta', 'phi', 'r', 'tx', 'tz'].forEach(k => { if (follow && (k === 'tx' || k === 'tz')) cur[k] = goal[k]; else cur[k] += (goal[k] - cur[k]) * fr(0.12); });
    }
    const sp = Math.sin(cur.phi), cp = Math.cos(cur.phi);
    camera.position.set(cur.tx + cur.r * sp * Math.sin(cur.theta), cur.r * cp, cur.tz + cur.r * sp * Math.cos(cur.theta));
    camera.lookAt(cur.tx, 0, cur.tz);
    placeLabels();
    if (sun.castShadow && frameNo % 2 === 0) renderer.shadowMap.needsUpdate = true;   // shadows refresh every 2nd frame
    frameNo++;
    renderer.render(scene, camera);
  }
  requestAnimationFrame(loop);
})();
</script>
"""


def city_simulation_3d(route, all_bins):
    """Open-world 3D city (three.js) with Start / Stop / Replay buttons inside the map."""
    cells = {
        (0, 0): ("STATION", "#38bdf8", 34), (1, 0): ("COLLEGE", "#6366f1", 46), (2, 0): ("HOSPITAL", "#fb7185", 54),
        (1, 1): ("RESIDENTIAL", "#a855f7", 40), (2, 1): ("MARKET", "#fbbf24", 30), (0, 2): ("HOUSING", "#fb923c", 38),
        (1, 2): ("MALL", "#ec4899", 44),
    }
    inset = 1.25  # distance from the road centre line to the building wall (keeps blocks airy)
    buildings = [{"x0": XS[i] + inset, "x1": XS[i + 1] - inset, "y0": YS[j] + inset, "y1": YS[j + 1] - inset,
                  "h": h, "color": color, "name": name} for (i, j), (name, color, h) in cells.items()]
    park_cells = [(0, 1), (2, 2)]
    parks = [[XS[i] + 0.95, YS[j] + 0.95, XS[i + 1] - 0.95, YS[j + 1] - 0.95] for i, j in park_cells]
    trees = []
    for p in parks:
        for fx, fy, sc in [(0.2, 0.25, 1), (0.75, 0.2, 0.9), (0.5, 0.8, 1.1), (0.2, 0.75, 0.9), (0.8, 0.75, 1), (0.5, 0.45, 0.8)]:
            trees.append([p[0] + fx * (p[2] - p[0]), p[1] + fy * (p[3] - p[1]), sc])
    for t in (2.5, 5, 7.5, 10, 12.5, 15):                       # trees around the edge of the city
        trees += [[t, -2.2, 0.85], [t, 17.2, 0.85], [17.2, t, 0.85]]
    trees += [[-2.2, t, 0.85] for t in (4, 6.5, 9, 11.5, 14)]
    lamps = []                                                   # street lamps along every road
    for r in XS:
        for k in range(len(YS) - 1):
            lamps += [[r - 0.72, YS[k] + f * (YS[k + 1] - YS[k])] for f in (0.3, 0.7)]
    for r in YS:
        for k in range(len(XS) - 1):
            lamps += [[XS[k] + f * (XS[k + 1] - XS[k]), r - 0.72] for f in (0.3, 0.7)]
    bins = []
    for item in all_bins:
        bgx, bgy = bin_ground(item)
        bins.append({"id": safe_id(item["rfid"]), "name": item["rfid"], "gx": bgx, "gy": bgy,
                     "fill": item["fill"], "color": bin_color(item["fill"])})
    segments, total_ms = build_timeline(route)
    data = {
        "xs": XS, "ys": YS, "half": ROAD_HALF, "walk": WALK_HALF, "lo": WORLD_LO, "hi": WORLD_HI,
        "cx": (XS[0] + XS[-1]) / 2, "canvasH": CANVAS_H, "intro": INTRO_MS,
        "buildings": buildings, "parks": parks, "trees": trees, "lamps": lamps, "bins": bins,
        "depot": {"x": -1.9, "z": 0, "w": 1.0, "d": 2.0}, "start": list(DEPOT),
        "route": [list(p) for p in full_route_points(route)], "segs": segments, "total": total_ms,
    }
    hud = "".join(
        f'<div class="chip" id="chip-{safe_id(i["rfid"])}" data-active="0">'
        f'<div class="n">{html.escape(i["rfid"])} · {html.escape(i["location"])}</div>'
        f'<div class="p" id="hud-pct-{safe_id(i["rfid"])}" style="color:{bin_color(i["fill"])}">{i["fill"]}%</div>'
        f'<div class="bar"><i id="hud-bar-{safe_id(i["rfid"])}" style="width:{i["fill"]}%;background:{bin_color(i["fill"])}"></i></div></div>'
        for i in all_bins
    )
    script = THREE_JS.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    page = CSS + BODY.replace("__HUD__", hud) + script
    components.html(page, height=CANVAS_H + 300, scrolling=True)


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
def reset_all():
    for b in BINS:
        st.session_state[f'fill_{b["rfid"]}'] = b["fill"]
    st.session_state.threshold = 75


for _b in BINS:
    st.session_state.setdefault(f'fill_{_b["rfid"]}', _b["fill"])
st.session_state.setdefault("threshold", 75)

st.title("♻️ Smart Waste City Simulator")
st.caption("RFID bin identification • Simulated ultrasonic sensing • Nearest-neighbour route optimization")
st.info("Educational simulation only: RFID and ultrasonic readings are simulated. The map uses illustrative coordinates, not real roads, GPS, traffic, or municipal data.")

with st.sidebar:
    st.header("🎛️ Control Centre")
    threshold = st.slider("Collection threshold (%)", 0, 100, key="threshold")
    st.divider()
    st.subheader("Simulated bin sensors")
    st.caption("Changing a slider rebuilds the map. Press ▶ Start inside the map to run the truck.")
    fills = {}
    for item in BINS:
        fills[item["rfid"]] = st.slider(f'{item["rfid"]} · {item["location"]}', 0, 100, key=f'fill_{item["rfid"]}')
    st.button("↺ Reset sample data", on_click=reset_all, use_container_width=True)

records = [
    {**item, "fill": fills[item["rfid"]], "gap": 100 - fills[item["rfid"]],
     "status": "COLLECT" if fills[item["rfid"]] >= threshold else "WAIT"}
    for item in BINS
]
selected = [r for r in records if r["fill"] >= threshold]
route, optimized = nearest_neighbour_route(selected)
baseline = fixed_order_distance(selected)

a, b, c, d = st.columns(4)
a.metric("🗑️ Total bins", len(records))
b.metric("🚛 Bins to collect", len(selected))
c.metric("⏳ Bins waiting", len(records) - len(selected))
d.metric("🎯 Threshold", f"{threshold}%")

st.subheader("🗺️ City simulation")
if not selected:
    st.info("No bins currently meet the collection threshold. The truck stays at the depot.")
city_simulation_3d(route, records)

st.subheader("📡 Smart bin monitoring")
df = pd.DataFrame([
    {"RFID ID": r["rfid"], "Location": r["location"], "Sensor gap (cm)": r["gap"],
     "Fill level (%)": r["fill"], "Status": r["status"]}
    for r in records
])
st.dataframe(
    df, use_container_width=True, hide_index=True,
    column_config={"Fill level (%)": st.column_config.ProgressColumn(
        "Fill level (%)", min_value=0, max_value=100, format="%d%%"
    )},
)

left, right = st.columns(2)
with left:
    st.subheader("📊 Bin fill levels")
    st.bar_chart(df.set_index("Location")[["Fill level (%)"]])
with right:
    st.subheader("🚦 Collection status")
    counts = df["Status"].value_counts().reindex(["COLLECT", "WAIT"], fill_value=0)
    st.bar_chart(counts)

st.subheader("🧭 Optimized collection route")
if route:
    names = ["Depot"] + [r["location"] for r in route] + ["Depot"]
    st.success("  ➜  ".join(names))
    st.dataframe(pd.DataFrame([
        {"Stop": i, "RFID": r["rfid"], "Location": r["location"], "Fill": f'{r["fill"]}%'}
        for i, r in enumerate(route, 1)
    ]), use_container_width=True, hide_index=True)
else:
    st.info("No collection route is required right now.")

x, y, z = st.columns(3)
x.metric("Optimized distance", f"{optimized:.2f} road units")
y.metric("Fixed-order distance", f"{baseline:.2f} road units")
if baseline > 0:
    z.metric("Difference vs fixed order", f"{(baseline - optimized) / baseline * 100:.1f}%")
else:
    z.metric("Difference vs fixed order", "—")

st.subheader("✅ Expected bin-fill percentages after collection")
collected = {r["rfid"] for r in route}
final_df = pd.DataFrame([
    {"RFID ID": r["rfid"], "Location": r["location"], "Before (%)": r["fill"],
     "After (%)": 0 if r["rfid"] in collected else r["fill"],
     "Collected": "Yes" if r["rfid"] in collected else "No"}
    for r in records
])
st.dataframe(
    final_df, use_container_width=True, hide_index=True,
    column_config={
        "Before (%)": st.column_config.ProgressColumn("Before (%)", min_value=0, max_value=100, format="%d%%"),
        "After (%)": st.column_config.ProgressColumn("After (%)", min_value=0, max_value=100, format="%d%%"),
    },
)
st.download_button(
    "⬇ Download final levels (CSV)", final_df.to_csv(index=False),
    file_name="final_bin_levels.csv", mime="text/csv",
)

with st.expander("ℹ️ How the simulation works"):
    st.write("Each bin has a simulated RFID identifier. The ultrasonic sensor is represented by a simulated gap reading, assuming a 100 cm bin: gap = 100 − fill percentage (cm). A bin is marked COLLECT when its fill reaches the selected threshold. The nearest-neighbour heuristic selects the next closest eligible bin and returns to the depot.")
    st.write("The route distance is the distance driven along the roads of a demonstration city (not real-world roads). The heuristic does not guarantee the globally shortest route.")

st.caption("Project: Smart Solid Waste Collection Route Optimization Using RFID and Sensors")
