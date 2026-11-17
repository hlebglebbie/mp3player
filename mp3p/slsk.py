"""Клиент SoulSeek поверх того же прокси-слоя, что и BitTorrent-часть.

Протокол изучен по исходникам nicotine+ (pynicotine/slskmessages.py):

* серверные и peer-сообщения: ``[u32 LE len][u32 LE code][payload]``, len
  считает байты ПОСЛЕ поля длины (code + payload);
* init-сообщения peer-соединения (PeerInit/PierceFirewall): len считает
  1 байт кода + payload, код — ОДИН байт;
* на 'F'-соединении после init: 4 сырых байта token (FileTransferInit от
  отдающего), 8 сырых байт offset (FileOffset от качающего), затем сырой
  поток байтов файла;
* клиент без слушающего порта жизнеспособен:сет даёт нам коду
  ConnectToPeer(18), на который мы отвечаем исходящим дозвоном и
  PierceFirewall(token); так приходят и результаты поиска (тип 'P'), и
  старты закачек (тип 'F').

Логин: server.slsknet.org:2242, md5(user+pass), major 177 (зарезервировано
для экспериментальных клиентов), minor 1. Участвующие в сети считают
отдачу «по-честному», поэтому клиент заявляет 0 шары — очереди у части
пиров от этого длиннее, но скачивание работает.
"""

from __future__ import annotations

import hashlib
import os
import random
import socket
import struct
import threading
import time
import zlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import MUSIC_EXTS, Config, human_size
from .net import Proxy

SERVER_HOST = "server.slsknet.org"
SERVER_PORT = 2242
MAJOR_VERSION = 177
MINOR_VERSION = 1

CONNECT_TIMEOUT = 15.0
IO_TIMEOUT = 75.0
PEER_ADDR_TTL = 300.0
GRANT_WAIT_FIRST = 90.0
SEARCH_WINDOW = 12.0
MAX_DECOMPRESSED = 16 * 1024 * 1024
STALL_TIMEOUT = 60.0

# серверные коды
MSG_LOGIN = 1
MSG_SET_WAIT_PORT = 2
MSG_GET_PEER_ADDRESS = 3
MSG_WATCH_USER = 5
MSG_GET_USER_STATUS = 7
MSG_CONNECT_TO_PEER = 18
MSG_FILE_SEARCH = 26
MSG_SET_STATUS = 28
MSG_SERVER_PING = 32
MSG_SHARED_FOLDERS = 35

# peer-коды (на 'P'-соединениях)
PMSG_FILE_SEARCH_RESPONSE = 9
PMSG_TRANSFER_REQUEST = 40
PMSG_TRANSFER_RESPONSE = 41
PMSG_QUEUE_UPLOAD = 43
PMSG_PLACE_IN_QUEUE = 44
PMSG_UPLOAD_FAILED = 46
PMSG_UPLOAD_DENIED = 50

# init-коды (1 байт)
INIT_PIERCE = 0
INIT_PEER = 1

TASK_LEGACY = "File not shared."


class SlskError(OSError):
    pass


# --------------------------------------------------------------------- buffer


class _Buf:
    """Курсор по payload сообщения (u32/u64/str/bool/ip — как в nicotine+).
    Обрезанный payload даёт SlskError, а не struct.error: парсер не должен
    валить поток-обработчик."""

    __slots__ = ("data", "off")

    def __init__(self, data: bytes):
        self.data = data
        self.off = 0

    def _need(self, count: int) -> None:
        if self.off + count > len(self.data):
            raise SlskError("обрезанный payload")

    def left(self) -> int:
        return len(self.data) - self.off

    def u8(self) -> int:
        self._need(1)
        v = self.data[self.off]
        self.off += 1
        return v

    def u16(self) -> int:
        self._need(2)
        v = struct.unpack_from("<H", self.data, self.off)[0]
        self.off += 2
        return v

    def u32(self) -> int:
        self._need(4)
        v = struct.unpack_from("<I", self.data, self.off)[0]
        self.off += 4
        return v

    def u64(self) -> int:
        self._need(8)
        v = struct.unpack_from("<Q", self.data, self.off)[0]
        self.off += 8
        return v

    def boolean(self) -> bool:
        return bool(self.u8())

    def string(self) -> str:
        length = self.u32()
        self._need(length)
        raw = self.data[self.off:self.off + length]
        self.off += length
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")

    def ip(self) -> str:
        self._need(4)
        raw = self.data[self.off:self.off + 4]
        self.off += 4
        return socket.inet_ntoa(raw[::-1])

    def file_size(self) -> int:
        # хвост 0xFF — баг Soulseek NS с файлами >2 GiB: старшие 4 байта мусор
        self._need(8)
        if self.data[self.off + 7] == 0xFF:
            size = self.u32()
            self.off += 4
            return size
        return self.u64()


def _u16(v: int) -> bytes:
    return struct.pack("<H", v)


def _u32(v: int) -> bytes:
    return struct.pack("<I", v)


def _u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _b(v: bool) -> bytes:
    return b"\x01" if v else b"\x00"


def _s(text: str, legacy: bool = False) -> bytes:
    if legacy:
        try:
            raw = text.encode("latin-1")
        except UnicodeEncodeError:
            raw = text.encode("utf-8", "replace")
    else:
        raw = text.encode("utf-8", "replace")
    return _u32(len(raw)) + raw


def _server_frame(code: int, payload: bytes) -> bytes:
    return _u32(4 + len(payload)) + _u32(code) + payload


def _peer_frame(code: int, payload: bytes) -> bytes:
    return _u32(4 + len(payload)) + _u32(code) + payload


def _init_frame(code: int, payload: bytes) -> bytes:
    return _u32(1 + len(payload)) + bytes([code]) + payload


def _recv_exact(sock: socket.socket, count: int, timeout: float = IO_TIMEOUT) -> bytes:
    sock.settimeout(timeout)
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(min(65536, count - len(chunks)))
        if not chunk:
            raise SlskError("соединение закрыто")
        chunks += chunk
    return bytes(chunks)


def _recv_some(sock: socket.socket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    return sock.recv(65536)


def _safe_component(text: str, maxlen: int = 120) -> str:
    cleaned = "".join(c if c not in '<>:"/\\|?*\x00-\x1f' and c.isprintable() else "_" for c in text)
    cleaned = " ".join(cleaned.split()) or "_"
    return cleaned.strip(" .")[:maxlen] or "_"


# -------------------------------------------------------------------- results


@dataclass
class SlskFileResult:
    """Строка поиска SoulSeek; поля в духе search.SearchResult для UI."""

    username: str
    path: str
    size: int
    bitrate: int = 0
    duration: int = 0
    slot_free: bool = False
    queue_len: int = 0
    speed: int = 0
    source: str = "slsk"

    @property
    def basename(self) -> str:
        return self.path.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def album_hint(self) -> str:
        parts = [p for p in self.path.replace("\\", "/").split("/") if p and not p.startswith("@@")]
        return parts[-2] if len(parts) >= 2 else ""

    @property
    def name(self) -> str:
        extra = []
        if self.bitrate:
            extra.append(f"{self.bitrate}k")
        if self.duration:
            minutes, seconds = divmod(self.duration, 60)
            extra.append(f"{minutes}:{seconds:02d}")
        extra.append(self.username)
        tag = f" [{', '.join(extra)}]" if extra else ""
        return f"{self.basename}{tag}"

    @property
    def seeders(self) -> int:
        return 1 if self.slot_free else 0

    @property
    def info_hash(self) -> str:
        # уникальный ключ строки для UI (meta["hash"])
        return "SLSK:" + hashlib.sha1(f"{self.username}|{self.path}".encode()).hexdigest()[:16].upper()

    @property
    def size_text(self) -> str:
        return human_size(self.size)

    @property
    def category_text(self) -> str:
        return "slsk"

    @property
    def age_text(self) -> str:
        return "-"


def _results_from_search_response(payload: bytes) -> tuple[str, int, list[SlskFileResult], bool, int, int] | None:
    """Разобрать FileSearchResponse (код 9, payload сжат zlib).

    Возвращает (username, token, files, slot_free, speed, queue) либо None,
    если сообщение не разбирается.
    """
    decompressor = zlib.decompressobj()
    try:
        data = decompressor.decompress(payload, MAX_DECOMPRESSED)
        if decompressor.unconsumed_tail:
            raise SlskError("слишком большие результаты поиска")
    except zlib.error as exc:
        raise SlskError(f"битый zlib в результатах: {exc}") from exc
    buf = _Buf(data)
    username = buf.string()
    token = buf.u32()
    files: list[SlskFileResult] = []

    for _ in range(buf.u32()):
        try:
            buf.u8()  # code (1 = файл)
            path = buf.string()
            size = buf.file_size()
            ext_len = buf.u32()
            buf.off += ext_len
            bitrate = duration = 0
            for _ in range(buf.u32()):
                attr, value = buf.u32(), buf.u32()
                if attr == 0:
                    bitrate = value
                elif attr == 1:
                    duration = value
        except SlskError:
            break  # обрезанное сообщение — берём что распарсилось
        ext = os.path.splitext(path.replace("\\", "/"))[1].lower()
        if ext in MUSIC_EXTS and size > 0:
            files.append(SlskFileResult(username, path, size, bitrate, duration))
    try:
        slot_free = buf.boolean()
        speed = buf.u32()
        queue_len = buf.u32()
    except SlskError:
        slot_free, speed, queue_len = False, 0, 0
    for result in files:
        result.slot_free = slot_free
        result.speed = speed
        result.queue_len = queue_len
    return username, token, files, slot_free, speed, queue_len


# ------------------------------------------------------------------- transfer


@dataclass
class SlskTransfer:
    """Одна закачка с SoulSeek; API в духе TorrentSession для UI."""

    client: "SlskClient"
    result: SlskFileResult
    state: str = "metadata"  # те же строки, что в downloader: metadata=queued
    error: str = ""
    downloaded: int = 0
    total: int = 0
    queue_place: int = 0
    grant_token: int = 0
    on_complete: object = None
    speed: float = 0.0
    started_at: float = field(default_factory=time.time)
    legacy: bool = False

    def __post_init__(self):
        self.total = self.result.size or 0
        self._hash = "SLSK" + hashlib.sha1(
            f"{self.result.username}|{self.result.path}".encode()
        ).hexdigest()[:16].upper()
        base = _safe_component(self.result.basename)
        self.target_dir = Path(self.client.cfg.download_dir) / "slsk" / _safe_component(self.result.username, 60)
        self.incomplete_path = self.target_dir / (base + ".part")
        self._samples: deque = deque()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._outcome: str | None = None
        self._outcome_event = threading.Event()

    # -- совместимость с интерфейсом сессии, который ждёт UI ----------------
    @property
    def hex_hash(self) -> str:
        return self._hash

    @property
    def display_name(self):
        return self.result.basename

    @property
    def file_path(self) -> Path:
        return self.target_dir / _safe_component(self.result.basename)

    def snapshot(self):
        from .downloader import SessionSnapshot

        with self._lock:
            place = self.queue_place
            downloaded = self.downloaded
            speed = self.speed
        progress = (downloaded / self.total) if self.total else (1.0 if self.state == "complete" else 0.0)
        eta = (self.total - downloaded) / speed if speed > 1024 and self.total > downloaded else 0.0
        return SessionSnapshot(
            info_hash=self.hex_hash,
            name=self.result.basename,
            state=self.state,
            progress=progress,
            downloaded=downloaded,
            total=self.total,
            speed=speed,
            peers=1,
            seeds=1 if self.result.slot_free else 0,
            leechers=0,
            eta=eta,
            error=self.error,
            pieces_have=0,
            pieces_total=0,
            files=1,
            selected=1,
            focus_file=-1,
        )

    # -- управление -----------------------------------------------------------
    def stop(self) -> None:
        self._stopped.set()

    def update_progress(self, nbytes: int) -> None:
        now = time.monotonic()
        with self._lock:
            self.downloaded += nbytes
            self._samples.append((now, self.downloaded))
            while self._samples and now - self._samples[0][0] > 6.0:
                self._samples.popleft()
            if len(self._samples) >= 2:
                dt = self._samples[-1][0] - self._samples[0][0]
                db = self._samples[-1][1] - self._samples[0][1]
                if dt > 0:
                    self.speed = db / dt

    def is_done(self) -> bool:
        return self.state in ("complete", "error", "stopped")

    # -- события от peer-соединений -------------------------------------------
    def on_grant(self, token: int, size: int) -> None:
        """Пир готов отдавать файл (TransferRequest direction=1)."""
        if self.grant_token:
            return
        self.grant_token = token
        if size and self.total and size != self.total:
            # размер изменился — начнём заново, старый .part не валиден
            try:
                self.incomplete_path.unlink()
            except OSError:
                pass
            self.total = size
        with self._lock:
            if self._outcome is None:
                self._outcome = "grant"
            self._outcome_event.set()

    def on_queue_place(self, place: int) -> None:
        with self._lock:
            self.queue_place = place

    def on_denied(self, reason: str) -> None:
        with self._lock:
            self.error = reason
            if self._outcome is None or self._outcome == "grant":
                self._outcome = "denied"
            self._outcome_event.set()

    def on_upload_failed(self) -> None:
        """UploadFailed(46): у пира что-то сломалось — пробуем ещё раз."""
        with self._lock:
            self.error = ""
            if self._outcome != "grant":
                self._outcome = "upload_failed"
            self._outcome_event.set()

    def wait_outcome(self, timeout: float) -> str:
        """'grant' | 'denied' | 'upload_failed' | 'stopped' | 'timeout'."""
        self._outcome_event.wait(timeout)
        if self._stopped.is_set():
            return "stopped"
        return self._outcome or "timeout"

    def mark(self, state: str, error: str = "") -> None:
        self.state = state
        if error:
            self.error = error


# ---------------------------------------------------------------- peer links


class _PeerLink(threading.Thread):
    """Исходящее 'P'-соединение к пользователю: QueueUpload и договорённости."""

    def __init__(self, client: "SlskClient", username: str, host: str, port: int, on_death):
        super().__init__(daemon=True, name=f"slsk-p-{username}")
        self.client = client
        self.username = username
        self.host = host
        self.port = port
        self.on_death = on_death
        self.sock: socket.socket | None = None
        self.dead = threading.Event()
        self.transfers: dict[str, SlskTransfer] = {}
        self._send_lock = threading.Lock()

    # -- отправка -------------------------------------------------------------
    def send_peer(self, code: int, payload: bytes) -> None:
        if self.sock is None:
            raise SlskError("сокет не открыт")
        with self._send_lock:
            self.sock.sendall(_peer_frame(code, payload))

    def queue_upload(self, transfer: SlskTransfer, legacy: bool) -> None:
        self.transfers[transfer.result.path] = transfer
        self.send_peer(PMSG_QUEUE_UPLOAD, _s(transfer.result.path, legacy=legacy))

    def transfer_response(self, token: int, allowed: bool, reason: str = "") -> None:
        payload = _u32(token) + _b(allowed)
        if not allowed:
            payload += _s(reason)
        self.send_peer(PMSG_TRANSFER_RESPONSE, payload)

    # -- поток ------------------------------------------------------------------
    def run(self) -> None:
        sock = None
        try:
            sock = self.client.open_tcp(self.host, self.port)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = sock
            sock.sendall(_init_frame(INIT_PEER, _s(self.client.username) + _s("P") + _u32(0)))
            self.client.log_link(self.username, "P-соединение открыто")
            self._read_loop(sock)
        except (SlskError, OSError) as exc:
            if not self.dead.is_set():
                self.client.log_link(self.username, f"P закрыто: {exc}")
        finally:
            self.dead.set()
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            try:
                self.on_death(self)
            except Exception:
                pass

    def _read_loop(self, sock: socket.socket) -> None:
        buffer = bytearray()
        while True:
            while len(buffer) >= 8:
                length, code = struct.unpack_from("<II", buffer, 0)
                if length < 4 or length > MAX_DECOMPRESSED:
                    raise SlskError(f"битый len в peer-сообщении: {length}")
                if len(buffer) < length + 4:
                    break
                payload = bytes(buffer[8:length + 4])
                del buffer[:length + 4]
                self._dispatch(code, payload)
            chunk = _recv_some(sock, IO_TIMEOUT)
            if not chunk:
                raise SlskError("соединение закрыто пиром")
            buffer += chunk

    def _dispatch(self, code: int, payload: bytes) -> None:
        if code == PMSG_TRANSFER_REQUEST:
            self._on_transfer_request(payload)
        elif code == PMSG_PLACE_IN_QUEUE:
            buf = _Buf(payload)
            path = buf.string()
            place = buf.u32()
            transfer = self.transfers.get(path)
            if transfer is not None:
                transfer.on_queue_place(place)
        elif code == PMSG_UPLOAD_DENIED:
            buf = _Buf(payload)
            path = buf.string()
            reason = buf.string()
            transfer = self.transfers.get(path)
            if transfer is not None:
                transfer.on_denied(reason)
        elif code == PMSG_UPLOAD_FAILED:
            # пир не смог читать файл или соединение — переочередим попытку
            buf = _Buf(payload)
            path = buf.string()
            transfer = self.transfers.get(path)
            if transfer is not None:
                transfer.on_upload_failed()
        # остальные коды (5, 9, 37… ) просто пропускаем

    def _on_transfer_request(self, payload: bytes) -> None:
        buf = _Buf(payload)
        direction = buf.u32()
        if direction != 1:
            return  # запрос НА нас качать — не поддерживаем
        token = buf.u32()
        path = buf.string()
        size = 0
        if buf.left() >= 8:
            size = buf.u64()
        transfer = self.transfers.get(path)
        if transfer is None:
            for candidate in self.transfers.values():
                if candidate.result.basename.lower() in path.lower():
                    transfer = candidate
                    break
        if transfer is None:
            self.transfer_response(token, False, "Cancelled")
            return
        transfer.mark("downloading")
        self.transfer_response(token, True)
        transfer.on_grant(token, size)


# ------------------------------------------------------------------- client


class SlskClient:
    """Серверное соединение + поиск + менеджер загрузок SoulSeek."""

    def __init__(self, cfg: Config, log=None):
        self.cfg = cfg
        self.username = cfg.slsk_user
        self.password = cfg.slsk_pass
        self._log = log
        self.proxy: Proxy | None = None
        proxy_url = cfg.effective_proxy()
        if proxy_url:
            try:
                self.proxy = Proxy.parse(proxy_url)
            except Exception:
                self.proxy = None

        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._logged_in = threading.Event()
        self._login_error = ""
        self._server_thread: threading.Thread | None = None
        self._search_token = random.randrange(1, 2 ** 31)
        self._search_lock = threading.Lock()

        self._searches: dict[int, dict] = {}
        self._addr_lock = threading.Lock()
        self._addr_waiters: dict[str, list] = {}

        self._links: dict[str, _PeerLink] = {}
        self._links_lock = threading.Lock()
        self._file_waiters: dict[str, dict] = {}
        self._file_waiters_lock = threading.Lock()

        self.transfers: list[SlskTransfer] = []
        self._transfers_lock = threading.Lock()

    # ----------------------------------------------------------- жизненный цикл
    def start(self) -> None:
        if self._server_thread and self._server_thread.is_alive():
            return
        self._stop.clear()
        self._server_thread = threading.Thread(target=self._run, name="slsk-server", daemon=True)
        self._server_thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with self._links_lock:
            links = list(self._links.values())
        for link in links:
            link.dead.set()
            if link.sock is not None:
                try:
                    link.sock.close()
                except OSError:
                    pass

    def log(self, message: str) -> None:
        if self._log is not None:
            try:
                self._log(message)
            except Exception:
                pass

    def log_link(self, username: str, message: str) -> None:
        self.log(f"slsk {username}: {message}")

    # ----------------------------------------------------------------- сокеты
    def open_tcp(self, host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> socket.socket:
        if self.proxy is not None:
            return self.proxy.open_tcp(host, port, timeout=timeout)
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(IO_TIMEOUT)
        return sock

    def _send_server(self, code: int, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise SlskError("нет соединения с сервером")
        with self._send_lock:
            sock.sendall(_server_frame(code, payload))

    def wait_login(self, timeout: float = 20.0) -> str:
        """'' когда готово, иначе текст ошибки."""
        self._logged_in.wait(timeout)
        if self._logged_in.is_set():
            return ""
        return self._login_error or "сервер soulseek не ответил"

    # --------------------------------------------------------- серверный поток
    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            sock = None
            try:
                self.log(f"slsk: подключаюсь к {SERVER_HOST}:{SERVER_PORT}"
                         + (f" через {self.proxy.url()}" if self.proxy else " напрямую"))
                sock = self.open_tcp(SERVER_HOST, SERVER_PORT, timeout=20.0)
                self._sock = sock
                self._login(sock)
                backoff = 5.0
                self._server_read_loop(sock)
            except (SlskError, OSError) as exc:
                if self._stop.is_set():
                    break
                self.log(f"slsk: соединение потеряно ({exc}), через {backoff:.0f} с пробую снова")
            finally:
                self._logged_in.clear()
                self._sock = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if self._stop.wait(backoff):
                break
            backoff = min(60.0, backoff * 1.6)
        self.log("slsk: остановлено")

    def _login(self, sock: socket.socket) -> None:
        checksum = hashlib.md5((self.username + self.password).encode()).hexdigest()
        payload = (
            _s(self.username) + _s(self.password) + _u32(MAJOR_VERSION)
            + _s(checksum) + _u32(MINOR_VERSION)
        )
        sock.settimeout(30.0)
        sock.sendall(_server_frame(MSG_LOGIN, payload))
        # ответ на Login по протоколу приходит первым сообщением
        length, code = struct.unpack("<II", _recv_exact(sock, 8, timeout=30.0))
        if code != MSG_LOGIN:
            raise SlskError(f"ожидался ответ на login, пришёл код {code}")
        body = _recv_exact(sock, length - 4, timeout=30.0)
        buf = _Buf(body)
        if not buf.boolean():
            reason = buf.string() if buf.left() else "отказ"
            detail = ""
            if buf.left():
                try:
                    detail = buf.string()
                except Exception:
                    detail = ""
            self._login_error = f"логин отклонён: {reason}" + (f" ({detail})" if detail else "")
            self._stop.set()
            raise SlskError(self._login_error)
        # motd/ip/checksum/supporter из ответа можно не разбирать
        # слушающего порта у нас нет — честно сообщаем серверу 0,
        # тогда пиры сразу идут через ConnectToPeer, без тупиковых дозвонов
        with self._send_lock:
            sock.sendall(_server_frame(MSG_SET_WAIT_PORT, _u32(0)))
            sock.sendall(_server_frame(MSG_SET_STATUS, struct.pack("<i", 2)))
            sock.sendall(_server_frame(MSG_SHARED_FOLDERS, _u32(0) + _u32(0)))
        self._login_error = ""
        self._logged_in.set()
        self.log(f"slsk: вошли как {self.username}")
        self._reset_links()

    def _server_read_loop(self, sock: socket.socket) -> None:
        buffer = bytearray()
        last_ping = time.monotonic()
        while not self._stop.is_set():
            while len(buffer) >= 8:
                length, code = struct.unpack_from("<II", buffer, 0)
                if length < 4 or length > MAX_DECOMPRESSED:
                    raise SlskError(f"битый len от сервера: {length}")
                if len(buffer) < length + 4:
                    break
                payload = bytes(buffer[8:length + 4])
                del buffer[:length + 4]
                last_ping = time.monotonic()
                try:
                    self._server_dispatch(code, payload)
                except Exception as exc:  # noqa: BLE001 - одно кривое сообщение не роняет соединение
                    self.log(f"slsk: не разобрал сообщение {code}: {exc}")
            if time.monotonic() - last_ping > 240:
                with self._send_lock:
                    sock.sendall(_server_frame(MSG_SERVER_PING, b""))
                last_ping = time.monotonic()
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise SlskError("сервер закрыл соединение")
            buffer += chunk

    def _server_dispatch(self, code: int, payload: bytes) -> None:
        if code == MSG_GET_PEER_ADDRESS:
            self._on_peer_address(payload)
        elif code == MSG_CONNECT_TO_PEER:
            self._on_connect_to_peer(payload)
        elif code == MSG_LOGIN:
            # повторный Login от сервера — наш аккаунт перелогинили в другом месте
            buf = _Buf(payload)
            if not buf.boolean():
                raise SlskError("нас перелогинили в другом месте")

    # ------------------------------------------------------------ адреса пиров
    def peer_address(self, username: str, timeout: float = 15.0) -> tuple[str, int] | None:
        """IP:port пользователя или None. Блокируется до ответа сервера."""
        problem = self.wait_login(timeout=10.0)
        if problem:
            return None
        waiter = {"addr": None, "event": threading.Event()}
        with self._addr_lock:
            self._addr_waiters.setdefault(username, []).append(waiter)
        try:
            self._send_server(MSG_GET_PEER_ADDRESS, _s(username))
            waiter["event"].wait(timeout + 5.0)
            return waiter["addr"]
        finally:
            with self._addr_lock:
                bucket = self._addr_waiters.get(username)
                if bucket is not None:
                    try:
                        bucket.remove(waiter)
                    except ValueError:
                        pass
                    if not bucket:
                        self._addr_waiters.pop(username, None)

    def _on_peer_address(self, payload: bytes) -> None:
        try:
            buf = _Buf(payload)
            username = buf.string()
            ip = buf.ip()
            port = buf.u32()
        except (struct.error, IndexError):
            return
        addr = (ip, port) if ip != "0.0.0.0" and port else None
        with self._addr_lock:
            waiters = self._addr_waiters.pop(username, [])
        for waiter in waiters:
            waiter["addr"] = addr
            waiter["event"].set()

    # --------------------------------------------------------- ConnectToPeer(18)
    def _on_connect_to_peer(self, payload: bytes) -> None:
        try:
            buf = _Buf(payload)
            username = buf.string()
            conn_type = buf.string()
            ip = buf.ip()
            port = buf.u32()
            token = buf.u32()
        except (struct.error, IndexError):
            return
        if not port or ip == "0.0.0.0":
            return
        if conn_type == "P":
            threading.Thread(
                target=self._pierce_search_dial, args=(username, ip, port, token),
                name=f"slsk-pierce-{username[:12]}", daemon=True,
            ).start()
        elif conn_type == "F":
            threading.Thread(
                target=self._pierce_file_transfer, args=(username, ip, port, token),
                name=f"slsk-file-{username[:12]}", daemon=True,
            ).start()
        # 'D' (distributed) игнорируем: мы не участвуем в рассылке чужих поисков

    def _pierce_search_dial(self, username: str, ip: str, port: int, token: int) -> None:
        """Пир просит нас дозвониться, чтобы отдать результаты поиска."""
        sock = None
        try:
            sock = self.open_tcp(ip, port)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(_init_frame(INIT_PIERCE, _u32(token)))
            buffer = bytearray()
            while not self._stop.is_set():
                while len(buffer) >= 8:
                    length, code = struct.unpack_from("<II", buffer, 0)
                    if length < 4 or length > MAX_DECOMPRESSED:
                        return
                    if len(buffer) < length + 4:
                        break
                    payload = bytes(buffer[8:length + 4])
                    del buffer[:length + 4]
                    if code == PMSG_FILE_SEARCH_RESPONSE:
                        try:
                            parsed = _results_from_search_response(payload)
                        except SlskError as exc:
                            self.log(f"slsk: {exc}")
                            parsed = None
                        if parsed is not None:
                            self._collect_search_results(parsed[1], parsed[2])
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, OSError):
                    return
                if not chunk:
                    return
                buffer += chunk
        except (SlskError, OSError):
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    # ---------------------------------------------------------------- поиск
    def search(self, query: str, limit: int, window: float = SEARCH_WINDOW) -> tuple[list[SlskFileResult], str]:
        """Вернуть (результаты, ошибка). Пустая ошибка — поиск прошёл."""
        problem = self.wait_login(timeout=15.0)
        if problem:
            return [], problem
        token = self._next_token()
        collector: dict = {"results": {}}
        with self._search_lock:
            self._searches[token] = collector
        try:
            self._send_server(MSG_FILE_SEARCH, _u32(token) + _s(query))
        except (SlskError, OSError):
            with self._search_lock:
                self._searches.pop(token, None)
            return [], "поиск не отправлен: соединение потеряно"
        self.log(f"slsk: ищу «{query}»")
        deadline = time.monotonic() + window
        while time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.4)
            with self._search_lock:
                if len(collector["results"]) >= limit:
                    break
        with self._search_lock:
            self._searches.pop(token, None)
            results = list(collector["results"].values())
        results.sort(key=lambda r: (not r.slot_free, r.queue_len, -(r.bitrate or 1), -r.size))
        return results[:limit], ""

    def _next_token(self) -> int:
        self._search_token = (self._search_token + 1) % (2 ** 31)
        return self._search_token

    def _collect_search_results(self, token: int, files: list[SlskFileResult]) -> None:
        with self._search_lock:
            collector = self._searches.get(token)
            if collector is None:
                return
            for result in files:
                collector["results"][(result.username, result.path)] = result

    # ----------------------------------------------------------- P-соединения
    def _reset_links(self) -> None:
        with self._links_lock:
            dead = list(self._links.values())
            self._links.clear()
        for link in dead:
            link.dead.set()

    def get_link(self, username: str, addr: tuple[str, int]) -> _PeerLink:
        """Живое P-соединение на пользователя; создаём при необходимости."""
        with self._links_lock:
            link = self._links.get(username)
            if link is not None and not link.dead.is_set():
                return link
            link = _PeerLink(self, username, addr[0], addr[1], on_death=self._link_died)
            self._links[username] = link
            link.start()
        return link

    def _link_died(self, link: _PeerLink) -> None:
        with self._links_lock:
            if self._links.get(link.username) is link:
                del self._links[link.username]

    # --------------------------------------------------------------- закачки
    def download(self, result: SlskFileResult) -> SlskTransfer:
        transfer = SlskTransfer(self, result)
        with self._transfers_lock:
            self.transfers.append(transfer)
        threading.Thread(
            target=self._transfer_worker, args=(transfer,),
            name=f"slsk-dl-{result.username[:12]}", daemon=True,
        ).start()
        return transfer

    def downloads(self) -> list[SlskTransfer]:
        with self._transfers_lock:
            return list(self.transfers)

    def _transfer_worker(self, transfer: SlskTransfer) -> None:
        """Цикл закачки: адрес → P-соединение → очередь → grant → F-поток."""
        attempts = 0
        try:
            while not transfer._stopped.is_set() and attempts < 5:
                attempts += 1
                outcome = self._queue_phase(transfer)
                if outcome == "stopped":
                    transfer.mark("stopped")
                    return
                if outcome == "denied":
                    reason = transfer.error or "отказ пира"
                    if TASK_LEGACY in reason and not transfer.legacy:
                        # старый клиент не понял utf-8 путь — повторяем latin-1
                        transfer.legacy = True
                        transfer._outcome = None
                        transfer._outcome_event.clear()
                        continue
                    transfer.mark("error", reason)
                    return
                if outcome == "fatal":
                    transfer.mark("error", transfer.error or "пир недоступен")
                    return
                if outcome != "grant":
                    # таймаут/разрыв — переочередь с чистым outcome
                    transfer._outcome = None
                    transfer._outcome_event.clear()
                    time.sleep(1.5)
                    continue
                ok = self._file_phase(transfer)
                if ok == "complete":
                    return
                if ok == "stopped" or transfer._stopped.is_set():
                    transfer.mark("stopped")
                    return
                # обрыв передачи: резюм через переочередь
                transfer._outcome = None
                transfer._outcome_event.clear()
                transfer.grant_token = 0
                time.sleep(1.5)
            if not transfer.is_done():
                transfer.mark("error", transfer.error or "не удалось получить файл")
        except Exception as exc:  # noqa: BLE001
            transfer.mark("error", f"сбой загрузки: {exc}")

    def _queue_phase(self, transfer: SlskTransfer) -> str:
        """QueueUpload и ожидание grant. grant | denied | fatal | retry | stopped."""
        addr = self.peer_address(transfer.result.username)
        if not addr or not addr[1]:
            transfer.error = f"{transfer.result.username} недоступен (offline или за NAT)"
            return "fatal"
        link = self.get_link(transfer.result.username, addr)
        deadline = time.monotonic() + 25.0
        while link.sock is None and not link.dead.is_set() and time.monotonic() < deadline:
            if transfer._stopped.is_set():
                return "stopped"
            time.sleep(0.2)
        if link.dead.is_set():
            transfer.error = ""
            return "retry"
        try:
            link.queue_upload(transfer, legacy=transfer.legacy)
        except (SlskError, OSError):
            transfer.error = ""
            return "retry"
        self.log_link(transfer.result.username, f"запрошен {transfer.result.basename}")
        # в очереди можно сидеть долго; ждём grant/отказ, отслеживая смерть линии
        while not transfer._outcome_event.is_set():
            if transfer._stopped.is_set():
                return "stopped"
            if link.dead.is_set():
                self.log_link(transfer.result.username, "P-соединение умерло в очереди")
                transfer.error = ""
                return "retry"
            transfer._outcome_event.wait(2.0)
        if transfer._outcome == "grant":
            return "grant"
        if transfer._outcome == "denied":
            return "denied"
        if transfer._outcome == "upload_failed":
            transfer.error = ""
            return "retry"
        transfer.error = ""
        return "retry"

    def _file_phase(self, transfer: SlskTransfer) -> str:
        """Ждать ConnectToPeer('F') и прокачать файловый поток до конца."""
        holder: dict = {"event": threading.Event(), "result": None}
        username = transfer.result.username
        with self._file_waiters_lock:
            self._file_waiters[username] = holder
        try:
            deadline = time.monotonic() + 300.0
            while not holder["event"].is_set():
                if transfer._stopped.is_set():
                    return "stopped"
                if time.monotonic() > deadline:
                    transfer.error = "пир не начал отдачу"
                    return "failed"
                holder["event"].wait(2.0)
            return holder["result"] or "failed"
        finally:
            with self._file_waiters_lock:
                if self._file_waiters.get(username) is holder:
                    self._file_waiters.pop(username, None)

    def _pierce_file_transfer(self, username: str, ip: str, port: int, pierce_token: int) -> None:
        """Сервер принёс ConnectToPeer('F'): дозвон, PierceFirewall, поток файла."""
        with self._file_waiters_lock:
            holder = self._file_waiters.pop(username, None)
        if holder is None:
            return  # никто не ждёт файл от этого пользователя
        transfer = None
        sock = None
        try:
            sock = self.open_tcp(ip, port)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(_init_frame(INIT_PIERCE, _u32(pierce_token)))
            # FileTransferInit: 4 сырых байта токена grant'а
            granted = struct.unpack("<I", _recv_exact(sock, 4, timeout=45.0))[0]
            with self._transfers_lock:
                for candidate in self.transfers:
                    if (candidate.result.username == username
                            and candidate.grant_token == granted
                            and not candidate.is_done()):
                        transfer = candidate
                        break
            if transfer is None:
                holder["result"] = "failed"
                holder["event"].set()
                return
            holder["result"] = self._stream_file(sock, transfer)
        except (SlskError, OSError) as exc:
            if transfer is not None and not transfer.is_done():
                transfer.error = f"передача оборвалась: {exc}"
            holder["result"] = "failed"
        finally:
            holder["event"].set()
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _stream_file(self, sock: socket.socket, transfer: SlskTransfer) -> str:
        """FileOffset → сырой поток в .part → rename в цель. complete|failed|stopped."""
        transfer.target_dir.mkdir(parents=True, exist_ok=True)
        offset = 0
        if transfer.incomplete_path.exists():
            offset = transfer.incomplete_path.stat().st_size
        if offset >= max(1, transfer.total):
            # «хвост» от другого файла — начинаем заново
            transfer.incomplete_path.unlink()
            offset = 0
        sock.sendall(_u64(offset))  # FileOffset без рамки
        transfer.mark("downloading")
        transfer.error = ""
        remaining = transfer.total - offset
        with transfer._lock:
            transfer.downloaded = offset
        with open(transfer.incomplete_path, "ab") as handle:
            while remaining > 0:
                if transfer._stopped.is_set():
                    return "stopped"
                try:
                    chunk = sock.recv(min(1 << 16, remaining))
                except socket.timeout:
                    transfer.error = "пиры молчат больше минуты"
                    return "failed"
                if not chunk:
                    transfer.error = f"передача оборвалась на {transfer.downloaded} из {transfer.total} байт"
                    return "failed"
                handle.write(chunk)
                remaining -= len(chunk)
                transfer.update_progress(len(chunk))
        target = transfer.file_path
        if target.exists():
            target.unlink()
        transfer.incomplete_path.rename(target)
        transfer.mark("complete")
        self.log_link(transfer.result.username, f"готово: {transfer.result.basename}")
        if transfer.on_complete is not None:
            try:
                transfer.on_complete(transfer)
            except Exception:
                pass
        return "complete"

