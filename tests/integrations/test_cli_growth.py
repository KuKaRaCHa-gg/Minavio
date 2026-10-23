"""CLI `growth` via CliRunner : écriture dans out/<profile>/reports/ et erreurs actionnables."""

from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from minavio.errors import MinavioError
from minavio.integrations import analytics as an
from minavio.integrations import cli_growth
from minavio.integrations import youtube_research as yr

runner = CliRunner()


def test_niche_command_writes_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(cli_growth, "_load", lambda pid: SimpleNamespace(id=pid))

    def fake_niche(prof, queries, llm, **kwargs):
        seen.update(profile=prof.id, queries=list(queries), llm=llm, **kwargs)
        return "# niche ok\n"

    monkeypatch.setattr(yr, "niche_report", fake_niche)
    result = runner.invoke(
        cli_growth.growth_app,
        [
            "niche",
            "gaming",
            "minecraft survie",
            "vlog",
            "--days",
            "14",
            "--shorts",
            "--out",
            "n.md",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "gaming" / "reports" / "n.md").read_text(
        encoding="utf-8"
    ) == "# niche ok\n"
    assert seen["queries"] == ["minecraft survie", "vlog"]
    assert seen["days"] == 14 and seen["kind"] == "short"
    assert seen["llm"] is None  # profil sans LLM


def test_niche_command_long_flag_and_default_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(cli_growth, "_load", lambda pid: SimpleNamespace(id=pid))
    monkeypatch.setattr(
        yr, "niche_report", lambda prof, q, llm, **kw: seen.update(kind=kw["kind"]) or "x"
    )
    result = runner.invoke(cli_growth.growth_app, ["niche", "gaming", "q", "--long"])

    assert result.exit_code == 0, result.output
    assert seen["kind"] == "long"
    assert (tmp_path / "out" / "gaming" / "reports" / "niche.md").is_file()


def test_analytics_command_writes_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_growth, "_load", lambda pid: SimpleNamespace(id=pid))
    monkeypatch.setattr(an, "channel_report", lambda prof, days: SimpleNamespace(days=days))
    monkeypatch.setattr(an, "growth_advice", lambda report, llm: f"# analytics {report.days} j\n")

    result = runner.invoke(cli_growth.growth_app, ["analytics", "gaming", "--days", "30"])

    assert result.exit_code == 0, result.output
    path = tmp_path / "out" / "gaming" / "reports" / "analytics.md"
    assert path.read_text(encoding="utf-8") == "# analytics 30 j\n"


def test_analytics_command_actionable_error_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_growth, "_load", lambda pid: SimpleNamespace(id=pid))

    def boom(prof, days):
        raise MinavioError("Le jeton YouTube n'a pas le scope Analytics. --logout")

    monkeypatch.setattr(an, "channel_report", boom)
    result = runner.invoke(cli_growth.growth_app, ["analytics", "gaming"])

    assert result.exit_code == 1
    assert not (tmp_path / "out").exists()


def test_help_lists_both_commands():
    result = runner.invoke(cli_growth.growth_app, ["--help"])
    assert result.exit_code == 0
    assert "niche" in result.output and "analytics" in result.output
