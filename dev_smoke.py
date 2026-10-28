"""One-deploy, read-only DEV smoke checks. Never prints authorization data or raw pages."""
import json
import os
import time

from main import app

AUTH = {"Authorization": "Bearer " + os.environ["EXTRACT_API_TOKEN"]}
SEARCH = "https://canarias.worten.es/search?query=radiador%20de%20aceite"
PDP = "https://canarias.worten.es/productos/radiador-aceite-orbegozo-rs2500-8235777"
IKEA = "https://www.islas.ikea.es/tenerife/es/pd/baggebo-libreria-blanco-art-20436713"


def emit(result):
    print("DEV_SMOKE " + json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)


def call(client, label, mode=None, url=None):
    start = time.monotonic()
    params = {"url": url, "mode": mode} if url is not None else None
    try:
        response = client.get("/extract" if params else "/health",
                              query_string=params, headers=AUTH if params else None)
        data = response.get_json(silent=True) or {}
        result = {"case": label, "http": response.status_code,
                  "elapsed_ms": round((time.monotonic() - start) * 1000),
                  "status": data.get("extraction_status", data.get("content_status")),
                  "error": (data.get("error") or {}).get("code")}
        if label == "health":
            result["healthy"] = data.get("status") == "ok"
        if mode == "search":
            products = data.get("products") or []
            result.update(count=data.get("product_count"), truncated=data.get("truncated"),
                          first_sku=products[0].get("sku") if products else None)
        elif mode == "product":
            product = data.get("product") or {}
            result.update(sku=product.get("sku"), price=product.get("price"),
                          currency=product.get("currency"), warnings=data.get("warnings"))
        elif mode in (None, "agent"):
            result["usable"] = data.get("usable")
        emit(result)
        return result
    except Exception as exc:
        result = {"case": label, "exception_type": type(exc).__name__}
        emit(result)
        return result


def main():
    client = app.test_client()
    emit({"case": "start", "kind": "isolated_dev_read_only", "network_retries": 0})
    call(client, "health")
    search = call(client, "worten-search", "search", SEARCH)
    if search.get("http") not in (403, 429) and search.get("error") not in (
            "access_denied", "upstream_access_denied", "challenge"):
        call(client, "worten-product", "product", PDP)
    else:
        emit({"case": "worten-product", "skipped": "search_access_denial"})
    call(client, "ikea-product", "product", IKEA)
    call(client, "ikea-agent", "agent", IKEA)
    call(client, "ikea-full", "", IKEA)
    emit({"case": "done"})


if __name__ == "__main__":
    main()
