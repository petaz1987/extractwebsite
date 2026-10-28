"""Temporary, one-shot read-only comparison of IKEA agent behavior in two DEV runtimes.

The same configured DEV bearer token is used without printing or storing it.
Do not publish response bodies, credentials, DNS addresses, or request headers.
"""
import json
import os
import time

import requests
from main import app

TARGET = "https://www.islas.ikea.es/tenerife/es/pd/baggebo-libreria-blanco-art-20436713"
PYTHON_DEV = "https://extractwebsite-worten-dev.onrender.com"
AUTH = {"Authorization": "Bearer " + os.environ["EXTRACT_API_TOKEN"]}


def log(**fields):
    print("DEV_COMPARE " + json.dumps(fields, sort_keys=True, separators=(",", ":")), flush=True)


def payload(result, case, start):
    body = result.get_json(silent=True) or {}
    return {"case": case, "http": result.status_code,
            "elapsed_ms": round((time.monotonic() - start)*1000),
            "status": body.get("content_status"), "usable": body.get("usable"),
            "error": (body.get("error") or {}).get("code")}


def local_request():
    original = requests.Session.get
    def instrument(self, *args, **kwargs):
        start = time.monotonic()
        try:
            res = original(self, *args, **kwargs)
            log(case="docker_upstream_fetch", outcome="returned",
                http=res.status_code, elapsed_ms=round((time.monotonic()-start)*1000))
            return res
        except requests.RequestException as exc:
            log(case="docker_upstream_fetch", outcome=type(exc).__name__,
                elapsed_ms=round((time.monotonic()-start)*1000))
            raise
    requests.Session.get = instrument
    try:
        with app.test_client() as client:
            start = time.monotonic()
            response = client.get("/extract", query_string={"mode":"agent","url":TARGET},
                                  headers=AUTH)
            log(**payload(response,"docker_ikea_agent",start))
    except Exception as exc:
        log(case="docker_ikea_agent", exception_type=type(exc).__name__)
    finally:
        requests.Session.get = original


def remote_request():
    session = requests.Session()
    session.trust_env = False
    try:
        start = time.monotonic()
        health = session.get(PYTHON_DEV + "/health", timeout=(10,50))
        log(case="python_dev_health", http=health.status_code,
            elapsed_ms=round((time.monotonic()-start)*1000))
        if health.status_code != 200:
            log(case="python_dev_ikea_agent", skipped="health_not_200")
            return
        start = time.monotonic()
        response = session.get(PYTHON_DEV + "/extract",
                               params={"mode":"agent","url":TARGET},
                               headers=AUTH, timeout=(10,35))
        content = response.json() if response.headers.get("content-type","").startswith("application/json") else {}
        log(case="python_dev_ikea_agent", http=response.status_code,
            elapsed_ms=round((time.monotonic()-start)*1000),
            status=content.get("content_status"),
            usable=content.get("usable"),
            error=(content.get("error") or {}).get("code"))
    except requests.RequestException as exc:
        log(case="python_dev_compare", exception_type=type(exc).__name__)
    except ValueError:
        log(case="python_dev_compare", exception_type="invalid_json")
    finally:
        session.close()


if __name__ == "__main__":
    log(case="start", test="single_ikea_agent_per_environment")
    remote_request()
    local_request()
    log(case="done")
