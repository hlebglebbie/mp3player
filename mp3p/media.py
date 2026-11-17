"""Audio pipeline: recognise audio inside a torrent, probe tags, transcode to mp3.

Everything a torrent yields lands in the library as a real MP3 file with ID3 tags
so the player, the likes and the playlists all refer to a stable local path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

COVER_NAMES = ("cover.jpg", "cover.jpeg", "folder.jpg", "folder.jpeg", "front.jpg", "album.jpg", "albumart.jpg")
COVER_EXTS = (".jpg", ".jpeg", ".png")

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")


class MediaError(RuntimeError):
    pass


@dataclass
class TrackInfo:
    path: str
    title: str
    artist: str
    album: str
    track_no: int
    duration: float
    bitrate: int
    size: int


def _run(cmd: list[str], timeout: float = 180.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise MediaError(f"{cmd[0]} not found in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{cmd[0]} timed out") from exc


def tools_available() -> tuple[bool, str]:
    for tool, name in ((FFMPEG, "ffmpeg"), (FFPROBE, "ffprobe")):
        if not shutil.which(tool) and not Path(tool).exists():
            return False, f"{name} missing"
    return True, ""


def safe_name(text: str, maxlen: int = 120) -> str:
    cleaned = _UNSAFE.sub("_", (text or "").strip()).strip(" .")
    cleaned = _SPACES.sub(" ", cleaned)
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:maxlen].strip(" .") or "untitled"


def probe(path: str | Path) -> dict:
    """Return ffprobe metadata for the first audio stream plus container tags."""
    path = str(path)
    result = _run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        timeout=60,
    )
    if result.returncode != 0:
        raise MediaError(result.stderr.decode("utf-8", "replace").strip()[:200] or "ffprobe failed")
    try:
        payload = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise MediaError("ffprobe produced no JSON") from exc
    audio_stream = None
    for stream in payload.get("streams", []):
        if stream.get("codec_type") == "audio":
            audio_stream = stream
            break
    fmt = payload.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    if audio_stream:
        for key, value in (audio_stream.get("tags") or {}).items():
            tags.setdefault(key.lower(), value)
    return {
        "duration": float(fmt.get("duration") or (audio_stream or {}).get("duration") or 0.0),
        "bitrate": int(float(fmt.get("bit_rate") or 0)),
        "format": fmt.get("format_name", ""),
        "codec": (audio_stream or {}).get("codec_name", ""),
        "tags": tags,
    }


def derive_meta(path: Path, hint_artist: str = "", hint_album: str = "") -> tuple[str, str, str, int]:
    """Best-effort (artist, title, album, track_no) from tags, path or filename."""
    name = path.stem
    track_no = 0
    match = re.match(r"\s*(\d{1,3})\s*[-._)]\s*(.+)", name)
    title = name
    if match:
        track_no = int(match.group(1))
        title = match.group(2).strip()
    artist = hint_artist
    album = hint_album

    pieces = [p for p in re.split(r"\s+-\s+", title) if p]
    if len(pieces) >= 2 and not artist:
        artist = pieces[0].strip()
        title = " - ".join(pieces[1:]).strip()

    parent = path.parent.name
    if not album:
        parent_clean = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", parent)
        parent_clean = re.sub(r"\b(19|20)\d{2}\b", " ", parent_clean)
        album = _SPACES.sub(" ", parent_clean).strip(" -_") or parent
    if not artist:
        artist_parts = [p for p in re.split(r"\s+-\s+", parent) if p]
        artist = artist_parts[0].strip() if artist_parts else "Unknown artist"
    return artist, title, album, track_no


def find_cover(directory: Path) -> Path | None:
    for candidate in COVER_NAMES:
        for entry in directory.glob("*"):
            if entry.is_file() and entry.name.lower() == candidate:
                return entry
    for entry in directory.glob("*"):
        if entry.is_file() and entry.suffix.lower() in COVER_EXTS:
            return entry
    return None


def library_target(cfg: Config, artist: str, album: str, track_no: int, title: str) -> Path:
    folder = Path(cfg.library_dir) / safe_name(artist, 60) / safe_name(album, 60)
    prefix = f"{track_no:02d} - " if track_no else ""
    return folder / f"{prefix}{safe_name(title, 80)}.mp3"


def transcode_to_mp3(
    source: str | Path,
    cfg: Config,
    hint_artist: str = "",
    hint_album: str = "",
    on_log=None,
) -> TrackInfo:
    """Decode any torrent audio file into ``library_dir`` as a tagged mp3."""
    source = Path(source)
    if not source.exists():
        raise MediaError(f"missing file: {source}")
    if source.suffix.lower() == ".mp3":
        try:
            info = probe(source)
        except MediaError:
            info = {}
    else:
        info = probe(source)

    tags = info.get("tags", {}) if isinstance(info, dict) else {}
    artist = (tags.get("artist") or tags.get("albumartist") or "").strip() or hint_artist
    album = (tags.get("album") or "").strip() or hint_album
    title = (tags.get("title") or "").strip()
    track_no = 0
    for key in ("track", "tracknumber"):
        raw = tags.get(key)
        if raw:
            try:
                track_no = int(str(raw).split("/")[0])
                break
            except ValueError:
                pass

    derived_artist, derived_title, derived_album, derived_no = derive_meta(source, hint_artist, hint_album)
    artist = artist or derived_artist
    album = album or derived_album
    title = title or derived_title
    track_no = track_no or derived_no

    target = library_target(cfg, artist, album, track_no, title)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 1024:
        duration = float(info.get("duration") or 0.0)
        return TrackInfo(
            path=str(target), title=title, artist=artist, album=album,
            track_no=track_no, duration=duration,
            bitrate=int(info.get("bitrate") or 0), size=target.stat().st_size,
        )

    cover = find_cover(source.parent)
    error = _encode_with_ffmpeg(source, target, cfg, artist, album, title, track_no, cover)
    if error is not None and cover is not None:
        # torrent cover art is frequently broken; retry as audio-only
        error = _encode_with_ffmpeg(source, target, cfg, artist, album, title, track_no, None)
    if error is not None:
        raise MediaError(error)

    duration = 0.0
    bitrate = int(cfg.mp3_bitrate.rstrip("k")) * 1000 if cfg.mp3_bitrate.rstrip("k").isdigit() else 0
    try:
        final = probe(target)
        duration = float(final.get("duration") or 0.0)
        bitrate = int(final.get("bitrate") or bitrate)
    except MediaError:
        pass
    return TrackInfo(
        path=str(target), title=title, artist=artist, album=album,
        track_no=track_no, duration=duration, bitrate=bitrate, size=target.stat().st_size,
    )


def _encode_with_ffmpeg(
    source: Path,
    target: Path,
    cfg: Config,
    artist: str,
    album: str,
    title: str,
    track_no: int,
    cover: Path | None,
) -> str | None:
    """Run the transcode. Returns None on success, otherwise the ffmpeg error."""
    cmd = [FFMPEG, "-v", "error", "-y", "-i", str(source)]
    if cover is not None:
        cmd += ["-i", str(cover), "-map", "0:a:0", "-map", "1:v:0",
                "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
    else:
        cmd += ["-vn", "-map", "0:a:0"]
    cmd += ["-id3v2_version", "3"]
    if source.suffix.lower() == ".mp3":
        cmd += ["-c:a", "copy"]  # already mp3: remux with clean tags, no second lossy pass
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", cfg.mp3_bitrate]
    cmd += [
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", f"album={album}",
        "-metadata", "genre=Torrent",
        "-metadata", f"comment=mp3player:{source.name}",
    ]
    if track_no:
        cmd += ["-metadata", f"track={track_no}"]
    cmd.append(str(target))

    result = _run(cmd, timeout=300)
    if result.returncode != 0 or not target.exists() or target.stat().st_size < 1024:
        message = result.stderr.decode("utf-8", "replace").strip()[:200]
        return f"ffmpeg failed: {message or 'unknown error'}"
    return None
