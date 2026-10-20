import json
import sys
import types
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from minavio import pipeline
from minavio.cli import app
from minavio.config import secrets
from minavio.core.models import ThumbnailCandidate, ThumbnailChoice
from tests.cli.test_pipeline_cli import Fakes

runner = CliRunner()


class ThumbFakes:
    def __init__(self):
        self.calls: list[str] = []
        self.rendered: list[dict] = []
        self.picked = []
        self.fail_render = False
        self.fail_extract = False

    def table(self, src):
        def extract(highlights, profile, out_dir, n=None):
            self.calls.append("extract")
            if self.fail_extract:
                raise RuntimeError("no frames")
            out_dir.mkdir(parents=True, exist_ok=True)
            img = out_dir / "c0.png"
            img.write_bytes(b"png")
            return [
                ThumbnailCandidate(source=src, t=2.0, image=img, score=0.9, signals={"kills": 3.0})
            ]

        def pick(candidates, profile, copy=None, llm=None):
            self.calls.append("pick")
            self.picked.append(list(profile.thumbnails.layouts))
            return [
                ThumbnailChoice(layout=lay, images=candidates[:1], text="LLM TEXT")
                for lay in profile.thumbnails.layouts[:2]
            ]

        def render(choice, fmt, theme, out, badge=None, kills=None):
            self.calls.append("render")
            if self.fail_render:
                raise RuntimeError("remotion down")
            out.write_bytes(b"png")
            self.rendered.append(
                {"text": choice.text, "fmt": fmt.name, "badge": badge, "kills": kills}
            )
            return out

        return {
            "extract_candidates": extract,
            "pick_thumbnails": pick,
            "render_thumbnail": render,
        }


@pytest.fixture
def tenv(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"video")
    fakes = Fakes(src)
    tf = ThumbFakes()
    table = {**fakes.table(), **tf.table(src)}
    monkeypatch.setattr(pipeline, "_api", lambda name: table[name])
    return fakes, tf, tmp_path


def job(tenv, **kw):
    fakes, _, tmp = tenv
    return pipeline.run_job(
        "minivitch", [fakes.src], work_root=tmp / "work", out_dir=tmp / "out", **kw
    )


def pngs(res):
    return [p for p in res.extras if p.suffix == ".png"]


def test_thumbnails_step_files_and_json(tenv):
    _, tf, _ = tenv
    res = job(tenv)
    assert len(pngs(res)) == 4  # 2 choix x 2 formats (youtube + short)
    assert all(p.exists() and "_thumb" in p.name for p in pngs(res))
    assert {r["badge"] for r in tf.rendered} == {"MINIVITCH"}
    assert {r["kills"] for r in tf.rendered} == {3}
    assert len(json.loads((res.work_dir / "thumbnails.json").read_text())) == 4
    assert (res.work_dir / "thumbs" / "c0.png").exists()


def test_thumbnails_disabled_by_flag(tenv):
    _, tf, _ = tenv
    res = job(tenv, thumbnails=False)
    assert "extract" not in tf.calls
    assert not pngs(res)


def test_thumbnails_resume_reads_json(tenv):
    _, tf, _ = tenv
    job(tenv)
    n = tf.calls.count("extract")
    res = job(tenv, resume=True)
    assert tf.calls.count("extract") == n
    assert len(pngs(res)) == 4


def test_thumbnails_resume_recomputes_if_file_missing(tenv):
    _, tf, _ = tenv
    res = job(tenv)
    pngs(res)[-1].unlink()
    job(tenv, resume=True)
    assert tf.calls.count("extract") == 2


def test_thumbnails_failure_is_warning(tenv):
    _, tf, _ = tenv
    tf.fail_extract = True
    res = job(tenv)
    assert res.outputs
    assert any("thumbnails" in w for w in res.warnings)


def test_thumbnails_render_failure_is_warning(tenv):
    _, tf, _ = tenv
    tf.fail_render = True
    res = job(tenv)
    assert res.outputs and not pngs(res)
    assert any("thumbnails" in w for w in res.warnings)


def test_thumbnails_command_imposes_text_and_layout(tenv, monkeypatch):
    _, tf, tmp = tenv
    res = job(tenv, thumbnails=False)
    monkeypatch.chdir(tmp)
    r = runner.invoke(
        app, ["thumbnails", "minivitch", res.job_id, "--text", "MON TEXTE", "--layout", "hero"]
    )
    assert r.exit_code == 0, r.output
    assert tf.picked[-1] == ["hero"]
    assert {x["text"] for x in tf.rendered} == {"MON TEXTE"}
    assert r.output.count(".png") == 2  # 1 choix x 2 formats


def test_thumbnails_command_errors(tenv, monkeypatch):
    _, _, tmp = tenv
    monkeypatch.chdir(tmp)
    r = runner.invoke(app, ["thumbnails", "minivitch", "nope"])
    assert r.exit_code == 1 and "introuvable" in r.output
    r = runner.invoke(app, ["thumbnails", "minivitch", "nope", "--layout", "zzz"])
    assert r.exit_code == 1 and "Layout" in r.output


def _fake_client(monkeypatch, providers):
    mod = types.ModuleType("minavio.llm.client")
    mod.LocalLLM = SimpleNamespace(from_settings=lambda s: SimpleNamespace(providers=providers))
    monkeypatch.setitem(sys.modules, "minavio.llm.client", mod)


def test_llm_ping_multi_providers(monkeypatch):
    def boom():
        raise RuntimeError("clé absente")

    _fake_client(
        monkeypatch,
        [
            SimpleNamespace(name="local qwen", ping=lambda: False),
            SimpleNamespace(name="anthropic claude", ping=lambda: True),
            SimpleNamespace(name="gemini", ping=boom),
        ],
    )
    r = runner.invoke(app, ["llm", "ping", "minivitch"])
    assert r.exit_code == 0  # un fournisseur joignable suffit
    assert "ok  2. anthropic claude" in r.output
    assert "KO  1. local qwen" in r.output
    assert "KO  3. gemini : clé absente" in r.output


def test_llm_ping_all_down_exits_1(monkeypatch):
    _fake_client(monkeypatch, [SimpleNamespace(name="local", ping=lambda: False)])
    assert runner.invoke(app, ["llm", "ping", "minivitch"]).exit_code == 1


def test_llm_keys_hides_values(monkeypatch):
    secrets.set_secret("ANTHROPIC_API_KEY", "sk-ant-TOPSECRET-1234")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_ENVSECRET")
    r = runner.invoke(app, ["llm", "keys"])
    assert r.exit_code == 0
    assert "TOPSECRET" not in r.output and "ENVSECRET" not in r.output
    states = dict(ln.split()[:2] for ln in r.output.splitlines() if ln.split()[0].isupper())
    assert states["ANTHROPIC_API_KEY"] == "fichier"
    assert states["GITHUB_TOKEN"] == "env"
    assert states["GEMINI_API_KEY"] == "absent"


def test_llm_set_key_hidden_prompt_and_delete():
    r = runner.invoke(app, ["llm", "set-key", "OPENAI_API_KEY"], input="sk-openai-abcd9999\n")
    assert r.exit_code == 0, r.output
    assert "sk-openai-abcd9999" not in r.output
    assert secrets.secret_status(["OPENAI_API_KEY"])["OPENAI_API_KEY"] == "fichier"
    r = runner.invoke(app, ["llm", "set-key", "OPENAI_API_KEY", "--delete"])
    assert r.exit_code == 0
    assert secrets.secret_status(["OPENAI_API_KEY"])["OPENAI_API_KEY"] == "absent"
