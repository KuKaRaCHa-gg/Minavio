"""Lot Minivitch : overlays par moment, SFX, chapitres, shorts, options CLI, profils.

Les modules externes (détection, ASR, LLM, rendu) sont remplacés par les fakes de
`test_pipeline_cli.Fakes`, appelés via `pipeline._api` comme dans les tests existants.
"""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from minavio import config as config_mod
from minavio import pipeline
from minavio.cli import app
from minavio.config import load_profile
from minavio.config.schema import ChannelProfile
from minavio.core.models import Chapter, Clip, Highlight, SfxCue, Timeline
from minavio.errors import MinavioError
from minavio.pipeline import build_overlays
from tests.cli.test_pipeline_cli import Fakes

runner = CliRunner()
SRC = Path("/videos/clip.mp4")
SHORT_RE = re.compile(r"_short\d+\.mp4$")


# --------------------------------------------------------------------------- helpers


def moment_timeline(cold_open_clips: int = 1) -> Timeline:
    """Cold open (multi_kill, 2.5 s) puis kill (3 s), multi_kill (4 s), clutch (4 s)."""
    clips = [
        Clip(source=SRC, start=10, end=12.5, highlight_label="multi_kill"),
        Clip(source=SRC, start=0, end=3, highlight_label="kill"),
        Clip(source=SRC, start=20, end=24, highlight_label="multi_kill"),
        Clip(source=SRC, start=40, end=44, highlight_label="clutch"),
    ]
    return Timeline(profile_id="minivitch", fps=60, clips=clips, cold_open_clips=cold_open_clips)


def profile_for(templates=(), order="chronological") -> ChannelProfile:
    p = load_profile("minivitch").model_copy(deep=True)
    p.motion.templates = list(templates)
    p.editing.order = order
    p.motion.captions.enabled = False
    return p


def by_template(overlays, name):
    return [o for o in overlays if o.template == name]


def hl(src, start, end, label, score=0.5, signals=None) -> Highlight:
    return Highlight(
        source=src,
        start=start,
        end=end,
        score=score,
        label=label,
        detector="t",
        signals=signals or {},
    )


def short_outputs(res):
    return [o for o in res.outputs if SHORT_RE.search(o.name)]


def api_with(monkeypatch, table, **over):
    merged = {**table, **over}
    monkeypatch.setattr(pipeline, "_api", lambda name: merged[name])


def mutate_profile(monkeypatch, fn):
    """Charge minivitch après `fn(profile)` (même principe que job_without_autocut)."""
    real = config_mod.load_profile

    def load(profile_id, *a, **k):
        p = real(profile_id, *a, **k)
        fn(p)
        return p

    monkeypatch.setattr(config_mod, "load_profile", load)


@pytest.fixture
def lot(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"video")
    fakes = Fakes(src)
    table = fakes.table()
    monkeypatch.setattr(pipeline, "_api", lambda name: table[name])
    return fakes, tmp_path, table


def run(lot, **kw):
    fakes, tmp, _ = lot
    return pipeline.run_job(
        "minivitch", [fakes.src], work_root=tmp / "work", out_dir=tmp / "out", **kw
    )


# --------------------------------------------------------------------------- overlays


def test_kill_counter_on_kill_and_multi_kill_not_on_cold_open():
    ov = build_overlays(moment_timeline(), profile_for(["kill_counter"]), None)
    kc = by_template(ov, "kill_counter")
    # le cold open (multi_kill) est exclu ; kill à 2.5 s, multi_kill à 5.5 s, +0.3 s
    assert [(o.start, o.duration, o.props["kills"]) for o in kc] == [
        (pytest.approx(2.8), 2.0, 1),
        (pytest.approx(5.8), 2.0, 2),
    ]


def test_kill_counter_multi_kill_uses_signals_kills():
    highlights = [hl(SRC, 20, 24, "multi_kill", signals={"kills": 4})]
    ov = build_overlays(moment_timeline(), profile_for(["kill_counter"]), None, highlights)
    assert [o.props["kills"] for o in by_template(ov, "kill_counter")] == [1, 4]


def test_kill_counter_needs_template():
    ov = build_overlays(moment_timeline(), profile_for([]), None)
    assert by_template(ov, "kill_counter") == []


def test_rank_card_countdown_numbers_real_moments():
    ov = build_overlays(moment_timeline(), profile_for(["rank_card"], "countdown"), None)
    assert [(c.start, c.props["rank"], c.props["total"]) for c in by_template(ov, "rank_card")] == [
        (pytest.approx(2.5), 3, 3),
        (pytest.approx(5.5), 2, 3),
        (pytest.approx(9.5), 1, 3),
    ]


def test_rank_card_absent_in_chronological():
    ov = build_overlays(moment_timeline(), profile_for(["rank_card"], "chronological"), None)
    assert by_template(ov, "rank_card") == []


def test_rewind_at_end_of_cold_open():
    ov = build_overlays(moment_timeline(), profile_for(["rewind"]), None)
    (rw,) = by_template(ov, "rewind")
    assert rw.start == pytest.approx(2.5 - 0.8)
    assert rw.duration == pytest.approx(0.8)


def test_rewind_absent_without_cold_open():
    ov = build_overlays(moment_timeline(cold_open_clips=0), profile_for(["rewind"]), None)
    assert by_template(ov, "rewind") == []


def test_title_moves_after_cold_open():
    ov = build_overlays(moment_timeline(), profile_for(), {"hook": "Hook!"})
    (title,) = by_template(ov, "title")
    assert title.start == pytest.approx(2.5)


def test_title_at_zero_without_cold_open():
    ov = build_overlays(moment_timeline(cold_open_clips=0), profile_for(), {"hook": "Hook!"})
    assert by_template(ov, "title")[0].start == 0


def test_popups_unchanged():
    copy = {"hook": None, "popups": [{"at": 1.0, "text": "wow"}]}
    ov = build_overlays(moment_timeline(), profile_for(), copy)
    assert [(o.start, o.props["text"]) for o in by_template(ov, "popup")] == [(1.0, "wow")]


# --------------------------------------------------------------------------- sfx


def test_sfx_step_writes_json_and_feeds_timeline(lot, monkeypatch):
    fakes, _, table = lot
    seen = {}

    def plan_sfx(timeline, settings):
        fakes.calls.append("sfx")
        seen["enabled"] = settings.enabled
        return [SfxCue(at=1.0, sound="whoosh")]

    api_with(monkeypatch, table, plan_sfx=plan_sfx)
    res = run(lot)
    assert "sfx" in fakes.calls and seen["enabled"] is True
    data = json.loads((res.work_dir / "sfx.json").read_text(encoding="utf-8"))
    assert data[0]["sound"] == "whoosh" and data[0]["at"] == 1.0


def test_sfx_skipped_when_disabled(lot, monkeypatch):
    fakes, _, _ = lot
    mutate_profile(monkeypatch, lambda p: setattr(p.audio.sfx, "enabled", False))
    res = run(lot)
    assert "sfx" not in fakes.calls
    assert not (res.work_dir / "sfx.json").exists()


def test_sfx_failure_is_warning(lot, monkeypatch):
    _, _, table = lot

    def boom(timeline, settings):
        raise RuntimeError("sfx down")

    api_with(monkeypatch, table, plan_sfx=boom)
    res = run(lot)
    assert any("sfx" in w for w in res.warnings)
    assert res.outputs


def test_sfx_resume_reads_json(lot):
    fakes, _, _ = lot
    run(lot)
    fakes.calls.clear()
    run(lot, resume=True)
    assert "sfx" not in fakes.calls


# --------------------------------------------------------------------------- chapitres


def _chapter_table(table):
    def gen(timeline, highlights, profile):
        return [Chapter(start=0, title="Intro"), Chapter(start=10, title="Clutch")]

    def fmt(chapters):
        return "\n".join(f"{int(c.start)} {c.title}" for c in chapters)

    return {**table, "generate_chapters": gen, "format_chapters": fmt}


def test_chapters_written_and_listed_in_extras(lot, monkeypatch):
    _, tmp, table = lot
    api_with(monkeypatch, table, **_chapter_table(table))
    res = run(lot)
    txt = tmp / "out" / "minivitch" / f"{res.job_id}_chapters.txt"
    assert txt.read_text(encoding="utf-8") == "0 Intro\n10 Clutch"
    assert res.extras == [txt]
    assert txt not in res.outputs
    chapters = json.loads((res.work_dir / "chapters.json").read_text(encoding="utf-8"))
    assert chapters[1] == {"start": 10.0, "title": "Clutch"}


def test_chapters_disabled_by_flag(lot, monkeypatch):
    fakes, _, table = lot
    api_with(monkeypatch, table, **_chapter_table(table))
    res = run(lot, chapters=False)
    assert "chapters" not in fakes.calls and res.extras == []


def test_chapters_disabled_by_profile(lot, monkeypatch):
    fakes, _, table = lot
    api_with(monkeypatch, table, **_chapter_table(table))
    mutate_profile(monkeypatch, lambda p: setattr(p, "chapters", False))
    res = run(lot)
    assert "chapters" not in fakes.calls and res.extras == []


def test_chapters_skipped_without_llm(lot, monkeypatch):
    fakes, _, table = lot
    api_with(monkeypatch, table, **_chapter_table(table))
    res = run(lot, use_llm=False)
    assert "chapters" not in fakes.calls and res.extras == []


def test_chapters_failure_is_warning(lot, monkeypatch):
    _, _, table = lot

    def boom(timeline, highlights, profile):
        raise RuntimeError("llm down")

    api_with(monkeypatch, table, generate_chapters=boom)
    res = run(lot)
    assert any("chapters" in w for w in res.warnings)
    assert res.extras == [] and len(res.outputs) == 3  # 2 formats + 1 short


# --------------------------------------------------------------------------- shorts


@pytest.fixture
def shorts_lot(tmp_path, monkeypatch):
    """Trois highlights (scores 0.4 / 0.9 / 0.7) ; la timeline reprend le label du highlight."""
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"video")
    fakes = Fakes(src)
    fakes.highlights = [
        hl(src, 1, 5, "multi_kill", score=0.4, signals={"kills": 3}),
        hl(src, 10, 14, "kill", score=0.9),
        hl(src, 20, 26, "clutch", score=0.7),
    ]
    table = fakes.table()
    plans: list[dict] = []

    def plan(h, profile, beats=None, music=None, transcript=None, keep_spans=None):
        plans.append(
            {
                "highlights": list(h),
                "target": profile.editing.target_duration_s,
                "order": profile.editing.order,
                "cold_open": profile.editing.cold_open.enabled,
                "id": profile.id,
                "transcript": transcript,
                "keep_spans": keep_spans,
            }
        )
        fakes.calls.append("plan")
        clips = [
            Clip(source=x.source, start=x.start, end=x.end, highlight_label=x.label) for x in h
        ]
        return Timeline(profile_id=profile.id, fps=60, clips=clips, captions=transcript or [])

    table["plan_timeline"] = plan
    monkeypatch.setattr(pipeline, "_api", lambda name: table[name])
    return fakes, tmp_path, plans, table


def run_shorts(shorts_lot, **kw):
    fakes, tmp, _, _ = shorts_lot
    return pipeline.run_job(
        "minivitch", [fakes.src], work_root=tmp / "work", out_dir=tmp / "out", **kw
    )


def test_shorts_one_per_highlight_best_first(shorts_lot):
    _, tmp, plans, _ = shorts_lot
    res = run_shorts(shorts_lot)
    shorts = short_outputs(res)
    assert [o.name.rsplit("_", 1)[1] for o in shorts] == ["short1.mp4", "short2.mp4", "short3.mp4"]
    assert all(o.exists() and o.parent == tmp / "out" / "minivitch" for o in shorts)
    assert len(res.outputs) == 5  # 2 formats + 3 shorts
    # un plan principal + un plan par short, le meilleur highlight d'abord
    assert len(plans) == 4
    assert [p["highlights"][0].score for p in plans[1:]] == [0.9, 0.7, 0.4]


def test_shorts_use_derived_profile(shorts_lot):
    _, _, plans, _ = shorts_lot
    run_shorts(shorts_lot)
    for p in plans[1:]:
        assert p["target"] == 30  # shorts.max_duration_s
        assert p["order"] == "chronological"
        assert p["cold_open"] is False
        assert p["id"] == "minivitch"
    # le profil principal n'est pas modifié
    assert plans[0]["target"] == 480 and plans[0]["cold_open"] is True


def test_shorts_overlays_have_no_title_and_kill_counter(shorts_lot):
    _, _, _, _ = shorts_lot
    res = run_shorts(shorts_lot)
    base = res.work_dir
    s1 = json.loads((base / "short1_overlays.json").read_text(encoding="utf-8"))
    assert [o["template"] for o in s1] == ["kill_counter"]
    assert s1[0]["props"]["kills"] == 1
    s2 = json.loads((base / "short2_overlays.json").read_text(encoding="utf-8"))
    assert s2 == []  # clutch : ni titre ni compteur
    s3 = json.loads((base / "short3_overlays.json").read_text(encoding="utf-8"))
    assert [o["props"]["kills"] for o in s3] == [3]  # multi_kill, signals.kills = 3


def test_shorts_count_from_argument(shorts_lot):
    res = run_shorts(shorts_lot, shorts=2)
    assert [o.name for o in short_outputs(res)] == [
        f"{res.job_id}_short1.mp4",
        f"{res.job_id}_short2.mp4",
    ]


def test_shorts_zero_disables(shorts_lot):
    res = run_shorts(shorts_lot, shorts=0)
    assert short_outputs(res) == []


def test_shorts_count_out_of_range(shorts_lot):
    with pytest.raises(MinavioError, match="attendu 0 à 10"):
        run_shorts(shorts_lot, shorts=11)


def test_shorts_format_missing_is_clear_error_before_work(shorts_lot, monkeypatch):
    fakes, _, _, _ = shorts_lot
    mutate_profile(monkeypatch, lambda p: setattr(p.shorts, "format", "nope"))
    with pytest.raises(MinavioError, match=r"shorts\.format 'nope' absent"):
        run_shorts(shorts_lot)
    assert fakes.calls == []


def test_shorts_format_not_vertical_is_error(shorts_lot, monkeypatch):
    fakes, _, _, _ = shorts_lot
    mutate_profile(monkeypatch, lambda p: setattr(p.shorts, "format", "youtube"))
    with pytest.raises(MinavioError, match=r"doivent être en 9:16"):
        run_shorts(shorts_lot)
    assert fakes.calls == []


def test_short_failure_is_warning_and_continues(shorts_lot):
    _, _, _, table = shorts_lot
    real_plan = table["plan_timeline"]

    def plan(h, profile, **kw):
        if len(h) == 1 and h[0].score == 0.9:  # le short n°1 seulement
            raise RuntimeError("montage impossible")
        return real_plan(h, profile, **kw)

    table["plan_timeline"] = plan  # `_api` lit ce dict à chaque appel
    res = run_shorts(shorts_lot)
    assert any(w.startswith("short 1 ignoré") for w in res.warnings)
    assert [o.name for o in short_outputs(res)] == [
        f"{res.job_id}_short2.mp4",
        f"{res.job_id}_short3.mp4",
    ]


def test_shorts_resume_per_file(shorts_lot):
    fakes, tmp, _, _ = shorts_lot
    res = run_shorts(shorts_lot)
    (res.work_dir / "short2_render.json").unlink()
    fakes.calls.clear()
    pipeline.run_job(
        "minivitch", [fakes.src], work_root=tmp / "work", out_dir=tmp / "out", resume=True
    )
    # seul le short 2 est re-rendu ; le reste vient des JSON et des fichiers existants
    assert fakes.calls == ["collect", "render:shorts"]


def test_shorts_get_transcript_and_keep_spans_of_their_source(shorts_lot):
    fakes, _, plans, _ = shorts_lot
    fakes.keep_spans = [(1.0, 4.0)]
    run_shorts(shorts_lot)
    short = plans[1]
    assert short["keep_spans"] == {fakes.src: [(1.0, 4.0)]}
    assert short["transcript"] and all(seg.source == fakes.src for seg in short["transcript"])


# --------------------------------------------------------------------------- CLI


def test_cli_shorts_and_no_chapters(shorts_lot, monkeypatch):
    fakes, tmp, _, table = shorts_lot
    monkeypatch.chdir(tmp)
    api_with(monkeypatch, table, **_chapter_table(table))
    r = runner.invoke(
        app,
        ["run", "minivitch", str(fakes.src), "--shorts", "2", "--no-chapters", "--out", "o"],
    )
    assert r.exit_code == 0, r.output
    assert len(re.findall(r"_short\d+\.mp4", r.stdout)) == 2
    assert "_chapters.txt" not in r.stdout
    assert "chapters" not in fakes.calls


def test_cli_extras_printed_after_outputs(lot, monkeypatch):
    fakes, tmp, table = lot
    monkeypatch.chdir(tmp)
    api_with(monkeypatch, table, **_chapter_table(table))
    r = runner.invoke(app, ["run", "minivitch", str(fakes.src), "-f", "16:9", "--out", "o"])
    assert r.exit_code == 0, r.output
    lines = r.stdout.strip().splitlines()
    assert lines[-1].endswith("_chapters.txt")
    assert SHORT_RE.search(lines[-2])  # un highlight -> un short, listé avant les extras


@pytest.mark.parametrize("value", ["11", "-1"])
def test_cli_shorts_out_of_range_rejected(lot, monkeypatch, value):
    fakes, tmp, _ = lot
    monkeypatch.chdir(tmp)
    r = runner.invoke(app, ["run", "minivitch", str(fakes.src), f"--shorts={value}"])
    assert r.exit_code == 2


def test_cli_shorts_format_error_exit_1(lot, monkeypatch):
    fakes, tmp, _ = lot
    monkeypatch.chdir(tmp)
    mutate_profile(monkeypatch, lambda p: setattr(p.shorts, "format", "nope"))
    r = runner.invoke(app, ["run", "minivitch", str(fakes.src)])
    assert r.exit_code == 1
    assert "Traceback" not in r.output
    assert "shorts.format 'nope' absent" in r.output


# --------------------------------------------------------------------------- profils


def test_profiles_validate_all_ok():
    r = runner.invoke(app, ["profiles", "validate"])
    assert r.exit_code == 0, r.output
    for pid in ("bytetricks", "minivitch", "minivitch_top10", "thelostchat"):
        assert f"ok    {pid}" in r.output


def test_minivitch_profile_lot_settings():
    p = load_profile("minivitch")
    assert p.audio.sfx.enabled is True
    assert p.editing.cold_open.enabled is True
    assert p.editing.cold_open.duration_s == 2.5
    assert p.chapters is True
    assert (p.shorts.per_highlight, p.shorts.max_duration_s, p.shorts.format) == (3, 30, "shorts")
    assert {"kill_counter", "rewind"} <= set(p.motion.templates)
    scene = [d for d in p.detection if d.name == "scene_change"]
    assert len(scene) == 1 and scene[0].weight == 0.3


def test_top10_profile_settings():
    p = load_profile("minivitch_top10")
    assert p.id == "minivitch_top10"
    assert p.display_name == "Minivitch — Top 10"
    assert p.editing.order == "countdown"
    assert p.llm.max_highlights == 10
    assert p.editing.target_duration_s == 420
    assert p.editing.cold_open.enabled is True
    assert "rank_card" in p.motion.templates
    assert p.shorts.per_highlight == 0


@pytest.mark.parametrize("pid", ["bytetricks", "thelostchat"])
def test_other_profiles_keep_defaults(pid):
    p = load_profile(pid)
    assert p.audio.sfx.enabled is False
    assert p.chapters is False
    assert p.shorts.per_highlight == 0
    assert p.editing.order == "chronological"
    assert p.editing.cold_open.enabled is False
