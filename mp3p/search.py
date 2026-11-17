"""Track search over public torrent indexes.

Two sources are queried and merged, because each one alone is unreliable: apibay
(The Pirate Bay API) answers fast but throttles, solidtorrents has a bigger audio
index. Every request goes through the proxy resolved by ``Config.search_proxy``,
which defaults to the proxy exported in the shell that launched the player.
"""

from __future__ import annotations

import html
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote_plus

from .config import Config
from .net import Proxy, http_request
from .torrent import build_magnet

APIBAY = "https://apibay.org/q.php"
SOLID = "https://solidtorrents.to/api/v1/search"

APIBAY_CATEGORIES = {
    "100": "Audio",
    "101": "Music",
    "102": "Audible",
    "103": "Sound",
    "104": "FLAC",
    "199": "Audio*",
}


class SearchError(OSError):
    pass


@dataclass
class SearchResult:
    name: str
    info_hash: str
    size: int
    seeders: int
    leechers: int
    added: int
    category: str
    num_files: int
    source: str = "apibay"

    @property
    def size_text(self) -> str:
        from .config import human_size

        return human_size(self.size)

    @property
    def category_text(self) -> str:
        if self.source == "solid":
            return {"1": "Music", "2": "FLAC", "3": "Audible"}.get(self.category, "Audio")
        return APIBAY_CATEGORIES.get(self.category, "Audio")

    @property
    def age_text(self) -> str:
        if not self.added:
            return "-"
        days = max(0, int((time.time() - self.added) / 86400))
        if days < 1:
            return "today"
        if days < 30:
            return f"{days}d"
        if days < 365:
            return f"{days // 30}mo"
        return f"{days // 365}y"

    def magnet(self, trackers: list[str]) -> str:
        return build_magnet(self.info_hash, self.name, trackers)


def _proxy_for(config: Config) -> Proxy | None:
    raw = config.search_effective_proxy()
    if not raw:
        return None
    try:
        return Proxy.parse(raw)
    except Exception as exc:
        raise SearchError(f"bad proxy: {exc}") from exc


def _get(url: str, proxy: Proxy | None, errors: list[str], label: str, attempts: int = 2, timeout: float = 14.0) -> bytes | None:
    for attempt in range(attempts):
        try:
            response = http_request(
                url,
                proxy=proxy,
                timeout=timeout + attempt * 6,
                headers={"Accept": "application/json, text/plain, */*", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
            )
        except OSError as exc:
            errors.append(f"{label}: {exc}")
            time.sleep(0.4 + attempt)
            continue
        if response.status == 200:
            return response.body
        errors.append(f"{label}: HTTP {response.status}")
    return None


def search_apibay(config: Config, query: str, limit: int, proxy: Proxy | None, errors: list[str]) -> list[SearchResult]:
    """One source; ``errors`` collects its failures so the caller can tell
    'nothing found' apart from 'the index or proxy is unreachable'."""
    out: list[SearchResult] = []
    for category in ("100", "0"):
        body = _get(f"{APIBAY}?q={quote_plus(query)}&cat={category}", proxy, errors, "apibay")
        if body is None:
            continue
        try:
            payload = _json(body)
        except ValueError:
            errors.append("apibay: not JSON")
            continue
        if not isinstance(payload, list):
            continue
        for entry in payload:
            result = _apibay_entry(entry)
            if result is not None:
                out.append(result)
        if out:
            break
    return out[:limit]


def _json(body: bytes):
    import json

    return json.loads(body.decode("utf-8", "replace"))


def search_solid(config: Config, query: str, limit: int, proxy: Proxy | None, errors: list[str]) -> list[SearchResult]:
    url = f"{SOLID}?q={quote_plus(query)}&type=1&sort=seeders&limit={min(50, max(20, limit))}"
    body = _get(url, proxy, errors, "solidtorrents")
    if body is None:
        return []
    try:
        payload = _json(body)
    except ValueError:
        errors.append("solidtorrents: not JSON")
        return []
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    out: list[SearchResult] = []
    for entry in results:
        result = _solid_entry(entry)
        if result is not None:
            out.append(result)
    return out[:limit]


def _apibay_entry(entry) -> SearchResult | None:
    if not isinstance(entry, dict):
        return None
    try:
        info_hash = str(entry.get("info_hash", "")).strip()
        if len(info_hash) != 40 or set(info_hash) == {"0"}:
            return None
        name = html.unescape(str(entry.get("name") or "")).strip()
        size = int(entry.get("size") or 0)
        if not name or size <= 0:
            return None
        return SearchResult(
            name=name,
            info_hash=info_hash.upper(),
            size=size,
            seeders=int(entry.get("seeders") or 0),
            leechers=int(entry.get("leechers") or 0),
            added=int(entry.get("added") or 0),
            category=str(entry.get("category") or ""),
            num_files=int(entry.get("num_files") or 0),
            source="apibay",
        )
    except (TypeError, ValueError):
        return None


def _solid_entry(entry) -> SearchResult | None:
    if not isinstance(entry, dict):
        return None
    try:
        info_hash = str(entry.get("infohash", "")).strip()
        if len(info_hash) != 40:
            return None
        name = html.unescape(str(entry.get("title") or "")).strip()
        size = int(entry.get("size") or 0)
        if not name or size <= 0:
            return None
        added = 0
        created = entry.get("createdAt")
        if isinstance(created, str) and created:
            try:
                added = int(time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")) - 3 * 3600)
            except ValueError:
                added = 0
        return SearchResult(
            name=name,
            info_hash=info_hash.upper(),
            size=size,
            seeders=int(entry.get("seeders") or 0),
            leechers=int(entry.get("leechers") or 0),
            added=added,
            category=str(entry.get("subCategory") or entry.get("category") or ""),
            num_files=0,
            source="solid",
        )
    except (TypeError, ValueError):
        return None


def search_tracks(config: Config, query: str, limit: int = 60, sort: str = "seeders") -> list[SearchResult]:
    """Query the configured indexes concurrently and merge the results."""
    query = query.strip()
    if not query:
        return []
    proxy = _proxy_for(config)
    errors: list[str] = []
    wanted = config.search_source.lower()

    jobs = []
    if wanted in ("all", "apibay", ""):
        jobs.append(("apibay", search_apibay))
    if wanted in ("all", "solid", "solidtorrents"):
        jobs.append(("solid", search_solid))

    collected: list[SearchResult] = []
    source_errors: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=len(jobs) or 1, thread_name_prefix="search") as pool:
        futures = {}
        for name, fn in jobs:
            bucket: list[str] = []
            source_errors[name] = bucket
            futures[pool.submit(fn, config, query, limit, proxy, bucket)] = name
        for future, name in futures.items():
            try:
                collected.extend(future.result())
            except Exception as exc:
                source_errors[name].append(str(exc))

    if not collected:
        # only a total failure of every source is an error; an empty index is not
        if all(source_errors.values()):
            merged = "; ".join(dict.fromkeys(e for bucket in source_errors.values() for e in bucket))
            raise SearchError(merged[:220])
        return []

    seen: set[str] = set()
    unique: list[SearchResult] = []
    for result in sorted(collected, key=lambda r: -r.seeders):
        if result.info_hash in seen:
            continue
        seen.add(result.info_hash)
        unique.append(result)

    if sort == "size":
        unique.sort(key=lambda r: -r.size)
    elif sort == "new":
        unique.sort(key=lambda r: -r.added)
    else:
        unique.sort(key=lambda r: (-r.seeders, -r.size))
    return unique[:limit]
