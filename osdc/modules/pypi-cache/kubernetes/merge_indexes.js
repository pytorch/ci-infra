/**
 * merge_indexes.js — njs script for merging pypiserver + pypi.org indexes.
 *
 * Resolves the BY/BZ index shadowing problem: when pypiserver returns 200
 * with wrong-variant wheels, the upstream PyPI index is never consulted.
 * This script fetches both indexes in parallel via subrequests and merges
 * the link lists, so clients always see the full set of available packages.
 *
 * URL rewriting: upstream pypi.org responses contain absolute URLs
 * (https://files.pythonhosted.org/..., https://download.pytorch.org/...).
 * These are rewritten to relative paths because cache-enforcer blocks
 * direct access to those domains from runner nodes. The nginx sub_filter
 * directive does not apply to njs subrequest responses, so the rewriting
 * is done here in JavaScript.
 *
 * Loaded via: js_import merge_indexes from /etc/nginx/merge_indexes.js;
 * Called via: js_content merge_indexes.mergeSimple;
 */

var UPSTREAM_HOSTS = [
    "https://files.pythonhosted.org",
    "https://download.pytorch.org",
];

/**
 * Parse <a> tags from a PEP 503 HTML index page.
 * Returns array of {href, text, attrs} objects.
 * The regex handles href appearing anywhere in the attribute list and
 * preserves all other attributes (data-requires-python, data-dist-info, etc.).
 */
function parseHtmlLinks(body) {
    var links = [];
    var re = /<a\s([^>]*href="([^"]*)"[^>]*)>([^<]*)<\/a>/gi;
    var match;
    while ((match = re.exec(body)) !== null) {
        links.push({
            fullAttrs: match[1],
            href: match[2],
            text: match[3],
        });
    }
    return links;
}

/**
 * Parse files array from a PEP 691 JSON index response.
 * Returns the files array (or empty array on parse failure).
 */
function parseJsonFiles(body) {
    try {
        var data = JSON.parse(body);
        return data.files || [];
    } catch (e) {
        return [];
    }
}

/**
 * Extract JSON metadata (meta object and package name) from a PEP 691
 * response body. Falls back to sensible defaults on parse failure.
 */
function extractJsonMeta(body) {
    try {
        var data = JSON.parse(body);
        return {
            meta: data.meta || { "api-version": "1.1" },
            name: data.name || "",
        };
    } catch (e) {
        return { meta: { "api-version": "1.1" }, name: "" };
    }
}

/**
 * Extract filename from a URL or path.
 * Strips hash fragments (#sha256=...) and query strings.
 * e.g. "/packages/ab/cd/foo-1.0.whl#sha256=abc" -> "foo-1.0.whl"
 *      "https://files.pythonhosted.org/packages/.../foo-1.0.whl" -> "foo-1.0.whl"
 */
function extractFilename(urlOrPath) {
    var parts = urlOrPath.split("/");
    return parts[parts.length - 1].split("#")[0].split("?")[0];
}

/**
 * Rewrite upstream absolute URLs to relative paths.
 * Strips https://files.pythonhosted.org and https://download.pytorch.org
 * prefixes, matching the sub_filter behavior in nginx.conf.
 */
function rewriteUrl(url) {
    for (var i = 0; i < UPSTREAM_HOSTS.length; i++) {
        if (url.indexOf(UPSTREAM_HOSTS[i]) === 0) {
            return url.substring(UPSTREAM_HOSTS[i].length);
        }
    }
    return url;
}

/**
 * Rewrite upstream absolute URLs in the fullAttrs string of an HTML link.
 * Handles href="https://files.pythonhosted.org/..." patterns.
 */
function rewriteAttrs(attrs) {
    var result = attrs;
    for (var i = 0; i < UPSTREAM_HOSTS.length; i++) {
        result = result.split(UPSTREAM_HOSTS[i]).join("");
    }
    return result;
}

/**
 * Merge two arrays of HTML link objects. Deduplicates by filename.
 * Local links win on collision (local wheel is faster to serve).
 */
function mergeHtmlLinks(localLinks, upstreamLinks) {
    var seen = {};
    var merged = [];

    for (var i = 0; i < localLinks.length; i++) {
        var filename = extractFilename(localLinks[i].href);
        if (filename) {
            seen[filename] = true;
        }
        merged.push(localLinks[i]);
    }

    for (var j = 0; j < upstreamLinks.length; j++) {
        var link = upstreamLinks[j];
        var fname = extractFilename(link.href);
        if (fname && seen[fname]) {
            continue;
        }
        if (fname) {
            seen[fname] = true;
        }
        merged.push({
            fullAttrs: rewriteAttrs(link.fullAttrs),
            href: rewriteUrl(link.href),
            text: link.text,
        });
    }

    return merged;
}

/**
 * Merge two arrays of PEP 691 JSON file objects. Deduplicates by filename.
 * Local files win on collision.
 */
function mergeJsonFiles(localFiles, upstreamFiles) {
    var seen = {};
    var merged = [];

    for (var i = 0; i < localFiles.length; i++) {
        var filename = localFiles[i].filename;
        if (filename) {
            seen[filename] = true;
        }
        merged.push(localFiles[i]);
    }

    for (var j = 0; j < upstreamFiles.length; j++) {
        var file = upstreamFiles[j];
        if (file.filename && seen[file.filename]) {
            continue;
        }
        if (file.filename) {
            seen[file.filename] = true;
        }
        var rewritten = {};
        for (var key in file) {
            rewritten[key] = file[key];
        }
        if (rewritten.url) {
            rewritten.url = rewriteUrl(rewritten.url);
        }
        merged.push(rewritten);
    }

    return merged;
}

/**
 * Convert HTML link objects to PEP 691 JSON file objects.
 * Used when pypiserver returns HTML but the client requested JSON format.
 * pypiserver v2.x does not support PEP 691 — it always returns HTML
 * regardless of the Accept header. This function bridges the format gap
 * so HTML responses can participate in JSON-format merges.
 */
function htmlLinksToJsonFiles(links) {
    var files = [];
    for (var i = 0; i < links.length; i++) {
        var filename = extractFilename(links[i].href);
        if (filename) {
            var entry = {
                filename: filename,
                url: links[i].href,
                hashes: {},
            };
            // Extract hash from URL fragment (#sha256=..., #md5=..., etc.)
            var hashIdx = links[i].href.indexOf("#");
            if (hashIdx !== -1) {
                var fragment = links[i].href.substring(hashIdx + 1);
                var eqIdx = fragment.indexOf("=");
                if (eqIdx !== -1) {
                    var algo = fragment.substring(0, eqIdx);
                    var digest = fragment.substring(eqIdx + 1);
                    if (algo && digest) {
                        entry.hashes[algo] = digest;
                    }
                }
            }
            files.push(entry);
        }
    }
    return files;
}

/**
 * Attempt to parse a response body as PEP 691 JSON; if JSON.parse fails
 * (indicating the response is HTML, not JSON), fall back to parsing as
 * HTML and converting to JSON file objects. This handles backends (like
 * pypiserver v2.x) that return HTML regardless of the Accept header.
 *
 * If JSON.parse succeeds, the result is trusted even if files is empty
 * (a valid JSON response with no files is not an HTML fallback case).
 */
function parseFilesWithFallback(responseText) {
    if (!responseText || responseText.length === 0) {
        return [];
    }
    try {
        var data = JSON.parse(responseText);
        return data.files || [];
    } catch (e) {
        return htmlLinksToJsonFiles(parseHtmlLinks(responseText));
    }
}

/**
 * Render a PEP 503 HTML index page from an array of link objects.
 */
function renderHtml(packageName, links) {
    var safeName = escapeHtml(packageName);
    var html = "<!DOCTYPE html>\n<html><head><title>Links for " +
        safeName + "</title></head>\n<body>\n<h1>Links for " +
        safeName + "</h1>\n";
    for (var i = 0; i < links.length; i++) {
        var link = links[i];
        html += "<a " + link.fullAttrs + ">" + link.text + "</a>\n";
    }
    html += "</body>\n</html>\n";
    return html;
}

/**
 * Render a PEP 691 JSON index response from metadata and a files array.
 */
function renderJson(meta, name, files) {
    return JSON.stringify({
        meta: meta,
        name: name,
        files: files,
    });
}

/**
 * Rewrite all upstream URLs in an HTML response body.
 * Applied when returning upstream-only responses (local non-200).
 */
function rewriteHtmlBody(body) {
    var result = body;
    for (var i = 0; i < UPSTREAM_HOSTS.length; i++) {
        result = result.split(UPSTREAM_HOSTS[i]).join("");
    }
    return result;
}

/**
 * Rewrite url fields in a PEP 691 JSON files array.
 * Returns a new array with rewritten URLs.
 */
function rewriteJsonFileUrls(files) {
    var rewritten = [];
    for (var i = 0; i < files.length; i++) {
        var file = {};
        for (var key in files[i]) {
            file[key] = files[i][key];
        }
        if (file.url) {
            file.url = rewriteUrl(file.url);
        }
        rewritten.push(file);
    }
    return rewritten;
}

/**
 * Check if the client wants JSON (PEP 691) or HTML (PEP 503).
 * Handles quality factors like application/vnd.pypi.simple.v1+json;q=0.9.
 */
function wantsJson(r) {
    var accept = r.headersIn["Accept"] || "";
    return accept.indexOf("application/vnd.pypi.simple.v1+json") !== -1;
}

/**
 * Check if a subrequest response is usable (status 200 with non-empty body).
 */
function isOk(resp) {
    return resp.status === 200 &&
        resp.responseText &&
        resp.responseText.length > 0;
}

/**
 * Check if this is a root /simple/ request (package listing).
 * Root listing is passed through to upstream since pypiserver's root
 * listing is incomplete by design.
 */
function isRootSimple(uri) {
    return uri === "/simple/" || uri === "/simple";
}

/**
 * Extract the package name from the request URI.
 * e.g. "/simple/cffi/" -> "cffi"
 */
function packageFromUri(uri) {
    return uri.replace(/^\/simple\//, "").replace(/\/$/, "");
}

/**
 * Escape HTML special characters to prevent XSS.
 * Applied to packageName (derived from client URI) before embedding in HTML.
 */
function escapeHtml(s) {
    return s.split("&").join("&amp;")
        .split("<").join("&lt;")
        .split(">").join("&gt;")
        .split('"').join("&quot;");
}

/**
 * Set standard response headers for merged index responses.
 */
function setHeaders(r, useJson) {
    if (useJson) {
        r.headersOut["Content-Type"] = "application/vnd.pypi.simple.v1+json";
    } else {
        r.headersOut["Content-Type"] = "text/html; charset=utf-8";
    }
    r.headersOut["X-Merged-Index"] = "true";
}

/**
 * Main handler: merge local pypiserver + upstream pypi.org indexes.
 * Called by js_content in the /simple/ location.
 */
function mergeSimple(r) {
    var localUri = "/_internal/local" + r.uri;
    var upstreamUri = "/_internal/upstream" + r.uri;
    var useJson = wantsJson(r);

    // Root /simple/ — prefer upstream (complete package listing).
    // Fall back to local if upstream fails.
    if (isRootSimple(r.uri)) {
        Promise.all([
            r.subrequest(localUri),
            r.subrequest(upstreamUri),
        ]).then(function (responses) {
            var localResp = responses[0];
            var upstreamResp = responses[1];

            setHeaders(r, useJson);

            if (isOk(upstreamResp)) {
                r.return(200, upstreamResp.responseText);
            } else if (isOk(localResp)) {
                r.return(200, localResp.responseText);
            } else {
                r.return(502, "No upstream available\n");
            }
        }).catch(function () {
            r.return(502, "Index merge error\n");
        });
        return;
    }

    // Package-specific request — merge local + upstream indexes.
    Promise.all([
        r.subrequest(localUri),
        r.subrequest(upstreamUri),
    ]).then(function (responses) {
        var localResp = responses[0];
        var upstreamResp = responses[1];

        var localOk = isOk(localResp);
        var upstreamOk = isOk(upstreamResp);

        // Both failed — return 404
        if (!localOk && !upstreamOk) {
            r.return(404, "Not Found\n");
            return;
        }

        setHeaders(r, useJson);
        var pkg = packageFromUri(r.uri);

        // Only local — return as-is (convert to JSON if client wants JSON)
        if (localOk && !upstreamOk) {
            if (useJson) {
                var localOnly = parseFilesWithFallback(localResp.responseText);
                r.return(200, renderJson(
                    { "api-version": "1.1" }, pkg, localOnly));
            } else {
                r.return(200, localResp.responseText);
            }
            return;
        }

        // Only upstream — return with URL rewriting
        if (!localOk && upstreamOk) {
            if (useJson) {
                var files = parseFilesWithFallback(upstreamResp.responseText);
                var rewritten = rewriteJsonFileUrls(files);
                var meta = extractJsonMeta(upstreamResp.responseText);
                r.return(200, renderJson(meta.meta, meta.name || pkg, rewritten));
            } else {
                r.return(200, rewriteHtmlBody(upstreamResp.responseText));
            }
            return;
        }

        // Both OK — merge and deduplicate
        if (useJson) {
            var localFiles = parseFilesWithFallback(localResp.responseText);
            var upstreamFiles = parseFilesWithFallback(upstreamResp.responseText);
            var merged = mergeJsonFiles(localFiles, upstreamFiles);
            var jsonMeta = extractJsonMeta(upstreamResp.responseText);
            if (!jsonMeta.name) {
                jsonMeta = extractJsonMeta(localResp.responseText);
            }
            r.return(200, renderJson(jsonMeta.meta, jsonMeta.name || pkg, merged));
        } else {
            var localLinks = parseHtmlLinks(localResp.responseText);
            var upstreamLinks = parseHtmlLinks(upstreamResp.responseText);
            var mergedLinks = mergeHtmlLinks(localLinks, upstreamLinks);
            r.return(200, renderHtml(pkg, mergedLinks));
        }
    }).catch(function () {
        r.return(502, "Index merge error\n");
    });
}

export default { mergeSimple };
