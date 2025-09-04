# jmeter_dashboard.py copy 2
from flask import Flask, request, jsonify, render_template_string, send_file, make_response
import sqlite3, time, statistics, csv, io, json
from datetime import datetime
import math

app = Flask(__name__)
DB_FILE = "jmeter_metrics.db"
def jmeter_percentile(data, percentile):
    n = len(data)
    if n == 0:
        return 0
    data_sorted = sorted(data)
    # JMeter nearest-rank method: ceil(p/100 * n)
    rank = math.ceil((percentile / 100) * n)
    rank = max(1, min(rank, n))  # clamp between 1 and n
    return data_sorted[rank - 1]  # convert to 0-based index
def custom_percentile(data, percentile):
    n = len(data)
    if n == 0:
        return 0
    data_sorted = sorted(data)
    raw_rank = (percentile / 100) * n
    
    if percentile == 90:
        # floor for 90th percentile
        rank = math.floor(raw_rank)
    else:
        # ceil for others
        rank = math.ceil(raw_rank)

    # adjust for 1-based rank
    rank = max(1, min(rank, n))
    return data_sorted[rank - 1]
def jmeter_median(data):
    n = len(data)
    if n == 0:
        return 0
    data_sorted = sorted(data)
    # JMeter takes ceil(n/2)
    rank = math.ceil(n / 2)
    return data_sorted[rank - 1]
# --------- DB ----------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS jmeter_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            label TEXT,
            response_time REAL,
            success INTEGER,
            thread_count INTEGER,
            status_code TEXT,
            error_message TEXT,
            received_bytes REAL,
            sent_bytes REAL,
            test_id TEXT                -- Add this column
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
    data = request.get_json(force=True)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO jmeter_samples (
            timestamp, label, response_time, success, thread_count,
            status_code, error_message, received_bytes, sent_bytes, test_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("timestamp"),
        data.get("label"),
        data.get("response_time"),
        data.get("success"),
        data.get("thread_count"),
        data.get("status_code"),
        data.get("error_message"),
        data.get("received_bytes", 0),
        data.get("sent_bytes", 0),
        data.get("test_id", "default")
    ))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

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
    test_id = request.args.get("test_id", "default")
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    conds = ["test_id = ?"]
    params = [test_id]
    if start:
        conds.append("timestamp >= ?")
        params.append(start)
    if end:
        conds.append("timestamp <= ?")
        params.append(end)
    where = " AND ".join(conds)
    q = f"SELECT label, response_time, success, received_bytes, sent_bytes, timestamp FROM jmeter_samples WHERE {where}"
    rows = run_query(q, tuple(params))
    print("Aggregate query:", q, params, "Rows:", len(rows))  # Debug

    agg = {}
    for lab, rt, succ, recv, sent, ts in rows:
        if lab not in agg:
            agg[lab] = {
                "samples": [],
                "errors": 0,
                "received_bytes": 0,
                "sent_bytes": 0,
                "timestamps": []
            }
        agg[lab]["samples"].append(rt)
        agg[lab]["received_bytes"] += recv if recv else 0
        agg[lab]["sent_bytes"] += sent if sent else 0
        agg[lab]["timestamps"].append(ts)
        if succ == 0:
            agg[lab]["errors"] += 1
    
    res = []
    for lab, d in agg.items():
        s = sorted(d["samples"])
        count = len(s)
        avg = round(sum(s)/count, 2) if count else 0
        mn = s[0] if s else 0
        mx = s[-1] if s else 0
        # median = round(statistics.median(s), 2) if s else 0
        # p90 = round(percentile(s, 90),2) if s else 0
        # p95 = round(percentile(s, 95),2) if s else 0
        # p99 = round(percentile(s, 99),2) if s else 0
        median=jmeter_median(s)
        p90=custom_percentile(s,90)
        p95=jmeter_percentile(s,95)
        p99=jmeter_percentile(s,99)
        errors = d["errors"]
        err_pct = round((errors/count)*100,2) if count else 0
        # Throughput and KB/sec calculations
        timestamps = d["timestamps"]
        duration = (max(timestamps) - min(timestamps) + 1) if timestamps else 1
        throughput = round(count / duration, 5) if duration > 0 else 0
        received_kb_sec = round((d["received_bytes"] / 1024) / duration, 2) if duration > 0 else 0
        sent_kb_sec = round((d["sent_bytes"] / 1024) / duration, 2) if duration > 0 else 0
        res.append({
            "test_id": test_id,
            "label": lab,
            "count": count,
            "avg": avg,
            "median": median,
            "min": mn,
            "max": mx,
            "pct90": p90,
            "pct95": p95,
            "pct99": p99,
            "error_pct": err_pct,
            "throughput": throughput,
            "received_kb_sec": received_kb_sec,
            "sent_kb_sec": sent_kb_sec
        })
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
@app.route("/api/errorpct", methods=["GET"])
def api_errorpct():
    """
    Returns per-second error percentage for a time window.
    Accepts either:
      - window (seconds) and optional end (epoch seconds), OR
      - start and optional end (both epoch seconds).
    Optional query param: label (to filter by label).
    Response:
      { "timestamps": [...], "error_pct": [...] }
    """
    window = request.args.get("window", type=int)
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    label = request.args.get("label")

    # choose range
    if window:
        end = end or int(time.time())
        start = end - window + 1
    else:
        end = end or int(time.time())
        start = start or (end - 60 + 1)  # default last 60s

    # Query totals and errors per second (optionally filtered by label)
    if label:
        total_q = "SELECT timestamp, COUNT(*) FROM jmeter_samples WHERE timestamp BETWEEN ? AND ? AND label = ? GROUP BY timestamp"
        err_q = "SELECT timestamp, COUNT(*) FROM jmeter_samples WHERE timestamp BETWEEN ? AND ? AND success=0 AND label = ? GROUP BY timestamp"
        total_rows = run_query(total_q, (start, end, label))
        err_rows = run_query(err_q, (start, end, label))
    else:
        total_q = "SELECT timestamp, COUNT(*) FROM jmeter_samples WHERE timestamp BETWEEN ? AND ? GROUP BY timestamp"
        err_q = "SELECT timestamp, COUNT(*) FROM jmeter_samples WHERE timestamp BETWEEN ? AND ? AND success=0 GROUP BY timestamp"
        total_rows = run_query(total_q, (start, end))
        err_rows = run_query(err_q, (start, end))

    total_map = {r[0]: r[1] for r in total_rows}
    err_map = {r[0]: r[1] for r in err_rows}

    timestamps = []
    error_pct = []
    for sec in range(start, end + 1):
        timestamps.append(sec)
        tot = total_map.get(sec, 0)
        err = err_map.get(sec, 0)
        pct = round((err / tot * 100.0) if tot else 0.0, 2)
        error_pct.append(pct)

    return jsonify({"timestamps": timestamps, "error_pct": error_pct})

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
  <title>Live Monitoring</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css" rel="stylesheet">
  <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/flatpickr"></script>
  <link href="https://cdn.jsdelivr.net/npm/flatpickr/dist/flatpickr.min.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/moment@2.29.4/moment.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/moment-timezone@0.5.43/builds/moment-timezone-with-data.min.js"></script>
  <script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
  <style>
    body{padding:20px;background:#f5f7fb}
    .card{border-radius:12px;box-shadow:0 6px 18px rgba(0,0,0,0.06)}
  </style>
</head>
<body>
  <h2>Live monitoring</h2>
  <div class="container-fluid">
    <div class="d-flex justify-content-between align-items-center mb-3">
      
      <br>
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
        <br><hr>
        <label class="me-2">Duration:</label>
        <select id="durationSelect" class="form-select d-inline-block w-auto me-2">
          <option value="">--</option>
          <option value="60">1 min</option>
          <option value="300">5 min</option>
          <option value="600">10 min</option>
          <option value="1800">30 min</option>
          <option value="3600">1 hr</option>
          <option value="7200">2 hr</option>
          <option value="10800">3 hr</option>
          <option value="21600">6 hr</option>
          <option value="43200">12 hr</option>
          <option value="86400">24 hr</option>
          <option value="172800">48 hr</option>
        </select>

        <button id="resetRange" class="btn btn-outline-secondary btn-sm">Reset</button>
        <span id="autoStatus" class="badge bg-info ms-2" style="display:none;">Auto-refresh ON</span>
        <!-- Add this inside your dashboard controls section -->
        <label for="testIdSelect" class="form-label me-2">TestId:</label>
        <select id="testIdSelect" class="form-select form-select-sm" style="width:auto;display:inline-block;">
          <!-- Options will be populated dynamically -->
        </select>
        </div>
    </div>

    <div class="row g-3">
      <div class="col-lg-6">
        <div class="card p-3">
          <div class="d-flex justify-content-between">
            <h5>TPS (per second)</h5>
          </div>
          <canvas id="tpsChart" height="160"></canvas>
          <div id="rangeDisplayTps" class="text-secondary small mt-2 text-end"></div>
        </div>
      </div>

      <div class="col-lg-6">
        <div class="card p-3">
          <div class="d-flex justify-content-between">
            <h5>Active Threads (avg per sec)</h5>
          </div>
          <canvas id="threadChart" height="160"></canvas>
          <div id="rangeDisplayThreads" class="text-secondary small mt-2 text-end"></div>
        </div>
      </div>
      <div class="col-lg-6">
        <div class="card p-3">
          <div class="d-flex justify-content-between">
            <h5>Error Percentage</h5>
          </div>
          <canvas id="errorPctChart" height="160"></canvas>
          <div id="rangeDisplayErrorPct" class="text-secondary small mt-2 text-end"></div>
        </div>
      </div>                              

      <div class="col-12">
        <div class="card p-3">
          <div class="d-flex justify-content-between mb-2">
            <h5>Aggregate Report</h5>
          </div>
          <table id="aggTable" class="table table-striped table-bordered">
  <thead class="table-dark">
    <tr>
      <th>TestId</th>
      <th>Label</th>
      <th>Count</th>
      <th>Avg</th>
      <th>Median</th>
      <th>Min</th>
      <th>Max</th>
      <th>90%</th>
      <th>95%</th>
      <th>99%</th>
      <th>Error %</th>
      <th>Throughput</th>
      <th>Received KB/sec</th>
      <th>Sent KB/sec</th>
    </tr>
  </thead>
  <tbody></tbody>
</table>
          <div id="rangeDisplayAgg" class="text-secondary small mt-2 text-end"></div>
        </div>
      </div>

      <div class="col-lg-12">
        <div class="card p-3">
          <div class="d-flex justify-content-between mb-2">
            <h5>Errors</h5>
          </div>
          <table id="errTable" class="table table-sm table-hover">
            <thead><tr><th>Label</th><th>Status</th><th>Count</th><th>Messages</th></tr></thead>
            <tbody></tbody>
          </table>
          <div id="rangeDisplayErr" class="text-secondary small mt-2 text-end"></div>
        </div>
      </div>

      <div class="col-lg-6">
        <div class="card p-3">
          <div class="d-flex justify-content-between mb-3">
            <h5>Successful Transactions</h5>
          </div>
          <table id="succTable" class="table table-sm table-hover">
            <thead><tr><th>Label</th><th>Count</th><th>Avg</th><th>Min</th><th>Max</th></tr></thead>
            <tbody></tbody>
          </table>
          <div id="rangeDisplaySucc" class="text-secondary small mt-2 text-end"></div>
        </div>
      </div>
      <div class="col-12 text-end">
        <a id="downloadSnapshot" class="btn btn-outline-primary">Download Hard-coded HTML Snapshot</a>
      </div>
    </div>
  </div>
  <div id="loadingStatus" class="position-absolute top-0 end-0 m-3" style="z-index:1000;">
  <span class="badge bg-success" style="display:none;">Loading completed</span>
</div>

<script>
  

                                  
  // ---------- Utilities ----------
  function epochToTZString(sec, tz, format12) {
    if(!sec) return "";
    let m = moment.unix(sec).tz(tz);
    return format12 ? m.format('YYYY-MM-DD hh:mm:ss A') : m.format('YYYY-MM-DD HH:mm:ss');
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
  const tzs = moment.tz.names();
  tzs.forEach(t => { $('#tzSelect').append(new Option(t, t)); });
  $('#tzSelect').val(moment.tz.guess());

  flatpickr("#startPicker", { enableTime:true, time_24hr:false, dateFormat:"Y-m-d h:i K" });
  flatpickr("#endPicker", { enableTime:true, time_24hr:false, dateFormat:"Y-m-d h:i K" });

  // ---------- Charts ----------
  let errorPctChart = new Chart(document.getElementById('errorPctChart'), {
    type: 'line',
    data: {
        labels: [],
        datasets: [{
        label: 'Error %',
        data: [],
        borderColor: 'red',
        backgroundColor: 'rgba(255,0,0,0.2)',
        borderWidth: 2,
        tension: 0.3
        }]
    },
    options: {
        scales: {
        x: { ticks: { maxRotation: 45 } },
        y: {
            beginAtZero: true,
            title: { display: true, text: "%" }
        }
        }
    }
    });
                                
  let tpsChart = new Chart(document.getElementById('tpsChart'), {
    type:'line',
    data:{
        labels:[],
        datasets:[{
        label:'TPS',
        data:[],
        tension:0.3,
        borderColor: 'blue',        // line color
        borderWidth: 3,             // line thickness
        pointRadius: 2,             // point size
        backgroundColor: 'rgba(0,0,255,0.1)' // optional fill under line
        }]
    }
    });
  let threadChart = new Chart(document.getElementById('threadChart'), {
  type:'line',
  data:{
    labels:[],
    datasets:[{
      label:'Threads',
      data:[],
      tension:0.3,
      borderColor: 'green',       // line color
      borderWidth: 2,             // line thickness
      pointRadius: 2,
      backgroundColor: 'rgba(0,255,0,0.1)'
    }]
  }
});

  // ---------- Data Params ----------
  function getRangeParams() {
    let tz = $('#tzSelect').val();
    let duration = $('#durationSelect').val();
    let startRaw = $('#startPicker').val();
    let endRaw = $('#endPicker').val();
    let start = null, end = null;

    if (duration) {
        // Duration mode: ignore start/end pickers
        end = moment().unix();
        start = end - parseInt(duration);
        return { start, end, tz, duration: parseInt(duration), mode: 'duration' };
    } else if (startRaw || endRaw) {
        // Start/end mode: ignore duration
        if (startRaw) start = moment.tz(startRaw, 'YYYY-MM-DD hh:mm:ss A', tz).unix();
        if (endRaw) end = moment.tz(endRaw, 'YYYY-MM-DD hh:mm:ss A', tz).unix();
        // If only start is set, default end to now
        if (start && !end) end = moment().unix();
        return { start, end, tz, duration: null, mode: 'range' };
    } else {
        // No filters: show all time
        return { start: null, end: null, tz, duration: null, mode: 'all' };
    }
  }
    function getRangeParamsNew() {
    const start = $('#startTime').val() ? Math.floor(new Date($('#startTime').val()).getTime()/1000) : null;
    const end   = $('#endTime').val() ? Math.floor(new Date($('#endTime').val()).getTime()/1000) : null;
    const duration = $('#durationSelect').val() ? parseInt($('#durationSelect').val()) : null;
    return { start, end, duration };
    }


  // ---------- Loaders ----------
    async function loadErrorPct() {
    const {start, end} = getRangeParams();
    let window = 60;
    if (start && end) {
        window = Math.max(60, end - start + 1);
    }
    const resp = await fetch('/api/errorpct?window=' + window + (end ? ('&end=' + end) : ''));
    const data = await resp.json();
    const tz = $('#tzSelect').val();
    let labels = data.timestamps.map(s => moment.unix(s).tz(tz).format('HH:mm:ss A'));
    errorPctChart.data.labels = labels;
    errorPctChart.data.datasets[0].data = data.error_pct;
    errorPctChart.update();
    }

  async function loadTPS() {
    const { start, end, duration, mode } = getRangeParams();
    let url = '/api/tps?';
    if (mode === 'duration') {
        url += 'window=' + duration + '&end=' + end;
    } else if (mode === 'range') {
        url += 'window=' + Math.max(60, end - start + 1) + '&end=' + end;
    } else {
        url += 'window=60';
    }
    const resp = await fetch(url);
    const data = await resp.json();
    const tz = $('#tzSelect').val();
    tpsChart.data.labels = data.timestamps.map(s => moment.unix(s).tz(tz).format('HH:mm:ss A')); 
    tpsChart.data.datasets[0].data = data.tps;
    tpsChart.update();
  }

  async function loadThreads() {
    const {start, end} = getRangeParams();
    let window = (start && end) ? Math.max(60, end - start + 1) : 60;
    const resp = await fetch('/api/threads?window=' + window + (end?('&end='+end):''));
    const data = await resp.json();
    const tz = $('#tzSelect').val();
    threadChart.data.labels = data.timestamps.map(s => moment.unix(s).tz(tz).format('HH:mm:ss A'));
    threadChart.data.datasets[0].data = data.threads;
    threadChart.update();
  }
  

    function getTestId() {
    return $('#testIdSelect').val();
    }

    async function loadAggregate() {
    const { start, end } = getRangeParams();
    const testId = getTestId();
    const params = new URLSearchParams();
    params.append('test_id', testId);
    if (start) params.append('start', start);
    if (end) params.append('end', end);

    const resp = await fetch('/api/aggregate?' + params.toString());
    const data = await resp.json();

    // Initialize DataTable only once
    let table;
    if (!$.fn.dataTable.isDataTable('#aggTable')) {
        table = $('#aggTable').DataTable({ order: [[2, "desc"]], pageLength: 10 });
    } else {
        table = $('#aggTable').DataTable();
        table.clear();
    }

    // Use DataTables API to add rows
    data.forEach(r => {
      table.row.add([
        r.test_id,
        r.label,
        r.count,
        r.avg,
        r.median,
        r.min,
        r.max,
        r.pct90,
        r.pct95,
        r.pct99,
        r.error_pct,
        r.throughput,
        r.received_kb_sec,
        r.sent_kb_sec
      ]);
    });
    table.draw();
}
                                

  async function loadErrors() {
    const {start,end} = getRangeParams();
    let url = '/api/errors?';
    if(start) url += 'start=' + start + '&';
    if(end) url += 'end=' + end + '&';
    const data = await (await fetch(url)).json();
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
    const data = await (await fetch(url)).json();
    const tbody = $('#succTable tbody').empty();
    data.forEach(r => {
      tbody.append(`<tr><td>${r.label}</td><td>${r.count}</td><td>${r.avg}</td><td>${r.min}</td><td>${r.max}</td></tr>`);
    });
  }

  async function refreshAll() {
    $('#loadingStatus span').hide();
    updateRangeDisplays();                                
    await Promise.all([loadTPS(), loadThreads(), loadErrorPct(), loadAggregate(), loadErrors(), loadSuccess()]);
    setAutoRefresh(parseInt($('#refreshSelect').val()));
    $('#loadingStatus span').show();
    setTimeout(() => { $('#loadingStatus span').fadeOut(); }, 2000); // Hide after 2 seconds
  }

  // ---------- Auto-refresh ----------
  let autoHandle = null;
  function setAutoRefresh(seconds) {
    if(autoHandle) { clearInterval(autoHandle); autoHandle = null; }
    if(seconds > 0) {
      const { start, end, duration } = getRangeParams();
      if(start && !end && !duration) {
        $('#autoStatus').show();
        autoHandle = setInterval(refreshAll, seconds * 1000);
      } else {
        $('#autoStatus').hide();
      }
    } else {
      $('#autoStatus').hide();
    }
  }
  setAutoRefresh(parseInt($('#refreshSelect').val()));

  // ---------- Events ----------
  $('#refreshSelect').on('change', function(){ setAutoRefresh(parseInt($(this).val())); });
  $('#labelFilter').on('change', loadAggregate);
  $('#startPicker,#endPicker,#durationSelect').on('change', refreshAll);
  $('#startPicker,#endPicker').on('change', function() {
    if ($('#startPicker').val() || $('#endPicker').val()) {
        $('#durationSelect').val('');
    }
});
  $('#resetRange').on('click', function(){
    $('#startPicker').val(''); $('#endPicker').val(''); $('#durationSelect').val('');
    refreshAll();
  });

  // Copy & download table/chart handlers unchanged...
  function enableChartButtons(chart, copyBtnId, downloadBtnId, filename) {
    $(copyBtnId).on('click', async function(){
      try {
        const url = chart.toBase64Image();
        const res = await fetch(url);
        const blob = await res.blob();
        await navigator.clipboard.write([new ClipboardItem({[blob.type]: blob})]);
        alert('Chart image copied to clipboard (if supported by browser).');
      } catch { $(downloadBtnId).click(); }
    });
    $(downloadBtnId).on('click', function(){
      const a = document.createElement('a');
      a.href = chart.toBase64Image(); a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
    });
  }
  enableChartButtons(tpsChart, '#copyTps', '#downloadTpsPng', 'tps.png');
  enableChartButtons(threadChart, '#copyThreads', '#downloadThreadsPng', 'threads.png');

  // ---------- Range Display Update ----------
  function updateRangeDisplays() {
    const { start, end, tz } = getRangeParams();
    let startStr = start ? epochToTZString(start, tz, true) : '';
    let endStr = end ? epochToTZString(end, tz, true) : '';
    let rangeText = '';
    if (startStr && endStr) {
        rangeText = `${startStr} to ${endStr}`;
    } else if (startStr) {
        rangeText = `${startStr} to Now`;
    } else if (endStr) {
        rangeText = `Up to ${endStr}`;
    } else {
        rangeText = 'All Time';
    }
    $('#rangeDisplayAgg').text(rangeText);
    $('#rangeDisplayTps').text(rangeText);
    $('#rangeDisplayErr').text(rangeText);
    $('#rangeDisplaySucc').text(rangeText);
    $('#rangeDisplayThreads').text(rangeText);
    $('#rangeDisplayErrorPct').text(rangeText);
}

  // Initial load
  $(document).ready(function(){ refreshAll(); setTimeout(refreshAll, 1000); });
  async function loadTestIds() {
    const resp = await fetch('/api/testids');
    const testIds = await resp.json();
    const sel = $('#testIdSelect').empty();
    testIds.forEach(id => {
        sel.append(`<option value="${id}">${id}</option>`);
    });
}
$(document).ready(function() {
    loadTestIds();
    // ...other init code...
});
</script>

</body>
</html>
    """, labels=labels)
    return html

@app.route("/api/testids", methods=["GET"])
def api_testids():
    rows = run_query("SELECT DISTINCT test_id FROM jmeter_samples")
    return jsonify([r[0] for r in rows])

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)