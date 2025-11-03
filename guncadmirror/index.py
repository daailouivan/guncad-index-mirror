import json
import logging
import os
import time
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import settings, stats, webui
from .hashcache import SdHashCache


# -----------------------------------------------------------------------------
# Constants / globals
# -----------------------------------------------------------------------------
headers = {
    "User-Agent": f"GunCADMirror/1.0 (https://guncadindex.com) {requests.utils.default_user_agent()}"
}

common_claim_search_bad_value_types = ["repost", "collection"]
common_claim_search_bad_stream_types = ["video"]

common_claim_search_args = {
    "stream_types": ["binary", "model", "document", "image"],
    "remove_duplicates": True,
    "fee_amount": "<=0",
    "has_source": True,
    "not_tags": ["c:members-only", "noindex", "nobot", "nobots"],
}

default_tags = [
    "2a3d", "3d2a", "3dg", "3dguns", "3dpg", "arewecoolyet?", "awcy",
    "blc", "fosscad", "gatalog", "guncad", "guncadindex", "hoffmantactical",
]

seen_sd_hashes = SdHashCache()
SPV_FAIL_COUNT = 0  # tracks consecutive ResolveTimeoutError/timeout chains


# -----------------------------------------------------------------------------
# HTTP helpers
# -----------------------------------------------------------------------------
def make_session(total_retries=5, backoff_factor=2.0):
    session = requests.Session()
    retries = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def resilient_post(session, url, **kwargs):
    logger = logging.getLogger("guncad-mirror")
    for attempt in range(5):
        try:
            return session.post(url, **kwargs)
        except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            wait = 2 ** attempt
            logger.warning(
                f"[resilient_post] POST {url} failed ({e}); retrying in {wait}s... (attempt {attempt+1}/5)"
            )
            time.sleep(wait)
    raise RuntimeError(f"[resilient_post] Failed to POST to {url} after 5 attempts.")


# -----------------------------------------------------------------------------
# LBRY readiness + verification
# -----------------------------------------------------------------------------
def stream_is_complete(session, lbry_url, sd_hash):
    logger = logging.getLogger("guncad-mirror")
    payload = {"method": "file_list", "params": {"sd_hash": sd_hash}}
    try:
        resp = session.post(lbry_url, json=payload, headers=headers, timeout=(5, 30))
        resp.raise_for_status()
        result = resp.json().get("result", {})
        items = result.get("items", []) if isinstance(result, dict) else []
        if not items:
            logger.info(f"[verify] No local file entries for {sd_hash[:8]} — not yet assembled.")
            return False

        entry = items[0]
        completed = entry.get("blobs_completed", 0)
        total = entry.get("blobs_in_stream", 0)
        remaining = entry.get("blobs_remaining", 0)
        file_name = entry.get("file_name", "unknown")

        logger.info(
            f"[verify] Stream {sd_hash[:8]} ({file_name}) "
            f"blobs_completed={completed}, blobs_in_stream={total}, blobs_remaining={remaining}"
        )
    except Exception as e:
        logger.warning(f"[verify] file_list check failed for {sd_hash[:8]}: {e}")
        return False

    try:
        r = session.post(lbry_url, json={"method": "blob_list", "params": {}},
                         headers=headers, timeout=(5, 30))
        r.raise_for_status()
        blob_items = r.json().get("result", {}).get("items", [])
        logger.info(f"[verify] Local blob inventory: {len(blob_items)} blobs on disk.")
    except Exception as e:
        logger.warning(f"[verify] blob_list check failed for {sd_hash[:8]}: {e}")

    if remaining == 0 and total > 0:
        logger.info(f"[verify] Stream {sd_hash[:8]} already fully assembled — skipping download.")
        return True
    logger.info(f"[verify] Stream {sd_hash[:8]} incomplete — proceeding to download.")
    return False


def wait_for_component(component, lbry_url="http://localhost:5279", poll_wait=1, max_attempts=60):
    """
    Poll lbrynet 'status' until a particular startup_status component is true, or it’s missing.
    Returns True if the component initializes; False if the daemon reports no such component or timeout.
    """
    logger = logging.getLogger("guncad-mirror")
    session = make_session()
    payload = {"method": "status"}

    for attempt in range(max_attempts):
        try:
            response = session.post(lbry_url, json=payload, headers=headers, timeout=(5, 15))
            response.raise_for_status()
            data = response.json()
            result = data.get("result", {}).get("startup_status", {})
            if result.get(component, False):
                logger.info(f"[wait_for_component] '{component}' ready after {attempt+1} polls.")
                return True
            elif component not in result:
                logger.info(f"[wait_for_component] '{component}' not advertised in startup_status.")
                return False
        except (requests.ConnectionError, requests.Timeout) as e:
            logger.debug(f"[wait_for_component] Connection issue ({e}) while waiting for '{component}'")
        time.sleep(poll_wait)

    logger.warning(f"[wait_for_component] Timeout waiting for component '{component}' to initialize after {max_attempts} tries.")
    return False


def wait_for_lbry_ready(lbry_url="http://localhost:5279"):
    """
    Wait for LBRY to have its wallet and stream_manager initialized (in that order).
    This mirrors the expectations from __main__.py before attempting to mirror/download.
    """
    logger = logging.getLogger("guncad-mirror")
    logger.info("[init] Waiting for LBRY components to initialize (wallet, stream_manager)...")
    wallet_ok = wait_for_component("wallet", lbry_url=lbry_url, poll_wait=1, max_attempts=60)
    if wallet_ok:
        logger.info("[init] Component 'wallet' ready.")
    else:
        logger.warning("[init] 'wallet' not ready — continuing anyway (daemon may still resolve).")

    sm_ok = wait_for_component("stream_manager", lbry_url=lbry_url, poll_wait=1, max_attempts=60)
    if sm_ok:
        logger.info("[init] Component 'stream_manager' ready.")
    else:
        logger.warning("[init] 'stream_manager' not ready — continuing anyway (will rely on retries).")


# -----------------------------------------------------------------------------
# Mirroring core
# -----------------------------------------------------------------------------
def mirror(release, lbry_url="http://localhost:5279", store_file=False):
    """
    Attempt to mirror a single release. Performs its own 3 internal attempts with backoff.
    Returns True on success, False otherwise. Does NOT kill the process; supervisor handles totals.
    """
    global SPV_FAIL_COUNT
    logger = logging.getLogger("guncad-mirror")
    session = make_session()

    claimid = release.get("id")
    author_handle = release.get("channel", {}).get("handle", "Unknown").replace(":", "#").replace("@", "")

    # blacklist check
    for pattern in stats.extrastats["mirror_blacklisted_handles"]:
        if pattern and author_handle.startswith(pattern):
            logger.info(f"[mirror] Channel blacklisted: {author_handle} (rule '{pattern}')")
            return False

    # assemble paths, write meta
    release_handle = release.get("url_lbry", claimid).replace("lbry://", "").replace(":", "#").replace("@", "")
    downloaddir = f"/data/mirror/{author_handle}/{release_handle}"
    os.makedirs(downloaddir, exist_ok=True)
    with open(os.path.join(downloaddir, "meta.json"), "w") as metajson:
        json.dump(release, metajson, indent=4)

    # build get payload
    payload = {
        "method": "get",
        "params": {
            "uri": release.get("url_lbry"),
            "download_directory": f"{downloaddir}",
            "timeout": 180,  # allow daemon time to resolve
        },
    }

    # discover sd_hash if missing
    sd_hash = release.get("sd_hash")
    if not sd_hash:
        logger.info("[mirror] Missing sd_hash; asking LBRY daemon via 'get'.")
        wait_for_component("wallet")
        response = resilient_post(session, lbry_url, json=payload, headers=headers, timeout=(5, 60))
        sd_hash = response.json().get("result", {}).get("sd_hash")

    if not sd_hash:
        logger.error("[mirror] Unable to resolve sd_hash from daemon response.")
        return False

    # dedupe/cache gate
    if not seen_sd_hashes.should_download(sd_hash):
        if store_file:
            logger.info(f"[mirror] sd_hash {sd_hash[:8]} known; verifying completeness before skipping...")
        else:
            logger.info(f"[mirror] sd_hash {sd_hash[:8]} known; skipping.")
            return False
    else:
        logger.info(f"[mirror] Acquiring new stream {sd_hash[:8]}")
        seen_sd_hashes.touch(sd_hash)

    # size gate
    if settings.maxsize and release.get("size", 0) > settings.maxsize:
        logger.info(f"[mirror] Skipping {sd_hash[:8]} (exceeds size limit).")
        return False

    # short-circuit if already complete
    if stream_is_complete(session, lbry_url, sd_hash):
        logger.info(f"[mirror] Stream {sd_hash[:8]} verified complete — skipping download.")
        return False

    # set file write flags
    payload["params"]["save_file"] = True
    if not store_file:
        payload["params"]["download_directory"] = "/dev"
        payload["params"]["file_name"] = "null"

    # ensure wallet is (likely) up
    wait_for_component("wallet")

    # try up to 3 internal attempts for this release
    for attempt in range(3):
        try:
            logger.info(f"[mirror][attempt {attempt+1}/3] Downloading {release.get('url_lbry')}")
            response = resilient_post(session, lbry_url, json=payload, headers=headers, timeout=(5, 120))
            response.raise_for_status()
            j = response.json()

            # explicit daemon error bubble-up
            if "error" in j:
                err_str = str(j["error"])
                if "ResolveTimeoutError" in err_str:
                    raise RuntimeError("ResolveTimeoutError from LBRY daemon")
                else:
                    raise RuntimeError(f"LBRY daemon returned error: {err_str}")

            logger.info(f"[mirror][success] Completed {release.get('url_lbry')}")
            SPV_FAIL_COUNT = 0
            return True

        except requests.exceptions.Timeout:
            wait = 5 * (2 ** attempt)
            logger.warning(f"[mirror][timeout] {release.get('url_lbry')} (attempt {attempt+1}/3); retrying in {wait}s...")
            time.sleep(wait)

        except RuntimeError as e:
            if "ResolveTimeoutError" in str(e):
                wait = 5 * (2 ** attempt)
                logger.warning(f"[mirror][ResolveTimeoutError] {release.get('url_lbry')} (attempt {attempt+1}/3); retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.warning(f"[mirror][daemon-error] {e} — aborting this release early.")
                break

        except Exception as e:
            logger.warning(f"[mirror][unexpected] {type(e).__name__}: {e} — aborting this release early.")
            break

    # internal 3 tries exhausted for this release
    logger.warning(f"[mirror][fail] {release.get('url_lbry')} failed after 3 tries.")
    return False


# -----------------------------------------------------------------------------
# Supervisor wrapper with controlled recovery (total 15 tries)
# -----------------------------------------------------------------------------
def mirror_with_recovery(release, lbry_url="http://localhost:5279", store_file=False):
    """
    Supervises mirror() with up to 15 total attempts.
    Every 5 consecutive failures, rebuild a fresh HTTP session context
    (by simply calling mirror() anew) to shake out bad state.
    On total exhaustion, graceful SIGTERM.
    """
    logger = logging.getLogger("guncad-mirror")
    global SPV_FAIL_COUNT

    max_total_attempts = 15
    recovery_interval = 5
    successful = False

    for attempt in range(1, max_total_attempts + 1):
        logger.info(f"[supervisor][attempt {attempt}/{max_total_attempts}] Starting mirror() for {release.get('url_lbry')}")
        ok = mirror(release, lbry_url, store_file)
        if ok:
            logger.info(f"[supervisor][success] {release.get('url_lbry')} succeeded on attempt {attempt}.")
            SPV_FAIL_COUNT = 0
            successful = True
            break

        SPV_FAIL_COUNT += 1
        logger.warning(f"[supervisor][fail {SPV_FAIL_COUNT}] mirror() failed for {release.get('url_lbry')}")

        # Every 5 failures, rebuild context by invoking mirror() again (fresh session inside)
        if attempt % recovery_interval == 0:
            logger.warning(f"[supervisor][reset] {recovery_interval} consecutive failures; rebuilding session and re-invoking mirror().")
            time.sleep(10)
            try:
                ok = mirror(release, lbry_url=lbry_url, store_file=store_file)
                if ok:
                    logger.info(f"[supervisor][recovery-success] {release.get('url_lbry')} recovered after rebuild.")
                    SPV_FAIL_COUNT = 0
                    successful = True
                    break
                else:
                    logger.warning("[supervisor][recovery-fail] mirror() still failing after rebuild.")
            except Exception as e:
                logger.error(f"[supervisor][recovery-exception] {type(e).__name__}: {e}")

        wait_time = min(60, 5 * attempt)
        logger.info(f"[supervisor][wait] Sleeping {wait_time}s before next attempt.")
        time.sleep(wait_time)

    if not successful:
        logger.error(f"[supervisor][fatal] mirror() failed {max_total_attempts} times. Initiating graceful shutdown.")
        os.kill(os.getpid(), signal.SIGTERM)
        return False

    return True


# -----------------------------------------------------------------------------
# Release enumeration
# -----------------------------------------------------------------------------
def get_releases(url, maxpages=1000):
    """
    Enumerate releases from the GunCAD Index API, following pagination.
    Falls back to LBRY search if zero results.
    """
    assert isinstance(url, str)
    session = make_session()
    logger = logging.getLogger("guncad-mirror")

    yielded = 0
    for _ in range(1, maxpages + 1):
        try:
            response = session.get(url, headers=headers, timeout=(5, 30))
            response.raise_for_status()
            data = response.json()

            for result in data.get("results", []):
                yield result
                yielded += 1

            nexturl = data.get("next")
            if nexturl:
                time.sleep(0.15)
                url = nexturl
            else:
                break
        except Exception as e:
            logger.exception(f"[get_releases] Error fetching releases from Index: {e}")
            stats.log("Encountered an error with the configured Index endpoint.")
            break

    if yielded < 1:
        stats.extrastats["mirror_fallback_mode"] = True
        try:
            stats.log("No releases yielded; falling back to LBRY search.")
            for release in get_releases_lbry():
                yield release
        except Exception as e:
            logger.exception(f"[get_releases] Error fetching releases from LBRY: {e}")
            stats.log("Your instance is broken and not mirroring.")
    else:
        stats.extrastats["mirror_fallback_mode"] = False


def get_releases_lbry(tags=default_tags):
    """
    Build faux-Index release objects by enumerating claims from tagged channels via LBRY.
    """
    for channelid, channeldata in channel_search(tags):
        handle = channeldata.get("canonical_url", "").replace("lbry://", "").replace("#", ":")
        # blacklist
        is_blacklisted = False
        for pattern in stats.extrastats["mirror_blacklisted_handles"]:
            if pattern and handle.replace("@", "").replace(":", "#").startswith(pattern):
                stats.log(f'Skipping blacklisted channel: {handle} (rule "{pattern}")', stdout=True)
                is_blacklisted = True
                break
        if is_blacklisted:
            continue

        for claimid, claimdata in claim_search(handle).items():
            data = claimdata.get("value", {})
            data_source = data.get("source", {})
            yield {
                "id": claimid,
                "synthetic_api_object": True,
                "name": data.get("title", "Unnamed release"),
                "url": claimdata.get("short_url", "").replace("#", ":").replace("lbry://", "https://odysee.com/"),
                "url_lbry": claimdata.get("short_url", "").replace("#", ":"),
                "size": int(data_source.get("size", 0)),
                "sd_hash": data_source.get("sd_hash"),
                "channel": {"handle": handle},
            }


def claim_search(handle, maxpages=20, lbry_url="http://localhost:5279"):
    """
    Query all claims for a channel handle using LBRY claim_search.
    Returns dict keyed by claim_id.
    """
    assert maxpages > 0
    claims = {}
    for i in range(1, maxpages):
        payload = {"method": "claim_search",
                   "params": {"channel": handle, "page_size": 50, "page": i} | common_claim_search_args}
        response = requests.post(lbry_url, json=payload, timeout=(5, 30))
        response.raise_for_status()
        data = response.json()
        for item in data.get("result", {}).get("items", []):
            if (item.get("value_type") in common_claim_search_bad_value_types or
                item.get("value", {}).get("stream_type") in common_claim_search_bad_stream_types):
                continue
            claims[item["claim_id"]] = item
        if i == data.get("result", {}).get("total_pages", 1):
            break
    return claims


def channel_search(tags=None, maxqueries=5000, lbry_url="http://localhost:5279"):
    """
    Enumerate channels by tags, paging in parallel (10 pages per window).
    """
    if tags is None:
        tags = []
    assert isinstance(tags, list)

    oldestclaim = time.time()

    def fetch_page(page, first_payload):
        with make_session() as s:
            resp = s.post(lbry_url,
                          json={**first_payload, "params": {**first_payload["params"], "page": page}},
                          timeout=(5, 30))
            resp.raise_for_status()
            return resp.json()

    for _ in range(1, maxqueries):
        stale_oldest = oldestclaim
        first_payload = {
            "method": "claim_search",
            "params": {
                "remove_duplicates": True,
                "order_by": "timestamp",
                "timestamp": f"<{oldestclaim}",
                "claim_type": "channel",
                "page_size": 100,
            },
        }
        if tags:
            first_payload["params"]["any_tags"] = tags

        with ThreadPoolExecutor() as executor:
            futures = {executor.submit(fetch_page, i, first_payload): i for i in range(1, 11)}
            for future in as_completed(futures):
                data = future.result()
                items = data.get("result", {}).get("items", [])
                for item in items:
                    tags_here = item.get("value", {}).get("tags", [])
                    # ignore extreme tag-spam
                    if len(tags_here) > 15:
                        continue
                    yield (item["claim_id"], item)
                    if item.get("timestamp") < oldestclaim:
                        oldestclaim = item["timestamp"]

        if stale_oldest == oldestclaim:
            break
