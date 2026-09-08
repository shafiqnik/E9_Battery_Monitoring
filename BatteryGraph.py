import json
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import paho.mqtt.client as mqtt

from Battery import battery_percent

BROKER, PORT, TOPIC = "ra-net.contigo.com", 7008, "/cell/#"
WEB_HOST = os.environ.get("E9_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("E9_WEB_PORT", "8081"))
LOG = "batteryLevel.txt"
ANOMALY_LOG = "anomalyEvents.txt"
OFFLINE_LOG = "offline_tags.txt"
LOW_BATTERY = 64
ONLINE_SEC = 60
OFFLINE_MIN_SEC = 60
ids = (sys.argv[1] if len(sys.argv) > 1 else "30AE7BE844CF").upper().replace(" ", "").replace(":", "").replace("'", "").split(",")

lock = threading.Lock()
status = "Connecting..."
started = time.monotonic()
history = defaultdict(list)
latest = {}
live = {}
anomalies = set()
replacements = set()
anomaly_events = []
event_keys = set()
presence = {}
offline_dirty = False

HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Battery Telemetry</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
body { margin: 0; font-family: Segoe UI, system-ui, sans-serif; background: #07111c; color: #e8f1f8; }
.wrap { max-width: 1200px; margin: 0 auto; padding: 24px; }
.kicker { color: #7ec8e3; letter-spacing: 2px; font-size: 12px; }
h1 { margin: 4px 0 16px; font-size: 22px; font-weight: 600; }
.meta { color: #8aa0b5; font-size: 13px; margin-bottom: 18px; }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 18px; }
.card { background: #102033; border: 1px solid #1e3a57; border-radius: 8px; padding: 16px; text-align: center; }
.card .n { font-size: 36px; font-weight: 700; margin: 6px 0; }
.card .l { color: #8aa0b5; font-size: 13px; }
.card .hint { color: #8aa0b5; font-size: 11px; margin-top: 6px; }
.card.clickable { cursor: pointer; }
.card.clickable:hover, .card.clickable.open { border-color: #e74c3c; }
.clickable { cursor: pointer; }
tr.clickable:hover { background: #1a324c; }
.step.clickable:hover { border-color: #7ec8e3; }
.flow .n { font-size: 22px; font-weight: 700; margin: 6px 0; }
a { color: #7ec8e3; font-size: 12px; }
.ok { color: #2ecc71; } .warn { color: #f1c40f; } .bad { color: #e74c3c; } .info { color: #7ec8e3; }
.panel { background: #102033; border: 1px solid #1e3a57; border-radius: 8px; padding: 16px 20px; margin-bottom: 18px; }
.panel h2 { margin: 0; font-size: 14px; color: #7ec8e3; letter-spacing: 1px; }
.panel-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.clock { color: #d5e4f0; font-size: 12px; line-height: 1.4; font-variant-numeric: tabular-nums; letter-spacing: 0.2px; }
.clock .sep { color: #6b8aa8; margin: 0 8px; }
.chart-box { position: relative; height: 260px; max-height: 260px; }
.table-box { max-height: 220px; overflow: auto; }
.legend { display: flex; gap: 20px; font-size: 12px; color: #c5d4e0; margin-top: 8px; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { border-bottom: 1px solid #1e3a57; padding: 8px; text-align: left; }
th { color: #7ec8e3; }
code { font-family: Consolas, monospace; }
.flow { display: flex; gap: 10px; align-items: stretch; }
.step { flex: 1; background: #0b1a2b; border: 1px solid #1e3a57; border-radius: 8px; padding: 12px; text-align: center; }
.step .t { font-weight: 600; margin-bottom: 4px; }
.step .d { color: #8aa0b5; font-size: 12px; }
.arrow { color: #7ec8e3; align-self: center; font-size: 22px; }
</style></head>
<body><div class="wrap">
<div class="kicker">MONITORING DASHBOARD</div>
<h1>Battery Telemetry &amp; Anomaly Detection</h1>
<p class="meta" id="status">Loading…</p>
<div class="cards">
  <div class="card"><div class="n info" id="total">0</div><div class="l">Total Tags</div></div>
  <div class="card"><div class="n ok" id="online">0</div><div class="l">Online</div></div>
  <div class="card"><div class="n warn" id="low">0</div><div class="l">Low Battery</div></div>
  <div class="card clickable" id="anomaly-card" role="button" tabindex="0">
    <div class="n bad" id="anomaly">0</div>
    <div class="l">Anomaly Detected</div>
    <div class="hint">click for details</div>
  </div>
</div>
<div class="panel" id="anomaly-panel" hidden>
  <div class="panel-head">
    <h2>ANOMALY DETAILS</h2>
    <div class="clock"><span id="anomaly-summary">0 shown</span><span class="sep">·</span><a href="#" id="anomaly-all">Show all</a></div>
  </div>
  <p class="meta" id="anomaly-empty">No anomalies yet. Flags stay set when new readings arrive.</p>
  <div class="table-box"><table>
    <thead><tr><th>when</th><th>ble_addr</th><th>previous</th><th>new</th><th>cause</th></tr></thead>
    <tbody id="anomaly-rows"></tbody>
  </table></div>
</div>
<div class="panel">
  <div class="panel-head">
    <h2>BATTERY LEVEL OVER TIME</h2>
    <div class="clock"><span id="now">—</span><span class="sep">·</span>Uptime <span id="uptime">00:00:00</span></div>
  </div>
  <div class="chart-box"><canvas id="chart"></canvas></div>
  <div class="legend">
    <span><i class="dot" style="background:#e74c3c"></i>ANOMALY / Battery replacement</span>
    <span><i class="dot" style="background:#2ecc71"></i>EXPECTED monotonic decline</span>
  </div>
</div>
<div class="panel">
  <h2>BLE MAC IDs</h2>
  <div class="table-box"><table>
    <thead><tr><th>ble_addr</th><th>battery</th><th>last seen</th><th>status</th><th>duration</th></tr></thead>
    <tbody id="tags"></tbody>
  </table></div>
</div>
<div class="panel">
  <div class="panel-head">
    <h2>NOT REPORTING / NOT SCANNED</h2>
    <div class="clock"><span id="missing-count">0 tags</span></div>
  </div>
  <p class="meta" id="missing-empty">All known tags are reporting.</p>
  <div class="table-box"><table>
    <thead><tr><th>ble_addr</th><th>last seen</th><th>age</th><th>last battery</th><th>reason</th></tr></thead>
    <tbody id="missing"></tbody>
  </table></div>
</div>
<div class="panel">
  <div class="panel-head">
    <h2>OFFLINE TRACKING</h2>
    <div class="clock"><span id="offline-count">0 tags</span></div>
  </div>
  <p class="meta" id="offline-empty">No live tags yet. Outages over 1 minute are written to offline_tags.txt.</p>
  <div class="table-box"><table>
    <thead><tr><th>ble_addr</th><th>offline since</th><th>times offline &gt;1 min</th><th>how often</th></tr></thead>
    <tbody id="offline-rows"></tbody>
  </table></div>
</div>
<div class="panel">
  <h2>BATTERY REPLACEMENT DETECTION</h2>
  <div class="flow">
    <div class="step clickable" data-kind="drop_zero"><div class="t bad">Drop to 0%</div><div class="n bad" id="flow-zero">0</div><div class="d">Anomaly triggered</div></div>
    <div class="arrow">→</div>
    <div class="step"><div class="t">Currently at 0%</div><div class="n warn" id="flow-at-zero">0</div><div class="d">Still reporting empty</div></div>
    <div class="arrow">→</div>
    <div class="step clickable" data-kind="replacement"><div class="t ok">Return to 100%</div><div class="n ok" id="flow-repl">0</div><div class="d">Replacement confirmed</div></div>
    <div class="arrow">→</div>
    <div class="step clickable" data-kind=""><div class="t ok">Event recorded</div><div class="n info" id="flow-recorded">0</div><div class="d">Click for audit trail</div></div>
  </div>
</div>
</div>
<script>
const palette = ['#2ecc71','#27ae60','#1abc9c','#3498db','#5dade2','#58d68d'];
let chart, last;
let showAnomalies = false, filterMac = '', filterKind = '';
function esc(s){return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');}
function renderEvents(d){
  const panel = document.getElementById('anomaly-panel');
  panel.hidden = !showAnomalies;
  document.getElementById('anomaly-card').classList.toggle('open', showAnomalies);
  const rows = (d.events || []).filter(e =>
    (!filterMac || e.ble_addr === filterMac) && (!filterKind || e.kind === filterKind)
  ).slice().reverse();
  const label = filterMac || filterKind || 'all events';
  document.getElementById('anomaly-summary').textContent = rows.length + ' shown · ' + label;
  document.getElementById('anomaly-empty').hidden = rows.length > 0;
  document.getElementById('anomaly-rows').innerHTML = rows.map(e =>
    '<tr><td>'+esc(e.when)+'</td><td><code>'+esc(e.ble_addr)+'</code></td><td>'+e.prev+'%</td><td>'+e.battery+'%</td><td>'+esc(e.reason)+'</td></tr>'
  ).join('');
}
function openAnomalies(mac, kind){
  showAnomalies = true;
  filterMac = mac || '';
  filterKind = kind || '';
  if (last) renderEvents(last);
  document.getElementById('anomaly-panel').scrollIntoView({block:'nearest'});
}
document.getElementById('anomaly-card').onclick = () => {
  if (showAnomalies && !filterMac && !filterKind) {
    showAnomalies = false;
    if (last) renderEvents(last);
  } else openAnomalies('', '');
};
document.getElementById('anomaly-card').onkeydown = (ev) => {
  if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); document.getElementById('anomaly-card').click(); }
};
document.getElementById('anomaly-all').onclick = (ev) => { ev.preventDefault(); openAnomalies('', ''); };
document.getElementById('tags').onclick = (ev) => {
  const tr = ev.target.closest('tr');
  if (!tr || (tr.dataset.state !== 'Anomaly' && tr.dataset.state !== 'Replacement')) return;
  openAnomalies(tr.dataset.mac, '');
};
document.querySelectorAll('.step[data-kind]').forEach(el => {
  el.onclick = () => openAnomalies('', el.dataset.kind);
});
async function refresh(){
  const d = last = await (await fetch('/api/graph')).json();
  document.getElementById('status').textContent = d.status + '  |  live MQTT';
  document.getElementById('now').textContent = d.now;
  document.getElementById('uptime').textContent = d.uptime;
  document.getElementById('total').textContent = d.cards.total;
  document.getElementById('online').textContent = d.cards.online;
  document.getElementById('low').textContent = d.cards.low;
  document.getElementById('anomaly').textContent = d.cards.anomaly;
  document.getElementById('flow-zero').textContent = d.flow.drop_zero;
  document.getElementById('flow-at-zero').textContent = d.flow.at_zero;
  document.getElementById('flow-repl').textContent = d.flow.replacement;
  document.getElementById('flow-recorded').textContent = d.flow.recorded;
  renderEvents(d);
  document.getElementById('tags').innerHTML = d.tags.map(t => {
    const flagged = t.state === 'Anomaly' || t.state === 'Replacement';
    return '<tr data-mac="'+esc(t.ble_addr)+'" data-state="'+esc(t.state)+'" class="'+(flagged?'clickable':'')+'"><td><code>'+esc(t.ble_addr)+'</code></td><td>'+t.battery+'%</td><td>'+esc(t.last_seen)+'</td><td>'+esc(t.state)+'</td><td>'+esc(t.duration || '—')+'</td></tr>';
  }).join('') || '<tr><td colspan="5">Waiting for E9 battery frames…</td></tr>';
  const missing = d.missing || [];
  document.getElementById('missing-count').textContent = missing.length + ' tags';
  document.getElementById('missing-empty').hidden = missing.length > 0;
  document.getElementById('missing').innerHTML = missing.map(t =>
    '<tr><td><code>'+esc(t.ble_addr)+'</code></td><td>'+esc(t.last_seen)+'</td><td>'+esc(t.age)+'</td><td>'+esc(t.battery)+'</td><td class="'+(t.reason === "Hasn't reported" ? 'bad' : 'warn')+'">'+esc(t.reason)+'</td></tr>'
  ).join('');
  const offline = d.offline || [];
  const down = offline.filter(t => t.offline_since !== 'online').length;
  document.getElementById('offline-count').textContent = down + ' offline · ' + offline.length + ' tracked';
  document.getElementById('offline-empty').hidden = offline.length > 0;
  document.getElementById('offline-rows').innerHTML = offline.map(t => {
    const downNow = t.offline_since !== 'online';
    return '<tr><td><code>'+esc(t.ble_addr)+'</code></td><td class="'+(downNow?'bad':'ok')+'">'+esc(t.offline_since)+'</td><td>'+t.times_over_1min+'</td><td>'+esc(t.how_often)+'</td></tr>';
  }).join('');
  const datasets = d.series.map((s,i) => ({
    label: s.mac,
    data: s.data,
    borderColor: s.kind === 'anomaly' ? '#e74c3c' : palette[i % palette.length],
    backgroundColor: 'transparent',
    borderWidth: s.kind === 'anomaly' ? 2.5 : 2,
    pointRadius: 2,
    spanGaps: true,
    tension: 0.15
  }));
  const cfg = {
    type: 'line',
    data: { labels: d.labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { title: { display: true, text: 'Time', color: '#8aa0b5' }, ticks: { color: '#8aa0b5', maxTicksLimit: 10 }, grid: { color: '#1e3a57' } },
        y: { min: 0, max: 100, title: { display: true, text: 'Battery (%)', color: '#8aa0b5' }, ticks: { color: '#8aa0b5' }, grid: { color: '#1e3a57' } }
      }
    }
  };
  if (!chart) chart = new Chart(document.getElementById('chart'), cfg);
  else { chart.data.labels = d.labels; chart.data.datasets = datasets; chart.update('none'); }
}
refresh();
setInterval(refresh, 1000);
</script>
</body></html>
"""


def device_list(obj):
    if isinstance(obj, dict):
        lst = obj.get("device_list")
        if isinstance(lst, list):
            return lst
        for v in obj.values():
            found = device_list(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = device_list(v)
            if found:
                return found
    return []


def record_event(mac, battery, when, data, prev, kind, reason, persist):
    ev = {
        "when": when.strftime("%Y-%m-%d %H:%M:%S"),
        "ble_addr": mac,
        "prev": prev,
        "battery": battery,
        "kind": kind,
        "reason": reason,
        "data": data,
    }
    key = (ev["when"], mac, prev, battery, kind)
    if key in event_keys:
        return
    event_keys.add(key)
    anomaly_events.append(ev)
    anomalies.add(mac)
    if kind == "replacement":
        replacements.add(mac)
    if persist:
        line = "\t".join([
            ev["when"], mac, str(prev), str(battery), kind,
            reason.replace("\t", " "), str(data).replace("\t", " "),
        ]) + "\n"
        with open(ANOMALY_LOG, "a", encoding="utf-8") as f:
            f.write(line)


def add_point(mac, battery, when, data="", persist=False):
    t = when.strftime("%H:%M:%S")
    prev = latest.get(mac, {}).get("battery")
    if prev is not None:
        if battery == 0 and prev > 0:
            record_event(mac, battery, when, data, prev, "drop_zero",
                         f"Dropped from {prev}% to 0%", persist)
        elif prev - battery >= 30:
            record_event(mac, battery, when, data, prev, "sudden_drop",
                         f"Sudden drop from {prev}% to {battery}% (≥30%)", persist)
        if prev < 100 and battery == 100:
            record_event(mac, battery, when, data, prev, "replacement",
                         f"Returned to 100% from {prev}% (battery replacement)", persist)
    latest[mac] = {"battery": battery, "t": t, "ts": when.timestamp(), "data": data}
    if persist:
        touch_presence(mac, when)
    pts = history[mac]
    if pts and pts[-1]["battery"] == battery and pts[-1]["t"] == t:
        return
    pts.append({"t": t, "battery": battery})
    del pts[:-200]


def load_anomalies():
    if not os.path.isfile(ANOMALY_LOG):
        return
    with open(ANOMALY_LOG, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 6:
                continue
            when_s, mac, prev_s, bat_s, kind, reason = parts[:6]
            data = parts[6] if len(parts) > 6 else ""
            try:
                when = datetime.strptime(when_s, "%Y-%m-%d %H:%M:%S")
                prev_i, bat_i = int(prev_s), int(bat_s)
            except ValueError:
                continue
            record_event(mac, bat_i, when, data, prev_i, kind, reason, persist=False)


def load_log():
    if not os.path.isfile(LOG):
        return
    with open(LOG, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) < 4:
                continue
            try:
                when = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
                bat = int(parts[3].replace("%", ""))
            except ValueError:
                continue
            add_point(parts[1], bat, when, parts[2], persist=False)


def format_uptime(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    clock = f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{days}d {clock}" if days else clock


def age_label(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    mins, sec = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m {sec:02d}s ago"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins:02d}m ago"


def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {mins:02d}m"
    if hours:
        return f"{hours}h {mins:02d}m {secs:02d}s"
    if mins:
        return f"{mins}m {secs:02d}s"
    return f"{secs}s"


def parse_ts(value):
    if not value or value in ("online", "—", "-", "None"):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def fmt_ts(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def mark_offline_dirty():
    global offline_dirty
    offline_dirty = True


def how_often_label(rec, now):
    n = rec["offline_gt_1min"]
    elapsed = max(0.0, now - rec["first_seen"])
    if n <= 0:
        return "never"
    if elapsed < 1:
        return f"{n} times"
    return f"{n} times in {format_duration(elapsed)} (avg every {format_duration(elapsed / n)})"


def new_presence(mac, ts, is_online=True):
    return {
        "last_seen": ts,
        "first_seen": ts,
        "is_online": is_online,
        "online_since": ts if is_online else None,
        "offline_since": None if is_online else ts,
        "offline_events": 0,
        "offline_gt_1min": 0,
        "counted_current": False,
    }


def touch_presence(mac, when):
    ts = when.timestamp()
    rec = presence.get(mac)
    if rec is None:
        presence[mac] = new_presence(mac, ts, is_online=True)
        mark_offline_dirty()
        return
    rec["last_seen"] = ts
    if rec["is_online"]:
        return
    rec["is_online"] = True
    rec["online_since"] = ts
    rec["offline_since"] = None
    rec["counted_current"] = False
    mark_offline_dirty()


def check_presence(now=None):
    now = datetime.now().timestamp() if now is None else now
    for rec in presence.values():
        if rec["is_online"]:
            if now - rec["last_seen"] <= ONLINE_SEC:
                continue
            rec["is_online"] = False
            rec["offline_since"] = rec["last_seen"]
            rec["online_since"] = None
            rec["offline_events"] += 1
            rec["counted_current"] = False
            mark_offline_dirty()
        if not rec["is_online"] and not rec["counted_current"]:
            since = rec["offline_since"] or rec["last_seen"]
            if now - since >= OFFLINE_MIN_SEC:
                rec["offline_gt_1min"] += 1
                rec["counted_current"] = True
                mark_offline_dirty()


def presence_duration(rec, now):
    if rec["is_online"] and rec["online_since"]:
        return "up " + format_duration(now - rec["online_since"])
    if rec["offline_since"]:
        return "down " + format_duration(now - rec["offline_since"])
    return "—"


def offline_rows(now):
    rows = []
    for mac, rec in presence.items():
        rows.append({
            "ble_addr": mac,
            "offline_since": "online" if rec["is_online"] else fmt_ts(rec["offline_since"]),
            "times_over_1min": rec["offline_gt_1min"],
            "how_often": how_often_label(rec, now),
        })
    rows.sort(key=lambda r: (r["offline_since"] == "online", r["ble_addr"]))
    return rows


def save_offline_tags():
    now = datetime.now().timestamp()
    check_presence(now)
    lines = [
        "ble_addr\toffline_since\ttimes_offline_over_1min\thow_often\tlast_seen\tfirst_seen\toffline_events\n"
    ]
    for mac, rec in sorted(presence.items()):
        offline_since = "online" if rec["is_online"] else fmt_ts(rec["offline_since"])
        lines.append("\t".join([
            mac,
            offline_since or "",
            str(rec["offline_gt_1min"]),
            how_often_label(rec, now),
            fmt_ts(rec["last_seen"]),
            fmt_ts(rec["first_seen"]),
            str(rec["offline_events"]),
        ]) + "\n")
    tmp = OFFLINE_LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, OFFLINE_LOG)


def load_offline_tags():
    if not os.path.isfile(OFFLINE_LOG):
        return
    with open(OFFLINE_LOG, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i == 0 and line.startswith("ble_addr"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4 or not parts[0].strip():
                continue
            mac = parts[0].strip()
            last_seen = parse_ts(parts[4]) if len(parts) > 4 else parse_ts(parts[1])
            first_seen = parse_ts(parts[5]) if len(parts) > 5 else last_seen
            if last_seen is None:
                continue
            try:
                gt1 = int(parts[2])
            except ValueError:
                gt1 = 0
            try:
                events = int(parts[6]) if len(parts) > 6 else gt1
            except ValueError:
                events = gt1
            is_online = parts[1] == "online"
            rec = new_presence(mac, last_seen, is_online=is_online)
            rec["first_seen"] = first_seen or last_seen
            rec["offline_gt_1min"] = max(0, gt1)
            rec["offline_events"] = max(0, events)
            rec["counted_current"] = not is_online
            if is_online:
                rec["online_since"] = last_seen
                rec["offline_since"] = None
            else:
                rec["online_since"] = None
                rec["offline_since"] = parse_ts(parts[1]) or last_seen
            presence[mac] = rec


def presence_loop():
    global offline_dirty
    while True:
        time.sleep(1)
        with lock:
            check_presence()
            if offline_dirty:
                save_offline_tags()
                offline_dirty = False


def note_live(mac, when, battery=None):
    row = live.setdefault(mac, {"scan_ts": 0, "bat_ts": None, "battery": None, "t": ""})
    row["scan_ts"] = when.timestamp()
    row["t"] = when.strftime("%H:%M:%S")
    if battery is not None:
        row["bat_ts"] = when.timestamp()
        row["battery"] = battery


def missing_tags(now):
    rows = []
    for mac, info in sorted(live.items()):
        scan_age = now - info["scan_ts"]
        bat_ts = info["bat_ts"]
        if bat_ts is not None and now - bat_ts <= ONLINE_SEC:
            continue
        scanned = scan_age <= ONLINE_SEC
        if scanned or bat_ts is None:
            reason = "Can't be scanned"
        else:
            reason = "Hasn't reported"
        rows.append({
            "ble_addr": mac,
            "last_seen": info["t"],
            "age": age_label(scan_age),
            "battery": "—" if info["battery"] is None else f"{info['battery']}%",
            "reason": reason,
        })
    return rows


def snapshot():
    now = datetime.now().timestamp()
    check_presence(now)
    tags = []
    for mac, info in sorted(latest.items()):
        if mac in replacements:
            state = "Replacement"
        elif mac in anomalies:
            state = "Anomaly"
        elif now - info["ts"] > ONLINE_SEC:
            state = "Offline"
        elif info["battery"] < LOW_BATTERY:
            state = "Low"
        else:
            state = "OK"
        rec = presence.get(mac)
        if rec:
            duration = presence_duration(rec, now)
        elif now - info["ts"] > ONLINE_SEC:
            duration = "down " + format_duration(now - info["ts"])
        else:
            duration = "—"
        tags.append({
            "ble_addr": mac,
            "battery": info["battery"],
            "last_seen": info["t"],
            "state": state,
            "duration": duration,
        })
    times = sorted({p["t"] for pts in history.values() for p in pts})
    series = []
    for mac, pts in sorted(history.items()):
        lookup = {p["t"]: p["battery"] for p in pts}
        series.append({
            "mac": mac,
            "kind": "anomaly" if mac in anomalies or mac in replacements else "expected",
            "data": [lookup.get(t) for t in times],
        })
    return {
        "status": status,
        "now": datetime.now().strftime("%A, %b %d, %Y  %H:%M:%S"),
        "uptime": format_uptime(time.monotonic() - started),
        "labels": times,
        "series": series,
        "tags": tags,
        "missing": missing_tags(now),
        "offline": offline_rows(now),
        "events": list(anomaly_events),
        "cards": {
            "total": len(latest),
            "online": sum(1 for v in latest.values() if now - v["ts"] <= ONLINE_SEC),
            "low": sum(1 for v in latest.values() if v["battery"] < LOW_BATTERY),
            "anomaly": len(anomalies | replacements),
        },
        "flow": {
            "drop_zero": sum(1 for e in anomaly_events if e["kind"] == "drop_zero"),
            "at_zero": sum(1 for v in latest.values() if v["battery"] == 0),
            "replacement": sum(1 for e in anomaly_events if e["kind"] == "replacement"),
            "recorded": len(anomaly_events),
        },
    }


def on_connect(client, userdata, flags, reason_code, properties):
    global status
    status = "Connected to Dusun Production farm"
    print(status)
    client.subscribe(TOPIC)


def log_battery(ble_addr, data, battery):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{ble_addr}\t{data}\t{battery}%\n"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line)


def on_message(client, userdata, msg):
    payload = msg.payload.decode(errors="replace")
    if not any(i in payload.upper().replace(":", "") for i in ids if i):
        return
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return
    now = datetime.now()
    with lock:
        for d in device_list(parsed):
            mac = str(d.get("ble_addr") or "")
            if not mac:
                continue
            data = str(d.get("data") or "")
            model = d.get("modelstr")
            bat = battery_percent(data, model)
            if bat is not None:
                add_point(mac, bat, now, data, persist=True)
                note_live(mac, now, battery=bat)
                if bat < 100:
                    log_battery(mac, data, bat)
            elif model == "Bledevice":
                note_live(mac, now)


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/api/graph":
            with lock:
                body = json.dumps(snapshot()).encode()
            ctype = "application/json"
        else:
            body = HTML.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(cfc_ids=None, open_browser=False):
    global ids, started
    if cfc_ids:
        ids = cfc_ids
    started = time.monotonic()
    load_anomalies()
    load_log()
    load_offline_tags()
    url = f"http://127.0.0.1:{WEB_PORT}" if WEB_HOST in ("0.0.0.0", "::") else f"http://{WEB_HOST}:{WEB_PORT}"
    server = Server((WEB_HOST, WEB_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=presence_loop, daemon=True).start()
    print(f"Battery graph: {url}  (bound {WEB_HOST}:{WEB_PORT})")
    print("CFC filter:", ids)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as exc:
            print(f"Could not open browser: {exc}")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set("contigo", "C0nt1g0")
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(BROKER, PORT)
    except Exception as exc:
        print(f"MQTT connect failed ({BROKER}:{PORT}): {exc}")
        raise
    client.loop_forever()


if __name__ == "__main__":
    run()
