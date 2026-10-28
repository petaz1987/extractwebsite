"""Isolated, bounded retrieval for Worten Canarias search and product pages."""

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from product_extraction import (
    _brand as _product_brand,
    _currency as _supported_currency,
    _availability_evidence,
    _find_main_context,
    _in_unrelated_product_section,
    _is_old_price,
    _number as _product_number,
    _record_image,
    _specifications,
)
from product_extraction import extract_product_content as _extract_product_html

WORTEN_HOST = "canarias.worten.es"
WORTEN_ORIGIN = f"https://{WORTEN_HOST}"
SEARCH_PATH = "/search"
SEARCH_API_URL = f"{WORTEN_ORIGIN}/worten-api/search-products"
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_CURL_TELEMETRY_BYTES = 64 * 1024
MAX_CURL_STDERR_BYTES = 16 * 1024
SEARCH_PAGE_SIZE = 48
MAX_OUTPUT_PRODUCTS = 20
MAX_SEARCH_HITS = 48
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 30_000
MAX_JSONLD_SCRIPTS = 20
MAX_JSONLD_SCRIPT_CHARS = 50_000
OPERATION_BUDGET_SECONDS = 15.0
MIN_CURL_VERSION = (8, 4, 0)
_PRODUCT_PATH = re.compile(r"^/productos/.+-([A-Za-z0-9]+)$")
_SKU = re.compile(r"^[A-Za-z0-9]{1,64}$")
_DNS_QUEUE = queue.Queue(maxsize=8)
_DNS_START_LOCK = threading.Lock()
_DNS_WORKER = None


class WortenAdapterError(Exception):
    """Safe error suitable for translation to the service's JSON error shape."""

    def __init__(self, code, message, status):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class _WortenChallenge(Exception):
    pass


def is_worten_host_url(url):
    try:
        return (urlsplit(url).hostname or "").casefold() == WORTEN_HOST
    except (TypeError, ValueError):
        return False


def _bad_url():
    return WortenAdapterError("invalid_url", "The requested Worten URL is not supported.", 400)


def _parse_worten_url(url, mode):
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise _bad_url()
    try:
        parts = urlsplit(url)
        port = parts.port
        query_items = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        raise _bad_url() from None
    if (
        parts.scheme.casefold() != "https"
        or (parts.hostname or "").casefold() != WORTEN_HOST
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.fragment
    ):
        raise _bad_url()
    if mode == "search":
        if parts.path != SEARCH_PATH:
            raise _bad_url()
        if len(query_items) != 1 or query_items[0][0] != "query" or not query_items[0][1].strip():
            raise WortenAdapterError(
                "invalid_search_parameters",
                "Only one non-empty query parameter is supported for Worten Search.",
                400,
            )
        return parts, query_items[0][1].strip(), None
    if mode == "product":
        if parts.query or parts.path.endswith("/"):
            raise _bad_url()
        match = _PRODUCT_PATH.fullmatch(parts.path)
        if not match or not _SKU.fullmatch(match.group(1)):
            raise _bad_url()
        return parts, None, match.group(1)
    raise _bad_url()


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0.01:
        raise WortenAdapterError("upstream_timeout", "The upstream request exceeded its time limit.", 504)
    return left


def _dns_worker():
    while True:
        callback, event, result = _DNS_QUEUE.get()
        try:
            result["value"] = callback(WORTEN_HOST)
        except BaseException as exc:  # carried back to the bounded caller
            result["error"] = exc
        finally:
            event.set()
            _DNS_QUEUE.task_done()


def _resolve_before_deadline(resolve_public_addresses, deadline):
    """Run the existing DNS/IP validator on one bounded daemon worker."""
    global _DNS_WORKER
    with _DNS_START_LOCK:
        if _DNS_WORKER is None or not _DNS_WORKER.is_alive():
            _DNS_WORKER = threading.Thread(target=_dns_worker, name="worten-dns-validation", daemon=True)
            _DNS_WORKER.start()
    event = threading.Event()
    result = {}
    try:
        _DNS_QUEUE.put_nowait((resolve_public_addresses, event, result))
    except queue.Full:
        raise WortenAdapterError("upstream_timeout", "The upstream host resolution exceeded its time limit.", 504) from None
    if not event.wait(_remaining(deadline)):
        raise WortenAdapterError("upstream_timeout", "The upstream host resolution exceeded its time limit.", 504)
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _controlled_env(executable, temp_dir):
    env = {"PATH": os.path.dirname(executable), "HOME": temp_dir, "TMPDIR": temp_dir}
    for key in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _curl_executable(deadline):
    executable = shutil.which("curl")
    if not executable:
        raise WortenAdapterError("runtime_unavailable", "The Worten retrieval runtime is unavailable.", 502)
    executable = os.path.abspath(executable)
    try:
        proc = subprocess.run(
            [executable, "--disable", "--version"],
            capture_output=True,
            text=True,
            timeout=min(1.0, _remaining(deadline)),
            check=False,
            env={"PATH": os.path.dirname(executable), "HOME": tempfile.gettempdir(), "TMPDIR": tempfile.gettempdir()},
        )
    except subprocess.TimeoutExpired:
        raise WortenAdapterError("upstream_timeout", "The retrieval runtime check exceeded its time limit.", 504) from None
    if proc.returncode != 0:
        raise WortenAdapterError("runtime_unavailable", "The Worten retrieval runtime is unavailable.", 502)
    match = re.match(r"curl\s+(\d+)\.(\d+)\.(\d+)", proc.stdout or "", re.I)
    if not match or tuple(map(int, match.groups())) < MIN_CURL_VERSION:
        raise WortenAdapterError("runtime_unavailable", "The installed curl version is not supported.", 502)
    return executable


def _curl_command_prefix(executable):
    """Return the executable argv prefix; separate for real-process test shims."""
    return [executable]


def _curl_url(url, payload, deadline, resolve_public_addresses):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or (parts.hostname or "").casefold() != WORTEN_HOST
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
    ):
        raise _bad_url()
    try:
        addresses = sorted(_resolve_before_deadline(resolve_public_addresses, deadline))
    except WortenAdapterError:
        raise
    except Exception as exc:
        if getattr(exc, "code", None) and getattr(exc, "status", None):
            raise
        raise WortenAdapterError("upstream_fetch_failed", "The upstream host could not be resolved.", 502) from None
    if not addresses:
        raise WortenAdapterError("upstream_fetch_failed", "The upstream host could not be resolved.", 502)
    # Pin one address from the fully validated DNS answer set so curl cannot perform
    # a second, unchecked resolution between validation and connection.
    address = addresses[0]
    pinned_address = f"[{address}]" if ":" in address else address
    executable = _curl_executable(deadline)
    with tempfile.TemporaryDirectory(prefix="extractwebsite-worten-") as temp_dir:
        body_path = os.path.join(temp_dir, "response.bin")
        args = _curl_command_prefix(executable) + [
            "--disable",  # must precede other options; suppresses a user curl config
            "--silent",
            "--show-error",
            "--proxy", "",
            "--proto", "=https",
            "--max-time", f"{_remaining(deadline):.3f}",
            "--connect-timeout", f"{min(5.0, _remaining(deadline)):.3f}",
            "--max-redirs", "0",
            "--retry", "0",
            "--max-filesize", str(MAX_BODY_BYTES),
            "--resolve", f"{WORTEN_HOST}:443:{pinned_address}",
            "--output", body_path,
            "--write-out", "%{json}",
        ]
        input_data = None
        if payload is not None:
            args.extend(["--header", "Content-Type: application/json", "--data-binary", "@-"])
            input_data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        args.extend(["--url", url])
        try:
            telemetry_path = os.path.join(temp_dir, "curl-telemetry.json")
            stderr_path = os.path.join(temp_dir, "curl-stderr.bin")
            with open(telemetry_path, "wb") as telemetry_stream, open(stderr_path, "wb") as stderr_stream:
                proc = subprocess.run(
                    args,
                    stdout=telemetry_stream,
                    stderr=stderr_stream,
                    timeout=_remaining(deadline),
                    check=False,
                    env=_controlled_env(executable, temp_dir),
                    input=input_data,
                )
        except subprocess.TimeoutExpired:
            raise WortenAdapterError("upstream_timeout", "The upstream request exceeded its time limit.", 504) from None
        if proc.returncode == 63:
            raise WortenAdapterError("response_too_large", "The upstream response exceeds the 5 MB limit.", 413)
        if proc.returncode != 0:
            raise WortenAdapterError("upstream_fetch_failed", "The upstream page could not be fetched.", 502)
        try:
            with open(telemetry_path, "rb") as telemetry_stream:
                telemetry_bytes = telemetry_stream.read(MAX_CURL_TELEMETRY_BYTES + 1)
            with open(stderr_path, "rb") as stderr_stream:
                stderr_bytes = stderr_stream.read(MAX_CURL_STDERR_BYTES + 1)
            if len(telemetry_bytes) > MAX_CURL_TELEMETRY_BYTES:
                raise ValueError("telemetry output too large")
            if len(stderr_bytes) > MAX_CURL_STDERR_BYTES:
                raise ValueError("curl diagnostic output too large")
            metrics = json.loads(telemetry_bytes.decode("utf-8"))
            body = open(body_path, "rb").read(MAX_BODY_BYTES + 1)
        except (OSError, ValueError, TypeError):
            raise WortenAdapterError("upstream_invalid_response", "The upstream response was invalid.", 502) from None
        if len(body) > MAX_BODY_BYTES or int(metrics.get("size_download", 0)) > MAX_BODY_BYTES:
            raise WortenAdapterError("response_too_large", "The upstream response exceeds the 5 MB limit.", 413)
        if _remaining(deadline) <= 0:
            raise WortenAdapterError("upstream_timeout", "The upstream request exceeded its time limit.", 504)
        content_type = (metrics.get("content_type") or "").split(";", 1)[0].strip().casefold()
        status_code = int(metrics.get("http_code") or 0)
        if status_code != 200:
            raise WortenAdapterError("upstream_access_denied" if status_code in {401, 403, 429} else "upstream_fetch_failed",
                                     "The upstream site denied the request." if status_code in {401, 403, 429} else "The upstream server returned an unsuccessful response.", 502)
        prefix = body[:10_000].lower()
        if b"cf-chl-" in prefix or b"<title>just a moment" in prefix:
            raise _WortenChallenge()
        return body, content_type


def _json_walk(value, deadline, max_nodes=MAX_JSON_NODES):
    stack = [(value, 0)]
    visited = 0
    while stack:
        if time.monotonic() > deadline:
            raise WortenAdapterError("upstream_timeout", "The upstream response exceeded its processing time limit.", 504)
        item, depth = stack.pop()
        visited += 1
        if visited > max_nodes or depth > MAX_JSON_DEPTH:
            raise WortenAdapterError("upstream_invalid_response", "The upstream response exceeded parsing limits.", 502)
        yield item
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values() if isinstance(child, (dict, list)))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item if isinstance(child, (dict, list)))


def _required_mapping(value, field):
    if not isinstance(value, dict):
        raise WortenAdapterError("upstream_invalid_response", "The upstream response was invalid.", 502)
    return value


def _text(value, limit=1000):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None


def _decimal_price(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0 or parsed > Decimal("1000000000"):
        return None
    return parsed


def _json_price(value):
    return int(value) if value == value.to_integral_value() else float(value)


def _https_pdp_url(raw_url, sku):
    if not isinstance(raw_url, str) or len(raw_url) > 2048:
        return None
    try:
        parts = urlsplit(raw_url)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.casefold() != "https"
        or (parts.hostname or "").casefold() != WORTEN_HOST
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or not parts.path.endswith("-" + sku)
        or not _PRODUCT_PATH.fullmatch(parts.path)
    ):
        return None
    return urlunsplit(("https", WORTEN_HOST, parts.path, "", ""))


def _search_maps(details):
    products_data = _required_mapping(details.get("productsData"), "productsData")
    canonicals_data = _required_mapping(details.get("productsCanonicalsData"), "productsCanonicalsData")
    offers_data = _required_mapping(details.get("offersData"), "offersData")

    def indexed(rows, key_fn, label):
        if not isinstance(rows, list) or len(rows) > 5000:
            raise WortenAdapterError("upstream_invalid_response", "The upstream response was invalid.", 502)
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise WortenAdapterError("upstream_invalid_response", "The upstream response was invalid.", 502)
            key = key_fn(row)
            if not isinstance(key, str) or not key:
                raise WortenAdapterError("upstream_invalid_response", "The upstream response was invalid.", 502)
            if key in result:
                raise WortenAdapterError("upstream_invalid_response", "The upstream response contained conflicting identifiers.", 502)
            result[key] = row
        return result

    products = indexed(products_data.get("products"), lambda p: (p.get("meta") or {}).get("refs", {}).get("webitem_id") if isinstance(p.get("meta"), dict) and isinstance(p.get("meta", {}).get("refs"), dict) else None, "product")
    canonicals = indexed(canonicals_data.get("web_items"), lambda p: p.get("id"), "canonical")
    offers = indexed(offers_data.get("offers"), lambda o: o.get("offer_id"), "offer")
    return products, canonicals, offers


def _search_product(hit, products, canonicals, offers):
    hit = _required_mapping(hit, "hit")
    hit_product = _required_mapping(hit.get("product"), "hit.product")
    webitem_id = hit_product.get("id")
    if not isinstance(webitem_id, str) or not webitem_id:
        raise WortenAdapterError("upstream_invalid_response", "The upstream response was invalid.", 502)
    product = products.get(webitem_id)
    canonical = canonicals.get(webitem_id)
    if product is None or canonical is None:
        raise WortenAdapterError("upstream_invalid_response", "A Search product could not be associated reliably.", 502)
    meta = product.get("meta")
    refs = meta.get("refs") if isinstance(meta, dict) else None
    sku = refs.get("sku") if isinstance(refs, dict) else None
    sku = str(sku) if isinstance(sku, (str, int)) and not isinstance(sku, bool) else None
    if not sku or not _SKU.fullmatch(sku):
        raise WortenAdapterError("upstream_invalid_response", "A Search product had no valid SKU.", 502)
    canonical_raw_url = canonical.get("url")
    canonical_resolved_url = urljoin(WORTEN_ORIGIN + "/", canonical_raw_url) if isinstance(canonical_raw_url, str) else None
    product_url = _https_pdp_url(canonical_resolved_url, sku)
    if not product_url or canonical.get("id") != webitem_id:
        raise WortenAdapterError("upstream_invalid_response", "A Search product canonical URL did not match its identity.", 502)
    offer_ref = product.get("woffer")
    offer_ref = offer_ref.get("offer_id") if isinstance(offer_ref, dict) else None
    offer = offers.get(offer_ref) if isinstance(offer_ref, str) else None
    if offer is None:
        raise WortenAdapterError("upstream_invalid_response", "A Search product offer could not be associated reliably.", 502)
    source_offer_code = offer.get("source_offer_code", sku)
    if str(source_offer_code) != sku:
        raise WortenAdapterError("upstream_invalid_response", "A Search offer did not match its product SKU.", 502)
    price_record = offer.get("price")
    price_record = _required_mapping(price_record, "offer.price")
    price = _decimal_price(price_record.get("final"))
    if price is None:
        raise WortenAdapterError("upstream_invalid_response", "A Search offer had no valid current price.", 502)
    properties = product.get("properties")
    properties = properties.get("text") if isinstance(properties, dict) else None
    properties = properties if isinstance(properties, dict) else {}
    name = _text(properties.get("name"))
    if not name:
        raise WortenAdapterError("upstream_invalid_response", "A Search product had no valid name.", 502)
    return {
        "name": name,
        "sku": sku,
        "price": _json_price(price),
        "currency": None,
        "variant": None,
        "url": product_url,
        "image_url": None,
    }


def _search_response(source_url, products, total_hits):
    unique = []
    identities = {}
    for product in products:
        identity = (product["sku"], product["url"])
        previous = identities.get(identity)
        if previous is not None:
            if previous != product:
                raise WortenAdapterError("upstream_invalid_response", "The upstream response contained conflicting product records.", 502)
            continue
        if any(existing["sku"] == product["sku"] and existing["url"] != product["url"] for existing in unique):
            raise WortenAdapterError("upstream_invalid_response", "A Search SKU referred to conflicting PDP URLs.", 502)
        identities[identity] = product
        unique.append(product)
    if len(unique) > MAX_OUTPUT_PRODUCTS:
        omitted = True
    else:
        omitted = False
    # Search is one fixed 48-row page. A larger service result count means the
    # single-page adapter did not exhaust the query even when the 48th row is
    # the last returned hit.
    truncated = omitted or total_hits > len(unique)
    output = unique[:MAX_OUTPUT_PRODUCTS]
    return {
        "mode": "search",
        "source_url": source_url,
        "extraction_status": "partial" if truncated else "ok",
        "product_count": len(output),
        "products": output,
        "truncated": truncated,
    }


def extract_worten_search(url, resolve_public_addresses):
    parts, query, _ = _parse_worten_url(url, "search")
    source_url = urlunsplit(("https", WORTEN_HOST, parts.path, parts.query, ""))
    deadline = time.monotonic() + OPERATION_BUDGET_SECONDS
    payload = {
        "query": query,
        "params": {
            "pageNumber": 0,
            "pageSize": SEARCH_PAGE_SIZE,
            "filters": [],
            "collapse": True,
            "favoriteStores": [],
            "debug": False,
        },
        "hasVariants": True,
        "debug": False,
    }
    try:
        body, content_type = _curl_url(SEARCH_API_URL, payload, deadline, resolve_public_addresses)
    except _WortenChallenge:
        return {"mode": "search", "source_url": source_url, "extraction_status": "unusable",
                "product_count": 0, "products": [], "truncated": False}
    if not body.strip():
        return {"mode": "search", "source_url": source_url, "extraction_status": "unusable",
                "product_count": 0, "products": [], "truncated": False}
    if content_type != "application/json":
        raise WortenAdapterError("upstream_invalid_response", "The upstream Search response was not JSON.", 502)
    try:
        response = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise WortenAdapterError("upstream_invalid_response", "The upstream Search response was invalid.", 502) from None
    if not isinstance(response, dict):
        raise WortenAdapterError("upstream_invalid_response", "The upstream Search response was invalid.", 502)
    search = _required_mapping(response.get("searchResponse"), "searchResponse")
    hits = search.get("hits")
    total_hits = search.get("totalHits")
    if isinstance(total_hits, bool) or not isinstance(total_hits, int) or total_hits < 0 or not isinstance(hits, list) or len(hits) > MAX_SEARCH_HITS:
        raise WortenAdapterError("upstream_invalid_response", "The upstream Search response was invalid.", 502)
    if total_hits == 0:
        if hits:
            raise WortenAdapterError("upstream_invalid_response", "The upstream Search response was inconsistent.", 502)
        return {"mode": "search", "source_url": source_url, "extraction_status": "no_products", "product_count": 0, "products": [], "truncated": False}
    if not hits or total_hits < len(hits):
        raise WortenAdapterError("upstream_invalid_response", "The upstream Search response was incomplete.", 502)
    details = response.get("detailsResponse")
    details = _required_mapping(details, "detailsResponse")
    products, canonicals, offers = _search_maps(details)
    valid_products = [_search_product(hit, products, canonicals, offers) for hit in hits]
    if time.monotonic() > deadline:
        raise WortenAdapterError("upstream_timeout", "The upstream response exceeded its processing time limit.", 504)
    return _search_response(source_url, valid_products, total_hits)


class _ProductPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jsonld = []
        self._script_type = None
        self._script_parts = None
        self._script_length = 0

    def handle_starttag(self, tag, attrs):
        if tag.casefold() == "script":
            attrs = dict(attrs)
            self._script_type = (attrs.get("type") or "").split(";", 1)[0].strip().casefold()
            self._script_parts = []
            self._script_length = 0

    def handle_endtag(self, tag):
        if tag.casefold() == "script" and self._script_parts is not None:
            if self._script_type == "application/ld+json" and len(self.jsonld) < MAX_JSONLD_SCRIPTS:
                raw = "".join(self._script_parts)
                if len(raw) <= MAX_JSONLD_SCRIPT_CHARS:
                    try:
                        self.jsonld.append(json.loads(raw))
                    except (ValueError, RecursionError):
                        pass
            self._script_parts = None
            self._script_type = None

    def handle_data(self, data):
        if self._script_parts is not None:
            if self._script_length + len(data) <= MAX_JSONLD_SCRIPT_CHARS:
                self._script_parts.append(data)
                self._script_length += len(data)
            else:
                self._script_length = MAX_JSONLD_SCRIPT_CHARS + 1


def _record_skus(record):
    values = set()
    for key in ("sku", "productID", "mpn"):
        value = record.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            normalized = str(value).strip()
            if normalized:
                values.add(normalized)
    return values


def _product_records(values, deadline):
    records = []
    for value in values:
        for item in _json_walk(value, deadline):
            if not isinstance(item, dict):
                continue
            raw_type = item.get("@type", [])
            types = [raw_type] if isinstance(raw_type, str) else raw_type if isinstance(raw_type, list) else []
            if any(str(item_type).casefold() == "product" for item_type in types):
                records.append(item)
                if len(records) > 100:
                    raise WortenAdapterError("upstream_invalid_response", "The PDP contained too many product records.", 502)
    return records


def _page_url_matches(raw_url, expected_url):
    if not raw_url:
        return True
    if not isinstance(raw_url, str):
        return False
    resolved_url = urljoin(expected_url, raw_url)
    try:
        parts = urlsplit(resolved_url)
        port = parts.port
    except (TypeError, ValueError):
        return False
    expected = urlsplit(expected_url)
    return (
        parts.scheme.casefold() == "https"
        and (parts.hostname or "").casefold() == WORTEN_HOST
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
        and parts.path.rstrip("/") == expected.path.rstrip("/")
    )


def _offer_facts(record, requested_sku, expected_url):
    offers = record.get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list) or not offers or len(offers) > 100:
        return None, None, False
    candidates = []
    for offer in offers:
        if not isinstance(offer, dict):
            return None, None, False
        offer_sku = _record_skus(offer)
        item_offered = offer.get("itemOffered")
        if isinstance(item_offered, dict):
            offer_sku.update(_record_skus(item_offered))
        if offer_sku and offer_sku != {requested_sku}:
            return None, None, False
        offer_url = offer.get("url")
        if offer_url and not _page_url_matches(offer_url, expected_url):
            return None, None, False
        price = _decimal_price(offer.get("price"))
        currency = offer.get("priceCurrency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            return None, None, False
        currency = _supported_currency(currency)
        if price is None or currency is None:
            return None, None, False
        candidates.append((price, currency))
    distinct = set(candidates)
    if len(distinct) != 1:
        return None, None, False
    return candidates[0][0], candidates[0][1], True


def _normalized_words(value):
    return " ".join((value or "").casefold().split())


def _visible_current_prices(soup, context):
    def is_price_node(node):
        classes = node.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        labels = [*classes, str(node.get("id", "")), str(node.get("itemprop", ""))]
        return any(re.search(r"(?:price|precio|importe)", label, re.I) for label in labels)

    candidates = [
        node for node in context.find_all(True)
        if is_price_node(node)
        and not _is_old_price(node, context)
        and not _in_unrelated_product_section(node, context)
    ]
    explicit_current = [node for node in candidates if re.search(r"current|actual|final|sale", " ".join(node.get("class", []) if isinstance(node.get("class", []), list) else [str(node.get("class", ""))]), re.I)]
    if explicit_current:
        selected = explicit_current
    else:
        candidate_set = set(candidates)
        selected = [
            node for node in candidates
            if not any(descendant in candidate_set for descendant in node.find_all(True))
        ]
    evidence = []
    for node in selected:
        text_parts = []
        for text_node in node.find_all(string=True):
            if _is_old_price(text_node.parent, node):
                continue
            text_parts.append(str(text_node))
        visible_text = " ".join(" ".join(text_parts).split())
        amount = _product_number(visible_text)
        currency = _supported_currency(visible_text)
        if amount is not None:
            evidence.append((amount, currency, visible_text))
    return evidence


def _fallback_product_from_verified_record(record, visible_name, schema_name, sku, final_url, price, currency,
                                           main_context, soup):
    specifications, specifications_truncated = _specifications(main_context, soup)
    availability_evidence, availability_truncated = _availability_evidence(soup)
    product = {
        "name": visible_name or schema_name,
        "sku": sku,
        "brand": _product_brand(record),
        "price": _json_price(price) if price is not None else None,
        "original_price": None,
        "currency": currency,
        "variant": None,
        "url": final_url,
        "image_url": _record_image(record, final_url),
        "description": _text(record.get("description"), 5000),
        "specifications": specifications,
        "availability_evidence": availability_evidence,
    }
    warnings = []
    if specifications_truncated:
        warnings.append("specifications_truncated")
    if availability_truncated:
        warnings.append("availability_truncated")
    return product, warnings


def _extract_verified_pdp(final_url, html, requested_sku, deadline):
    soup = BeautifulSoup(html, "html.parser")
    title_h1 = soup.find("h1")
    visible_name = _text(title_h1.get_text(" ", strip=True)) if title_h1 else None
    _, main_context = _find_main_context(soup)
    visible_prices = _visible_current_prices(soup, main_context)
    canonical_links = [tag.get("href") for tag in soup.find_all("link", rel=lambda v: v and "canonical" in v)]
    if len({link for link in canonical_links if link}) > 1:
        raise WortenAdapterError("upstream_invalid_response", "The PDP had conflicting canonical URLs.", 502)
    canonical = next((link for link in canonical_links if link), None)
    if canonical and not _page_url_matches(canonical, final_url):
        raise WortenAdapterError("upstream_invalid_response", "The PDP canonical URL did not match the requested product.", 502)
    parser = _ProductPage()
    try:
        parser.feed(html)
    except Exception:
        raise WortenAdapterError("upstream_invalid_response", "The PDP HTML was invalid.", 502) from None
    records = _product_records(parser.jsonld, deadline)
    matching = [record for record in records if requested_sku in _record_skus(record)]
    if not matching:
        return {
            "mode": "product", "source_url": final_url, "extraction_status": "no_product",
            "product": None, "warnings": ["identity_unverified"],
        }
    if len(matching) != 1:
        raise WortenAdapterError("upstream_invalid_response", "The PDP had ambiguous product identity records.", 502)
    record = matching[0]
    if _record_skus(record) != {requested_sku}:
        raise WortenAdapterError("upstream_invalid_response", "The PDP structured identifiers conflicted.", 502)
    structured_url = record.get("url") or record.get("@id")
    if structured_url and not _page_url_matches(structured_url, final_url):
        raise WortenAdapterError("upstream_invalid_response", "The PDP structured URL did not match the requested product.", 502)
    schema_name = _text(record.get("name"))
    if visible_name and schema_name and _normalized_words(visible_name) != _normalized_words(schema_name):
        # Some sites append merchandising text in only one representation. Permit
        # a contained match, but do not combine unrelated names.
        if _normalized_words(visible_name) not in _normalized_words(schema_name) and _normalized_words(schema_name) not in _normalized_words(visible_name):
            raise WortenAdapterError("upstream_invalid_response", "The PDP product names conflicted.", 502)
    selected_price, selected_currency, offers_unambiguous = _offer_facts(record, requested_sku, final_url)
    result = _extract_product_html(final_url, html, content_status="ok")
    generic_product_unavailable = result.get("extraction_status") == "no_product" or not isinstance(result.get("product"), dict)
    warnings = result.get("warnings", []) if isinstance(result, dict) else []
    if generic_product_unavailable:
        fallback_product, fallback_warnings = _fallback_product_from_verified_record(
            record, visible_name, schema_name, requested_sku, final_url, selected_price, selected_currency,
            main_context, soup,
        )
        result = {
            "mode": "product",
            "source_url": final_url,
            "extraction_status": "partial",
            "product": fallback_product,
            "warnings": [*warnings, "generic_identity_fallback", *fallback_warnings],
        }
    product = result["product"]
    warnings = result.get("warnings", [])
    if not canonical and not structured_url:
        warnings = list(dict.fromkeys([*warnings, "canonical_unverified"]))
    if product.get("sku") not in (None, requested_sku):
        raise WortenAdapterError("upstream_invalid_response", "The extracted PDP SKU did not match the requested product.", 502)
    # The shared Product parser recognizes IKEA's eight-digit article pattern;
    # Worten's SKU is established above from the URL + matching Product JSON-LD.
    product["sku"] = requested_sku
    if offers_unambiguous:
        visible_amounts = {item[0] for item in visible_prices}
        visible_currencies = {item[1] for item in visible_prices if item[1] is not None}
        visible_match = any(amount == selected_price and currency == selected_currency for amount, currency, _ in visible_prices)
        if (visible_amounts and visible_amounts != {selected_price}) or (visible_currencies and visible_currencies != {selected_currency}):
            warnings = list(dict.fromkeys([*warnings, "price_conflict"]))
        elif not visible_match:
            warnings = list(dict.fromkeys([*warnings, "visible_price_unverified"]))
        if product.get("price") is not None and Decimal(str(product["price"])) != selected_price:
            warnings = list(dict.fromkeys([*warnings, "price_conflict"]))
        if product.get("currency") and product["currency"] != selected_currency:
            warnings = list(dict.fromkeys([*warnings, "currency_conflict"]))
        product["price"] = int(selected_price) if selected_price == selected_price.to_integral_value() else float(selected_price)
        product["currency"] = selected_currency
    else:
        product["price"] = None
        product["currency"] = None
        warnings = list(dict.fromkeys([*warnings, "price_ambiguous"]))
    if product.get("name") is None:
        product["name"] = visible_name or schema_name
    if product.get("name") is None:
        warnings = list(dict.fromkeys([*warnings, "name_unverified"]))
    if any(code in warnings for code in ("price_conflict", "currency_conflict", "visible_price_unverified", "canonical_unverified")) or not offers_unambiguous:
        result["extraction_status"] = "partial"
    elif generic_product_unavailable and product.get("name") is not None:
        result["extraction_status"] = "ok"
    elif product.get("price") is None or product.get("currency") is None or product.get("name") is None:
        result["extraction_status"] = "partial"
    if time.monotonic() > deadline:
        raise WortenAdapterError("upstream_timeout", "The PDP exceeded its processing time limit.", 504)
    result["source_url"] = final_url
    result["warnings"] = list(dict.fromkeys(warnings))[:16]
    return result


def extract_worten_product(url, resolve_public_addresses):
    parts, _, requested_sku = _parse_worten_url(url, "product")
    final_url = urlunsplit(("https", WORTEN_HOST, parts.path, "", ""))
    deadline = time.monotonic() + OPERATION_BUDGET_SECONDS
    try:
        body, content_type = _curl_url(final_url, None, deadline, resolve_public_addresses)
    except _WortenChallenge:
        return {"mode": "product", "source_url": final_url, "extraction_status": "unusable",
                "product": None, "warnings": ["content_blocked"]}
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise WortenAdapterError("upstream_invalid_response", "The upstream PDP was not HTML.", 502)
    if not body.strip():
        return {"mode": "product", "source_url": final_url, "extraction_status": "unusable",
                "product": None, "warnings": []}
    message = MessageLike(content_type)
    try:
        html = body.decode(message.charset or "utf-8", errors="strict")
    except (LookupError, UnicodeDecodeError):
        html = body.decode("windows-1252", errors="replace")
    result = _extract_verified_pdp(final_url, html, requested_sku, deadline)
    if time.monotonic() > deadline:
        raise WortenAdapterError("upstream_timeout", "The PDP exceeded its processing time limit.", 504)
    return result


class MessageLike:
    """Small Content-Type charset parser without coupling to Requests behavior."""

    def __init__(self, content_type):
        from email.message import Message

        message = Message()
        message["content-type"] = content_type
        self.charset = message.get_content_charset()
