import os

os.environ.setdefault("EXTRACT_API_TOKEN", "test-secret")

import pytest
import requests

import main


@pytest.fixture
def client():
    return main.create_app("test-secret").test_client()


def public_dns(monkeypatch, ip="93.184.216.34"):
    monkeypatch.setattr(main.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, (ip, 0))])


def test_authentication_and_health(client, monkeypatch):
    public_dns(monkeypatch)
    monkeypatch.setattr(main, "extract_web_content", lambda url: {"url": url})
    assert client.get("/health").status_code == 200
    assert client.get("/extract?url=https://example.com").status_code == 401
    assert client.get("/extract?url=https://example.com&mode=agent").status_code == 401
    assert client.get("/extract?url=https://example.com", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/extract?url=https://example.com&mode=agent", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/extract?url=https://example.com", headers={"Authorization": "Bearer test-secret"})
    assert response.status_code == 200


@pytest.mark.parametrize("url", ["https://example.com", "http://example.com/path"])
def test_valid_public_schemes(url, monkeypatch):
    public_dns(monkeypatch)
    assert main.validate_url(url) == url


@pytest.mark.parametrize("url", [
    "ftp://example.com", "file:///etc/passwd", "data:text/html,hi", "javascript:alert(1)",
    "gopher://example.com", "http:///missing-host", "not a url", "http://example.com:99999",
    "http://user:pass@example.com", "https://" + "a" * 2050,
])
def test_invalid_urls(url):
    with pytest.raises(main.ExtractionError) as exc:
        main.validate_url(url)
    assert exc.value.status == 400


@pytest.mark.parametrize("url", [
    "http://localhost", "http://localhost.", "http://a.localhost", "http://service.local",
    "http://127.0.0.1", "http://10.0.0.1", "http://172.16.0.1", "http://192.168.1.1",
    "http://169.254.1.1", "http://169.254.169.254", "http://0.0.0.0", "http://224.0.0.1",
    "http://[::1]", "http://[fc00::1]", "http://[fe80::1]", "http://[::ffff:127.0.0.1]",
    "http://[2001:db8::1]",
])
def test_blocked_ip_and_hostname_targets(url):
    with pytest.raises(main.ExtractionError) as exc:
        main.validate_url(url)
    assert exc.value.code == "unsafe_url"
    assert exc.value.status == 403


def test_public_looking_hostname_resolving_private_is_blocked(monkeypatch):
    public_dns(monkeypatch, "10.20.30.40")
    with pytest.raises(main.ExtractionError) as exc:
        main.validate_url("https://looks-public.example/")
    assert exc.value.code == "unsafe_url"


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self._chunks = chunks or [b"<html><title>Test</title><body>Text</body></html>"]
        self.closed = False
        self.is_redirect = status in {301, 302, 303, 307, 308}
        self.is_permanent_redirect = status in {301, 308}

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping
        self.trust_env = True
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


def install_session(monkeypatch, mapping):
    session = FakeSession(mapping)
    monkeypatch.setattr(main.requests, "Session", lambda: session)
    public_dns(monkeypatch)
    return session


def test_public_redirect_accepted_and_revalidated(monkeypatch):
    first = FakeResponse(302, {"Location": "https://other.example/page"})
    final = FakeResponse()
    session = install_session(monkeypatch, {"https://example.com/": first, "https://other.example/page": final})
    final_url, _, _, _ = main._fetch_page("https://example.com/")
    assert final_url == "https://other.example/page"
    assert len(session.calls) == 2
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert session.trust_env is False


@pytest.mark.parametrize("location", ["http://localhost/", "http://10.0.0.1/"])
def test_redirect_to_private_target_blocked(monkeypatch, location):
    install_session(monkeypatch, {"https://example.com/": FakeResponse(302, {"Location": location})})
    with pytest.raises(main.ExtractionError) as exc:
        main._fetch_page("https://example.com/")
    assert exc.value.code == "unsafe_url"


def test_redirect_loop_and_limit(monkeypatch):
    a = "https://a.example/"
    b = "https://b.example/"
    install_session(monkeypatch, {a: FakeResponse(302, {"Location": b}), b: FakeResponse(302, {"Location": a})})
    with pytest.raises(main.ExtractionError, match="redirect loop"):
        main._fetch_page(a)

    mapping = {f"https://host{i}.example/": FakeResponse(302, {"Location": f"https://host{i + 1}.example/"}) for i in range(6)}
    install_session(monkeypatch, mapping)
    with pytest.raises(main.ExtractionError) as exc:
        main._fetch_page("https://host0.example/")
    assert exc.value.code == "too_many_redirects"


@pytest.mark.parametrize("error,code,status", [
    (requests.Timeout(), "upstream_timeout", 504),
    (requests.ConnectionError(), "upstream_fetch_failed", 502),
])
def test_timeout_and_upstream_error(monkeypatch, error, code, status):
    install_session(monkeypatch, {"https://example.com/": error})
    with pytest.raises(main.ExtractionError) as exc:
        main._fetch_page("https://example.com/")
    assert (exc.value.code, exc.value.status) == (code, status)


def test_oversized_body_and_content_type(monkeypatch):
    huge = FakeResponse(headers={"Content-Type": "text/html", "Content-Length": str(main.MAX_RESPONSE_BYTES + 1)})
    install_session(monkeypatch, {"https://example.com/": huge})
    with pytest.raises(main.ExtractionError) as exc:
        main._fetch_page("https://example.com/")
    assert (exc.value.code, exc.value.status) == ("response_too_large", 413)

    binary = FakeResponse(headers={"Content-Type": "application/pdf"})
    install_session(monkeypatch, {"https://example.com/": binary})
    with pytest.raises(main.ExtractionError) as exc:
        main._fetch_page("https://example.com/")
    assert (exc.value.code, exc.value.status) == ("unsupported_content_type", 415)


def test_streaming_body_limit(monkeypatch):
    chunks = [b"x" * (main.MAX_RESPONSE_BYTES // 2), b"x" * (main.MAX_RESPONSE_BYTES // 2 + 1)]
    install_session(monkeypatch, {"https://example.com/": FakeResponse(chunks=chunks)})
    with pytest.raises(main.ExtractionError) as exc:
        main._fetch_page("https://example.com/")
    assert exc.value.status == 413


def test_content_extraction_fallback_and_links(monkeypatch):
    html = """<html><head><title> Page title </title><meta name="description" content="A description"></head>
      <body><nav>Menu noise</nav><main><h1>Article</h1><p>Useful page text.</p></main><footer>Footer noise</footer>
      <a href="/story">Story</a><a href="/story">Duplicate</a><a href="http://[invalid">Malformed</a>
      <a href="javascript:alert(1)">Bad</a>
      <a href="mailto:x@example.com">Email</a><a href="#part">Fragment</a></body></html>"""
    monkeypatch.setattr(main, "_fetch_page", lambda url: ("https://example.com/final", "text/html", 200, html))
    monkeypatch.setattr(main.trafilatura, "extract", lambda *args, **kwargs: None)
    result = main.extract_web_content("https://example.com/start")
    assert result["title"] == "Page title"
    assert result["meta_description"] == "A description"
    assert "Useful page text." in result["main_text"]
    assert "Menu noise" not in result["main_text"] and "Footer noise" not in result["main_text"]
    assert result["requested_url"] == "https://example.com/start"
    assert result["url"] == "https://example.com/final"
    assert result["links"] == [{"text": "Story", "href": "https://example.com/story"}]


def test_trafilatura_content_and_link_limit(monkeypatch):
    html = "<html><body>Fallback" + "".join(f'<a href="/{i}">L{i}</a>' for i in range(150)) + "</body></html>"
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, html))
    monkeypatch.setattr(main.trafilatura, "extract", lambda *args, **kwargs: "High quality article text")
    result = main.extract_web_content("https://example.com/")
    assert result["main_text"] == "High quality article text"
    assert len(result["links"]) == main.MAX_LINKS


def test_exactly_one_url_parameter_required(client):
    headers = {"Authorization": "Bearer test-secret"}
    assert client.get("/extract", headers=headers).status_code == 400
    assert client.get("/extract?url=https://a.example&url=https://b.example", headers=headers).status_code == 400


def test_response_modes_preserve_full_payload_and_project_agent_fields(client, monkeypatch):
    full_result = {
        "url": "https://example.com/final",
        "requested_url": "https://example.com/start",
        "title": "Example",
        "meta_description": "Description",
        "main_text": "Article text",
        "links": [{"text": "Read", "href": "https://example.com/read"}],
        "raw_html": "<html>page</html>",
        "content_type": "text/html",
        "status_code": 200,
    }
    monkeypatch.setattr(main, "extract_web_content", lambda url: full_result)
    headers = {"Authorization": "Bearer test-secret"}

    full_response = client.get("/extract?url=https://example.com", headers=headers)
    assert full_response.status_code == 200
    assert full_response.get_json() == full_result
    assert {"main_text", "links", "raw_html", "title", "meta_description"}.issubset(full_response.get_json())

    agent_response = client.get("/extract?url=https://example.com&mode=agent", headers=headers)
    assert agent_response.status_code == 200
    assert agent_response.get_json() == {
        "url": "https://example.com/final",
        "requested_url": "https://example.com/start",
        "title": "Example",
        "meta_description": "Description",
        "main_text": "Article text",
        "content_type": "text/html",
        "status_code": 200,
    }
    assert "raw_html" not in agent_response.get_json()
    assert "links" not in agent_response.get_json()


def test_unsupported_or_duplicate_modes_are_rejected(client, monkeypatch):
    monkeypatch.setattr(main, "extract_web_content", lambda url: {"url": url})
    headers = {"Authorization": "Bearer test-secret"}

    unsupported = client.get("/extract?url=https://example.com&mode=compact", headers=headers)
    assert unsupported.status_code == 400
    assert unsupported.get_json() == {"error": {"code": "invalid_mode", "message": "Unsupported extraction mode."}}

    duplicate = client.get("/extract?url=https://example.com&mode=agent&mode=agent", headers=headers)
    assert duplicate.status_code == 400
    assert duplicate.get_json() == {"error": {"code": "invalid_mode", "message": "Unsupported extraction mode."}}


def test_startup_requires_token(monkeypatch):
    monkeypatch.delenv("EXTRACT_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="EXTRACT_API_TOKEN"):
        main.create_app()
