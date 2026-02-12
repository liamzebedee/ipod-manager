"""Local music library database backed by SQLite + mutagen metadata extraction."""
from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlparse

import mutagen
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.id3 import ID3
from PIL import Image, ImageOps


DB_DIR = Path.home() / ".local" / "share" / "ipod-manager"
DB_PATH = DB_DIR / "library.db"
CONVERT_DIR = DB_DIR / "converted"
QL_PLAYLISTS = Path.home() / ".config" / "quodlibet" / "playlists"
IPOD_FORMATS = {".mp3", ".m4a", ".aac", ".wav", ".aiff"}
XSPF_NS = {"x": "http://xspf.org/ns/0/"}


def _album_key(artist: str, album: str) -> str:
    """Canonical key for deduplicating artwork per album."""
    return artist.strip().lower() + "\x00" + album.strip().lower()


def _preprocess_artwork(raw_bytes: bytes) -> bytes:
    """Convert raw cover art to a 600x600 baseline JPEG optimized for iPod."""
    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB",):
        img = img.convert("RGB")
    # Center-crop to square
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    # Resize to 600x600 max
    if side > 600:
        img = img.resize((600, 600), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, progressive=False)
    return buf.getvalue()


SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    artist      TEXT NOT NULL DEFAULT '',
    album       TEXT NOT NULL DEFAULT '',
    genre       TEXT NOT NULL DEFAULT '',
    track_nr    INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    bitrate     INTEGER DEFAULT 0,
    filesize    INTEGER DEFAULT 0,
    cover_art   BLOB,
    synced      INTEGER DEFAULT 0,
    deleted     INTEGER DEFAULT 0,
    added_at    TEXT DEFAULT (datetime('now')),
    filetype    TEXT DEFAULT 'mp3'
);
CREATE TABLE IF NOT EXISTS playlists (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position    INTEGER DEFAULT 0,
    PRIMARY KEY (playlist_id, track_id)
);

CREATE TABLE IF NOT EXISTS ipod_artwork (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    album_key  TEXT UNIQUE NOT NULL,
    image_hash TEXT NOT NULL,
    image_data BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracks_deleted_synced ON tracks(deleted, synced);
CREATE INDEX IF NOT EXISTS idx_tracks_artist_album_track ON tracks(artist, album, track_nr);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track_id ON playlist_tracks(track_id);
"""


def extract_metadata(file_path: str) -> dict:
    """Read tags from an MP3 or M4A file. Returns a dict ready for DB insertion."""
    path = Path(file_path)
    ext = path.suffix.lower()
    meta = {
        "file_path": str(path.resolve()),
        "title": path.stem,
        "artist": "",
        "album": "",
        "genre": "",
        "track_nr": 0,
        "duration_ms": 0,
        "bitrate": 0,
        "filesize": path.stat().st_size,
        "cover_art": None,
        "filetype": ext.lstrip("."),
    }

    try:
        audio = mutagen.File(file_path)
    except Exception:
        return meta

    if audio is None:
        return meta

    meta["duration_ms"] = int((audio.info.length or 0) * 1000)
    meta["bitrate"] = getattr(audio.info, "bitrate", 0) or 0

    if ext == ".mp3":
        try:
            tags = ID3(file_path)
        except Exception:
            return meta
        meta["title"] = str(tags.get("TIT2", meta["title"]))
        meta["artist"] = str(tags.get("TPE1", ""))
        meta["album"] = str(tags.get("TALB", ""))
        meta["genre"] = str(tags.get("TCON", ""))
        trck = tags.get("TRCK")
        if trck:
            try:
                meta["track_nr"] = int(str(trck).split("/")[0])
            except ValueError:
                pass
        for key in tags:
            if key.startswith("APIC"):
                meta["cover_art"] = tags[key].data
                break

    elif ext == ".m4a":
        if hasattr(audio, "tags") and audio.tags:
            t = audio.tags
            meta["title"] = str(t.get("\xa9nam", [meta["title"]])[0])
            meta["artist"] = str(t.get("\xa9ART", [""])[0])
            meta["album"] = str(t.get("\xa9alb", [""])[0])
            meta["genre"] = str(t.get("\xa9gen", [""])[0])
            trkn = t.get("trkn")
            if trkn:
                meta["track_nr"] = trkn[0][0]
            covr = t.get("covr")
            if covr:
                meta["cover_art"] = bytes(covr[0])

    return meta


def _convert_one(src: str) -> tuple[str, str | None]:
    """Convert a single file to MP3 (worker function for parallel conversion).
    Returns (src, dest_path_or_None)."""
    CONVERT_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(src).stem
    dest = CONVERT_DIR / f"{stem}.mp3"
    if dest.exists():
        return src, str(dest)
    try:
        subprocess.run(
            ["ffmpeg", "-i", src, "-vn",
             "-codec:a", "libmp3lame", "-q:a", "2",
             "-map_metadata", "0", "-id3v2_version", "3",
             str(dest)],
            capture_output=True, check=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return src, None

    return src, str(dest)


def convert_to_mp3(src: str, progress_cb=None) -> str | None:
    """Convert a single file to MP3. For batch use, prefer convert_batch."""
    _, result = _convert_one(src)
    return result


def convert_batch(sources: list[str], progress_cb=None) -> dict[str, str | None]:
    """Convert multiple files to MP3 in parallel using all CPU cores.
    Returns {src_path: dest_path_or_None}.
    progress_cb(done, total, filename) is called as each finishes."""
    CONVERT_DIR.mkdir(parents=True, exist_ok=True)

    # Split into already-done and need-conversion
    results = {}
    to_convert = []
    for src in sources:
        dest = CONVERT_DIR / f"{Path(src).stem}.mp3"
        if dest.exists():
            results[src] = str(dest)
        else:
            to_convert.append(src)

    if not to_convert:
        return results

    done = len(results)
    total = len(sources)
    workers = os.cpu_count() or 4

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_convert_one, src): src for src in to_convert}
        for future in as_completed(futures):
            src, dest = future.result()
            results[src] = dest
            done += 1
            if progress_cb:
                progress_cb(done, total, Path(src).name)

    return results


def discover_ql_playlists() -> list[dict]:
    """Find all Quod Libet playlists. Returns [{name, path, track_count}]."""
    results = []
    if not QL_PLAYLISTS.is_dir():
        return results
    for f in sorted(QL_PLAYLISTS.iterdir()):
        if f.suffix.lower() != ".xspf":
            continue
        try:
            tree = ET.parse(f)
            root = tree.getroot()
            title_el = root.find("x:title", XSPF_NS)
            name = title_el.text if title_el is not None else f.stem
            tracks = root.findall(".//x:track", XSPF_NS)
            results.append({"name": name, "path": str(f),
                            "track_count": len(tracks)})
        except ET.ParseError:
            continue
    return results


def parse_xspf(playlist_path: str) -> list[str]:
    """Parse an XSPF playlist, return list of local file paths."""
    tree = ET.parse(playlist_path)
    paths = []
    for track in tree.findall(".//x:track", XSPF_NS):
        loc = track.find("x:location", XSPF_NS)
        if loc is None or loc.text is None:
            continue
        parsed = urlparse(loc.text)
        if parsed.scheme == "file":
            paths.append(unquote(parsed.path))
    return paths


class LibraryDB:
    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate_schema()
        self.backfill_artwork()

    def _migrate_schema(self):
        """Add ipod_artwork_id column to tracks if missing."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tracks)").fetchall()}
        if "ipod_artwork_id" not in cols:
            self.conn.execute(
                "ALTER TABLE tracks ADD COLUMN ipod_artwork_id INTEGER "
                "REFERENCES ipod_artwork(id)")
            self.conn.commit()
            self._backfill_ipod_artwork()

    def _backfill_ipod_artwork(self):
        """One-time migration: generate iPod artwork for all existing albums."""
        reps = self.conn.execute(
            "SELECT MIN(id) as rep_id, artist, album FROM tracks "
            "WHERE cover_art IS NOT NULL AND deleted=0 "
            "GROUP BY artist, album"
        ).fetchall()
        if not reps:
            return
        # Fetch cover_art blobs for representative tracks
        rep_ids = [r["rep_id"] for r in reps]
        placeholders = ",".join("?" * len(rep_ids))
        art_rows = self.conn.execute(
            f"SELECT id, cover_art FROM tracks WHERE id IN ({placeholders})",
            rep_ids).fetchall()
        art_by_id = {r["id"]: r["cover_art"] for r in art_rows}

        # Build work items: (album_key, hash, raw_bytes)
        work = []
        for r in reps:
            raw = art_by_id.get(r["rep_id"])
            if not raw:
                continue
            key = _album_key(r["artist"], r["album"])
            h = hashlib.sha256(raw).hexdigest()
            work.append((key, h, raw, r["artist"], r["album"]))

        # Parallel preprocessing
        workers = min(os.cpu_count() or 4, len(work))
        processed = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_preprocess_artwork, w[2]): w for w in work}
            for fut in as_completed(futures):
                w = futures[fut]
                try:
                    processed[w[0]] = (w[0], w[1], fut.result())
                except Exception:
                    continue

        # Batch insert into ipod_artwork
        insert_data = [(p[0], p[1], p[2]) for p in processed.values()]
        self.conn.executemany(
            "INSERT OR IGNORE INTO ipod_artwork (album_key, image_hash, image_data) "
            "VALUES (?,?,?)", insert_data)

        # Build album_key → ipod_artwork.id mapping
        all_art = self.conn.execute("SELECT id, album_key FROM ipod_artwork").fetchall()
        key_to_id = {r["album_key"]: r["id"] for r in all_art}

        # Batch update tracks
        update_data = []
        for w in work:
            art_id = key_to_id.get(w[0])
            if art_id:
                update_data.append((art_id, w[3], w[4]))
        self.conn.executemany(
            "UPDATE tracks SET ipod_artwork_id=? WHERE artist=? AND album=?",
            update_data)
        self.conn.commit()

    def _get_or_create_ipod_artwork(self, artist: str, album: str, raw_data: bytes) -> int:
        """Get or create iPod-optimized artwork for an album. Returns ipod_artwork.id."""
        key = _album_key(artist, album)
        h = hashlib.sha256(raw_data).hexdigest()
        row = self.conn.execute(
            "SELECT id, image_hash FROM ipod_artwork WHERE album_key=?", (key,)
        ).fetchone()
        if row:
            if row["image_hash"] == h:
                return row["id"]
            # Art changed — reprocess and update
            processed = _preprocess_artwork(raw_data)
            self.conn.execute(
                "UPDATE ipod_artwork SET image_hash=?, image_data=? WHERE id=?",
                (h, processed, row["id"]))
            return row["id"]
        # New album art
        processed = _preprocess_artwork(raw_data)
        cur = self.conn.execute(
            "INSERT INTO ipod_artwork (album_key, image_hash, image_data) VALUES (?,?,?)",
            (key, h, processed))
        return cur.lastrowid

    def add_track(self, file_path: str) -> int | None:
        """Extract metadata and insert. Returns row id, or None if duplicate."""
        meta = extract_metadata(file_path)
        try:
            cur = self.conn.execute(
                """INSERT INTO tracks
                   (file_path, title, artist, album, genre, track_nr,
                    duration_ms, bitrate, filesize, cover_art, filetype)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (meta["file_path"], meta["title"], meta["artist"], meta["album"],
                 meta["genre"], meta["track_nr"], meta["duration_ms"],
                 meta["bitrate"], meta["filesize"], meta["cover_art"],
                 meta["filetype"]),
            )
            track_id = cur.lastrowid
            if meta["cover_art"]:
                art_id = self._get_or_create_ipod_artwork(
                    meta["artist"], meta["album"], meta["cover_art"])
                self.conn.execute(
                    "UPDATE tracks SET ipod_artwork_id=? WHERE id=?", (art_id, track_id))
            self.conn.commit()
            return track_id
        except sqlite3.IntegrityError:
            return None

    def add_track_from_meta(self, meta: dict) -> int | None:
        """Insert a track from pre-extracted metadata. Returns row id or None."""
        try:
            cur = self.conn.execute(
                """INSERT INTO tracks
                   (file_path, title, artist, album, genre, track_nr,
                    duration_ms, bitrate, filesize, cover_art, filetype)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (meta["file_path"], meta["title"], meta["artist"], meta["album"],
                 meta["genre"], meta["track_nr"], meta["duration_ms"],
                 meta["bitrate"], meta["filesize"], meta["cover_art"],
                 meta["filetype"]),
            )
            track_id = cur.lastrowid
            if meta.get("cover_art"):
                art_id = self._get_or_create_ipod_artwork(
                    meta["artist"], meta["album"], meta["cover_art"])
                self.conn.execute(
                    "UPDATE tracks SET ipod_artwork_id=? WHERE id=?", (art_id, track_id))
            self.conn.commit()
            return track_id
        except sqlite3.IntegrityError:
            return None

    def remove_track(self, track_id: int):
        """Mark a track as deleted (soft delete)."""
        self.conn.execute("UPDATE tracks SET deleted=1 WHERE id=?", (track_id,))
        self.conn.commit()

    def get_or_create_playlist(self, name: str) -> int:
        """Return playlist id, creating if needed."""
        row = self.conn.execute(
            "SELECT id FROM playlists WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO playlists (name) VALUES (?)", (name,))
        self.conn.commit()
        return cur.lastrowid

    def add_track_to_playlist(self, playlist_id: int, track_id: int, position: int = 0):
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) "
                "VALUES (?,?,?)", (playlist_id, track_id, position))
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def get_all_tracks(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tracks WHERE deleted=0 ORDER BY artist, album, track_nr"
        ).fetchall()

    def get_playlist_tracks(self, playlist_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT t.* FROM tracks t
               JOIN playlist_tracks pt ON pt.track_id = t.id
               WHERE pt.playlist_id = ? AND t.deleted = 0
               ORDER BY pt.position, t.artist, t.album, t.track_nr""",
            (playlist_id,)).fetchall()

    def get_unsynced(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tracks WHERE synced=0 AND deleted=0"
        ).fetchall()

    def get_deleted(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tracks WHERE deleted=1 AND synced=1"
        ).fetchall()

    def mark_synced(self, track_id: int):
        self.conn.execute("UPDATE tracks SET synced=1 WHERE id=?", (track_id,))
        self.conn.commit()

    def mark_synced_batch(self, track_ids: list[int]):
        """Mark multiple tracks as synced in a single transaction."""
        self.conn.executemany(
            "UPDATE tracks SET synced=1 WHERE id=?",
            [(tid,) for tid in track_ids])
        self.conn.commit()

    def backfill_artwork(self):
        """Re-extract cover art for tracks missing it, then derive iPod artwork."""
        # Phase 1: backfill cover_art from source files
        rows = self.conn.execute(
            "SELECT id, file_path FROM tracks WHERE cover_art IS NULL AND deleted=0"
        ).fetchall()
        for row in rows:
            if not Path(row["file_path"]).exists():
                continue
            meta = extract_metadata(row["file_path"])
            if meta["cover_art"] is not None:
                self.conn.execute(
                    "UPDATE tracks SET cover_art=? WHERE id=?",
                    (meta["cover_art"], row["id"]))
        if rows:
            self.conn.commit()

        # Phase 2: derive iPod artwork for tracks missing ipod_artwork_id
        need_art = self.conn.execute(
            "SELECT id, artist, album, cover_art FROM tracks "
            "WHERE ipod_artwork_id IS NULL AND cover_art IS NOT NULL AND deleted=0"
        ).fetchall()
        if not need_art:
            return
        # Deduplicate by album — only process once per unique (artist, album)
        seen_keys = {}
        update_data = []
        for row in need_art:
            key = _album_key(row["artist"], row["album"])
            if key not in seen_keys:
                art_id = self._get_or_create_ipod_artwork(
                    row["artist"], row["album"], row["cover_art"])
                seen_keys[key] = art_id
            update_data.append((seen_keys[key], row["id"]))
        self.conn.executemany(
            "UPDATE tracks SET ipod_artwork_id=? WHERE id=?", update_data)
        self.conn.commit()

    def purge_deleted(self):
        """Remove soft-deleted tracks that have been un-synced from iPod."""
        self.conn.execute("DELETE FROM tracks WHERE deleted=1")
        self.conn.commit()

    def close(self):
        self.conn.close()
