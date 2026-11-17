"""HTTP(S) tracker announces, tunnelled through the configured proxy.

UDP trackers are deliberately not spoken here: a TCP proxy cannot carry raw UDP.
Peer discovery therefore relies on HTTP trackers, DHT is skipped, and the peer
exchange extension (ut_pex) fills the gap once the first peers are known.
"""

from __future__ import annotations

import random
import socket
import time
import string
import struct
import urllib.parse
from dataclasses import dataclass, field

from . import bencode
from .net import Proxy, http_request

PEER_ID_PREFIX = "-MP1000-"


def make_peer_id() -> bytes:
    suffix = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    return (PEER_ID_PREFIX + suffix).encode("ascii")


@dataclass(frozen=True, order=True)
class PeerAddress:
    host: str
    port: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.host, self.port)

    def __str__(self) -> str:
        host = self.host
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{self.port}"


@dataclass
class AnnounceResult:
    peers: list[PeerAddress] = field(default_factory=list)
    interval: int = 1800
    min_interval: int = 0
    seeders: int = 0
    leechers: int = 0
    tracker_id: str = ""
    warning: str = ""
    error: str = ""


def parse_compact_peers(raw: bytes) -> list[PeerAddress]:
    peers: list[PeerAddress] = []
    for offset in range(0, len(raw) - 5, 6):
        ip = socket.inet_ntoa(raw[offset:offset + 4])
        port = struct.unpack(">H", raw[offset + 4:offset + 6])[0]
        if port and not ip.startswith("0."):
            peers.append(PeerAddress(ip, port))
    return peers


def parse_compact_peers6(raw: bytes) -> list[PeerAddress]:
    peers: list[PeerAddress] = []
    for offset in range(0, len(raw) - 17, 18):
        ip = socket.inet_ntop(socket.AF_INET6, raw[offset:offset + 16])
        port = struct.unpack(">H", raw[offset + 16:offset + 18])[0]
        if port:
            peers.append(PeerAddress(ip, port))
    return peers


def parse_peers(raw) -> list[PeerAddress]:
    if isinstance(raw, bytes):
        return parse_compact_peers(raw)
    peers: list[PeerAddress] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            ip = entry.get(b"ip") or b""
            port = entry.get(b"port") or 0
            if isinstance(ip, bytes) and ip:
                try:
                    peers.append(PeerAddress(ip.decode("latin-1"), int(port)))
                except (ValueError, UnicodeDecodeError):
                    continue
    return peers


class TrackerError(OSError):
    pass


class HTTPTracker:
    def __init__(self, url: str):
        self.url = url

    @property
    def scheme(self) -> str:
        return self.url.split(":", 1)[0].lower()

    @property
    def usable(self) -> bool:
        return self.scheme in ("http", "https")

    def announce(
        self,
        info_hash: bytes,
        peer_id: bytes,
        port: int,
        left: int,
        proxy: Proxy | None,
        event: str = "",
        uploaded: int = 0,
        downloaded: int = 0,
        numwant: int = 60,
        timeout: float = 20.0,
    ) -> AnnounceResult:
        if not self.usable:
            raise TrackerError(f"tracker scheme not usable through a TCP proxy: {self.url}")
        separator = "&" if "?" in self.url else "?"
        params = [
            "info_hash=" + urllib.parse.quote(info_hash, safe=""),
            "peer_id=" + urllib.parse.quote(peer_id, safe=""),
            f"port={port}",
            f"uploaded={uploaded}",
            f"downloaded={downloaded}",
            f"left={left}",
            "compact=1",
            f"numwant={numwant}",
        ]
        if event:
            params.append(f"event={event}")
        url = self.url + separator + "&".join(params)
        response = http_request(
            url,
            proxy=proxy,
            timeout=timeout,
            headers={"Accept": "text/plain", "User-Agent": "mp3player/1.0"},
        )
        if response.status >= 400:
            raise TrackerError(f"tracker HTTP {response.status}")
        try:
            payload = bencode.decode(response.body, strict=False)
        except bencode.BencodeError as exc:
            raise TrackerError(f"tracker sent invalid bencode: {exc}") from exc
        if not isinstance(payload, dict):
            raise TrackerError("tracker response is not a dictionary")
        if b"failure reason" in payload:
            reason = payload[b"failure reason"].decode("utf-8", "replace")
            raise TrackerError(reason)

        result = AnnounceResult()
        peers = parse_peers(payload.get(b"peers", b""))
        peers.extend(parse_compact_peers6(payload.get(b"peers6", b"")))
        result.peers = _dedupe_peers(peers)
        result.interval = int(payload.get(b"interval") or 1800)
        result.min_interval = int(payload.get(b"min interval") or 0)
        result.seeders = int(payload.get(b"complete") or 0)
        result.leechers = int(payload.get(b"incomplete") or 0)
        tracker_id = payload.get(b"tracker id")
        if isinstance(tracker_id, bytes):
            result.tracker_id = tracker_id.decode("latin-1")
        warning = payload.get(b"warning message")
        if isinstance(warning, bytes):
            result.warning = warning.decode("utf-8", "replace")
        return result


def _dedupe_peers(peers: list[PeerAddress]) -> list[PeerAddress]:
    seen: set[tuple[str, int]] = set()
    out: list[PeerAddress] = []
    for peer in peers:
        if peer.key not in seen:
            seen.add(peer.key)
            out.append(peer)
    return out


def announce_one(
    url: str,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    left: int,
    proxy: Proxy | None,
    event: str = "",
    timeout: float = 10.0,
) -> AnnounceResult:
    """Announce to a single tracker."""
    return HTTPTracker(url).announce(
        info_hash=info_hash,
        peer_id=peer_id,
        port=port,
        left=left,
        proxy=proxy,
        event=event,
        timeout=timeout,
    )


def announce_parallel(
    trackers: list[str],
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    left: int,
    proxy: Proxy | None,
    event: str = "",
    timeout: float = 10.0,
    budget: float = 20.0,
    on_peers=None,
    should_stop=None,
) -> tuple[list[PeerAddress], list[str], int]:
    """Announce to all HTTP trackers concurrently.

    Peers are handed to ``on_peers`` as soon as any tracker answers, so the
    download starts while slower trackers are still being queried.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    usable = [url for url in trackers if HTTPTracker(url).usable]
    peers: list[PeerAddress] = []
    errors: list[str] = []
    interval = 1800
    if not usable:
        return peers, errors, interval
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(10, len(usable)), thread_name_prefix="announce") as pool:
        futures = {
            pool.submit(announce_one, url, info_hash, peer_id, port, left, proxy, event, timeout): url
            for url in usable
        }
        for future in as_completed(futures, timeout=budget):
            url = futures[future]
            try:
                result = future.result()
            except (TrackerError, OSError, ValueError) as exc:
                errors.append(f"{_short_url(url)}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - keep the swarm usable
                errors.append(f"{_short_url(url)}: {exc}")
                continue
            if result.peers:
                interval = min(interval, max(60, result.interval))
                peers.extend(result.peers)
                if on_peers is not None:
                    try:
                        on_peers(result.peers)
                    except Exception:
                        pass
            if should_stop is not None and should_stop():
                break
            if time.monotonic() - started > budget:
                break
    return _dedupe_peers(peers), errors, interval


def _short_url(url: str, width: int = 48) -> str:
    host = urllib.parse.urlsplit(url).hostname or url
    return host if len(host) <= width else host[:width] + "…"
