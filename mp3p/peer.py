"""BitTorrent peer wire protocol (BEP 3) plus BEP 10 / 9 / 11 extensions.

Every connection is established through :meth:`mp3p.net.Proxy.open_tcp`, so peer
traffic never touches the operator's real address when a proxy is configured.
Supported extensions:

* BEP 10 — extension protocol handshake
* BEP 9  — ``ut_metadata`` (magnet links carry no file list, this fetches it)
* BEP 11 — ``ut_pex`` (peer exchange; replaces DHT/UDP trackers behind a proxy)
"""

from __future__ import annotations

import hashlib
import socket
import struct
import threading
import time

from . import bencode
from .net import Proxy
from .tracker import PeerAddress

PROTOCOL = b"BitTorrent protocol"
BLOCK_SIZE = 16 * 1024
METADATA_PIECE_SIZE = 16 * 1024

MSG_CHOKE = 0
MSG_UNCHOKE = 1
MSG_INTERESTED = 2
MSG_NOT_INTERESTED = 3
MSG_HAVE = 4
MSG_BITFIELD = 5
MSG_REQUEST = 6
MSG_PIECE = 7
MSG_CANCEL = 8
MSG_PORT = 9
MSG_EXTENDED = 20

EXT_HANDSHAKE_ID = 0
OUR_UT_METADATA_ID = 1
OUR_UT_PEX_ID = 2

IDLE_TIMEOUT = 75.0
CONNECT_TIMEOUT = 15.0
# Окно запросов к ОДНОМУ пиру: до 8 кусков и не больше 96 блоков (1.5 MB).
# Раньше окно было 12 БЛОКОВ (~200 KB): через латентный прокси это резало
# скорость пира до ~1 MB/s, сколько бы带宽 он ни готов был отдать.
PIPELINE_PIECES = 8
MAX_OUTSTANDING_BLOCKS = 96


class PeerError(OSError):
    pass


class PeerConnection(threading.Thread):
    """One peer socket plus the request pipeline for it."""

    def __init__(self, session, address: PeerAddress, proxy: Proxy | None):
        super().__init__(daemon=True, name=f"peer-{address.host}:{address.port}")
        self.session = session
        self.address = address
        self.proxy = proxy
        self.sock: socket.socket | None = None

        self.bitfield = bytearray()
        self.peer_choking = True
        self.peer_interested = False
        self.am_interested = False
        self.connected = False
        self.closed = False
        self.error = ""
        self.peer_id = b""

        self.ext_enabled = False
        self.their_ut_metadata = 0
        self.their_ut_pex = 0
        self.metadata_size = 0
        self.peer_client = ""

        self.outstanding: dict[tuple[int, int], int] = {}
        self.requested_pieces: set[int] = set()
        self.downloaded = 0
        self.uploaded = 0
        self.last_activity = time.monotonic()
        self.connected_at = 0.0
        self.last_piece_at = 0.0
        self._pex_sent_added: set[tuple[str, int]] = set()
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------ public
    @property
    def label(self) -> str:
        return str(self.address)

    def stop(self) -> None:
        self.closed = True
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------- thread
    def run(self) -> None:
        try:
            self._connect()
            self._handshake()
            self._loop()
        except (PeerError, OSError, ValueError) as exc:
            if not self.closed:
                self.error = str(exc) or exc.__class__.__name__
        finally:
            self.closed = True
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
            self.session.peer_gone(self)

    def _connect(self) -> None:
        if self.proxy is not None:
            self.sock = self.proxy.open_tcp(self.address.host, self.address.port, timeout=CONNECT_TIMEOUT)
        else:
            self.sock = socket.create_connection(
                (self.address.host, self.address.port), timeout=CONNECT_TIMEOUT
            )
        self.sock.settimeout(IDLE_TIMEOUT)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connected = True
        self.connected_at = time.monotonic()

    def _handshake(self) -> None:
        reserved = bytearray(8)
        reserved[5] |= 0x10  # BEP 10 extension protocol
        payload = bytes([len(PROTOCOL)]) + PROTOCOL + bytes(reserved) + self.session.info_hash + self.session.peer_id
        self._send_raw(payload)

        header = self._recv_exact(68)
        if header[0] != len(PROTOCOL) or header[1:20] != PROTOCOL:
            raise PeerError("bad handshake")
        if header[28:48] != self.session.info_hash:
            raise PeerError("info hash mismatch")
        self.peer_id = header[48:68]
        self.ext_enabled = bool(header[25] & 0x10)
        if self.ext_enabled:
            self._send_ext_handshake()

    def _send_ext_handshake(self) -> None:
        blob = self.session.metadata_blob()
        metadata_size = len(blob) if blob else 0
        payload = bencode.encode({
            b"m": {b"ut_metadata": OUR_UT_METADATA_ID, b"ut_pex": OUR_UT_PEX_ID},
            b"v": b"mp3player/1.0",
            b"p": self.session.listen_port,
            b"metadata_size": metadata_size,
            b"reqq": 250,
        })
        self._send_extended(EXT_HANDSHAKE_ID, payload)

    def request_metadata_piece(self, index: int) -> None:
        if not self.their_ut_metadata:
            return
        self._send_extended(self.their_ut_metadata, bencode.encode({b"msg_type": 0, b"piece": index}))

    # --------------------------------------------------------------------- loop
    def _loop(self) -> None:
        while not self.closed:
            if not self._read_message():
                break
            self._fill_pipeline()

    def _read_message(self) -> bool:
        try:
            header = self._recv_exact(4, allow_eof=True)
        except socket.timeout:
            raise PeerError("idle timeout")
        if header is None:
            return False
        length = struct.unpack(">I", header)[0]
        if length == 0:
            self.last_activity = time.monotonic()
            return True
        if length > 2 * 1024 * 1024:
            raise PeerError(f"oversized message: {length}")
        payload = self._recv_exact(length)
        if not payload:
            return False
        self.last_activity = time.monotonic()
        msg_id = payload[0]
        body = payload[1:]
        try:
            self._dispatch(msg_id, body)
        except (ValueError, KeyError, IndexError) as exc:
            raise PeerError(f"bad message {msg_id}: {exc}") from exc
        return True

    def _dispatch(self, msg_id: int, body: bytes) -> None:
        if msg_id == MSG_CHOKE:
            self.peer_choking = True
            self._drop_outstanding()
        elif msg_id == MSG_UNCHOKE:
            self.peer_choking = False
            self._fill_pipeline()
        elif msg_id == MSG_INTERESTED:
            self.peer_interested = True
        elif msg_id == MSG_NOT_INTERESTED:
            self.peer_interested = False
        elif msg_id == MSG_HAVE:
            if len(body) >= 4:
                index = struct.unpack(">I", body[:4])[0]
                self._set_have(index)
                if self.session.metainfo is not None and self.session.wants_piece(index):
                    self._send_interested()
        elif msg_id == MSG_BITFIELD:
            self.bitfield = bytearray(body)
            if self.session.metainfo is not None and self.session.wants_piece_anywhere(self):
                self._send_interested()
        elif msg_id == MSG_REQUEST:
            self._on_request(body)
        elif msg_id == MSG_PIECE:
            self._on_piece(body)
        elif msg_id == MSG_CANCEL:
            pass
        elif msg_id == MSG_EXTENDED:
            self._on_extended(body)

    # ----------------------------------------------------------- extended (BEP10)
    def _on_extended(self, body: bytes) -> None:
        if not body:
            return
        ext_id = body[0]
        payload = body[1:]
        if ext_id == EXT_HANDSHAKE_ID:
            try:
                data = bencode.decode(payload, strict=False)
            except bencode.BencodeError:
                return
            if not isinstance(data, dict):
                return
            mapping = data.get(b"m") or {}
            if isinstance(mapping, dict):
                self.their_ut_metadata = int(mapping.get(b"ut_metadata") or 0)
                self.their_ut_pex = int(mapping.get(b"ut_pex") or 0)
            self.metadata_size = int(data.get(b"metadata_size") or 0)
            client = data.get(b"v")
            if isinstance(client, bytes):
                self.peer_client = client.decode("utf-8", "replace")[:24]
            self.session.on_extended_handshake(self)
            if self.their_ut_pex:
                self.send_pex()
            self._fill_pipeline()
        elif ext_id == OUR_UT_METADATA_ID:
            self._on_ut_metadata(payload)
        elif ext_id == OUR_UT_PEX_ID:
            self._on_ut_pex(payload)

    def _on_ut_metadata(self, payload: bytes) -> None:
        try:
            header, offset = bencode.decode_from(payload, 0, strict=False)
        except bencode.BencodeError:
            return
        if not isinstance(header, dict):
            return
        msg_type = int(header.get(b"msg_type", -1))
        piece = int(header.get(b"piece", 0))
        if msg_type == 1:  # data
            data = payload[offset:]
            self.session.on_metadata_piece(self, piece, data)
        elif msg_type == 0:  # they want a piece from us
            blob = self.session.metadata_blob()
            if blob is None:
                return
            start = piece * METADATA_PIECE_SIZE
            chunk = blob[start:start + METADATA_PIECE_SIZE]
            if not chunk:
                return
            response = bencode.encode({b"msg_type": 1, b"piece": piece, b"total_size": len(blob)})
            self._send_extended(self.their_ut_metadata, response + chunk)
        # msg_type 2 (reject) is simply ignored; another peer will serve the piece

    def _on_ut_pex(self, payload: bytes) -> None:
        try:
            data = bencode.decode(payload, strict=False)
        except bencode.BencodeError:
            return
        if not isinstance(data, dict):
            return
        added = data.get(b"added")
        peers: list[PeerAddress] = []
        if isinstance(added, bytes):
            for offset in range(0, len(added) - 5, 6):
                ip = socket.inet_ntoa(added[offset:offset + 4])
                port = struct.unpack(">H", added[offset + 4:offset + 6])[0]
                if port and not ip.startswith(("0.", "127.")):
                    peers.append(PeerAddress(ip, port))
        added6 = data.get(b"added6")
        if isinstance(added6, bytes):
            for offset in range(0, len(added6) - 17, 18):
                ip = socket.inet_ntop(socket.AF_INET6, added6[offset:offset + 16])
                port = struct.unpack(">H", added6[offset + 16:offset + 18])[0]
                if port:
                    peers.append(PeerAddress(ip, port))
        if peers:
            self.session.on_pex_peers(peers)

    def send_pex(self) -> None:
        if not self.their_ut_pex:
            return
        candidates = self.session.pex_candidates(self)
        fresh = [p for p in candidates if p.key not in self._pex_sent_added][:40]
        if not fresh:
            return
        added = b"".join(
            socket.inet_aton(p.host) + struct.pack(">H", p.port)
            for p in fresh
            if ":" not in p.host
        )
        if not added:
            return
        flags = b"\x01" * (len(added) // 6)
        payload = bencode.encode({b"added": added, b"added.f": flags, b"dropped": b""})
        self._send_extended(self.their_ut_pex, payload)
        self._pex_sent_added.update(p.key for p in fresh)

    # ------------------------------------------------------------ piece traffic
    def _on_piece(self, body: bytes) -> None:
        if len(body) < 8:
            return
        index, begin = struct.unpack(">II", body[:8])
        block = body[8:]
        self.outstanding.pop((index, begin), None)
        self.downloaded += len(block)
        self.last_piece_at = time.monotonic()
        self.session.on_block(self, index, begin, block)

    def _on_request(self, body: bytes) -> None:
        if len(body) < 12:
            return
        index, begin, length = struct.unpack(">III", body[:12])
        if length > 128 * 1024:
            return
        data = self.session.read_block(index, begin, length)
        if data is None:
            return
        self.uploaded += len(data)
        self._send(MSG_PIECE, struct.pack(">II", index, begin) + data)

    def _drop_outstanding(self) -> None:
        # one release per piece, not per block: the session counts in-flight pieces
        for index in {index for index, _begin in list(self.outstanding)}:
            self.session.release_piece(index)
        self.outstanding.clear()
        self.requested_pieces.clear()

    def ensure_interested(self) -> None:
        """Announce interest as soon as there is something to fetch.

        Metadata can arrive *after* a peer's bitfield, so this must be re-checked
        independently of message handling — otherwise the peer stays choked forever.
        """
        if self.closed or self.session.metainfo is None or self.am_interested:
            return
        if not self.bitfield:
            return
        if self.session.wants_piece_anywhere(self):
            self._send_interested()

    def _fill_pipeline(self) -> None:
        if self.closed or self.session.metainfo is None:
            return
        self.ensure_interested()
        if self.peer_choking or not self.am_interested:
            return
        if len(self.outstanding) >= MAX_OUTSTANDING_BLOCKS:
            return
        depth = PIPELINE_PIECES - len(self.requested_pieces)
        picks = self.session.pick_pieces(self, depth)
        for position, index in enumerate(picks):
            budget = MAX_OUTSTANDING_BLOCKS - len(self.outstanding)
            if budget <= 0:
                # окно блоков забито ранее выбранными кусками — незапрошенные
                # выборки вернуть сессии, иначе они зависнут в inflight
                for leftover in picks[position:]:
                    self.session.release_piece(leftover)
                break
            plan = self.session.block_plan(index, budget)
            for begin, block_len in plan:
                self.outstanding[(index, begin)] = block_len
                self._send(MSG_REQUEST, struct.pack(">III", index, begin, block_len))
            if len(self.outstanding) >= MAX_OUTSTANDING_BLOCKS:
                break

    def _send_interested(self) -> None:
        if not self.am_interested:
            self.am_interested = True
            self._send(MSG_INTERESTED, b"")

    def _set_have(self, index: int) -> None:
        byte = index // 8
        if byte >= len(self.bitfield):
            self.bitfield.extend(b"\x00" * (byte + 1 - len(self.bitfield)))
        self.bitfield[byte] |= 1 << (7 - index % 8)

    def has_piece(self, index: int) -> bool:
        byte = index // 8
        if byte >= len(self.bitfield):
            return False
        return bool(self.bitfield[byte] & (1 << (7 - index % 8)))

    # ---------------------------------------------------------------- primitives
    def _send(self, msg_id: int, payload: bytes = b"") -> None:
        self._send_raw(struct.pack(">IB", len(payload) + 1, msg_id) + payload)

    def _send_extended(self, ext_id: int, payload: bytes) -> None:
        self._send(MSG_EXTENDED, bytes([ext_id]) + payload)

    def _send_raw(self, data: bytes) -> None:
        sock = self.sock
        if sock is None:
            raise PeerError("socket closed")
        with self._write_lock:
            sock.sendall(data)

    def _recv_exact(self, count: int, allow_eof: bool = False) -> bytes | None:
        sock = self.sock
        if sock is None:
            raise PeerError("socket closed")
        chunks = bytearray()
        while len(chunks) < count:
            chunk = sock.recv(count - len(chunks))
            if not chunk:
                if allow_eof and not chunks:
                    return None
                raise PeerError("peer closed connection")
            chunks += chunk
        return bytes(chunks)


def verify_piece(data: bytes, expected: bytes) -> bool:
    return hashlib.sha1(data).digest() == expected
