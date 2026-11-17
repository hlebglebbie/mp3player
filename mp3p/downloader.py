"""Torrent download session: piece picking, disk I/O, peer pool, tracker loop.

One :class:`TorrentSession` == one torrent. It is driven by a manager thread
(announce + peer pool + progress) and N :class:`mp3p.peer.PeerConnection`
threads that do the actual wire traffic. All peer sockets are opened through the
configured proxy, so nothing bypasses it.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import bencode
from .config import Config, human_speed
from .net import Proxy
from .peer import BLOCK_SIZE, METADATA_PIECE_SIZE, PeerConnection
from .torrent import Metainfo, TorrentError, TorrentFile, parse_info_dict
from .tracker import PeerAddress, announce_parallel, make_peer_id

STATE_METADATA = "metadata"
STATE_DOWNLOADING = "downloading"
STATE_COMPLETE = "complete"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"

SPEED_WINDOW = 6.0
MD_RETRY = 5.0
# Сколько секунд кусок может не получать блоки, прежде чем second peer
# разрешается его «перехватить» (дублируем запросы только по делу).
PIECE_STALL_AFTER = 10.0


@dataclass
class SessionSnapshot:
    info_hash: str
    name: str
    state: str
    progress: float
    downloaded: int
    total: int
    speed: float
    peers: int
    seeds: int
    leechers: int
    eta: float
    error: str
    pieces_have: int
    pieces_total: int
    files: int
    selected: int
    focus_file: int
    trackers_errors: list[str] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.state in (STATE_METADATA, STATE_DOWNLOADING)

    @property
    def is_ready(self) -> bool:
        return self.state == STATE_COMPLETE

    def progress_text(self) -> str:
        return f"{self.progress * 100:5.1f}%"

    def speed_text(self) -> str:
        return human_speed(self.speed) if self.speed > 1 else "-"

    def eta_text(self) -> str:
        if self.state == STATE_COMPLETE:
            return "done"
        if self.speed < 1024 or self.eta <= 0:
            return "--:--"
        from .config import human_time

        return human_time(self.eta)


class TorrentSession:
    def __init__(
        self,
        info_hash: bytes,
        config: Config,
        name: str = "",
        trackers: Iterable[str] = (),
        metainfo: Metainfo | None = None,
        info_blob: bytes | None = None,
        source: str = "",
        on_complete: Callable[["TorrentSession"], None] | None = None,
        on_metadata: Callable[["TorrentSession"], None] | None = None,
    ):
        self.info_hash = info_hash
        self.config = config
        self.name = name or info_hash.hex().upper()
        self.source = source
        self.metainfo = metainfo
        self._info_blob = info_blob
        self.on_complete = on_complete
        self.on_metadata = on_metadata

        self.proxy: Proxy | None = None
        proxy_url = config.effective_proxy()
        if proxy_url:
            try:
                self.proxy = Proxy.parse(proxy_url)
            except Exception:
                self.proxy = None

        self.peer_id = make_peer_id()
        self.listen_port = int(os.environ.get("MP3P_LISTEN_PORT", "6881"))

        tracker_list = list(trackers) or config.resolve_trackers()
        if self.proxy is not None:
            tracker_list = [t for t in tracker_list if t.lower().startswith(("http://", "https://"))]
        self.trackers = _dedupe(tracker_list)

        self.state = STATE_METADATA if metainfo is None else STATE_DOWNLOADING
        self.error = ""
        self.trackers_errors: list[str] = []
        self.seeds = 0
        self.leechers = 0

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._manager: threading.Thread | None = None
        self._announce_thread: threading.Thread | None = None
        self._announce_busy = threading.Event()
        self._next_announce = 0.0
        self._announce_interval = 60

        # metadata exchange
        self.metadata_size = 0
        self._md_pieces: dict[int, bytes] = {}
        self._md_requested: dict[int, float] = {}
        self._md_peers: set[str] = set()

        # piece state
        self.wanted: set[int] = set()
        self.have: set[int] = set()
        self._inflight: dict[int, int] = {}
        # [bytearray piece, set(begin), received] — буфер живёт, пока кусок
        # не собран; смерть одного пира больше НЕ сбрасывает прогресс других.
        self._buffers: dict[int, list] = {}
        self._piece_progress: dict[int, float] = {}
        self._finalizing: set[int] = set()
        self._priority: list[int] = []
        self.focus_file = -1
        self.selected_files: set[int] = set()

        # peers
        self.peers: dict[tuple[str, int], PeerConnection] = {}
        self._candidates: deque[PeerAddress] = deque()
        self._seen: set[tuple[str, int]] = set()
        self._attempts: dict[tuple[str, int], float] = {}
        self.connected_peak = 0

        # stats
        self._samples: deque[tuple[float, int]] = deque()
        self.total_received = 0
        self.speed = 0.0
        self.finished_at = 0.0
        self.started_at = time.time()

        # disk
        base_name = (metainfo.hex_hash if metainfo else info_hash.hex().upper())
        self.base_dir = Path(self.config.download_dir) / base_name
        self._files: dict[int, object] = {}
        self._allocated = False

        if metainfo is not None:
            self._on_metainfo_ready(initial=True)

    # ------------------------------------------------------------------ helpers
    @property
    def hex_hash(self) -> str:
        return self.info_hash.hex().upper()

    def metadata_blob(self) -> bytes | None:
        return self._info_blob

    def display_name(self) -> str:
        with self._lock:
            if self.metainfo is not None and self.metainfo.name:
                return self.metainfo.name
            return self.name

    # -------------------------------------------------------------------- setup
    def _on_metainfo_ready(self, initial: bool = False) -> None:
        meta = self.metainfo
        if meta is None:
            return
        self.name = meta.name or self.name
        audio = {f.index for f in meta.audio_files()}
        self.selected_files = audio if audio else {f.index for f in meta.files if f.length}
        self.wanted = meta.pieces_for_files(self.selected_files)
        self._priority = sorted(self.wanted)
        if not initial:
            self.state = STATE_DOWNLOADING
        self._allocate_files()
        if self.on_metadata is not None and not initial:
            try:
                self.on_metadata(self)
            except Exception:
                pass

    def _allocate_files(self) -> None:
        meta = self.metainfo
        if meta is None or self._allocated:
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        for f in meta.files:
            target = self.path_for_file(f)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.stat().st_size != f.length:
                with open(target, "wb") as handle:
                    if f.length:
                        handle.truncate(f.length)
        self._allocated = True

    def path_for_file(self, f: TorrentFile) -> Path:
        path = self.base_dir
        for part in f.safe_path_parts():
            path = path / part
        return path

    def file_path(self, index: int) -> Path | None:
        meta = self.metainfo
        if meta is None:
            return None
        f = meta.file_by_index(index)
        return self.path_for_file(f) if f else None

    # -------------------------------------------------------------------- state
    def start(self) -> None:
        if self._manager and self._manager.is_alive():
            return
        self._stop.clear()
        self._manager = threading.Thread(target=self._run, name=f"session-{self.hex_hash[:8]}", daemon=True)
        self._manager.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            peers = list(self.peers.values())
        for peer in peers:
            peer.stop()
        if self.state not in (STATE_COMPLETE, STATE_ERROR):
            self.state = STATE_STOPPED

    def is_done(self) -> bool:
        return self.state in (STATE_COMPLETE, STATE_ERROR, STATE_STOPPED)

    # ------------------------------------------------------------ peer interface
    def peer_gone(self, peer: PeerConnection) -> None:
        with self._lock:
            key = peer.address.key
            if self.peers.get(key) is peer:
                del self.peers[key]
            for index in list(peer.requested_pieces):
                self._release_locked(index)
            peer.requested_pieces.clear()
            peer.outstanding.clear()
            # Буферы недокачанных кусков СОХРАНЯЮТСЯ: отколовшийся пир не должен
            # уничтожать прогресс, собранный остальными (раньше один рвущийся
            # пир обнулял все частичные куски и скорость падала в разы).
            # Чистим только кэши кусков, выпавших из выбора.
            stale = [
                index for index in self._buffers
                if index not in self.wanted and index not in self.have
            ]
            for index in stale:
                self._buffers.pop(index, None)
                self._piece_progress.pop(index, None)

    def on_extended_handshake(self, peer: PeerConnection) -> None:
        with self._lock:
            if self.metainfo is not None or not peer.their_ut_metadata:
                return
            if peer.metadata_size:
                self.metadata_size = peer.metadata_size
            self._md_peers.add(peer.label)
        self._pull_metadata(peer)

    def _metadata_piece_count(self) -> int:
        if not self.metadata_size:
            return 0
        return -(-self.metadata_size // METADATA_PIECE_SIZE)

    def _pull_metadata(self, peer: PeerConnection, count: int = 3) -> None:
        """Ask one peer for up to ``count`` missing metadata pieces.

        A request is stamped with a timestamp rather than remembered forever, so a
        silent peer cannot block the swarm: after ``MD_RETRY`` seconds another peer
        asks again.
        """
        if self.metainfo is not None or not peer.their_ut_metadata:
            return
        now = time.monotonic()
        pending: list[int] = []
        with self._lock:
            total = self._metadata_piece_count()
            for index in range(total):
                if index in self._md_pieces:
                    continue
                last = self._md_requested.get(index, 0.0)
                if last and now - last < MD_RETRY:
                    continue
                self._md_requested[index] = now
                pending.append(index)
                if len(pending) >= count:
                    break
        for index in pending:
            peer.request_metadata_piece(index)

    def on_metadata_piece(self, peer: PeerConnection, index: int, data: bytes) -> None:
        complete_blob: bytes | None = None
        with self._lock:
            self._md_requested.pop(index, None)
            if index in self._md_pieces:
                return
            self._md_pieces[index] = data
            count = self._metadata_piece_count()
            if count and len(self._md_pieces) >= count:
                blob = b"".join(self._md_pieces[i] for i in range(count))
                if hashlib.sha1(blob).digest() == self.info_hash:
                    complete_blob = blob
                else:
                    self._md_pieces.clear()
                    self.metadata_size = 0
        if complete_blob is not None:
            self._install_metadata(complete_blob)
            return
        self._pull_metadata(peer)

    def _install_metadata(self, blob: bytes) -> None:
        try:
            info = bencode.decode(blob)
            meta = parse_info_dict(info, self.trackers)
        except (bencode.BencodeError, TorrentError, ValueError) as exc:
            self.error = f"metadata parse failed: {exc}"
            self.state = STATE_ERROR
            return
        with self._lock:
            self.metainfo = meta
            self._info_blob = blob
            self._md_pieces.clear()
            self._on_metainfo_ready()

    def wants_piece(self, index: int) -> bool:
        with self._lock:
            return index in self.wanted and index not in self.have

    def wants_piece_anywhere(self, peer: PeerConnection) -> bool:
        with self._lock:
            for index in self._priority:
                if index in self.have:
                    continue
                if peer.has_piece(index):
                    return True
            return False

    def pick_pieces(self, peer: PeerConnection, depth: int) -> list[int]:
        """Choose up to ``depth`` pieces for a peer.

        Сессия — единственный владелец учёта in-flight. Кусок отдаётся ОДНОМУ
        пиру, пока блоки по нему идут; второй подключается только когда кусок
        «завис» дольше ``PIECE_STALL_AFTER`` — раньше оба пира качали один
        кусок целиком и половина трафика выбрасывалась как дубликаты.
        """
        if depth <= 0:
            return []
        picks: list[int] = []
        now = time.monotonic()
        with self._lock:
            for index in self._priority:
                if len(picks) >= depth:
                    break
                if index in self.have or index in self._finalizing:
                    continue
                inflight = self._inflight.get(index, 0)
                if inflight:
                    stalled = now - self._piece_progress.get(index, 0.0) >= PIECE_STALL_AFTER
                    if inflight >= (2 if stalled else 1):
                        continue
                if not peer.has_piece(index):
                    continue
                self._inflight[index] = inflight + 1
                if index not in self._piece_progress:
                    self._piece_progress[index] = now
                peer.requested_pieces.add(index)
                picks.append(index)
        return picks

    def block_plan(self, index: int, budget: int) -> list[tuple[int, int]]:
        """Недостающие блоки куска: (begin, length), не больше ``budget`` штук.

        Перехваченный у зависшего пира кусок дозапрашивается только хвостами —
        уже принятые блоки запрашивать заново не нужно.
        """
        meta = self.metainfo
        if meta is None:
            return []
        size = meta.piece_size(index)
        plan: list[tuple[int, int]] = []
        with self._lock:
            entry = self._buffers.get(index)
            done = set(entry[1]) if entry is not None else set()
        begin = 0
        while begin < size and len(plan) < budget:
            block_len = min(BLOCK_SIZE, size - begin)
            if begin not in done:
                plan.append((begin, block_len))
            begin += BLOCK_SIZE
        return plan

    def release_piece(self, index: int) -> None:
        with self._lock:
            self._release_locked(index)

    def _release_locked(self, index: int) -> None:
        count = self._inflight.get(index, 0) - 1
        if count <= 0:
            self._inflight.pop(index, None)
        else:
            self._inflight[index] = count

    def release_peer_pieces(self, peer: PeerConnection) -> None:
        for index in list(peer.requested_pieces):
            self.release_piece(index)
        peer.requested_pieces.clear()

    def on_block(self, peer: PeerConnection, index: int, begin: int, block: bytes) -> None:
        """Принять блок. Хеширование и запись куска — БЕЗ session-лока:
        иначе все peer-потоки сериализуются на SHA1+writeframing одного куска
        и суммарная скорость упирается в самый медленный диск."""
        finalize: tuple[int, bytes] | None = None
        with self._lock:
            if index in self.have or self.metainfo is None or index in self._finalizing:
                return
            size = self.metainfo.piece_size(index)
            if begin + len(block) > size:
                return
            entry = self._buffers.get(index)
            if entry is None:
                entry = [bytearray(size), set(), 0]
                self._buffers[index] = entry
            buffer, blocks, received = entry
            if begin in blocks:
                return
            buffer[begin:begin + len(block)] = block
            blocks.add(begin)
            entry[2] = received + len(block)
            self._piece_progress[index] = time.monotonic()
            self.total_received += len(block)
            if entry[2] >= size:
                del self._buffers[index]
                self._finalizing.add(index)
                finalize = (index, bytes(buffer))
        if finalize is not None:
            self._finalize_piece(peer, *finalize)

    def _finalize_piece(self, peer: PeerConnection, index: int, data: bytes) -> None:
        """SHA1 + запись куска вне лока, публикация результата под локом."""
        ok = False
        try:
            ok = hashlib.sha1(data).digest() == self.metainfo.piece_hashes[index]
            if ok:
                self._write_piece(index, data)
        except OSError as exc:
            with self._lock:
                self._finalizing.discard(index)
                self.error = f"disk write failed: {exc}"
                self.state = STATE_ERROR
            self._stop.set()
            return
        finish = False
        with self._lock:
            self._finalizing.discard(index)
            self._piece_progress.pop(index, None)
            if ok:
                self.have.add(index)
                self.wanted.discard(index)
                self._inflight.pop(index, None)
                for other in list(self.peers.values()):
                    other.requested_pieces.discard(index)
                    # пришедшие «вдогонку» блоки куска — просто дроп; outstanding
                    # чистим, чтобы пайплайн пира не висел на мёртвых запросах
                    for key in [k for k in other.outstanding if k[0] == index]:
                        other.outstanding.pop(key, None)
                if not self.wanted:
                    finish = True
            else:
                # испорченный кусок: учёт обнуляем, п сами пересыпят запросы
                self._inflight.pop(index, None)
                for other in list(self.peers.values()):
                    other.requested_pieces.discard(index)
        if finish:
            self._finish()

    def read_block(self, index: int, begin: int, length: int) -> bytes | None:
        with self._lock:
            if index not in self.have or self.metainfo is None:
                return None
            offset = index * self.metainfo.piece_length + begin
        try:
            return self._read_abs(offset, length)
        except OSError:
            return None

    def on_pex_peers(self, peers: list[PeerAddress]) -> None:
        self._add_candidates(peers)

    def pex_candidates(self, peer: PeerConnection) -> list[PeerAddress]:
        out: list[PeerAddress] = []
        with self._lock:
            for key, other in self.peers.items():
                if other is peer:
                    continue
                out.append(other.address)
                if len(out) >= 40:
                    break
        return out

    # -------------------------------------------------------------------- disk
    def _handle_for(self, index: int):
        handle = self._files.get(index)
        if handle is not None:
            return handle
        meta = self.metainfo
        if meta is None:
            raise OSError("no metadata")
        f = meta.file_by_index(index)
        path = self.path_for_file(f)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "r+b")
        self._files[index] = handle
        return handle

    def _write_piece(self, index: int, data: bytes) -> None:
        meta = self.metainfo
        assert meta is not None
        self._write_abs(index * meta.piece_length, data)

    def _write_abs(self, offset: int, data: bytes) -> None:
        meta = self.metainfo
        assert meta is not None
        end = offset + len(data)
        for f in meta.files:
            if f.length == 0:
                continue
            f_start, f_end = f.offset, f.offset + f.length
            if f_end <= offset or f_start >= end:
                continue
            chunk_start = max(offset, f_start)
            chunk_end = min(end, f_end)
            handle = self._handle_for(f.index)
            handle.seek(chunk_start - f_start)
            handle.write(data[chunk_start - offset:chunk_end - offset])

    def _read_abs(self, offset: int, length: int) -> bytes:
        meta = self.metainfo
        assert meta is not None
        out = bytearray()
        end = offset + length
        for f in meta.files:
            if f.length == 0:
                continue
            f_start, f_end = f.offset, f.offset + f.length
            if f_end <= offset or f_start >= end or len(out) >= length:
                continue
            chunk_start = max(offset, f_start)
            chunk_end = min(end, f_end)
            handle = self._handle_for(f.index)
            handle.seek(chunk_start - f_start)
            out += handle.read(chunk_end - chunk_start)
        return bytes(out[:length])

    def close_files(self) -> None:
        with self._lock:
            for handle in self._files.values():
                try:
                    handle.close()
                except OSError:
                    pass
            self._files.clear()

    # ------------------------------------------------------------------- peers
    def _add_candidates(self, peers: Iterable[PeerAddress]) -> None:
        with self._lock:
            for peer in peers:
                key = peer.key
                if key in self._seen or key in self.peers:
                    continue
                self._seen.add(key)
                self._candidates.append(peer)
                if len(self._candidates) > 500:
                    self._candidates.popleft()
                    self._seen.discard(key)

    def _spawn_peers(self) -> None:
        with self._lock:
            limit = max(4, int(self.config.max_peers))
            wanted = limit - len(self.peers)
            skipped: list[PeerAddress] = []
            now = time.monotonic()
            while wanted > 0 and self._candidates:
                peer = self._candidates.popleft()
                key = peer.key
                if key in self.peers:
                    continue
                last = self._attempts.get(key, 0.0)
                if last and now - last < 120:
                    if len(skipped) < 200:
                        skipped.append(peer)
                    continue
                self._attempts[key] = now
                connection = PeerConnection(self, peer, self.proxy)
                self.peers[key] = connection
                connection.start()
                wanted -= 1
            for peer in skipped:
                self._candidates.append(peer)

    # ------------------------------------------------------------------- manager
    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._tick()
                self._stop.wait(1.0)
        finally:
            self.close_files()
            if self.state not in (STATE_COMPLETE, STATE_ERROR, STATE_STOPPED):
                self.state = STATE_STOPPED

    def _tick(self) -> None:
        now = time.monotonic()
        self._samples.append((now, self.total_received))
        while self._samples and now - self._samples[0][0] > SPEED_WINDOW:
            self._samples.popleft()
        if len(self._samples) >= 2:
            dt = self._samples[-1][0] - self._samples[0][0]
            db = self._samples[-1][1] - self._samples[0][1]
            self.speed = db / dt if dt > 0 else 0.0
        self._reap_peers(now)
        with self._lock:
            self.connected_peak = max(self.connected_peak, len(self.peers))
        self._spawn_peers()
        if now >= self._next_announce and not self._announce_busy.is_set():
            self._next_announce = now + self._announce_interval
            self._announce_thread = threading.Thread(target=self._do_announce, daemon=True)
            self._announce_thread.start()
        if self.state == STATE_METADATA and self.metainfo is None:
            with self._lock:
                peers_meta = [p for p in self.peers.values() if p.their_ut_metadata]
            for peer in peers_meta:
                self._pull_metadata(peer, count=4)
            if time.time() - self.started_at > 180 and not self._md_pieces:
                with self._lock:
                    peers = len(self.peers)
                if peers == 0:
                    self.error = "no peers reachable through proxy for metadata"
        if self.metainfo is not None and not self.wanted and self.state == STATE_DOWNLOADING:
            self._finish()

    def _reap_peers(self, now: float) -> None:
        """Free peer slots held by dead or unhelpful connections.

        A public proxy relays plenty of black holes: sockets that connect but never
        unchoke. Without this the pool fills up with them and the torrent stalls.
        """
        doomed: list[PeerConnection] = []
        with self._lock:
            peers = list(self.peers.values())
        for peer in peers:
            if not peer.connected:
                if now - self._attempts.get(peer.address.key, now) > 25:
                    doomed.append(peer)
                continue
            silent = now - peer.last_activity
            age = now - peer.connected_at
            if silent > 60:
                doomed.append(peer)
            elif age > 25 and not peer.bitfield and not peer.metadata_size:
                # connected but never said anything useful: a black hole behind the proxy
                doomed.append(peer)
            elif age > 45 and peer.am_interested and peer.peer_choking and not peer.outstanding:
                doomed.append(peer)
        for peer in doomed:
            peer.stop()

    def _do_announce(self) -> None:
        self._announce_busy.set()
        try:
            left = self.remaining_pieces_bytes()
            found: list[PeerAddress] = []

            def on_peers(new_peers: list[PeerAddress]) -> None:
                found.extend(new_peers)
                self._add_candidates(new_peers)

            peers, errors, interval = announce_parallel(
                self.trackers,
                self.info_hash,
                self.peer_id,
                self.listen_port,
                left,
                self.proxy,
                event="started" if not self._seen else "",
                timeout=9.0,
                budget=14.0,
                on_peers=on_peers,
                should_stop=self._stop.is_set,
            )
            with self._lock:
                self.trackers_errors = errors[:6]
                self._announce_interval = max(45, min(interval, 1800))
                if not found:
                    self._next_announce = min(self._next_announce, time.monotonic() + 12)
            if found:
                self.seeds = max(self.seeds, len(found))
        finally:
            self._announce_busy.clear()

    def _finish(self) -> None:
        with self._lock:
            if self.state == STATE_COMPLETE:
                return
            self.state = STATE_COMPLETE
            self.finished_at = time.time()
        self.close_files()
        for peer in list(self.peers.values()):
            peer.stop()
        if self.on_complete is not None:
            try:
                self.on_complete(self)
            except Exception:
                pass

    # -------------------------------------------------------------------- stats
    def remaining_pieces_bytes(self) -> int:
        meta = self.metainfo
        if meta is None:
            return 1
        with self._lock:
            return sum(meta.piece_size(p) for p in self.wanted if p not in self.have)

    def wanted_bytes(self) -> int:
        meta = self.metainfo
        if meta is None:
            return 0
        return meta.bytes_for_pieces(meta.pieces_for_files(self.selected_files))

    def downloaded_bytes(self) -> int:
        meta = self.metainfo
        if meta is None:
            return 0
        with self._lock:
            return sum(meta.piece_size(p) for p in self.have if p in meta.pieces_for_files(self.selected_files))

    def file_progress(self, index: int) -> float:
        meta = self.metainfo
        if meta is None:
            return 0.0
        f = meta.file_by_index(index)
        if f is None or f.length == 0:
            return 1.0
        with self._lock:
            total = f.last_piece - f.first_piece + 1
            have = sum(1 for p in range(f.first_piece, f.last_piece + 1) if p in self.have)
        return have / total if total else 0.0

    def file_ready(self, index: int) -> bool:
        meta = self.metainfo
        if meta is None:
            return False
        f = meta.file_by_index(index)
        if f is None:
            return False
        with self._lock:
            return all(p in self.have for p in range(f.first_piece, f.last_piece + 1))

    def set_focus(self, file_index: int) -> None:
        meta = self.metainfo
        if meta is None:
            return
        f = meta.file_by_index(file_index)
        if f is None:
            return
        with self._lock:
            self.focus_file = file_index
            focused = list(range(f.first_piece, f.last_piece + 1))
            focus_set = {p for p in focused if p in self.wanted}
            rest = [p for p in sorted(self.wanted) if p not in focus_set]
            self._priority = sorted(focus_set) + rest

    def set_selection(self, file_indexes: Iterable[int]) -> None:
        meta = self.metainfo
        if meta is None:
            return
        selection = {i for i in file_indexes if meta.file_by_index(i) is not None}
        if not selection:
            return
        with self._lock:
            self.selected_files = selection
            self.wanted = meta.pieces_for_files(selection) - self.have
            self._priority = sorted(self.wanted)
        # hand back the pieces we no longer track, keeping the counters exact
        for peer in list(self.peers.values()):
            for index in list(peer.requested_pieces):
                self.release_piece(index)
            peer.requested_pieces.clear()
            peer.outstanding.clear()

    def snapshot(self) -> SessionSnapshot:
        meta = self.metainfo
        total = self.wanted_bytes() if meta else 0
        done = self.downloaded_bytes() if meta else 0
        progress = (done / total) if total else (1.0 if self.state == STATE_COMPLETE else 0.0)
        if self.state == STATE_COMPLETE:
            progress = 1.0
        eta = (total - done) / self.speed if self.speed > 1024 and total > done else 0.0
        with self._lock:
            peers = len(self.peers)
            pieces_have = len(self.have)
            selected = len(self.selected_files)
        return SessionSnapshot(
            info_hash=self.hex_hash,
            name=self.display_name(),
            state=self.state,
            progress=progress,
            downloaded=done,
            total=total,
            speed=self.speed,
            peers=peers,
            seeds=self.seeds,
            leechers=self.leechers,
            eta=eta,
            error=self.error,
            pieces_have=pieces_have,
            pieces_total=len(self.wanted) + pieces_have if meta else 0,
            files=len(meta.files) if meta else 0,
            selected=selected,
            focus_file=self.focus_file,
            trackers_errors=list(self.trackers_errors),
        )


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
