"""D0: instants that carry the local offset in force when they happened.

Two clocks meet here. Transcripts stamp UTC (`Z`), which says when but not what
the wall clock read; hooklog stamps naive local time, which says what the wall
clock read but not when, and the machine's zone has changed under it more than
once. An `Instant` keeps both halves. `OffsetTimeline` recovers the hooklog's
offsets from moments where the same event appears in both clocks.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Union

from . import seam

QUANTUM = timedelta(minutes=15)
MAX_RESIDUAL = timedelta(seconds=120)
# Real zones span UTC-12 to UTC+14; a "pair" implying anything outside that is
# two unrelated records, not a clock reading.
MIN_OFFSET = timedelta(hours=-12)
MAX_OFFSET = timedelta(hours=14)


def host_offset(naive_local: Optional[datetime] = None) -> timedelta:
    """The host zone's offset at that local wall time (DST-aware), or now."""
    dt = naive_local if naive_local is not None else datetime.now()
    try:
        off = dt.astimezone().utcoffset()
    except (OverflowError, OSError, ValueError):
        off = datetime.now().astimezone().utcoffset()
    return off or timedelta(0)


def _host_offset_at_utc(utc: datetime) -> timedelta:
    try:
        return utc.astimezone().utcoffset() or timedelta(0)
    except (OverflowError, OSError, ValueError):
        return host_offset()


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_offset(offset: timedelta) -> str:
    total = int(offset.total_seconds()) // 60
    sign = "-" if total < 0 else "+"
    h, m = divmod(abs(total), 60)
    return f"{sign}{h:02d}:{m:02d}"


@dataclass(frozen=True, order=True)
class Instant:
    utc: datetime
    offset: timedelta

    def __post_init__(self) -> None:
        object.__setattr__(self, "utc", _as_utc(self.utc))

    @classmethod
    def now(cls) -> "Instant":
        utc = datetime.now(timezone.utc)
        return cls(utc, _host_offset_at_utc(utc))

    @classmethod
    def from_local(cls, naive_local: datetime, offset: timedelta) -> "Instant":
        return cls((naive_local - offset).replace(tzinfo=timezone.utc), offset)

    @property
    def local(self) -> datetime:
        return self.utc.astimezone(timezone(self.offset))

    def iso(self) -> str:
        """`2026-10-29T12:58:02Z (07:58 -05:00)`: UTC first, the wall clock beside it."""
        return (
            f"{self.utc.strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"({self.local.strftime('%H:%M')} {_fmt_offset(self.offset)})"
        )

    def __str__(self) -> str:
        return self.iso()


def offset_of(naive_local: datetime, utc: datetime) -> Optional[tuple[timedelta, timedelta]]:
    """(offset rounded to 15 min, |residual|) for one reading of the same moment
    in both clocks, or None when the implied offset is not a real zone."""
    diff = naive_local - _as_utc(utc).replace(tzinfo=None)
    quanta = round(diff / QUANTUM)
    offset = QUANTUM * quanta
    if not (MIN_OFFSET <= offset <= MAX_OFFSET):
        return None
    return offset, abs(diff - offset)


def parse_transcript_ts(
    s: str, offset_hint: Union["Instant", "OffsetTimeline", None] = None
) -> Optional[Instant]:
    """A transcript `Z` timestamp as an Instant. The offset comes from the hint
    (an Instant nearby, or an OffsetTimeline), else the host's zone at that time."""
    dt = seam.parse_ts(s)
    if dt is None:
        return None
    utc = _as_utc(dt)
    if isinstance(offset_hint, OffsetTimeline):
        offset = offset_hint.offset_at_utc(utc)
    elif isinstance(offset_hint, Instant):
        offset = offset_hint.offset
    else:
        offset = _host_offset_at_utc(utc)
    return Instant(utc, offset)


class OffsetTimeline:
    """Local offset as a function of naive local wall time, from paired samples."""

    def __init__(self, samples: Iterable[tuple[datetime, timedelta]] = ()) -> None:
        self.samples: list[tuple[datetime, timedelta]] = sorted(samples, key=lambda s: s[0])
        self._keys = [n for n, _ in self.samples]
        self._offsets = [o for _, o in self.samples]
        by_utc = sorted(((n - o), o) for n, o in self.samples)
        self._utc_keys = [u for u, _ in by_utc]
        self._utc_offsets = [o for _, o in by_utc]

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[datetime, datetime]]) -> "OffsetTimeline":
        """Pairs are (naive local, utc) readings of one moment. Pairs whose
        difference is more than MAX_RESIDUAL off a 15-minute multiple are
        dropped: they are two different moments, not one moment in two clocks."""
        samples = []
        for naive, utc in pairs:
            got = offset_of(naive, utc)
            if got is None or got[1] > MAX_RESIDUAL:
                continue
            samples.append((naive, got[0]))
        return cls(samples)

    def __len__(self) -> int:
        return len(self.samples)

    def __bool__(self) -> bool:
        return bool(self.samples)

    @staticmethod
    def _nearest(keys: list, values: list, x) -> Optional[timedelta]:
        if not keys:
            return None
        i = bisect.bisect_left(keys, x)
        if i == 0:
            return values[0]
        if i == len(keys):
            return values[-1]
        before, after = keys[i - 1], keys[i]
        return values[i - 1] if (x - before) <= (after - x) else values[i]

    def offset_at(self, naive_local: datetime) -> timedelta:
        """The offset of the nearest sample; the host zone's when there are none."""
        got = self._nearest(self._keys, self._offsets, naive_local)
        return got if got is not None else host_offset(naive_local)

    def offset_at_utc(self, utc: datetime) -> timedelta:
        naive_utc = _as_utc(utc).replace(tzinfo=None)
        got = self._nearest(self._utc_keys, self._utc_offsets, naive_utc)
        return got if got is not None else _host_offset_at_utc(_as_utc(utc))

    def instant(self, naive_local: datetime) -> Instant:
        return Instant.from_local(naive_local, self.offset_at(naive_local))
