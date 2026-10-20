import json
from typing import Any

import pytest

from minavio.core import ffmpeg, healing
from minavio.errors import FFmpegError, MinavioError

LADDER = {
    "gpu": [{"video_codec": "libx264"}],
    "encoder": [{"video_codec": "libx264"}],
    "oom": [
        {"slowmo_mode": "minterpolate", "threads": 2},
        {"slowmo_mode": "none", "threads": 1},
    ],
}


@pytest.mark.parametrize(
    ("stderr", "category"),
    [
        ("Cannot load libcuda.so.1", "gpu"),
        ("No NVENC capable devices found", "gpu"),
        ("CUDA_ERROR_OUT_OF_MEMORY: out of memory on GPU", "gpu"),
        ("Cannot allocate memory", "oom"),
        ("MemoryError", "oom"),
        ("Killed", "oom"),
        ("[mov] moov atom not found", "corrupt"),
        ("Invalid data found when processing input", "corrupt"),
        ("file is corrupt", "corrupt"),
        ("Unknown encoder 'h264_nvenc'", "encoder"),
        ("Encoder not found", "encoder"),
        ("all good", None),
        ("", None),
    ],
)
def test_classify(stderr, category):
    assert healing.classify(stderr) == category


def test_with_healing_gpu_then_oom_then_success(tmp_path):
    log = tmp_path / "healing.json"
    messages = iter(["No NVENC capable devices found", "Cannot allocate memory", None])
    received: list[dict[str, Any]] = []

    def fn(**overrides):
        received.append(dict(overrides))
        msg = next(messages)
        if msg is not None:
            raise FFmpegError(msg)
        return "out.mp4"

    result = healing.with_healing(fn, steps=[healing.ladder(LADDER)], log_path=log, max_attempts=3)

    assert result == "out.mp4"
    assert received == [
        {},
        {"video_codec": "libx264"},
        {"video_codec": "libx264", "slowmo_mode": "minterpolate", "threads": 2},
    ]
    entries = json.loads(log.read_text(encoding="utf-8"))
    assert [e["category"] for e in entries] == ["gpu", "oom"]
    assert entries[0]["action"] == "video_codec=libx264"
    assert entries[1]["error"] == "Cannot allocate memory"


def test_with_healing_exhausted_lists_attempts(tmp_path):
    log = tmp_path / "healing.json"
    calls = []

    def fn(**overrides):
        calls.append(overrides)
        raise FFmpegError("Cannot allocate memory")

    with pytest.raises(FFmpegError, match="3 essai") as info:
        healing.with_healing(
            fn,
            steps=[healing.ladder(LADDER)],
            log_path=log,
            max_attempts=3,
        )
    assert len(calls) == 3
    message = str(info.value)
    assert "essai 1 : oom" in message and "essai 3 : oom" in message
    entries = json.loads(log.read_text(encoding="utf-8"))
    assert len(entries) == 3
    assert entries[-1]["action"] is None


def test_with_healing_no_applicable_step_raises_immediately(tmp_path):
    calls = []

    def fn(**overrides):
        calls.append(overrides)
        raise MinavioError("Invalid data found when processing input")

    with pytest.raises(MinavioError, match="1 essai"):
        healing.with_healing(fn, steps=[healing.ladder({})], log_path=None)
    assert calls == [{}]


def test_with_healing_unclassified_error_is_not_retried(tmp_path):
    calls = []

    def fn(**overrides):
        calls.append(overrides)
        raise FFmpegError("something else entirely")

    with pytest.raises(FFmpegError):
        healing.with_healing(fn, steps=[healing.ladder(LADDER)], log_path=None)
    assert len(calls) == 1


def test_error_message_is_truncated_in_attempt():
    attempt = healing.Attempt(category="gpu", action=None, error="x" * 1000)
    assert attempt.error is not None and len(attempt.error) == 300


def _lavfi_mp4(path, seconds=3):
    ffmpeg.run(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=s=160x120:r=10:d={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ]
    )
    return path


def test_repair_source_truncated_mp4(tmp_path):
    good = _lavfi_mp4(tmp_path / "good.mp4")
    data = good.read_bytes()
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(data[: int(len(data) * 0.6)])  # moov (faststart) conservé, mdat coupé

    repaired = healing.repair_source(broken, tmp_path / "work")

    assert repaired.parent == tmp_path / "work" / "repaired"
    info = ffmpeg.probe(repaired)
    assert info.width == 160 and info.height == 120
    assert info.duration > 0


def test_repair_source_garbage_raises(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all" * 10)
    with pytest.raises(FFmpegError, match="Réparation impossible"):
        healing.repair_source(junk, tmp_path / "work")
