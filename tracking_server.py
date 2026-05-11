"""
tracking_server.py — QR scan tracking server for Citykart.

When a customer scans a store's QR code, their phone hits:
    GET /r/<STORE_CODE>

This server:
  1. Logs the scan to Google Sheets (permanent storage — never lost on redeploy)
  2. Redirects the customer to the Google review page for that store

Store map and Place IDs are hardcoded below — no CSV files needed on Railway.

Setup (one time):
  1. Create a Google Sheet → copy its Spreadsheet ID from the URL
  2. Set SPREADSHEET_ID below (already set)
  3. Push token.json to your GitHub repo (same token used for Google My Business)
     → Railway will have it on every deploy

Dashboard:
    GET /dashboard          → Browser dashboard (HTML, reads from Google Sheets)
    GET /api/scans          → JSON: all scan logs
    GET /api/scans/<CODE>   → JSON: scans for one store
    GET /api/summary        → JSON: per-store scan counts

Requirements:
    pip install flask flask-cors requests user-agents gspread google-auth google-auth-oauthlib gunicorn
"""

import csv
import io
import json
import os
from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, render_template_string, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# ── STORE MAP  (hardcoded — no CSV files needed on Railway) ─────────────────
# ---------------------------------------------------------------------------
# Format: "STORE_CODE": {"name": "...", "city": "...", "place_id": "..."}
# place_id → direct Google write-review link (starts with ChIJ...)
# Leave place_id as "" if unknown → falls back to Maps search URL
# ---------------------------------------------------------------------------
STORES = {
    "ABT": {"name": "CityKart Agra",                   "city": "Agra",      "place_id": "ChIJ9cntFgB3dDkRV9_mckyVRVY"},
    "BDN": {"name": "Citykart Budaun",                 "city": "Budaun",    "place_id": "ChIJQ9fzBwDpRDcR85mEZBiefhE"},
    "TZP": {"name": "Citykart Tezpur",                 "city": "Sonitpur",  "place_id": "ChIJRzmi9VoZDTkRqUBB9PDXKy8"},
    "NBR": {"name": "CityKart Burari",                 "city": "New Delhi", "place_id": ""},
    "GPB": {"name": "CityKart Paltan Bazar Guwahati",  "city": "Guwahati",  "place_id": ""},
    "LBL": {"name": "Citykart Balaganj Lucknow",       "city": "Lucknow",   "place_id": "ChIJ7QqULKD_mzkRUWjCuvMaLkE"},
    "LBK": {"name": "Citykart Bakshi Ka Talab Lucknow","city": "Lucknow",   "place_id": "ChIJ979nEtpRmTkROsH9HMd9FBY"},
    "LAM": {"name": "Citykart Alambagh Lucknow",       "city": "Lucknow",   "place_id": "ChIJUartaCT8mzkRl5ySY8Crr94"},
    "LMP": {"name": "Citykart Mall Munsipulia",        "city": "Lucknow",   "place_id": "ChIJDba3kyjjmzkRt91EmZUbpzY"},
    "LAD": {"name": "Citykart Adil Nagar Lucknow",     "city": "Lucknow",   "place_id": "ChIJA_fwXLhXmTkRizPFFE1CRos"},
}

# ---------------------------------------------------------------------------
# ── GOOGLE SHEETS CONFIG ─────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
SPREADSHEET_ID   = "1GHVnnRsX2s9zZehRTf0d4BTYBo5ekndyUS6MlZT2wrk"
SHEET_NAME       = "Scan Logs"
SHEET_HEADERS    = ["ID", "Timestamp", "Store Code", "Store Name",
                    "IP", "City", "Country", "Device", "Browser", "OS", "User Agent"]

TOKEN_FILE       = "token.json"
SCOPES           = [
    "https://www.googleapis.com/auth/business.manage",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ---------------------------------------------------------------------------
# ── LOCAL CSV — in-memory fallback (lost on redeploy, that's OK) ─────────────
# ---------------------------------------------------------------------------
SCAN_LOG_FILE = "scan_logs.csv"
LOG_FIELDS    = ["id", "timestamp", "store_code", "store_name",
                 "ip", "city", "country", "device", "browser", "os", "user_agent"]

# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)


# ---------------------------------------------------------------------------
# Review URL builder
# ---------------------------------------------------------------------------
def _review_url(store_code: str) -> str:
    store = STORES.get(store_code, {})
    if store.get("place_id"):
        return f"https://search.google.com/local/writereview?placeid={store['place_id']}"
    name  = store.get("name", "Citykart")
    city  = store.get("city", "")
    query = f"{name} {city}".replace(" ", "+")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


# ---------------------------------------------------------------------------
# Google Sheets — PRIMARY storage
# ---------------------------------------------------------------------------
_worksheet     = None
_sheets_failed = False   # once broken, stop retrying every request

TOKEN_FILE = "token.json"

# Create token.json from Railway environment variable
if "TOKEN_JSON" in os.environ:
    with open(TOKEN_FILE, "w") as f:
        f.write(os.environ["TOKEN_JSON"])

def _get_worksheet():
    return None
    """
    Returns the gspread worksheet. Initialises once; returns None silently
    if token.json is missing or auth fails — CSV fallback takes over.
    """
    # global _worksheet, _sheets_failed

    # if _worksheet is not None:
    #     return _worksheet
    # if _sheets_failed:
    #     return None

    # if not SPREADSHEET_ID:
    #     print("  ⚠  SPREADSHEET_ID not set — Sheets logging disabled.")
    #     _sheets_failed = True
    #     return None

    # if not os.path.exists(TOKEN_FILE):
    #     print(f"  ⚠  {TOKEN_FILE} not found — Sheets logging disabled.")
    #     print("     Push token.json to the Railway repo to enable permanent scan storage.")
    #     _sheets_failed = True
    #     return None

    # try:
    #     import gspread
    #     from google.auth.transport.requests import Request
    #     from google.oauth2.credentials import Credentials

    #     creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    #     if not creds.valid:
    #         if creds.expired and creds.refresh_token:
    #             creds.refresh(Request())
    #             with open(TOKEN_FILE, "w") as f:
    #                 f.write(creds.to_json())
    #             print("  ✓  token.json refreshed")
    #         else:
    #             print("  ⚠  token.json invalid and cannot be refreshed — Sheets logging disabled.")
    #             _sheets_failed = True
    #             return None

    #     client      = gspread.authorize(creds)
    #     spreadsheet = client.open_by_key(SPREADSHEET_ID)

    #     try:
    #         ws = spreadsheet.worksheet(SHEET_NAME)
    #     except gspread.WorksheetNotFound:
    #         ws = spreadsheet.add_worksheet(
    #             title=SHEET_NAME, rows=100000, cols=len(SHEET_HEADERS)
    #         )
    #         ws.append_row(SHEET_HEADERS, value_input_option="RAW")
    #         ws.format("A1:K1", {"textFormat": {"bold": True}})
    #         spreadsheet.batch_update({"requests": [{
    #             "updateSheetProperties": {
    #                 "properties": {
    #                     "sheetId": ws.id,
    #                     "gridProperties": {"frozenRowCount": 1}
    #                 },
    #                 "fields": "gridProperties.frozenRowCount"
    #             }
    #         }]})
    #         print(f"  ✓  Created '{SHEET_NAME}' tab in Google Sheet")

    #     _worksheet = ws
    #     print(f"  ✓  Connected to Google Sheets: {spreadsheet.title}")
    #     return ws

    # except Exception as e:
    #     print(f"  ⚠  Google Sheets init failed: {e}")
    #     _sheets_failed = True
    #     return None


def _next_scan_id() -> int:
    """Get next scan ID from Sheets row count; fall back to CSV count."""
    ws = _get_worksheet()
    if ws:
        try:
            return max(1, ws.row_count - 1)   # approximate; fast, no full read
        except Exception:
            pass
    # fallback: count CSV rows
    if not os.path.exists(SCAN_LOG_FILE):
        return 1
    with open(SCAN_LOG_FILE, encoding="utf-8") as f:
        return sum(1 for _ in f)   # header + rows, close enough


# ---------------------------------------------------------------------------
# CSV — local fallback (survives within a single Railway instance lifetime)
# ---------------------------------------------------------------------------
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
# Read all scans — Sheets is primary (permanent), CSV is fallback
# ---------------------------------------------------------------------------
def _read_all_scans() -> list:
    ws = _get_worksheet()
    if ws:
        try:
            rows = ws.get_all_records()   # list of dicts using header row as keys
            # Normalise keys to match CSV field names (lowercase, spaces→underscores)
            normalised = []
            for r in rows:
                normalised.append({
                    "id":         str(r.get("ID", "")),
                    "timestamp":  str(r.get("Timestamp", "")),
                    "store_code": str(r.get("Store Code", "")),
                    "store_name": str(r.get("Store Name", "")),
                    "ip":         str(r.get("IP", "")),
                    "city":       str(r.get("City", "")),
                    "country":    str(r.get("Country", "")),
                    "device":     str(r.get("Device", "")),
                    "browser":    str(r.get("Browser", "")),
                    "os":         str(r.get("OS", "")),
                    "user_agent": str(r.get("User Agent", "")),
                })
            return normalised
        except Exception as e:
            print(f"  ⚠  Sheets read failed, falling back to CSV: {e}")

    # Fallback to local CSV
    return _read_csv()


# ---------------------------------------------------------------------------
# IP → city lookup
# ---------------------------------------------------------------------------
def _geo_from_ip(ip: str) -> tuple:
    if ip in ("127.0.0.1", "::1", "localhost"):
        return "Local", "Local"
    try:
        import requests as req
        r = req.get(f"https://ip-api.com/json/{ip}?fields=city,country", timeout=2)
        if r.status_code == 200:
            data = r.json()
            return data.get("city", "Unknown"), data.get("country", "Unknown")
    except Exception:
        pass
    return "Unknown", "Unknown"


# ---------------------------------------------------------------------------
# Device / browser detection
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
# Main scan logger
# ---------------------------------------------------------------------------
def _log_scan(store_code: str, store_name: str, ip: str, ua_string: str) -> dict:
    city, country            = _geo_from_ip(ip)
    device, browser, os_name = _parse_ua(ua_string)
    timestamp                = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    scan_id                  = _next_scan_id()

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

    # PRIMARY: Google Sheets (permanent — survives redeployment)
    ws = _get_worksheet()
    if ws:
        try:
            ws.append_row(
                [scan_id, timestamp, store_code, store_name,
                 ip, city, country, device, browser, os_name, ua_string[:200]],
                value_input_option="RAW",
            )
            print(f"  ✓  Scan #{scan_id} logged to Sheets — {store_code}  {city}, {country}  [{device}]")
        except Exception as e:
            print(f"  ⚠  Sheets write failed: {e} — saved to CSV only")
            _write_csv(row_data)
    else:
        # FALLBACK: local CSV (lost on redeploy)
        _write_csv(row_data)
        print(f"  ⚠  Scan #{scan_id} saved to CSV only (Sheets unavailable) — {store_code}  [{device}]")

    return row_data


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    sheets_status = "connected" if _get_worksheet() else "unavailable (push token.json to Railway)"
    return jsonify({
        "service":  "Citykart QR Tracking Server",
        "status":   "running",
        "storage":  f"Google Sheets ({sheets_status})",
        "stores":   len(STORES),
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
    code  = store_code.upper()
    store = STORES.get(code)

    if not store:
        # Unknown store code — redirect to generic Citykart search
        return redirect("https://www.google.com/search?q=Citykart+reviews", 302)

    # Extract real client IP (Railway sits behind a reverse proxy)
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
    if "," in ip:
        ip = ip.split(",")[0].strip()

    ua_string = request.headers.get("User-Agent", "")

    scan = _log_scan(code, store["name"], ip, ua_string)
    print(f"  SCAN #{scan['id']}  {code}  {scan['city']}, {scan['country']}  [{scan['device']}]")

    return redirect(_review_url(code), 302)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.route("/api/summary")
def api_summary():
    # Seed all known stores with 0 scans
    summary = {
        code: {"name": s["name"], "city": s["city"], "scans": 0}
        for code, s in STORES.items()
    }
    for row in _read_all_scans():
        code = row.get("store_code", "")
        if code in summary:
            summary[code]["scans"] += 1
        elif code:
            summary[code] = {"name": row.get("store_name", code), "city": "", "scans": 1}

    return jsonify(summary)


@app.route("/api/scans")
def api_scans_all():
    return jsonify(_read_all_scans())


@app.route("/api/scans/<store_code>")
def api_scans_store(store_code):
    scans = [r for r in _read_all_scans() if r.get("store_code") == store_code.upper()]
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
  .storage-badge { display:inline-block; margin-bottom:20px; background:#fff; border:1px solid #e2e8f0;
                   border-radius:8px; padding:10px 18px; font-size:13px; font-weight:600; }
  .storage-ok   { color: #166534; border-color: #bbf7d0; background: #f0fdf4; }
  .storage-warn { color: #92400e; border-color: #fde68a; background: #fffbeb; }
  .sheets-link  { display:inline-block; margin-left:12px; margin-bottom:20px; background:#fff;
                  border:1px solid #e2e8f0; border-radius:8px; padding:10px 18px; font-size:13px;
                  color:#7c3aed; text-decoration:none; font-weight:600; }
  .sheets-link:hover { background:#faf5ff; }
  @media (max-width: 640px) { .kpi-row { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>
<header>
  <h1>📸 Citykart — QR Scan Tracker</h1>
  <p>Storage: Google Sheets (permanent) · auto-refreshes every 30 seconds</p>
</header>
<div class="container">
  <div id="storage-info"></div>
  <div id="kpis" class="kpi-row"></div>
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

async function load() {
  const [sumResp, scanResp, rootResp] = await Promise.all([
    fetch('/api/summary'), fetch('/api/scans'), fetch('/')
  ]);
  const summary = await sumResp.json();
  const scans   = await scanResp.json();
  const root    = await rootResp.json();

  // Storage badge
  const storageOk = root.storage && root.storage.includes('connected');
  document.getElementById('storage-info').innerHTML =
    `<span class="storage-badge ${storageOk ? 'storage-ok' : 'storage-warn'}">
      ${storageOk ? '✅ Sheets connected — scans stored permanently' : '⚠️ Sheets unavailable — scans in local CSV only (lost on redeploy)'}
    </span>` +
    (SHEET_ID && storageOk
      ? `<a class="sheets-link" href="https://docs.google.com/spreadsheets/d/${SHEET_ID}" target="_blank">📊 Open Sheet →</a>`
      : '');

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
  scans.forEach(s => { if (s.store_code) lastScan[s.store_code] = s.timestamp; });

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
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    print(f"\n🟣  Citykart QR Tracking Server")
    print(f"    Stores loaded   : {len(STORES)}")
    print(f"    Spreadsheet ID  : {SPREADSHEET_ID or 'NOT SET'}")
    print(f"    Primary storage : Google Sheets (token.json {'found' if os.path.exists(TOKEN_FILE) else 'MISSING — push to Railway'})")
    print(f"    Fallback storage: {SCAN_LOG_FILE} (local, lost on redeploy)")
    print(f"    Redirect route  : http://0.0.0.0:{port}/r/<STORE_CODE>")
    print(f"    Dashboard       : http://localhost:{port}/dashboard")
    print(f"\n    Press Ctrl+C to stop.\n")

    app.run(host="0.0.0.0", port=port, debug=debug)
