"""Application layer: wires search, torrent sessions, media pipeline, library, player, UI."""

from __future__ import annotations

import argparse
import os
import queue
import random
import shutil
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import media, search
from .config import Config, load_config, save_config
from .downloader import STATE_COMPLETE, STATE_ERROR, TorrentSession
from .library import Library, Track, dir_prefix
from .net import Proxy
from .player import Playable, Player
from .slsk import SERVER_HOST, SERVER_PORT, SlskClient, SlskFileResult, SlskTransfer
from .torrent import parse_magnet

SESSION_POLL = 0.25


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """Сносить опустевшие каталоги ``start`` и выше, не трогая сам ``stop``."""
    stop_str = str(stop).rstrip(os.sep)
    current = start
    while str(current) != stop_str and str(current).startswith(stop_str + os.sep):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


class App:
    def __init__(self, cfg: Config, ui=None):
        self.cfg = cfg
        self.ui = ui
        self.library = Library(cfg.db_path)
        self.player = Player(volume=cfg.audio_volume)
        self.sessions: dict[str, TorrentSession] = {}
        self.search_results: list = []
        self.searching = False
        self.search_error = ""
        self.last_query = ""

        # SoulSeek-клиент: логин фоном, чтобы первый поиск уже видел сеть.
        # Учётка генерируется один раз и сразу сохраняется — так «призрачных»
        # регистраций на сервере не плодим при аварийных перезапусках.
        self.slsk: SlskClient | None = None
        if cfg.slsk_enabled:
            if not cfg.slsk_user or not cfg.slsk_pass:
                cfg.slsk_user = cfg.slsk_user or (
                    "mp3player_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
                )
                cfg.slsk_pass = cfg.slsk_pass or (
                    "".join(random.choices(string.ascii_letters + string.digits, k=14))
                )
                try:
                    save_config(cfg)
                except Exception:
                    pass
            self.slsk = SlskClient(cfg)
            self.slsk.start()

        self.pending_play: dict[str, int] = {}
        self._imported: set[tuple[str, int]] = set()
        self._auto_import_running: set[str] = set()

        self._ui_queue: queue.Queue = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="app")
        self._tracks_cache: list[Track] = []
        self._liked_cache: list[Track] = []
        self._cache_at = 0.0
        self._cache_dirty = True
        self._playing_track_id: int | None = None
        self._last_session_poll = 0.0
        self._shutdown = False

    # ------------------------------------------------------------------ helpers
    def flash(self, message: str) -> None:
        if self.ui is not None:
            self.ui.flash(message)

    def proxy_label(self) -> str:
        raw = self.cfg.effective_proxy()
        if not raw:
            return ""
        try:
            return Proxy.parse(raw).url()
        except Exception:
            return raw

    def now_playing_track_id(self) -> int | None:
        current = self.player.current
        return current.track_id if current is not None else None

    def sessions_snapshot(self):
        out = []
        for session in reversed(list(self.sessions.values())):
            out.append((session.snapshot(), session))
        if self.slsk is not None:
            for transfer in reversed(self.slsk.downloads()):
                out.append((transfer.snapshot(), transfer))
        return out

    # ------------------------------------------------------------------- search
    def start_search(self, query: str) -> None:
        self.last_query = query
        self.searching = True
        self.search_error = ""
        self.search_results = []

        def job():
            outcome = {"torrent": [], "slsk": [], "torrent_error": "", "slsk_error": ""}

            def torrent_part():
                try:
                    outcome["torrent"] = search.search_tracks(self.cfg, query, limit=self.cfg.search_limit)
                except Exception as exc:  # noqa: BLE001
                    outcome["torrent_error"] = str(exc)

            def slsk_part():
                if self.slsk is None:
                    return
                results, error = self.slsk.search(query, max(50, self.cfg.search_limit))
                outcome["slsk"] = results
                outcome["slsk_error"] = error

            # торренты и soulseek параллельно: суммарно — окно самого медленного
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="srch") as pool:
                pool.submit(torrent_part)
                pool.submit(slsk_part)

            def apply():
                self.searching = False
                torrents = outcome["torrent"]
                slsk_results = outcome["slsk"]
                self.search_results = list(torrents) + list(slsk_results)
                # ошибкой считаем только полный провал: оба источника упали,
                # либо единственный источник не ответил
                if not torrents and not slsk_results:
                    parts = [p for p in (outcome["torrent_error"], outcome["slsk_error"]) if p]
                    if parts:
                        self.search_error = "; ".join(parts)[:220]
                        self.flash(f"ошибка поиска: {self.search_error}")
                        return
                if not self.search_results:
                    self.search_error = "ничего не найдено"
                    self.flash("ничего не найдено")
                    return
                extra = f" (+{len(slsk_results)} soulseek)" if slsk_results else ""
                self.search_error = ""
                if not slsk_results and outcome["slsk_error"]:
                    self.search_error = f"soulseek: {outcome['slsk_error']}"
                    self.flash(f"найдено: {len(torrents)}; soulseek недоступен: {outcome['slsk_error'][:60]}")
                else:
                    self.flash(f"найдено: {len(torrents)}{extra}")

            return apply

        self._bg(job)

    def refresh_library(self) -> None:
        self._cache_dirty = True

    def library_tracks(self) -> list[Track]:
        self._refresh_cache()
        return self._tracks_cache

    def liked_tracks(self) -> list[Track]:
        self._refresh_cache()
        return self._liked_cache

    def _refresh_cache(self) -> None:
        if not self._cache_dirty and time.time() - self._cache_at < 3.0:
            return
        try:
            self._tracks_cache = self.library.tracks()
            self._liked_cache = self.library.liked()
        except Exception:
            self._tracks_cache, self._liked_cache = [], []
        self._cache_at = time.time()
        self._cache_dirty = False

    def playlist_list(self):
        return self.library.playlists()

    def playlist_names(self) -> list[str]:
        return self.library.playlist_names()

    def playlist_tracks(self, name: str) -> list[Track]:
        return self.library.playlist_tracks(name)

    def create_playlist(self, name: str) -> int | None:
        return self.library.create_playlist(name)

    def delete_playlist(self, name: str) -> None:
        self.library.delete_playlist(name)

    def add_to_playlist(self, name: str, track_id: int) -> bool:
        return self.library.add_to_playlist(name, track_id)

    def remove_from_playlist(self, name: str, track_id: int) -> None:
        self.library.remove_from_playlist(name, track_id)

    def toggle_like(self, track_id: int) -> bool:
        liked = self.library.toggle_like(track_id)
        self._cache_dirty = True
        return liked

    # ---------------------------------------------------------------- downloads
    def download_result(self, result) -> TorrentSession | SlskTransfer | None:
        if isinstance(result, SlskFileResult):
            return self._download_slsk(result)
        return self._download_torrent(result)

    def _download_slsk(self, result: SlskFileResult) -> SlskTransfer | None:
        if self.slsk is None:
            self.flash("soulseek отключён (slsk_enabled в конфиге)")
            return None
        for transfer in self.slsk.downloads():
            if transfer.result.username == result.username and transfer.result.path == result.path:
                return transfer
        transfer = self.slsk.download(result)
        transfer.on_complete = lambda t: self._ui_queue.put(("slsk_complete", t))
        return transfer

    def _download_torrent(self, result: search.SearchResult) -> TorrentSession | None:
        key = result.info_hash.upper()
        existing = self.sessions.get(key)
        if existing is not None:
            return existing
        trackers = list(self.cfg.resolve_trackers()) + list(self.cfg.extra_search_trackers)
        try:
            magnet = parse_magnet(result.magnet(trackers))
        except Exception as exc:  # noqa: BLE001
            self.flash(f"плохой magnet: {exc}")
            return None
        session = TorrentSession(
            magnet.info_hash,
            self.cfg,
            name=result.name,
            trackers=magnet.trackers,
            source=result.name,
            on_complete=lambda s: self._ui_queue.put(("complete", s.hex_hash)),
            on_metadata=lambda s: self._ui_queue.put(("metadata", s.hex_hash)),
        )
        self.sessions[key] = session
        session.start()
        return session

    def cancel_session(self, session) -> None:
        if isinstance(session, SlskTransfer):
            session.stop()
            if session.state not in ("complete",):
                session.mark("stopped")
            if self.slsk is not None:
                with self.slsk._transfers_lock:
                    try:
                        self.slsk.transfers.remove(session)
                    except ValueError:
                        pass
            return
        session.stop()
        self.pending_play.pop(session.hex_hash, None)
        self.sessions.pop(session.hex_hash, None)

    def request_play(self, session: TorrentSession, file_index: int) -> None:
        session.set_focus(file_index)
        self.pending_play[session.hex_hash] = file_index

    # ------------------------------------------------------------------ library
    def set_library_dir(self, raw: str) -> tuple[bool, str, str, int]:
        """Сменить папку библиотеки (куда импортируются треки).

        Возвращает ``(применено, сообщение, старая_папка, треков_в_старой)``.
        Конфиг сохраняется сразу же — смена не должна теряться при аварийном
        выходе.
        """
        old = self.cfg.library_dir
        try:
            path = Path(os.path.expanduser(str(raw))).expanduser()
            path = Path(os.path.abspath(str(path)))
            if not path.exists():
                path.mkdir(parents=True)
            if not path.is_dir():
                return False, "это не каталог", old, 0
            if not os.access(path, os.W_OK):
                return False, "нет прав на запись в эту папку", old, 0
        except OSError as exc:
            return False, f"не удалось открыть папку: {exc}", old, 0
        if path == Path(os.path.abspath(os.path.expanduser(old))):
            return False, "это уже текущая папка библиотеки", old, 0
        tracks_here = len(self.library.tracks_under(old))
        self.cfg.library_dir = str(path)
        self.cfg.ensure_dirs()
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self._cache_dirty = True
        return True, f"папка библиотеки: {path}", old, tracks_here

    def move_library_tracks(self, old_dir: str) -> None:
        """Фоном перенести файлы треков из ``old_dir`` в текущую библиотеку
        и переписать пути в БД (UNIQUE-конфликты — через « (n)» суффиксы)."""

        def job():
            new_dir = Path(self.cfg.library_dir)
            moved = missing = 0
            errors: list[str] = []
            for track_id, old_path in self.library.tracks_under(old_dir):
                relative = old_path[len(dir_prefix(old_dir)):]
                target = new_dir / relative
                final = target
                # свободное имя на диске и в БД
                attempt = 0
                while True:
                    clash_db = False
                    if final.exists():
                        attempt += 1
                        final = target.with_name(f"{target.stem} ({attempt}){target.suffix}")
                        continue
                    if not self.library.update_track_path(track_id, str(final)):
                        clash_db = True
                    if not clash_db:
                        break
                    attempt += 1
                    final = target.with_name(f"{target.stem} ({attempt}){target.suffix}")
                source = Path(old_path)
                try:
                    if source.exists():
                        final.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(source), str(final))
                        moved += 1
                        _prune_empty_dirs(source.parent, Path(old_dir))
                    else:
                        missing += 1
                except OSError as exc:
                    errors.append(f"{Path(old_path).name}: {exc}")

            def apply():
                self._cache_dirty = True
                parts = [f"перенесено: {moved}"]
                if missing:
                    parts.append(f"пропало с диска: {missing}")
                if errors:
                    parts.append(f"ошибок: {len(errors)} ({errors[0][:40]})")
                self.flash(", ".join(parts))

            return apply

        self._bg(job)

    # ----------------------------------------------------------------- playback
    def play_tracks(self, tracks: list[Track], start: int = 0) -> None:
        playables = [Playable.from_track(track) for track in tracks if Path(track.path).exists()]
        if not playables:
            self.flash("файлы не найдены на диске")
            return
        offset = 0
        if 0 <= start < len(tracks):
            target_path = tracks[start].path
            for index, playable in enumerate(playables):
                if playable.path == target_path:
                    offset = index
                    break
        self.player.play_tracks(playables, offset)
        self._mark_played(playables[offset])

    def _mark_played(self, playable: Playable) -> None:
        if playable.track_id:
            self._playing_track_id = playable.track_id
            try:
                self.library.mark_played(playable.track_id)
            except Exception:
                pass

    def toggle_pause(self) -> None:
        self.player.toggle()

    def next_track(self) -> None:
        self.player.next()

    def previous_track(self) -> None:
        self.player.previous()

    def volume(self, delta: int) -> None:
        self.player.set_volume(self.player.volume + delta)

    def seek(self, delta: float) -> None:
        self.player.seek_relative(delta)

    def toggle_shuffle(self) -> None:
        enabled = self.player.toggle_shuffle()
        self.flash(f"шаффл {'вкл' if enabled else 'выкл'}")

    def cycle_repeat(self) -> None:
        mode = self.player.cycle_repeat()
        self.flash({"all": "повтор всего", "one": "повтор трека", "off": "без повтора"}.get(mode, mode))

    # -------------------------------------------------------------------- media
    def _bg(self, fn) -> None:
        def wrapper():
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - report to the UI
                self._ui_queue.put(("error", str(exc)))
                return
            if callable(result):
                self._ui_queue.put(("call", result))

        self._pool.submit(wrapper)

    def _import_file(self, session: TorrentSession, file_index: int, play_after: bool = False) -> None:
        key = (session.hex_hash, file_index)
        if key in self._imported:
            if play_after:
                self._ui_queue.put(("play_imported", (session.hex_hash, file_index)))
            return
        self._imported.add(key)
        source = session.file_path(file_index)
        if source is None:
            return
        meta = session.metainfo
        album_hint = meta.name if meta else session.name

        def job():
            info = media.transcode_to_mp3(source, self.cfg, hint_album=album_hint)
            short = f"{info.artist} - {info.title}"

            def apply():
                track = self.library.add_track(
                    info.path, info.title, info.artist, info.album, info.track_no,
                    info.duration, info.bitrate, info.size, session.hex_hash, session.name,
                )
                self._cache_dirty = True
                if play_after:
                    self.player.play_tracks([Playable.from_track(track)], 0)
                    self._mark_played(Playable.from_track(track))
                    self.flash(f"играю: {short}")
                else:
                    self.flash(f"в библиотеке: {short}")

            return apply

        self._bg(job)

    def _import_slsk(self, transfer: SlskTransfer) -> None:
        """Готовый файл SoulSeek — в библиотеку (с тегами), сырьё убрать."""
        key = ("slsk", transfer.hex_hash)
        if key in self._imported:
            return
        self._imported.add(key)
        source = transfer.file_path
        if not Path(source).exists():
            return
        album_hint = transfer.result.album_hint
        keep_raw = self.cfg.keep_torrent_data

        def job():
            info = media.transcode_to_mp3(source, self.cfg, hint_album=album_hint)
            short = f"{info.artist} - {info.title}"

            def apply():
                track = self.library.add_track(
                    info.path, info.title, info.artist, info.album, info.track_no,
                    info.duration, info.bitrate, info.size,
                    transfer.hex_hash, f"slsk:{transfer.result.username}",
                )
                self._cache_dirty = True
                self.flash(f"в библиотеке: {short}")

            if not keep_raw:
                try:
                    Path(source).unlink()
                    if source.parent.exists() and not any(source.parent.iterdir()):
                        source.parent.rmdir()
                        parent = source.parent.parent
                        if parent != Path(self.cfg.download_dir) / "slsk" and not any(parent.iterdir()):
                            parent.rmdir()
                except OSError:
                    pass
            return apply

        self._bg(job)

    def _import_selected(self, session: TorrentSession) -> None:
        if session.hex_hash in self._auto_import_running:
            return
        meta = session.metainfo
        if meta is None:
            return
        self._auto_import_running.add(session.hex_hash)
        indexes = [f.index for f in meta.files if f.is_audio and f.index in session.selected_files]
        missing = [i for i in indexes if (session.hex_hash, i) not in self._imported]
        if not missing:
            self._auto_import_running.discard(session.hex_hash)
            return
        keep_raw = self.cfg.keep_torrent_data

        def job():
            imported = 0
            errors = 0
            for index in missing:
                source = session.file_path(index)
                if source is None or not Path(source).exists():
                    continue
                try:
                    info = media.transcode_to_mp3(source, self.cfg, hint_album=meta.name)
                except Exception:
                    errors += 1
                    continue
                self._imported.add((session.hex_hash, index))
                imported += 1
                self.library.add_track(
                    info.path, info.title, info.artist, info.album, info.track_no,
                    info.duration, info.bitrate, info.size, session.hex_hash, session.name,
                )
            if not keep_raw and not errors:
                shutil.rmtree(session.base_dir, ignore_errors=True)

            def apply():
                self._cache_dirty = True
                self._auto_import_running.discard(session.hex_hash)
                if imported:
                    self.flash(f"импортировано треков: {imported}")
                if errors:
                    self.flash(f"ошибка импорта: {errors} файл(ов)")

            return apply

        self._bg(job)

    # --------------------------------------------------------------------- tick
    def tick(self) -> None:
        self.player.poll()
        self._drain_queue()
        now = time.monotonic()
        if now - self._last_session_poll >= SESSION_POLL:
            self._last_session_poll = now
            self._poll_sessions()

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "call":
                try:
                    payload()
                except Exception as exc:  # noqa: BLE001
                    self.flash(f"ошибка: {exc}")
            elif kind == "error":
                self.flash(f"ошибка: {payload}")
            elif kind == "complete":
                session = self.sessions.get(payload)
                if session is not None:
                    self.flash(f"загрузка готова: {session.display_name()[:40]}")
                    self._import_selected(session)
                    self.pending_play.pop(payload, None)
            elif kind == "metadata":
                session = self.sessions.get(payload)
                if session is not None and session.metainfo is not None:
                    audio = len(session.metainfo.audio_files())
                    self.flash(f"метаданные получены: {audio} аудиофайл(ов)")
            elif kind == "slsk_complete":
                self._import_slsk(payload)
            elif kind == "play_imported":
                pass

    def _poll_sessions(self) -> None:
        for key, file_index in list(self.pending_play.items()):
            session = self.sessions.get(key)
            if session is None:
                self.pending_play.pop(key, None)
                continue
            if session.state in (STATE_ERROR, "stopped"):
                self.pending_play.pop(key, None)
                self.flash(f"загрузка прервана: {session.error or session.state}")
                continue
            if session.file_ready(file_index):
                self.pending_play.pop(key, None)
                self._import_file(session, file_index, play_after=True)
            elif session.state == STATE_COMPLETE:
                self.pending_play.pop(key, None)
        for session in list(self.sessions.values()):
            if session.state == STATE_COMPLETE:
                self._import_selected(session)

    # ----------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        for session in list(self.sessions.values()):
            try:
                session.stop()
            except Exception:
                pass
        if self.slsk is not None:
            try:
                self.slsk.stop()
            except Exception:
                pass
        self.player.shutdown()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.library.close()


# ----------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mp3play",
        description="Терминальный mp3-плеер: ищет треки в торрентах через прокси, качает, лайкает и складывает в плейлисты.",
    )
    parser.add_argument("--config", help="путь к config.json")
    parser.add_argument("--proxy", help="прокси для трекеров/пиров/поиска (например socks5h://127.0.0.1:9050)")
    parser.add_argument("--no-proxy", action="store_true", help="работать напрямую, игнорируя переменные окружения")
    parser.add_argument("--no-slsk", action="store_true", help="отключить soulseek (искать/качать только торренты)")
    parser.add_argument("--download-dir", help="куда качать торренты")
    parser.add_argument("--library-dir", help="куда складывать готовые mp3")
    parser.add_argument("--max-peers", type=int, help="максимум одновременных пиров на торрент")
    parser.add_argument("--bitrate", help="битрейт mp3 при конвертации (например 320k)")
    parser.add_argument("--query", "-q", help="сразу выполнить поиск при старте")
    parser.add_argument("--check", action="store_true", help="проверить окружение, прокси и инструменты")
    parser.add_argument("--write-config", action="store_true", help="сохранить текущие параметры в config.json")
    parser.add_argument("--version", action="version", version="mp3player 1.0")
    return parser


def apply_args(cfg: Config, args) -> Config:
    if args.proxy:
        cfg.proxy = args.proxy
        cfg.search_proxy = args.proxy
    if args.no_proxy:
        cfg.proxy = ""
        cfg.search_proxy = ""
    if args.no_slsk:
        cfg.slsk_enabled = False
    if args.download_dir:
        cfg.download_dir = str(Path(args.download_dir).expanduser())
    if args.library_dir:
        cfg.library_dir = str(Path(args.library_dir).expanduser())
    if args.max_peers:
        cfg.max_peers = max(2, args.max_peers)
    if args.bitrate:
        cfg.mp3_bitrate = args.bitrate
    cfg.ensure_dirs()
    return cfg


def run_check(cfg: Config) -> int:
    print(f"конфиг:        {cfg.db_path}")
    print(f"загрузки:      {cfg.download_dir}")
    print(f"библиотека:    {cfg.library_dir}")
    raw = cfg.effective_proxy()
    print(f"прокси:        {raw or 'нет — трафик идёт напрямую'}")
    ok, error = media.tools_available()
    print(f"ffmpeg/ffprobe:{' ok' if ok else ' ' + error}")
    print(f"ffplay:        {'ok' if shutil.which('ffplay') else 'нет — воспроизведение не заработает'}")
    if shutil.which("ffplay") is None:
        print("  подсказка: pacman -S ffmpeg   (ffplay входит в пакет ffmpeg)")
    trackers = cfg.resolve_trackers()
    print(f"трекеров:      {len(trackers)} HTTP(S)")
    if raw:
        try:
            proxy = Proxy.parse(raw)
        except Exception as exc:
            print(f"прокси невалиден: {exc}")
            return 2
        try:
            response = proxy.open_tcp("apibay.org", 443, timeout=12)
            response.close()
            print("туннель:       ok (apibay.org:443 через прокси)")
        except Exception as exc:
            print(f"туннель:       FAILED — {exc}")
            return 2
    try:
        results = search.search_tracks(cfg, "daft punk", limit=3)
        print(f"поиск:         ok, {len(results)} результатов")
    except Exception as exc:
        print(f"поиск:         FAILED — {exc}")
        return 2
    if cfg.slsk_enabled:
        print(f"soulseek:      server.slsknet.org:{SERVER_PORT}", end=" ")
        try:
            import socket as _socket

            if raw:
                Proxy.parse(raw).open_tcp(SERVER_HOST, SERVER_PORT, timeout=12).close()
            else:
                _socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=12).close()
            print("ok")
        except Exception as exc:
            print(f"FAILED — {exc}")
    else:
        print("soulseek:      отключён (--no-slsk)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.config).expanduser() if args.config else None)
    cfg = apply_args(cfg, args)
    if args.write_config:
        save_config(cfg, Path(args.config).expanduser() if args.config else None)
        print(f"сохранено: {args.config or '~/.config/mp3player/config.json'}")
        return 0
    if args.check:
        return run_check(cfg)

    ok, error = media.tools_available()
    if not ok:
        print(f"не хватает инструментов: {error}", file=sys.stderr)

    from .ui import Interface

    app = App(cfg)
    interface = Interface(app)
    app.ui = interface
    if args.query:
        app.start_search(args.query)
    try:
        interface.run()
    finally:
        app.shutdown()
        save_config(cfg, Path(args.config).expanduser() if args.config else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
