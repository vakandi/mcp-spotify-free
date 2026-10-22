#!/usr/bin/env python3
"""Dependency-free demo of mcp-spotify-free.

    python3 examples/quickstart.py            # read-only tour
    python3 examples/quickstart.py play      # also play something
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spotify_free import SpotifyFreeClient  # noqa: E402


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "read").lower()
    client = SpotifyFreeClient()

    print("account session :", "ok" if client.access_token() else "failed")
    print("liked songs     :", client.liked_songs_count())

    print("\ndevices")
    for device in client.devices():
        marker = "*" if device["is_active"] else " "
        print(
            f" {marker} {device['name']:<32} {str(device['type']):<10} "
            f"vol {device['volume_percent']}%"
        )

    print("\n10 most recent liked songs")
    for index, track in enumerate(client.liked_songs(limit=10), start=1):
        artists = ", ".join(track["artists"])
        print(f" {index:>2}. {track['name']} - {artists}  [{track['added_at'][:10]}]")

    if mode == "play":
        target = os.environ.get("SPOTIFY_DEVICE", "Chromecast")
        track = client.liked_songs(limit=1)[0]
        print(f"\nplaying '{track['name']}' on {target} ...")
        client.play_track(track["uri"], device=target)
        time.sleep(2)
        print("now playing:", client.now_playing())
        client.pause()
        print("paused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
