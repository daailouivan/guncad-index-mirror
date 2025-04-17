import json
import logging
import os
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .hashcache import SdHashCache

headers = {
    "User-Agent": f"GunCADMirror/1.0 (https://guncadindex.com) {requests.utils.default_user_agent()}"
}
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
    if store_file:
        # If we're configured to store files, we should not skip over seen
        # sd_hashes because there's no guarantee we have the file assembled
        # This means we can also safely skip over errors, which is fun
        pass
    elif not sd_hash:
        logger.error(f"Unable to get sd_hash: {response.json()}")
        return returncode
    elif not seen_sd_hashes.should_download(sd_hash):
        logger.info(f"Already have sd_hash {sd_hash[:8]}, skipping")
        return returncode
    else:
        logger.info(f"Acquiring new stream described by sd_hash {sd_hash[:8]}")
        seen_sd_hashes.touch(sd_hash)
        returncode = True
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
        total=10,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # Acquire the data
    for page in range(1, maxpages + 1):
        response = session.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        for result in data.get("results", []):
            yield (result)

        nexturl = data.get("next", False)
        if nexturl:
            time.sleep(sleepduration)
            url = nexturl
        else:
            break
