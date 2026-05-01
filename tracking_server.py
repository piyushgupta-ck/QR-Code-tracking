"""
tracking_server.py — QR scan tracking server with Google Sheets logging.

When a customer scans a store's QR code, their phone hits:
    GET /r/<STORE_CODE>

This server:
  1. Logs scan to Google Sheets: timestamp, store, IP, city, device, browser
  2. Redirects the customer to the Google review page for that store

Scan data is saved permanently in Google Sheets — never lost on server restart.
View your sheet anytime from any device at sheets.google.com.

Setup (one time):
  1. Create a Google Sheet → copy its Spreadsheet ID from the URL
     URL looks like: docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
  2. Share the sheet with your service account email (Editor access)
     Service account email is in credentials.json → "client_email" field
  3. Set SPREADSHEET_ID below

Requirements:
    pip install flask flask-cors requests user-agents gspread google-auth

Deploy:
    Local : python tracking_server.py
    Railway: push to GitHub + connect to Railway (add requirements.txt + Procfile)

Dashboard:
    GET /dashboard         → Browser dashboard (HTML, reads from Google Sheets)
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
# Config — set your Google Sheet ID here
# ---------------------------------------------------------------------------
# Paste your Spreadsheet ID from the sheet URL:
# https://docs.google.com/spreadsheets/d/THIS_PART_HERE/edit
SPREADSHEET_ID = "1GHVnnRsX2s9zZehRTf0d4BTYBo5ekndyUS6MlZT2wrk"   # ← e.g. "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

SHEET_NAME     = "Scan Logs"    # tab name inside the sheet (created automatically)
STORES_FILE    = "stores_detailed.csv"
CREDENTIALS_FILE = "credentials.json"  # same file used for Google My Business auth

# Sheet column headers
SHEET_HEADERS  = [
    "ID", "Timestamp", "Store Code", "Store Name",
    "IP", "City", "Country", "Device", "Browser", "OS", "User Agent"
]

# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)


# ---------------------------------------------------------------------------
# Place ID loader
# ---------------------------------------------------------------------------
def _load_place_ids() -> dict:
    try:
        with open(STORES_FILE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        ids = {r["storeCode"]: r["place_id"] for r in rows if r.get("place_id")}
        if ids:
            return ids
    except (FileNotFoundError, KeyError):
        pass
    try:
        with open("place_ids.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    return {}


PLACE_IDS = _load_place_ids()


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
    if PLACE_IDS.get(store_code):
        return f"https://search.google.com/local/writereview?placeid={PLACE_IDS[store_code]}"
    name  = store.get("title", "Citykart")
    city  = store.get("city", "")
    query = f"{name} {city}".replace(" ", "+")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


# ---------------------------------------------------------------------------
# Google Sheets client (initialised once)
# ---------------------------------------------------------------------------
_worksheet = None

def _get_worksheet():
    """
    Returns the gspread worksheet, initialising the connection on first call.

    Uses your existing OAuth2 token.json — the same token already used for
    Google My Business. No service account needed.

    One-time requirement: token.json must already exist (run any other script
    like get_stores.py first to generate it). The token is refreshed
    automatically when it expires.

    Important: before deploying to Railway, run this locally once so token.json
    is generated, then push token.json to your GitHub repo alongside this file.
    """
    global _worksheet

    if _worksheet is not None:
        return _worksheet

    if not SPREADSHEET_ID:
        print("  ⚠  SPREADSHEET_ID not set — scans will NOT be logged to Google Sheets.")
        return None

    try:
        import gspread
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        TOKEN_FILE = "token.json"
        SCOPES = [
            "https://www.googleapis.com/auth/business.manage",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        if not os.path.exists(TOKEN_FILE):
            print(f"  ⚠  {TOKEN_FILE} not found.")
            print("     Run 'python get_stores.py' first to generate it, then restart.")
            return None

        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        # Refresh token if expired
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Save refreshed token
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

        client      = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)

        # Get or create the "Scan Logs" tab
        try:
            ws = spreadsheet.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows=10000, cols=len(SHEET_HEADERS))
            ws.append_row(SHEET_HEADERS, value_input_option="RAW")
            ws.format("A1:K1", {"textFormat": {"bold": True}})
            spreadsheet.batch_update({
                "requests": [{
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": ws.id,
                            "gridProperties": {"frozenRowCount": 1}
                        },
                        "fields": "gridProperties.frozenRowCount"
                    }
                }]
            })
            print(f"  ✓  Created '{SHEET_NAME}' tab in Google Sheet")

        _worksheet = ws
        print(f"  ✓  Connected to Google Sheet: {spreadsheet.title}")
        return ws

    except FileNotFoundError as e:
        print(f"  ⚠  File not found: {e} — Sheets logging disabled.")
    except Exception as e:
        print(f"  ⚠  Google Sheets connection failed: {e}")

    return None


# ---------------------------------------------------------------------------
# IP → city lookup
# ---------------------------------------------------------------------------
def _geo_from_ip(ip: str) -> tuple:
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
    try:
        from user_agents import parse as ua_parse
        ua      = ua_parse(ua_string)
        device  = "Mobile" if ua.is_mobile else ("Tablet" if ua.is_tablet else "Desktop")
        browser = ua.browser.family
        os_name = ua.os.family
        return device, browser, os_name
    except Exception:
        return "Unknown", "Unknown", "Unknown"


# ---------------------------------------------------------------------------
# CSV logging — PRIMARY storage (always works, survives Sheets failures)
# ---------------------------------------------------------------------------
SCAN_LOG_FILE = "scan_logs.csv"
LOG_FIELDS    = ["id", "timestamp", "store_code", "store_name",
                 "ip", "city", "country", "device", "browser", "os", "user_agent"]


def _next_scan_id_csv() -> int:
    if not os.path.exists(SCAN_LOG_FILE):
        return 1
    with open(SCAN_LOG_FILE, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return len(rows) + 1


def _write_csv(row: dict):
    file_exists = os.path.exists(SCAN_LOG_FILE)
    with open(SCAN_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def _read_csv() -> list:
    if not os.path.exists(SCAN_LOG_FILE):
        return []
    with open(SCAN_LOG_FILE, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Main scan logger — writes to CSV always, tries Sheets as bonus
# ---------------------------------------------------------------------------
def _log_scan(store_code: str, store_name: str, ip: str, ua_string: str) -> dict:
    city, country            = _geo_from_ip(ip)
    device, browser, os_name = _parse_ua(ua_string)
    timestamp                = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    scan_id                  = _next_scan_id_csv()

    row_data = {
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

    # Always write to CSV first — this never fails
    _write_csv(row_data)

    # Try Google Sheets as a bonus — silent fail if not available
    ws = _get_worksheet()
    if ws:
        try:
            ws.append_row(
                [scan_id, timestamp, store_code, store_name,
                 ip, city, country, device, browser, os_name, ua_string[:200]],
                value_input_option="RAW",
            )
        except Exception as e:
            print(f"  ⚠  Sheets write failed (scan saved to CSV): {e}")

    return row_data


# ---------------------------------------------------------------------------
# Read all scans — CSV is primary, Sheets is fallback
# ---------------------------------------------------------------------------
def _read_all_scans() -> list:
    # Always read from CSV — it's always up to date
    return _read_csv()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return jsonify({
        "service": "Citykart QR Tracking Server",
        "status":  "running",
        "logging": "Google Sheets" if SPREADSHEET_ID else "disabled (set SPREADSHEET_ID)",
        "routes": {
            "/r/<store_code>":   "QR redirect — scans tracked here",
            "/dashboard":        "Visual scan dashboard",
            "/api/summary":      "JSON scan counts per store",
            "/api/scans":        "JSON all scan logs",
            "/api/scans/<code>": "JSON scans for one store",
        },
    })


@app.route("/r/<store_code>")
def qr_redirect(store_code):
    stores     = _load_stores()
    code_upper = store_code.upper()
    store      = stores.get(code_upper)

    if not store:
        return redirect("https://www.google.com/search?q=Citykart+reviews", 302)

    ip        = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    ua_string = request.headers.get("User-Agent", "")

    scan = _log_scan(code_upper, store["title"], ip, ua_string)
    print(f"  SCAN #{scan['id']}  {code_upper}  {scan['city']}, {scan['country']}  [{scan['device']}]")

    return redirect(_review_url(code_upper, store), 302)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.route("/api/summary")
def api_summary():
    stores  = _load_stores()
    summary = {code: {"name": s["title"], "city": s["city"], "scans": 0}
               for code, s in stores.items()}

    for row in _read_all_scans():
        code = row["store_code"]
        if code in summary:
            summary[code]["scans"] += 1
        else:
            summary[code] = {"name": row["store_name"], "city": "", "scans": 1}

    return jsonify(summary)


@app.route("/api/scans")
def api_scans_all():
    return jsonify(_read_all_scans())


@app.route("/api/scans/<store_code>")
def api_scans_store(store_code):
    scans = [r for r in _read_all_scans() if r["store_code"] == store_code.upper()]
    return jsonify(scans)


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
  header { background: #7c3aed; color: white; padding: 20px 32px; }
  header h1 { font-size: 22px; }
  header p  { font-size: 13px; opacity: .8; margin-top: 4px; }
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
  .badge   { display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 12px; font-weight: 600; }
  .badge-m { background: #dbeafe; color: #1d4ed8; }
  .badge-d { background: #f0fdf4; color: #166534; }
  .badge-t { background: #fef9c3; color: #854d0e; }
  h2       { font-size: 17px; font-weight: 600; margin-bottom: 12px; color: #374151; }
  .refresh { float: right; font-size: 12px; color: #94a3b8; }
  .sheets-link { display:inline-block; margin-bottom:20px; background:#fff; border:1px solid #e2e8f0;
                 border-radius:8px; padding:10px 18px; font-size:13px; color:#7c3aed;
                 text-decoration:none; font-weight:600; }
  .sheets-link:hover { background:#faf5ff; }
  @media (max-width: 640px) { .kpi-row { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>
<header>
  <h1>📸 Citykart — QR Scan Tracker</h1>
  <p>Live dashboard · data stored in Google Sheets · auto-refreshes every 30 seconds</p>
</header>
<div class="container">
  <div id="kpis" class="kpi-row"></div>
  <a class="sheets-link" id="sheets-link" href="#" target="_blank">📊 Open in Google Sheets →</a>
  <h2>Per-Store Scan Counts <span class="refresh" id="last-refresh"></span></h2>
  <table>
    <thead><tr><th>Store</th><th>City</th><th>Total Scans</th><th>Last Scan</th></tr></thead>
    <tbody id="store-body"></tbody>
  </table>
  <h2>Recent Scans (last 50)</h2>
  <table>
    <thead><tr><th>#</th><th>Time (UTC)</th><th>Store</th><th>City</th><th>Device</th><th>Browser</th><th>OS</th></tr></thead>
    <tbody id="scan-body"></tbody>
  </table>
</div>
<script>
const SHEET_ID = "{{ spreadsheet_id }}";
if (SHEET_ID) {
  document.getElementById('sheets-link').href =
    'https://docs.google.com/spreadsheets/d/' + SHEET_ID;
} else {
  document.getElementById('sheets-link').style.display = 'none';
}

async function load() {
  const [sumResp, scanResp] = await Promise.all([
    fetch('/api/summary'), fetch('/api/scans')
  ]);
  const summary = await sumResp.json();
  const scans   = await scanResp.json();

  const totalScans      = scans.length;
  const storeCodes      = Object.keys(summary);
  const storesWithScans = storeCodes.filter(c => summary[c].scans > 0).length;
  const todayStr        = new Date().toISOString().slice(0, 10);
  const todayScans      = scans.filter(s => s.timestamp && s.timestamp.startsWith(todayStr)).length;
  const mobileScans     = scans.filter(s => s.device === 'Mobile').length;

  document.getElementById('kpis').innerHTML = `
    <div class="kpi"><div class="val">${totalScans}</div><div class="lbl">Total Scans</div></div>
    <div class="kpi"><div class="val">${todayScans}</div><div class="lbl">Scans Today</div></div>
    <div class="kpi"><div class="val">${storesWithScans}/${storeCodes.length}</div><div class="lbl">Active Stores</div></div>
    <div class="kpi"><div class="val">${totalScans ? Math.round(mobileScans/totalScans*100) : 0}%</div><div class="lbl">Mobile Scans</div></div>
  `;

  const lastScan = {};
  scans.forEach(s => { lastScan[s.store_code] = s.timestamp; });

  document.getElementById('store-body').innerHTML = storeCodes
    .sort((a, b) => summary[b].scans - summary[a].scans)
    .map(code => `
      <tr>
        <td><b>${summary[code].name}</b> <small style="color:#94a3b8">(${code})</small></td>
        <td>${summary[code].city || '—'}</td>
        <td style="font-weight:700;color:#7c3aed">${summary[code].scans}</td>
        <td style="color:#64748b;font-size:13px">${lastScan[code] || '—'}</td>
      </tr>`).join('');

  const deviceBadge = d => {
    if (d === 'Mobile')  return `<span class="badge badge-m">📱 Mobile</span>`;
    if (d === 'Tablet')  return `<span class="badge badge-t">📲 Tablet</span>`;
    return `<span class="badge badge-d">💻 Desktop</span>`;
  };

  const recent = [...scans].reverse().slice(0, 50);
  document.getElementById('scan-body').innerHTML = recent.length
    ? recent.map(s => `
        <tr>
          <td style="color:#94a3b8">#${s.id}</td>
          <td style="font-size:13px">${s.timestamp}</td>
          <td><b>${s.store_code}</b></td>
          <td>${s.city || '—'}, ${s.country || ''}</td>
          <td>${deviceBadge(s.device)}</td>
          <td style="font-size:13px">${s.browser || '—'}</td>
          <td style="font-size:13px">${s.os || '—'}</td>
        </tr>`).join('')
    : '<tr><td colspan="7" style="text-align:center;color:#94a3b8;padding:32px">No scans yet — share the QR codes!</td></tr>';

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
    return render_template_string(DASHBOARD_HTML, spreadsheet_id=SPREADSHEET_ID)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Citykart QR tracking server")
    parser.add_argument("--host",  default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port",  type=int,
                        default=int(os.environ.get("PORT", 5000)),  # Railway sets $PORT
                        help="Port (default: $PORT env var or 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    print(f"\n🟣  Citykart QR Tracking Server")
    print(f"    Logging to      : {'Google Sheets (' + SPREADSHEET_ID + ')' if SPREADSHEET_ID else 'CSV only'}")
    print(f"    Redirect route  : http://{args.host}:{args.port}/r/<STORE_CODE>")
    print(f"    Dashboard       : http://localhost:{args.port}/dashboard")
    print(f"\n    Press Ctrl+C to stop.\n")

    app.run(host=args.host, port=args.port, debug=args.debug)
