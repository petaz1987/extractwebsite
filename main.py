"""Small, bounded web-page extraction API with SSRF protections."""

import hmac
import ipaddress
import logging
import re
import socket
import unicodedata
from email.message import Message
from urllib.parse import urljoin, urlsplit

import requests
import trafilatura
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

LOG = logging.getLogger(__name__)
MAX_URL_LENGTH = 2048
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_TEXT_LENGTH = 50_000
MAX_RAW_HTML_LENGTH = 100_000
MAX_LINKS = 100
TIMEOUT = (5, 10)
USER_AGENT = "ExtractWebsite/1.0 (+https://github.com/petaz1987/extractwebsite)"
ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
SOFT_BLOCK_MARKERS = (
    "haz clic en el boton de abajo para seguir comprando",
    "click the button below to continue shopping",
    "sorry we just need to make sure you re not a robot",
    "verify you are human",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
)


class ExtractionError(Exception):
    def __init__(self, code, message, status):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _resolve_public_addresses(hostname):
    """Resolve a hostname and reject the whole answer if any address is unsafe."""
    normalized = hostname.rstrip(".").lower()
    if not normalized or normalized == "localhost" or normalized.endswith(".localhost"):
        raise ExtractionError("unsafe_url", "The requested URL is not allowed.", 403)
    if normalized.endswith(".local") or normalized == "local":
        raise ExtractionError("unsafe_url", "The requested URL is not allowed.", 403)

    try:
        answers = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        LOG.info("Target hostname could not be resolved")
        raise ExtractionError("upstream_fetch_failed", "The upstream host could not be resolved.", 502) from exc

    addresses = set()
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer[4][0].split("%", 1)[0])
        except (ValueError, IndexError, TypeError):
            raise ExtractionError("unsafe_url", "The requested URL is not allowed.", 403)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if address.is_multicast or address.is_reserved or not address.is_global:
            raise ExtractionError("unsafe_url", "The requested URL is not allowed.", 403)
        addresses.add(str(address))

    if not addresses:
        raise ExtractionError("upstream_fetch_failed", "The upstream host could not be resolved.", 502)
    return addresses


def validate_url(url):
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise ExtractionError("invalid_url", "Provide one valid URL of at most 2048 characters.", 400)
    try:
        parts = urlsplit(url)
        # Accessing .port validates malformed port syntax and range.
        _ = parts.port
    except ValueError as exc:
        raise ExtractionError("invalid_url", "The requested URL is malformed.", 400) from exc

    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ExtractionError("invalid_url", "Only valid HTTP and HTTPS URLs are supported.", 400)
    if parts.username is not None or parts.password is not None:
        raise ExtractionError("invalid_url", "URLs containing credentials are not allowed.", 400)
    hostname = parts.hostname
    try:
        literal = ipaddress.ip_address(hostname)
        normalized = literal.ipv4_mapped if isinstance(literal, ipaddress.IPv6Address) and literal.ipv4_mapped else literal
        if normalized.is_multicast or normalized.is_reserved or not normalized.is_global:
            raise ExtractionError("unsafe_url", "The requested URL is not allowed.", 403)
    except ValueError:
        _resolve_public_addresses(hostname)
    return url


def _safe_error(code, message, status):
    return jsonify({"error": {"code": code, "message": message}}), status


def _read_bounded_body(response):
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise ExtractionError("response_too_large", "The upstream response exceeds the 5 MB limit.", 413)
        except ValueError:
            pass

    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"", "identity"}:
        raise ExtractionError("unsupported_content_encoding", "The upstream response encoding is not supported.", 502)

    chunks = []
    size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ExtractionError("response_too_large", "The upstream response exceeds the 5 MB limit.", 413)
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_html_body(body, content_type_header, fallback_encoding):
    content_type = Message()
    content_type["content-type"] = content_type_header
    declared_encoding = content_type.get_content_charset()
    if declared_encoding:
        try:
            return body.decode(declared_encoding, errors="replace")
        except LookupError:
            pass

    try:
        return body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        encoding = fallback_encoding or "windows-1252"
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            return body.decode("windows-1252", errors="replace")


def _fetch_page(requested_url):
    current_url = requested_url
    visited = set()
    redirects = 0
    session = requests.Session()
    session.trust_env = False
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity"}
    try:
        while True:
            validate_url(current_url)
            if current_url in visited:
                raise ExtractionError("redirect_loop", "The upstream redirect loop was blocked.", 502)
            visited.add(current_url)

            try:
                response = session.get(current_url, headers=headers, timeout=TIMEOUT, allow_redirects=False, stream=True)
            except requests.Timeout as exc:
                raise ExtractionError("upstream_timeout", "The upstream server did not respond in time.", 504) from exc
            except requests.RequestException as exc:
                LOG.info("Upstream request failed (%s)", type(exc).__name__)
                raise ExtractionError("upstream_fetch_failed", "The upstream page could not be fetched.", 502) from exc

            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise ExtractionError("upstream_fetch_failed", "The upstream redirect was invalid.", 502)
                if redirects >= MAX_REDIRECTS:
                    raise ExtractionError("too_many_redirects", "The upstream page redirected too many times.", 502)
                try:
                    next_url = urljoin(current_url, location)
                except (TypeError, ValueError) as exc:
                    raise ExtractionError("invalid_redirect", "The upstream redirect was invalid.", 502) from exc
                validate_url(next_url)
                if next_url in visited:
                    raise ExtractionError("redirect_loop", "The upstream redirect loop was blocked.", 502)
                current_url = next_url
                redirects += 1
                continue

            if response.status_code < 200 or response.status_code >= 300:
                response.close()
                raise ExtractionError("upstream_fetch_failed", "The upstream server returned an unsuccessful response.", 502)

            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in ALLOWED_CONTENT_TYPES:
                response.close()
                raise ExtractionError("unsupported_content_type", "The upstream response is not an HTML page.", 415)
            try:
                body = _read_bounded_body(response)
                html = _decode_html_body(body, response.headers.get("Content-Type", ""), response.encoding)
            finally:
                response.close()
            return current_url, content_type, response.status_code, html
    finally:
        session.close()


def _clean_text(value):
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


def _normalize_for_matching(value):
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def _content_assessment(title, main_text):
    if not isinstance(main_text, str):
        return {"content_status": "empty", "usable": False, "block_reason": None}
    normalized_main_text = " ".join(main_text.split())
    if not normalized_main_text:
        return {"content_status": "empty", "usable": False, "block_reason": None}

    normalized_title = " ".join(title.split()) if isinstance(title, str) else ""
    marker_text = _normalize_for_matching(
        " ".join((normalized_title, normalized_main_text[:3000]))
    )
    if any(marker in marker_text for marker in SOFT_BLOCK_MARKERS):
        return {"content_status": "blocked", "usable": False, "block_reason": "anti_bot_challenge"}
    return {"content_status": "ok", "usable": True, "block_reason": None}


def _fallback_text(soup):
    for node in soup.find_all(["script", "style", "noscript", "nav", "footer", "header", "form", "template"]):
        node.decompose()
    for node in soup.select("[role=navigation], [role=contentinfo], [aria-hidden=true]"):
        node.decompose()
    return _clean_text(soup.get_text(separator="\n", strip=True))


def _extract_links(soup, final_url):
    links = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        raw_href = anchor.get("href", "").strip()
        if not raw_href or raw_href.startswith("#"):
            continue
        try:
            href = urljoin(final_url, raw_href)
            parts = urlsplit(href)
            if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
                continue
        except (ValueError, TypeError):
            continue
        if href in seen:
            continue
        seen.add(href)
        text = _clean_text(anchor.get_text(" ", strip=True))[:500]
        links.append({"text": text, "href": href})
        if len(links) >= MAX_LINKS:
            break
    return links


def extract_web_content(url):
    final_url, content_type, status_code, html = _fetch_page(url)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": lambda value: value and value.lower() == "description"})
    meta_description = meta.get("content", "").strip() if meta else ""

    try:
        main_text = trafilatura.extract(html, include_comments=False, include_tables=True, favor_precision=True) or ""
    except Exception as exc:  # Trafilatura failures should not prevent useful fallback extraction.
        LOG.info("Main-content extraction failed (%s)", type(exc).__name__)
        main_text = ""
    main_text = _clean_text(main_text) or _fallback_text(soup)
    content_assessment = _content_assessment(title, main_text)

    return {
        "url": final_url,
        "requested_url": url,
        "title": title[:1000],
        "meta_description": meta_description[:5000],
        "main_text": main_text[:MAX_TEXT_LENGTH],
        "links": _extract_links(soup, final_url),
        "raw_html": html[:MAX_RAW_HTML_LENGTH],
        "content_type": content_type,
        "status_code": status_code,
        **content_assessment,
    }


def project_response(result, mode):
    """Return either the backwards-compatible full response or its agent projection."""
    if mode != "agent":
        return result
    fields = (
        "url",
        "requested_url",
        "title",
        "meta_description",
        "main_text",
        "content_type",
        "status_code",
        "content_status",
        "usable",
        "block_reason",
    )
    return {field: result[field] for field in fields}


def create_app(token=None):
    configured_token = token if token is not None else __import__("os").environ.get("EXTRACT_API_TOKEN")
    if not configured_token:
        raise RuntimeError("EXTRACT_API_TOKEN must be configured before starting the service")

    app = Flask(__name__)
    app.config["EXTRACT_API_TOKEN"] = configured_token

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "extractwebsite"})

    @app.get("/extract")
    def extract():
        auth = request.headers.get("Authorization", "")
        scheme, _, supplied = auth.partition(" ")
        if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, app.config["EXTRACT_API_TOKEN"]):
            return _safe_error("unauthorized", "Valid bearer authentication is required.", 401)

        modes = request.args.getlist("mode")
        if len(modes) > 1:
            return _safe_error("invalid_mode", "Unsupported extraction mode.", 400)
        mode = modes[0] if modes else ""
        if mode and mode != "agent":
            return _safe_error("invalid_mode", "Unsupported extraction mode.", 400)

        urls = request.args.getlist("url")
        if len(urls) != 1 or not urls[0].strip():
            return _safe_error("invalid_url", "Provide exactly one non-empty url query parameter.", 400)
        try:
            result = extract_web_content(urls[0])
        except ExtractionError as exc:
            return _safe_error(exc.code, exc.message, exc.status)
        except Exception as exc:
            # Avoid formatting arbitrary upstream exception strings, which may contain URLs.
            LOG.error("Unexpected extraction failure (%s)", type(exc).__name__)
            return _safe_error("upstream_fetch_failed", "The upstream page could not be fetched.", 502)
        return jsonify(project_response(result, mode))

    return app


app = create_app()


if __name__ == "__main__":
    import os

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
