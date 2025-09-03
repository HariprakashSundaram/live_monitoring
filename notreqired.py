# jmeter_dashboard.py
from flask import Flask, request, jsonify, render_template_string, send_file, make_response
import sqlite3, time, statistics, csv, io, json
from datetime import datetime
import math

app = Flask(__name__)
DB_FILE = "jmeter_metrics.db"

# --------- DB ----------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS jmeter_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,          -- epoch seconds
            label TEXT,
            response_time REAL,
            success INTEGER,            -- 1 or 0
            thread_count INTEGER,
            status_code TEXT,
            error_message TEXT
        )
    """)
    conn.commit()
    conn.close()

def run_query(q, params=()):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(q, params)
    rows = c.fetchall()
    conn.close()
    return rows

# --------- Ingest endpoint (JMeter posts here) ----------
@app.route("/metrics", methods=["POST"])
def receive_metrics():
    data = request.json
    if not data:
        return jsonify({"error": "no json"}), 400

    # Accept either aggregated or per-sample; try to be flexible
    label = data.get("label", data.get("sampler", "ALL"))
    # priority: explicit responseTime, avgResponseTime, else try sample fields
    rt = data.get("responseTime", data.get("avgResponseTime", data.get("avg", 0.0)))
    # determine success: if there's an "errorPct" >0 consider failure; if "success" present use it
    success = 1
    if "success" in data:
        success = 1 if data.get("success") else 0
    elif "errorPct" in data:
        success = 0 if data.get("errorPct", 0) > 0 else 1
    # status code & error message if provided
    status_code = str(data.get("statusCode", data.get("rc", "200")))
    error_message = data.get("errorMessage", data.get("errorMsg", "")) if not success else ""
    thread_count = int(data.get("activeThreads", data.get("threads", 0)))

    ts = int(time.time())

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO jmeter_samples (timestamp, label, response_time, success, thread_count, status_code, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (ts, label, float(rt), success, thread_count, status_code, error_message))
    conn.commit()
    conn.close()

    return jsonify({"status": "ok"}), 200

# --------- Helper: compute percentiles safely ----------
def percentile(sorted_list, pct):
    if not sorted_list:
        return None
    k = (len(sorted_list)-1) * (pct/100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_list[int(k)]
    d0 = sorted_list[int(f)] * (c-k)
    d1 = sorted_list[int(c)] * (k-f)
    return d0 + d1

# --------- Aggregate endpoint ----------
@app.route("/api/aggregate", methods=["GET"])
def api_aggregate():
    # optional filters: label, start,end (epoch seconds), timezone ignored here (frontend handles)
    label = request.args.get("label")
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    q = "SELECT label, response_time, success FROM jmeter_samples"
    conds = []
    params = []
    if label:
        conds.append("label = ?")
        params.append(label)
    if start:
        conds.append("timestamp >= ?")
        params.append(start)
    if end:
        conds.append("timestamp <= ?")
        params.append(end)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    rows = run_query(q, tuple(params))
    agg = {}
    for lab, rt, succ in rows:
        if lab not in agg:
            agg[lab] = {"samples": [], "errors": 0}
        agg[lab]["samples"].append(rt)
        if succ == 0:
            agg[lab]["errors"] += 1

    res = []
    for lab, d in agg.items():
        s = sorted(d["samples"])
        count = len(s)
        avg = round(sum(s)/count, 2) if count else 0
        mn = s[0] if s else 0
        mx = s[-1] if s else 0
        p90 = round(percentile(s, 90),2) if s else 0
        p95 = round(percentile(s, 95),2) if s else 0
        p99 = round(percentile(s, 99),2) if s else 0
        errors = d["errors"]
        err_pct = round((errors/count)*100,2) if count else 0
        res.append({
            "label": lab,
            "count": count,
            "avg": avg,
            "min": mn,
            "max": mx,
            "pct90": p90,
            "pct95": p95,
            "pct99": p99,
            "error_pct": err_pct
        })
    # sort by count desc
    res = sorted(res, key=lambda x: x["count"], reverse=True)
    return jsonify(res)

# --------- TPS per second endpoint ----------
@app.route("/api/tps", methods=["GET"])
def api_tps():
    # optional window size n seconds (default last 60 seconds)
    window = request.args.get("window", default=60, type=int)
    end = request.args.get("end", type=int) or int(time.time())
    start = end - window + 1
    rows = run_query("SELECT timestamp, COUNT(*) FROM jmeter_samples WHERE timestamp BETWEEN ? AND ? GROUP BY timestamp ORDER BY timestamp ASC", (start, end))
    # build continuous time series for window
    ts_map = {r[0]: r[1] for r in rows}
    labels = []
    values = []
    for sec in range(start, end+1):
        labels.append(sec)
        values.append(ts_map.get(sec, 0))
    # return as human timestamps (seconds) and values
    return jsonify({
        "timestamps": labels,
        "tps": values
    })

# --------- Thread counts over time ----------
@app.route("/api/threads", methods=["GET"])
def api_threads():
    # average thread_count per second in window
    window = request.args.get("window", default=60, type=int)
    end = request.args.get("end", type=int) or int(time.time())
    start = end - window + 1
    rows = run_query("""
        SELECT timestamp, AVG(thread_count) FROM jmeter_samples
        WHERE timestamp BETWEEN ? AND ?
        GROUP BY timestamp ORDER BY timestamp ASC
    """, (start, end))
    ts_map = {r[0]: round(r[1],2) for r in rows}
    labels = []
    values = []
    for sec in range(start, end+1):
        labels.append(sec)
        values.append(ts_map.get(sec, 0))
    return jsonify({"timestamps": labels, "threads": values})

# --------- Errors table endpoint ----------
@app.route("/api/errors", methods=["GET"])
def api_errors():
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    q = """
    SELECT label, status_code, COUNT(*), GROUP_CONCAT(DISTINCT error_message)
    FROM jmeter_samples
    WHERE success=0
    """
    conds = []
    params = []
    if start:
        conds.append("timestamp >= ?"); params.append(start)
    if end:
        conds.append("timestamp <= ?"); params.append(end)
    if conds:
        q += " AND " + " AND ".join(conds)
    q += " GROUP BY label, status_code ORDER BY COUNT(*) DESC"
    rows = run_query(q, tuple(params))
    result = [{"label": r[0], "status": r[1], "count": r[2], "message": r[3] or ""} for r in rows]
    return jsonify(result)

# --------- Success transactions endpoint ----------
@app.route("/api/success", methods=["GET"])
def api_success():
    # supply start,end optional
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    q = """
    SELECT label, COUNT(*), AVG(response_time), MIN(response_time), MAX(response_time)
    FROM jmeter_samples WHERE success=1
    """
    conds = []
    params = []
    if start:
        conds.append("timestamp >= ?"); params.append(start)
    if end:
        conds.append("timestamp <= ?"); params.append(end)
    if conds:
        q += " AND " + " AND ".join(conds)
    q += " GROUP BY label ORDER BY COUNT(*) DESC"
    rows = run_query(q, tuple(params))
    result = [{"label": r[0], "count": r[1], "avg": round(r[2],2), "min": r[3], "max": r[4]} for r in rows]
    return jsonify(result)

# --------- Download CSV endpoints ----------
def generate_csv(rows, headers):
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(headers)
    cw.writerows(rows)
    return si.getvalue()

@app.route("/download/aggregate.csv")
def download_aggregate_csv():
    # optional label/start/end filters forwarded to aggregate
    label = request.args.get("label")
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    # reuse api_aggregate
    with app.test_request_context(f"/api/aggregate?label={label or ''}&start={start or ''}&end={end or ''}"):
        data = api_aggregate().get_json()
    rows = []
    for r in data:
        rows.append([r["label"], r["count"], r["avg"], r["min"], r["max"], r["pct90"], r["pct95"], r["pct99"], r["error_pct"]])
    csv_text = generate_csv(rows, ["Label","Count","Avg","Min","Max","90%","95%","99%","Error %"])
    resp = make_response(csv_text)
    resp.headers["Content-Disposition"] = "attachment; filename=aggregate.csv"
    resp.headers["Content-Type"] = "text/csv"
    return resp

@app.route("/download/errors.csv")
def download_errors_csv():
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    with app.test_request_context(f"/api/errors?start={start or ''}&end={end or ''}"):
        data = api_errors().get_json()
    rows = [[r["label"], r["status"], r["count"], r["message"]] for r in data]
    csv_text = generate_csv(rows, ["Label","Status Code","Count","Messages"])
    resp = make_response(csv_text)
    resp.headers["Content-Disposition"] = "attachment; filename=errors.csv"
    resp.headers["Content-Type"] = "text/csv"
    return resp

@app.route("/download/success.csv")
def download_success_csv():
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    with app.test_request_context(f"/api/success?start={start or ''}&end={end or ''}"):
        data = api_success().get_json()
    rows = [[r["label"], r["count"], r["avg"], r["min"], r["max"]] for r in data]
    csv_text = generate_csv(rows, ["Label","Count","Avg","Min","Max"])
    resp = make_response(csv_text)
    resp.headers["Content-Disposition"] = "attachment; filename=success.csv"
    resp.headers["Content-Type"] = "text/csv"
    return resp

# --------- Download snapshot HTML (hard-coded HTML file with current data embedded) ----------
@app.route("/download/snapshot.html")
def download_snapshot():
    # produce a static HTML that embeds current aggregate, errors and success as JSON so file is standalone
    agg = api_aggregate().get_json()
    errs = api_errors().get_json()
    succ = api_success().get_json()
    snapshot_html = f"""
    <!doctype html>
    <html>
    <head><meta charset="utf-8"><title>JMeter Dashboard Snapshot</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body class="p-4">
    <h3>Snapshot taken: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</h3>
    <h4>Aggregate Report</h4>
    <pre id="agg">{json.dumps(agg, indent=2)}</pre>
    <h4>Errors</h4>
    <pre id="err">{json.dumps(errs, indent=2)}</pre>
    <h4>Success</h4>
    <pre id="succ">{json.dumps(succ, indent=2)}</pre>
    </body></html>
    """
    resp = make_response(snapshot_html)
    resp.headers["Content-Disposition"] = "attachment; filename=dashboard_snapshot.html"
    resp.headers["Content-Type"] = "text/html"
    return resp

# --------- Dashboard UI ----------
@app.route("/dashboard")
def dashboard():
    # Build list of distinct labels for filter dropdown
    rows = run_query("SELECT DISTINCT label FROM jmeter_samples")
    labels = sorted([r[0] for r in rows])
    # serve a single big html template (kept inline for single-file simplicity)
    html = render_template_string("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>JMeter Live Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css" rel="stylesheet">
  <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/flatpickr"></script>
  <link href="https://cdn.jsdelivr.net/npm/flatpickr/dist/flatpickr.min.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/flatpickr/dist/plugins/confirmDate/confirmDate.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/moment@2.29.4/moment.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/moment-timezone@0.5.43/builds/moment-timezone-with-data.min.js"></script>
  <script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
  <style> body{padding:20px;background:#f5f7fb} .card{border-radius:12px;box-shadow:0 6px 18px rgba(0,0,0,0.06)} </style>
</head>
<body>
  <div class="container-fluid">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h2>JMeter Live Dashboard</h2>
      <div>
        <label class="me-2">Auto-refresh:</label>
        <select id="refreshSelect" class="form-select d-inline-block w-auto me-3">
          <option value="2">2s</option><option value="5">5s</option><option value="10">10s</option>
          <option value="30">30s</option><option value="60">1m</option><option value="0">Off</option>
        </select>

        <label class="me-2">Timezone:</label>
        <select id="tzSelect" class="form-select d-inline-block w-auto me-3"></select>

        <label class="me-2">Start:</label>
        <input id="startPicker" class="form-control d-inline-block w-auto me-2" placeholder="Start time">

        <label class="me-2">End:</label>
        <input id="endPicker" class="form-control d-inline-block w-auto me-2" placeholder="End time">

        <button id="applyRange" class="btn btn-primary btn-sm">Apply</button>
      </div>
    </div>

    <div class="row g-3">
      <div class="col-lg-6">
        <div class="card p-3">
          <div class="d-flex justify-content-between">
            <h5>TPS (per second)</h5>
            <div>
              <button id="copyTps" class="btn btn-outline-secondary btn-sm">Copy Chart</button>
              <button id="downloadTpsPng" class="btn btn-outline-secondary btn-sm">Download PNG</button>
            </div>
          </div>
          <canvas id="tpsChart" height="160"></canvas>
        </div>
      </div>

      <div class="col-lg-6">
        <div class="card p-3">
          <div class="d-flex justify-content-between">
            <h5>Active Threads (avg per sec)</h5>
            <div>
              <button id="copyThreads" class="btn btn-outline-secondary btn-sm">Copy Chart</button>
              <button id="downloadThreadsPng" class="btn btn-outline-secondary btn-sm">Download PNG</button>
            </div>
          </div>
          <canvas id="threadChart" height="160"></canvas>
        </div>
      </div>

      <div class="col-12">
        <div class="card p-3">
          <div class="d-flex justify-content-between mb-2">
            <h5>Aggregate Report</h5>
            <div>
              <label class="me-2">Filter Label:</label>
              <select id="labelFilter" class="form-select d-inline-block w-auto me-2">
                <option value="">All</option>
                {% for l in labels %}
                  <option value="{{ l|e }}">{{ l|e }}</option>
                {% endfor %}
              </select>
              <button id="downloadAgg" class="btn btn-success btn-sm">Download CSV</button>
              <button id="copyAgg" class="btn btn-outline-secondary btn-sm">Copy Table</button>
            </div>
          </div>
          <table id="aggTable" class="table table-striped table-bordered">
            <thead class="table-dark">
              <tr><th>Label</th><th>Count</th><th>Avg</th><th>Min</th><th>Max</th>
              <th>90%</th><th>95%</th><th>99%</th><th>Error %</th></tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div class="col-lg-6">
        <div class="card p-3">
          <div class="d-flex justify-content-between mb-2">
            <h5>Errors</h5>
            <div>
              <button id="downloadErr" class="btn btn-danger btn-sm">Download CSV</button>
              <button id="copyErr" class="btn btn-outline-secondary btn-sm">Copy Table</button>
            </div>
          </div>
          <table id="errTable" class="table table-sm table-hover">
            <thead><tr><th>Label</th><th>Status</th><th>Count</th><th>Messages</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div class="col-lg-6">
        <div class="card p-3">
          <div class="d-flex justify-content-between mb-2">
            <h5>Successful Transactions</h5>
            <div>
              <button id="downloadSucc" class="btn btn-success btn-sm">Download CSV</button>
              <button id="copySucc" class="btn btn-outline-secondary btn-sm">Copy Table</button>
            </div>
          </div>
          <table id="succTable" class="table table-sm table-hover">
            <thead><tr><th>Label</th><th>Count</th><th>Avg</th><th>Min</th><th>Max</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div class="col-12 text-end">
        <a id="downloadSnapshot" class="btn btn-outline-primary">Download Hard-coded HTML Snapshot</a>
      </div>
    </div>
  </div>

<script>
  // ---------- Utilities ----------
  function epochToTZString(sec, tz, format12) {
    if(!sec) return "";
    let m = moment.unix(sec).tz(tz);
    if(format12) return m.format('YYYY-MM-DD hh:mm:ss A');
    return m.format('YYYY-MM-DD HH:mm:ss');
  }

  function downloadText(filename, text) {
    const blob = new Blob([text], {type: 'text/csv;charset=utf-8;'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename; document.body.appendChild(a); a.click();
    a.remove(); URL.revokeObjectURL(url);
  }

  function tableToCSV($table) {
    var data = [];
    $table.find('tr').each(function() {
      var row = [];
      $(this).find('th,td').each(function() {
        row.push('"' + $(this).text().replace(/"/g,'""').trim() + '"');
      });
      data.push(row.join(','));
    });
    return data.join('\\n');
  }

  // ---------- Controls ----------
  const refreshSelect = $('#refreshSelect');
  const tzSelect = $('#tzSelect');
  const startPicker = document.getElementById('startPicker');
  const endPicker = document.getElementById('endPicker');
  const applyRange = $('#applyRange');
  const labelFilter = $('#labelFilter');

  // populate timezones (common list)
  const tzs = moment.tz.names();
  // we'll add a subset first then allow search via select2? to keep simple, add all but pick first 40 as visible and allow typing
  tzs.forEach(t => {
    $('#tzSelect').append(new Option(t, t));
  });
  // default timezone local
  $('#tzSelect').val(moment.tz.guess());

  // flatpickr for 12-hour format
  flatpickr(startPicker, { enableTime:true, time_24hr:false, dateFormat:"Y-m-d h:i K" });
  flatpickr(endPicker, { enableTime:true, time_24hr:false, dateFormat:"Y-m-d h:i K" });

  // ---------- Charts ----------
  let tpsChart = new Chart(document.getElementById('tpsChart'), {
    type:'line', data:{labels:[], datasets:[{label:'TPS', data:[], tension:0.3}]}, options:{scales:{x:{ticks:{maxRotation:0}}}}
  });
  let threadChart = new Chart(document.getElementById('threadChart'), {
    type:'line', data:{labels:[], datasets:[{label:'Threads', data:[], tension:0.3}]}, options:{scales:{x:{ticks:{maxRotation:0}}}}
  });

  // ---------- Data loaders ----------
  let autoHandle = null;
  function getRangeParams() {
    let tz = $('#tzSelect').val();
    let startRaw = $('#startPicker').val();
    let endRaw = $('#endPicker').val();
    let start = null, end = null;
    if(startRaw) start = moment.tz(startRaw, 'YYYY-MM-DD hh:mm:ss A', tz).unix();
    if(endRaw) end = moment.tz(endRaw, 'YYYY-MM-DD hh:mm:ss A', tz).unix();
    return {start, end, tz};
  }

  async function loadTPS() {
    const {start, end} = getRangeParams();
    // use window size derived: 60s or based on selected range
    let window = 60;
    if(start && end) {
      window = Math.max(60, end - start + 1);
    }
    const resp = await fetch('/api/tps?window=' + window + (end?('&end='+end):''));
    const data = await resp.json();
    const tz = $('#tzSelect').val();
    // transform labels to readable strings
    let labels = data.timestamps.map(s => epochToTZString(s, tz, true));
    tpsChart.data.labels = labels;
    tpsChart.data.datasets[0].data = data.tps;
    tpsChart.update();
  }

  async function loadThreads() {
    const {start, end} = getRangeParams();
    let window = 60;
    if(start && end) {
      window = Math.max(60, end - start + 1);
    }
    const resp = await fetch('/api/threads?window=' + window + (end?('&end='+end):''));
    const data = await resp.json();
    const tz = $('#tzSelect').val();
    let labels = data.timestamps.map(s => epochToTZString(s, tz, true));
    threadChart.data.labels = labels;
    threadChart.data.datasets[0].data = data.threads;
    threadChart.update();
  }

  async function loadAggregate() {
    const {start,end} = getRangeParams();
    const label = $('#labelFilter').val();
    let url = '/api/aggregate?';
    if(label) url += 'label=' + encodeURIComponent(label) + '&';
    if(start) url += 'start=' + start + '&';
    if(end) url += 'end=' + end + '&';
    const resp = await fetch(url);
    const data = await resp.json();
    const tbody = $('#aggTable tbody').empty();
    data.forEach(r => {
      tbody.append(`<tr>
        <td>${r.label}</td><td>${r.count}</td><td>${r.avg}</td><td>${r.min}</td><td>${r.max}</td>
        <td>${r.pct90}</td><td>${r.pct95}</td><td>${r.pct99}</td><td>${r.error_pct}</td>
      </tr>`);
    });
    // initialize DataTable if not already
    if(!$.fn.dataTable.isDataTable('#aggTable')) {
      $('#aggTable').DataTable({ "order": [[1, "desc"]], "pageLength": 10 });
    }
  }

  async function loadErrors() {
    const {start,end} = getRangeParams();
    let url = '/api/errors?';
    if(start) url += 'start=' + start + '&';
    if(end) url += 'end=' + end + '&';
    const resp = await fetch(url);
    const data = await resp.json();
    const tbody = $('#errTable tbody').empty();
    data.forEach(r => {
      tbody.append(`<tr><td>${r.label}</td><td>${r.status}</td><td>${r.count}</td><td>${r.message||''}</td></tr>`);
    });
  }

  async function loadSuccess() {
    const {start,end} = getRangeParams();
    let url = '/api/success?';
    if(start) url += 'start=' + start + '&';
    if(end) url += 'end=' + end + '&';
    const resp = await fetch(url);
    const data = await resp.json();
    const tbody = $('#succTable tbody').empty();
    data.forEach(r => {
      tbody.append(`<tr><td>${r.label}</td><td>${r.count}</td><td>${r.avg}</td><td>${r.min}</td><td>${r.max}</td></tr>`);
    });
  }

  async function refreshAll() {
    await Promise.all([loadTPS(), loadThreads(), loadAggregate(), loadErrors(), loadSuccess()]);
  }

  // ---------- Auto-refresh handling ----------
  function setAutoRefresh(seconds) {
    if(autoHandle) { clearInterval(autoHandle); autoHandle = null; }
    if(seconds > 0) autoHandle = setInterval(refreshAll, seconds * 1000);
  }
  // init auto refresh value
  setAutoRefresh(parseInt($('#refreshSelect').val()));

  // ---------- Buttons ----------
  $('#refreshSelect').on('change', function(){
    const s = parseInt($(this).val());
    setAutoRefresh(s);
  });

  $('#applyRange').on('click', function(){ refreshAll(); });

  $('#labelFilter').on('change', function(){ loadAggregate(); });

  $('#downloadAgg').on('click', function(){
    const {start,end} = getRangeParams();
    const label = $('#labelFilter').val();
    let url = '/download/aggregate.csv?';
    if(label) url += 'label=' + encodeURIComponent(label) + '&';
    if(start) url += 'start=' + start + '&';
    if(end) url += 'end=' + end + '&';
    window.location = url;
  });

  $('#downloadErr').on('click', function(){
    const {start,end} = getRangeParams();
    let url = '/download/errors.csv?';
    if(start) url += 'start=' + start + '&';
    if(end) url += 'end=' + end + '&';
    window.location = url;
  });

  $('#downloadSucc').on('click', function(){
    const {start,end} = getRangeParams();
    let url = '/download/success.csv?';
    if(start) url += 'start=' + start + '&';
    if(end) url += 'end=' + end + '&';
    window.location = url;
  });

  $('#downloadSnapshot').on('click', function(){
    window.location = '/download/snapshot.html';
  });

  // copy table buttons
  $('#copyAgg').on('click', function(){
    const csv = tableToCSV($('#aggTable'));
    navigator.clipboard.writeText(csv).then(()=>alert('Aggregate table copied to clipboard'));
  });
  $('#copyErr').on('click', function(){
    const csv = tableToCSV($('#errTable'));
    navigator.clipboard.writeText(csv).then(()=>alert('Error table copied to clipboard'));
  });
  $('#copySucc').on('click', function(){
    const csv = tableToCSV($('#succTable'));
    navigator.clipboard.writeText(csv).then(()=>alert('Success table copied to clipboard'));
  });

  // chart copy/download
  function enableChartButtons(chart, copyBtnId, downloadBtnId, filename) {
    $(copyBtnId).on('click', async function(){
      try {
        const url = chart.toBase64Image();
        const res = await fetch(url);
        const blob = await res.blob();
        await navigator.clipboard.write([new ClipboardItem({[blob.type]: blob})]);
        alert('Chart image copied to clipboard (if supported by browser).');
      } catch (e) {
        // fallback: download
        alert('Copy to clipboard not supported — downloading PNG instead.');
        $(downloadBtnId).click();
      }
    });
    $(downloadBtnId).on('click', function(){
      const a = document.createElement('a');
      a.href = chart.toBase64Image();
      a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
    });
  }

  enableChartButtons(tpsChart, '#copyTps', '#downloadTpsPng', 'tps.png');
  enableChartButtons(threadChart, '#copyThreads', '#downloadThreadsPng', 'threads.png');

  // initial load
  $(document).ready(function(){
    refreshAll();
    // also refresh once after 1 second to get up-to-date
    setTimeout(refreshAll, 1000);
  });
</script>

</body>
</html>
    """, labels=labels)
    return html

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
