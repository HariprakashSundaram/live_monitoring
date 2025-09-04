import java.net.URL
import java.net.HttpURLConnection

// Collect JMeter metrics
def label        = prev.getSampleLabel()
def responseTime = prev.getTime()
def success      = prev.isSuccessful()? 1: 0
def threadCount  = ctx.getThreadGroup().getNumberOfThreads()
def timeStamp    = System.currentTimeMillis()

def tps = responseTime > 0 ? (1000.0 / responseTime) : 0.0
def errorPct = success ? 0.0 : 100.0
def status_code = prev.getResponseCode()
def error_message = prev.isSuccessful() ? "" : prev.getResponseMessage()
def received_bytes = prev.getBytes()
def sent_bytes = prev.getSentBytes()
def test_id = vars.get("test_id") ?: "TestRun_" + new Date().format("yyyyMMdd_HHmmss")
def json = """{
  "label": "${label}",
  "thread_count": ${threadCount},
  "success": ${success},
  "tps": ${tps},
  "response_time": ${responseTime},
  "errorPct": ${errorPct},
  "timestamp": ${timeStamp},
  "status_code": "${status_code}",
  "error_message": "${error_message}",
  "received_bytes": ${received_bytes},
  "sent_bytes": ${sent_bytes},
  "test_id": "${test_id}"
}"""
"

try {
    def url = new URL("http://localhost:5000/metrics")
    def conn = url.openConnection() as HttpURLConnection
    conn.setDoOutput(true)
    conn.setRequestMethod("POST")
    conn.setRequestProperty("Content-Type", "application/json")

    conn.outputStream.withWriter("UTF-8") { it.write(json) }

    log.info("Posted metrics: " + json)
    log.info("Response code: " + conn.responseCode)

    conn.disconnect()
} catch (Exception e) {
    log.error("Error sending metrics", e)
}
