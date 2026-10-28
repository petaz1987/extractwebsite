"""One-shot read-only observation of the existing Worten Search request in Docker DEV."""
import json
import os
import subprocess
from pathlib import Path
from main import app

def emit(data):
    print("WORTEN_DIAG " + json.dumps(data, separators=(",", ":"), ensure_ascii=True), flush=True)

original = subprocess.run
def observed_run(cmd, *args, **kwargs):
    if not (isinstance(cmd, list) and "--write-out" in cmd and "--output" in cmd):
        return original(cmd, *args, **kwargs)
    # Adds only response-header capture; no change to the actual HTTP request.
    output_file = cmd[cmd.index("--output") + 1]
    header_file = str(Path(output_file).with_name("headers-observed.txt"))
    index = cmd.index("--url")
    with_headers = [*cmd[:index], "--dump-header", header_file, *cmd[index:]]
    proc = original(with_headers, *args, **kwargs)
    telemetry = {}
    try:
        stream = kwargs.get("stdout")
        stream.flush()
        telemetry = json.loads(Path(stream.name).read_bytes()[:65536])
    except (OSError, ValueError, TypeError, AttributeError):
        emit({"case":"telemetry", "available": False})
    headers = ""
    try:
        headers = Path(header_file).read_text(encoding="latin-1")[:16384].lower()
    except OSError:
        pass
    fields = {"case":"upstream", "curl_exit":proc.returncode,
              "http":telemetry.get("http_code"),
              "http_version":telemetry.get("http_version"),
              "remote_ip":telemetry.get("remote_ip"),
              "ssl_verify_result":telemetry.get("ssl_verify_result"),
              "time_connect":telemetry.get("time_connect"),
              "time_appconnect":telemetry.get("time_appconnect"),
              "time_starttransfer":telemetry.get("time_starttransfer"),
              "time_total":telemetry.get("time_total"),
              "size_download":telemetry.get("size_download"),
              "content_type":telemetry.get("content_type"),
              "cf_mitigated": "cf-mitigated:" in headers,
              "has_location": "\\nlocation:" in headers,
              "has_www_authenticate": "www-authenticate:" in headers}
    emit(fields)
    return proc

if __name__ == "__main__":
    subprocess.run = observed_run
    try:
        with app.test_client() as client:
            resp = client.get("/extract", query_string={
                "mode":"search",
                "url":"https://canarias.worten.es/search?query=radiador%20de%20aceite"
            }, headers={"Authorization":"Bearer "+os.environ["EXTRACT_API_TOKEN"]})
            data = resp.get_json(silent=True) or {}
            emit({"case":"result","http":resp.status_code,
                  "extraction_status":data.get("extraction_status"),
                  "error":(data.get("error") or {}).get("code"),
                  "count":data.get("product_count")})
    except Exception as exc:
        emit({"case":"diagnostic_exception","type":type(exc).__name__})
    finally:
        subprocess.run = original
