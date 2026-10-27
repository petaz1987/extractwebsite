import json
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
    assert client.get("/extract?url=https://example.com&mode=agent&include_links=1").status_code == 401
    assert client.get("/extract?url=https://example.com&mode=search").status_code == 401
    assert client.get("/extract?url=https://example.com&mode=product").status_code == 401
    assert client.get("/extract?url=https://example.com", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/extract?url=https://example.com&mode=agent", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/extract?url=https://example.com&mode=product", headers={"Authorization": "Bearer wrong"}).status_code == 401
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


def test_agent_path_skips_trafilatura_and_link_inventory(monkeypatch):
    html = "<html><head><title>Sample</title></head><body><main><p>Useful content.</p></main></body></html>"
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, html))
    monkeypatch.setattr(main.trafilatura, "extract", lambda *args, **kwargs: pytest.fail("Trafilatura ran in agent mode"))
    monkeypatch.setattr(main, "_extract_links", lambda *args, **kwargs: pytest.fail("link inventory built in agent mode"))

    result = main.extract_agent_content("https://example.com/page")

    assert result["main_text"]
    assert "raw_html" not in result
    assert "links" not in result
    assert set(result) == {
        "url", "requested_url", "title", "meta_description", "main_text", "content_type", "status_code",
        "content_status", "usable", "block_reason",
    }


def test_agent_mode_optionally_includes_links_from_one_fetch(client, monkeypatch):
    requested_url = "https://example.com/events/start"
    final_url = "https://example.com/events/2026/"
    html = """<html><head><title>Events</title></head><body>
      <main><h1>Cultural Events</h1><p>November programme and event details.</p></main>
      <a href="../../files/programme.xlsx">Download <b>November programme</b></a>
      <a href="https://example.com/files/programme.xlsx">Duplicate link</a>
      <a href="javascript:alert(1)">JavaScript</a>
      <a href="mailto:events@example.com">Email</a>
      <a href="#details">Fragment</a>
      <a href="http://[invalid">Malformed</a>
    </body></html>"""
    fetches = []

    def fake_fetch(url):
        fetches.append(url)
        return final_url, "text/html", 200, html

    monkeypatch.setattr(main, "_fetch_page", fake_fetch)
    response = client.get(
        f"/extract?url={requested_url}&mode=agent&include_links=1",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert response.status_code == 200
    result = response.get_json()
    assert set(result) == {
        "url", "requested_url", "title", "meta_description", "main_text", "content_type", "status_code",
        "content_status", "usable", "block_reason", "links",
    }
    assert result["url"] == final_url
    assert result["requested_url"] == requested_url
    assert result["links"] == [{
        "text": "Download November programme",
        "href": "https://example.com/files/programme.xlsx",
    }]
    assert result["main_text"] == main._reduce_agent_page(main.BeautifulSoup(html, "html.parser"))
    assert result["content_status"] == "ok"
    assert result["usable"] is True
    assert result["block_reason"] is None
    assert fetches == [requested_url]


def test_agent_include_links_returns_empty_array_and_respects_max_links(monkeypatch):
    empty_html = "<html><body><p>Useful page content.</p></body></html>"
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, empty_html))
    empty = main.extract_agent_content("https://example.com/empty", include_links=True)
    assert empty["links"] == []

    links_html = "<html><body><p>Useful page content.</p>" + "".join(
        f'<a href="/document/{index}">Document {index}</a>' for index in range(main.MAX_LINKS + 25)
    ) + "</body></html>"
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, links_html))
    result = main.extract_agent_content("https://example.com/", include_links=True)
    assert len(result["links"]) == main.MAX_LINKS


@pytest.mark.parametrize("query", [
    "mode=agent&include_links=0",
    "mode=agent&include_links=true",
    "mode=agent&include_links=1&include_links=1",
    "mode=agent&include_links=1&include_links=0",
    "include_links=1",
])
def test_invalid_or_duplicate_include_links_parameters_are_rejected(client, query):
    response = client.get(
        f"/extract?url=https://example.com&{query}",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert response.status_code == 400
    assert response.get_json() == {
        "error": {"code": "invalid_include_links", "message": "Unsupported include_links parameter."}
    }


def test_search_mode_rejects_include_links(client):
    response = client.get(
        "/extract?url=https://example.com/search&mode=search&include_links=1",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {"code": "invalid_include_links", "message": "Unsupported include_links parameter."}
    }


def test_product_mode_uses_one_existing_fetch_and_rejects_include_links(client, monkeypatch):
    requested_url = "https://shop.example/pd/item-12345678"
    final_url = "https://shop.example/final/pd/item-12345678"
    html = """<html><head><title>Item</title><meta property='product:price:amount' content='12'>
      <meta property='product:price:currency' content='EUR'></head><body><main><h1>Item</h1>
      <p>Article number 12345678</p></main></body></html>"""
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return final_url, "text/html", 200, html

    monkeypatch.setattr(main, "_fetch_page", fake_fetch)
    response = client.get(
        f"/extract?url={requested_url}&mode=product",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert response.status_code == 200
    result = response.get_json()
    assert result["mode"] == "product"
    assert result["source_url"] == final_url
    assert result["extraction_status"] == "ok"
    assert result["product"]["sku"] == "12345678"
    assert result["product"]["url"] == final_url
    assert "raw_html" not in result
    assert "links" not in result
    assert calls == [requested_url]

    rejected = client.get(
        f"/extract?url={requested_url}&mode=product&include_links=1",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert rejected.status_code == 400
    assert rejected.get_json()["error"]["code"] == "invalid_include_links"


def test_search_mode_uses_existing_fetch_once_and_returns_compact_product_records(client, monkeypatch):
    requested_url = "https://shop.example/search?q=items"
    final_url = "https://shop.example/ten/search?q=items"
    html = '''<html><head><title>Search</title></head><body><main>
      <div class="itemBlock" id="art_101_x">
        <a data-item="101" href="/ten/pd/item-one"><h3>ITEM ONE</h3></a>
        <div data-id="itemFacts-101">White, 60x30 cm</div>
        <div class="itemPriceBox" data-id="101"><div class="itemNormalPrice"><span data-price="24,90">24,90 €</span></div></div>
      </div>
    </main></body></html>'''
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return final_url, "text/html", 200, html

    monkeypatch.setattr(main, "_fetch_page", fake_fetch)
    response = client.get(
        f"/extract?url={requested_url}&mode=search",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "mode": "search",
        "source_url": final_url,
        "extraction_status": "ok",
        "product_count": 1,
        "products": [{
            "name": "ITEM ONE",
            "sku": "101",
            "price": 24.9,
            "currency": "EUR",
            "variant": "White, 60x30 cm",
            "url": "https://shop.example/ten/pd/item-one",
            "image_url": None,
        }],
        "truncated": False,
    }
    assert "raw_html" not in response.get_json()
    assert "links" not in response.get_json()
    assert calls == [requested_url]


def test_search_mode_accepts_complete_embedded_records_without_visible_text(monkeypatch):
    html = '''<html><head><title>Search results</title></head><body>
      <script>var item = {"partnumber":"101","name":"ITEM","current_price":24.9,"currency":"EUR","url":"/pd/101","default_image":"/101.jpg"};</script>
    </body></html>'''
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, html))

    result = main.extract_search_content("https://shop.example/search")

    assert result["extraction_status"] == "ok"
    assert result["product_count"] == 1
    assert result["products"][0]["sku"] == "101"


def test_search_mode_keeps_title_only_challenge_unusable_with_embedded_record(monkeypatch):
    html = '''<html><head><title>Verify you are human</title></head><body>
      <script>var item = {"partnumber":"101","name":"ITEM","url":"/pd/101"};</script>
    </body></html>'''
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, html))

    result = main.extract_search_content("https://shop.example/challenge")

    assert result["extraction_status"] == "unusable"
    assert result["products"] == []


def test_search_mode_distinguishes_empty_content_from_usable_no_product_page(monkeypatch):
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, "<html><body></body></html>"))
    empty = main.extract_search_content("https://shop.example/empty")
    assert empty["extraction_status"] == "unusable"

    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, "<html><body>Search shell</body></html>"))
    shell = main.extract_search_content("https://shop.example/shell")
    assert shell["extraction_status"] == "no_products"


def test_agent_reducer_preserves_product_context_forms_tables_and_semantic_values():
    html = """<html><body><main>
      <h1 id="product-title">Generic Tablet</h1>
      <span class="price"><span class="amount">199,99 €</span></span>
      <form><label for="qty">Quantity</label><input id="qty" name="quantity" value="2"><button>Add to cart</button></form>
      <table><tr><th>Memory</th><td>8 GB</td></tr></table>
      <meta property="og:price:amount" content="199.99">
      <div itemprop="price" content="199.99"></div>
    </main></body></html>"""
    result = main._reduce_agent_page(main.BeautifulSoup(html, "html.parser"))

    assert "[h1#product-title] Generic Tablet" in result
    assert "[span.amount] 199,99 €" in result
    assert "[label] Quantity" in result
    assert "[input#qty name=quantity] 2" in result
    assert "[button] Add to cart" in result
    assert "[th] Memory" in result and "[td] 8 GB" in result
    assert "[meta property=og:price:amount] 199.99" in result
    assert "[div itemprop=price] 199.99" in result


def test_agent_reducer_keeps_jsonld_compact_and_ignores_malformed_jsonld():
    html = """<html><head>
      <script type="application/ld+json">{ "@type": "Product", "name": "Widget" }</script>
      <script type="application/ld+json">{ malformed }</script>
    </head><body><p>Visible text</p></body></html>"""
    result = main._reduce_agent_page(main.BeautifulSoup(html, "html.parser"))

    assert '[script type=application/ld+json] {"@type":"Product","name":"Widget"}' in result
    assert "Visible text" in result
    assert "malformed" not in result


def test_jsonld_is_preserved_across_bounded_continuation_lines():
    json_data = {"name": "x" * 4000}
    compact_json = json.dumps(json_data, ensure_ascii=False, separators=(",", ":"))
    html = f'<script type="application/ld+json">{json.dumps(json_data)}</script>'

    result = main._reduce_agent_page(main.BeautifulSoup(html, "html.parser"))
    lines = result.splitlines()
    first_prefix = "[script type=application/ld+json] "
    continuation_prefix = "[script type=application/ld+json continued] "
    json_parts = []
    for line in lines:
        assert len(line) <= main.MAX_AGENT_LINE_LENGTH
        if line.startswith(first_prefix):
            json_parts.append(line[len(first_prefix):])
        elif line.startswith(continuation_prefix):
            json_parts.append(line[len(continuation_prefix):])

    assert len(json_parts) > 1
    assert "".join(json_parts) == compact_json


def test_agent_reducer_removes_noise_and_hidden_content():
    html = """<html><body>
      <script>application state noise</script><style>.x { color: red }</style><noscript>noscript noise</noscript>
      <svg><text>svg noise</text></svg><template>template noise</template><nav>navigation noise</nav>
      <footer>footer noise</footer><div hidden>hidden noise</div><div aria-hidden="true">aria hidden noise</div>
      <main><p>Visible semantic content</p></main>
    </body></html>"""
    result = main._reduce_agent_page(main.BeautifulSoup(html, "html.parser"))

    assert "Visible semantic content" in result
    for noise in ("application state", "color: red", "noscript noise", "svg noise", "template noise",
                  "navigation noise", "footer noise", "hidden noise", "aria hidden noise"):
        assert noise not in result


def test_agent_reducer_output_is_bounded_and_truncation_is_deterministic():
    paragraphs = "".join(f"<p>content-{index} {'x' * 800}</p>" for index in range(60))
    html = f"<html><body><main>{paragraphs}</main></body></html>"
    first = main._reduce_agent_page(main.BeautifulSoup(html, "html.parser"))
    second = main._reduce_agent_page(main.BeautifulSoup(html, "html.parser"))

    assert first == second
    assert len(first) <= main.MAX_AGENT_CONTENT_LENGTH
    assert main.AGENT_TRUNCATION_MARKER in first
    assert all(len(line) <= main.MAX_AGENT_LINE_LENGTH for line in first.splitlines())

    large_jsonld = "{" + '"value":"' + ("x" * (main.MAX_AGENT_JSONLD_INPUT_CHARS + 1)) + '"}'
    large_jsonld_page = f'<script type="application/ld+json">{large_jsonld}</script><p>Useful text</p>'
    reduced = main._reduce_agent_page(main.BeautifulSoup(large_jsonld_page, "html.parser"))
    assert len(reduced) <= main.MAX_AGENT_CONTENT_LENGTH
    assert main.AGENT_TRUNCATION_MARKER in reduced
    assert "Useful text" in reduced


def test_agent_challenge_and_empty_reduced_content_assessment(monkeypatch):
    challenge_html = "<html><body><p>Haz clic en el botón de abajo para seguir comprando</p></body></html>"
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, challenge_html))
    result = main.extract_agent_content("https://example.com/challenge")
    assert result["status_code"] == 200
    assert result["content_status"] == "blocked"
    assert result["usable"] is False
    assert result["block_reason"] == "anti_bot_challenge"

    empty_html = "<html><body><nav>menu</nav><footer>footer</footer></body></html>"
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, empty_html))
    empty = main.extract_agent_content("https://example.com/empty")
    assert empty["content_status"] == "empty"
    assert empty["usable"] is False
    assert empty["block_reason"] is None

    normal_html = "<html><body><article>CAPTCHA and access denied are discussed as security terms.</article></body></html>"
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, normal_html))
    normal = main.extract_agent_content("https://example.com/article")
    assert normal["content_status"] == "ok"
    assert normal["usable"] is True
    assert normal["block_reason"] is None


def test_no_charset_utf8_body_decodes_correctly_and_detects_challenge(monkeypatch):
    url = "https://www.amazon.es/example"
    html = "<html><body>Haz clic en el botón de abajo para seguir comprando</body></html>"
    response = FakeResponse(headers={"Content-Type": "text/html"}, chunks=[html.encode("utf-8")])
    # Requests may default an undeclared HTML charset to ISO-8859-1.
    response.encoding = "ISO-8859-1"
    install_session(monkeypatch, {url: response})
    monkeypatch.setattr(main.trafilatura, "extract", lambda *args, **kwargs: None)

    result = main.extract_web_content(url)

    assert "botón" in result["main_text"]
    assert "botÃ³n" not in result["main_text"]
    assert result["status_code"] == 200
    assert result["content_status"] == "blocked"
    assert result["usable"] is False
    assert result["block_reason"] == "anti_bot_challenge"


def test_explicit_non_utf8_charset_is_respected(monkeypatch):
    url = "https://example.com/legacy"
    html = "<html><body>café</body></html>"
    response = FakeResponse(
        headers={"Content-Type": "text/html; charset=iso-8859-1"},
        chunks=[html.encode("iso-8859-1")],
    )
    response.encoding = "utf-8"
    install_session(monkeypatch, {url: response})

    _, _, status_code, decoded_html = main._fetch_page(url)

    assert status_code == 200
    assert "café" in decoded_html


def test_invalid_utf8_without_charset_uses_response_encoding_fallback(monkeypatch):
    url = "https://example.com/legacy"
    html = "<html><body>café</body></html>"
    response = FakeResponse(headers={"Content-Type": "text/html"}, chunks=[html.encode("iso-8859-1")])
    response.encoding = "ISO-8859-1"
    install_session(monkeypatch, {url: response})
    monkeypatch.setattr(main.trafilatura, "extract", lambda *args, **kwargs: None)

    result = main.extract_web_content(url)

    assert "café" in result["main_text"]
    assert result["status_code"] == 200


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
    assert result["content_status"] == "ok"
    assert result["usable"] is True
    assert result["block_reason"] is None


def test_amazon_spanish_challenge_is_blocked_without_changing_http_status(monkeypatch):
    html = "<html><head><title>Continuar</title></head><body>Haz clic en el botón de abajo para seguir comprando</body></html>"
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, html))
    monkeypatch.setattr(main.trafilatura, "extract", lambda *args, **kwargs: None)

    result = main.extract_web_content("https://www.amazon.es/example")

    assert result["status_code"] == 200
    assert result["content_status"] == "blocked"
    assert result["usable"] is False
    assert result["block_reason"] == "anti_bot_challenge"


@pytest.mark.parametrize("challenge", [
    "Click the button below to continue shopping.",
    "Sorry, we just need to make sure you're not a robot.",
    "Please verify you are human.",
    "Checking your browser before accessing this site.",
    "Enable JavaScript and cookies to continue.",
])
def test_english_high_confidence_challenges_are_blocked(challenge):
    result = main._content_assessment("", challenge)
    assert result == {
        "content_status": "blocked",
        "usable": False,
        "block_reason": "anti_bot_challenge",
    }


@pytest.mark.parametrize("main_text", [None, "", "  \n\t  "])
def test_missing_or_whitespace_only_content_is_empty(main_text):
    result = main._content_assessment("A title", main_text)
    assert result == {"content_status": "empty", "usable": False, "block_reason": None}


def test_generic_security_terms_in_normal_article_do_not_cause_block():
    article = (
        "The CAPTCHA on our test environment failed during the morning run. "
        "The log reported access denied for one user, and the security verification "
        "was later confirmed as a configuration issue. The article also discusses "
        "robot checks and anti-bot protection as common web security techniques."
    )
    result = main._content_assessment("Troubleshooting access denied errors", article)
    assert result == {"content_status": "ok", "usable": True, "block_reason": None}


def test_challenge_phrase_after_first_3000_main_text_characters_does_not_block():
    article = ("Ordinary article content. " * 150) + "verify you are human"
    assert "verify you are human" not in article[:3000]

    result = main._content_assessment("A normal article", article)

    assert result == {"content_status": "ok", "usable": True, "block_reason": None}


def test_trafilatura_content_and_link_limit(monkeypatch):
    html = "<html><body>Fallback" + "".join(f'<a href="/{i}">L{i}</a>' for i in range(150)) + "</body></html>"
    monkeypatch.setattr(main, "_fetch_page", lambda url: (url, "text/html", 200, html))
    monkeypatch.setattr(main.trafilatura, "extract", lambda *args, **kwargs: "High quality article text")
    result = main.extract_web_content("https://example.com/")
    assert result["main_text"] == "High quality article text"
    assert len(result["links"]) == main.MAX_LINKS
    assert "raw_html" in result and "content_status" in result


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
        "content_status": "ok",
        "usable": True,
        "block_reason": None,
    }
    monkeypatch.setattr(main, "extract_web_content", lambda url: full_result)
    agent_result = {key: value for key, value in full_result.items() if key not in {"links", "raw_html"}}
    monkeypatch.setattr(main, "extract_agent_content", lambda url: agent_result)
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
        "content_status": "ok",
        "usable": True,
        "block_reason": None,
    }
    assert "raw_html" not in agent_response.get_json()
    assert "links" not in agent_response.get_json()


def test_unsupported_or_duplicate_modes_are_rejected(client, monkeypatch):
    monkeypatch.setattr(main, "extract_web_content", lambda url: {"url": url})
    headers = {"Authorization": "Bearer test-secret"}

    unsupported = client.get("/extract?url=https://example.com&mode=compact", headers=headers)
    assert unsupported.status_code == 400
    assert unsupported.get_json() == {"error": {"code": "invalid_mode", "message": "Unsupported extraction mode."}}

    explicit_full = client.get("/extract?url=https://example.com&mode=full", headers=headers)
    assert explicit_full.status_code == 400
    assert explicit_full.get_json() == {"error": {"code": "invalid_mode", "message": "Unsupported extraction mode."}}

    duplicate = client.get("/extract?url=https://example.com&mode=agent&mode=agent", headers=headers)
    assert duplicate.status_code == 400
    assert duplicate.get_json() == {"error": {"code": "invalid_mode", "message": "Unsupported extraction mode."}}

    duplicate_product = client.get("/extract?url=https://example.com&mode=product&mode=product", headers=headers)
    assert duplicate_product.status_code == 400
    assert duplicate_product.get_json() == {"error": {"code": "invalid_mode", "message": "Unsupported extraction mode."}}


def test_startup_requires_token(monkeypatch):
    monkeypatch.delenv("EXTRACT_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="EXTRACT_API_TOKEN"):
        main.create_app()
