import json
import os
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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
        response = session.post(lbry_url, json=payload)
        response.raise_for_status()
        data = response.json()
        result = data.get("result", {}).get("startup_status", {})
        if result.get(component, False):
            return True
        elif not component in result.keys():
            return False
        time.sleep(poll_wait)


def mirror(release, lbry_url="http://localhost:5279"):
    """
    Mirrors a release over LBRY.

        release     The API object of a release
        lbry_url    The URL to the lbrynet daemon we should talk to

    Returns nothing.
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
    # Get some data
    claimid = release.get("id")
    downloaddir = f"/data/mirror/{claimid[:2]}/{claimid[2:]}"
    os.makedirs(downloaddir, exist_ok=True)
    with open(os.path.join(downloaddir, "meta.json"), "w") as metajson:
        json.dump(release, metajson, indent=4)
    # Set up to talk to LBRY
    payload = {
        "method": "get",
        "params": {
            "uri": release.get("url_lbry"),
            #"download_directory": f"{downloaddir}",
            "download_directory": "/dev",
            "file_name": "null",
            "save_file": True,
            "timeout": 60,
        },
    }
    response = session.post(lbry_url, json=payload)
    response.raise_for_status()
    return response.json()


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
    # Acquire the data
    for page in range(1, maxpages + 1):
        response = session.get(url)
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
