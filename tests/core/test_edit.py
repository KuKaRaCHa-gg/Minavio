from pathlib import Path

import pytest

from minavio.config.schema import ChannelProfile
from minavio.core.edit import plan_timeline
from minavio.core.models import Highlight, TranscriptSegment, Word

SRC = Path("a.mp4")


def hl(start: float, end: float, score: float = 0.5, label: str = "kill", src: Path = SRC):
    return Highlight(source=src, start=start, end=end, score=score, label=label, detector="t")


def prof(profile: ChannelProfile, **editing) -> ChannelProfile:
    return profile.model_copy(update={"editing": profile.editing.model_copy(update=editing)})


def test_rolls_and_bounds(profile):
    p = prof(
        profile, pre_roll_s=2, post_roll_s=1, min_clip_s=2, max_clip_s=8, target_duration_s=100
    )
    tl = plan_timeline([hl(10, 11)], p)
    c = tl.clips[0]
    assert (c.start, c.end) == (8, 12)
    tl = plan_timeline([hl(0.5, 1)], p)  # pre-roll borné à 0
    assert tl.clips[0].start == 0
    tl = plan_timeline([hl(10, 40)], p)  # trop long -> max_clip_s
    assert tl.clips[0].duration == pytest.approx(8)


def test_min_duration_extends(profile):
    p = prof(profile, pre_roll_s=0, post_roll_s=0, min_clip_s=3, target_duration_s=100)
    assert plan_timeline([hl(5, 5.5)], p).clips[0].duration == pytest.approx(3)


def test_target_duration_best_scores_then_chronological(profile):
    p = prof(profile, pre_roll_s=0, post_roll_s=0, min_clip_s=2, max_clip_s=5, target_duration_s=10)
    hs = [
        hl(100, 104, 0.2, "low"),
        hl(10, 14, 0.9, "best"),
        hl(50, 54, 0.8, "good"),
        hl(70, 74, 0.7, "ok"),
    ]
    tl = plan_timeline(hs, p)
    assert [c.highlight_label for c in tl.clips] == ["best", "good"]  # chronologique
    assert tl.duration <= 10


def test_overlapping_highlights_merge(profile):
    p = prof(
        profile, pre_roll_s=0, post_roll_s=0, min_clip_s=1, max_clip_s=20, target_duration_s=100
    )
    tl = plan_timeline([hl(10, 14, 0.4, "a"), hl(12, 16, 0.9, "b")], p)
    assert len(tl.clips) == 1
    assert (tl.clips[0].start, tl.clips[0].end, tl.clips[0].highlight_label) == (10, 16, "b")


def test_beat_snap(profile):
    p = prof(
        profile,
        pre_roll_s=0,
        post_roll_s=0,
        min_clip_s=2,
        max_clip_s=10,
        target_duration_s=100,
        pacing="dynamic",
    )
    p = p.model_copy(update={"audio": p.audio.model_copy(update={"beat_sync": True})})
    beats = [i * 0.5 for i in range(40)]
    tl = plan_timeline([hl(10, 14.12), hl(30, 34.4)], p, beats=beats, music=Path("m.wav"))
    ends = []
    t = 0.0
    for c in tl.clips:
        t += c.duration
        ends.append(t)
    assert ends[0] == pytest.approx(4.0)  # 4.12 -> 4.0 (dans la tolérance)
    assert all(min(abs(e - b) for b in beats) < 1e-6 for e in ends[:1])
    assert tl.music == Path("m.wav")
    # hors tolérance : pas de snap
    p2 = prof(
        profile,
        pre_roll_s=0,
        post_roll_s=0,
        min_clip_s=2,
        max_clip_s=10,
        target_duration_s=100,
        pacing="hyper",
    )
    tl2 = plan_timeline([hl(10, 14.3)], p2, beats=[0.0, 4.0, 5.0])
    assert tl2.clips[0].duration == pytest.approx(4.3)


def test_captions_remapped_with_words(profile):
    p = prof(
        profile, pre_roll_s=0, post_roll_s=0, min_clip_s=1, max_clip_s=20, target_duration_s=100
    )
    words = [
        Word(start=10.0, end=10.5, text="un"),
        Word(start=10.5, end=11.0, text="deux"),
        Word(start=12.0, end=12.5, text="trois"),
    ]
    seg = TranscriptSegment(start=10.0, end=12.5, text="un deux trois", words=words)
    outside = TranscriptSegment(start=50, end=52, text="dehors")
    # clip [10.5, 12.5] -> "un" exclu, "deux" tronqué en début de clip
    tl = plan_timeline([hl(10.5, 12.5)], p, transcript=[seg, outside])
    assert len(tl.captions) == 1
    cap = tl.captions[0]
    assert [w.text for w in cap.words] == ["deux", "trois"]
    assert cap.words[0].start == pytest.approx(0.0)
    assert cap.words[1].start == pytest.approx(1.5)
    assert cap.start == pytest.approx(0.0) and cap.end == pytest.approx(2.0)
    assert cap.text == "deux trois"


def test_captions_offset_across_clips(profile):
    p = prof(
        profile, pre_roll_s=0, post_roll_s=0, min_clip_s=1, max_clip_s=20, target_duration_s=100
    )
    tl = plan_timeline(
        [hl(0, 4), hl(20, 24)],
        p,
        transcript=[TranscriptSegment(start=21, end=22, text="salut")],
    )
    assert tl.captions[0].start == pytest.approx(5.0)
    assert tl.captions[0].end == pytest.approx(6.0)


def test_empty_highlights(profile):
    assert plan_timeline([], profile).clips == []
