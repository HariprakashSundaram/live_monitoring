import sqlite3
conn = sqlite3.connect("jmeter_metrics.db")
test_id = "TestDryRun"
rows = conn.execute("SELECT * FROM jmeter_samples WHERE test_id = ?", (test_id,)).fetchall()
print(len(rows), "rows for test_id:", test_id)