"""Spotify web-player TOTP (Time-based One-Time Password).

``open.spotify.com`` protects its ``/api/token`` endpoint with a 6-digit TOTP
(HMAC-SHA1, 30 second period) whose secret is embedded, XOR-obfuscated, in the
web-player JavaScript bundle.

The obfuscation works like this::

    values = [ord(ch) ^ (index % 33 + 9) for index, ch in enumerate(secret)]
    key    = "".join(str(v) for v in values).encode("utf-8")

The bundle ships several secret versions; the highest one is the active
protocol version.  Version 61 is hardcoded as a fallback, and
:func:`discover` re-reads the live bundle so the module keeps working after
Spotify ships a new version.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import struct

#: Obfuscated secret for protocol version 61 (from the web-player bundle).
RAW_SECRET_V61 = ',7/*F("rLJ2oxaKL^f+E1xvP@N'

#: Protocol version implemented by this module.
TOTP_VERSION = 61

#: Where the secret list lives inside the web-player bundle.
_BUNDLE_URL = "https://open.spotifycdn.com/cdn/build/web-player/web-player.js"
_PLAYER_URL = "https://open.spotify.com/"
_BUNDLE_RE = re.compile(r"https://open\.spotifycdn\.com/cdn/build/web-player/[A-Za-z0-9_.\-]+\.js")
_SECRET_RE = re.compile(r"\{secret:(['\"])(?P<secret>(?:\\.|(?!\1).)*?)\1\s*,\s*version:(?P<version>\d+)\}")


def derive_key(raw_secret: str) -> bytes:
    """Turn an obfuscated bundle secret into the raw HMAC key."""
    values = [ord(ch) ^ (index % 33 + 9) for index, ch in enumerate(raw_secret)]
    return "".join(str(value) for value in values).encode("utf-8")


def _hotp(key: bytes, counter: int, digits: int = 6) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def totp(timestamp_ms: int, key: bytes | None = None) -> str:
    """6-digit TOTP for a millisecond timestamp."""
    counter = int(timestamp_ms // 1000 // 30)
    return _hotp(key or derive_key(RAW_SECRET_V61), counter)


def totp_for_version(timestamp_ms: int, version: int, secrets: dict[int, bytes]) -> str:
    """TOTP for an explicit protocol version."""
    key = secrets.get(version)
    if key is None:
        raise KeyError(f"no secret known for TOTP version {version}")
    return totp(timestamp_ms, key)


def parse_bundle(text: str) -> list[tuple[int, bytes]]:
    """Extract ``(version, key)`` pairs from a web-player bundle."""
    found: list[tuple[int, bytes]] = []
    for match in _SECRET_RE.finditer(text):
        raw = match.group("secret")
        # undo the JS string escaping used in the bundle
        raw = (
            raw.replace("\\\\", "\\")
            .replace("\\'", "'")
            .replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
        )
        found.append((int(match.group("version")), derive_key(raw)))
    return found


def discover(session=None, timeout: int = 20) -> dict[int, bytes]:
    """Read the live bundle and return ``{version: key}``.

    The web-player bundle is renamed on every Spotify release, so the current
    file name is read from the open.spotify.com HTML page first.  Falls back
    to the hardcoded version 61 when the bundle cannot be fetched or parsed.
    """
    import requests

    fallback = {TOTP_VERSION: derive_key(RAW_SECRET_V61)}
    if session is None:
        session = requests.Session()
        # Spotify traffic must not go through the local LLM proxy
        session.trust_env = False
    candidates: list[str] = []
    try:
        page = session.get(_PLAYER_URL, timeout=timeout)
        if page.status_code == 200:
            candidates.extend(_BUNDLE_RE.findall(page.text))
    except Exception:  # noqa: BLE001 - network is best effort
        pass
    candidates.extend(
        [
            _BUNDLE_URL,
            "https://open.spotifycdn.com/cdn/build/web-player/web-player.5a6ef385.js",
        ]
    )
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code != 200 or len(response.text) < 1000:
                continue
            pairs = parse_bundle(response.text)
            if pairs:
                found = dict(pairs)
                # never lose the hardcoded fallback
                for version, key in fallback.items():
                    found.setdefault(version, key)
                return found
        except Exception:  # noqa: BLE001 - network is best effort
            continue
    return fallback


def active_secret(secrets: dict[int, bytes]) -> tuple[int, bytes]:
    """Return the newest ``(version, key)`` pair available."""
    version = max(secrets)
    return version, secrets[version]
