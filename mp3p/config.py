"""Configuration and filesystem layout for the terminal mp3 player."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

APP_NAME = "mp3player"

HOME = Path(os.path.expanduser("~"))
CONFIG_DIR = HOME / ".config" / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_DIR = HOME / ".local" / "share" / APP_NAME

DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",  # only used if proxy disabled
    "http://tracker.opentrackr.org:1337/announce",
    "https://tracker.tamersunion.org:443/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "https://tracker2.ctix.cn:443/announce",
    "https://tracker.torrent.eu.org:451/announce",
    "http://open.tracker.cl:1337/announce",
    "https://tracker.gbitt.info:443/announce",
    "http://bt1.archive.org:6969/announce",
    "https://tr.burnabyte.com:443/announce",
    "https://tracker.zhuqling.eu.org:443/announce",
]

MUSIC_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aiff", ".ape", ".alac",
}


@dataclass
class Config:
    # proxy: "auto" picks https_proxy/http_proxy/all_proxy from the environment,
    # any explicit URL ("http://u:p@host:port", "socks5h://host:port") wins,
    # empty string disables proxying.
    proxy: str = "auto"
    download_dir: str = str(STATE_DIR / "torrents")
    library_dir: str = str(HOME / "Music" / "mp3player")
    db_path: str = str(STATE_DIR / "library.db")

    mp3_bitrate: str = "320k"
    keep_torrent_data: bool = False
    max_peers: int = 40
    search_limit: int = 60
    search_source: str = "all"
    search_proxy: str = "auto"
    audio_volume: int = 80
    trackers: list[str] = field(default_factory=list)
    extra_search_trackers: list[str] = field(default_factory=lambda: list(DEFAULT_TRACKERS[:6]))
    # SoulSeek: аккаунт генерируется один раз и сохраняется — это стабильная
    # учётка в сети, а не рандом на каждый запуск (случайные имена запрещены
    # правилами сервера, см. slsk.py)
    slsk_enabled: bool = True
    slsk_user: str = ""
    slsk_pass: str = ""

    def resolve_trackers(self) -> list[str]:
        """Trackers usable for peer discovery. UDP needs raw packets, so it is
        dropped whenever traffic must go through a proxy."""
        base = list(self.trackers) if self.trackers else list(DEFAULT_TRACKERS)
        if self.effective_proxy() is not None:
            base = [t for t in base if not t.lower().startswith("udp://")]
        return base

    def effective_proxy(self) -> str | None:
        return resolve_proxy(self.proxy)

    def search_effective_proxy(self) -> str | None:
        return resolve_proxy(self.search_proxy)

    def ensure_dirs(self) -> None:
        Path(self.download_dir).mkdir(parents=True, exist_ok=True)
        Path(self.library_dir).mkdir(parents=True, exist_ok=True)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)


PROXY_ENV_KEYS = (
    "all_proxy", "ALL_PROXY",
    "https_proxy", "HTTPS_PROXY",
    "http_proxy", "HTTP_PROXY",
    "socks_proxy", "SOCKS_PROXY",
)


def proxy_from_env() -> str | None:
    for key in PROXY_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def resolve_proxy(value: str | None) -> str | None:
    """Turn a config value into a concrete proxy URL or None."""
    if value is None:
        return proxy_from_env()
    value = value.strip()
    if value.lower() in ("", "none", "off", "direct", "false"):
        return None
    if value.lower() == "auto":
        return proxy_from_env()
    return value


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    known = {f for f in Config.__dataclass_fields__}
    clean = {k: v for k, v in data.items() if k in known}
    cfg = Config(**clean)
    cfg.ensure_dirs()
    return cfg


def save_config(cfg: Config, path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def human_size(num: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(num) < 1024 or unit == "T":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}T"


def human_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def human_speed(bps: float) -> str:
    return human_size(bps) + "/s"
