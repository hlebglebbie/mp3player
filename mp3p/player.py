"""Audio playback engine.

Uses ``ffplay`` as a dumb decoder+sink and does the transport itself: pause is
SIGSTOP/SIGCONT, seek and volume changes restart the decoder at the current
position. That keeps the terminal free for curses (ffplay gets /dev/null on
stdin and no window at all).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

FFPLAY = shutil.which("ffplay") or "ffplay"

IDLE = "idle"
PLAYING = "playing"
PAUSED = "paused"
ERROR = "error"


@dataclass
class Playable:
    """Something the transport can play — a library track or a loose file."""

    path: str
    title: str = ""
    artist: str = ""
    duration: float = 0.0
    track_id: int | None = None
    key: str = ""

    @property
    def label(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or Path(self.path).name

    @classmethod
    def from_track(cls, track) -> "Playable":
        return cls(
            path=track.path,
            title=track.title,
            artist=track.artist,
            duration=track.duration,
            track_id=track.id,
            key=f"track:{track.id}",
        )

    def as_dict(self) -> dict:
        return {"path": self.path, "title": self.title, "artist": self.artist, "duration": self.duration}


@dataclass
class PlayerState:
    status: str = IDLE
    volume: int = 80
    position: float = 0.0
    duration: float = 0.0
    index: int = -1
    repeat: str = "all"  # off | all | one
    shuffle: bool = False
    message: str = ""
    queue_len: int = 0

    @property
    def fraction(self) -> float:
        if self.duration <= 0:
            return 0.0
        return max(0.0, min(1.0, self.position / self.duration))


class Player:
    def __init__(self, volume: int = 80):
        self.status = IDLE
        self.volume = max(0, min(100, volume))
        self.queue: list[Playable] = []
        self.index = -1
        self.repeat = "all"
        self.shuffle = False
        self.message = ""
        self.current: Playable | None = None
        self.events: list[str] = []

        self._proc: subprocess.Popen | None = None
        self._base = 0.0
        self._started_at = 0.0
        self._paused_total = 0.0
        self._paused_at = 0.0
        self._duration = 0.0
        self._restarting = False
        self._history: list[int] = []
        self._output = os.environ.get("MP3P_AUDIO", "")

    # ------------------------------------------------------------------ helpers
    def _spawn(self, offset: float) -> None:
        self._kill()
        if self.current is None:
            return
        path = self.current.path
        if not Path(path).exists():
            self.message = f"file missing: {Path(path).name}"
            self.status = ERROR
            self.current = None
            return
        cmd = [
            FFPLAY, "-nodisp", "-autoexit", "-loglevel", "error", "-vn",
            "-volume", str(self.volume),
        ]
        if self._output:
            cmd += ["-f", self._output]
        cmd += ["-ss", f"{max(0.0, offset):.3f}", "-i", path]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            self.message = "ffplay not found"
            self.status = ERROR
            self._proc = None
            return
        self._base = max(0.0, offset)
        self._started_at = time.monotonic()
        self._paused_total = 0.0
        self._paused_at = 0.0
        self._restarting = False
        self.status = PLAYING
        self.message = ""

    def _kill(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGCONT)
                except OSError:
                    pass
                proc.terminate()
                try:
                    proc.wait(timeout=0.4)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except OSError:
                        proc.kill()
        except OSError:
            pass

    def _signal(self, sig) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except OSError:
            try:
                proc.send_signal(sig)
            except OSError:
                pass

    # ------------------------------------------------------------------ transport
    def play_tracks(self, tracks: list[Playable], start: int = 0) -> None:
        if not tracks:
            return
        self.queue = list(tracks)
        self.index = max(0, min(start, len(self.queue) - 1))
        self._start_current()

    def _start_current(self) -> None:
        if not (0 <= self.index < len(self.queue)):
            self.current = None
            self.status = IDLE
            self._kill()
            return
        item = self.queue[self.index]
        self.current = item
        self._duration = item.duration or self._probe_duration(item.path)
        self._history.append(self.index)
        self._spawn(0.0)
        self.events.append("started")

    def _probe_duration(self, path: str) -> float:
        try:
            from .media import probe

            return float(probe(path).get("duration") or 0.0)
        except Exception:
            return 0.0

    def toggle(self) -> None:
        if self.status == PLAYING:
            self.pause()
        elif self.status == PAUSED:
            self.resume()
        elif self.current is not None:
            self._spawn(self.position)
        elif self.queue:
            self.index = max(0, self.index)
            self._start_current()

    def pause(self) -> None:
        if self.status != PLAYING:
            return
        self._signal(signal.SIGSTOP)
        self._paused_at = time.monotonic()
        self.status = PAUSED

    def resume(self) -> None:
        if self.status != PAUSED:
            return
        if self._paused_at:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = 0.0
        self._signal(signal.SIGCONT)
        self.status = PLAYING

    def stop(self) -> None:
        self._kill()
        self.status = IDLE
        self.current = None
        self._base = 0.0
        self._duration = 0.0

    def seek(self, target: float) -> None:
        if self.current is None:
            return
        duration = self._duration or 0.0
        target = max(0.0, target)
        if duration:
            target = min(target, max(0.0, duration - 0.5))
        was_paused = self.status == PAUSED
        self._spawn(target)
        if was_paused:
            self.pause()

    def seek_relative(self, delta: float) -> None:
        self.seek(self.position + delta)

    def set_volume(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        if value == self.volume:
            return
        self.volume = value
        if self.status in (PLAYING, PAUSED):
            was_paused = self.status == PAUSED
            position = self.position
            self._spawn(position)
            if was_paused:
                self.pause()

    def next(self, auto: bool = False) -> None:
        if not self.queue:
            return
        if self.repeat == "one" and auto:
            self._start_current()
            return
        if self.shuffle:
            import random

            candidates = [i for i in range(len(self.queue)) if i != self.index] or [self.index]
            self.index = random.choice(candidates)
        else:
            self.index += 1
            if self.index >= len(self.queue):
                if self.repeat == "all":
                    self.index = 0
                else:
                    self.stop()
                    return
        self._start_current()

    def previous(self) -> None:
        if not self.queue:
            return
        if self.position > 4.0:
            self.seek(0.0)
            return
        self.index = (self.index - 1) % len(self.queue)
        self._start_current()

    def cycle_repeat(self) -> str:
        order = ["all", "one", "off"]
        self.repeat = order[(order.index(self.repeat) + 1) % len(order)]
        return self.repeat

    def toggle_shuffle(self) -> bool:
        self.shuffle = not self.shuffle
        return self.shuffle

    # -------------------------------------------------------------------- polling
    @property
    def position(self) -> float:
        if self.status == IDLE or self.current is None:
            return 0.0
        if self.status == PAUSED:
            return max(0.0, min(self._base + (self._paused_at - self._started_at - self._paused_total), self._duration or 1e9))
        elapsed = time.monotonic() - self._started_at - self._paused_total
        pos = self._base + elapsed
        if self._duration:
            pos = min(pos, self._duration)
        return max(0.0, pos)

    @property
    def duration(self) -> float:
        return self._duration

    def poll(self) -> list[str]:
        """Advance state; returns events produced since the last call."""
        events, self.events = self.events, []
        proc = self._proc
        if proc is not None and self.status in (PLAYING, PAUSED):
            code = proc.poll()
            if code is not None and not self._restarting:
                self._proc = None
                if code == 0:
                    events.append("finished")
                    self._advance_after_finish()
                else:
                    self.status = ERROR
                    self.message = f"ffplay exited with code {code}"
                    events.append("error")
        if self.current is not None and self._duration and self.status == PLAYING:
            if self.position >= self._duration - 0.05 and self._proc is None:
                pass
        return events

    def _advance_after_finish(self) -> None:
        if self.repeat == "one":
            self._start_current()
            return
        if not self.queue:
            self.status = IDLE
            return
        if self.shuffle:
            self.next(auto=True)
            return
        next_index = self.index + 1
        if next_index < len(self.queue):
            self.index = next_index
            self._start_current()
        elif self.repeat == "all":
            self.index = 0
            self._start_current()
        else:
            self.status = IDLE
            self.index = len(self.queue) - 1
            self._base = self._duration
            self.current = self.queue[self.index] if self.queue else None

    def state(self) -> PlayerState:
        return PlayerState(
            status=self.status,
            volume=self.volume,
            position=self.position,
            duration=self._duration,
            index=self.index,
            repeat=self.repeat,
            shuffle=self.shuffle,
            message=self.message,
            queue_len=len(self.queue),
        )

    def shutdown(self) -> None:
        self._kill()
