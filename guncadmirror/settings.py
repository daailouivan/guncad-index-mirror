import logging
import os
import random

from . import stats

# Should we assemble files from blobs into... well, the files.
assemble_files = False
# Should we enable the web UI?
enable_webui = False
# What is our target API endpoint
endpoint = "https://guncadindex.com/api/releases/?format=json&limit=100"
# What's the biggest file we'll accept from the Index?
maxsize = 10737418240 # 10GB in B

# Generate a cachebuster to use for this session
cachebuster = "?cb=" + "".join(str(random.randint(0, 9)) for _ in range(16))


def str_to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    result = value.strip().lower() in ("1", "true", "t", "yes", "on", "enabled")
    return result

def get_envvar_bool(variable, message=None, messagewhen=True) -> bool:
    logger = logging.getLogger("guncad-mirror")
    result = str_to_bool(os.getenv(variable, False))
    if message and (result == messagewhen):
        logger.info(f"{message} ({variable}={result})")
    return result


def get_envvar_string(variable, default=None, message=None) -> str:
    logger = logging.getLogger("guncad-mirror")
    result = os.getenv(variable, default or "")
    if message:
        logger.info(f'{message}: "{result}" ({variable})')
    return result


def get_envvar_int(variable, default=None, message=None) -> str:
    logger = logging.getLogger("guncad-mirror")
    result = int(os.getenv(variable, default or "0"))
    if message:
        logger.info(f'{message}: "{result}" ({variable})')
    return result


def parse_environment():
    global assemble_files, enable_webui, endpoint
    logger = logging.getLogger("guncad-mirror")
    assemble_files = get_envvar_bool(
        "MIRROR_ASSEMBLE_FILES",
        message="MIRROR_ASSEMBLE_FILES is set -- we will mirror WHOLE FILES. Note that this uses TWICE AS MUCH DISK as not doing so.",
    )
    stats.extrastats["mirror_assemble_files"] = assemble_files

    enable_webui = get_envvar_bool(
        "MIRROR_ENABLE_WEBUI",
        message="MIRROR_ENABLE_WEBUI is set -- view stats on :8081 (or whatever port you forwarded that to)",
    )
    stats.extrastats["mirror_enable_webui"] = enable_webui

    endpoint = get_envvar_string(
        "MIRROR_API_ENDPOINT",
        default="https://guncadindex.com/api/releases/?format=json&limit=100",
        message="Using API endpoint",
    )
    stats.extrastats["mirror_api_endpoint"] = endpoint

    maxsize = get_envvar_int(
        "MIRROR_RELEASE_MAX_SIZE",
        default=10737418240,
        message="Accepting releases up to size"
    )
    stats.extrastats["mirror_release_max_size"] = maxsize

    stats.log("Updated settings from environment variables", stdout=True)
    return
