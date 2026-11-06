from datetime import datetime, timedelta, timezone

from workstream.clock import Instant, OffsetTimeline, host_offset, offset_of, parse_transcript_ts

H = timedelta(hours=1)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_instant_local_and_iso():
    i = Instant(utc(2026, 10, 29, 12, 58, 2), -5 * H)
    assert i.local.hour == 7
    assert i.local.utcoffset() == -5 * H
    assert i.iso() == "2026-10-29T12:58:02Z (07:58 -05:00)"


def test_instant_normalises_naive_and_foreign_zones_to_utc():
    a = Instant(datetime(2026, 10, 29, 12, 0), 0 * H)
    b = Instant(datetime(2026, 10, 29, 5, 0, tzinfo=timezone(-7 * H)), -7 * H)
    assert a.utc.tzinfo is timezone.utc
    assert a.utc == b.utc


def test_from_local_round_trips():
    i = Instant.from_local(datetime(2026, 10, 19, 9, 30), -7 * H)
    assert i.utc == utc(2026, 10, 19, 16, 30)
    assert i.local.replace(tzinfo=None) == datetime(2026, 10, 19, 9, 30)


def test_offset_of_rounds_to_quarter_hour_and_rejects_impossible_zones():
    off, resid = offset_of(datetime(2026, 10, 29, 7, 58, 50), utc(2026, 10, 29, 12, 58, 2))
    assert off == -5 * H
    assert resid == timedelta(seconds=48)
    # India-style half-hour zone survives the rounding.
    off, _ = offset_of(datetime(2026, 10, 29, 18, 30, 1), utc(2026, 10, 29, 13, 0, 0))
    assert off == timedelta(hours=5, minutes=30)
    assert offset_of(datetime(2026, 10, 30, 12, 0), utc(2026, 10, 29, 12, 0)) is None


def test_timeline_infers_offsets_across_zone_changes():
    pairs = [
        # UTC-7, a few seconds of skew between hook and transcript
        (datetime(2026, 10, 19, 9, 0, 3), utc(2026, 10, 19, 16, 0, 0)),
        # UTC-5 (travel)
        (datetime(2026, 10, 25, 9, 0, 0), utc(2026, 10, 25, 14, 0, 40)),
        # UTC-7 again
        (datetime(2026, 10, 28, 9, 0, 0), utc(2026, 10, 28, 16, 1, 30)),
        # UTC-8 after DST ended
        (datetime(2026, 11, 2, 9, 0, 0), utc(2026, 11, 2, 17, 0, 0)),
        # residual of 5 min: two different moments, dropped
        (datetime(2026, 10, 26, 9, 0, 0), utc(2026, 10, 26, 13, 55, 0)),
    ]
    tl = OffsetTimeline.from_pairs(pairs)
    assert [o for _, o in tl.samples] == [-7 * H, -5 * H, -7 * H, -8 * H]
    assert tl.offset_at(datetime(2026, 10, 25, 20, 0)) == -5 * H
    assert tl.offset_at(datetime(2026, 11, 1, 23, 0)) == -8 * H
    inst = tl.instant(datetime(2026, 10, 25, 10, 0))
    assert inst.utc == utc(2026, 10, 25, 15, 0)
    assert inst.offset == -5 * H


def test_offset_at_takes_nearest_sample_and_clamps_at_ends():
    tl = OffsetTimeline(
        [(datetime(2026, 10, 20, 0, 0), -7 * H), (datetime(2026, 10, 22, 0, 0), -5 * H)]
    )
    assert tl.offset_at(datetime(2026, 10, 20, 23, 59)) == -7 * H
    assert tl.offset_at(datetime(2026, 10, 21, 0, 1)) == -5 * H
    assert tl.offset_at(datetime(2026, 1, 1)) == -7 * H
    assert tl.offset_at(datetime(2027, 1, 1)) == -5 * H
    assert tl.offset_at_utc(utc(2026, 10, 20, 7, 0)) == -7 * H


def test_empty_timeline_falls_back_to_host_zone():
    tl = OffsetTimeline()
    assert not tl
    naive = datetime(2026, 7, 1, 12, 0)
    assert tl.offset_at(naive) == host_offset(naive)
    assert tl.instant(naive).offset == naive.astimezone().utcoffset()


def test_parse_transcript_ts_uses_hint_then_host():
    tl = OffsetTimeline([(datetime(2026, 10, 29, 7, 0), -5 * H)])
    a = parse_transcript_ts("2026-10-29T12:59:37.123Z", tl)
    assert a.utc == utc(2026, 10, 29, 12, 59, 37, 123000)
    assert a.offset == -5 * H
    b = parse_transcript_ts("2026-10-29T12:59:37Z", Instant(utc(2026, 1, 1), 3 * H))
    assert b.offset == 3 * H
    c = parse_transcript_ts("2026-10-29T12:59:37Z")
    assert c.offset == c.utc.astimezone().utcoffset()
    assert parse_transcript_ts("not a time") is None
