import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import paho.mqtt.client as mqtt

from Battery import battery_percent
from credentials import load_credentials

BROKER, PORT, TOPIC = "ra-net.contigo.com", 7008, "/cell/#"
WEB_HOST, WEB_PORT = "127.0.0.1", 8080
ids = (sys.argv[1] if len(sys.argv) > 1 else "30AE7BE844CF").upper().replace(" ", "").replace(":", "").replace("'", "").split(",")
rows, lock, status = [], threading.Lock(), "Connecting..."

HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>BLE Dashboard</title>
<style>
body { font-family: system-ui, sans-serif; margin: 24px; background: #f5f5f5; }
h1 { margin-bottom: 4px; }
.meta { color: #666; margin-bottom: 16px; }
table { width: 100%; border-collapse: collapse; background: #fff; }
th, td { border: 1px solid #ddd; padding: 10px 12px; text-align: left; }
th { background: #333; color: #fff; }
tr:nth-child(even) { background: #fafafa; }
code { font-family: Consolas, monospace; font-size: 0.85rem; word-break: break-all; }
.empty { color: #888; font-style: italic; }
</style></head>
<body>
<h1>BLE Dashboard</h1>
<p class="meta" id="status">Loading…</p>
<table>
<thead><tr><th>ble_addr</th><th>data</th><th>battery</th></tr></thead>
<tbody id="rows"><tr><td colspan="3" class="empty">Waiting for MQTT messages…</td></tr></tbody>
</table>
<script>
function esc(s) {
  return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
}
async function refresh() {
  const r = await fetch('/api/rows');
  const d = await r.json();
  document.getElementById('status').textContent = d.status + '  |  rows: ' + d.rows.length;
  const body = document.getElementById('rows');
  if (!d.rows.length) {
    body.innerHTML = '<tr><td colspan="3" class="empty">Waiting for MQTT messages…</td></tr>';
    return;
  }
  body.innerHTML = d.rows.map(x =>
    '<tr><td><code>' + esc(x.ble_addr) + '</code></td><td><code>' + esc(x.data) + '</code></td><td>' + esc(x.battery) + '</td></tr>'
  ).join('');
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


def on_connect(client, userdata, flags, reason_code, properties):
    global status
    status = "Connected to Dusun Production farm"
    print(status)
    client.subscribe(TOPIC)


def on_message(client, userdata, msg):
    payload = msg.payload.decode(errors="replace")
    if not any(i in payload.upper().replace(":", "") for i in ids if i):
        return
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return
    new_rows = []
    for d in device_list(parsed):
        if not (d.get("ble_addr") or d.get("data")):
            continue
        data = str(d.get("data") or "")
        bat = battery_percent(data, d.get("modelstr"))
        if bat is None:
            continue
        new_rows.append({
            "ble_addr": str(d.get("ble_addr") or ""),
            "data": data,
            "battery": f"{bat}%",
        })
    if not new_rows:
        return
    with lock:
        rows.extend(new_rows)
        del rows[:-1000]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/api/rows":
            with lock:
                body = json.dumps({"status": status, "rows": list(reversed(rows))}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
user, password = load_credentials()
client.username_pw_set(user, password)
client.on_connect = on_connect
client.on_message = on_message
client.connect(BROKER, PORT)
client.loop_start()
print(f"Dashboard: http://{WEB_HOST}:{WEB_PORT}")
print("CFC filter:", ids)
HTTPServer((WEB_HOST, WEB_PORT), Handler).serve_forever()
