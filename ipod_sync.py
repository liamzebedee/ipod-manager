"""libgpod ctypes wrapper for syncing tracks to an iPod Classic and ejecting."""

import ctypes
import ctypes.util
import os
import re
import subprocess
from pathlib import Path

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio

# ---------------------------------------------------------------------------
# Load libraries
# ---------------------------------------------------------------------------
libgpod = ctypes.CDLL("libgpod.so.4")
libglib = ctypes.CDLL("libglib-2.0.so.0")

# ---------------------------------------------------------------------------
# Primitive type aliases
# ---------------------------------------------------------------------------
time_t = getattr(ctypes, "c_time_t", ctypes.c_int64)
gboolean = ctypes.c_int

# ---------------------------------------------------------------------------
# g_strdup / g_free  (g_strdup MUST return c_void_p, not c_char_p)
# ---------------------------------------------------------------------------
libglib.g_strdup.argtypes = (ctypes.c_char_p,)
libglib.g_strdup.restype = ctypes.c_void_p

libglib.g_free.argtypes = (ctypes.c_void_p,)
libglib.g_free.restype = None

# ---------------------------------------------------------------------------
# Struct forward declarations
# ---------------------------------------------------------------------------
class GList(ctypes.Structure):
    pass

GList._fields_ = [
    ("data", ctypes.c_void_p),
    ("next", ctypes.POINTER(GList)),
    ("prev", ctypes.POINTER(GList)),
]


class Itdb_iTunesDB(ctypes.Structure):
    _fields_ = [
        ("tracks", ctypes.POINTER(GList)),
    ]


class Itdb_Chapterdata(ctypes.Structure):
    _fields_ = [("dummy", ctypes.c_void_p)]


class Itdb_Playlist(ctypes.Structure):
    _fields_ = [
        ("itdb", ctypes.POINTER(Itdb_iTunesDB)),
        ("name", ctypes.c_char_p),
        ("type", ctypes.c_uint8),
        ("flag1", ctypes.c_uint8),
        ("flag2", ctypes.c_uint8),
        ("flag3", ctypes.c_uint8),
        ("num", ctypes.c_int),
        ("members", ctypes.POINTER(GList)),
    ]


class Itdb_Track(ctypes.Structure):
    _fields_ = [
        ("itdb", ctypes.POINTER(Itdb_iTunesDB)),
        ("title", ctypes.c_void_p),
        ("ipod_path", ctypes.c_void_p),
        ("album", ctypes.c_void_p),
        ("artist", ctypes.c_void_p),
        ("genre", ctypes.c_void_p),
        ("filetype", ctypes.c_void_p),
        ("comment", ctypes.c_void_p),
        ("category", ctypes.c_void_p),
        ("composer", ctypes.c_void_p),
        ("grouping", ctypes.c_void_p),
        ("description", ctypes.c_void_p),
        ("podcasturl", ctypes.c_void_p),
        ("podcastrss", ctypes.c_void_p),
        ("chapterdata", ctypes.POINTER(Itdb_Chapterdata)),
        ("subtitle", ctypes.c_void_p),
        ("tvshow", ctypes.c_void_p),
        ("tvepisode", ctypes.c_void_p),
        ("tvnetwork", ctypes.c_void_p),
        ("albumartist", ctypes.c_void_p),
        ("keywords", ctypes.c_void_p),
        ("sort_artist", ctypes.c_void_p),
        ("sort_title", ctypes.c_void_p),
        ("sort_album", ctypes.c_void_p),
        ("sort_albumartist", ctypes.c_void_p),
        ("sort_composer", ctypes.c_void_p),
        ("sort_tvshow", ctypes.c_void_p),
        # --- numeric fields ---
        ("id", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("tracklen", ctypes.c_int32),
        ("cd_nr", ctypes.c_int32),
        ("cds", ctypes.c_int32),
        ("track_nr", ctypes.c_int32),
        ("bitrate", ctypes.c_int32),
        ("samplerate", ctypes.c_uint16),
        ("samplerate_low", ctypes.c_uint16),
        ("year", ctypes.c_int32),
        ("volume", ctypes.c_int32),
        ("soundcheck", ctypes.c_uint32),
        ("soundcheck2", ctypes.c_uint32),
        ("time_added", time_t),
        ("time_modified", time_t),
        ("time_played", time_t),
        ("bookmark_time", ctypes.c_uint32),
        ("rating", ctypes.c_uint32),
        ("playcount", ctypes.c_uint32),
        ("playcount2", ctypes.c_uint32),
        ("recent_playcount", ctypes.c_uint32),
        ("transferred", gboolean),
        ("BPM", ctypes.c_int16),
        ("app_rating", ctypes.c_uint8),
        ("type1", ctypes.c_uint8),
        ("type2", ctypes.c_uint8),
        ("compilation", ctypes.c_uint8),
        ("starttime", ctypes.c_uint32),
        ("stoptime", ctypes.c_uint32),
        ("checked", ctypes.c_uint8),
        ("dbid", ctypes.c_uint64),
        ("drm_userid", ctypes.c_uint32),
        ("visible", ctypes.c_uint32),
        ("filetype_marker", ctypes.c_uint32),
        ("artwork_count", ctypes.c_uint16),
        ("artwork_size", ctypes.c_uint32),
        ("samplerate2", ctypes.c_float),
        ("unk126", ctypes.c_uint16),
        ("unk132", ctypes.c_uint32),
        ("time_released", time_t),
        ("unk144", ctypes.c_uint16),
        ("explicit_flag", ctypes.c_uint16),
        ("unk148", ctypes.c_uint32),
        ("unk152", ctypes.c_uint32),
        ("skipcount", ctypes.c_uint32),
        ("recent_skipcount", ctypes.c_uint32),
        ("last_skipped", ctypes.c_uint32),
        ("has_artwork", ctypes.c_uint8),
        ("skip_when_shuffling", ctypes.c_uint8),
        ("remember_playback_position", ctypes.c_uint8),
        ("flag4", ctypes.c_uint8),
        ("dbid2", ctypes.c_uint64),
        ("lyrics_flag", ctypes.c_uint8),
        ("movie_flag", ctypes.c_uint8),
        ("mark_unplayed", ctypes.c_uint8),
        ("unk179", ctypes.c_uint8),
        ("unk180", ctypes.c_uint32),
        ("pregap", ctypes.c_uint32),
        ("samplecount", ctypes.c_uint64),
        ("unk196", ctypes.c_uint32),
        ("postgap", ctypes.c_uint32),
        ("unk204", ctypes.c_uint32),
        ("mediatype", ctypes.c_uint32),
    ]


# ---------------------------------------------------------------------------
# libgpod function signatures
# ---------------------------------------------------------------------------
libgpod.itdb_parse.argtypes = (ctypes.c_char_p, ctypes.c_void_p)
libgpod.itdb_parse.restype = ctypes.POINTER(Itdb_iTunesDB)

libgpod.itdb_init_ipod.argtypes = (ctypes.c_char_p, ctypes.c_char_p,
                                    ctypes.c_char_p, ctypes.c_void_p)
libgpod.itdb_init_ipod.restype = gboolean

libgpod.itdb_playlist_mpl.argtypes = (ctypes.POINTER(Itdb_iTunesDB),)
libgpod.itdb_playlist_mpl.restype = ctypes.POINTER(Itdb_Playlist)

libgpod.itdb_track_new.argtypes = ()
libgpod.itdb_track_new.restype = ctypes.POINTER(Itdb_Track)

libgpod.itdb_track_add.argtypes = (ctypes.POINTER(Itdb_iTunesDB),
                                    ctypes.POINTER(Itdb_Track), ctypes.c_int32)
libgpod.itdb_track_add.restype = None

libgpod.itdb_playlist_add_track.argtypes = (ctypes.POINTER(Itdb_Playlist),
                                             ctypes.POINTER(Itdb_Track), ctypes.c_int32)
libgpod.itdb_playlist_add_track.restype = None

libgpod.itdb_cp_track_to_ipod.argtypes = (ctypes.POINTER(Itdb_Track),
                                            ctypes.c_char_p, ctypes.c_void_p)
libgpod.itdb_cp_track_to_ipod.restype = gboolean

libgpod.itdb_track_set_thumbnails_from_data.argtypes = (
    ctypes.POINTER(Itdb_Track), ctypes.c_char_p, ctypes.c_size_t)
libgpod.itdb_track_set_thumbnails_from_data.restype = gboolean

libgpod.itdb_playlist_remove_track.argtypes = (ctypes.POINTER(Itdb_Playlist),
                                                ctypes.POINTER(Itdb_Track))
libgpod.itdb_playlist_remove_track.restype = None

libgpod.itdb_track_remove.argtypes = (ctypes.POINTER(Itdb_Track),)
libgpod.itdb_track_remove.restype = None

libgpod.itdb_write.argtypes = (ctypes.POINTER(Itdb_iTunesDB), ctypes.c_void_p)
libgpod.itdb_write.restype = gboolean

libgpod.itdb_free.argtypes = (ctypes.POINTER(Itdb_iTunesDB),)
libgpod.itdb_free.restype = None

ITDB_MEDIATYPE_AUDIO = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _glist_foreach(glist_ptr, cast_type):
    """Iterate a GList*, yielding ctypes-casted items."""
    cur = glist_ptr
    while cur:
        yield ctypes.cast(cur[0].data, cast_type)
        nxt = cur[0].next
        if not nxt:
            break
        cur = nxt


def _str_at(void_ptr) -> str:
    """Read a UTF-8 string from a c_void_p, returning '' if NULL."""
    if not void_ptr:
        return ""
    return ctypes.string_at(void_ptr).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# iPodSync
# ---------------------------------------------------------------------------
class iPodSync:
    def __init__(self):
        self.mountpoint: str | None = None

    def detect_ipod(self) -> str | None:
        """Scan Gio mounts for an iPod (looks for iPod_Control directory)."""
        vm = Gio.VolumeMonitor.get()
        for mount in vm.get_mounts():
            root = mount.get_root()
            if root is None:
                continue
            path = root.get_path()
            if path and os.path.isdir(os.path.join(path, "iPod_Control")):
                self.mountpoint = path
                self._ensure_firewire_guid(path)
                return path
        # Fallback: check common mount point
        fallback = "/media/liam/IPOD"
        if os.path.isdir(os.path.join(fallback, "iPod_Control")):
            self.mountpoint = fallback
            self._ensure_firewire_guid(fallback)
            return fallback
        self.mountpoint = None
        return None

    def needs_init(self) -> bool:
        """Check if the iPod needs SysInfoExtended for hash generation."""
        if not self.mountpoint:
            return False
        ext = os.path.join(self.mountpoint, "iPod_Control", "Device",
                           "SysInfoExtended")
        return not os.path.isfile(ext)

    def initialise(self) -> tuple[bool, str]:
        """Run ipod-read-sysinfo-extended via pkexec to set up the iPod.
        Returns (success, message)."""
        if not self.mountpoint:
            return False, "No iPod detected"

        # Find the block device from /proc/mounts
        block_dev = None
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == self.mountpoint:
                        block_dev = parts[0]
                        break
        except OSError:
            pass

        if not block_dev:
            # Fallback: strip partition number to get whole disk
            block_dev = "/dev/sda"

        # Strip partition (e.g. /dev/sda1 -> /dev/sda) for SCSI query
        disk_dev = re.sub(r'\d+$', '', block_dev)

        try:
            r = subprocess.run(
                ["pkexec", "ipod-read-sysinfo-extended", disk_dev,
                 self.mountpoint],
                capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return False, "Timed out waiting for authorisation"
        except FileNotFoundError:
            return False, "pkexec or ipod-read-sysinfo-extended not found"

        ext = os.path.join(self.mountpoint, "iPod_Control", "Device",
                           "SysInfoExtended")
        if os.path.isfile(ext):
            # Also ensure FirewireGuid in SysInfo
            self._ensure_firewire_guid(self.mountpoint)
            return True, "iPod initialised successfully"
        else:
            msg = r.stderr.strip() or r.stdout.strip() or "Unknown error"
            return False, f"Initialisation failed: {msg}"

    @staticmethod
    def _ensure_firewire_guid(mountpoint: str):
        """iPod Classic requires FirewireGuid in SysInfo for DB hash.
        Auto-detect from lsusb if missing."""
        sysinfo = os.path.join(mountpoint, "iPod_Control", "Device", "SysInfo")
        if os.path.isfile(sysinfo):
            with open(sysinfo) as f:
                contents = f.read()
            if "FirewireGuid" in contents:
                return
        else:
            contents = ""

        # Get serial from lsusb
        try:
            r = subprocess.run(["lsusb", "-v"], capture_output=True,
                               text=True, timeout=5)
            out = r.stderr + r.stdout
        except Exception:
            return
        # Find 16-char hex serial from Apple device
        serials = re.findall(r"iSerial\s+\d+\s+([0-9A-Fa-f]{16})\b", out)
        if not serials:
            return

        guid = serials[0]
        line = f"FirewireGuid: 0x{guid}\n"
        with open(sysinfo, "a") as f:
            f.write(line)
        print(f"Auto-added FirewireGuid: 0x{guid} to SysInfo")

    def sync(self, db, progress_cb=None):
        """Sync unsynced tracks to iPod, remove deleted tracks, write DB.

        Args:
            db: LibraryDB instance
            progress_cb: callable(fraction: float, message: str) for UI updates
        """
        if not self.mountpoint:
            raise RuntimeError("No iPod detected")

        mp = self.mountpoint.encode("utf-8")

        # Parse or init the iPod database
        itdb = libgpod.itdb_parse(mp, None)
        if not itdb:
            ok = libgpod.itdb_init_ipod(mp, None, b"iPod", None)
            if not ok:
                raise RuntimeError("Failed to initialise iPod database")
            itdb = libgpod.itdb_parse(mp, None)
            if not itdb:
                raise RuntimeError("Failed to parse iPod database after init")

        mpl = libgpod.itdb_playlist_mpl(itdb)
        if not mpl:
            libgpod.itdb_free(itdb)
            raise RuntimeError("No master playlist found on iPod")

        # --- Add unsynced tracks ---
        unsynced = db.get_unsynced()
        total = len(unsynced) + len(db.get_deleted())
        done = 0

        for row in unsynced:
            src = row["file_path"]
            if not os.path.isfile(src):
                done += 1
                continue

            if progress_cb:
                progress_cb(done / max(total, 1), f"Copying: {row['title']}")

            track = libgpod.itdb_track_new()
            t = track[0]
            t.title = libglib.g_strdup(row["title"].encode("utf-8"))
            t.artist = libglib.g_strdup(row["artist"].encode("utf-8"))
            t.album = libglib.g_strdup(row["album"].encode("utf-8"))
            t.genre = libglib.g_strdup(row["genre"].encode("utf-8"))
            t.filetype = libglib.g_strdup(row["filetype"].encode("utf-8"))
            t.track_nr = row["track_nr"]
            t.tracklen = row["duration_ms"]
            t.bitrate = row["bitrate"] // 1000 if row["bitrate"] > 1000 else row["bitrate"]
            t.size = row["filesize"]
            t.mediatype = ITDB_MEDIATYPE_AUDIO

            libgpod.itdb_track_add(itdb, track, -1)
            libgpod.itdb_playlist_add_track(mpl, track, -1)

            ok = libgpod.itdb_cp_track_to_ipod(track, src.encode("utf-8"), None)
            if not ok:
                print(f"Warning: failed to copy {src} to iPod")

            # Cover art
            cover = row["cover_art"]
            if cover:
                libgpod.itdb_track_set_thumbnails_from_data(
                    track, cover, len(cover))

            db.mark_synced(row["id"])
            done += 1

        # --- Remove deleted tracks from iPod ---
        deleted = db.get_deleted()
        # Build lookup set of (title, artist, album) to remove
        to_remove = {(r["title"], r["artist"], r["album"]) for r in deleted}

        if to_remove:
            # Collect iPod tracks matching deleted entries
            tracks_to_remove = []
            for tptr in _glist_foreach(itdb[0].tracks, ctypes.POINTER(Itdb_Track)):
                key = (_str_at(tptr[0].title),
                       _str_at(tptr[0].artist),
                       _str_at(tptr[0].album))
                if key in to_remove:
                    tracks_to_remove.append(tptr)

            for tptr in tracks_to_remove:
                if progress_cb:
                    progress_cb(done / max(total, 1),
                                f"Removing: {_str_at(tptr[0].title)}")
                libgpod.itdb_playlist_remove_track(mpl, tptr)
                libgpod.itdb_track_remove(tptr)
                done += 1

            db.purge_deleted()

        # --- Write and free ---
        if progress_cb:
            progress_cb(0.95, "Writing iPod database...")

        ok = libgpod.itdb_write(itdb, None)
        libgpod.itdb_free(itdb)

        if not ok:
            raise RuntimeError("Failed to write iPod database")

        if progress_cb:
            progress_cb(1.0, "Sync complete")

    def eject(self, done_cb=None):
        """Eject the iPod via Gio, with udisksctl fallback."""
        if not self.mountpoint:
            raise RuntimeError("No iPod to eject")

        vm = Gio.VolumeMonitor.get()
        for mount in vm.get_mounts():
            root = mount.get_root()
            if root and root.get_path() == self.mountpoint:
                def _on_eject_done(source, result, _user_data):
                    try:
                        source.eject_with_operation_finish(result)
                    except Exception as e:
                        print(f"Eject error: {e}")
                    if done_cb:
                        done_cb(True)

                def _on_unmount_done(source, result, _user_data):
                    try:
                        source.unmount_with_operation_finish(result)
                    except Exception as e:
                        print(f"Unmount error: {e}")
                    if done_cb:
                        done_cb(True)

                if mount.can_eject():
                    mount.eject_with_operation(
                        Gio.MountUnmountFlags.NONE, None, None,
                        _on_eject_done, None)
                    self.mountpoint = None
                    return
                elif mount.can_unmount():
                    mount.unmount_with_operation(
                        Gio.MountUnmountFlags.NONE, None, None,
                        _on_unmount_done, None)
                    self.mountpoint = None
                    return

        # Fallback: udisksctl
        try:
            subprocess.run(["udisksctl", "unmount", "-p",
                            f"block_devices/{self._guess_block_dev()}"],
                           check=True, capture_output=True)
            subprocess.run(["udisksctl", "power-off", "-p",
                            f"block_devices/{self._guess_block_dev()}"],
                           capture_output=True)
        except Exception as e:
            print(f"udisksctl fallback failed: {e}")
        self.mountpoint = None
        if done_cb:
            done_cb(True)

    @staticmethod
    def _guess_block_dev() -> str:
        """Try to find the block device for the iPod from /proc/mounts."""
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and "IPOD" in parts[1]:
                        return os.path.basename(parts[0])
        except OSError:
            pass
        return "sda1"
