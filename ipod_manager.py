#!/usr/bin/python3
"""iPod Classic Music Manager - GTK4 GUI for managing music on an iPod Classic."""

import os
import sys
import traceback

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, Gio, GLib, GObject, GdkPixbuf

from ipod_db import (LibraryDB, discover_ql_playlists, parse_xspf,
                     convert_to_mp3, convert_batch, IPOD_FORMATS)
from ipod_sync import iPodSync


class TrackItem(GObject.Object):
    __gtype_name__ = "TrackItem"

    def __init__(self, track_id, title, artist, album, duration_ms,
                 cover_art_bytes, synced, file_path):
        super().__init__()
        self.track_id = track_id
        self.title = title or ""
        self.artist = artist or ""
        self.album = album or ""
        self.duration_ms = duration_ms or 0
        self.cover_art_bytes = cover_art_bytes
        self.synced = bool(synced)
        self.file_path = file_path or ""


def _format_duration(ms):
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


class IPodManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="iPod Manager",
                         default_width=900, default_height=600)
        self.db = LibraryDB()
        self.ipod = iPodSync()
        self.store = Gio.ListStore(item_type=TrackItem)

        self._build_ui()
        self._setup_drag_drop()
        self._refresh_list()
        self._detect_ipod()
        self._watch_mounts()

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

        # Playlist dropdown
        self.playlists = discover_ql_playlists()
        if self.playlists:
            pl_store = Gtk.StringList()
            pl_store.append("-- Import playlist --")
            for pl in self.playlists:
                pl_store.append(f"{pl['name']} ({pl['track_count']})")
            self.dd_playlist = Gtk.DropDown(model=pl_store)
            self.dd_playlist.set_tooltip_text("Import Quod Libet playlist")
            self.dd_playlist.connect("notify::selected", self._on_playlist_selected)
            header.pack_start(self.dd_playlist)

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

        # Column view
        scroll = Gtk.ScrolledWindow(vexpand=True)
        vbox.append(scroll)

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

    @staticmethod
    def _bind_art(_factory, list_item):
        item = list_item.get_item()
        picture = list_item.get_child()
        if item.cover_art_bytes:
            try:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(item.cover_art_bytes)
                loader.close()
                pixbuf = loader.get_pixbuf()
                scaled = pixbuf.scale_simple(36, 36, GdkPixbuf.InterpType.BILINEAR)
                texture = Gdk.Texture.new_for_pixbuf(scaled)
                picture.set_paintable(texture)
            except Exception:
                picture.set_paintable(None)
        else:
            picture.set_paintable(None)

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
            self._refresh_list()
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
            self._refresh_list()
            self.lbl_status.set_label(f"Added {added} track(s)")

    def _on_playlist_selected(self, dropdown, _pspec):
        idx = dropdown.get_selected()
        if idx == 0 or idx == Gtk.INVALID_LIST_POSITION:
            return
        pl = self.playlists[idx - 1]
        paths = parse_xspf(pl["path"])

        # Split into iPod-ready and needs-conversion
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

        # Batch convert in parallel
        converted_map = {}
        if to_convert:
            self.lbl_status.set_label(
                f"Converting {len(to_convert)} tracks using {os.cpu_count()} cores...")
            self.progress.set_fraction(0)
            ctx = GLib.MainContext.default()
            while ctx.iteration(False):
                pass

            def on_progress(done, total, name):
                GLib.idle_add(self._update_convert_progress, done, total, name)

            converted_map = convert_batch(to_convert, on_progress)

        # Add all tracks to library
        added = 0
        converted = 0
        for path in ready:
            if self.db.add_track(path) is not None:
                added += 1
        for src, dest in converted_map.items():
            if dest and self.db.add_track(dest) is not None:
                added += 1
                converted += 1

        self._refresh_list()
        self.progress.set_fraction(0)
        msg = f"Imported {added} track(s) from '{pl['name']}'"
        if converted:
            msg += f" ({converted} converted to MP3)"
        self.lbl_status.set_label(msg)
        dropdown.set_selected(0)

    def _update_convert_progress(self, done, total, name):
        self.progress.set_fraction(done / max(total, 1))
        self.lbl_status.set_label(f"Converted {done}/{total}: {name}")
        return False  # remove idle callback

    def _on_remove_clicked(self, _btn):
        sel = self.selection.get_selection()
        to_remove = []
        for i in range(self.store.get_n_items()):
            if sel.contains(i):
                to_remove.append(self.store.get_item(i).track_id)
        for tid in to_remove:
            self.db.remove_track(tid)
        if to_remove:
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
        self.store.remove_all()
        for row in self.db.get_all_tracks():
            item = TrackItem(
                track_id=row["id"],
                title=row["title"],
                artist=row["artist"],
                album=row["album"],
                duration_ms=row["duration_ms"],
                cover_art_bytes=row["cover_art"],
                synced=row["synced"],
                file_path=row["file_path"],
            )
            self.store.append(item)
        n = self.store.get_n_items()
        self.set_title(f"iPod Manager ({n} tracks)")


class IPodManagerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.github.ipod-manager")

    def do_activate(self):
        win = IPodManagerWindow(self)
        win.present()


def main():
    app = IPodManagerApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
