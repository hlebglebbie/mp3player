"""Torrent metadata: magnets, .torrent parsing, file/piece addressing."""

from __future__ import annotations

import hashlib
import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Iterable

from . import bencode
from .config import MUSIC_EXTS

PIECE_HASH_LEN = 20


class TorrentError(ValueError):
    pass


@dataclass(frozen=True)
class TorrentFile:
    index: int
    path_parts: tuple[str, ...]
    length: int
    offset: int
    first_piece: int
    last_piece: int

    @property
    def path(self) -> str:
        return "/".join(self.path_parts)

    @property
    def name(self) -> str:
        return self.path_parts[-1] if self.path_parts else ""

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def is_audio(self) -> bool:
        return self.ext in MUSIC_EXTS and self.length > 0

    @property
    def piece_span(self) -> int:
        return self.last_piece - self.first_piece + 1

    def safe_path_parts(self) -> tuple[str, ...]:
        parts = []
        for part in self.path_parts:
            cleaned = part.replace("\x00", "").replace("/", "_")
            if cleaned in ("", ".", ".."):
                cleaned = "_"
            parts.append(cleaned)
        return tuple(parts) or ("unnamed",)


@dataclass
class Metainfo:
    info_hash: bytes
    name: str
    piece_length: int
    piece_hashes: list[bytes]
    files: list[TorrentFile]
    total_length: int
    trackers: list[str] = field(default_factory=list)
    private: bool = False
    comment: str = ""
    created_by: str = ""
    creation_date: int = 0
    is_multi_file: bool = False

    @property
    def piece_count(self) -> int:
        return len(self.piece_hashes)

    @property
    def hex_hash(self) -> str:
        return self.info_hash.hex().upper()

    def file_for_piece(self, piece: int) -> list[TorrentFile]:
        return [f for f in self.files if f.first_piece <= piece <= f.last_piece]

    def audio_files(self) -> list[TorrentFile]:
        return [f for f in self.files if f.is_audio]

    def file_by_index(self, index: int) -> TorrentFile | None:
        for f in self.files:
            if f.index == index:
                return f
        return None

    def piece_size(self, piece: int) -> int:
        if piece == self.piece_count - 1:
            remainder = self.total_length - self.piece_length * (self.piece_count - 1)
            if remainder > 0:
                return remainder
        return self.piece_length

    def pieces_for_files(self, file_indexes: Iterable[int]) -> set[int]:
        wanted: set[int] = set()
        for idx in file_indexes:
            f = self.file_by_index(idx)
            if f:
                wanted.update(range(f.first_piece, f.last_piece + 1))
        return wanted

    def bytes_for_pieces(self, pieces: Iterable[int]) -> int:
        return sum(self.piece_size(p) for p in pieces)


def _build_files(info: dict, piece_length: int) -> tuple[list[TorrentFile], int, bool]:
    files: list[TorrentFile] = []
    offset = 0
    if b"files" in info:
        entries = info[b"files"]
        if not isinstance(entries, list) or not entries:
            raise TorrentError("torrent has an empty file list")
        multi = True
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or b"length" not in entry:
                continue
            parts = entry.get(b"path") or []
            parts = [p.decode("utf-8", "replace") for p in parts if p]
            length = int(entry[b"length"])
            if length < 0:
                raise TorrentError("negative file length")
            first = offset // piece_length
            last = max(first, (offset + length - 1) // piece_length) if length else first
            files.append(
                TorrentFile(index, tuple(parts), length, offset, first, last)
            )
            offset += length
        total = offset
    else:
        multi = False
        length = int(info.get(b"length", 0))
        name = (info.get(b"name") or b"unnamed").decode("utf-8", "replace")
        piece_count = max(1, -(-length // piece_length)) if length else 0
        files.append(TorrentFile(0, (name,), length, 0, 0, max(0, piece_count - 1)))
        total = length
    return files, total, multi


def parse_info_dict(info: dict, trackers: Iterable[str] | None = None) -> Metainfo:
    """Build :class:`Metainfo` from a raw bencoded ``info`` dictionary."""
    if not isinstance(info, dict):
        raise TorrentError("info is not a dictionary")
    raw_pieces = info.get(b"pieces")
    if not isinstance(raw_pieces, bytes) or len(raw_pieces) % PIECE_HASH_LEN != 0:
        raise TorrentError("invalid pieces field")
    piece_length = int(info.get(b"piece length", 0))
    if piece_length <= 0 or piece_length % 16384 != 0:
        # be permissive: some trackers ship odd piece lengths
        if piece_length <= 0:
            raise TorrentError("invalid piece length")
    piece_hashes = [
        raw_pieces[i:i + PIECE_HASH_LEN] for i in range(0, len(raw_pieces), PIECE_HASH_LEN)
    ]
    info_hash = hashlib.sha1(bencode.encode(info)).digest()
    files, total, multi = _build_files(info, piece_length)
    if multi and info.get(b"name"):
        prefix = info[b"name"].decode("utf-8", "replace")
        files = [
            TorrentFile(f.index, (prefix,) + f.path_parts, f.length, f.offset, f.first_piece, f.last_piece)
            for f in files
        ]
    name = (info.get(b"name") or b"unnamed").decode("utf-8", "replace")
    return Metainfo(
        info_hash=info_hash,
        name=name,
        piece_length=piece_length,
        piece_hashes=piece_hashes,
        files=files,
        total_length=total,
        trackers=_dedupe(trackers or []),
        private=bool(info.get(b"private")),
        comment=(info.get(b"comment") or b"").decode("utf-8", "replace"),
        created_by=(info.get(b"created by") or b"").decode("utf-8", "replace"),
        creation_date=int(info.get(b"creation date") or 0),
        is_multi_file=multi,
    )


def parse_torrent(data: bytes) -> Metainfo:
    try:
        root = bencode.decode(data)
    except bencode.BencodeError as exc:
        raise TorrentError(f"not a valid torrent file: {exc}") from exc
    if not isinstance(root, dict) or b"info" not in root:
        raise TorrentError("torrent file has no info dictionary")
    trackers = _trackers_from(root)
    return parse_info_dict(root[b"info"], trackers)


def _trackers_from(root: dict) -> list[str]:
    found: list[str] = []
    announce = root.get(b"announce")
    if isinstance(announce, bytes):
        found.append(announce.decode("utf-8", "replace"))
    for tier in root.get(b"announce-list") or []:
        if isinstance(tier, list):
            for url in tier:
                if isinstance(url, bytes):
                    found.append(url.decode("utf-8", "replace"))
    return found


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        url = url.strip()
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


@dataclass
class Magnet:
    info_hash: bytes
    name: str = ""
    trackers: list[str] = field(default_factory=list)

    @property
    def hex_hash(self) -> str:
        return self.info_hash.hex().upper()


def parse_magnet(uri: str) -> Magnet:
    if not uri.lower().startswith("magnet:"):
        raise TorrentError("not a magnet link")
    query = urllib.parse.urlsplit(uri).query
    params = urllib.parse.parse_qsl(query, keep_blank_values=True)
    info_hash: bytes | None = None
    name = ""
    trackers: list[str] = []
    for key, value in params:
        low = key.lower()
        if low == "xt" and value.lower().startswith("urn:btih:"):
            raw = value[9:]
            if len(raw) == 40:
                try:
                    info_hash = bytes.fromhex(raw)
                except ValueError:
                    pass
            elif len(raw) == 32:
                import base64

                try:
                    info_hash = base64.b32decode(raw.upper())
                except Exception:
                    pass
        elif low == "dn":
            name = value
        elif low == "tr":
            trackers.append(value)
    if info_hash is None:
        raise TorrentError("magnet has no usable btih info hash")
    return Magnet(info_hash, name, _dedupe(trackers))


def build_magnet(info_hash: bytes | str, name: str = "", trackers: Iterable[str] = ()) -> str:
    if isinstance(info_hash, bytes):
        hash_text = info_hash.hex().upper()
    else:
        hash_text = info_hash.strip()
    parts = [f"xt=urn:btih:{hash_text}"]
    if name:
        parts.append("dn=" + urllib.parse.quote(name))
    for tracker in trackers:
        parts.append("tr=" + urllib.parse.quote(tracker, safe=""))
    return "magnet:?" + "&".join(parts)
