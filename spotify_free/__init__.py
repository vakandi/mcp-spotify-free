"""Spotify control for FREE accounts - MCP server package.

Public API::

    from spotify_free import SpotifyFreeClient

    client = SpotifyFreeClient()
    tracks = client.liked_songs(limit=20)
    client.play_track(tracks[0]["uri"], device="Chromecast")
"""

from .client import (
    LIKED_SONGS_PLAYLIST_ID,
    YOUR_EPISODES_PLAYLIST_ID,
    SpotifyError,
    SpotifyFreeClient,
)

__all__ = [
    "LIKED_SONGS_PLAYLIST_ID",
    "YOUR_EPISODES_PLAYLIST_ID",
    "SpotifyError",
    "SpotifyFreeClient",
]

__version__ = "1.1.1"
