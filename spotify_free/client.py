"""Client for the Spotify web-player backend - works on FREE accounts.

Why this exists
---------------
Spotify's official Web API only exposes "Liked Songs"
(``GET /v1/me/tracks``) to Premium subscribers.  A free account gets a
``403`` there.  The web player, however, serves the very same library to free
users through two internal endpoints that this module speaks:

* ``GET  https://open.spotify.com/api/token`` - hands out a *user* access token
  (guarded by a TOTP, see :mod:`spotify_free.totp`).
* ``POST https://api-partner.spotify.com/pathfinder/v2/query`` - the GraphQL-ish
  gateway the web player uses for every page (library, playlists, tracks).

Playback is driven through the "harmony" command protocol of the Spotify
Connect backend, which is what lets a script start a track *on a chosen
device* (a Chromecast, the desktop app, a phone...) instead of only on the
machine running the script.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any

import requests

from . import cookies as cookies_mod
from . import totp as totp_mod

# --- endpoints -------------------------------------------------------------
OPEN_API = "https://open.spotify.com"
PARTNER_API = "https://api-partner.spotify.com"
SPCLIENT = "https://gew1-spclient.spotify.com"
CLIENTTOKEN_API = "https://clienttoken.spotify.com/v1/clienttoken"
APRESOLVE_API = "https://apresolve.spotify.com"

# --- constants lifted from the web player ----------------------------------
APP_VERSION = "1.3.7.2082.g27118464e36a"
CLIENT_ID = "d8a5ed958d274c2e8ee717e6a4b0971d"
HARMONY_VERSION = "4.11.0-af0ef98"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0"
)
DEALER = "tcp://0a0e2cc2.ip.gew1.spotify.net:5700"
DEALER_KEY = "2BF7A0B662ED04741CCC9845CBA67F640CBC344E02CF7C333091910CA170EF"

#: Universal playlist aliases used by the web player (not account specific).
LIKED_SONGS_PLAYLIST_ID = "37i9dQZF1F5p3rmiWPIYgZ"   # "Liked Songs"
YOUR_EPISODES_PLAYLIST_ID = "37i9dQZF1FgnTBfUlzkeKt"  # "Your Episodes"

#: persistedQuery hashes used by the web player.
SHA_LIBRARY = "390c78e5b951029bad359785e69b07b536a509c581cbcd0aded5e5067f187455"
SHA_PLAYLIST = "21ea11a6ac75a5f3e01fd365d29c18e0a4a8b7dc09f89bbb5d73910190c6acaf"
SHA_PLAYER_OPTIONS = "e2dcfcab470854d4d1c7cb1a851438f14fe0a94d57db7f0b9dde492559d5395d"
SHA_SEARCH = "dd78eaff943eba629ed70ee25517b9cea0dcaa41193e2592ae2727660b21892c"


class SpotifyError(RuntimeError):
    """Any failure while talking to Spotify."""


def _truthy(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _cover_url(payload: dict[str, Any] | None) -> str | None:
    """Biggest cover-art URL of a pathfinder album/track payload."""
    if not payload:
        return None
    sources = (payload.get("coverArt") or {}).get("sources") or []
    if not sources:
        return None
    return max(sources, key=lambda s: s.get("width") or 0).get("url")


class SpotifyFreeClient:
    """Minimal Spotify client for free accounts (library read + playback)."""

    def __init__(
        self,
        browser: str | None = None,
        cookies: dict[str, str] | None = None,
        cookies_file: str | None = None,
        use_proxy: bool | None = None,
        device_name: str = "MCP Spotify Free",
    ) -> None:
        self.browser = browser or os.environ.get("SPOTIFY_BROWSER", "edge")
        self._cookies = cookies
        self._cookies_file = cookies_file
        self.use_proxy = _truthy(os.environ.get("SPOTIFY_USE_PROXY"), False)
        if use_proxy is not None:
            self.use_proxy = use_proxy
        self.device_name = device_name

        self.session = requests.Session()
        # The local proxy (proxy-local) only carries LLM traffic; Spotify calls
        # must not go through it.
        self.session.trust_env = self.use_proxy
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Referer": "https://open.spotify.com/",
                "Origin": "https://open.spotify.com",
                "app-platform": "WebPlayer",
                "spotify-app-version": APP_VERSION,
                "accept": "application/json",
                "content-type": "application/json",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self._access_token: str | None = None
        self._token_expiry_ms: int = 0
        self._client_token: str | None = None
        self._totp_secrets: dict[int, bytes] | None = None
        self._device_id: str | None = None
        self._connection_id: str | None = None
        self._library_cache: list[dict[str, Any]] = []
        self._library_cached_at: float = 0.0
        self._source_id: str | None = None
        self._last_device_id: str | None = None
        # the persisted state (our own device id + the last addressed device)
        # is read once, at startup
        self._load_state()

    # ------------------------------------------------------------------ auth
    def _cookie_jar(self) -> dict[str, str]:
        """Resolve the cookie jar: explicit > stored file > browser profile."""
        if self._cookies is None:
            from . import session as session_mod

            stored = session_mod.load_stored()
            if stored:
                self._cookies = stored
            else:
                self._cookies = cookies_mod.load_cookies(self.browser, self._cookies_file)
        for name, value in self._cookies.items():
            self.session.cookies.set(name, value, domain=".spotify.com")
        return self._cookies

    def _secrets(self) -> dict[int, bytes]:
        if self._totp_secrets is None:
            self._totp_secrets = totp_mod.discover(self.session)
        return self._totp_secrets

    def access_token(self, force: bool = False) -> str:
        """Return a valid user access token (cached until it expires)."""
        if not force and self._access_token and time.time() * 1000 < self._token_expiry_ms - 60_000:
            return self._access_token
        self._cookie_jar()
        secrets = self._secrets()
        version, key = totp_mod.active_secret(secrets)
        now = int(time.time() * 1000)
        params = {
            "reason": "init",
            "productType": "web-player",
            "totp": totp_mod.totp(now, key),
            "totpServer": totp_mod.totp(now, key),
            "totpVer": str(version),
        }
        response = self.session.get(f"{OPEN_API}/api/token", params=params, timeout=20)
        if response.status_code != 200:
            raise SpotifyError(
                f"token request failed ({response.status_code}): {response.text[:200]}"
            )
        payload = response.json()
        self._access_token = payload["accessToken"]
        self._token_expiry_ms = int(payload.get("accessTokenExpirationTimestampMs", 0))
        self._client_token = None  # bound to the access token
        return self._access_token

    def token_expiry_minutes(self) -> float:
        """Minutes left before the current access token expires."""
        return round(max(0, self._token_expiry_ms - int(time.time() * 1000)) / 60000, 1)

    def client_token(self) -> str:
        """A client token, required by the playback backend."""

        if self._client_token:
            return self._client_token
        jar = self._cookie_jar()
        body = {
            "client_data": {
                "client_version": APP_VERSION,
                "client_id": CLIENT_ID,
                "js_sdk_data": {
                    "device_brand": "Apple",
                    "device_model": "unknown",
                    "os": "macos",
                    "os_version": "10.15.7",
                    "device_id": jar.get("sp_t", str(uuid.uuid4())),
                    "device_type": "computer",
                },
            }
        }
        response = self.session.post(
            CLIENTTOKEN_API, data=json.dumps(body).encode("utf-8"), timeout=20
        )
        if response.status_code != 200 or not response.content:
            raise SpotifyError(f"client token request failed ({response.status_code})")
        payload = response.json()
        self._client_token = payload["granted_token"]["token"]
        return self._client_token

    def _auth_headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.access_token()}",
            "client-token": self.client_token(),
        }

    # ------------------------------------------------------------- pathfinder
    def _pathfinder(self, operation: str, variables: dict[str, Any], sha: str) -> dict[str, Any]:
        headers = self._auth_headers()
        headers["content-type"] = "application/json"
        response = self.session.post(
            f"{PARTNER_API}/pathfinder/v2/query",
            data=json.dumps(
                {
                    "variables": variables,
                    "operationName": operation,
                    "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha}},
                }
            ).encode("utf-8"),
            headers=headers,
            timeout=25,
        )
        if response.status_code != 200:
            raise SpotifyError(
                f"pathfinder {operation} failed ({response.status_code}): {response.text[:200]}"
            )
        payload = response.json()
        if payload.get("errors"):
            raise SpotifyError(f"pathfinder {operation}: {json.dumps(payload['errors'])[:300]}")
        return payload.get("data") or {}

    # ---------------------------------------------------------------- library
    def liked_songs_count(self) -> int:
        """Number of liked tracks on the account."""
        data = self._pathfinder(
            "libraryV3",
            {
                "order": None,
                "textFilter": "",
                "features": ["LIKED_SONGS"],
                "limit": 5,
                "offset": 0,
                "flatten": False,
                "expandedFolders": [],
                "folderUri": None,
                "includeFoldersWhenFlattening": True,
            },
            SHA_LIBRARY,
        )
        for item in data.get("me", {}).get("libraryV3", {}).get("items", []):
            wrapper = item.get("item", {})
            if wrapper.get("_uri") == "spotify:collection:tracks":
                return int(wrapper.get("data", {}).get("count", 0))
        return 0

    def liked_songs(
        self, limit: int = 50, offset: int = 0, playlist_id: str = LIKED_SONGS_PLAYLIST_ID
    ) -> list[dict[str, Any]]:
        """Read the "Liked Songs" playlist (free accounts included)."""
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        data = self._pathfinder(
            "fetchPlaylistContents",
            {
                "uri": f"spotify:playlist:{playlist_id}",
                "offset": offset,
                "limit": limit,
                "includeEpisodeContentRatingsV2": True,
            },
            SHA_PLAYLIST,
        )
        playlist = data.get("playlistV2") or {}
        content = playlist.get("content") or {}
        items = content.get("items") or []
        return [self._parse_track(item) for item in items]

    def all_liked_songs(self, max_items: int = 2000, refresh: bool = False) -> list[dict[str, Any]]:
        """Read the whole liked-songs library (paginates for you).

        The result is cached in memory for a short while: the library barely
        changes, but a full scan costs half a dozen requests.
        """
        if not refresh and self._library_cache and time.time() - self._library_cached_at < 120:
            return self._library_cache
        out: list[dict[str, Any]] = []
        offset = 0
        while len(out) < max_items:
            batch = self.liked_songs(limit=100, offset=offset)
            if not batch:
                break
            out.extend(batch)
            offset += len(batch)
            if len(batch) < 100:
                break
        out = out[:max_items]
        self._library_cache = out
        self._library_cached_at = time.time()
        return out

    @staticmethod
    def _parse_track(item: dict[str, Any]) -> dict[str, Any]:
        """Flatten a pathfinder playlist item into a friendly dict."""
        data = (item.get("itemV2") or {}).get("data") or {}
        if not data:
            return {
                "name": None,
                "uri": None,
                "artists": [],
                "album": None,
                "added_at": (item.get("addedAt") or {}).get("isoString"),
                "type": (item.get("itemV2") or {}).get("__typename"),
            }
        artists = [
            (a.get("profile") or {}).get("name")
            for a in ((data.get("artists") or {}).get("items") or [])
        ]
        album = data.get("albumOfTrack") or {}
        image = _cover_url(album)
        duration = (data.get("trackDuration") or {}).get("totalMilliseconds")
        return {
            "name": data.get("name"),
            "uri": data.get("uri"),
            "track_id": (data.get("uri") or "").split(":")[-1] or None,
            "artists": [a for a in artists if a],
            "album": album.get("name"),
            "album_uri": album.get("uri"),
            "duration_ms": duration,
            "added_at": (item.get("addedAt") or {}).get("isoString"),
            "playable": (data.get("playability") or {}).get("playable"),
            "image": image,
            "type": data.get("__typename"),
        }

    def search_liked(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Case-insensitive search over the liked-songs library."""
        query = (query or "").strip().lower()
        if not query:
            return []
        results = []
        for track in self.all_liked_songs():
            haystack = " ".join(
                [track.get("name") or ""] + list(track.get("artists") or []) + [track.get("album") or ""]
            ).lower()
            if query in haystack:
                results.append(track)
            if len(results) >= limit:
                break
        return results

    # ---------------------------------------------------------------- search
    def search_catalog(
        self, query: str, limit: int = 20, offset: int = 0
    ) -> dict[str, list[dict[str, Any]]]:
        """Search the whole Spotify catalog (tracks, artists, albums, playlists).

        This is the endpoint the web player uses for its search box - it works
        on a free account, unlike the public Web API.
        """
        query = (query or "").strip()
        if not query:
            return {"tracks": [], "artists": [], "albums": [], "playlists": [], "users": []}
        limit = max(1, min(int(limit), 50))
        variables = {
            "query": query,
            "limit": limit,
            "offset": max(0, int(offset)),
            # the web player always asks for a big top-results window, then
            # renders the first matches of every category
            "numberOfTopResults": 50,
            "includeArtistHasConcertsField": False,
            "includeAudiobooks": True,
            "includeAuthors": True,
            "includePreReleases": True,
            "includeAlbumPreReleases": False,
            "includeEpisodeContentRatingsV2": True,
            "isPrefix": None,
            "sectionFilters": ["GENERIC", "VIDEO_CONTENT"],
        }
        data = self._pathfinder("searchTopResultsList", variables, SHA_SEARCH)
        results = self._parse_search(data)
        return {key: value[:limit] for key, value in results.items()}

    @staticmethod
    def _parse_search(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """Flatten the pathfinder search payload into typed lists."""
        out: dict[str, list[dict[str, Any]]] = {
            "tracks": [],
            "artists": [],
            "albums": [],
            "playlists": [],
            "users": [],
        }
        search = data.get("searchV2") or {}
        items = (search.get("topResultsV2") or {}).get("itemsV2") or []
        for item in items:
            wrapper = item.get("item") or {}
            kind = wrapper.get("__typename")
            payload = wrapper.get("data") or {}
            if kind == "TrackResponseWrapper":
                out["tracks"].append(
                    {
                        "name": payload.get("name"),
                        "uri": payload.get("uri"),
                        "track_id": (payload.get("uri") or "").split(":")[-1] or None,
                        "artists": [
                            name
                            for name in (
                                (a.get("profile") or {}).get("name")
                                for a in ((payload.get("artists") or {}).get("items") or [])
                            )
                            if name
                        ],
                        "album": (payload.get("albumOfTrack") or {}).get("name"),
                        "duration_ms": (payload.get("trackDuration") or {}).get("totalMilliseconds"),
                        "image": _cover_url(payload.get("albumOfTrack")),
                    }
                )
            elif kind == "ArtistResponseWrapper":
                out["artists"].append(
                    {
                        "name": (payload.get("profile") or {}).get("name"),
                        "uri": payload.get("uri"),
                        "artist_id": (payload.get("uri") or "").split(":")[-1] or None,
                    }
                )
            elif kind == "AlbumResponseWrapper":
                out["albums"].append(
                    {
                        "name": payload.get("name"),
                        "uri": payload.get("uri"),
                        "artists": [
                            name
                            for name in (
                                (a.get("profile") or {}).get("name")
                                for a in ((payload.get("artists") or {}).get("items") or [])
                            )
                            if name
                        ],
                        "image": _cover_url(payload),
                    }
                )
            elif kind == "PlaylistResponseWrapper":
                out["playlists"].append(
                    {
                        "name": payload.get("name"),
                        "uri": payload.get("uri"),
                        "playlist_id": (payload.get("uri") or "").split(":")[-1] or None,
                        "description": (payload.get("description") or "")[:200] or None,
                    }
                )
            elif kind == "UserResponseWrapper":
                out["users"].append(
                    {
                        "name": payload.get("displayName"),
                        "uri": payload.get("uri"),
                    }
                )
        return out

    # ---------------------------------------------------------------- devices
    def cluster(self) -> dict[str, Any]:
        """Raw Spotify Connect cluster (devices + player state)."""
        response = self.session.get(
            f"{SPCLIENT}/connect-state/v1/cluster", headers=self._auth_headers(), timeout=20
        )
        if response.status_code != 200:
            raise SpotifyError(f"cluster request failed ({response.status_code})")
        return response.json()

    def devices(self) -> list[dict[str, Any]]:
        """Every Spotify Connect device visible to the account."""
        cluster = self.cluster()
        out = []
        for device_id, device in (cluster.get("devices") or {}).items():
            out.append(
                {
                    "id": device_id,
                    "name": device.get("name") or device_id[:12],
                    "brand": device.get("brand"),
                    "model": device.get("model"),
                    "type": device.get("type") or device.get("device_type"),
                    "is_active": device_id == cluster.get("active_device_id"),
                    "volume": device.get("volume"),
                    "volume_percent": (
                        round(device["volume"] / 65535 * 100) if device.get("volume") is not None else None
                    ),
                }
            )
        out.sort(key=lambda d: (not d["is_active"], (d["name"] or "").lower()))
        return out

    def active_device(self) -> dict[str, Any] | None:
        for device in self.devices():
            if device["is_active"]:
                return device
        return None

    def find_device(self, name: str) -> dict[str, Any]:
        """Resolve a device by (partial) name, id, brand or type."""
        needle = (name or "").strip().lower()
        devices = self.devices()
        if not needle:
            raise SpotifyError("no device name given")
        for device in devices:
            if device["id"].lower() == needle:
                return device
        matches = [
            device
            for device in devices
            if needle in (device["name"] or "").lower()
            or needle in (device["brand"] or "").lower()
            or needle in (device["model"] or "").lower()
            or needle in (device["type"] or "").lower()
        ]
        if not matches:
            available = ", ".join(f"{d['name']} ({d['type']})" for d in devices)
            raise SpotifyError(f"no device matching '{name}'. Available: {available}")
        return matches[0]

    # -------------------------------------------------------------- playback
    def _state_path(self) -> str:
        override = os.environ.get("SPOTIFY_STATE_FILE")
        if override:
            return override
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, "mcp-spotify-free", "device.json")

    def _load_state(self) -> None:
        """Reuse the Connect device of a previous run (keeps the device list clean)."""
        if self._device_id and self._connection_id:
            return
        try:
            with open(self._state_path(), "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return
        device_id = state.get("device_id")
        connection_id = state.get("connection_id")
        if device_id and connection_id:
            self._device_id = device_id
            self._connection_id = connection_id
        self._last_device_id = state.get("last_device_id") or self._last_device_id

    def _save_state(self) -> None:
        try:
            path = self._state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "device_id": self._device_id,
                        "connection_id": self._connection_id,
                        "last_device_id": self._last_device_id,
                    },
                    handle,
                )
        except OSError:
            pass

    def _register_device(self) -> str:
        """Register this script as a Spotify Connect device.

        The device id is *stable*: it is persisted and reused, so a re-run
        refreshes the same entry instead of piling up a new "device" in the
        account's device list on every call.
        """
        self._load_state()
        if not self._device_id:
            self._device_id = str(uuid.uuid4())
        self._connection_id = base64.b64encode(
            f"{uuid.uuid4()}+dealer={DEALER}+{DEALER_KEY}".encode("utf-8")
        ).decode("ascii")
        jar = self._cookie_jar()
        body = {
            "device": {
                "brand": "spotify",
                "capabilities": {
                    "change_volume": True,
                    "enable_play_token": True,
                    "supports_file_media_type": True,
                    "play_token_lost_behavior": "pause",
                    "disable_connect": False,
                    "audio_podcasts": True,
                    "video_playback": True,
                    "manifest_formats": [
                        "file_ids_mp3",
                        "file_urls_mp3",
                        "manifest_urls_audio_ad",
                        "manifest_ids_video",
                        "file_urls_external",
                        "file_ids_mp4",
                        "file_urls_mp4_dual",
                    ],
                    "supports_preferred_media_type": True,
                    "supports_playback_offsets": True,
                    "supports_playback_speed": True,
                },
                "device_id": self._device_id,
                "device_type": "computer",
                "metadata": {},
                "model": "web_player",
                "name": self.device_name,
                "platform_identifier": "web_player osx 10.15.7;desktop",
                "is_group": False,
                "is_public": False,
                "correlation_id": jar.get("sp_t", ""),
                "client_version": "harmony:4.81.1-1f3d48b7f",
            },
            "outro_endcontent_snooping": False,
            "connection_id": self._connection_id,
            "client_version": "harmony:4.81.1-1f3d48b7f",
            "volume": 65535,
        }
        response = self.session.post(
            f"{SPCLIENT}/track-playback/v1/devices",
            data=json.dumps(body).encode("utf-8"),
            headers=self._auth_headers(),
            timeout=20,
        )
        if response.status_code != 200:
            raise SpotifyError(f"device registration failed ({response.status_code})")
        self._save_state()
        self._subscribe()
        return self._device_id

    def _subscribe(self) -> None:
        """Subscribe to the connect-state feed of our device."""
        self.session.put(
            f"{SPCLIENT}/connect-state/v1/devices/hobs_{self._device_id}",
            data=json.dumps(
                {
                    "member_type": "CONNECT_STATE",
                    "device": {
                        "device_info": {
                            "capabilities": {
                                "can_be_player": False,
                                "hidden": True,
                                "needs_full_player_state": True,
                            }
                        }
                    },
                }
            ).encode("utf-8"),
            headers={
                **self._auth_headers(),
                "content-type": "application/json",
                "x-spotify-connection-id": self._connection_id,
            },
            timeout=20,
        )

    def _web_player_device(self) -> dict[str, Any] | None:
        """The account's own web-player device, if there is one.

        The web player already registered a Connect device ("Web Player
        (Microsoft Edge)").  Sending commands *from* that device id works and
        - unlike registering our own - leaves no trace in the account's device
        list, so the server never adds a device that has to be cleaned up.
        """
        for device in self.devices():
            name = (device.get("name") or "").lower()
            if name.startswith("web player"):
                return device
        return None

    def _source_device_id(self) -> str:
        """Device id used as the source of the Connect commands."""
        if self._source_id:
            return self._source_id
        web = self._web_player_device()
        if web:
            self._source_id = web["id"]
            return self._source_id
        self._source_id = self._register_device()
        return self._source_id

    def forget_device(self) -> bool:
        """Delete our own Connect device from the account (cleanup helper)."""
        self._load_state()
        if not self._device_id:
            return False
        ok = self._remove_device(self._device_id)
        if ok:
            self._device_id = None
            self._connection_id = None
            self._source_id = None
            try:
                os.unlink(self._state_path())
            except OSError:
                pass
        return ok

    def _remove_device(self, device_id: str) -> bool:
        """Hard-remove a Connect device from the account.

        ``DELETE /connect-state/v1/devices/{id}`` drops the entry from the
        account's device list right away.  The ``hobs_`` variant only drops the
        subscription and leaves a ghost entry visible for hours.
        """
        response = self.session.delete(
            f"{SPCLIENT}/connect-state/v1/devices/{device_id}",
            headers=self._auth_headers(),
            timeout=20,
        )
        return response.status_code in (200, 204, 404)

    def cleanup_devices(self, only_ours: bool = True) -> dict[str, Any]:
        """Remove the leftover "MCP Spotify Free" Connect devices of this server.

        The server no longer registers a device of its own (it reuses the
        account's web-player device), but earlier versions did - those entries
        are removed here.  The persisted device state is dropped too, so a
        future fallback registration starts from a clean slate.
        """
        removed: list[str] = []
        failed: list[str] = []
        for device in self.devices():
            if only_ours and (device["name"] or "") != self.device_name:
                continue
            if self._remove_device(device["id"]):
                removed.append(device["id"])
            else:
                failed.append(device["id"])
        self._load_state()
        if self._device_id and self._device_id not in removed:
            if self._remove_device(self._device_id):
                removed.append(self._device_id)
            self._device_id = None
            self._connection_id = None
            self._source_id = None
            try:
                os.unlink(self._state_path())
            except OSError:
                pass
        return {
            "removed": removed,
            "failed": failed,
            "note": (
                "Devices are removed from the account's device list right away; "
                "the server does not register a device of its own any more."
            ),
        }

    def _command(self, command: dict[str, Any], device_id: str) -> dict[str, Any]:
        """Send a harmony command to a device."""
        source = self._source_device_id()
        response = self._post_command(command, device_id, source)
        if response.status_code != 200 and source != self._device_id:
            # the account's web-player device may be stale -> use our own
            self._source_id = self._register_device()
            response = self._post_command(command, device_id, self._source_id)
        if response.status_code != 200:
            raise SpotifyError(f"command failed ({response.status_code}): {response.text[:200]}")
        self._remember_device(device_id)
        try:
            return response.json()
        except ValueError:
            return {"ack_id": None}

    def _remember_device(self, device_id: str) -> None:
        """Remember the last addressed device (used when Spotify reports none)."""
        if self._last_device_id == device_id:
            return
        self._last_device_id = device_id
        self._save_state()

    def _post_command(
        self, command: dict[str, Any], device_id: str, source_id: str
    ) -> Any:
        return self.session.post(
            f"{SPCLIENT}/connect-state/v1/player/command/from/{source_id}/to/{device_id}",
            data=json.dumps({"command": command}).encode("utf-8"),
            headers=self._auth_headers(),
            timeout=20,
        )

    def play_track(self, track_uri: str, device: str | None = None) -> dict[str, Any]:
        """Play a track (by Spotify URI) on the given device or the active one."""
        target = self._target_device(device)
        command = {
            "context": {
                "uri": track_uri,
                "url": f"context://{track_uri}",
                "metadata": {},
            },
            "play_origin": {
                "feature_identifier": "harmony",
                "feature_version": HARMONY_VERSION,
            },
            "options": {
                "license": "on-demand",
                "skip_to": {"track_index": 0},
                "player_options_override": {},
            },
            "endpoint": "play",
        }
        return self._command(command, target["id"])

    def simple_command(self, endpoint: str, device: str | None = None, **extra) -> dict[str, Any]:
        """Pause / resume / skip next / skip previous."""
        target = self._target_device(device)
        command = {"endpoint": endpoint}
        command.update(extra)
        return self._command(command, target["id"])

    def pause(self, device: str | None = None) -> dict[str, Any]:
        return self.simple_command("pause", device)

    def resume(self, device: str | None = None) -> dict[str, Any]:
        return self.simple_command("resume", device)

    def skip_next(self, device: str | None = None) -> dict[str, Any]:
        return self.simple_command("skip_next", device)

    def skip_previous(self, device: str | None = None) -> dict[str, Any]:
        return self.simple_command("skip_prev", device)

    def set_volume(self, level: int, device: str | None = None) -> dict[str, Any]:
        """Set the volume of a device (0-100)."""
        target = self._target_device(device)
        level = max(0, min(int(level), 100))
        source = self._source_device_id()
        response = self.session.put(
            f"{SPCLIENT}/connect-state/v1/connect/volume/from/{source}/to/{target['id']}",
            data=json.dumps({"volume": level * 65535 / 100}).encode("utf-8"),
            headers=self._auth_headers(),
            timeout=20,
        )
        if response.status_code not in (200, 204):
            raise SpotifyError(f"volume command failed ({response.status_code}): {response.text[:200]}")
        self._remember_device(target["id"])
        return {"device": target, "volume": level}

    def _target_device(self, device: str | None) -> dict[str, Any]:
        """Resolve the device a command must be sent to."""
        if device:
            return self.find_device(device)
        active = self.active_device()
        if active:
            return active
        # Spotify's cluster is served by several backends: "active_device_id"
        # is regularly null even while a device is playing.  The last device we
        # addressed is then the best guess.
        if self._last_device_id:
            for candidate in self.devices():
                if candidate["id"] == self._last_device_id:
                    return candidate
        raise SpotifyError(
            "no active Spotify device - is the Spotify app running? "
            "(pass a device name to target one explicitly)"
        )

    # ----------------------------------------------------------- now playing
    def now_playing(self, settle: float = 0.0) -> dict[str, Any]:
        """Current track + playback state.

        ``settle`` waits a moment before reading the state: right after a
        command the connect-state feed still serves the previous track.
        """
        if settle > 0:
            time.sleep(settle)
        cluster = self.cluster()
        state = cluster.get("player_state") or {}
        track = state.get("track") or {}
        meta = track.get("metadata") or {}
        active_id = cluster.get("active_device_id")
        active_name = None
        active_volume = None
        for device in self.devices():
            if device["id"] == active_id or (
                not active_id and device["id"] == self._last_device_id
            ):
                active_name = device["name"]
                active_volume = device.get("volume_percent")
        artists = meta.get("artists") or []
        if not artists and meta.get("artist_name"):
            artists = [meta["artist_name"]]
        position = state.get("position_as_of_timestamp")
        return {
            "track": {
                "name": meta.get("title") or meta.get("name") or None,
                "uri": track.get("uri"),
                "artists": artists,
                "album": meta.get("album_title") or None,
                "image": meta.get("image_large_url") or meta.get("image_url") or None,
                "duration_ms": int(meta["duration"]) if meta.get("duration") else None,
            },
            "context_uri": state.get("context_uri"),
            "is_playing": state.get("is_playing"),
            "is_paused": state.get("is_paused"),
            "position_ms": int(position) if position is not None else None,
            "volume": active_volume,
            "active_device": active_name,
            "active_device_id": active_id,
        }
