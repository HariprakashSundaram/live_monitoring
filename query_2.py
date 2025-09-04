import sqlite3

def show_response_times(label, test_id="default"):
    conn = sqlite3.connect("jmeter_metrics.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT response_time FROM jmeter_samples WHERE label = ? AND test_id = ? ORDER BY timestamp",
        (label, test_id)
    )
    times = [row[0] for row in cursor.fetchall()]
    conn.close()
    print(f"Response times for label '{label}' (TestId: {test_id}):")
    print(times)

# Example usage:
show_response_times("Login", "TestDryRun11")