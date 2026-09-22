"""Harbor API client for artifact-scoped cache purging."""

import logging
from enum import StrEnum
from urllib.parse import quote

import requests
from pull_failures import is_digest
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("harbor-cache-recovery")

HARBOR_REQUEST_TIMEOUT_SECONDS = 10


class PurgeOutcome(StrEnum):
    """Result of one artifact delete. Only FAILED is worth failing the job over.

    ABSENT, REFERENCED and UNRESOLVED all mean "no delete happened"; none of them
    clear on retry, so letting them set the exit code would page on every run.
    """

    PURGED = "purged"
    ABSENT = "absent"
    REFERENCED = "referenced"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


class _NoCookieJar(requests.cookies.RequestsCookieJar):
    """Cookie jar that refuses to store cookies.

    Harbor sets a ``sid`` session cookie on every response. If the session
    stores it, subsequent mutation requests carry the cookie, which makes
    Harbor enforce CSRF — even though we authenticate with Basic Auth.
    Disabling cookie storage at the jar level avoids this globally.
    """

    def set_cookie(self, *_args, **_kwargs):
        return

    def extract_cookies(self, *_args, **_kwargs):
        return


def create_harbor_session(harbor_url: str, admin_password: str) -> requests.Session:
    session = requests.Session()
    session.cookies = _NoCookieJar()
    session.auth = ("admin", admin_password)
    session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    # total=1: a single retry only. Corrupted entries that don't purge this run
    # get another shot in 5 minutes — better than burning the deadline on one
    # slow repo and leaving the rest of the queue untouched.
    retry = Retry(total=1, backoff_factor=1, status_forcelist=[502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_csrf_token(session: requests.Session, harbor_url: str) -> None:
    resp = session.get(f"{harbor_url}/api/v2.0/systeminfo", timeout=HARBOR_REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    csrf_token = resp.headers.get("X-Harbor-CSRF-Token")
    if csrf_token:
        session.headers["X-Harbor-CSRF-Token"] = csrf_token


def purge_cached_artifact(
    session: requests.Session, harbor_url: str, project: str, repo_path: str, reference: str
) -> PurgeOutcome:
    """Delete one cached artifact from a Harbor proxy cache project.

    Only the failing tag or digest is evicted, so other tags of the same repository
    stay cached for their consumers.
    """
    # Harbor API requires double-encoded slashes: / → %2F → %252F. Everything else is
    # percent-encoded too, so a "?" in a repo path cannot truncate the URL and turn this
    # into a whole-repository delete.
    encoded_path = quote(repo_path, safe="").replace("%2F", "%252F")
    encoded_ref = quote(reference, safe="")
    url = f"{harbor_url}/api/v2.0/projects/{project}/repositories/{encoded_path}/artifacts/{encoded_ref}"
    target = f"{project}/{repo_path}:{reference}"
    try:
        resp = session.delete(url, timeout=HARBOR_REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            log.info("Purged: %s", target)
            return PurgeOutcome.PURGED
        if resp.status_code == 412:
            log.warning("Still cached, pinned as a child of a multi-arch index: %s", target)
            return PurgeOutcome.REFERENCED
        if resp.status_code == 404:
            if is_digest(reference):
                log.warning(
                    "Not purged, no artifact under this digest: %s. Harbor stores proxied "
                    "multi-arch indexes under a recomputed digest, so the entry may still be "
                    "cached under a different one.",
                    target,
                )
                return PurgeOutcome.UNRESOLVED
            log.info("Already gone: %s", target)
            return PurgeOutcome.ABSENT
        log.warning("Purge failed %s: HTTP %d %s", target, resp.status_code, resp.text[:200])
    except requests.RequestException:
        log.exception("Purge failed %s", target)
    return PurgeOutcome.FAILED
