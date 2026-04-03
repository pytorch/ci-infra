"""Unit tests for PyPI index merge algorithm (njs merge_indexes.js).

The merge logic runs as njs inside nginx (merge_indexes.js). These tests
validate the algorithm by reimplementing the core parsing/merging functions
in Python and testing exhaustively.

Python implementations mirror the njs functions exactly -- same names, same
algorithm -- so bugs in one surface in the other.
"""

from __future__ import annotations

import json
import re

# ---------------------------------------------------------------------------
# Python reimplementation of njs merge_indexes.js functions
# ---------------------------------------------------------------------------

UPSTREAM_HOSTS = [
    "https://files.pythonhosted.org",
    "https://download.pytorch.org",
]


def parse_html_links(body: str) -> list[dict]:
    """Parse <a> tags from a PEP 503 HTML index page.

    Returns list of {href, text, attrs} dicts.
    Mirrors: parseHtmlLinks() in merge_indexes.js
    """
    links = []
    for match in re.finditer(r'<a\s+href="([^"]*)"([^>]*)>([^<]*)</a>', body, re.IGNORECASE):
        links.append(
            {
                "href": match.group(1),
                "attrs": match.group(2).strip(),
                "text": match.group(3),
            }
        )
    return links


def parse_json_files(body: str) -> list[dict]:
    """Parse files array from a PEP 691 JSON index response.

    Returns the files array (or empty list on parse failure).
    Mirrors: parseJsonFiles() in merge_indexes.js
    """
    try:
        data = json.loads(body)
        return data.get("files", [])
    except (json.JSONDecodeError, TypeError):
        return []


def extract_filename(url_or_path: str) -> str:
    """Extract filename from a URL or path.

    e.g. "/packages/ab/cd/foo-1.0.whl" -> "foo-1.0.whl"
    Mirrors: extractFilename() in merge_indexes.js
    """
    parts = url_or_path.split("/")
    return parts[-1].split("#")[0]


def rewrite_upstream_urls(href: str) -> str:
    """Remove upstream host prefixes from absolute URLs.

    Strips https://files.pythonhosted.org and https://download.pytorch.org
    prefixes, matching the sub_filter behavior in nginx.conf.
    Mirrors: rewriteUrl() in merge_indexes.js
    """
    for host in UPSTREAM_HOSTS:
        if href.startswith(host):
            return href[len(host) :]
    return href


def merge_links(local_links: list[dict], upstream_links: list[dict]) -> list[dict]:
    """Merge two arrays of HTML link objects. Deduplicates by filename.

    Local links win on collision (local wheel is faster to serve).
    Mirrors: mergeHtmlLinks() in merge_indexes.js
    """
    seen: dict[str, bool] = {}
    merged = []

    for link in local_links:
        filename = extract_filename(link["href"])
        if filename:
            seen[filename] = True
        merged.append(link)

    for link in upstream_links:
        fname = extract_filename(link["href"])
        if fname and fname in seen:
            continue
        if fname:
            seen[fname] = True
        merged.append(
            {
                "href": rewrite_upstream_urls(link["href"]),
                "attrs": link["attrs"],
                "text": link["text"],
            }
        )

    return merged


def merge_json_files(local_files: list[dict], upstream_files: list[dict]) -> list[dict]:
    """Merge two arrays of PEP 691 JSON file objects. Deduplicates by filename.

    Local files win on collision.
    Mirrors: mergeJsonFiles() in merge_indexes.js
    """
    seen: dict[str, bool] = {}
    merged = []

    for f in local_files:
        filename = f.get("filename", "")
        if filename:
            seen[filename] = True
        merged.append(f)

    for f in upstream_files:
        filename = f.get("filename", "")
        if filename and filename in seen:
            continue
        if filename:
            seen[filename] = True
        rewritten = dict(f)
        if "url" in rewritten:
            rewritten["url"] = rewrite_upstream_urls(rewritten["url"])
        merged.append(rewritten)

    return merged


def render_html(package_name: str, links: list[dict]) -> str:
    """Render a PEP 503 HTML index page from link objects.

    Mirrors: renderHtml() in merge_indexes.js
    """
    html = (
        "<!DOCTYPE html>\n<html><head><title>"
        + package_name
        + "</title></head>\n<body>\n<h1>"
        + package_name
        + "</h1>\n"
    )
    for link in links:
        html += '<a href="' + link["href"] + '"'
        if link["attrs"]:
            html += " " + link["attrs"]
        html += ">" + link["text"] + "</a>\n"
    html += "</body>\n</html>\n"
    return html


def render_json(package_name: str, files: list[dict]) -> str:
    """Render a PEP 691 JSON index response from file objects.

    Mirrors: renderJson() in merge_indexes.js
    """
    return json.dumps(
        {
            "meta": {"api_version": "1.0"},
            "name": package_name,
            "files": files,
        }
    )


def html_links_to_json_files(links: list[dict]) -> list[dict]:
    """Convert HTML link objects to PEP 691 JSON file objects.

    Extracts hash from URL fragment (#sha256=...) into the hashes dict,
    which is required by PEP 691 (uv strictly validates this).
    Used when pypiserver returns HTML but the client requested JSON format.
    Mirrors: htmlLinksToJsonFiles() in merge_indexes.js
    """
    files = []
    for link in links:
        filename = extract_filename(link["href"])
        if filename:
            entry: dict = {
                "filename": filename,
                "url": link["href"],
                "hashes": {},
            }
            href = link["href"]
            hash_idx = href.find("#")
            if hash_idx != -1:
                fragment = href[hash_idx + 1 :]
                if "=" in fragment:
                    algo, digest = fragment.split("=", 1)
                    if algo and digest:
                        entry["hashes"] = {algo: digest}
            files.append(entry)
    return files


def parse_files_with_fallback(response_text: str) -> list[dict]:
    """Parse as PEP 691 JSON; if JSON.parse fails, fall back to HTML.

    If JSON.parse succeeds, the result is trusted even if files is empty.
    Mirrors: parseFilesWithFallback() in merge_indexes.js
    """
    if not response_text:
        return []
    try:
        data = json.loads(response_text)
        return data.get("files", [])
    except (json.JSONDecodeError, ValueError):
        return html_links_to_json_files(parse_html_links(response_text))


def rewrite_html_body(body: str) -> str:
    """Rewrite all upstream URLs in an HTML response body.

    Mirrors: rewriteHtmlBody() in merge_indexes.js
    """
    result = body
    for host in UPSTREAM_HOSTS:
        result = result.replace(host, "")
    return result


def rewrite_json_files(files: list[dict]) -> list[dict]:
    """Rewrite all upstream URLs in a JSON files array.

    Mirrors: rewriteJsonFiles() in merge_indexes.js
    """
    rewritten = []
    for f in files:
        entry = dict(f)
        if "url" in entry:
            entry["url"] = rewrite_upstream_urls(entry["url"])
        rewritten.append(entry)
    return rewritten


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


def _html_page(package: str, links: list[tuple[str, str]]) -> str:
    """Build a minimal PEP 503 HTML page. links = [(href, text), ...]."""
    body = f"<!DOCTYPE html>\n<html><head><title>{package}</title></head>\n<body>\n"
    for href, text in links:
        body += f'<a href="{href}">{text}</a>\n'
    body += "</body>\n</html>\n"
    return body


def _html_page_with_attrs(package: str, links: list[tuple[str, str, str]]) -> str:
    """Build a PEP 503 HTML page with attrs. links = [(href, attrs, text), ...]."""
    body = f"<!DOCTYPE html>\n<html><head><title>{package}</title></head>\n<body>\n"
    for href, attrs, text in links:
        if attrs:
            body += f'<a href="{href}" {attrs}>{text}</a>\n'
        else:
            body += f'<a href="{href}">{text}</a>\n'
    body += "</body>\n</html>\n"
    return body


def _json_response(package: str, files: list[dict]) -> str:
    """Build a PEP 691 JSON response."""
    return json.dumps(
        {
            "meta": {"api_version": "1.0"},
            "name": package,
            "files": files,
        }
    )


def _json_file(filename: str, url: str, **kwargs: object) -> dict:
    """Build a PEP 691 file entry."""
    entry: dict = {"filename": filename, "url": url}
    entry.update(kwargs)
    return entry


# ---------------------------------------------------------------------------
# Sample test data (realistic package names and filenames)
# ---------------------------------------------------------------------------

# Local pypiserver HTML (wrong variant -- cp312-x86_64 only)
LOCAL_HTML_WRONG_VARIANT = (
    "<!DOCTYPE html>\n"
    "<html><head><title>Links for cffi</title></head><body>\n"
    "<h1>Links for cffi</h1>\n"
    '<a href="/packages/cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64'
    '.manylinux2014_x86_64.whl#sha256=abc123">'
    "cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64"
    ".manylinux2014_x86_64.whl</a>\n"
    "</body></html>"
)

# Upstream pypi.org HTML (has cp311-aarch64 wheel + sdist)
UPSTREAM_HTML_FULL = (
    "<!DOCTYPE html>\n"
    "<html><head><title>Links for cffi</title></head><body>\n"
    "<h1>Links for cffi</h1>\n"
    '<a href="https://files.pythonhosted.org/packages/fc/97/'
    "cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64"
    '.manylinux2014_x86_64.whl#sha256=abc123"'
    ' data-requires-python="&gt;=3.8">'
    "cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64"
    ".manylinux2014_x86_64.whl</a>\n"
    '<a href="https://files.pythonhosted.org/packages/1e/bf/'
    "cffi-1.17.1-cp311-cp311-manylinux_2_17_aarch64"
    '.manylinux2014_aarch64.whl#sha256=def456"'
    ' data-requires-python="&gt;=3.8">'
    "cffi-1.17.1-cp311-cp311-manylinux_2_17_aarch64"
    ".manylinux2014_aarch64.whl</a>\n"
    '<a href="https://files.pythonhosted.org/packages/04/dd/'
    'cffi-1.17.1.tar.gz#sha256=ghi789"'
    ' data-requires-python="&gt;=3.8">'
    "cffi-1.17.1.tar.gz</a>\n"
    "</body></html>"
)

# Upstream HTML with sdist only
UPSTREAM_HTML_SDIST_ONLY = (
    "<!DOCTYPE html>\n"
    "<html><head><title>Links for cffi</title></head><body>\n"
    "<h1>Links for cffi</h1>\n"
    '<a href="https://files.pythonhosted.org/packages/04/dd/'
    'cffi-1.17.1.tar.gz#sha256=ghi789"'
    ' data-requires-python="&gt;=3.8">'
    "cffi-1.17.1.tar.gz</a>\n"
    "</body></html>"
)

# Local HTML with exact match (correct variant for aarch64)
LOCAL_HTML_EXACT_MATCH = (
    "<!DOCTYPE html>\n"
    "<html><head><title>Links for cffi</title></head><body>\n"
    "<h1>Links for cffi</h1>\n"
    '<a href="/packages/cffi-1.17.1-cp311-cp311-manylinux_2_17_aarch64'
    '.manylinux2014_aarch64.whl#sha256=localdef">'
    "cffi-1.17.1-cp311-cp311-manylinux_2_17_aarch64"
    ".manylinux2014_aarch64.whl</a>\n"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# TestParseHtmlLinks
# ---------------------------------------------------------------------------
class TestParseHtmlLinks:
    def test_parses_standard_pep503_page(self):
        """Standard pypiserver/pypi.org index page with multiple <a> tags."""
        links = parse_html_links(UPSTREAM_HTML_FULL)
        assert len(links) == 3
        assert links[0]["text"] == ("cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
        assert links[1]["text"] == ("cffi-1.17.1-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl")
        assert links[2]["text"] == "cffi-1.17.1.tar.gz"

    def test_preserves_data_requires_python(self):
        """data-requires-python attribute is preserved in parsed output."""
        links = parse_html_links(UPSTREAM_HTML_FULL)
        for link in links:
            assert 'data-requires-python="&gt;=3.8"' in link["attrs"]

    def test_preserves_hash_fragment(self):
        """#sha256=... hash fragment in href is preserved."""
        links = parse_html_links(UPSTREAM_HTML_FULL)
        assert links[0]["href"].endswith("#sha256=abc123")
        assert links[1]["href"].endswith("#sha256=def456")
        assert links[2]["href"].endswith("#sha256=ghi789")

    def test_handles_empty_body(self):
        """Empty HTML body returns empty list."""
        assert parse_html_links("") == []

    def test_handles_no_links(self):
        """HTML with no <a> tags returns empty list."""
        html = "<!DOCTYPE html><html><body><h1>No links</h1></body></html>"
        assert parse_html_links(html) == []

    def test_multiple_attributes(self):
        """Multiple attributes (data-requires-python, data-dist-info-metadata) preserved."""
        html = '<a href="/p.whl" data-requires-python="&gt;=3.8" data-dist-info-metadata="true">p.whl</a>'
        links = parse_html_links(html)
        assert len(links) == 1
        assert "data-requires-python" in links[0]["attrs"]
        assert "data-dist-info-metadata" in links[0]["attrs"]

    def test_case_insensitive_tag(self):
        """<A HREF=...> parsed same as <a href=...>."""
        html = '<A HREF="/pkg.whl">pkg.whl</A>'
        links = parse_html_links(html)
        assert len(links) == 1

    def test_single_link_no_attrs(self):
        """Single link without extra attributes."""
        html = '<a href="/pkg-1.0.whl">pkg-1.0.whl</a>'
        links = parse_html_links(html)
        assert len(links) == 1
        assert links[0]["attrs"] == ""


# ---------------------------------------------------------------------------
# TestParseJsonFiles
# ---------------------------------------------------------------------------
class TestParseJsonFiles:
    def test_parses_standard_pep691_response(self):
        """Standard pypi.org JSON response with files array."""
        body = _json_response(
            "numpy",
            [
                _json_file(
                    "numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl",
                    "https://files.pythonhosted.org/packages/ab/cd/numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl",
                    hashes={"sha256": "abc123"},
                ),
                _json_file(
                    "numpy-1.26.4.tar.gz",
                    "https://files.pythonhosted.org/packages/ef/gh/numpy-1.26.4.tar.gz",
                ),
            ],
        )
        files = parse_json_files(body)
        assert len(files) == 2
        assert files[0]["filename"] == "numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl"

    def test_preserves_hashes(self):
        """Hash metadata (sha256, md5) is preserved."""
        body = _json_response(
            "pkg",
            [
                _json_file("pkg-1.0.whl", "/pkg.whl", hashes={"sha256": "dead", "md5": "beef"}),
            ],
        )
        files = parse_json_files(body)
        assert files[0]["hashes"] == {"sha256": "dead", "md5": "beef"}

    def test_preserves_requires_python(self):
        """requires-python field is preserved."""
        body = _json_response(
            "pkg",
            [
                _json_file("pkg-1.0.whl", "/pkg.whl", requires_python=">=3.8"),
            ],
        )
        files = parse_json_files(body)
        assert files[0]["requires_python"] == ">=3.8"

    def test_handles_empty_files(self):
        """JSON with empty files array returns empty list."""
        body = json.dumps({"meta": {"api_version": "1.0"}, "name": "empty", "files": []})
        assert parse_json_files(body) == []

    def test_handles_invalid_json(self):
        """Invalid JSON returns empty list."""
        assert parse_json_files("not valid json{{{") == []

    def test_handles_missing_files_key(self):
        """JSON without files key returns empty list."""
        assert parse_json_files(json.dumps({"meta": {}})) == []

    def test_handles_none_input(self):
        """None input returns empty list."""
        assert parse_json_files(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestRewriteUpstreamUrls
# ---------------------------------------------------------------------------
class TestRewriteUpstreamUrls:
    def test_rewrites_pythonhosted_url(self):
        """https://files.pythonhosted.org/packages/... -> /packages/..."""
        url = "https://files.pythonhosted.org/packages/fc/97/cffi-1.17.1.whl"
        assert rewrite_upstream_urls(url) == "/packages/fc/97/cffi-1.17.1.whl"

    def test_rewrites_pytorch_url(self):
        """https://download.pytorch.org/whl/... -> /whl/..."""
        url = "https://download.pytorch.org/whl/cu121/torch-2.2.0-cp311-cp311-linux_x86_64.whl"
        assert rewrite_upstream_urls(url) == "/whl/cu121/torch-2.2.0-cp311-cp311-linux_x86_64.whl"

    def test_preserves_relative_url(self):
        """Relative URLs (from pypiserver) are unchanged."""
        url = "/packages/cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.whl"
        assert rewrite_upstream_urls(url) == url

    def test_preserves_hash_fragment(self):
        """Hash fragment is preserved after rewriting."""
        url = "https://files.pythonhosted.org/packages/fc/97/cffi.whl#sha256=abc123"
        assert rewrite_upstream_urls(url) == "/packages/fc/97/cffi.whl#sha256=abc123"

    def test_preserves_unknown_host(self):
        """URLs with unrecognized hosts are unchanged."""
        url = "https://example.com/packages/foo-1.0.whl"
        assert rewrite_upstream_urls(url) == url

    def test_empty_string(self):
        """Empty string returns empty string."""
        assert rewrite_upstream_urls("") == ""

    def test_host_only_no_path(self):
        """Host prefix with no path returns empty string."""
        assert rewrite_upstream_urls("https://files.pythonhosted.org") == ""


# ---------------------------------------------------------------------------
# TestExtractFilename
# ---------------------------------------------------------------------------
class TestExtractFilename:
    def test_relative_path(self):
        assert extract_filename("/packages/ab/cd/foo-1.0.whl") == "foo-1.0.whl"

    def test_absolute_url(self):
        assert extract_filename("https://files.pythonhosted.org/packages/ab/cd/foo-1.0.whl") == "foo-1.0.whl"

    def test_strips_hash_fragment(self):
        assert extract_filename("/packages/ab/cd/foo-1.0.whl#sha256=abc123") == "foo-1.0.whl"

    def test_bare_filename(self):
        assert extract_filename("foo-1.0.whl") == "foo-1.0.whl"

    def test_empty_string(self):
        assert extract_filename("") == ""

    def test_trailing_slash(self):
        assert extract_filename("/packages/ab/cd/") == ""


# ---------------------------------------------------------------------------
# TestMergeLinks (HTML)
# ---------------------------------------------------------------------------
class TestMergeLinks:
    def test_local_only(self):
        """Only local links, no upstream -> returns local links unchanged."""
        local = [{"href": "/packages/local/pkg-1.0.whl", "attrs": "", "text": "pkg-1.0.whl"}]
        merged = merge_links(local, [])
        assert len(merged) == 1
        assert merged[0]["href"] == "/packages/local/pkg-1.0.whl"

    def test_upstream_only(self):
        """Only upstream links, no local -> returns upstream with URL rewriting."""
        upstream = [
            {
                "href": "https://files.pythonhosted.org/packages/ab/cd/pkg-1.0.whl",
                "attrs": "",
                "text": "pkg-1.0.whl",
            }
        ]
        merged = merge_links([], upstream)
        assert len(merged) == 1
        assert merged[0]["href"] == "/packages/ab/cd/pkg-1.0.whl"

    def test_both_no_overlap(self):
        """Local and upstream have different packages -> all included."""
        local = [{"href": "/local/local-1.0.whl", "attrs": "", "text": "local-1.0.whl"}]
        upstream = [
            {
                "href": "https://files.pythonhosted.org/packages/ab/cd/upstream-2.0.whl",
                "attrs": "",
                "text": "upstream-2.0.whl",
            }
        ]
        merged = merge_links(local, upstream)
        assert len(merged) == 2
        filenames = [extract_filename(m["href"]) for m in merged]
        assert "local-1.0.whl" in filenames
        assert "upstream-2.0.whl" in filenames

    def test_both_with_overlap_local_wins(self):
        """Same filename in both -> local version kept, upstream dropped."""
        local = [{"href": "/packages/local/pkg-1.0.whl", "attrs": "", "text": "pkg-1.0.whl"}]
        upstream = [
            {
                "href": "https://files.pythonhosted.org/packages/ab/cd/pkg-1.0.whl",
                "attrs": 'data-requires-python=">=3.8"',
                "text": "pkg-1.0.whl",
            }
        ]
        merged = merge_links(local, upstream)
        assert len(merged) == 1
        assert merged[0]["href"] == "/packages/local/pkg-1.0.whl"
        assert merged[0]["attrs"] == ""

    def test_deduplicates_by_filename(self):
        """Filename is the dedup key, not the full URL path."""
        local = [{"href": "/local/a/b/pkg-1.0.whl", "attrs": "", "text": "pkg-1.0.whl"}]
        upstream = [
            {
                "href": "https://files.pythonhosted.org/packages/x/y/z/pkg-1.0.whl",
                "attrs": "",
                "text": "pkg-1.0.whl",
            }
        ]
        merged = merge_links(local, upstream)
        assert len(merged) == 1

    def test_upstream_urls_rewritten(self):
        """Upstream URLs are rewritten even when no local overlap."""
        upstream = [
            {
                "href": "https://files.pythonhosted.org/packages/ab/cd/pkg-1.0.whl#sha256=abc",
                "attrs": "",
                "text": "pkg-1.0.whl",
            }
        ]
        merged = merge_links([], upstream)
        assert merged[0]["href"] == "/packages/ab/cd/pkg-1.0.whl#sha256=abc"

    def test_empty_local_empty_upstream(self):
        """Both empty -> returns empty list."""
        assert merge_links([], []) == []

    def test_order_local_first(self):
        """Local links appear before upstream links in merged output."""
        local = [{"href": "/local/a.whl", "attrs": "", "text": "a.whl"}]
        upstream = [{"href": "/upstream/b.whl", "attrs": "", "text": "b.whl"}]
        merged = merge_links(local, upstream)
        assert merged[0]["text"] == "a.whl"
        assert merged[1]["text"] == "b.whl"

    def test_attrs_preserved_for_upstream(self):
        """Upstream link attrs preserved after merge."""
        upstream = [
            {
                "href": "https://files.pythonhosted.org/packages/ab/cd/pkg-1.0.whl",
                "attrs": 'data-requires-python="&gt;=3.8"',
                "text": "pkg-1.0.whl",
            }
        ]
        merged = merge_links([], upstream)
        assert merged[0]["attrs"] == 'data-requires-python="&gt;=3.8"'


# ---------------------------------------------------------------------------
# TestMergeJsonFiles
# ---------------------------------------------------------------------------
class TestMergeJsonFiles:
    def test_local_wins_on_collision(self):
        """Same filename in both -> local version kept."""
        local = [_json_file("pkg-1.0.whl", "/local/pkg-1.0.whl")]
        upstream = [
            _json_file(
                "pkg-1.0.whl",
                "https://files.pythonhosted.org/packages/ab/cd/pkg-1.0.whl",
                hashes={"sha256": "upstream"},
            )
        ]
        merged = merge_json_files(local, upstream)
        assert len(merged) == 1
        assert merged[0]["url"] == "/local/pkg-1.0.whl"
        assert "hashes" not in merged[0]

    def test_upstream_urls_rewritten(self):
        """Upstream file URLs are rewritten to remove host prefix."""
        upstream = [
            _json_file(
                "pkg-1.0.whl",
                "https://files.pythonhosted.org/packages/ab/cd/pkg-1.0.whl",
            )
        ]
        merged = merge_json_files([], upstream)
        assert merged[0]["url"] == "/packages/ab/cd/pkg-1.0.whl"

    def test_empty_lists(self):
        """Both empty -> returns empty list."""
        assert merge_json_files([], []) == []

    def test_metadata_preserved(self):
        """Upstream metadata (hashes, requires_python, size) preserved."""
        upstream = [
            _json_file(
                "pkg-1.0.whl",
                "https://files.pythonhosted.org/packages/ab/cd/pkg-1.0.whl",
                hashes={"sha256": "abc123"},
                requires_python=">=3.8",
                size=12345,
            )
        ]
        merged = merge_json_files([], upstream)
        assert merged[0]["hashes"] == {"sha256": "abc123"}
        assert merged[0]["requires_python"] == ">=3.8"
        assert merged[0]["size"] == 12345

    def test_no_mutation_of_input(self):
        """Upstream file dicts are not mutated -- copies are made."""
        original = _json_file(
            "pkg-1.0.whl",
            "https://files.pythonhosted.org/packages/ab/cd/pkg-1.0.whl",
        )
        original_url = original["url"]
        merge_json_files([], [original])
        assert original["url"] == original_url

    def test_file_without_url(self):
        """File entry without url key -- no rewriting, still included."""
        upstream = [{"filename": "pkg-1.0.whl"}]
        merged = merge_json_files([], upstream)
        assert len(merged) == 1
        assert "url" not in merged[0]


# ---------------------------------------------------------------------------
# TestRenderHtml
# ---------------------------------------------------------------------------
class TestRenderHtml:
    def test_renders_valid_html(self):
        """Rendered HTML contains DOCTYPE, title, and all links."""
        links = [
            {"href": "/packages/foo-1.0.whl", "attrs": "", "text": "foo-1.0.whl"},
            {"href": "/packages/bar-2.0.tar.gz", "attrs": 'data-requires-python="&gt;=3.8"', "text": "bar-2.0.tar.gz"},
        ]
        html = render_html("test-pkg", links)
        assert html.startswith("<!DOCTYPE html>")
        assert "<title>test-pkg</title>" in html
        assert "<h1>test-pkg</h1>" in html
        assert 'href="/packages/foo-1.0.whl"' in html
        assert 'href="/packages/bar-2.0.tar.gz"' in html
        assert 'data-requires-python="&gt;=3.8"' in html

    def test_empty_links(self):
        """No links -> valid HTML with no <a> tags."""
        html = render_html("empty", [])
        assert "<!DOCTYPE html>" in html
        assert "<a " not in html

    def test_no_attrs_no_extra_space(self):
        """Links without attrs have no spurious space before >."""
        links = [{"href": "/pkg.whl", "attrs": "", "text": "pkg.whl"}]
        html = render_html("pkg", links)
        assert '<a href="/pkg.whl">pkg.whl</a>' in html


# ---------------------------------------------------------------------------
# TestRenderJson
# ---------------------------------------------------------------------------
class TestRenderJson:
    def test_renders_valid_json(self):
        """Rendered JSON is valid and contains expected structure."""
        files = [_json_file("foo-1.0.whl", "/packages/foo-1.0.whl")]
        result = json.loads(render_json("foo", files))
        assert result["meta"]["api_version"] == "1.0"
        assert result["name"] == "foo"
        assert len(result["files"]) == 1
        assert result["files"][0]["filename"] == "foo-1.0.whl"

    def test_empty_files(self):
        """No files -> valid JSON with empty files array."""
        result = json.loads(render_json("empty", []))
        assert result["files"] == []


# ---------------------------------------------------------------------------
# TestRewriteHtmlBody
# ---------------------------------------------------------------------------
class TestRewriteHtmlBody:
    def test_pythonhosted_stripped(self):
        body = '<a href="https://files.pythonhosted.org/packages/ab/cd/pkg-1.0.whl">pkg</a>'
        assert rewrite_html_body(body) == '<a href="/packages/ab/cd/pkg-1.0.whl">pkg</a>'

    def test_pytorch_stripped(self):
        body = '<a href="https://download.pytorch.org/whl/cu121/torch.whl">torch</a>'
        assert rewrite_html_body(body) == '<a href="/whl/cu121/torch.whl">torch</a>'

    def test_both_hosts_in_one_page(self):
        body = (
            '<a href="https://files.pythonhosted.org/packages/a/b/pkg.whl">pkg</a>\n'
            '<a href="https://download.pytorch.org/whl/torch.whl">torch</a>'
        )
        result = rewrite_html_body(body)
        assert "files.pythonhosted.org" not in result
        assert "download.pytorch.org" not in result

    def test_no_upstream_urls(self):
        body = '<a href="/local/pkg.whl">pkg</a>'
        assert rewrite_html_body(body) == body

    def test_multiple_occurrences(self):
        body = "https://files.pythonhosted.org/a https://files.pythonhosted.org/b"
        assert rewrite_html_body(body) == "/a /b"


# ---------------------------------------------------------------------------
# TestRewriteJsonFiles
# ---------------------------------------------------------------------------
class TestRewriteJsonFiles:
    def test_rewrites_urls(self):
        files = [_json_file("pkg.whl", "https://files.pythonhosted.org/packages/ab/cd/pkg.whl")]
        result = rewrite_json_files(files)
        assert result[0]["url"] == "/packages/ab/cd/pkg.whl"

    def test_preserves_metadata(self):
        files = [
            _json_file(
                "pkg.whl", "https://files.pythonhosted.org/packages/ab/cd/pkg.whl", hashes={"sha256": "abc"}, size=999
            )
        ]
        result = rewrite_json_files(files)
        assert result[0]["hashes"] == {"sha256": "abc"}
        assert result[0]["size"] == 999

    def test_no_url_key(self):
        files = [{"filename": "pkg.whl"}]
        result = rewrite_json_files(files)
        assert "url" not in result[0]

    def test_no_mutation(self):
        original = _json_file("pkg.whl", "https://files.pythonhosted.org/packages/ab/cd/pkg.whl")
        original_url = original["url"]
        rewrite_json_files([original])
        assert original["url"] == original_url


# ---------------------------------------------------------------------------
# TestScenarioMatrix -- All 9 scenarios from the routing analysis
# ---------------------------------------------------------------------------
class TestScenarioMatrix:
    """Test all 9 scenarios from the routing analysis.

    For each scenario, simulate the merge inputs and verify output.

    Matrix axes:
      Local:     A=404/empty, B=200 wrong variants, C=200 exact match
      Upstream:  X=404, Y=200 sdist only, Z=200 has wheel
    """

    def test_scenario_ax_empty_local_not_on_pypi(self):
        """AX: Both 404 -> merge returns empty, 404 status."""
        merged = merge_links([], [])
        assert merged == []

    def test_scenario_ay_empty_local_sdist_on_pypi(self):
        """AY: Local 404, upstream has sdist -> upstream sdist links returned."""
        upstream = parse_html_links(UPSTREAM_HTML_SDIST_ONLY)
        merged = merge_links([], upstream)
        assert len(merged) == 1
        assert "tar.gz" in extract_filename(merged[0]["href"])
        # URL should be rewritten to relative
        assert not merged[0]["href"].startswith("https://")

    def test_scenario_az_empty_local_wheel_on_pypi(self):
        """AZ: Local 404, upstream has wheel -> upstream wheel links returned."""
        upstream = parse_html_links(UPSTREAM_HTML_FULL)
        merged = merge_links([], upstream)
        assert len(merged) == 3
        filenames = [extract_filename(link["href"]) for link in merged]
        assert any(f.endswith(".whl") for f in filenames)
        assert any(f.endswith(".tar.gz") for f in filenames)

    def test_scenario_bx_wrong_variants_not_on_pypi(self):
        """BX: Local has wrong variants, upstream 404 -> local links only (pip will reject)."""
        local = parse_html_links(LOCAL_HTML_WRONG_VARIANT)
        merged = merge_links(local, [])
        assert len(merged) == 1
        assert "x86_64" in extract_filename(merged[0]["href"])

    def test_scenario_by_wrong_variants_sdist_on_pypi(self):
        """BY (THE FIX): Local has wrong variants, upstream has sdist -> MERGED index with both.

        This is the critical scenario the merge algorithm fixes. Without merging,
        pypiserver returns 200 with only the wrong-variant wheel, and nginx never
        falls back to pypi.org. The client cannot find a compatible distribution.

        With merging, the response contains BOTH the local incompatible wheel AND
        the upstream sdist, so pip/uv can select the compatible one.
        """
        local = parse_html_links(LOCAL_HTML_WRONG_VARIANT)
        upstream = parse_html_links(UPSTREAM_HTML_SDIST_ONLY)
        merged = merge_links(local, upstream)

        filenames = [extract_filename(link["href"]) for link in merged]
        # Must contain BOTH local wheel AND upstream sdist
        assert any("x86_64" in f and f.endswith(".whl") for f in filenames), (
            "Merged output must include local incompatible wheel"
        )
        assert any(f.endswith(".tar.gz") for f in filenames), "Merged output must include upstream sdist"
        assert len(merged) == 2

    def test_scenario_bz_wrong_variants_wheel_on_pypi(self):
        """BZ (THE FIX): Local has wrong variants, upstream has wheel -> MERGED index with both.

        Similar to BY but upstream has compatible wheels instead of just sdist.
        The merged output must contain ALL distributions so pip/uv can pick the
        compatible upstream wheel while still seeing local wheels.
        """
        local = parse_html_links(LOCAL_HTML_WRONG_VARIANT)
        upstream = parse_html_links(UPSTREAM_HTML_FULL)
        merged = merge_links(local, upstream)

        filenames = [extract_filename(link["href"]) for link in merged]
        # Local cp312-x86_64 wheel present (deduped -- local wins over upstream)
        assert ("cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl") in filenames
        # Upstream cp311-aarch64 wheel present (not in local)
        assert ("cffi-1.17.1-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl") in filenames
        # Upstream sdist present
        assert "cffi-1.17.1.tar.gz" in filenames
        # Total: 1 local + 2 upstream-unique = 3
        assert len(merged) == 3

    def test_scenario_cx_exact_match_not_on_pypi(self):
        """CX: Local exact match, upstream 404 -> local links only."""
        local = parse_html_links(LOCAL_HTML_EXACT_MATCH)
        merged = merge_links(local, [])
        assert len(merged) == 1
        assert "aarch64" in extract_filename(merged[0]["href"])

    def test_scenario_cy_exact_match_sdist_on_pypi(self):
        """CY: Local exact match + upstream sdist -> merged, pip prefers local wheel."""
        local = parse_html_links(LOCAL_HTML_EXACT_MATCH)
        upstream = parse_html_links(UPSTREAM_HTML_SDIST_ONLY)
        merged = merge_links(local, upstream)
        filenames = [extract_filename(link["href"]) for link in merged]
        assert any(f.endswith(".whl") for f in filenames)
        assert any(f.endswith(".tar.gz") for f in filenames)
        assert len(merged) == 2

    def test_scenario_cz_exact_match_wheel_on_pypi(self):
        """CZ: Local exact match + upstream wheel -> merged, local wins on dedup."""
        local = parse_html_links(LOCAL_HTML_EXACT_MATCH)
        upstream = parse_html_links(UPSTREAM_HTML_FULL)
        merged = merge_links(local, upstream)

        filenames = [extract_filename(link["href"]) for link in merged]
        # cp311-aarch64 in both -- local wins (local href kept)
        aarch64_links = [link for link in merged if "aarch64" in extract_filename(link["href"])]
        assert len(aarch64_links) == 1
        assert aarch64_links[0]["href"].startswith("/packages/")

        # Also includes upstream-unique entries: cp312-x86_64 + sdist
        assert ("cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl") in filenames
        assert "cffi-1.17.1.tar.gz" in filenames


# ---------------------------------------------------------------------------
# TestScenarioMatrixJson -- JSON variants of key scenarios
# ---------------------------------------------------------------------------
class TestScenarioMatrixJson:
    """JSON format variants of key scenarios (BY, BZ)."""

    def _local_json_wrong_variant(self) -> list[dict]:
        body = _json_response(
            "cffi",
            [
                _json_file(
                    "cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                    "/packages/cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                    hashes={"sha256": "abc123"},
                ),
            ],
        )
        return parse_json_files(body)

    def _upstream_json_full(self) -> list[dict]:
        body = _json_response(
            "cffi",
            [
                _json_file(
                    "cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                    "https://files.pythonhosted.org/packages/fc/97/cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                    hashes={"sha256": "abc123"},
                ),
                _json_file(
                    "cffi-1.17.1-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
                    "https://files.pythonhosted.org/packages/1e/bf/cffi-1.17.1-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
                    hashes={"sha256": "def456"},
                ),
                _json_file(
                    "cffi-1.17.1.tar.gz",
                    "https://files.pythonhosted.org/packages/04/dd/cffi-1.17.1.tar.gz",
                    hashes={"sha256": "ghi789"},
                ),
            ],
        )
        return parse_json_files(body)

    def _upstream_json_sdist_only(self) -> list[dict]:
        body = _json_response(
            "cffi",
            [
                _json_file(
                    "cffi-1.17.1.tar.gz",
                    "https://files.pythonhosted.org/packages/04/dd/cffi-1.17.1.tar.gz",
                    hashes={"sha256": "ghi789"},
                ),
            ],
        )
        return parse_json_files(body)

    def test_scenario_by_json(self):
        """BY (JSON): Wrong variants + upstream sdist -> merged JSON with both."""
        local = self._local_json_wrong_variant()
        upstream = self._upstream_json_sdist_only()
        merged = merge_json_files(local, upstream)

        filenames = [f["filename"] for f in merged]
        assert ("cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl") in filenames
        assert "cffi-1.17.1.tar.gz" in filenames
        assert len(merged) == 2

    def test_scenario_bz_json(self):
        """BZ (JSON): Wrong variants + upstream wheels -> merged JSON with both."""
        local = self._local_json_wrong_variant()
        upstream = self._upstream_json_full()
        merged = merge_json_files(local, upstream)

        filenames = [f["filename"] for f in merged]
        assert len(merged) == 3
        assert ("cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl") in filenames
        assert ("cffi-1.17.1-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl") in filenames
        assert "cffi-1.17.1.tar.gz" in filenames

        # Upstream URLs should be rewritten
        sdist = next(f for f in merged if f["filename"] == "cffi-1.17.1.tar.gz")
        assert sdist["url"].startswith("/packages/")

    def test_rendered_json_roundtrip(self):
        """End-to-end: merge + render produces valid PEP 691 JSON."""
        local = self._local_json_wrong_variant()
        upstream = self._upstream_json_full()
        merged = merge_json_files(local, upstream)
        rendered = render_json("cffi", merged)

        parsed = json.loads(rendered)
        assert parsed["name"] == "cffi"
        assert len(parsed["files"]) == 3
        assert parsed["meta"]["api_version"] == "1.0"


# ---------------------------------------------------------------------------
# TestHtmlLinksToJsonFiles
# ---------------------------------------------------------------------------
class TestHtmlLinksToJsonFiles:
    """Tests for htmlLinksToJsonFiles() — HTML link objects to JSON file objects."""

    def test_basic_conversion(self):
        """Converts standard HTML links to JSON file objects with hashes."""
        links = [
            {"href": "/packages/foo-1.0.whl#sha256=abc", "text": "foo-1.0.whl"},
            {"href": "/packages/bar-2.0.tar.gz", "text": "bar-2.0.tar.gz"},
        ]
        result = html_links_to_json_files(links)
        assert len(result) == 2
        assert result[0] == {
            "filename": "foo-1.0.whl",
            "url": "/packages/foo-1.0.whl#sha256=abc",
            "hashes": {"sha256": "abc"},
        }
        assert result[1] == {
            "filename": "bar-2.0.tar.gz",
            "url": "/packages/bar-2.0.tar.gz",
            "hashes": {},
        }

    def test_skips_empty_filename(self):
        """Links with no extractable filename are skipped."""
        links = [
            {"href": "/", "text": ".."},
            {"href": "/packages/foo-1.0.whl", "text": "foo-1.0.whl"},
        ]
        result = html_links_to_json_files(links)
        assert len(result) == 1
        assert result[0]["filename"] == "foo-1.0.whl"

    def test_empty_input(self):
        """Empty link list returns empty file list."""
        assert html_links_to_json_files([]) == []

    def test_preserves_full_href_as_url(self):
        """The url field retains the full href including hash fragment."""
        links = [
            {
                "href": "/packages/numpy-2.4.4-cp312-cp312-manylinux_2_27_x86_64.whl#sha256=deadbeef",
                "text": "numpy-2.4.4-cp312-cp312-manylinux_2_27_x86_64.whl",
            },
        ]
        result = html_links_to_json_files(links)
        assert result[0]["url"] == links[0]["href"]
        assert result[0]["filename"] == "numpy-2.4.4-cp312-cp312-manylinux_2_27_x86_64.whl"

    def test_upstream_absolute_urls(self):
        """Upstream absolute URLs are preserved (rewriting is a separate step)."""
        links = [
            {
                "href": "https://files.pythonhosted.org/packages/fc/97/foo-1.0.whl",
                "text": "foo-1.0.whl",
            },
        ]
        result = html_links_to_json_files(links)
        assert result[0]["url"] == links[0]["href"]
        assert result[0]["filename"] == "foo-1.0.whl"


# ---------------------------------------------------------------------------
# TestParseFilesWithFallback
# ---------------------------------------------------------------------------
class TestParseFilesWithFallback:
    """Tests for parseFilesWithFallback() — JSON parse with HTML fallback."""

    def test_valid_json_returns_files(self):
        """Valid PEP 691 JSON is parsed normally."""
        body = _json_response(
            "numpy",
            [
                _json_file("numpy-2.4.4.whl", "/packages/numpy-2.4.4.whl"),
            ],
        )
        result = parse_files_with_fallback(body)
        assert len(result) == 1
        assert result[0]["filename"] == "numpy-2.4.4.whl"

    def test_html_input_falls_back(self):
        """HTML input (JSON parse fails) falls back to HTML parsing."""
        result = parse_files_with_fallback(LOCAL_HTML_WRONG_VARIANT)
        assert len(result) == 1
        assert result[0]["filename"] == ("cffi-1.17.1-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")

    def test_empty_string_returns_empty(self):
        """Empty string returns empty list (no fallback attempt)."""
        assert parse_files_with_fallback("") == []

    def test_json_with_empty_files_and_nonempty_body(self):
        """JSON with empty files array returns empty (no fallback — JSON was valid)."""
        body = _json_response("empty-pkg", [])
        result = parse_files_with_fallback(body)
        assert result == []

    def test_garbage_input_returns_empty(self):
        """Non-JSON, non-HTML input returns empty (HTML parse finds no links)."""
        assert parse_files_with_fallback("this is not html or json") == []

    def test_pypiserver_html_with_multiple_wheels(self):
        """pypiserver-style HTML with multiple wheels is converted correctly."""
        html = _html_page(
            "torch",
            [
                ("/packages/torch-2.5.0-cp312-cp312-linux_x86_64.whl", "torch-2.5.0-cp312-cp312-linux_x86_64.whl"),
                ("/packages/torch-2.5.0-cp311-cp311-linux_x86_64.whl", "torch-2.5.0-cp311-cp311-linux_x86_64.whl"),
            ],
        )
        result = parse_files_with_fallback(html)
        assert len(result) == 2
        filenames = [f["filename"] for f in result]
        assert "torch-2.5.0-cp312-cp312-linux_x86_64.whl" in filenames
        assert "torch-2.5.0-cp311-cp311-linux_x86_64.whl" in filenames


# ---------------------------------------------------------------------------
# TestMixedFormatMerge — the exact bug scenario (PEP 691 format mismatch)
# ---------------------------------------------------------------------------
class TestMixedFormatMerge:
    """Tests for the PEP 691 format mismatch bug.

    Reproduces the exact failure: pip requests JSON, pypiserver returns HTML,
    pypi.org returns JSON. Without parseFilesWithFallback, local files parse
    as empty and upstream hash-based URLs pass through without deduplication.
    """

    # Realistic numpy HTML from pypiserver (flat local paths)
    LOCAL_NUMPY_HTML = (
        "<!DOCTYPE html>\n"
        "<html><head><title>Links for numpy</title></head><body>\n"
        "<h1>Links for numpy</h1>\n"
        '<a href="/packages/numpy-2.4.4-cp312-cp312-manylinux_2_27_x86_64'
        '.manylinux_2_28_x86_64.whl#sha256=aaa111">'
        "numpy-2.4.4-cp312-cp312-manylinux_2_27_x86_64"
        ".manylinux_2_28_x86_64.whl</a>\n"
        '<a href="/packages/numpy-2.4.4-cp311-cp311-manylinux_2_27_x86_64'
        '.manylinux_2_28_x86_64.whl#sha256=bbb222">'
        "numpy-2.4.4-cp311-cp311-manylinux_2_27_x86_64"
        ".manylinux_2_28_x86_64.whl</a>\n"
        "</body></html>"
    )

    # Realistic numpy JSON from pypi.org (hash-based upstream paths)
    UPSTREAM_NUMPY_JSON = _json_response(
        "numpy",
        [
            _json_file(
                "numpy-2.4.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
                "https://files.pythonhosted.org/packages/0a/0d/0e3ecece05b7a7e87ab9fb587855548da437a061326fff64a223b6dcb78a/"
                "numpy-2.4.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
                hashes={"sha256": "upstream_hash_1"},
            ),
            _json_file(
                "numpy-2.4.4-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
                "https://files.pythonhosted.org/packages/1b/2c/abcdef/"
                "numpy-2.4.4-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
                hashes={"sha256": "upstream_hash_2"},
            ),
            _json_file(
                "numpy-2.4.4-cp312-cp312-macosx_14_0_arm64.whl",
                "https://files.pythonhosted.org/packages/3d/4e/fghijk/numpy-2.4.4-cp312-cp312-macosx_14_0_arm64.whl",
                hashes={"sha256": "upstream_hash_3"},
            ),
        ],
    )

    def test_bug_scenario_old_behavior(self):
        """Without fallback: local HTML parsed as JSON returns empty,
        ALL upstream files pass through (the bug)."""
        # Simulate the OLD broken behavior
        local_files = parse_json_files(self.LOCAL_NUMPY_HTML)  # returns []
        upstream_files = parse_json_files(self.UPSTREAM_NUMPY_JSON)

        assert local_files == []  # This IS the bug
        assert len(upstream_files) == 3

        merged = merge_json_files(local_files, upstream_files)
        # All 3 upstream files pass through — no dedup happened
        assert len(merged) == 3

    def test_bug_scenario_fixed_behavior(self):
        """With parseFilesWithFallback: local HTML is converted to JSON files,
        deduplication works, local flat paths win on collision."""
        local_files = parse_files_with_fallback(self.LOCAL_NUMPY_HTML)
        upstream_files = parse_files_with_fallback(self.UPSTREAM_NUMPY_JSON)

        # Local HTML was successfully parsed via fallback
        assert len(local_files) == 2
        # Upstream JSON parsed normally
        assert len(upstream_files) == 3

        merged = merge_json_files(local_files, upstream_files)

        # 2 local + 1 upstream-only (macosx) = 3 total
        assert len(merged) == 3

        filenames = {f["filename"]: f for f in merged}

        # Local wheels use flat paths (local wins dedup)
        cp312_linux = filenames["numpy-2.4.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"]
        assert cp312_linux["url"].startswith("/packages/numpy-")
        assert "pythonhosted" not in cp312_linux["url"]

        cp311_linux = filenames["numpy-2.4.4-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"]
        assert cp311_linux["url"].startswith("/packages/numpy-")
        assert "pythonhosted" not in cp311_linux["url"]

        # Upstream-only wheel passes through with rewritten URL
        macosx = filenames["numpy-2.4.4-cp312-cp312-macosx_14_0_arm64.whl"]
        assert macosx["url"].startswith("/packages/3d/4e/")

    def test_local_only_html_as_json(self):
        """Local-only scenario (upstream failed): local HTML served as JSON."""
        local_files = parse_files_with_fallback(self.LOCAL_NUMPY_HTML)
        rendered = render_json("numpy", local_files)
        parsed = json.loads(rendered)

        assert parsed["name"] == "numpy"
        assert len(parsed["files"]) == 2
        assert all(f["url"].startswith("/packages/numpy-") for f in parsed["files"])

    def test_upstream_only_json(self):
        """Upstream-only scenario (local failed): upstream JSON returned with rewriting."""
        upstream_files = parse_files_with_fallback(self.UPSTREAM_NUMPY_JSON)
        rewritten = rewrite_json_files(upstream_files)
        rendered = render_json("numpy", rewritten)
        parsed = json.loads(rendered)

        assert len(parsed["files"]) == 3
        # All upstream URLs should be rewritten (pythonhosted prefix removed)
        for f in parsed["files"]:
            assert "pythonhosted.org" not in f["url"]
            assert f["url"].startswith("/packages/")

    def test_both_html_still_works(self):
        """When both sources return HTML, fallback parses both correctly."""
        local_files = parse_files_with_fallback(LOCAL_HTML_WRONG_VARIANT)
        upstream_files = parse_files_with_fallback(UPSTREAM_HTML_FULL)

        assert len(local_files) == 1
        assert len(upstream_files) == 3

        merged = merge_json_files(local_files, upstream_files)
        # 1 local (cp312 x86_64) + 1 upstream-only (cp311 aarch64) + 1 sdist = 3
        assert len(merged) == 3
