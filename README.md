# ExtractWebsite

A lightweight Flask service that fetches public web pages and returns structured, cleaned content for the Morpheus `fetch_webpage` tool. It does not run a browser or JavaScript.

## Endpoints

### `GET /health`

Unauthenticated liveness response. It makes no external network request.

```json
{"status":"ok","service":"extractwebsite"}
```

### `GET /extract?url=<url>`

Requires `Authorization: Bearer <EXTRACT_API_TOKEN>`. The service returns a JSON object containing:

- `url`: final URL after redirects
- `requested_url`: original URL
- `title`, `meta_description`
- `main_text`: preferred cleaned page content, capped at 50,000 characters
- `links`: up to 100 deduplicated HTTP(S) links with trimmed text and absolute URLs
- `raw_html`: compatibility field, capped at 100,000 characters; `main_text` is preferred
- `content_type`, `status_code`

Add the optional `mode=agent` query parameter for LLM/agent consumers. This compact response includes only `url`, `requested_url`, `title`, `meta_description`, `main_text`, `content_type`, and `status_code`, avoiding raw HTML and the link inventory in model context. Omitting `mode` preserves the full response; unsupported or duplicate mode parameters return HTTP 400.

Example:

```sh
curl --get 'https://YOUR-RENDER-HOST/extract' \
  --data-urlencode 'url=https://example.com/article' \
  --data-urlencode 'mode=agent' \
  -H 'Authorization: Bearer YOUR_CONFIGURED_TOKEN'
```

Do not put a real token in source control, shell history, or this README. Configure `EXTRACT_API_TOKEN` in the process environment or Render environment settings. The application refuses to start if it is missing. `.env.example` is a placeholder only.

## Errors

Errors use a stable JSON shape and never include exception details:

```json
{"error":{"code":"unsafe_url","message":"The requested URL is not allowed."}}
```

Typical statuses: 400 invalid URL/input, 401 missing or invalid bearer token, 403 unsafe target, 413 body exceeds limit, 415 unsupported content type, 502 upstream or redirect failure, and 504 upstream timeout.

## Security and resource limits

- Only HTTP and HTTPS URLs are accepted. Credentials in URLs, localhost names, `.local` names, and any resolved address that is not globally routable are rejected. IPv4 and IPv6 are checked, including IPv4-mapped IPv6.
- DNS is resolved and checked before each request and redirect. Redirects are followed manually, with a maximum of five; every destination is revalidated. `requests` performs its own DNS lookup when connecting, so DNS rebinding between validation and connection remains a residual TOCTOU risk. This lightweight approach does not claim to eliminate that risk.
- The service uses a fresh Requests session per extraction, disables environment proxy settings, forwards no caller headers/cookies, and has fixed 5-second connect and 10-second read timeouts.
- Only `text/html` and `application/xhtml+xml` are parsed. Compressed response bodies are not accepted. Streamed response bodies are capped at 5 MiB.
- Authentication uses constant-time token comparison. Tokens are not logged or returned.

## Extraction

Trafilatura attempts high-quality main-content extraction. If it cannot identify content or raises an error, BeautifulSoup provides a cleaned fallback with scripts, styles, navigation, headers, footers, forms, templates, and hidden elements removed. Raw HTML remains available for compatibility within its limit.

## Local development

Python 3.10 or newer is recommended.

```sh
python -m venv .venv
# Activate the environment, then:
pip install -r requirements-dev.txt
$env:EXTRACT_API_TOKEN = 'local-development-token'  # PowerShell
python main.py
```

The service binds to `0.0.0.0` and uses `PORT` when set, otherwise port 5000.

Run the offline test suite:

```sh
python -m pytest
```

Tests mock DNS and HTTP; they do not call public sites or Render.

## Render

Keep using the existing Render service and project. Set `EXTRACT_API_TOKEN` in that service's environment. The existing Python start command can continue to run `python main.py`; the app honors Render's `PORT`. No second service or Docker configuration is required.
