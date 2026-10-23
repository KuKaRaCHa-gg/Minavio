"""Commandes `minavio growth` : recherche de niche et analytics de mes chaînes (lecture seule).

Monté par la CLI principale (`app.add_typer(growth_app, name="growth")`). Les rapports sont
écrits dans `out/<profile>/reports/`.
"""

from __future__ import annotations

from pathlib import Path

import typer

from minavio.config import load_profile
from minavio.errors import MinavioError
from minavio.integrations import analytics as an
from minavio.integrations import youtube_research as yr
from minavio.integrations.common import llm_for_profile

growth_app = typer.Typer(
    help="Recherche de niche YouTube et analytics de mes chaînes (lecture seule).",
    no_args_is_help=True,
)


def _load(profile_id: str):
    return load_profile(profile_id)


def _write_report(profile_id: str, name: str, text: str) -> Path:
    directory = Path("out") / profile_id / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / Path(name).name  # pas de chemin hors du dossier de rapports
    dest.write_text(text, encoding="utf-8")
    return dest


def _fail(exc: MinavioError) -> None:
    typer.secho(str(exc), fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@growth_app.command("niche")
def niche(
    profile: str = typer.Argument(..., help="Identifiant du profil de chaîne."),
    queries: list[str] = typer.Argument(..., help="Une ou plusieurs requêtes de niche."),  # noqa: B008
    days: int = typer.Option(30, "--days", min=1, max=365, help="Fenêtre de publication."),
    shorts: bool | None = typer.Option(
        None, "--shorts/--long", help="Ne garder que les Shorts (--shorts) ou les formats longs."
    ),
    out: str = typer.Option(
        "niche.md", "--out", help="Nom du rapport dans out/<profile>/reports/."
    ),
) -> None:
    """Vidéos qui marchent sur des requêtes de niche (outliers, motifs, idées)."""
    kind = None if shorts is None else ("short" if shorts else "long")
    try:
        prof = _load(profile)
        text = yr.niche_report(prof, queries, llm_for_profile(prof), days=days, kind=kind)
    except MinavioError as exc:
        _fail(exc)
    path = _write_report(profile, out, text)
    typer.echo(f"Rapport écrit : {path}")


@growth_app.command("analytics")
def analytics(
    profile: str = typer.Argument(..., help="Identifiant du profil de chaîne."),
    days: int = typer.Option(90, "--days", min=1, max=365, help="Fenêtre d'analyse."),
    out: str = typer.Option(
        "analytics.md", "--out", help="Nom du rapport dans out/<profile>/reports/."
    ),
) -> None:
    """Analytics de ma chaîne : ce qui marche, ce qui ne marche pas, recommandations."""
    try:
        prof = _load(profile)
        report = an.channel_report(prof, days=days)
        text = an.growth_advice(report, llm_for_profile(prof))
    except MinavioError as exc:
        _fail(exc)
    path = _write_report(profile, out, text)
    typer.echo(f"Rapport écrit : {path}")
