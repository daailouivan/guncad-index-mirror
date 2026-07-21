#!/usr/bin/env python3
"""Apply Mirror's private qBittorrent control-plane configuration."""

from __future__ import annotations

import base64
import configparser
import hashlib
import os
from pathlib import Path

CONFIG_PATH = Path("/config/qBittorrent/config/qBittorrent.conf")
PASSWORD_ITERATIONS = 100_000


def required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} must be set")
    if "\n" in value or "\r" in value:
        raise SystemExit(f"{name} must not contain newlines")
    return value


def password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha512",
        password.encode(),
        salt,
        PASSWORD_ITERATIONS,
    )
    encoded_salt = base64.b64encode(salt).decode()
    encoded_digest = base64.b64encode(digest).decode()
    return f'"@ByteArray({encoded_salt}:{encoded_digest})"'


def environment_boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"{name} must be a boolean value")


def configure() -> None:
    username = required_environment("QBITTORRENT_USERNAME")
    password = required_environment("QBITTORRENT_PASSWORD")
    host_header_validation = environment_boolean(
        "QBITTORRENT_HOST_HEADER_VALIDATION", True
    )
    try:
        webui_port = int(os.environ.get("QBT_WEBUI_PORT", "8080"))
        torrenting_port = int(os.environ.get("QBT_TORRENTING_PORT", "6881"))
    except ValueError as error:
        raise SystemExit("qBittorrent ports must be integers") from error
    if not 1 <= webui_port <= 65535 or not 1 <= torrenting_port <= 65535:
        raise SystemExit("qBittorrent ports must be between 1 and 65535")

    config = configparser.RawConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    if CONFIG_PATH.exists():
        config.read(CONFIG_PATH)
    for section in ("BitTorrent", "LegalNotice", "Meta", "Preferences"):
        if not config.has_section(section):
            config.add_section(section)

    config["BitTorrent"].update(
        {
            r"Session\DefaultSavePath": "/downloads",
            r"Session\Port": str(torrenting_port),
            r"Session\QueueingSystemEnabled": "false",
        }
    )
    config["LegalNotice"]["Accepted"] = "true"
    config["Meta"]["MigrationVersion"] = "9999"
    config["Preferences"].update(
        {
            r"WebUI\Address": "*",
            r"WebUI\AuthSubnetWhitelistEnabled": "false",
            r"WebUI\CSRFProtection": "true",
            r"WebUI\ClickjackingProtection": "true",
            r"WebUI\HostHeaderValidation": str(host_header_validation).lower(),
            r"WebUI\LocalHostAuth": "true",
            r"WebUI\Password_PBKDF2": password_hash(password),
            r"WebUI\Port": str(webui_port),
            r"WebUI\SecureCookie": "false",
            r"WebUI\ServerDomains": "qbittorrent",
            r"WebUI\UseUPnP": "false",
            r"WebUI\Username": username,
        }
    )

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        config.write(output, space_around_delimiters=False)
    temporary.chmod(0o600)
    temporary.replace(CONFIG_PATH)


if __name__ == "__main__":
    configure()
    os.execv("/entrypoint.sh", ["/entrypoint.sh", *os.sys.argv[1:]])
