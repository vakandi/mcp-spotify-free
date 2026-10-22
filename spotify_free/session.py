"""Session bootstrap: get a logged-in Spotify session without a Premium account.

The server can read the Spotify cookies of a browser where the user is already
logged in.  Two situations need help:

* the browser is not logged in at all -> the user logs in once, we pick the
  session up from the browser profile;
* the cookies are not flushed to disk yet -> the user quits the browser
  (Cmd+Q) so Chromium writes the session cookie, we read it again.

Once a working session is found it is stored on the filesystem
(``~/.config/mcp-spotify-free/cookies.json``, mode 0600) so every later run
works without the browser being open.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable

from . import cookies as cookies_mod

LOGIN_URL = "https://open.spotify.com/login"
BROWSER_BUNDLE_IDS = {
    "edge": "com.microsoft.edgemac",
    "chrome": "com.google.Chrome",
    "brave": "com.brave.Browser",
    "vivaldi": "com.vivaldi.Vivaldi",
    "opera": "com.operasoftware.Opera",
}


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "mcp-spotify-free")


def store_path() -> str:
    return os.environ.get("SPOTIFY_COOKIES_FILE") or os.path.join(config_dir(), "cookies.json")


def load_stored() -> dict[str, str]:
    """Read the persisted cookie jar (empty dict when there is none)."""
    path = store_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_stored(cookies: dict[str, str]) -> str:
    """Persist the cookie jar so the server works without the browser."""
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(cookies, handle, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def clear_stored() -> bool:
    path = store_path()
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def open_browser(browser: str = "edge") -> bool:
    """Open the Spotify login page in the user's browser."""
    bundle = BROWSER_BUNDLE_IDS.get(browser.lower(), BROWSER_BUNDLE_IDS["edge"])
    try:
        subprocess.run(["open", "-a", bundle, LOGIN_URL], check=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        try:
            subprocess.run(["open", LOGIN_URL], check=True, timeout=15)
            return True
        except (OSError, subprocess.SubprocessError):
            return False


def session_is_valid(cookies: dict[str, str]) -> tuple[bool, str]:
    """Check the cookie jar against the real backend.

    Returns ``(ok, reason)`` - the reason is empty when the session works.
    """
    from .client import SpotifyError, SpotifyFreeClient

    if not cookies:
        return False, "no cookies"
    if "sp_t" not in cookies:
        return False, "missing sp_t cookie"
    client = SpotifyFreeClient(cookies=cookies)
    try:
        client.access_token()
    except SpotifyError as exc:
        return False, str(exc)[:160]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:160]
    return True, ""


def browser_cookies(browser: str = "edge") -> dict[str, str]:
    """Cookies currently on disk for the given browser (may be empty)."""
    try:
        return cookies_mod.read_cookies(browser)
    except cookies_mod.CookieError:
        return {}


def login(
    browser: str = "edge",
    timeout: int = 300,
    open_it: bool = True,
    log: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Interactive login helper.

    1. a stored session is tried first,
    2. then the browser profile (the normal case),
    3. and if the browser keeps the session in memory the user is asked to
       quit it so Chromium flushes the cookie to disk.
    """
    say = log or (lambda message: print(message, file=sys.stderr, flush=True))

    stored = load_stored()
    if stored:
        ok, reason = session_is_valid(stored)
        if ok:
            say("A stored session is already valid - nothing to do.")
            return {"ok": True, "source": "stored", "reason": ""}
        say(f"Stored session is not usable ({reason}); trying the browser.")

    # the browser usually already holds a usable session - no interaction needed
    jar = browser_cookies(browser)
    if jar:
        ok, reason = session_is_valid(jar)
        if ok:
            path = save_stored(jar)
            say(f"Session found in the {browser} profile ({len(jar)} cookies) -> {path}")
            return {
                "ok": True,
                "source": "browser",
                "reason": "",
                "cookies": len(jar),
                "path": path,
                "attempts": 0,
            }
        say(f"The {browser} profile has cookies but they are not usable yet ({reason}).")

    if open_it:
        say(f"Opening {LOGIN_URL} in {browser} - log in there (free account is fine).")
        if not open_browser(browser):
            say(f"Could not open the browser automatically. Open {LOGIN_URL} yourself.")

    deadline = time.time() + max(30, int(timeout))
    asked_to_quit = False
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        jar = browser_cookies(browser)
        if jar:
            ok, reason = session_is_valid(jar)
            if ok:
                path = save_stored(jar)
                say(f"Session captured ({len(jar)} cookies) and saved to {path}.")
                return {
                    "ok": True,
                    "source": "browser",
                    "reason": "",
                    "cookies": len(jar),
                    "path": path,
                    "attempts": attempts,
                }
            if not asked_to_quit and attempts >= 4:
                asked_to_quit = True
                say(
                    "The session cookie is not on disk yet (Chromium keeps it in memory).\n"
                    "  -> Finish the login, then QUIT the browser completely (Cmd+Q).\n"
                    "  -> This server keeps polling and will pick the session up on its own."
                )
        time.sleep(3)

    say(
        "Timed out. Log in to open.spotify.com, quit the browser with Cmd+Q, "
        "then run the login command again."
    )
    return {"ok": False, "source": "timeout", "reason": "no valid session found", "attempts": attempts}


def logout() -> dict[str, object]:
    """Forget the stored session (does not log the account out of Spotify)."""
    removed = clear_stored()
    return {"ok": True, "removed": removed, "path": store_path()}
