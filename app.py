import csv
import html
import json
import math
import re
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Smart Waste City Simulator", page_icon="♻️", layout="wide")

# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------
DEPOT = (0, 0)
BINS = [
    {"rfid": "BIN-001", "location": "Market Road", "x": 2, "y": 3, "fill": 92},
    {"rfid": "BIN-002", "location": "College Gate", "x": 5, "y": 8, "fill": 48},
    {"rfid": "BIN-003", "location": "Bus Stand", "x": 7, "y": 2, "fill": 86},
    {"rfid": "BIN-004", "location": "City Park", "x": 3, "y": 7, "fill": 35},
    {"rfid": "BIN-005", "location": "Hospital Lane", "x": 9, "y": 6, "fill": 79},
    {"rfid": "BIN-006", "location": "Residential Block", "x": 6, "y": 4, "fill": 67},
    {"rfid": "BIN-007", "location": "Shopping Complex", "x": 10, "y": 2, "fill": 96},
    {"rfid": "BIN-008", "location": "Railway Colony", "x": 1, "y": 9, "fill": 81},
]

# Animation speed settings (milliseconds). Python builds ONE timeline from these
# numbers; the browser plays it and Python waits for exactly the same duration,
# so the two can never drift apart.
MS_PER_GRID_UNIT = 230   # truck speed (lower = faster)
MIN_LEG_MS = 700         # shortest drive between two stops
COLLECT_MS = 1600        # time spent emptying one bin
END_BUFFER_S = 2.0       # safety margin before the page refreshes

RESULTS_CSV = Path(__file__).with_name("final_bin_levels.csv")


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


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


def map_x(x):
    return 90 + x * 65


def map_y(y):
    return 445 - y * 40


def safe_id(rfid):
    return re.sub(r"[^A-Za-z0-9_-]", "-", rfid)


def bin_color(fill):
    """Red (>=75) -> yellow (50-74) -> green (<50). Same rule is used in the browser."""
    return "#ef4444" if fill >= 75 else ("#eab308" if fill >= 50 else "#22c55e")


def stop_point(item):
    """Where the truck parks for a bin: just left of it, so the bin stays visible."""
    return map_x(item["x"]) - 36, map_y(item["y"]) - 2


def build_timeline(route):
    """Return (segments, total_ms). Every drive and every collection is one segment."""
    depot_px = (map_x(DEPOT[0]), map_y(DEPOT[1]))
    segments, t = [], 0.0
    cur_px, cur_grid = depot_px, DEPOT
    for item in route:
        tx, ty = stop_point(item)
        grid = (item["x"], item["y"])
        dur = max(MIN_LEG_MS, distance(cur_grid, grid) * MS_PER_GRID_UNIT)
        segments.append({
            "type": "move", "t0": round(t), "t1": round(t + dur),
            "fx": cur_px[0], "fy": cur_px[1], "tx": tx, "ty": ty,
            "label": f'Driving to {item["rfid"]} · {item["location"]}',
        })
        t += dur
        segments.append({
            "type": "collect", "t0": round(t), "t1": round(t + COLLECT_MS),
            "id": safe_id(item["rfid"]), "name": item["rfid"], "fill": int(item["fill"]),
            "bx": map_x(item["x"]), "by": map_y(item["y"]), "tx": tx, "ty": ty,
        })
        t += COLLECT_MS
        cur_px, cur_grid = (tx, ty), grid
    dur = max(MIN_LEG_MS, distance(cur_grid, DEPOT) * MS_PER_GRID_UNIT)
    segments.append({
        "type": "move", "t0": round(t), "t1": round(t + dur),
        "fx": cur_px[0], "fy": cur_px[1], "tx": depot_px[0], "ty": depot_px[1],
        "label": "Returning to depot",
    })
    t += dur
    return segments, round(t)


ANIMATION_JS = """
<script>
(() => {
  const segs = __SEGS__;
  const total = __TOTAL__;
  const svgNS = 'http://www.w3.org/2000/svg';
  const truck = document.getElementById('truck');
  const status = document.getElementById('sim-status');
  const fx = document.getElementById('fx');
  const clamp01 = v => Math.max(0, Math.min(1, v));
  const ease = t => 0.5 - Math.cos(Math.PI * t) / 2;
  const colorFor = v => v >= 75 ? '#ef4444' : (v >= 50 ? '#eab308' : '#22c55e');

  // Collection effect: an expanding ring plus small waste pieces flying into the truck.
  const ring = document.createElementNS(svgNS, 'circle');
  ring.setAttribute('fill', 'none'); ring.setAttribute('stroke', '#16a34a');
  ring.setAttribute('stroke-width', '3'); ring.setAttribute('opacity', '0');
  fx.appendChild(ring);
  const parts = [];
  for (let k = 0; k < 7; k++) {
    const c = document.createElementNS(svgNS, 'circle');
    c.setAttribute('r', '3.5'); c.setAttribute('fill', k % 2 ? '#92400e' : '#65a30d');
    c.setAttribute('opacity', '0');
    fx.appendChild(c); parts.push(c);
  }

  const binEls = {};
  segs.filter(s => s.type === 'collect').forEach(s => {
    binEls[s.id] = {
      rect: document.getElementById('binrect-' + s.id),
      label: document.getElementById('fill-' + s.id),
      shown: null
    };
  });

  let facing = 1, lastText = null;
  const setStatus = t => { if (t !== lastText) { status.textContent = t; lastText = t; } };

  // The whole picture is a pure function of elapsed time, so it can never get out of sync.
  function render(e) {
    let pos = { x: segs[0].fx, y: segs[0].fy };
    let text = null, moving = false, active = null;
    for (const s of segs) {
      if (e >= s.t1) {
        pos = { x: s.tx, y: s.ty };
      } else if (e >= s.t0) {
        active = s;
        if (s.type === 'move') {
          const p = ease(clamp01((e - s.t0) / (s.t1 - s.t0)));
          pos = { x: s.fx + (s.tx - s.fx) * p, y: s.fy + (s.ty - s.fy) * p };
          moving = true; text = s.label;
        } else {
          pos = { x: s.tx, y: s.ty };
        }
      } else break;
      if (s.type === 'move' && e >= s.t0 && s.tx !== s.fx) facing = s.tx > s.fx ? 1 : -1;
    }

    // Bins: live fill-percentage and colour.
    let fxShown = false;
    for (const s of segs) {
      if (s.type !== 'collect' || e < s.t0) continue;
      const p = clamp01((e - s.t0) / (s.t1 - s.t0));
      const val = p >= 1 ? 0 : Math.round(s.fill * (1 - p));
      const b = binEls[s.id];
      if (b.shown !== val) {
        b.shown = val;
        b.label.textContent = val + '%';
        b.rect.setAttribute('fill', colorFor(val));
      }
      if (p < 1) {
        fxShown = true;
        text = 'Collecting waste at ' + s.name + ' — ' + val + '%';
        const q = (p * 2) % 1;
        ring.setAttribute('cx', s.bx); ring.setAttribute('cy', s.by - 10);
        ring.setAttribute('r', 10 + 34 * q);
        ring.setAttribute('opacity', 0.7 * (1 - q));
        const edge = Math.min(1, p * 8, (1 - p) * 8);
        parts.forEach((c, k) => {
          const ph = (p * 3 + k / parts.length) % 1;
          const sy = s.by - 10, ey = s.ty - 4;
          c.setAttribute('cx', s.bx + (s.tx - s.bx) * ph);
          c.setAttribute('cy', sy + (ey - sy) * ph - Math.sin(Math.PI * ph) * 22);
          c.setAttribute('opacity', Math.sin(Math.PI * ph) * edge);
        });
      }
    }
    if (!fxShown) {
      ring.setAttribute('opacity', '0');
      parts.forEach(c => c.setAttribute('opacity', '0'));
    }

    if (e >= total) { text = 'Collection complete — truck returned to depot'; facing = 1; }
    if (text) setStatus(text);
    const bob = moving ? Math.sin(e / 45) * 1.2 : 0;
    truck.setAttribute('transform', 'translate(' + pos.x + ',' + (pos.y + bob) + ') scale(' + facing + ',1)');
  }

  const begin = performance.now();
  const tick = now => {
    const e = Math.max(0, Math.min(total, now - begin));
    render(e);
    if (e < total) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
})();
</script>
"""


def city_simulation(route, all_bins, message="", animate_route=False):
    """Render the city map. The animation runs entirely inside the browser."""
    depot_px = (map_x(DEPOT[0]), map_y(DEPOT[1]))
    path_pts = [depot_px] + [stop_point(b) for b in route] + ([depot_px] if route else [])
    path = " ".join(
        (f"M {p[0]} {p[1]}" if i == 0 else f"L {p[0]} {p[1]}") for i, p in enumerate(path_pts)
    )

    buildings = []
    for x, y, name, color in [
        (210, 55, "COLLEGE", "#dceafe"), (470, 55, "HOSPITAL", "#fee2e2"),
        (690, 160, "MARKET", "#fef3c7"), (270, 300, "PARK", "#dcfce7"),
        (590, 350, "RESIDENTIAL", "#ede9fe"), (70, 170, "STATION", "#e0f2fe"),
    ]:
        buildings.append(
            f'<g><rect x="{x}" y="{y}" width="115" height="58" rx="9" fill="{color}" stroke="#94a3b8"/>'
            f'<text x="{x+57}" y="{y+34}" text-anchor="middle" font-size="11" font-weight="700" fill="#334155">{name}</text></g>'
        )

    bin_svg = []
    for item in all_bins:
        fill = item["fill"]
        bx, by = map_x(item["x"]), map_y(item["y"])
        sid = safe_id(item["rfid"])
        bin_svg.append(
            f'<g transform="translate({bx-12},{by-24})">'
            f'<rect id="binrect-{sid}" x="0" y="0" width="24" height="27" rx="4" fill="{bin_color(fill)}" stroke="#334155" stroke-width="1.5"/>'
            f'<rect x="3" y="-4" width="18" height="5" rx="2" fill="#334155"/>'
            f'<text x="12" y="18" text-anchor="middle" font-size="10" fill="white">♻</text>'
            f'<text x="12" y="42" text-anchor="middle" font-size="10" font-weight="700" fill="#0f172a">{html.escape(item["rfid"][-3:])}</text>'
            f'<text id="fill-{sid}" x="12" y="54" text-anchor="middle" font-size="9" fill="#334155">{fill}%</text>'
            f'</g>'
        )

    truck = (
        f'<g id="truck" transform="translate({depot_px[0]},{depot_px[1]}) scale(1,1)">'
        f'<rect x="-19" y="-11" width="29" height="17" rx="4" fill="#f97316" stroke="#7c2d12" stroke-width="1.5"/>'
        f'<rect x="7" y="-7" width="16" height="13" rx="3" fill="#fb923c" stroke="#7c2d12"/>'
        f'<rect x="12" y="-5" width="8" height="5" rx="1" fill="#e0f2fe"/>'
        f'<circle cx="-10" cy="9" r="4" fill="#1e293b"/><circle cx="15" cy="9" r="4" fill="#1e293b"/></g>'
    )

    animation_js = ""
    if animate_route and route:
        segments, total_ms = build_timeline(route)
        animation_js = (
            ANIMATION_JS
            .replace("__SEGS__", json.dumps(segments).replace("</", "<\\/"))
            .replace("__TOTAL__", str(total_ms))
        )

    safe_message = html.escape(message)
    page = f"""
    <div style="background:linear-gradient(135deg,#e0f2fe,#f0fdf4);padding:12px;border-radius:16px;border:1px solid #cbd5e1">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:4px 8px 10px;font-family:Arial">
        <b style="color:#0f172a">🏙️ SMART CITY • LIVE COLLECTION MAP</b>
        <span style="font-size:12px;color:#475569">Illustrative grid • not GPS/real roads</span>
      </div>
      <svg viewBox="0 0 900 500" width="100%" role="img" aria-label="Illustrative city map with bins and collection truck">
        <rect width="900" height="500" rx="12" fill="#ecfdf5"/>
        <path d="M0 110 H900 M0 245 H900 M0 380 H900 M150 0 V500 M390 0 V500 M630 0 V500 M835 0 V500" stroke="#cbd5e1" stroke-width="34" fill="none"/>
        <path d="M0 110 H900 M0 245 H900 M0 380 H900 M150 0 V500 M390 0 V500 M630 0 V500 M835 0 V500" stroke="#f8fafc" stroke-width="3" stroke-dasharray="12 12" fill="none"/>
        {''.join(buildings)}
        <path d="{path}" fill="none" stroke="#2563eb" stroke-width="5" stroke-dasharray="10 7" stroke-linecap="round" opacity="0.9"/>
        <g transform="translate({depot_px[0]},{depot_px[1]})">
          <rect x="-17" y="-17" width="34" height="34" rx="7" fill="#1d4ed8" stroke="white" stroke-width="3"/>
          <text x="0" y="5" text-anchor="middle" font-size="18">🏠</text>
          <text x="0" y="32" text-anchor="middle" font-size="11" font-weight="bold" fill="#1e3a8a">DEPOT</text>
        </g>
        {''.join(bin_svg)}
        <g id="fx"></g>
        {truck}
      </svg>
      <div style="font:12px Arial;color:#334155;padding:4px 8px">🟢 Below 50% &nbsp; 🟡 50–74% &nbsp; 🔴 75% or above &nbsp; 🔵 Optimized route</div>
      <div id="sim-status" style="font:bold 13px Arial;color:#1e3a8a;padding:4px 8px">{safe_message}</div>
    </div>
    {animation_js}
    """
    components.html(page, height=555, scrolling=False)


# ----------------------------------------------------------------------------
# State. The sidebar sliders ARE the bin fill levels, so emptying a bin simply
# sets its slider to 0 and everything on the page stays consistent.
# ----------------------------------------------------------------------------
for _b in BINS:
    st.session_state.setdefault(f'fill_{_b["rfid"]}', _b["fill"])
st.session_state.setdefault("threshold", 75)
st.session_state.setdefault("run_request", False)
st.session_state.setdefault("collection_status", "Ready to start")

# Apply the result of the previous run BEFORE the sliders are created.
for _rfid in st.session_state.pop("pending_zero", []):
    st.session_state[f"fill_{_rfid}"] = 0


def current_fills():
    return {b["rfid"]: int(st.session_state[f'fill_{b["rfid"]}']) for b in BINS}


def start_run():
    fills = current_fills()
    if not any(v >= st.session_state.threshold for v in fills.values()):
        st.session_state.collection_status = "No bins need collection"
        return
    st.session_state.run_start_fills = fills
    st.session_state.collection_status = "Collection in progress"
    st.session_state.run_request = True


def replay_run():
    snapshot = st.session_state.get("run_start_fills")
    if snapshot:
        for rfid, value in snapshot.items():
            st.session_state[f"fill_{rfid}"] = value
    start_run()


def reset_all():
    for b in BINS:
        st.session_state[f'fill_{b["rfid"]}'] = b["fill"]
    st.session_state.threshold = 75
    st.session_state.collection_status = "Ready to start"
    st.session_state.run_request = False
    st.session_state.pop("last_run", None)
    st.session_state.pop("run_start_fills", None)


locked = st.session_state.run_request  # True while the truck is driving

st.title("♻️ Smart Waste City Simulator")
st.caption("RFID bin identification • Simulated ultrasonic sensing • Nearest-neighbour route optimization")
st.info("Educational simulation only: RFID and ultrasonic readings are simulated. The map uses illustrative coordinates, not real roads, GPS, traffic, or municipal data.")

with st.sidebar:
    st.header("🎛️ Control Centre")
    threshold = st.slider("Collection threshold (%)", 0, 100, key="threshold", disabled=locked)
    st.divider()
    st.subheader("Simulated bin sensors")
    if locked:
        st.caption("🔒 Controls are locked while the truck is on its route.")
    fills = {}
    for item in BINS:
        fills[item["rfid"]] = st.slider(
            f'{item["rfid"]} · {item["location"]}', 0, 100,
            key=f'fill_{item["rfid"]}', disabled=locked,
        )
    st.button("↺ Reset sample data", on_click=reset_all, disabled=locked, use_container_width=True)


def build_records():
    return [
        {**item, "fill": fills[item["rfid"]], "gap": 100 - fills[item["rfid"]],
         "status": "COLLECT" if fills[item["rfid"]] >= threshold else "WAIT"}
        for item in BINS
    ]


records = build_records()
selected = [item for item in records if item["fill"] >= threshold]
route, optimized = nearest_neighbour_route(selected)
baseline = fixed_order_distance(selected)

a, b, c, d = st.columns(4)
a.metric("🗑️ Total bins", len(records))
b.metric("🚛 Bins to collect", len(selected))
c.metric("⏳ Bins waiting", len(records) - len(selected))
d.metric("🎯 Threshold", f"{threshold}%")

st.subheader("🗺️ City simulation")
st.caption("The truck stays at the depot until you start a collection run.")
start_col, replay_col, status_col = st.columns([1, 1, 2])
with start_col:
    st.button("▶ Start Collection", type="primary", on_click=start_run,
              disabled=locked, use_container_width=True)
with replay_col:
    st.button("↻ Replay", on_click=replay_run, disabled=locked, use_container_width=True)
with status_col:
    status_text = st.session_state.collection_status
    if status_text.startswith("Collection complete"):
        st.success(f"**Status:** {status_text}")
    else:
        st.info(f"**Status:** {status_text}")

if locked and route:
    st.info("🚛 Truck is on its route. Watch each bin's fill percentage drop as it is emptied.")
    city_simulation(route, records, message="Truck leaving depot", animate_route=True)
else:
    if not selected:
        st.info("No bins currently meet the collection threshold. The truck stays at the depot.")
    city_simulation(route, records, message=st.session_state.collection_status)

# ----------------------------------------------------------------------------
# Dashboard (drawn before the wait so it is never "stale" during the animation)
# ----------------------------------------------------------------------------
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

last_run = st.session_state.get("last_run")
shown_opt = optimized if route else (last_run["optimized"] if last_run else 0.0)
shown_base = baseline if route else (last_run["baseline"] if last_run else 0.0)
x, y, z = st.columns(3)
x.metric("Optimized distance", f"{shown_opt:.2f} grid units")
y.metric("Fixed-order distance", f"{shown_base:.2f} grid units")
if shown_base > 0:
    z.metric("Difference vs fixed order", f"{(shown_base - shown_opt) / shown_base * 100:.1f}%")
else:
    z.metric("Difference vs fixed order", "—")
if not route and last_run:
    st.caption("Distances shown are from the last completed run.")

if last_run:
    st.subheader("✅ Final bin-fill percentages (last run)")
    final_df = pd.DataFrame(last_run["rows"])
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
    st.write("The route distance is straight-line Euclidean distance on a demonstration grid. It is not real road distance and the heuristic does not guarantee the globally shortest route.")

st.caption("Project: Smart Solid Waste Collection Route Optimization Using RFID and Sensors")

# ----------------------------------------------------------------------------
# Finish the run. Placed LAST so the page above is fully drawn first. We wait for
# exactly the timeline length the browser is playing, then save the results and
# refresh once. Widgets are locked meanwhile, so nothing can interrupt the run.
# ----------------------------------------------------------------------------
if locked:
    if not route:
        st.session_state.run_request = False
        st.rerun()
    _, total_ms = build_timeline(route)
    finished = False
    try:
        time.sleep(total_ms / 1000 + END_BUFFER_S)
        finished = True
    finally:
        st.session_state.run_request = False  # never leave the controls locked

    if finished:
        collected = {r["rfid"] for r in route}
        rows = [
            {"RFID ID": r["rfid"], "Location": r["location"], "Before (%)": r["fill"],
             "After (%)": 0 if r["rfid"] in collected else r["fill"],
             "Collected": "Yes" if r["rfid"] in collected else "No"}
            for r in records
        ]
        st.session_state.last_run = {"rows": rows, "optimized": optimized, "baseline": baseline}
        st.session_state.pending_zero = list(collected)
        st.session_state.collection_status = "Collection complete — truck returned to depot"
        try:
            with RESULTS_CSV.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
        except OSError:
            pass  # saving to disk is optional; the on-page table and CSV download still work
        st.rerun()
