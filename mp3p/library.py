"""SQLite-backed music library: tracks, likes, playlists, play history."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    NOT NULL UNIQUE,
    title       TEXT    NOT NULL DEFAULT '',
    artist      TEXT    NOT NULL DEFAULT '',
    album       TEXT    NOT NULL DEFAULT '',
    track_no    INTEGER NOT NULL DEFAULT 0,
    duration    REAL    NOT NULL DEFAULT 0,
    bitrate     INTEGER NOT NULL DEFAULT 0,
    size        INTEGER NOT NULL DEFAULT 0,
    info_hash   TEXT    NOT NULL DEFAULT '',
    source_name TEXT    NOT NULL DEFAULT '',
    liked       INTEGER NOT NULL DEFAULT 0,
    play_count  INTEGER NOT NULL DEFAULT 0,
    added_at    REAL    NOT NULL DEFAULT 0,
    played_at   REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tracks_liked ON tracks(liked);
CREATE INDEX IF NOT EXISTS tracks_artist ON tracks(artist);

CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    created_at REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position    REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, track_id)
);
"""


@dataclass
class Track:
    id: int
    path: str
    title: str
    artist: str
    album: str
    track_no: int
    duration: float
    bitrate: int
    size: int
    liked: bool
    play_count: int
    added_at: float

    @property
    def label(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or self.artist or Path(self.path).name

    @property
    def duration_text(self) -> str:
        from .config import human_time

        return human_time(self.duration)

    @property
    def exists(self) -> bool:
        return Path(self.path).exists()


def _row_to_track(row: sqlite3.Row) -> Track:
    return Track(
        id=row["id"], path=row["path"], title=row["title"], artist=row["artist"],
        album=row["album"], track_no=row["track_no"], duration=row["duration"],
        bitrate=row["bitrate"], size=row["size"], liked=bool(row["liked"]),
        play_count=row["play_count"], added_at=row["added_at"],
    )


def dir_prefix(path: str) -> str:
    """Нормализованный префикс каталога со слэшем на конце:
    ``/a/bc`` → ``/a/bc/`` — чтобы ``startswith`` не ловил каталог-«тёзку»."""
    import os

    normalized = os.path.normpath(os.path.expanduser(path))
    if not normalized.endswith(os.sep):
        normalized += os.sep
    return normalized


class Library:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------- tracks
    def add_track(
        self,
        path: str,
        title: str = "",
        artist: str = "",
        album: str = "",
        track_no: int = 0,
        duration: float = 0.0,
        bitrate: int = 0,
        size: int = 0,
        info_hash: str = "",
        source_name: str = "",
    ) -> Track:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO tracks (path, title, artist, album, track_no, duration, bitrate, size,
                                       info_hash, source_name, added_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                       title=excluded.title, artist=excluded.artist, album=excluded.album,
                       track_no=excluded.track_no, duration=excluded.duration,
                       bitrate=excluded.bitrate, size=excluded.size""",
                (path, title, artist, album, track_no, duration, bitrate, size, info_hash, source_name, now),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM tracks WHERE path=?", (path,)).fetchone()
        return _row_to_track(row)

    def get_track(self, track_id: int) -> Track | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        return _row_to_track(row) if row else None

    def tracks(self, filter_text: str = "", liked_only: bool = False, limit: int = 2000) -> list[Track]:
        query = "SELECT * FROM tracks"
        clauses = []
        params: list = []
        if liked_only:
            clauses.append("liked=1")
        if filter_text:
            clauses.append("(lower(title) LIKE ? OR lower(artist) LIKE ? OR lower(album) LIKE ?)")
            needle = f"%{filter_text.lower()}%"
            params.extend([needle, needle, needle])
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, track_no, title LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_track(r) for r in rows]

    def liked(self) -> list[Track]:
        return self.tracks(liked_only=True)

    def toggle_like(self, track_id: int) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT liked FROM tracks WHERE id=?", (track_id,)).fetchone()
            if row is None:
                return False
            new_value = 0 if row["liked"] else 1
            self._conn.execute("UPDATE tracks SET liked=? WHERE id=?", (new_value, track_id))
            self._conn.commit()
        return bool(new_value)

    def mark_played(self, track_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tracks SET play_count=play_count+1, played_at=? WHERE id=?",
                (time.time(), track_id),
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])

    # ------------------------------------------------------------- перенос путей
    def tracks_under(self, prefix: str) -> list[tuple[int, str]]:
        """(id, нормализованный path) треков, чей файл лежит в ``prefix``.

        Пути в БД могут храниться с ``~`` или без нормализации (рукою правленый
        конфиг) — сравниваем и возвращаем нормализованные формы.
        """
        prefix = dir_prefix(prefix)
        import os

        with self._lock:
            rows = self._conn.execute("SELECT id, path FROM tracks").fetchall()
        out: list[tuple[int, str]] = []
        for r in rows:
            raw = r["path"]
            if not raw:
                continue
            normalized = os.path.normpath(os.path.expanduser(raw))
            if normalized == prefix[:-1] or normalized.startswith(prefix):
                out.append((int(r["id"]), normalized))
        return out

    def update_track_path(self, track_id: int, new_path: str) -> bool:
        """Новое место трека. False — конфликт UNIQUE(path): пути уже есть в БД."""
        with self._lock:
            try:
                self._conn.execute("UPDATE tracks SET path=? WHERE id=?", (new_path, track_id))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # ---------------------------------------------------------------- playlists
    def playlists(self) -> list[tuple[int, str, int]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT p.id, p.name, COUNT(i.track_id) AS n
                   FROM playlists p LEFT JOIN playlist_items i ON i.playlist_id = p.id
                   GROUP BY p.id ORDER BY p.name COLLATE NOCASE"""
            ).fetchall()
        return [(r["id"], r["name"], r["n"]) for r in rows]

    def playlist_names(self) -> list[str]:
        return [name for _id, name, _n in self.playlists()]

    def create_playlist(self, name: str) -> int | None:
        name = name.strip()
        if not name:
            return None
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "INSERT INTO playlists (name, created_at) VALUES (?, ?)", (name, time.time())
                )
                self._conn.commit()
                return int(cursor.lastrowid)
            except sqlite3.IntegrityError:
                row = self._conn.execute("SELECT id FROM playlists WHERE name=?", (name,)).fetchone()
                return int(row["id"]) if row else None

    def delete_playlist(self, name: str) -> None:
        with self._lock:
            row = self._conn.execute("SELECT id FROM playlists WHERE name=?", (name,)).fetchone()
            if row is None:
                return
            self._conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (row["id"],))
            self._conn.execute("DELETE FROM playlists WHERE id=?", (row["id"],))
            self._conn.commit()

    def add_to_playlist(self, name: str, track_id: int) -> bool:
        playlist_id = self.create_playlist(name)
        if playlist_id is None:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(position) FROM playlist_items WHERE playlist_id=?", (playlist_id,)
            ).fetchone()
            position = (row[0] or 0.0) + 1.0 if row else 1.0
            try:
                self._conn.execute(
                    "INSERT INTO playlist_items (playlist_id, track_id, position) VALUES (?,?,?)",
                    (playlist_id, track_id, position),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def remove_from_playlist(self, name: str, track_id: int) -> None:
        with self._lock:
            row = self._conn.execute("SELECT id FROM playlists WHERE name=?", (name,)).fetchone()
            if row is None:
                return
            self._conn.execute(
                "DELETE FROM playlist_items WHERE playlist_id=? AND track_id=?",
                (row["id"], track_id),
            )
            self._conn.commit()

    def playlist_tracks(self, name: str) -> list[Track]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.* FROM playlist_items i
                   JOIN playlists p ON p.id = i.playlist_id
                   JOIN tracks t ON t.id = i.track_id
                   WHERE p.name = ?
                   ORDER BY i.position""",
                (name,),
            ).fetchall()
        return [_row_to_track(r) for r in rows]
