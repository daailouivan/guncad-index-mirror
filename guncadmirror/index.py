import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import settings, stats, webui
from .hashcache import SdHashCache

headers = {
    "User-Agent": f"GunCADMirror/1.0 (https://guncadindex.com) {requests.utils.default_user_agent()}"
}
common_claim_search_bad_value_types = [
    "repost",  # We should get this one from the OG source
    "collection",  # Playlists are irrelevant to our needs
]
common_claim_search_bad_stream_types = [
    "video",  # Just in case these sneak through
]
common_claim_search_args = {
    "stream_types": ["binary", "model", "document", "image"],
    "remove_duplicates": True,
    "fee_amount": "<=0",
    "has_source": True,
    "not_tags": ["c:members-only", "noindex", "nobot", "nobots"],
}
default_tags = [
    "2a3d",
    "3d2a",
    "3dg",
    "3dguns",
    "3dpg",
    "arewecoolyet?",
    "awcy",
    "blc",
    "fosscad",
    "gatalog",
    "guncad",
    "guncadindex",
    "hoffmantactical",
]
seen_sd_hashes = SdHashCache()


def wait_for_component(component, lbry_url="http://localhost:5279", poll_wait=1):
    """
    Waits for a LBRY component to have initialized

        component   The component to wait for
        poll_wait   How long to wait in-between each polling

    Returns False if we can't find that component, otherwise True whwen it
    finishes initializing
    """
    # Boilerplate setup, gearing up for retries n stuff
    sleepduration = 1
    session = requests.Session()
    retries = Retry(
        total=10,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    payload = {"method": "status"}
    while True:
        response = session.post(lbry_url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        result = data.get("result", {}).get("startup_status", {})
        if result.get(component, False):
            return True
        elif not component in result.keys():
            return False
        time.sleep(poll_wait)


def mirror(release, lbry_url="http://localhost:5279", store_file=False):
    """
    Mirrors a release over LBRY.

        release     The API object of a release
        lbry_url    The URL to the lbrynet daemon we should talk to

    Returns whether we downloaded the file or not.
    """
    # Boilerplate setup, gearing up for retries n stuff
    sleepduration = 1
    session = requests.Session()
    retries = Retry(
        total=10,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    logger = logging.getLogger("guncad-mirror")
    # Get some data
    claimid = release.get("id")
    author_handle = (
        release.get("channel", {})
        .get("handle", "Unknown handle")
        .replace(":", "#")
        .replace("@", "")
    )
    # If this author's in the blacklist, just bail
    for pattern in stats.extrastats["mirror_blacklisted_handles"]:
        if author_handle.startswith(pattern):
            stats.log(
                f'Channel is blacklisted: {author_handle} (matched rule "{pattern}")',
                stdout=True,
            )
            return False
    release_handle = (
        release.get("url_lbry", claimid)
        .replace("lbry://", "")
        .replace(":", "#")
        .replace("@", "")
    )
    downloaddir = f"/data/mirror/{author_handle}/{release_handle}"
    os.makedirs(downloaddir, exist_ok=True)
    with open(os.path.join(downloaddir, "meta.json"), "w") as metajson:
        json.dump(release, metajson, indent=4)
    # Get the sd_hash of the file
    payload = {
        "method": "get",
        "params": {
            "uri": release.get("url_lbry"),
            "download_directory": f"{downloaddir}",
            "timeout": 60,
        },
    }
    if release.get("sd_hash", False):
        sd_hash = release.get("sd_hash")
    else:
        logger.info("GunCAD Index didn't have sd_hash -- fetching from LBRY")
        wait_for_component("wallet")
        response = session.post(lbry_url, json=payload, headers=headers)
        response.raise_for_status()
        sd_hash = response.json().get("result", {}).get("sd_hash", None)
    # Have we seen this sd_hash before?
    returncode = False
    if not sd_hash:
        logger.error(f"Unable to get sd_hash: {response.json()}")
        return False
    elif not seen_sd_hashes.should_download(sd_hash):
        if store_file:
            logger.info(
                f"Already have sd_hash {sd_hash[:8]}, but continuing to ensure we assemble the file"
            )
        else:
            logger.info(f"Already have sd_hash {sd_hash[:8]}, skipping")
        returncode = False
    else:
        logger.info(f"Acquiring new stream described by sd_hash {sd_hash[:8]}")
        seen_sd_hashes.touch(sd_hash)
        returncode = True
    # Short-circuit if we run afoul of restrictions and intend to download the file
    if returncode:
        if settings.maxsize and release.get("size") > settings.maxsize:
            logger.info(
                f"Skipping sd_hash {sd_hash[:8]} ({release.get('name')}) since filesize {webui.humanize_bytes(release.get('size'))} greater than configured maximum {webui.humanize_bytes(settings.maxsize)}"
            )
            returncode = False
    # If we:
    # * Don't want to store the file; and
    # * Don't see a new file that we may want to mirror; then
    # We can short-circuit
    if not store_file and not returncode:
        return returncode
    # Pull the release from LBRY
    payload["params"]["save_file"] = True
    if not store_file:
        payload["params"]["download_directory"] = "/dev"
        payload["params"]["file_name"] = "null"
    wait_for_component("wallet")
    response = session.post(lbry_url, json=payload, headers=headers)
    response.raise_for_status()
    return returncode


def get_releases(url, maxpages=1000):
    """
    Get all releases from a GunCAD Index instance at the given API url.
    Add query parameters to narrow your search down

        url         The URL to hit
        maxpages    The maximum number of pages the call should traverse.
                    This * 50 is the maximum number of results you'll get.

    Yields API objects until it gets all of them
    """
    # Basic assertions
    assert type(url) == str
    # Boilerplate setup, gearing up for retries n stuff
    sleepduration = 0.15
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    logger = logging.getLogger("guncad-mirror")
    # Acquire the data
    yielded = 0
    for page in range(1, maxpages + 1):
        try:
            response = session.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            for result in data.get("results", []):
                yield (result)
                yielded += 1

            nexturl = data.get("next", False)
            if nexturl:
                time.sleep(sleepduration)
                url = nexturl
            else:
                break
        except Exception as e:
            logger.error(
                f"Error fetching releases from the configured GunCAD Index endpoint: {e}"
            )
            logger.exception(e)
            stats.log(
                f"Encountered an error when communicating with the configured Index endpoint. See logs for more."
            )
            break
    if yielded < 1:
        # If we failed to yield any objects, we've failed at something or the endpoint is misbehaving.
        # We should fall back to using LBRY data instead
        stats.extrastats["mirror_fallback_mode"] = True
        try:
            stats.log("Did not get any releases. Falling back to LBRY search")
            for release in get_releases_lbry():
                yield (release)
        except Exception as e:
            logger.error(f"Error fetching releases from LBRY: {e}")
            logger.exception(e)
            stats.log(
                f"Encountered an error when communicating with LBRY. See logs for more."
            )
            stats.log(
                "Your instance is broken and not mirroring. Please reconfigure it."
            )
    else:
        stats.extrastats["mirror_fallback_mode"] = False


def get_releases_lbry(tags=default_tags):
    """
    Get all releases from LBRY:
     * From channels with a particular set of tags (`tags`); and
     * Whose filetypes are in a particular whitelist (defined at top of file)
    This function then wraps them up and attempts to build faux-Index API
    objects out of them for consumption by later functions.

        tags        The list of tags to use when searching for channels

    Yields API objects until it gets all of them
    """
    for channelid, channeldata in channel_search(tags):
        handle = (
            channeldata.get("canonical_url", "")
            .replace("lbry://", "")
            .replace("#", ":")
        )
        for claimid, claimdata in claim_search(handle).items():
            data = claimdata.get("value", {})
            data_channel = claimdata.get("signing_channel", {})
            data_source = data.get("source", {})
            yield {
                "id": claimid,
                "synthetic_api_object": True,
                "name": data.get("title", "Unnamed release"),
                "url": claimdata.get("short_url", "")
                .replace("#", ":")
                .replace("lbry://", "https://odysee.com/"),
                "url_lbry": claimdata.get("short_url", "").replace("#", ":"),
                "size": int(data_source.get("size", 0)),
                "sd_hash": data_source.get("sd_hash", None),
                "channel": {
                    "handle": handle,
                },
            }


def claim_search(handle, maxpages=20, lbry_url="http://localhost:5279"):
    """
    Calls the claim_search method in LBRY, attempting to find all claims for a handle (@foo:b)
    Returns a dict, indexed by claim_id, of all releases
    """
    assert maxpages > 0
    claims = {}
    for i in range(1, maxpages):
        payload = {
            "method": "claim_search",
            "params": {"channel": handle, "page_size": 50, "page": i}
            | common_claim_search_args,
        }
        response = requests.post(lbry_url, json=payload)
        response.raise_for_status()
        data = response.json()
        items = data.get("result", {}).get("items", [])
        for item in items:
            if (
                item.get("value_type", "") in common_claim_search_bad_value_types
                or item.get("value", {}).get("stream_type", "")
                in common_claim_search_bad_stream_types
            ):
                continue
            claims[item["claim_id"]] = item
        if i == data.get("result", {}).get("total_pages", 1):
            break
    return claims


def channel_search(tags=[], maxqueries=5000, lbry_url="http://localhost:5279"):
    """
    Calls the claim_search method in LBRY, attempting to find all channel claims given a list
    of tags. Defaults to some standard GunCAD creator tags.
    """
    assert type(tags) == list
    oldestclaim = time.time()
    session = requests.Session()
    retries = Retry(
        total=10,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    for i in range(1, maxqueries):
        stale_oldestclaim = oldestclaim
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
        # Note: If you omit the tags parameter, we enumerate *the entire blockchain*.
        # The whole fucking thing.
        # So be really careful with that.
        if tags:
            first_payload["params"]["any_tags"] = tags
        # Note: not a magic number here. The LBRY API supports 1000 claims in a single query.
        # Thus, the 100 claims per page and the 10 iterations we do here matches as much as we
        # can without reformulating the query
        # Parallelize page requests
        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(
                    session.post,
                    lbry_url,
                    json={
                        **first_payload,
                        "params": {**first_payload["params"], "page": i},
                    },
                ): i
                for i in range(1, 11)
            }

            for future in as_completed(futures):
                response = future.result()
                response.raise_for_status()
                data = response.json()
                items = data.get("result", {}).get("items", [])
                for item in items:
                    observed_tags = item.get("value", {}).get("tags", [])
                    # Ignore tag-spammers
                    # The Odysee UI only lets you put 5 in. LBRY Desktop probably lets you do more,
                    # but if you're significantly above budget you're probably SEO spamming.
                    if len(observed_tags) > 15:
                        continue
                    yield (item["claim_id"], item)
                    if item.get("timestamp") < oldestclaim:
                        oldestclaim = item.get("timestamp")

        if stale_oldestclaim == oldestclaim:
            break
    return
