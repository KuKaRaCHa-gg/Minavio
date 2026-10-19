from minavio.config.schema import CaptionStyle
from minavio.core.models import TranscriptSegment, Word
from minavio.motion.captions import build_caption_overlays


def _seg() -> TranscriptSegment:
    words = [
        Word(text="un", start=10.0, end=10.2),
        Word(text="deux", start=10.2, end=10.6),
        Word(text="trois", start=10.6, end=11.0),
        Word(text="quatre", start=11.0, end=11.5),
        Word(text="cinq", start=11.5, end=12.0),
    ]
    return TranscriptSegment(start=10.0, end=12.0, text="un deux trois quatre cinq", words=words)


def test_groups_words_into_lines_with_relative_times():
    overlays = build_caption_overlays([_seg()], CaptionStyle(max_words_per_line=3))
    assert len(overlays) == 2
    first, second = overlays
    assert first.template == "captions"
    assert (first.start, first.duration) == (10.0, 1.0)
    assert [w["text"] for w in first.props["words"]] == ["un", "deux", "trois"]
    assert first.props["words"][1] == {"text": "deux", "start": 0.2, "end": 0.6}
    assert first.props["position"] == "bottom"
    assert first.props["style"] == "pixel"
    assert second.start == 10.6 + 0.4
    assert second.props["words"][0]["start"] == 0.0


def test_disabled_and_not_word_by_word():
    assert build_caption_overlays([_seg()], CaptionStyle(enabled=False)) == []
    out = build_caption_overlays(
        [_seg()], CaptionStyle(word_by_word=False, max_words_per_line=5, position="top")
    )
    assert len(out) == 1
    assert {w["start"] for w in out[0].props["words"]} == {0.0}
    assert out[0].props["position"] == "top"


def test_segment_without_word_timestamps_is_split_evenly():
    seg = TranscriptSegment(start=1.0, end=3.0, text="a b c d")
    out = build_caption_overlays([seg], CaptionStyle(max_words_per_line=2))
    assert [o.start for o in out] == [1.0, 2.0]
    assert out[0].props["words"] == [
        {"text": "a", "start": 0.0, "end": 0.5},
        {"text": "b", "start": 0.5, "end": 1.0},
    ]


def test_empty_input():
    assert build_caption_overlays([], CaptionStyle()) == []
