# Guanxe Product Mode V1 — review candidate

**Status:** source implementation + offline regression complete. **NOT approved for production:** full live PDP HTML and API responses have not been available in this environment. No merge, deploy, n8n changes, or additional network fetch was made.

## Repository boundary and paused work

- Base `main`: `fac2f17d47961ea23b5e84b898776693ce2d45dd` (production Python Free, unchanged).
- Worten branch: `codex/worten-canarias-adapter-v1` at `eb0465ff78c676ee876a0b86d934f36167ac32ab`, preserved.
- Docker Worten DEV: `dev/docker-trixie-worten` at `01787b9a6fbc127dd497aa0a9a8adf61fdff5fe7`, Render deploy `dep-dbgvh4c9v7es73bfkrc0` Live, preserved.
- Guanxe branch: `feat/guanxe-product-v1` is based on `main`, so this work **does not import or modify paused Worten code**.

## Source-level findings

Generic Product Mode prioritizes page-local `itemGtag`, matched JSON-LD, IKEA-style article numbers and 8-digit URL article IDs. Guanxe's own visible `Ref:` is not recognized. The generic `_find_main_context` may restrict specifications to the H1 product-information wrapper even though Guanxe renders a separately tabbed product description and/or technical details, including tables, feature lists and `dl` pairs. Generic descriptions favor JSON-LD/meta content before truncating at 5,000 characters, without isolating promotional text or shopping policies. Generic availability scans paragraph text from the whole document, potentially including unrelated sections or promotional phrasing.

## Implementation and semantics

Only `mode=product` requests for `https://guanxe.com/es/<category>/<numeric-id>-<slug>.html` or the `www` equivalent activate a bounded, one-pass **HTML-only** post-processor. All other sites retain the generic parser. Five response keys and all product keys are unchanged. No network, browser or additional request is introduced. Previously working name, brand, current price, currency, original price and image keep the generic source/precedence rules.

- `sku` is set **only** from one unambiguous, explicit `Ref:` found close to the PDP H1; URL IDs, model tokens inferred from title and EAN do **not** become `sku`.
- Catalog internal ID in URL is used solely to confirm that two Guanxe category URLs identify the same product. Explicit `<link rel="canonical">` may change the category path only with same host and numeric product ID; a different ID is rejected.
- JSON-LD Product URL association uses that same restricted Guanxe identity rule, preserving known brand/price when a canonical category changes.
- Technical specifications are gathered only from recognized PDP description / technical tabs: two-column tables, `<dl>` terms and bounded explicitly technical nested lists. Identical label-value pairs are deduplicated; group labels are retained. Embedded EAN may be listed under `Identifiers`, never substituted for SKU. Marketing, recommendations and policy sections are excluded.
- Description is selected from product prose paragraphs/headings, stopping at returns-policy content, and does not serialize technical-list prose or shopping boilerplate. If clean description replaces an overlong generic metadata description, its `field_truncated` warning is removed only when the product name is not truncated.
- Availability uses short, product-scoped text leaves only. Explicit temporary out-of-stock and low-units messages are retained as evidence (without interpreting them as ATP); generic "Disponible en Canarias" is excluded. Mixed explicit positive and negative stock labels produce `availability_conflict`.
- Unverified selected variant is `null`, even when title includes color/storage. Existing generic extraction remains unmodified for other domains.

## Tests

Local Python / BeautifulSoup / pytest: **34 PASS**, covering 11 Guanxe fixture checks and all 23 existing generic/IKEA Product Mode tests. `compileall` passes. Synthetic tests cover JBL, S24 Ultra, Z Flip7 FE, Ref conflict, cross-product isolation, canonical category changes, non-PDP URLs, missing JSON-LD identity, no-summary-stock, and returns-policy contamination. These are reduced synthetic HTML fixtures informed by observed page text, **not saved full live PDP HTML**.

## Before / after: offline fixture data only

These prices are **test inputs**, not independently verified current Guanxe prices. The complete before/after five-key JSON for JBL, S24 and Flip is in `guanxe-before-after-synthetic.json` (external review artifact, not production output).

| Synthetic PDP | SKU before / after | Technical specs before / after | Description chars before / after | Warnings before / after | Compact JSON bytes before / after |
|---|---|---:|---:|---|---:|
| JBL Tune 527BT | `null` → `30458` | 0 → 4 | 28 → 105 | [] → [] | 861 → 1,261 |
| Samsung S24 Ultra | `null` → `SM-S928B/DS` | 3 → 3 | 28 → 83 | [] → [] | 960 → 1,082 |
| Samsung Flip7 FE | `null` → `4040102798` | 0 → 1 | 5000 → 43 | [`field_truncated`] → [] | 5,850 → 947 |

The S24 fixture already contains a technical table recognized by the generic extractor. Guanxe improves its **reference and description**, not necessarily the number of specifications in every page.

## Performance

On the synthetic HTML inputs, 100 repeated parse-only calls per variant (median): JBL 1.687 → 3.793 ms; S24 1.645 → 3.652 ms; Flip 1.496 → 3.273 ms. These values exclude network fetch, TLS, Render cold starts and n8n; they must **not** be compared directly with previously observed ~9–10 seconds real n8n Product Mode latency. No fresh production or DEV HTTP latency nor live response-size measurement is available, because no deployment or authorized live HTML capture has been performed.

## Acceptance gaps / limitations

- The actual HTML DOM and current values of the three Guanxe PDPs have **not** been verified with this branch. Publicly indexed text indicates Ref/specifications/availability but may be stale. Structured tabs/selectors might vary; live smoke tests are necessary before owner approval.
- The existing n8n execution payloads 2820, 2821 and 2813 were described by the owner but their raw responses/HTML were not supplied in the accessible source set.
- Manufacturer model and EAN are only emitted as specifications if explicitly present in scoped product HTML, not inferred from URL suffix or name. No regional pickup/delivery ATP is implied.
- This feature branch deliberately excludes Worten until its task resumes; a later combined release must be integrated explicitly, with conflicts and regression reviewed.

## Proposed release and rollback (not executed)

1. Obtain actual fetched HTML or run owner-approved checks against the three Guanxe PDP URLs using the isolated review build, checking fields, canonical identity, warnings, latency and byte size. Confirm a relevant IKEA Product regression against existing production response.
2. Owner reviews JSON and approves. Keep the tested commit immutable for review; merge only with explicit approval. Note that `main` has Render **auto-deploy enabled**, so merging is a production deployment event.
3. After merge/deploy, smoke-test `/health`, three Guanxe Product responses, an IKEA Product and one generic Product, all with the original bearer-token contract. No n8n changes until validated.
4. If any regression is found, restore last known production `main` revision `fac2f17d...` via a controlled revert or Render rollback, preserving endpoint/token and retaining the isolated Guanxe branch for analysis.
5. Only after production pass, give Morpheus the unchanged five-key Product response contract for its Exa → Product → Router n8n DEV integration.
