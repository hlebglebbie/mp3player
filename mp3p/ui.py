"""Curses interface for the torrent mp3 player.

Views: Search, Downloads, Library, Liked, Playlists. A modal file picker opens on
a torrent so a single track can be pulled out of an album. The player bar at the
bottom is always live and is refreshed from ``App.tick()``.
"""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import human_size, human_time
from .downloader import STATE_COMPLETE, STATE_DOWNLOADING, STATE_ERROR, STATE_METADATA, STATE_STOPPED

VIEWS = ["search", "downloads", "library", "liked", "playlists"]
VIEW_TITLES = {
    "search": "Поиск",
    "downloads": "Загрузки",
    "library": "Библиотека",
    "liked": "Любимое",
    "playlists": "Плейлисты",
}

C_DEFAULT = 1
C_ACCENT = 2
C_DIM = 3
C_GOOD = 4
C_BAD = 5
C_SELECT = 6
C_HEART = 7
C_WARN = 8


@dataclass
class Row:
    kind: str
    obj: object
    text: str = ""
    extra: str = ""
    meta: dict = field(default_factory=dict)


class Prompt:
    def __init__(self, label: str, initial: str, on_submit, hint: str = ""):
        self.label = label
        self.buffer = list(initial)
        self.cursor = len(self.buffer)
        self.on_submit = on_submit
        self.hint = hint

    def insert(self, text: str) -> None:
        for char in text:
            if char.isprintable():
                self.buffer.insert(self.cursor, char)
                self.cursor += 1

    def backspace(self) -> None:
        if self.cursor > 0:
            del self.buffer[self.cursor - 1]
            self.cursor -= 1

    def delete(self) -> None:
        if self.cursor < len(self.buffer):
            del self.buffer[self.cursor]

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        self.cursor = min(len(self.buffer), self.cursor + 1)

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.buffer)

    @property
    def value(self) -> str:
        return "".join(self.buffer)


class Interface:
    def __init__(self, app):
        self.app = app
        self.stdscr = None
        self.view = "search"
        self.cursor: dict[str, int] = {v: 0 for v in VIEWS}
        self.offset: dict[str, int] = {v: 0 for v in VIEWS}
        self.rows: list[Row] = []
        self.prompt: Prompt | None = None
        self.modal_session = None
        self.modal_cursor = 0
        self.modal_offset = 0
        # пикер папки библиотеки: {"dir": Path, "rows": [(kind, label, payload)], ...}
        self.path_picker: dict | None = None
        self.playlist_open: str | None = None
        self.show_help = False
        self.status = ""
        self.status_at = 0.0
        self.running = True
        self._last_size = (0, 0)

    # --------------------------------------------------------------------- setup
    def flash(self, message: str) -> None:
        self.status = message
        self.status_at = time.time()

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, stdscr) -> None:
        self.stdscr = stdscr
        curses.curs_set(0)
        stdscr.keypad(True)
        try:
            curses.mousemask(0)
        except curses.error:
            pass
        self._init_colors()
        stdscr.timeout(120)
        while self.running:
            self.app.tick()
            self._draw()
            self._read_key()

    def _init_colors(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(C_DEFAULT, -1, -1)
            curses.init_pair(C_ACCENT, curses.COLOR_CYAN, -1)
            curses.init_pair(C_DIM, curses.COLOR_BLUE, -1)
            curses.init_pair(C_GOOD, curses.COLOR_GREEN, -1)
            curses.init_pair(C_BAD, curses.COLOR_RED, -1)
            curses.init_pair(C_SELECT, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(C_HEART, curses.COLOR_MAGENTA, -1)
            curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
        except curses.error:
            pass

    def _attr(self, pair: int, bold: bool = False, dim: bool = False) -> int:
        attr = curses.color_pair(pair) if curses.has_colors() else 0
        if bold:
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        return attr

    # -------------------------------------------------------------------- input
    def _read_key(self) -> None:
        try:
            key = self.stdscr.get_wch()
        except curses.error:
            return
        except KeyboardInterrupt:
            self.running = False
            return
        if isinstance(key, int):
            self._on_special(key)
            return
        if key == "\x1b":
            self._on_escape()
            return
        if key in ("\n", "\r"):
            self._on_enter()
            return
        if key in ("\t",):
            self._switch_view(1)
            return
        if key in ("\x7f", "\b"):
            self._on_backspace()
            return
        if ord(key) < 32:
            if key == "\x0e":  # Ctrl-N
                self._cycle(1)
            elif key == "\x10":  # Ctrl-P
                self._cycle(-1)
            elif key == "\x04":  # Ctrl-D
                self._on_ctrl_d()
            return
        self._on_char(key)

    def _on_special(self, key: int) -> None:
        if key == curses.KEY_RESIZE:
            self._resize()
            return
        if self.path_picker is not None:
            if key == curses.KEY_UP:
                self._picker_move(-1)
            elif key == curses.KEY_DOWN:
                self._picker_move(1)
            elif key == curses.KEY_HOME:
                self._picker_jump(0)
            elif key == curses.KEY_END:
                self._picker_jump(10 ** 6)
            elif key in (curses.KEY_NPAGE,):
                self._picker_move(8)
            elif key in (curses.KEY_PPAGE,):
                self._picker_move(-8)
            return
        mapping = {
            curses.KEY_UP: lambda: self._cycle(-1),
            curses.KEY_DOWN: lambda: self._cycle(1),
            curses.KEY_LEFT: lambda: self.app.seek(-5),
            curses.KEY_RIGHT: lambda: self.app.seek(5),
            curses.KEY_NPAGE: lambda: self._page(1),
            curses.KEY_PPAGE: lambda: self._page(-1),
            curses.KEY_HOME: lambda: self._jump(0),
            curses.KEY_END: lambda: self._jump(10 ** 6),
            curses.KEY_RESIZE: self._resize,
            curses.KEY_BACKSPACE: self._on_backspace,
        }
        action = mapping.get(key)
        if action:
            action()

    def _on_backspace(self) -> None:
        if self.prompt is not None:
            self.prompt.backspace()
        elif self.path_picker is not None:
            self._picker_parent()
        elif self.modal_session is not None:
            return
        elif self.view == "playlists" and self.playlist_open:
            self.playlist_open = None

    def _on_ctrl_d(self) -> None:
        if self.prompt is not None or self.path_picker is not None:
            return
        if self.view == "playlists" and self.playlist_open:
            self._delete_playlist(self.playlist_open)
        else:
            self._delete_current()

    def _on_escape(self) -> None:
        if self.prompt is not None:
            self.prompt = None
            return
        if self.show_help:
            self.show_help = False
            return
        if self.path_picker is not None:
            self.path_picker = None
            self.flash("Отменено — папка библиотеки не изменилась")
            return
        if self.modal_session is not None:
            self.modal_session = None
            return
        if self.view == "playlists" and self.playlist_open:
            self.playlist_open = None
            return
        if self.view != "search":
            self.view = "search"
            return
        self.running = False

    def _on_char(self, key: str) -> None:
        if self.prompt is not None:
            self.prompt.insert(key)
            return
        if self.show_help:
            self.show_help = False
            return
        if self.path_picker is not None:
            self._picker_key(key)
            return
        if self.modal_session is not None:
            self._modal_key(key)
            return
        handler = {
            "q": self._quit,
            "j": lambda: self._cycle(1),
            "k": lambda: self._cycle(-1),
            "g": lambda: self._jump(0),
            "G": lambda: self._jump(10 ** 6),
            " ": self.app.toggle_pause,
            "n": self.app.next_track,
            "p": self.app.previous_track,
            "+": lambda: self.app.volume(5),
            "=": lambda: self.app.volume(5),
            "-": lambda: self.app.volume(-5),
            "_": lambda: self.app.volume(-5),
            "l": self._toggle_like,
            "L": self._open_library_picker,
            "a": self._add_to_playlist,
            "N": self._new_playlist,
            "/": self._open_search,
            "s": self.app.toggle_shuffle,
            "R": self.app.cycle_repeat,
            "d": self._delete_current,
            "<": lambda: self.app.seek(-30),
            ">": lambda: self.app.seek(30),
            ",": lambda: self.app.seek(-10),
            ".": lambda: self.app.seek(10),
            "?": lambda: setattr(self, "show_help", True),
            "r": self._refresh,
            "1": lambda: self._set_view("search"),
            "2": lambda: self._set_view("downloads"),
            "3": lambda: self._set_view("library"),
            "4": lambda: self._set_view("liked"),
            "5": lambda: self._set_view("playlists"),
        }
        action = handler.get(key)
        if action:
            action()

    def _quit(self) -> None:
        self.running = False

    # --------------------------------------------------------------- navigation
    def _set_view(self, view: str) -> None:
        self.view = view
        if view != "playlists":
            self.playlist_open = None

    def _switch_view(self, delta: int) -> None:
        index = (VIEWS.index(self.view) + delta) % len(VIEWS)
        self._set_view(VIEWS[index])

    def _row_count(self) -> int:
        return len(self.rows)

    def _cycle(self, delta: int) -> None:
        count = self._row_count()
        if count == 0:
            return
        key = self._cursor_key()
        self.cursor[key] = (self.cursor.get(key, 0) + delta) % count

    def _page(self, delta: int) -> None:
        count = self._row_count()
        if count == 0:
            return
        key = self._cursor_key()
        height = max(3, self._list_height())
        self.cursor[key] = max(0, min(count - 1, self.cursor.get(key, 0) + delta * (height - 1)))

    def _jump(self, position: int) -> None:
        count = self._row_count()
        if count == 0:
            return
        key = self._cursor_key()
        self.cursor[key] = min(count - 1, position) if position < 10 ** 6 else count - 1

    def _cursor_key(self) -> str:
        if self.view == "playlists" and self.playlist_open:
            return f"playlists:{self.playlist_open}"
        return self.view

    def _current_row(self) -> Row | None:
        index = self.cursor.get(self._cursor_key(), 0)
        if 0 <= index < len(self.rows):
            return self.rows[index]
        return None

    def _resize(self) -> None:
        curses.update_lines_cols()
        self.stdscr.clear()

    # ------------------------------------------------------------------ actions
    def _open_search(self) -> None:
        self._set_view("search")

        def submit(value: str) -> None:
            self.prompt = None
            if value.strip():
                self.app.start_search(value.strip())

        self.prompt = Prompt("Поиск: ", self.app.last_query, submit,
                             hint="Enter — искать (торренты + soulseek, через прокси), Esc — отмена")

    def _refresh(self) -> None:
        if self.view == "search" and self.app.last_query:
            self.app.start_search(self.app.last_query)
        else:
            self.app.refresh_library()
            self.flash("Библиотека обновлена")

    def _on_enter(self) -> None:
        if self.prompt is not None:
            prompt = self.prompt
            self.prompt = None
            value = prompt.value.strip()
            prompt.on_submit(value)
            return
        if self.show_help:
            self.show_help = False
            return
        if self.path_picker is not None:
            self._picker_enter()
            return
        if self.modal_session is not None:
            self._modal_toggle_file()
            return
        row = self._current_row()
        if row is None:
            return
        if row.kind == "result":
            self.app.download_result(row.obj)
            self.flash(f"Загрузка: {row.obj.name[:50]}")
            self._set_view("downloads")
        elif row.kind == "session":
            if not hasattr(row.obj, "metainfo"):
                # soulseek качает один файл целиком — списка файлов нет
                user = getattr(getattr(row.obj, "result", None), "username", "пир")
                self.flash(f"SoulSeek {user}: файл качается целиком, d — отмена")
                return
            self.modal_session = row.obj
            self.modal_cursor = 0
            self.modal_offset = 0
        elif row.kind == "track":
            self._play_from_row(row)
        elif row.kind == "playlist":
            self.playlist_open = row.obj[1]
            self.cursor[f"playlists:{self.playlist_open}"] = 0

    def _play_from_row(self, row: Row) -> None:
        tracks = [r.obj for r in self.rows if r.kind == "track"]
        if not tracks:
            return
        index = tracks.index(row.obj)
        self.app.play_tracks(tracks, index)

    def _toggle_like(self) -> None:
        if self.modal_session is not None:
            return
        row = self._current_row()
        if row is not None and row.kind == "track":
            liked = self.app.toggle_like(row.obj.id)
            self.flash("Добавлено в любимое ♥" if liked else "Убрано из любимого")
            return
        playing = self.app.now_playing_track_id()
        if playing:
            liked = self.app.toggle_like(playing)
            self.flash("Трек лайкнут ♥" if liked else "Лайк снят")

    def _add_to_playlist(self) -> None:
        if self.modal_session is not None:
            return
        row = self._current_row()
        track_id = None
        if row is not None and row.kind == "track":
            track_id = row.obj.id
        elif self.view in ("library", "liked"):
            track_id = self.app.now_playing_track_id()
        if track_id is None:
            self.flash("Нет выбранного трека")
            return
        names = self.app.playlist_names()
        hint = "существующие: " + ", ".join(names[:6]) if names else "новый плейлист — просто введите имя"

        def submit(value: str) -> None:
            if not value:
                return
            ok = self.app.add_to_playlist(value, track_id)
            self.flash(f"Добавлено в «{value}»" if ok else f"Уже есть в «{value}»")

        self.prompt = Prompt("Плейлист: ", "", submit, hint=hint)

    def _new_playlist(self) -> None:
        def submit(value: str) -> None:
            if not value:
                return
            self.app.create_playlist(value)
            self.flash(f"Плейлист «{value}» создан")
            self._set_view("playlists")

        self.prompt = Prompt("Новый плейлист: ", "", submit, hint="Enter — создать")

    def _delete_current(self) -> None:
        row = self._current_row()
        if row is None:
            return
        if row.kind == "session":
            self.app.cancel_session(row.obj)
            self.flash("Загрузка отменена")
        elif row.kind == "playlist":
            self._delete_playlist(row.obj[1])
        elif row.kind == "track" and self.view == "playlists" and self.playlist_open:
            self.app.remove_from_playlist(self.playlist_open, row.obj.id)
            self.flash("Убрано из плейлиста")

    def _delete_playlist(self, name: str) -> None:
        self.app.delete_playlist(name)
        if self.playlist_open == name:
            self.playlist_open = None
        self.flash(f"Плейлист «{name}» удалён")

    # -------------------------------------------------------------------- modal
    def _modal_key(self, key: str) -> None:
        session = self.modal_session
        if session is None:
            return
        meta = session.metainfo
        count = len(meta.files) if meta else 0
        if key in ("j", "J"):
            self.modal_cursor = min(max(0, count - 1), self.modal_cursor + 1)
        elif key in ("k", "K"):
            self.modal_cursor = max(0, self.modal_cursor - 1)
        elif key == "g":
            self.modal_cursor = 0
        elif key == "G":
            self.modal_cursor = max(0, count - 1)
        elif key == "q":
            self.modal_session = None
        elif key == "p":
            self._modal_play_file()
        elif key == "s" and meta is not None:
            session.set_selection([f.index for f in meta.audio_files()] or [f.index for f in meta.files])
            self.flash("Выбраны все аудиофайлы")
        elif key == "x":
            self.app.cancel_session(session)
            self.modal_session = None
            self.flash("Загрузка отменена")

    def _modal_toggle_file(self) -> None:
        session = self.modal_session
        if session is None or session.metainfo is None:
            return
        files = session.metainfo.files
        if not (0 <= self.modal_cursor < len(files)):
            return
        target = files[self.modal_cursor]
        selection = set(session.selected_files)
        if target.index in selection:
            selection.discard(target.index)
        else:
            selection.add(target.index)
        if not selection:
            selection = {target.index}
        session.set_selection(selection)
        self.flash(f"{'Выбран' if target.index in selection else 'Снят'}: {target.name[:60]}")

    def _modal_play_file(self) -> None:
        session = self.modal_session
        if session is None or session.metainfo is None:
            return
        files = session.metainfo.files
        if not (0 <= self.modal_cursor < len(files)):
            return
        target = files[self.modal_cursor]
        if not target.is_audio:
            self.flash("Это не аудиофайл")
            return
        selection = set(session.selected_files)
        selection.add(target.index)
        session.set_selection(selection)
        self.app.request_play(session, target.index)
        self.flash(f"Качаю и поставлю в очередь: {target.name[:50]}")
        self.modal_session = None

    # -------------------------------------------------- пикер папки библиотеки
    def _open_library_picker(self) -> None:
        current = Path(self.app.cfg.library_dir).expanduser()
        self.path_picker = {"dir": current, "rows": [], "cursor": 0, "offset": 0, "error": ""}
        self._picker_scan()
        self.flash("Выбор папки библиотеки: j/k · Enter · q отмена")

    def _picker_scan(self) -> None:
        picker = self.path_picker
        if picker is None:
            return
        rows = [
            ("confirm", "✓  Выбрать эту папку", None),
            ("parent", "↑  ../", None),
            ("manual", "✎  Ввести путь вручную…", None),
        ]
        try:
            subdirs = sorted(
                (entry for entry in picker["dir"].iterdir() if entry.is_dir()),
                key=lambda p: p.name.lower(),
            )
            picker["error"] = ""
        except OSError as exc:
            subdirs = []
            picker["error"] = str(exc)
        for entry in subdirs:
            rows.append(("dir", f"▸ {entry.name}/", entry))
        picker["rows"] = rows
        if picker["cursor"] >= len(rows):
            picker["cursor"] = max(0, len(rows) - 1)

    def _picker_move(self, delta: int) -> None:
        picker = self.path_picker
        if picker is None or not picker["rows"]:
            return
        count = len(picker["rows"])
        picker["cursor"] = (picker["cursor"] + delta) % count

    def _picker_jump(self, position: int) -> None:
        picker = self.path_picker
        if picker is None:
            return
        count = len(picker["rows"])
        picker["cursor"] = 0 if count == 0 else min(count - 1, position)

    def _picker_key(self, key: str) -> None:
        if key in ("j", "J"):
            self._picker_move(1)
        elif key in ("k", "K"):
            self._picker_move(-1)
        elif key == "g":
            self._picker_jump(0)
        elif key == "G":
            self._picker_jump(10 ** 6)
        elif key in ("q", "Q"):
            self.path_picker = None
            self.flash("Отменено — папка библиотеки не изменилась")
        elif key == "u":
            self._picker_parent()

    def _picker_parent(self) -> None:
        picker = self.path_picker
        if picker is None:
            return
        parent = picker["dir"].parent
        if parent == picker["dir"]:
            return  # корень
        picker["dir"] = parent
        picker["cursor"] = 0
        picker["offset"] = 0
        self._picker_scan()

    def _picker_enter(self) -> None:
        picker = self.path_picker
        if picker is None:
            return
        rows = picker["rows"]
        index = picker["cursor"]
        if not (0 <= index < len(rows)):
            return
        kind, _label, payload = rows[index]
        if kind == "confirm":
            self._picker_confirm()
        elif kind == "parent":
            self._picker_parent()
        elif kind == "manual":
            current = str(picker["dir"])
            self.path_picker = None
            self._prompt_library_path(current)
        elif kind == "dir" and payload is not None:
            # _picker_scan сам покажет ошибку в списке, если каталог не читается
            picker["dir"] = payload
            picker["cursor"] = 0
            picker["offset"] = 0
            self._picker_scan()

    def _picker_confirm(self) -> None:
        picker = self.path_picker
        if picker is None:
            return
        chosen = str(picker["dir"])
        self.path_picker = None
        self._apply_library_dir(chosen)

    def _apply_library_dir(self, raw: str) -> None:
        ok, message, old_dir, tracks_here = self.app.set_library_dir(raw)
        self.flash(message)
        if not ok or not tracks_here:
            return

        def submit(value: str) -> None:
            self.prompt = None
            if value.strip().lower() in ("y", "yes", "д", "да"):
                self.app.move_library_tracks(old_dir)
                self.flash(f"Переношу треков: {tracks_here} в новую папку…")
            else:
                self.flash(f"Старые треки остаются в {old_dir}")

        self.prompt = Prompt(
            f"Перенести {tracks_here} трек(ов) из старой папки? (y/n): ", "",
            submit, hint="y — перенести файлы и обновить пути · пустой Enter — оставить",
        )

    def _prompt_library_path(self, initial: str) -> None:
        def submit(value: str) -> None:
            self.prompt = None
            if not value.strip():
                return
            self._apply_library_dir(value.strip())

        self.prompt = Prompt(
            "Папка библиотеки: ", initial, submit,
            hint="Полный путь (~ поддерживается) · Enter — применить · Esc — отмена",
        )

    # ------------------------------------------------------------------- drawing
    def _draw(self) -> None:
        stdscr = self.stdscr
        if stdscr is None:
            return
        height, width = stdscr.getmaxyx()
        self._last_size = (height, width)
        stdscr.erase()
        if height < 16 or width < 60:
            self._safe_add(0, 0, "Терминал слишком мал — нужно минимум 60x16", self._attr(C_BAD, bold=True))
            stdscr.refresh()
            return

        self._draw_header(width)
        self.rows = []
        top = 2
        bottom = height - 4
        if self.view == "search":
            self.rows = self._search_rows()
        elif self.view == "downloads":
            self.rows = self._download_rows()
        elif self.view == "library":
            self.rows = self._track_rows(self.app.library_tracks(), empty="Библиотека пуста — найди треки через /")
        elif self.view == "liked":
            self.rows = self._track_rows(self.app.liked_tracks(), empty="Пока ничего не лайкнуто")
        elif self.view == "playlists":
            self.rows = self._playlist_rows()

        self._draw_rows(top, bottom, width)
        self._draw_player(bottom, width)
        if self.prompt is not None:
            self._draw_prompt(height, width)
        elif self.path_picker is not None:
            self._draw_picker(bottom, width)
        elif self.modal_session is not None:
            self._draw_modal(bottom, width)
        elif self.show_help:
            self._draw_help(bottom, width)
        stdscr.refresh()

    def _safe_add(self, y: int, x: int, text: str, attr: int = 0) -> None:
        if self.stdscr is None:
            return
        height, width = self.stdscr.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        try:
            self.stdscr.addstr(y, x, text[: max(0, width - x - 1)], attr)
        except curses.error:
            pass

    def _draw_header(self, width: int) -> None:
        tabs = []
        for index, view in enumerate(VIEWS, start=1):
            marker = f"{index}:{VIEW_TITLES[view]}"
            tabs.append(marker)
        self._safe_add(0, 0, " ", 0)
        x = 1
        for index, view in enumerate(VIEWS):
            active = view == self.view
            label = f" {index + 1} {VIEW_TITLES[view]} "
            attr = self._attr(C_SELECT, bold=True) if active else self._attr(C_DIM)
            self._safe_add(0, x, label, attr)
            x += len(label) + 1
        proxy = self.app.proxy_label()
        status = f"proxy: {proxy}" if proxy else "proxy: нет (напрямую)"
        self._safe_add(0, max(x, width - len(status) - 2), status, self._attr(C_DIM))
        self._safe_add(1, 0, "─" * (width - 1), self._attr(C_DIM))

    def _draw_rows(self, top: int, bottom: int, width: int) -> None:
        height = bottom - top
        if height <= 0:
            return
        count = len(self.rows)
        key = self._cursor_key()
        cursor = min(self.cursor.get(key, 0), max(0, count - 1))
        self.cursor[key] = cursor
        offset = self.offset.get(key, 0)
        if cursor < offset:
            offset = cursor
        elif cursor >= offset + height:
            offset = cursor - height + 1
        self.offset[key] = offset
        if not count:
            return
        for index in range(offset, min(count, offset + height)):
            row = self.rows[index]
            selected = index == cursor
            y = top + (index - offset)
            attr = self._attr(C_SELECT) if selected else self._attr(C_DEFAULT)
            text = row.text
            if row.extra:
                text = f"{row.text}  {row.extra}"
            self._safe_add(y, 0, " " * (width - 1), attr if selected else 0)
            self._safe_add(y, 1, text, attr)
        if count > height:
            info = f"{cursor + 1}/{count}"
            self._safe_add(bottom - 1, max(0, width - len(info) - 2), info, self._attr(C_DIM))

    # --------------------------------------------------------------------- views
    def _search_rows(self) -> list[Row]:
        if self.app.searching:
            return [Row("info", None, "Ищу через прокси…")]
        results = self.app.search_results
        if not results:
            hint = "Нажми / и введи запрос — ищу по торрент-индексам через прокси"
            return [Row("info", None, self.app.search_error or hint)]
        rows = []
        for result in results:
            text = f"{result.seeders:>4}  {result.size_text:>8}  {result.category_text:<7} {result.name[:150]}"
            rows.append(Row("result", result, text, meta={"hash": result.info_hash}))
        return rows

    def _download_rows(self) -> list[Row]:
        sessions = self.app.sessions_snapshot()
        if not sessions:
            return [Row("info", None, "Нет загрузок — найди торрент через /")]
        rows = []
        for snap, session in sessions:
            state_icon = {
                STATE_METADATA: "◌",
                STATE_DOWNLOADING: "↓",
                STATE_COMPLETE: "✓",
                STATE_ERROR: "✗",
                STATE_STOPPED: "■",
            }.get(snap.state, "?")
            bar = self._bar(snap.progress, 12)
            text = (
                f"{state_icon} {bar} {snap.progress_text()} {snap.speed_text():>9} "
                f"p{snap.peers:<3} {snap.name[:90]}"
            )
            extra = ""
            if not hasattr(session, "metainfo"):
                # soulseek-строка
                if snap.state == STATE_DOWNLOADING:
                    extra = f"eta {snap.eta_text()}"
                elif snap.state == STATE_METADATA:
                    place = getattr(session, "queue_place", 0)
                    extra = f"в очереди у {getattr(session.result, 'username', 'пира')}" + (
                        f" №{place}" if place else ""
                    )
                elif snap.state == STATE_COMPLETE:
                    extra = f"{human_size(snap.downloaded)}"
                elif snap.error:
                    extra = snap.error[:40]
            elif snap.state == STATE_DOWNLOADING:
                extra = f"eta {snap.eta_text()}"
            elif snap.state == STATE_COMPLETE:
                extra = f"{human_size(snap.downloaded)}"
            elif snap.state == STATE_METADATA:
                extra = "метаданные…"
            elif snap.error:
                extra = snap.error[:40]
            rows.append(Row("session", session, text, extra, meta={"state": snap.state}))
        return rows

    def _track_rows(self, tracks, empty: str) -> list[Row]:
        if not tracks:
            return [Row("info", None, empty)]
        rows = []
        for track in tracks:
            heart = "♥" if track.liked else " "
            text = f"{heart} {track.artist[:28]:<28} {track.title[:60]:<60} {track.duration_text:>6}"
            extra = track.album[:24]
            rows.append(Row("track", track, text, extra))
        return rows

    def _playlist_rows(self) -> list[Row]:
        if self.playlist_open:
            return self._track_rows(
                self.app.playlist_tracks(self.playlist_open),
                "Плейлист пуст — добавь треки клавишей a",
            )
        playlists = self.app.playlist_list()
        if not playlists:
            return [Row("info", None, "Плейлистов нет — создай клавишей N")]
        return [
            Row("playlist", entry, f"♪ {entry[1][:60]:<60} {entry[2]:>3} трек(ов)", "")
            for entry in playlists
        ]

    def _bar(self, fraction: float, width: int) -> str:
        fraction = max(0.0, min(1.0, fraction))
        filled = int(round(fraction * width))
        return "█" * filled + "░" * (width - filled)

    def _draw_player(self, bottom: int, width: int) -> None:
        player = self.app.player
        state = player.state()
        current = player.current
        self._safe_add(bottom, 0, "─" * (width - 1), self._attr(C_DIM))

        if current is None:
            title = "нет воспроизведения — Enter на треке, чтобы играть"
            attr = self._attr(C_DIM)
        else:
            title = current.label
            attr = self._attr(C_ACCENT, bold=True)
        icon = {"playing": "▶", "paused": "⏸", "idle": "■", "error": "✗"}.get(state.status, "·")
        self._safe_add(bottom + 1, 1, icon, self._attr(C_GOOD if state.status == "playing" else C_DIM, bold=True))
        self._safe_add(bottom + 1, 3, title[: max(10, width - 24)], attr)

        bar_width = max(10, min(46, width - 40))
        times = (
            f"{human_time(state.position)}/{human_time(state.duration)}"
            if state.duration > 0
            else "--:--/--:--"
        )
        self._safe_add(bottom + 2, 1, f"{self._bar(state.fraction, bar_width)} {times}", self._attr(C_DIM))

        # single-width labels only: emoji are double-width in most terminals and
        # would push this right-aligned block off screen
        tail = " ".join(
            part
            for part in (
                f"rnd:{'on' if state.shuffle else 'off'}",
                f"rep:{state.repeat}",
                f"vol:{state.volume:3d}",
                f"q:{state.index + 1}/{state.queue_len}" if state.queue_len else "",
            )
            if part
        )
        self._safe_add(bottom + 2, max(1, width - len(tail) - 2), tail, self._attr(C_DIM))

        message = self.status if time.time() - self.status_at < 6 else state.message
        if message:
            self._safe_add(bottom + 3, 1, message[: width - 2], self._attr(C_WARN))

    def _draw_prompt(self, height: int, width: int) -> None:
        prompt = self.prompt
        if prompt is None:
            return
        y = height - 1
        text = f"{prompt.label}{prompt.value}"
        self._safe_add(y, 0, " " * (width - 1), self._attr(C_SELECT))
        self._safe_add(y, 0, text[: width - 2], self._attr(C_SELECT, bold=True))
        if prompt.hint:
            self._safe_add(y - 1, 1, prompt.hint[: width - 2], self._attr(C_DIM))
        try:
            self.stdscr.move(y, min(width - 2, len(text)))
        except curses.error:
            pass

    def _draw_modal(self, bottom: int, width: int) -> None:
        session = self.modal_session
        if session is None:
            return
        snap = session.snapshot()
        meta = session.metainfo
        top = 3
        box_bottom = bottom - 1  # row of the closing border, kept clear of the player separator
        box_width = width - 4
        title = f" {snap.name[:box_width - 24]} "
        self._safe_add(top, 2, "┌" + "─" * (box_width - 2) + "┐", self._attr(C_ACCENT))
        self._safe_add(top, 2, "┤" + title, self._attr(C_ACCENT, bold=True))

        if meta is None:
            self._safe_add(top + 1, 3, "получаю метаданные через прокси — файлы появятся после обмена ut_metadata…",
                           self._attr(C_DIM))
            self._safe_add(top + 2, 3, f"пиров: {snap.peers}  состояние: {snap.state}", self._attr(C_DIM))
        else:
            files = meta.files
            if self.modal_cursor >= len(files):
                self.modal_cursor = max(0, len(files) - 1)
            view_height = max(1, box_bottom - 1 - (top + 1))
            if self.modal_cursor < self.modal_offset:
                self.modal_offset = self.modal_cursor
            elif self.modal_cursor >= self.modal_offset + view_height:
                self.modal_offset = self.modal_cursor - view_height + 1
            for index in range(self.modal_offset, min(len(files), self.modal_offset + view_height)):
                f = files[index]
                selected = index == self.modal_cursor
                chosen = f.index in session.selected_files
                progress = session.file_progress(f.index)
                mark = "[x]" if chosen else "[ ]"
                ready = "✓" if session.file_ready(f.index) else f"{progress * 100:3.0f}%"
                icon = "♪" if f.is_audio else "·"
                text = f"{mark} {icon} {ready:>4} {human_size(f.length):>8}  {f.path[:box_width - 24]}"
                attr = self._attr(C_SELECT) if selected else (
                    self._attr(C_GOOD) if chosen and f.is_audio else self._attr(C_DEFAULT)
                )
                self._safe_add(top + 1 + (index - self.modal_offset), 3, text[:box_width - 4], attr)
        footer = "Enter выбор · p качать+играть · s все аудио · x отменить · q закрыть"
        self._safe_add(box_bottom - 1, 3, footer[:box_width - 4], self._attr(C_DIM))
        self._safe_add(box_bottom, 2, "└" + "─" * (box_width - 2) + "┘", self._attr(C_ACCENT))

    def _draw_picker(self, bottom: int, width: int) -> None:
        picker = self.path_picker
        if picker is None:
            return
        import shutil as _shutil

        top = 3
        box_bottom = bottom - 1
        box_width = width - 4
        free_text = ""
        try:
            free_text = " · свободно " + human_size(_shutil.disk_usage(picker["dir"]).free)
        except OSError:
            pass
        title = f" Папка библиотеки: {str(picker['dir'])[:max(20, box_width - 44)]}{free_text} "
        self._safe_add(top, 2, "┌" + "─" * (box_width - 2) + "┐", self._attr(C_ACCENT))
        self._safe_add(top, 2, "┤" + title[:box_width - 3], self._attr(C_ACCENT, bold=True))

        rows = picker["rows"]
        view_height = max(1, box_bottom - 1 - (top + 1))
        cursor = min(picker["cursor"], max(0, len(rows) - 1))
        if cursor < picker["offset"]:
            picker["offset"] = cursor
        elif cursor >= picker["offset"] + view_height:
            picker["offset"] = cursor - view_height + 1
        if picker["error"]:
            self._safe_add(top + 1, 3, f"не читается: {picker['error']}"[:box_width - 4], self._attr(C_BAD))
        for index in range(picker["offset"], min(len(rows), picker["offset"] + view_height)):
            kind, label, _payload = rows[index]
            selected = index == cursor
            if selected:
                attr = self._attr(C_SELECT, bold=kind in ("confirm", "manual"))
            elif kind == "confirm":
                attr = self._attr(C_GOOD, bold=True)
            else:
                attr = self._attr(C_DEFAULT)
            self._safe_add(top + 1 + (index - picker["offset"]), 3, f"  {label}"[:box_width - 4], attr)
        footer = "Enter выбрать/войти · Backspace выше · g/G концы · q/Esc отмена"
        self._safe_add(box_bottom - 1, 3, footer[:box_width - 4], self._attr(C_DIM))
        self._safe_add(box_bottom, 2, "└" + "─" * (box_width - 2) + "┘", self._attr(C_ACCENT))

    def _draw_help(self, bottom: int, width: int) -> None:
        lines = [
            "ПОИСК И ЗАГРУЗКА",
            "  /          поиск: торренты (apibay + solidtorrents) и soulseek",
            "  Enter      на результате — начать загрузку; на загрузке — файлы",
            "  В окне файлов: Enter выбрать/снять · p качать и играть · s все аудио",
            "  d / Ctrl-D отменить загрузку (торрент или soulseek)",
            "",
            "ПЛЕЕР",
            "  space      пауза/продолжить      n / p  следующий / предыдущий",
            "  ← →        ±5 сек                , .    ±10 сек        < >  ±30 сек",
            "  + / -      громкость             s  шаффл              R  повтор",
            "",
            "БИБЛИОТЕКА",
            "  Enter      играть выбранный трек с этой позиции",
            "  l          лайк (трек под курсором или играющий)",
            "  a          добавить в плейлист   N  создать плейлист   d  удалить",
            "  L          сменить папку библиотеки (обзор каталогов; можно",
            "             перенести уже скачанные треки в новое место)",
            "  1..5 / Tab переключение вкладок  r  обновить  ?  справка  Esc  назад",
        ]
        box_width = width - 6
        top = max(2, bottom - 1 - len(lines) - 1)
        self._safe_add(top, 2, "┌" + "─" * box_width + "┐", self._attr(C_ACCENT))
        for offset, line in enumerate(lines):
            attr = self._attr(C_ACCENT, bold=True) if line and not line.startswith(" ") else self._attr(C_DEFAULT)
            self._safe_add(top + 1 + offset, 3, line[: box_width - 2], attr)
        self._safe_add(top + len(lines) + 1, 2, "└" + "─" * box_width + "┘", self._attr(C_ACCENT))
