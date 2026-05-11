"""
tracking_server.py — QR scan tracking server with Google review redirect.

When a customer scans a store's QR code, their phone hits:
    GET /r/<STORE_CODE>

This server:
  1. Logs: store_code, timestamp, IP address, city (from IP), device type, browser
  2. Redirects the customer to the Google review page for that store

All scan data is saved to scan_logs.csv — open it in Excel or Google Sheets
to analyse how many scans each store is getting and when.

Requirements:
    pip install flask flask-cors requests user-agents

Deploy options:
    A. Local / LAN only:
       python tracking_server.py
       # Access at http://localhost:5000  (or your LAN IP)

    B. Free public hosting (Railway):
       1. Push this file to a GitHub repo
       2. Connect to Railway.app → Deploy → it auto-runs tracking_server.py
       3. Copy the Railway URL → paste into generate_qr.py --base-url

    C. Free public hosting (Render):
       Same as Railway; add a Procfile with:  web: python tracking_server.py

Usage:
    python tracking_server.py
    python tracking_server.py --port 8080
    python tracking_server.py --host 0.0.0.0 --port 5000

Dashboard:
    GET /dashboard         → Browser dashboard (HTML)
    GET /api/scans         → JSON: all scan logs
    GET /api/scans/<CODE>  → JSON: scans for one store
    GET /api/summary       → JSON: per-store scan counts
"""

import argparse
import csv
import json
import os
import re
from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, render_template_string, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCAN_LOG_FILE = "scan_logs.csv"
STORES_FILE   = "stores_detailed.csv"

def _load_place_ids() -> dict:
    """
    Load Place IDs automatically — no manual entry needed.
    Priority:
      1. stores_detailed.csv  (has place_id column after running get_stores.py)
      2. place_ids.json       (also written by get_stores.py as a quick-lookup file)
    Falls back to empty dict — redirects still work via Maps search fallback.
    """
    # Try stores_detailed.csv first
    try:
        with open(STORES_FILE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        ids = {r["storeCode"]: r["place_id"] for r in rows if r.get("place_id")}
        if ids:
            return ids
    except (FileNotFoundError, KeyError):
        pass

    # Fall back to place_ids.json
    try:
        with open("place_ids.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        pass

    return {}


# Loaded once at startup
PLACE_IDS = _load_place_ids()

LOG_FIELDS = [
    "id", "timestamp", "store_code", "store_name",
    "ip", "city", "country", "device", "browser", "os", "user_agent",
]

# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)


# ---------------------------------------------------------------------------
# Store loader
# ---------------------------------------------------------------------------
def _load_stores() -> dict:
    stores = {}
    try:
        with open(STORES_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                stores[row["storeCode"]] = row
    except FileNotFoundError:
        pass
    return stores


# ---------------------------------------------------------------------------
# Review URL builder
# ---------------------------------------------------------------------------
def _review_url(store_code: str, store: dict) -> str:
    if store_code in PLACE_IDS:
        return f"https://search.google.com/local/writereview?placeid={PLACE_IDS[store_code]}"
    name  = store.get("title", "Citykart")
    city  = store.get("city", "")
    query = f"{name} {city}".replace(" ", "+")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


# ---------------------------------------------------------------------------
# IP → city lookup (free, no API key needed)
# ---------------------------------------------------------------------------
def _geo_from_ip(ip: str) -> tuple:
    """Return (city, country) from IP using free ip-api.com service."""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return "Local", "Local"
    try:
        import requests as req
        r = req.get(f"http://ip-api.com/json/{ip}?fields=city,country", timeout=2)
        if r.status_code == 200:
            data = r.json()
            return data.get("city", "Unknown"), data.get("country", "Unknown")
    except Exception:
        pass
    return "Unknown", "Unknown"


# ---------------------------------------------------------------------------
# Device/browser detection
# ---------------------------------------------------------------------------
def _parse_ua(ua_string: str) -> tuple:
    """Return (device_type, browser, os) from User-Agent string."""
    try:
        from user_agents import parse as ua_parse
        ua = ua_parse(ua_string)
        device  = "Mobile" if ua.is_mobile else ("Tablet" if ua.is_tablet else "Desktop")
        browser = ua.browser.family
        os_name = ua.os.family
        return device, browser, os_name
    except Exception:
        return "Unknown", "Unknown", "Unknown"


# ---------------------------------------------------------------------------
# Scan logger
# ---------------------------------------------------------------------------
def _next_id() -> int:
    if not os.path.exists(SCAN_LOG_FILE):
        return 1
    with open(SCAN_LOG_FILE, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return len(rows) + 1


def _log_scan(store_code: str, store_name: str, ip: str, ua_string: str):
    city, country      = _geo_from_ip(ip)
    device, browser, os_name = _parse_ua(ua_string)
    scan_id            = _next_id()
    timestamp          = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    row = {
        "id":         scan_id,
        "timestamp":  timestamp,
        "store_code": store_code,
        "store_name": store_name,
        "ip":         ip,
        "city":       city,
        "country":    country,
        "device":     device,
        "browser":    browser,
        "os":         os_name,
        "user_agent": ua_string[:200],
    }

    file_exists = os.path.exists(SCAN_LOG_FILE)
    with open(SCAN_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    return row


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return jsonify({
        "service": "Citykart QR Tracking Server",
        "status":  "running",
        "routes": {
            "/r/<store_code>":    "QR redirect (scans tracked here)",
            "/dashboard":         "Visual scan dashboard",
            "/api/summary":       "JSON scan counts per store",
            "/api/scans":         "JSON all scan logs",
            "/api/scans/<code>":  "JSON scans for one store",
        },
    })


@app.route("/r/<store_code>")
def qr_redirect(store_code):
    """
    Main QR landing route. Logs the scan and redirects to Google review page.
    """
    stores     = _load_stores()
    code_upper = store_code.upper()
    store      = stores.get(code_upper)

    if not store:
        # Unknown store code — redirect to generic Citykart Google search
        return redirect("https://www.google.com/search?q=Citykart+reviews", 302)

    # Get client info
    ip        = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()   # first IP if behind proxy
    ua_string = request.headers.get("User-Agent", "")

    # Log the scan
    scan = _log_scan(code_upper, store["title"], ip, ua_string)
    print(f"  SCAN #{scan['id']}  {code_upper}  {scan['city']}, {scan['country']}  [{scan['device']}]")

    # Redirect to Google review page
    review_url = _review_url(code_upper, store)
    return redirect(review_url, 302)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.route("/api/summary")
def api_summary():
    """Per-store scan counts."""
    stores  = _load_stores()
    summary = {code: {"name": s["title"], "city": s["city"], "scans": 0}
               for code, s in stores.items()}

    if os.path.exists(SCAN_LOG_FILE):
        with open(SCAN_LOG_FILE, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                code = row["store_code"]
                if code in summary:
                    summary[code]["scans"] += 1
                else:
                    summary[code] = {"name": row["store_name"], "city": "", "scans": 1}

    return jsonify(summary)


@app.route("/api/scans")
def api_scans_all():
    """All scan logs."""
    if not os.path.exists(SCAN_LOG_FILE):
        return jsonify([])
    with open(SCAN_LOG_FILE, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return jsonify(rows)


@app.route("/api/scans/<store_code>")
def api_scans_store(store_code):
    """Scans for a specific store."""
    if not os.path.exists(SCAN_LOG_FILE):
        return jsonify([])
    with open(SCAN_LOG_FILE, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["store_code"] == store_code.upper()]
    return jsonify(rows)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Citykart QR Scan Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #f1f5f9; color: #1e293b; }
  header { background: #7c3aed; color: white; padding: 20px 32px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 22px; }
  header p  { font-size: 13px; opacity: .8; }
  .container { max-width: 1100px; margin: 32px auto; padding: 0 20px; }
  .kpi-row   { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 28px; }
  .kpi       { background: white; border-radius: 12px; padding: 20px 24px; box-shadow: 0 1px 4px rgba(0,0,0,.06); }
  .kpi .val  { font-size: 36px; font-weight: 700; color: #7c3aed; }
  .kpi .lbl  { font-size: 13px; color: #64748b; margin-top: 4px; }
  table      { width: 100%; border-collapse: collapse; background: white; border-radius: 12px;
               overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.06); margin-bottom: 28px; }
  th         { background: #7c3aed; color: white; padding: 12px 16px; text-align: left; font-size: 13px; }
  td         { padding: 12px 16px; border-bottom: 1px solid #f1f5f9; font-size: 14px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td      { background: #faf5ff; }
  .badge     { display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 12px; font-weight: 600; }
  .badge-m   { background: #dbeafe; color: #1d4ed8; }
  .badge-d   { background: #f0fdf4; color: #166534; }
  .badge-t   { background: #fef9c3; color: #854d0e; }
  h2         { font-size: 17px; font-weight: 600; margin-bottom: 12px; color: #374151; }
  .refresh   { float: right; font-size: 12px; color: #94a3b8; }
  @media (max-width: 640px) { .kpi-row { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>
<header>
  <div>
    <h1>📸 Citykart — QR Scan Tracker</h1>
    <p>Live dashboard · auto-refreshes every 30 seconds</p>
  </div>
</header>
<div class="container">
  <div id="kpis" class="kpi-row"></div>
  <h2>Per-Store Scan Counts <span class="refresh" id="last-refresh"></span></h2>
  <table id="store-table">
    <thead><tr><th>Store</th><th>City</th><th>Total Scans</th><th>Last Scan</th></tr></thead>
    <tbody id="store-body"></tbody>
  </table>
  <h2>Recent Scans (last 50)</h2>
  <table id="scan-table">
    <thead><tr><th>#</th><th>Time</th><th>Store</th><th>City</th><th>Device</th><th>Browser</th><th>OS</th></tr></thead>
    <tbody id="scan-body"></tbody>
  </table>
</div>
<script>
async function load() {
  const [sumResp, scanResp] = await Promise.all([
    fetch('/api/summary'), fetch('/api/scans')
  ]);
  const summary = await sumResp.json();
  const scans   = await scanResp.json();

  // KPIs
  const totalScans  = scans.length;
  const storeCodes  = Object.keys(summary);
  const storesWithScans = storeCodes.filter(c => summary[c].scans > 0).length;
  const todayStr    = new Date().toISOString().slice(0,10);
  const todayScans  = scans.filter(s => s.timestamp && s.timestamp.startsWith(todayStr)).length;
  const mobileScans = scans.filter(s => s.device === 'Mobile').length;

  document.getElementById('kpis').innerHTML = `
    <div class="kpi"><div class="val">${totalScans}</div><div class="lbl">Total Scans</div></div>
    <div class="kpi"><div class="val">${todayScans}</div><div class="lbl">Scans Today</div></div>
    <div class="kpi"><div class="val">${storesWithScans}/${storeCodes.length}</div><div class="lbl">Active Stores</div></div>
    <div class="kpi"><div class="val">${totalScans ? Math.round(mobileScans/totalScans*100) : 0}%</div><div class="lbl">Mobile Scans</div></div>
  `;

  // Per-store table
  // find last scan per store
  const lastScan = {};
  scans.forEach(s => { lastScan[s.store_code] = s.timestamp; });

  const storeRows = storeCodes.sort((a,b) => (summary[b].scans - summary[a].scans))
    .map(code => `
      <tr>
        <td><b>${summary[code].name}</b> <small style="color:#94a3b8">(${code})</small></td>
        <td>${summary[code].city || '—'}</td>
        <td style="font-weight:700;color:#7c3aed">${summary[code].scans}</td>
        <td style="color:#64748b;font-size:13px">${lastScan[code] || '—'}</td>
      </tr>`).join('');
  document.getElementById('store-body').innerHTML = storeRows;

  // Recent scans
  const recent = [...scans].reverse().slice(0, 50);
  const deviceBadge = d => {
    if (d==='Mobile') return `<span class="badge badge-m">📱 Mobile</span>`;
    if (d==='Tablet') return `<span class="badge badge-t">📲 Tablet</span>`;
    return `<span class="badge badge-d">💻 Desktop</span>`;
  };
  const scanRows = recent.map(s => `
    <tr>
      <td style="color:#94a3b8">#${s.id}</td>
      <td style="font-size:13px">${s.timestamp}</td>
      <td><b>${s.store_code}</b></td>
      <td>${s.city || '—'}, ${s.country || ''}</td>
      <td>${deviceBadge(s.device)}</td>
      <td style="font-size:13px">${s.browser || '—'}</td>
      <td style="font-size:13px">${s.os || '—'}</td>
    </tr>`).join('');
  document.getElementById('scan-body').innerHTML = scanRows || '<tr><td colspan="7" style="text-align:center;color:#94a3b8;padding:32px">No scans yet — share the QR codes!</td></tr>';

  document.getElementById('last-refresh').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

load();
setInterval(load, 30000);
</script>
</body>
</html>
"""

@app.route("/dashboard")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Citykart QR tracking server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    print(f"\n🟣  Citykart QR Tracking Server")
    print(f"    Redirect route : http://{args.host}:{args.port}/r/<STORE_CODE>")
    print(f"    Dashboard       : http://localhost:{args.port}/dashboard")
    print(f"    Scan log file   : {SCAN_LOG_FILE}")
    print(f"\n    Press Ctrl+C to stop.\n")

    app.run(host=args.host, port=args.port, debug=args.debug)
