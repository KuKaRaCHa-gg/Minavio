import os
import time
from types import SimpleNamespace

from minavio.config import secrets
from minavio.ui import services as svc


def test_list_jobs_sorted_by_date(tmp_path):
    for name, age in (("old", 1000), ("new", 0)):
        d = tmp_path / name
        d.mkdir()
        (d / "detect.json").write_text("[]")
        t = time.time() - age
        os.utime(d, (t, t))
    (tmp_path / "junk").mkdir()  # pas de detect.json : ignoré
    assert [j.job_id for j in svc.list_jobs(tmp_path)] == ["new", "old"]
    assert svc.job_choices(tmp_path)[0][1] == "new"
    assert svc.list_jobs(tmp_path / "absent") == []


def test_build_thumbnails_args():
    args = svc.build_thumbnails_args(
        "minivitch", "abc", text=" TXT ", layout="split", out_dir="/o", work_dir="/w"
    )
    assert args[1:6] == ["-m", "minavio", "thumbnails", "minivitch", "abc"]
    assert args[args.index("--text") + 1] == "TXT"
    assert args[args.index("--layout") + 1] == "split"
    assert "--layout" not in svc.build_thumbnails_args("p", "j", layout="auto")


def test_build_run_args_no_thumbnails():
    assert "--no-thumbnails" in svc.build_run_args("x", "/c", thumbnails=False)
    assert "--no-thumbnails" not in svc.build_run_args("x", "/c")


def test_job_state_collects_png(tmp_path):
    png = tmp_path / "j_thumb1_youtube.png"
    png.write_bytes(b"x")
    st = svc.JobState()
    st.feed(str(png))
    assert st.thumbs == [png] and st.outputs == []


def test_key_status_rows_never_expose_values():
    secrets.set_secret("ANTHROPIC_API_KEY", "sk-ant-SECRETVALUE-1234")
    rows = svc.key_status_rows()
    assert "SECRETVALUE" not in " ".join(" ".join(r) for r in rows)
    by_var = {r[1]: r for r in rows}
    assert by_var["ANTHROPIC_API_KEY"][2:] == ["fichier", "sk-…1234"]
    assert by_var["GITHUB_TOKEN"][2] == "absent"


def test_save_and_remove_key():
    msg = svc.save_key("GEMINI_API_KEY", "AIza-abcdefghijkl")
    assert msg.startswith("✅") and "abcdefghijkl" not in msg
    assert svc.save_key("NOPE", "x").startswith("❌")
    assert svc.save_key("GEMINI_API_KEY", "").startswith("❌")
    assert svc.remove_key("GEMINI_API_KEY").startswith("✅")


def test_provider_rows_chain(monkeypatch):
    from minavio.llm import providers as prov_mod

    secrets.set_secret("ANTHROPIC_API_KEY", "sk-ant-abcdefgh1234")
    pings = {"local": True, "anthropic": False}
    monkeypatch.setattr(
        prov_mod,
        "build_provider",
        lambda prov, settings: SimpleNamespace(ping=lambda: pings[prov.kind]),
    )
    rows = svc.provider_rows("minivitch")
    assert [r[1] for r in rows] == ["local", "anthropic"]
    assert rows[1][3] == "fichier" and rows[0][4] == "non testé"
    rows = svc.provider_rows("minivitch", ping=True)
    assert rows[0][4] == "✅ ok" and rows[1][4].startswith("❌")
    assert svc.provider_rows("bytetricks")[0][1] == "local"


def test_profile_defaults_thumbnails():
    assert svc.profile_defaults("minivitch")["thumbnails"] is True


def test_provider_rows_ping_each_provider_not_chain_position(monkeypatch, tmp_path):
    """Claude sans clé en 1er, local en 2e : chaque ligne porte SON état (pas celui du voisin)."""
    from minavio.config.schema import LlmProvider
    from minavio.llm import providers as prov_mod
    from minavio.ui import services as svc

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MINAVIO_SECRETS_FILE", str(tmp_path / "s.env"))
    real_load = svc.load_profile

    def fake_load(pid):
        p = real_load(pid)
        p.llm.providers = [LlmProvider(kind="anthropic"), LlmProvider(kind="local")]
        return p

    class FakeLocal:
        def ping(self):
            return True

    def fake_build(prov, settings):
        return None if prov.kind == "anthropic" else FakeLocal()

    monkeypatch.setattr(svc, "load_profile", fake_load)
    monkeypatch.setattr(prov_mod, "build_provider", fake_build)
    rows = svc.provider_rows("minivitch", ping=True)
    assert [r[1] for r in rows] == ["anthropic", "local"]
    assert rows[0][4] == "⏭ ignoré : clé absente" and rows[1][4] == "✅ ok"
