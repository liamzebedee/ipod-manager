"""Dedupe tracks in the iPod's iTunesDB in place.

Groups tracks by (title, artist, album), keeps the first, removes the rest from
all playlists and from the track list, deletes the orphaned audio files, and
writes the DB back.
"""
from __future__ import annotations

import ctypes
import os
import sys
from collections import defaultdict

from ipod_sync import (
    Itdb_Playlist,
    Itdb_Track,
    _glist_foreach,
    _str_at,
    iPodSync,
    libgpod,
)


def main() -> int:
    sync = iPodSync()
    mp = sync.detect_ipod()
    if not mp:
        print("No iPod detected", file=sys.stderr)
        return 1
    print(f"iPod: {mp}")

    itdb = libgpod.itdb_parse(mp.encode("utf-8"), None)
    if not itdb:
        print("Failed to parse iTunesDB", file=sys.stderr)
        return 1

    try:
        # Group tracks by (title, artist, album)
        groups: dict[tuple[str, str, str], list] = defaultdict(list)
        all_tracks = []
        for tptr in _glist_foreach(itdb[0].tracks, ctypes.POINTER(Itdb_Track)):
            key = (
                _str_at(tptr[0].title),
                _str_at(tptr[0].artist),
                _str_at(tptr[0].album),
            )
            groups[key].append(tptr)
            all_tracks.append((key, tptr))

        # Collect duplicates: keep first, remove rest
        dupes = []
        for key, ptrs in groups.items():
            if len(ptrs) > 1:
                dupes.extend(ptrs[1:])

        if not dupes:
            print("No duplicates found.")
            return 0

        print(f"Found {len(dupes)} duplicate track entries across "
              f"{sum(1 for g in groups.values() if len(g) > 1)} albums/tracks.")

        # Gather ipod_path (relative, colon-separated) for each dupe BEFORE removal
        dupe_ptrs = set()
        dupe_paths: list[str] = []
        for tptr in dupes:
            dupe_ptrs.add(ctypes.addressof(tptr[0]))
            rel = _str_at(tptr[0].ipod_path)
            if rel:
                # libgpod uses ':' as separator on-disk
                rel_fs = rel.replace(":", "/").lstrip("/")
                dupe_paths.append(os.path.join(mp, rel_fs))

        # Remove dupes from every playlist
        for pptr in _glist_foreach(itdb[0].playlists,
                                   ctypes.POINTER(Itdb_Playlist)):
            to_pull = []
            for tptr in _glist_foreach(pptr[0].members,
                                       ctypes.POINTER(Itdb_Track)):
                if ctypes.addressof(tptr[0]) in dupe_ptrs:
                    to_pull.append(tptr)
            for tptr in to_pull:
                libgpod.itdb_playlist_remove_track(pptr, tptr)

        # Remove dupes from the main track list
        for tptr in dupes:
            libgpod.itdb_track_remove(tptr)

        # Write DB
        print("Writing iTunesDB...")
        ok = libgpod.itdb_write(itdb, None)
        if not ok:
            print("itdb_write failed", file=sys.stderr)
            return 1

        # Delete orphaned audio files
        deleted = 0
        for p in dupe_paths:
            try:
                os.unlink(p)
                deleted += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"  could not delete {p}: {e}", file=sys.stderr)
        print(f"Removed {len(dupes)} DB entries, deleted {deleted} audio files.")
        return 0
    finally:
        libgpod.itdb_free(itdb)


if __name__ == "__main__":
    sys.exit(main())
