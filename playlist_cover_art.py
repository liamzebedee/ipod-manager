#!/usr/bin/env python3
"""Check and embed missing cover art for tracks in a Quod Libet XSPF playlist.

Fetches art from the iTunes Search API (searching by artist+title, then
artist+album) and embeds it into MP3/FLAC/M4A files via mutagen.

Usage:
    python3 playlist_cover_art.py PLAYLIST_NAME          # dry-run / status
    python3 playlist_cover_art.py PLAYLIST_NAME --apply  # fetch + embed
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

PLAYLIST_DIR = Path.home() / ".config/quodlibet/playlists"
XSPF_NS = {"x": "http://xspf.org/ns/0/"}


def playlist_tracks(xspf_path: Path):
    for t in ET.parse(xspf_path).getroot().findall("x:trackList/x:track", XSPF_NS):
        loc = t.find("x:location", XSPF_NS).text
        def _text(tag):
            el = t.find(f"x:{tag}", XSPF_NS)
            return el.text if el is not None else None
        path = urllib.parse.unquote(loc[len("file://"):])
        yield Path(path), _text("title"), _text("creator"), _text("album")


def has_cover(path: Path) -> bool:
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return False
            return any(k.startswith("APIC") for k in tags.keys())
        if ext == ".flac":
            return bool(FLAC(path).pictures)
        if ext in (".m4a", ".mp4"):
            tags = MP4(path).tags
            return bool(tags and tags.get("covr"))
    except Exception:
        return False
    return False


def itunes_art(artist, title, album=None):
    queries = []
    if artist and title:
        queries.append(f"{artist} {title}")
    if artist and album:
        queries.append(f"{artist} {album}")
    for q in queries:
        url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": q, "media": "music", "entity": "song", "limit": 5})
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"    itunes error for {q!r}: {e}")
            continue
        results = data.get("results", [])
        if not results:
            continue
        best = results[0]
        if title:
            t_low = title.lower()
            for r in results:
                if t_low in (r.get("trackName") or "").lower():
                    best = r
                    break
        art_url = best.get("artworkUrl100")
        if not art_url:
            continue
        art_url = art_url.replace("100x100bb.jpg", "600x600bb.jpg")
        try:
            with urllib.request.urlopen(art_url, timeout=15) as r:
                img = r.read()
            return img, best.get("artistName"), best.get("collectionName"), best.get("trackName")
        except Exception as e:
            print(f"    fetch error: {e}")
    return None, None, None, None


def embed(path: Path, img: bytes, mime: str = "image/jpeg"):
    ext = path.suffix.lower()
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        for k in [k for k in tags.keys() if k.startswith("APIC")]:
            del tags[k]
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=img))
        tags.save(path)
    elif ext == ".flac":
        f = FLAC(path)
        f.clear_pictures()
        pic = Picture()
        pic.type = 3
        pic.mime = mime
        pic.data = img
        f.add_picture(pic)
        f.save()
    elif ext in (".m4a", ".mp4"):
        f = MP4(path)
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        f.tags["covr"] = [MP4Cover(img, imageformat=fmt)]
        f.save()
    else:
        raise ValueError(f"unsupported format: {ext}")


def resolve_playlist(name: str) -> Path:
    candidates = [
        Path(name),
        PLAYLIST_DIR / name,
        PLAYLIST_DIR / f"{name}.xspf",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError(f"playlist not found: tried {candidates}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("playlist", help="playlist name (in ~/.config/quodlibet/playlists) or path to .xspf")
    ap.add_argument("--apply", action="store_true", help="actually fetch + embed (default: dry-run)")
    args = ap.parse_args()

    xspf = resolve_playlist(args.playlist)
    print(f"Playlist: {xspf}\n")

    needs = []
    for path, title, artist, album in playlist_tracks(xspf):
        if not path.exists():
            print(f"[NOTFOUND]  {path}")
            continue
        if has_cover(path):
            print(f"[OK]        {artist} - {title}")
            continue
        needs.append((path, title, artist, album))
        print(f"[MISSING]   {artist} - {title} ({album or 'single'})")

    if not needs:
        print("\nAll tracks have cover art.")
        return 0

    print(f"\n{len(needs)} track(s) need cover art\n")
    for path, title, artist, album in needs:
        print(f"• {artist} - {title}")
        img, a, c, t = itunes_art(artist, title, album)
        if not img:
            print("    NO RESULT")
            continue
        print(f"    → {a} - {c} - {t}  ({len(img)} bytes)")
        if args.apply:
            try:
                embed(path, img)
                print("    embedded ✓")
            except Exception as e:
                print(f"    EMBED FAILED: {e}")

    if not args.apply:
        print("\n(dry-run. Re-run with --apply to embed.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
