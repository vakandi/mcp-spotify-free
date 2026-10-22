#!/usr/bin/env python3
"""mcp-spotify-free - Spotify control for FREE accounts (MCP server).

Exposes the account's "Liked Songs" library, playback control and Spotify
Connect device switching (MacBook / Chromecast / phone / TV) as MCP tools.

Run as an MCP server (stdio):
    python3 server.py

Run as a CLI:
    python3 server.py login      # capture a session (browser or manual)
    python3 server.py logout     # forget the stored session
    python3 server.py status     # check the session and show devices
    python3 server.py doctor     # diagnose the session
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from typing import Any

from fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spotify_free import SpotifyError, SpotifyFreeClient  # noqa: E402
from spotify_free import session as session_mod  # noqa: E402

mcp = FastMCP("spotify-free")

_client: SpotifyFreeClient | None = None


def get_client() -> SpotifyFreeClient:
    """Lazily build the shared client (cookies are read once)."""
    global _client
    if _client is None:
        _client = SpotifyFreeClient(
            browser=os.environ.get("SPOTIFY_BROWSER", "edge"),
            cookies_file=os.environ.get("SPOTIFY_COOKIES_FILE") or None,
        )
    return _client


def _session_remedy(exc: Exception) -> dict[str, Any] | None:
    """Detect a session/cookie failure and describe how the agent fixes it.

    Returns None for everything else (network, device, search errors...).
    """
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    auth = (
        "cookieerror" in low
        or "cookie" in low
        or "log in to open.spotify" in low
        or "(401)" in text
        or "(403)" in text
    )
    if not auth:
        return None
    popup = (
        'mcp-cli call macos-popup show_popup '
        '\'{"title":"Spotify","message":"Ta session Spotify a expire - '
        'connecte-toi a open.spotify.com pour que je puisse mettre de la '
        'musique.","level":"warning","duration":18000}\''
    )
    return {
        "session_expired": True,
        "hint": "Spotify cookies invalid or expired. The user must (re)log in "
        "before any music tool can work.",
        "how_to_fix": [
            "1. Tell the user WHY nothing plays - show a macOS popup:",
            f"   {popup}",
            "2. Open the login page in the default browser:",
            "   open https://open.spotify.com",
            "3. Once the user has logged in, re-login and RETRY the original call:",
            "   mcp-cli call spotify-free spotify_login '{}'",
        ],
        "login_url": "https://open.spotify.com",
        "login_command": "mcp-cli call spotify-free spotify_login '{}'",
    }


def _guard(func):
    """Turn exceptions into a structured error payload."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        caught: Exception
        try:
            return func(*args, **kwargs)
        except SpotifyError as exc:
            caught = exc
            payload = {"ok": False, "error": str(exc), "action": func.__name__}
        except Exception as exc:  # noqa: BLE001
            caught = exc
            payload = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "action": func.__name__,
            }
        remedy = _session_remedy(caught)
        if remedy:
            payload["remedy"] = remedy
        return payload

    return wrapper


def _device_payload(device: dict[str, Any] | None) -> dict[str, Any] | None:
    if not device:
        return None
    return {
        "id": device.get("id"),
        "name": device.get("name"),
        "type": device.get("type"),
        "brand": device.get("brand"),
        "volume_percent": device.get("volume_percent"),
    }


def _track_payload(track: dict[str, Any] | None) -> dict[str, Any] | None:
    if not track:
        return None
    return {
        "name": track.get("name"),
        "artists": track.get("artists"),
        "album": track.get("album"),
        "uri": track.get("uri"),
        "added_at": track.get("added_at"),
        "duration_ms": track.get("duration_ms"),
    }


# --------------------------------------------------------------------------
# docs
# --------------------------------------------------------------------------
DOCS = """\
mcp-spotify-free - Spotify for FREE accounts (no Premium needed)
================================================================

WHAT THIS IS
------------
Spotify's public Web API hides "Liked Songs" behind Premium (GET /v1/me/tracks
returns 403 on a free account).  This server talks to the same internal
endpoints the Spotify web player uses, so a free account gets its full library
plus real playback control - and it can push audio to ANY Spotify Connect
device (Chromecast, TV, phone, the desktop app) instead of only the local
machine.

HOW THE AUTH WORKS (so you know what it touches)
-------------------------------------------------
It reads the session cookies of a browser where you are ALREADY logged in to
open.spotify.com (Edge by default), decrypts them locally with the macOS
keychain, and exchanges them for a short-lived user access token.  Nothing is
sent anywhere else, no password is ever handled, and no token is written to
disk.  Spotify's own Developer Terms forbid third-party use of the web-player
token endpoint - this is a personal-use tool, do not ship it as a product.

THE SESSION IS STORED ON THE FILESYSTEM
----------------------------------------
spotify_login() captures a working session and writes it to
~/.config/mcp-spotify-free/cookies.json (mode 0600).  Every later run reads
that file first, so the server works with the browser CLOSED.  Nothing else is
persisted: the access token lives in memory only.

  spotify_session_status()  - where the cookies come from and if they work
  spotify_login()           - capture / refresh the session
  spotify_logout()          - delete the stored cookies (does not log you out
                              of Spotify itself)

CLI EQUIVALENTS (handy outside an agent)
  mcp-spotify-free login      capture the session (opens the browser)
  mcp-spotify-free logout     forget the stored session
  mcp-spotify-free status     session + liked count + devices
  mcp-spotify-free doctor     diagnose the session


QUICK START
-----------
1. get_docs()                 - read this file
2. spotify_status()           - is the session alive, what is playing, which
                                devices exist
3. list_liked_songs(limit=20) - browse the library
4. play_song("daft punk", device="Chromecast")
5. chromecast_sent("daft punk")   - shortcut: always the Chromecast
6. pause_playback() / resume_playback()


TOOLS
-----
get_docs()
    This text. Everything else is documented below.

spotify_status()
    Full snapshot: account tier, token state, liked-song count, currently
    playing track, and every Spotify Connect device with its volume.
    -> {ok, account, liked_songs_count, now_playing, devices, token}

list_liked_songs(limit=50, offset=0, query="")
    Read the "Liked Songs" playlist (newest first, same order as the app).
    With `query` the WHOLE library is scanned (not only the current page).
    -> {ok, count, total_liked, offset, tracks:[{name, artists, album, uri,
        track_id, duration_ms, added_at, playable, image}]}

search_liked_songs(query, limit=20)
    Same filtering, but returns only the matches.  Use it when you know
    roughly what the track is called but not exactly.
    -> {ok, query, count, tracks:[...]}

search_spotify(query, limit=20)
    Search the WHOLE Spotify catalog (not only your library): tracks,
    artists, albums, playlists and profiles.  Same search box as the web
    player.
    -> {ok, query, counts, tracks, artists, albums, playlists, users}

play_song(query, device="", index=0)
    THE TOOL TO USE.  Looks the title/artist up in the Liked Songs first and
    falls back to the catalog search, then plays it.
      query  - text to match, e.g. "get lucky", "daft punk one more time"
      device - optional Spotify Connect device name ("Chromecast",
               "MacBook Pro", "iPhone").  Empty = the active device.
      index  - which match to use when several match (0 = best match).
    -> {ok, action, query, source, device, track, now_playing}
    `source` is "liked_songs" or "catalog" - it tells where the match came
    from.

play_liked_song(query, device="", index=0)
    Same, but the Liked Songs library only (no catalog fallback).
    -> {ok, action, device, track, now_playing}

play_track(uri, device="")
    Play a raw Spotify URI ("spotify:track:xxxx" or a plain track id).
    -> {ok, action, device, uri, now_playing}

chromecast_sent(track_query="", action="play")
    THE SHORTCUT.  Always targets the Chromecast.
      track_query - title/artist to play; empty = keep the current track and
                    just resume it.
      action      - "play" (default), "resume", "pause", "next", "previous".
    It resolves the Chromecast by name, sends the command to that device and
    verifies the active device actually switched to it.
    -> {ok, action, device, track, now_playing, active_device}

pause_playback(device="")
    Pause.  Empty device = the device that is currently playing.
    -> {ok, action, device, now_playing}

resume_playback(device="")
    Resume playback.
    -> {ok, action, device, now_playing}

skip_next(device="") / skip_previous(device="")
    Move one track forward / backward.
    -> {ok, action, device, now_playing}

set_volume(level, device="")
    Volume 0-100 on a device.  Empty device = the active device.
    -> {ok, action, device, volume}

list_devices()
    Every Spotify Connect device of the account (name, type, brand, volume,
    which one is active).
    -> {ok, count, devices:[{id, name, type, brand, volume_percent, is_active}]}

now_playing()
    Current track, playing/paused flag, position and the active device.
    -> {ok, now_playing}

cleanup_devices()
    Remove the leftover "MCP Spotify Free" Connect devices of the earlier
    versions.  The server does not register a device of its own any more
    (it reuses the account's web-player device to send the commands), so
    nothing is added to the account's device list any more.
    -> {ok, action, removed, failed, note, devices}

spotify_session_status()
    Diagnose the session: which sources are available (stored file / browser
    profile) and whether each one is actually accepted by Spotify.
    -> {ok, action, browser, store_path, checks, hint}

spotify_login(browser="", timeout=300)
    Capture a working session.  Tries the stored session, then the browser
    profile; if neither works it opens open.spotify.com and keeps polling.
    When Chromium keeps the session cookie in memory, the user is asked to
    quit the browser (Cmd+Q) so the cookie is flushed to disk - the tool
    picks it up on its own and stores it for later runs.
    -> {ok, action, browser, source, cookies_captured, path, liked_songs_count}

spotify_logout()
    Delete the stored session file.  Does not log the account out of Spotify.
    -> {ok, action, removed, path}


HOW TO PICK A DEVICE
--------------------
Device names come from list_devices().  Typical entries on this account:
  "Chromecast"                     - the Google TV / Chromecast
  "MacBook Pro de vakandi"         - the desktop app
  "Web Player (Microsoft Edge)"    - the browser
Partial names work ("chromecast", "macbook", "tv").  When you pass a device
name, the server switches the Spotify Connect session to it: the audio moves
from the current device to the requested one.


TIPS FOR THE AGENT
------------------
* Just call play_song("title or artist") - it looks in the Liked Songs
  first, then in the catalog, and plays the best match.
* The liked library is ordered newest-first - the first match is usually the
  most recently liked song.
* Use device="" for whatever is already playing, device="Chromecast" to move
  the sound to the TV.
* Every tool returns {"ok": true/false, ...}.  On ok=false, "error" explains
  what failed (most common cause: not logged in to open.spotify.com in the
  browser any more).
"""


@mcp.tool
@_guard
def get_docs() -> str:
    """Full usage guide for every tool of this server (one big text)."""
    return DOCS


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
@mcp.tool
@_guard
def spotify_status() -> dict[str, Any]:
    """Account, token, library size, now playing and every Connect device."""
    client = get_client()
    token = client.access_token()
    devices = client.devices()
    now = client.now_playing()
    try:
        liked_count = client.liked_songs_count()
    except SpotifyError:
        liked_count = None
    return {
        "ok": True,
        "action": "status",
        "account": {
            "signed_in": bool(token),
            "tier": "free (works without Premium)",
            "browser": client.browser,
        },
        "token": {
            "valid": bool(token),
            "expires_in_min": client.token_expiry_minutes(),
        },
        "liked_songs_count": liked_count,
        "now_playing": now,
        "devices": devices,
    }


@mcp.tool
@_guard
def list_devices() -> dict[str, Any]:
    """List every Spotify Connect device (Chromecast, MacBook, phones, TVs)."""
    devices = get_client().devices()
    return {
        "ok": True,
        "action": "list_devices",
        "count": len(devices),
        "devices": [
            {
                "id": d["id"],
                "name": d["name"],
                "type": d["type"],
                "brand": d["brand"],
                "volume_percent": d["volume_percent"],
                "is_active": d["is_active"],
            }
            for d in devices
        ],
    }


@mcp.tool
@_guard
def now_playing() -> dict[str, Any]:
    """Current track, play/pause state and the active device."""
    return {
        "ok": True,
        "action": "now_playing",
        "now_playing": get_client().now_playing(),
    }


# --------------------------------------------------------------------------
# library
# --------------------------------------------------------------------------
@mcp.tool
@_guard
def list_liked_songs(limit: int = 50, offset: int = 0, query: str = "") -> dict[str, Any]:
    """Read the Liked Songs playlist (free accounts included).

    Args:
        limit: how many tracks to return (1-100).
        offset: pagination offset (0 = most recently liked first).
        query: optional filter on title / artist / album.  When set, the whole
            library is scanned (not only the current page).
    """
    client = get_client()
    if query:
        tracks = client.search_liked(query, limit=limit)
        offset = 0
    else:
        tracks = client.liked_songs(limit=limit, offset=offset)
    try:
        total = client.liked_songs_count()
    except SpotifyError:
        total = None
    return {
        "ok": True,
        "action": "list_liked_songs",
        "count": len(tracks),
        "total_liked": total,
        "offset": offset,
        "query": query or None,
        "tracks": tracks,
    }


@mcp.tool
@_guard
def search_liked_songs(query: str, limit: int = 20) -> dict[str, Any]:
    """Search the whole Liked Songs library for a title / artist / album.

    Args:
        query: text to look for, e.g. "daft punk", "one more time".
        limit: maximum number of matches to return.
    """
    tracks = get_client().search_liked(query, limit=limit)
    return {
        "ok": True,
        "action": "search_liked_songs",
        "query": query,
        "count": len(tracks),
        "tracks": tracks,
    }


@mcp.tool
@_guard
def search_spotify(query: str, limit: int = 20) -> dict[str, Any]:
    """Search the whole Spotify catalog (not only your library).

    Same search box as the web player: tracks, artists, albums, playlists and
    profiles.  Use it when the song is not in the Liked Songs.

    Args:
        query: title, artist, album or playlist to look for.
        limit: maximum number of results per category (1-50).
    """
    results = get_client().search_catalog(query, limit=limit)
    return {
        "ok": True,
        "action": "search_spotify",
        "query": query,
        "counts": {key: len(value) for key, value in results.items()},
        **results,
    }


# --------------------------------------------------------------------------
# playback
# --------------------------------------------------------------------------
def _resolve_liked(client, query: str, index: int) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        raise SpotifyError("no track name given")
    matches = client.search_liked(query, limit=25)
    if not matches:
        raise SpotifyError(f"no liked song matches '{query}'")
    if index < 0 or index >= len(matches):
        raise SpotifyError(
            f"index {index} out of range, {len(matches)} match(es) for '{query}'"
        )
    return matches[index]


def _resolve_any(client, query: str, index: int) -> tuple:
    """Find a track: Liked Songs first, then the whole catalog."""
    query = (query or "").strip()
    if not query:
        raise SpotifyError("no track name given")
    matches = client.search_liked(query, limit=25)
    source = "liked_songs"
    if not matches:
        matches = (client.search_catalog(query, limit=25) or {}).get("tracks") or []
        source = "catalog"
    if not matches:
        raise SpotifyError(f"no song matches '{query}' (library + catalog)")
    if index < 0 or index >= len(matches):
        raise SpotifyError(
            f"index {index} out of range, {len(matches)} match(es) for '{query}'"
        )
    return matches[index], source


@mcp.tool
@_guard
def play_liked_song(query: str, device: str = "", index: int = 0) -> dict[str, Any]:
    """Play a liked song, optionally on a specific Spotify Connect device.

    Args:
        query: title / artist of the liked song, e.g. "one more time".
        device: device name to play on ("Chromecast", "MacBook Pro"...).
                Empty = the currently active device.
        index: which match to use when several songs match.
    """
    client = get_client()
    track = _resolve_liked(client, query, index)
    ack = client.play_track(track["uri"], device=device or None)
    target = client.find_device(device) if device else client.active_device()
    return {
        "ok": True,
        "action": "play_liked_song",
        "query": query,
        "device": _device_payload(target),
        "track": _track_payload(track),
        "ack_id": ack.get("ack_id"),
        "now_playing": client.now_playing(settle=1.2),
    }


@mcp.tool
@_guard
def play_track(uri: str, device: str = "") -> dict[str, Any]:
    """Play a raw Spotify track URI on a device.

    Args:
        uri: "spotify:track:xxxxx" or a bare track id.
        device: device name ("" = active device).
    """
    client = get_client()
    uri = uri.strip()
    if not uri.startswith("spotify:"):
        uri = f"spotify:track:{uri}"
    ack = client.play_track(uri, device=device or None)
    target = client.find_device(device) if device else client.active_device()
    return {
        "ok": True,
        "action": "play_track",
        "device": _device_payload(target),
        "uri": uri,
        "ack_id": ack.get("ack_id"),
        "now_playing": client.now_playing(settle=1.2),
    }


@mcp.tool
@_guard
def play_song(query: str, device: str = "", index: int = 0) -> dict[str, Any]:
    """Play a song by name - Liked Songs first, then the whole catalog.

    This is the tool to use when the user says "play <something>": it looks
    the title/artist up in the Liked Songs and, if nothing matches, falls back
    to the Spotify catalog search.

    Args:
        query: title and/or artist, e.g. "one more time daft punk".
        device: device name to play on ("" = the device that is active).
        index: which match to use when several match (0 = best match).
    """
    client = get_client()
    track, source = _resolve_any(client, query, index)
    ack = client.play_track(track["uri"], device=device or None)
    target = client.find_device(device) if device else client.active_device()
    return {
        "ok": True,
        "action": "play_song",
        "query": query,
        "source": source,
        "device": _device_payload(target),
        "track": _track_payload(track),
        "ack_id": ack.get("ack_id"),
        "now_playing": client.now_playing(settle=1.2),
    }


@mcp.tool
@_guard
def chromecast_sent(track_query: str = "", action: str = "play") -> dict[str, Any]:
    """Shortcut: drive the Chromecast.

    Always targets the device named "Chromecast" (falls back to a Google /
    Chromecast device if the name differs).

    Args:
        track_query: title / artist of a liked song to play.  Leave empty to
                     keep the current track (used with action="resume").
        action: "play" (default), "resume", "pause", "next" or "previous".
    """
    client = get_client()
    action = (action or "play").strip().lower()
    if action not in ("play", "resume", "pause", "next", "previous"):
        raise SpotifyError(
            f"unknown action '{action}' - use play, resume, pause, next or previous"
        )
    device = _find_chromecast(client)
    track = None
    if action == "play":
        if track_query:
            track, _source = _resolve_any(client, track_query, 0)
            client.play_track(track["uri"], device=device["id"])
        else:
            client.resume(device=device["id"])
    elif action == "resume":
        client.resume(device=device["id"])
    elif action == "pause":
        client.pause(device=device["id"])
    elif action == "next":
        client.skip_next(device=device["id"])
    elif action == "previous":
        client.skip_previous(device=device["id"])
    now = client.now_playing(settle=1.2)
    return {
        "ok": True,
        "action": "chromecast_sent",
        "requested_action": action,
        "device": _device_payload(device),
        "track": _track_payload(track) or _now_track_payload(now),
        "now_playing": now,
        "active_device": now.get("active_device"),
    }


def _find_chromecast(client) -> dict[str, Any]:
    devices = client.devices()
    for device in devices:
        if (device["name"] or "").lower() == "chromecast":
            return device
    for device in devices:
        blob = f"{(device['name'] or '').lower()} {(device['brand'] or '').lower()} {(device['model'] or '').lower()}"
        if "chromecast" in blob or "google" in blob or device["type"] == "tv":
            return device
    raise SpotifyError(
        "no Chromecast found - call list_devices() to see the available devices"
    )


def _now_track_payload(now: dict[str, Any]) -> dict[str, Any] | None:
    track = now.get("track") or {}
    if not track.get("uri"):
        return None
    return {
        "name": track.get("name"),
        "uri": track.get("uri"),
        "artists": track.get("artists"),
    }


@mcp.tool
@_guard
def pause_playback(device: str = "") -> dict[str, Any]:
    """Pause playback ("" = the device that is currently playing)."""
    client = get_client()
    ack = client.pause(device=device or None)
    target = client.find_device(device) if device else client.active_device()
    return {
        "ok": True,
        "action": "pause_playback",
        "device": _device_payload(target),
        "ack_id": ack.get("ack_id"),
        "now_playing": client.now_playing(settle=1.2),
    }


@mcp.tool
@_guard
def resume_playback(device: str = "") -> dict[str, Any]:
    """Resume playback ("" = the device that is currently playing)."""
    client = get_client()
    ack = client.resume(device=device or None)
    target = client.find_device(device) if device else client.active_device()
    return {
        "ok": True,
        "action": "resume_playback",
        "device": _device_payload(target),
        "ack_id": ack.get("ack_id"),
        "now_playing": client.now_playing(settle=1.2),
    }


@mcp.tool
@_guard
def skip_next(device: str = "") -> dict[str, Any]:
    """Skip to the next track."""
    client = get_client()
    ack = client.skip_next(device=device or None)
    target = client.find_device(device) if device else client.active_device()
    return {
        "ok": True,
        "action": "skip_next",
        "device": _device_payload(target),
        "ack_id": ack.get("ack_id"),
        "now_playing": client.now_playing(settle=1.2),
    }


@mcp.tool
@_guard
def skip_previous(device: str = "") -> dict[str, Any]:
    """Go back to the previous track."""
    client = get_client()
    ack = client.skip_previous(device=device or None)
    target = client.find_device(device) if device else client.active_device()
    return {
        "ok": True,
        "action": "skip_previous",
        "device": _device_payload(target),
        "ack_id": ack.get("ack_id"),
        "now_playing": client.now_playing(settle=1.2),
    }


@mcp.tool
@_guard
def set_volume(level: int, device: str = "") -> dict[str, Any]:
    """Set the volume of a device (0-100).

    Args:
        level: 0 = mute, 100 = max.
        device: device name ("" = active device).
    """
    client = get_client()
    result = client.set_volume(level, device=device or None)
    return {
        "ok": True,
        "action": "set_volume",
        "device": _device_payload(result.get("device")),
        "volume": result.get("volume"),
    }


@mcp.tool
@_guard
def cleanup_devices() -> dict[str, Any]:
    """Remove the leftover "MCP Spotify Free" Connect devices of this server.

    The server does not register a device of its own any more (it reuses the
    account's web-player device to send the commands), but earlier versions
    did - those entries are removed here, right away.
    """
    client = get_client()
    result = client.cleanup_devices()
    return {
        "ok": True,
        "action": "cleanup_devices",
        "removed": len(result.get("removed", [])),
        "failed": len(result.get("failed", [])),
        "note": result.get("note"),
        "devices": [d["name"] for d in client.devices()],
    }


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------
@mcp.tool
@_guard
def spotify_login(browser: str = "", timeout: int = 300) -> dict[str, Any]:
    """Capture a working Spotify session (free account is enough).

    Tries, in order: the stored session, then the browser profile.  If the
    browser keeps the session cookie in memory, the user is asked to quit the
    browser (Cmd+Q) so Chromium flushes it to disk - the tool keeps polling
    until the session is captured, then stores it on the filesystem so later
    runs work without the browser.

    Args:
        browser: "edge" (default), "chrome", "brave", "vivaldi" or "opera".
        timeout: how long to wait for the user, in seconds.
    """
    browser = browser or os.environ.get("SPOTIFY_BROWSER", "edge")
    result = session_mod.login(browser=browser, timeout=timeout, open_it=True)
    payload = {
        "ok": bool(result.get("ok")),
        "action": "spotify_login",
        "browser": browser,
        "source": result.get("source"),
        "cookies_captured": result.get("cookies"),
        "path": result.get("path") or session_mod.store_path(),
        "reason": result.get("reason") or None,
    }
    if payload["ok"]:
        try:
            client = get_client()
            client._cookies = None
            payload["liked_songs_count"] = client.liked_songs_count()
            payload["devices"] = [d["name"] for d in client.devices()]
        except Exception as exc:  # noqa: BLE001
            payload["warning"] = f"session stored but not verified: {exc}"
    return payload


@mcp.tool
@_guard
def spotify_logout() -> dict[str, Any]:
    """Forget the stored session (does not log the account out of Spotify)."""
    result = session_mod.logout()
    return {
        "ok": True,
        "action": "spotify_logout",
        "removed": result.get("removed"),
        "path": result.get("path"),
    }


@mcp.tool
@_guard
def spotify_session_status() -> dict[str, Any]:
    """Diagnose the session: where the cookies come from and if they work."""
    stored = session_mod.load_stored()
    browser = os.environ.get("SPOTIFY_BROWSER", "edge")
    in_browser = session_mod.browser_cookies(browser)
    checks = {}
    for name, jar in (("stored_file", stored), ("browser_profile", in_browser)):
        if not jar:
            checks[name] = {"present": False, "valid": False, "reason": "no cookies"}
            continue
        ok, reason = session_mod.session_is_valid(jar)
        checks[name] = {
            "present": True,
            "valid": ok,
            "reason": reason or None,
            "cookies": len(jar),
            "has_session_cookie": "sp_dc" in jar,
        }
    return {
        "ok": True,
        "action": "spotify_session_status",
        "browser": browser,
        "store_path": session_mod.store_path(),
        "checks": checks,
        "hint": (
            "Run spotify_login() if no source is valid. If the browser has a valid "
            "session but it is not on disk yet, quit the browser (Cmd+Q) so "
            "Chromium flushes the cookie, then run spotify_login() again."
        ),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _cli_login(args: argparse.Namespace) -> int:
    print(f"[spotify-free] session store: {session_mod.store_path()}")
    result = session_mod.login(
        browser=args.browser,
        timeout=args.timeout,
        open_it=not args.no_open,
        log=lambda message: print(f"[spotify-free] {message}", file=sys.stderr, flush=True),
    )
    if result.get("ok"):
        print(json.dumps({"ok": True, "source": result.get("source"), "path": result.get("path")}, indent=2))
        return 0
    print(json.dumps({"ok": False, "reason": result.get("reason")}, indent=2), file=sys.stderr)
    return 1


def _cli_logout(args: argparse.Namespace) -> int:
    result = session_mod.logout()
    print(json.dumps(result, indent=2))
    return 0


def _cli_status(args: argparse.Namespace) -> int:
    payload = spotify_session_status()
    print(json.dumps(payload, indent=2))
    if payload.get("ok") and any(c.get("valid") for c in payload["checks"].values()):
        client = get_client()
        print(json.dumps({"liked_songs_count": client.liked_songs_count(),
                          "devices": [d["name"] for d in client.devices()]}, indent=2))
    return 0


def _cli_doctor(args: argparse.Namespace) -> int:
    print(json.dumps(spotify_session_status(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-spotify-free", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")

    login_cmd = sub.add_parser("login", help="capture a working Spotify session")
    login_cmd.add_argument("--browser", default=os.environ.get("SPOTIFY_BROWSER", "edge"))
    login_cmd.add_argument("--timeout", type=int, default=300)
    login_cmd.add_argument("--no-open", action="store_true", help="do not open the browser")
    login_cmd.set_defaults(func=_cli_login)

    logout_cmd = sub.add_parser("logout", help="forget the stored session")
    logout_cmd.set_defaults(func=_cli_logout)

    status_cmd = sub.add_parser("status", help="show session + devices")
    status_cmd.set_defaults(func=_cli_status)

    doctor_cmd = sub.add_parser("doctor", help="diagnose the session")
    doctor_cmd.set_defaults(func=_cli_doctor)

    args = parser.parse_args(argv)
    if getattr(args, "func", None):
        return args.func(args)
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
