"""Read Spotify session cookies from a Chromium-based browser on macOS.

Chrome/Edge on macOS encrypt cookie values with AES-128-CBC using a key derived
from the "Safe Storage" keychain item (PBKDF2-HMAC-SHA1, 1003 iterations, salt
"saltysalt").  Values are prefixed with "v10" (or "v11") and the first 32 bytes
of the decrypted payload are the SHA-256 of the cookie's host key.

Only the cookies of the profile that actually holds the Spotify session are
returned, and nothing ever leaves this machine.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: Browser profile roots on macOS.
BROWSER_ROOTS: dict[str, str] = {
    "edge": "~/Library/Application Support/Microsoft Edge",
    "chrome": "~/Library/Application Support/Google/Chrome",
    "brave": "~/Library/Application Support/BraveSoftware/Brave-Browser",
    "vivaldi": "~/Library/Application Support/Vivaldi",
    "opera": "~/Library/Application Support/com.operasoftware.Opera",
}

#: Keychain service holding the "Safe Storage" password per browser.
KEYCHAIN_SERVICES: dict[str, str] = {
    "edge": "Microsoft Edge Safe Storage",
    "chrome": "Chrome Safe Storage",
    "brave": "Brave Safe Storage",
    "vivaldi": "Vivaldi Safe Storage",
    "opera": "Opera Safe Storage",
}

#: Cookies the Spotify web player needs to issue a user access token.
REQUIRED_COOKIES = ("sp_t", "sp_key", "sp_dc")


class CookieError(RuntimeError):
    """Raised when the browser cookies cannot be read or the session is missing."""


def _keychain_password(service: str) -> str | None:
    """Read a generic password from the login keychain (may prompt the user)."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        raise CookieError(f"cannot read keychain item '{service}': {exc}") from exc
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _derive_key(password: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", 1003, dklen=16)


def _decrypt_value(encrypted: bytes, key: bytes, host_key: str) -> str | None:
    """Decrypt one cookie value.  Returns None for unsupported encodings."""
    if not encrypted or encrypted[:3] not in (b"v10", b"v11"):
        return None
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    plain = decryptor.update(encrypted[3:]) + decryptor.finalize()
    plain = plain[: -plain[-1]]  # strip PKCS#7 padding
    digest = hashlib.sha256(host_key.encode("utf-8")).digest()
    if plain[:32] == digest:
        plain = plain[32:]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _profile_dirs(root: str) -> Iterable[str]:
    if not os.path.isdir(root):
        return []
    return [
        os.path.join(root, name)
        for name in sorted(os.listdir(root))
        if name == "Default" or name.startswith("Profile")
    ]


def _cookie_rows(db_path: str, domain: str) -> list:
    """Read cookie rows from a (copied) Chromium cookie database."""
    tmp = tempfile.mktemp(suffix=".sqlite")
    shutil.copy2(db_path, tmp)
    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            return conn.execute(
                "SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE ?",
                (f"%{domain}%",),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_cookies(browser: str = "edge", domain: str = "spotify.com") -> dict[str, str]:
    """Return ``{cookie_name: value}`` for the given browser profile.

    Raises :class:`CookieError` when the keychain is unreadable or the browser
    has no Spotify cookies at all.
    """
    browser = browser.lower()
    root = os.path.expanduser(BROWSER_ROOTS.get(browser, BROWSER_ROOTS["edge"]))
    service = KEYCHAIN_SERVICES.get(browser, KEYCHAIN_SERVICES["edge"])
    password = _keychain_password(service)
    if not password:
        raise CookieError(
            f"keychain item '{service}' is not readable - unlock the login keychain "
            f"or set SPOTIFY_COOKIES_FILE instead"
        )
    key = _derive_key(password)

    found: dict[str, str] = {}
    for profile in _profile_dirs(root):
        for relative in ("Cookies", "Network/Cookies"):
            db_path = os.path.join(profile, relative)
            if not os.path.exists(db_path):
                continue
            for host_key, name, encrypted in _cookie_rows(db_path, domain):
                value = _decrypt_value(encrypted, key, host_key)
                if value is not None:
                    found[name] = value
    if not found:
        raise CookieError(
            f"no spotify cookies found in {root} - log in to open.spotify.com in "
            f"{browser} first"
        )
    return found


def load_cookies(
    browser: str | None = None,
    cookies_file: str | None = None,
) -> dict[str, str]:
    """Resolve cookies from, in order of precedence:

    1. an explicit JSON file (``SPOTIFY_COOKIES_FILE`` / ``cookies_file``),
    2. the local browser profile (``SPOTIFY_BROWSER`` / ``browser``).
    """
    import json

    cookies_file = cookies_file or os.environ.get("SPOTIFY_COOKIES_FILE")
    if cookies_file:
        with open(cookies_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        raise CookieError(f"cookie file {cookies_file} must contain a JSON object")

    browser = browser or os.environ.get("SPOTIFY_BROWSER", "edge")
    return read_cookies(browser)


def has_session(cookies: dict[str, str]) -> bool:
    """True when the cookie jar looks like a logged-in Spotify session."""
    return all(name in cookies for name in ("sp_t", "sp_key"))
