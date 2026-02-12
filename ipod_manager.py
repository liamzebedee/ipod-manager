#!/usr/bin/python3
"""iPod Classic Music Manager - GTK4 GUI for managing music on an iPod Classic."""

import os
import sqlite3
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, Gio, GLib, GObject, GdkPixbuf

from ipod_db import (LibraryDB, discover_ql_playlists, parse_xspf,
                     extract_metadata, convert_batch, IPOD_FORMATS)
from ipod_sync import iPodSync


class TrackItem(GObject.Object):
    __gtype_name__ = "TrackItem"

    def __init__(self, track_id, title, artist, album, duration_ms,
                 synced, file_path):
        super().__init__()
        self.track_id = track_id
        self.title = title or ""
        self.artist = artist or ""
        self.album = album or ""
        self.duration_ms = duration_ms or 0
        self.synced = bool(synced)
        self.file_path = file_path or ""


def _format_duration(ms):
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


class IPodManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="iPod Manager",
                         default_width=900, default_height=600,
                         icon_name="multimedia-player-apple-ipod")
        self.db = LibraryDB()
        self.ipod = iPodSync()
        self.store = Gio.ListStore(item_type=TrackItem)

        self._active_playlist_id = None  # None = "All", else DB playlist id
        self._all_tracks = []            # [TrackItem, ...] loaded once
        self._playlist_members = {}      # {playlist_id: set(track_ids)}
        self._art_cache = {}             # {(artist, album): Gdk.Texture}
        self._no_art_texture = self._make_placeholder_texture()

        self._build_ui()
        self._setup_drag_drop()
        self._load_data()
        self._refresh_list()
        self._load_art_background()
        self._detect_ipod()
        self._watch_mounts()

    # ------------------------------------------------------------------
    # Data loading (once at startup, re-called after import/add/remove)
    # ------------------------------------------------------------------
    def _load_data(self):
        """Load all tracks and playlist memberships into memory."""
        rows = self.db.conn.execute(
            "SELECT id, file_path, title, artist, album, duration_ms, synced "
            "FROM tracks WHERE deleted=0 ORDER BY artist, album, track_nr"
        ).fetchall()
        self._all_tracks = [
            TrackItem(
                track_id=r["id"], title=r["title"], artist=r["artist"],
                album=r["album"], duration_ms=r["duration_ms"],
                synced=r["synced"], file_path=r["file_path"],
            )
            for r in rows
        ]
        self._playlist_members = {}
        for r in self.db.conn.execute(
                "SELECT playlist_id, track_id FROM playlist_tracks"):
            self._playlist_members.setdefault(
                r["playlist_id"], set()).add(r["track_id"])

        self._update_playlist_counts()

    def _update_playlist_counts(self):
        """Recompute sidebar counts from in-memory playlist membership data."""
        for i, pl in enumerate(self.playlists):
            r = self.db.conn.execute(
                "SELECT id FROM playlists WHERE name=?", (pl["name"],)).fetchone()
            if r:
                count = len(self._playlist_members.get(r["id"], set()))
            else:
                count = 0
            _, _, lbl_count = self._pl_widgets[i]
            lbl_count.set_label(str(count))

    @staticmethod
    def _make_placeholder_texture():
        """Render a 36x36 placeholder from the audio-x-generic icon."""
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon = theme.lookup_icon(
            "audio-x-generic-symbolic", None, 36, 1,
            Gtk.TextDirection.NONE, Gtk.IconLookupFlags(0))
        f = icon.get_file()
        if f and f.get_path():
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(f.get_path(), 36, 36, True)
            return Gdk.Texture.new_for_pixbuf(pb)
        return None

    def _load_art_background(self):
        """Kick off background thread to decode album art in parallel."""
        threading.Thread(target=self._art_worker, daemon=True).start()

    def _art_worker(self):
        """Background: load one cover_art per unique album, decode in parallel."""
        conn = sqlite3.connect(str(self.db.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, cover_art FROM tracks
               WHERE deleted=0 AND cover_art IS NOT NULL"""
        ).fetchall()
        conn.close()

        to_decode = [r for r in rows if r["id"] not in self._art_cache]
        if not to_decode:
            return

        def decode(row):
            try:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(bytes(row["cover_art"]))
                loader.close()
                pb = loader.get_pixbuf()
                return (row["id"],
                        pb.scale_simple(36, 36, GdkPixbuf.InterpType.BILINEAR))
            except Exception:
                return None, None

        results = []
        with ThreadPoolExecutor() as pool:
            for key, pixbuf in pool.map(decode, to_decode):
                if pixbuf:
                    results.append((key, pixbuf))

        if results:
            GLib.idle_add(self._apply_art, results)

    def _apply_art(self, results):
        """Main thread: create textures and refresh visible rows."""
        for track_id, pixbuf in results:
            self._art_cache[track_id] = Gdk.Texture.new_for_pixbuf(pixbuf)
        self._refresh_list()
        return False

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(vbox)

        # Header bar
        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        btn_add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add files")
        btn_add.connect("clicked", self._on_add_clicked)
        header.pack_start(btn_add)

        self.btn_remove = Gtk.Button(icon_name="user-trash-symbolic",
                                     tooltip_text="Remove selected")
        self.btn_remove.connect("clicked", self._on_remove_clicked)
        header.pack_start(self.btn_remove)

        self.btn_eject = Gtk.Button(icon_name="media-eject-symbolic",
                                    tooltip_text="Eject iPod")
        self.btn_eject.connect("clicked", self._on_eject_clicked)
        header.pack_end(self.btn_eject)

        self.btn_sync = Gtk.Button(label="Sync", icon_name="emblem-synchronizing-symbolic",
                                   tooltip_text="Sync to iPod")
        self.btn_sync.connect("clicked", self._on_sync_clicked)
        header.pack_end(self.btn_sync)

        self.lbl_ipod = Gtk.Label(label="No iPod")
        self.lbl_ipod.add_css_class("dim-label")
        header.pack_end(self.lbl_ipod)

        # Main content: playlist sidebar + track list
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_vexpand(True)
        vbox.append(paned)

        # Playlist sidebar
        self.playlists = discover_ql_playlists()
        self._pl_widgets = []  # [(import_btn, progress_bar, count_lbl), ...] per playlist
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.set_size_request(220, -1)

        pl_scroll = Gtk.ScrolledWindow(vexpand=True)
        sidebar.append(pl_scroll)

        self.pl_listbox = Gtk.ListBox()
        self.pl_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.pl_listbox.add_css_class("navigation-sidebar")
        self.pl_listbox.connect("row-selected", self._on_sidebar_selected)
        pl_scroll.set_child(self.pl_listbox)

        # "All" row
        all_row = Gtk.ListBoxRow()
        all_lbl = Gtk.Label(label="All", xalign=0)
        all_lbl.set_margin_start(8)
        all_lbl.set_margin_end(8)
        all_lbl.set_margin_top(6)
        all_lbl.set_margin_bottom(6)
        all_row.set_child(all_lbl)
        self.pl_listbox.append(all_row)

        # Playlist rows
        for i, pl in enumerate(self.playlists):
            row = Gtk.ListBoxRow()
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row_box.set_margin_start(8)
            row_box.set_margin_end(4)
            row_box.set_margin_top(4)
            row_box.set_margin_bottom(4)

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            lbl_name = Gtk.Label(label=pl["name"], xalign=0, hexpand=True)
            lbl_name.set_ellipsize(3)
            lbl_count = Gtk.Label(label="0")
            lbl_count.add_css_class("dim-label")
            btn = Gtk.Button(icon_name="document-import-symbolic",
                             tooltip_text=f"Import '{pl['name']}'")
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_import_playlist, i)
            top.append(lbl_name)
            top.append(lbl_count)
            top.append(btn)
            row_box.append(top)

            prog = Gtk.ProgressBar()
            prog.set_visible(False)
            row_box.append(prog)

            self._pl_widgets.append((btn, prog, lbl_count))
            row.set_child(row_box)
            self.pl_listbox.append(row)

        # Select "All" by default
        self.pl_listbox.select_row(all_row)

        paned.set_start_child(sidebar)
        paned.set_shrink_start_child(False)

        # Column view (right side)
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        right_box.append(scroll)

        self.selection = Gtk.MultiSelection(model=self.store)
        self.column_view = Gtk.ColumnView(model=self.selection)
        self.column_view.add_css_class("data-table")
        scroll.set_child(self.column_view)

        self.column_view.append_column(self._make_art_column())
        self.column_view.append_column(
            self._make_label_column("Title", self._bind_title))
        self.column_view.append_column(
            self._make_label_column("Artist", self._bind_artist))
        self.column_view.append_column(
            self._make_label_column("Album", self._bind_album))
        self.column_view.append_column(
            self._make_label_column("Duration", self._bind_duration, width=80))
        self.column_view.append_column(self._make_synced_column())

        paned.set_end_child(right_box)
        paned.set_shrink_end_child(False)
        paned.set_position(220)

        # Storage bar
        storage_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        storage_box.set_margin_start(8)
        storage_box.set_margin_end(8)
        storage_box.set_margin_top(4)
        vbox.append(storage_box)

        self.storage_bar = Gtk.LevelBar()
        self.storage_bar.set_min_value(0)
        self.storage_bar.set_max_value(1)
        self.storage_bar.set_hexpand(True)
        storage_box.append(self.storage_bar)

        self.lbl_storage = Gtk.Label(label="")
        self.lbl_storage.add_css_class("dim-label")
        storage_box.append(self.lbl_storage)

        # Bottom bar
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom.set_margin_start(8)
        bottom.set_margin_end(8)
        bottom.set_margin_top(4)
        bottom.set_margin_bottom(4)
        vbox.append(bottom)

        self.progress = Gtk.ProgressBar(hexpand=True)
        bottom.append(self.progress)

        self.lbl_status = Gtk.Label(label="Ready")
        bottom.append(self.lbl_status)

    # ------------------------------------------------------------------
    # Column factories
    # ------------------------------------------------------------------
    def _make_label_column(self, title, bind_func, width=None):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_label)
        factory.connect("bind", bind_func)
        col = Gtk.ColumnViewColumn(title=title, factory=factory)
        if width:
            col.set_fixed_width(width)
        else:
            col.set_expand(True)
        return col

    def _make_art_column(self):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_picture)
        factory.connect("bind", self._bind_art)
        col = Gtk.ColumnViewColumn(title="", factory=factory)
        col.set_fixed_width(44)
        return col

    def _make_synced_column(self):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_image)
        factory.connect("bind", self._bind_synced)
        col = Gtk.ColumnViewColumn(title="Synced", factory=factory)
        col.set_fixed_width(60)
        return col

    @staticmethod
    def _setup_label(_factory, list_item):
        label = Gtk.Label(xalign=0)
        label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        list_item.set_child(label)

    @staticmethod
    def _setup_picture(_factory, list_item):
        pic = Gtk.Picture()
        pic.set_size_request(36, 36)
        pic.set_content_fit(Gtk.ContentFit.COVER)
        list_item.set_child(pic)

    @staticmethod
    def _setup_image(_factory, list_item):
        img = Gtk.Image()
        list_item.set_child(img)

    @staticmethod
    def _bind_title(_factory, list_item):
        item = list_item.get_item()
        list_item.get_child().set_label(item.title)

    @staticmethod
    def _bind_artist(_factory, list_item):
        item = list_item.get_item()
        list_item.get_child().set_label(item.artist)

    @staticmethod
    def _bind_album(_factory, list_item):
        item = list_item.get_item()
        list_item.get_child().set_label(item.album)

    @staticmethod
    def _bind_duration(_factory, list_item):
        item = list_item.get_item()
        list_item.get_child().set_label(_format_duration(item.duration_ms))

    def _bind_art(self, _factory, list_item):
        item = list_item.get_item()
        pic = list_item.get_child()
        texture = self._art_cache.get(item.track_id)
        pic.set_paintable(texture or self._no_art_texture)

    @staticmethod
    def _bind_synced(_factory, list_item):
        item = list_item.get_item()
        img = list_item.get_child()
        if item.synced:
            img.set_from_icon_name("emblem-ok-symbolic")
        else:
            img.clear()

    # ------------------------------------------------------------------
    # Drag and drop
    # ------------------------------------------------------------------
    def _setup_drag_drop(self):
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_files_dropped)
        self.get_child().add_controller(drop)

    def _on_files_dropped(self, _target, value, _x, _y):
        files = value.get_files()
        added = 0
        for gfile in files:
            path = gfile.get_path()
            if path and path.lower().endswith((".mp3", ".m4a")):
                row_id = self.db.add_track(path)
                if row_id is not None:
                    added += 1
        if added:
            self._load_data()
            self._refresh_list()
            self._load_art_background()
            self.lbl_status.set_label(f"Added {added} track(s)")
        return True

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------
    def _on_add_clicked(self, _btn):
        dialog = Gtk.FileDialog()
        filt = Gtk.FileFilter()
        filt.set_name("Audio files")
        filt.add_mime_type("audio/mpeg")
        filt.add_mime_type("audio/mp4")
        filt.add_pattern("*.mp3")
        filt.add_pattern("*.m4a")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(filt)
        dialog.set_filters(filters)
        dialog.open_multiple(self, None, self._on_files_chosen)

    def _on_files_chosen(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        added = 0
        for i in range(files.get_n_items()):
            gfile = files.get_item(i)
            path = gfile.get_path()
            if path and path.lower().endswith((".mp3", ".m4a")):
                row_id = self.db.add_track(path)
                if row_id is not None:
                    added += 1
        if added:
            self._load_data()
            self._refresh_list()
            self._load_art_background()
            self.lbl_status.set_label(f"Added {added} track(s)")

    def _on_sidebar_selected(self, listbox, row):
        if row is None:
            return
        idx = row.get_index()
        if idx == 0:
            self._active_playlist_id = None
        else:
            pl = self.playlists[idx - 1]
            r = self.db.conn.execute(
                "SELECT id FROM playlists WHERE name=?", (pl["name"],)).fetchone()
            self._active_playlist_id = r["id"] if r else -1
        self._refresh_list()

    def _on_import_playlist(self, btn, pl_idx):
        pl = self.playlists[pl_idx]
        btn_w, prog_w, _ = self._pl_widgets[pl_idx]
        btn_w.set_sensitive(False)
        prog_w.set_fraction(0)
        prog_w.set_visible(True)
        self.lbl_status.set_label(f"Importing '{pl['name']}'...")

        # Create playlist in DB on main thread before spawning worker
        pl_db_id = self.db.get_or_create_playlist(pl["name"])

        thread = threading.Thread(
            target=self._import_worker, args=(pl, pl_idx, pl_db_id), daemon=True)
        thread.start()

    def _import_worker(self, pl, pl_idx, pl_db_id):
        """Background thread — only does file I/O. Posts results to main thread."""
        paths = parse_xspf(pl["path"])

        # Classify files
        ready = []
        to_convert = []
        for path in paths:
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext in IPOD_FORMATS:
                ready.append(path)
            else:
                to_convert.append(path)

        # Convert non-iPod formats in parallel (ThreadPoolExecutor + ffmpeg subprocesses)
        converted_map = {}
        if to_convert:
            def on_convert_progress(done, total, name):
                frac = done / max(total, 1) * 0.5
                GLib.idle_add(self._pl_progress, pl_idx, frac,
                              f"Converting: {name}")

            converted_map = convert_batch(to_convert, on_convert_progress)

        # Build final path list
        all_paths = list(ready)
        for src, dest in converted_map.items():
            if dest:
                all_paths.append(dest)

        # Extract metadata + post to main thread for DB insert
        total = len(all_paths)
        for i, path in enumerate(all_paths):
            try:
                meta = extract_metadata(path)
            except Exception:
                meta = None
            frac = 0.5 + (i + 1) / max(total, 1) * 0.5
            GLib.idle_add(self._add_track_from_worker, pl_idx, pl_db_id,
                          meta, i + 1, total, frac)

        GLib.idle_add(self._finish_import, pl_idx, pl_db_id, pl["name"], total)

    def _pl_progress(self, pl_idx, frac, msg):
        _, prog_w, _ = self._pl_widgets[pl_idx]
        prog_w.set_fraction(frac)
        self.lbl_status.set_label(msg)
        return False

    def _add_track_from_worker(self, pl_idx, pl_db_id, meta, done, total, frac):
        """Insert track + playlist membership on main thread."""
        _, prog_w, _ = self._pl_widgets[pl_idx]
        prog_w.set_fraction(frac)
        if meta:
            track_id = self.db.add_track_from_meta(meta)
            if track_id is None:
                # Already exists — look up its id and undelete if needed
                row = self.db.conn.execute(
                    "SELECT id FROM tracks WHERE file_path=?",
                    (meta["file_path"],)).fetchone()
                if row:
                    track_id = row["id"]
                    self.db.conn.execute(
                        "UPDATE tracks SET deleted=0 WHERE id=? AND deleted=1",
                        (track_id,))
                    self.db.conn.commit()
            if track_id:
                self.db.add_track_to_playlist(pl_db_id, track_id, done)
        self.lbl_status.set_label(f"Adding tracks... {done}/{total}")
        return False

    def _finish_import(self, pl_idx, pl_db_id, name, count):
        btn_w, prog_w, _ = self._pl_widgets[pl_idx]
        prog_w.set_visible(False)
        btn_w.set_sensitive(True)
        self._active_playlist_id = pl_db_id
        row = self.pl_listbox.get_row_at_index(pl_idx + 1)
        self.pl_listbox.select_row(row)
        self._load_data()
        self._refresh_list()
        self._load_art_background()
        self.lbl_status.set_label(f"Imported from '{name}' ({count} tracks)")
        return False

    def _on_remove_clicked(self, _btn):
        sel = self.selection.get_selection()
        to_remove = []
        for i in range(self.store.get_n_items()):
            if sel.contains(i):
                to_remove.append(self.store.get_item(i).track_id)
        for tid in to_remove:
            self.db.remove_track(tid)
        if to_remove:
            self._load_data()
            self._refresh_list()
            self.lbl_status.set_label(f"Removed {len(to_remove)} track(s)")

    def _on_sync_clicked(self, _btn):
        if not self.ipod.mountpoint:
            self._detect_ipod()
            if not self.ipod.mountpoint:
                self.lbl_status.set_label("No iPod detected")
                return

        if self.ipod.needs_init():
            self._prompt_init()
            return

        self.btn_sync.set_sensitive(False)
        self.lbl_status.set_label("Syncing...")

        def progress_cb(frac, msg):
            self.progress.set_fraction(frac)
            self.lbl_status.set_label(msg)
            # Pump GTK event loop for UI responsiveness
            ctx = GLib.MainContext.default()
            while ctx.iteration(False):
                pass

        try:
            self.ipod.sync(self.db, progress_cb)
            self._load_data()
            self._refresh_list()
            self._update_storage()
            self.lbl_status.set_label("Sync complete")
        except Exception as e:
            self.lbl_status.set_label(f"Sync error: {e}")
            traceback.print_exc()
        finally:
            self.progress.set_fraction(0)
            self.btn_sync.set_sensitive(True)

    def _on_eject_clicked(self, _btn):
        if not self.ipod.mountpoint:
            return
        self.lbl_status.set_label("Ejecting...")

        def done_cb(success):
            GLib.idle_add(self._post_eject)

        self.ipod.eject(done_cb)

    def _post_eject(self):
        self.lbl_ipod.set_label("No iPod")
        self.lbl_status.set_label("iPod ejected safely")
        self.btn_sync.set_sensitive(False)
        self.btn_eject.set_sensitive(False)

    # ------------------------------------------------------------------
    # Mount monitoring
    # ------------------------------------------------------------------
    def _watch_mounts(self):
        """Listen for iPod connect/disconnect via Gio.VolumeMonitor."""
        self._volume_monitor = Gio.VolumeMonitor.get()
        self._volume_monitor.connect("mount-added", self._on_mount_changed)
        self._volume_monitor.connect("mount-removed", self._on_mount_changed)

    def _on_mount_changed(self, _monitor, _mount):
        self._detect_ipod()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _detect_ipod(self):
        mp = self.ipod.detect_ipod()
        if mp:
            self.lbl_ipod.set_label(f"iPod: {mp}")
            self.btn_sync.set_sensitive(True)
            self.btn_eject.set_sensitive(True)
            self._update_storage()
            if self.ipod.needs_init():
                self._prompt_init()
        else:
            self.lbl_ipod.set_label("No iPod")
            self.btn_sync.set_sensitive(False)
            self.btn_eject.set_sensitive(False)
            self.storage_bar.set_value(0)
            self.lbl_storage.set_label("")

    def _update_storage(self):
        """Update the storage bar with iPod disk usage."""
        mp = self.ipod.mountpoint
        if not mp:
            return
        try:
            stat = os.statvfs(mp)
            total = stat.f_frsize * stat.f_blocks
            free = stat.f_frsize * stat.f_bavail
            used = total - free
            frac = used / total if total else 0
            self.storage_bar.set_value(frac)
            self.lbl_storage.set_label(
                f"{used / 1e9:.1f} GB used / {total / 1e9:.1f} GB — "
                f"{free / 1e9:.1f} GB available")
        except OSError:
            pass

    def _prompt_init(self):
        """Show a dialog when the iPod needs first-time initialisation."""
        dlg = Gtk.AlertDialog()
        dlg.set_message("iPod needs initialisation")
        dlg.set_detail(
            "This iPod hasn't been set up for Linux syncing yet. "
            "A one-time setup is needed to read device info from the iPod "
            "(requires admin password).\n\n"
            "Without this, synced music won't appear on the iPod.")
        dlg.set_buttons(["Initialise", "Later"])
        dlg.set_default_button(0)
        dlg.set_cancel_button(1)
        dlg.choose(self, None, self._on_init_response)

    def _on_init_response(self, dlg, result):
        try:
            choice = dlg.choose_finish(result)
        except GLib.Error:
            return
        if choice != 0:
            self.lbl_status.set_label("iPod not initialised — sync may not work")
            return

        self.lbl_status.set_label("Initialising iPod (check for password prompt)...")
        # Pump events so label updates before blocking pkexec call
        ctx = GLib.MainContext.default()
        while ctx.iteration(False):
            pass

        ok, msg = self.ipod.initialise()
        self.lbl_status.set_label(msg)

    def _refresh_list(self):
        """Pure in-memory filter + splice. No DB queries."""
        if self._active_playlist_id is not None and self._active_playlist_id > 0:
            members = self._playlist_members.get(
                self._active_playlist_id, set())
            items = [t for t in self._all_tracks if t.track_id in members]
        elif self._active_playlist_id == -1:
            items = []
        else:
            items = self._all_tracks

        # Remove then re-add to force GTK to re-bind all rows
        self.store.remove_all()
        self.store.splice(0, 0, items)
        self.set_title(f"iPod Manager ({len(items)} tracks)")


class IPodManagerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.github.ipod-manager")

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Gtk.Window.set_default_icon_name("multimedia-player-apple-ipod")

    def do_activate(self):
        win = IPodManagerWindow(self)
        win.present()


def main():
    app = IPodManagerApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
