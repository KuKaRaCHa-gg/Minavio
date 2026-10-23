"""Sous-application Typer `stats_app` : stats de jeu et fact-check (montée par l'orchestrateur)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from minavio.config import load_profile
from minavio.errors import MinavioError
from minavio.integrations.factcheck import check_claims, factcheck_report
from minavio.integrations.gamestats import (
    apex_lines,
    apex_map_rotation,
    apex_player,
    riot_account,
    riot_last_lol_match,
    riot_valorant_last_match_id,
)
from minavio.llm.client import LocalLLM

stats_app = typer.Typer(
    help="Statistiques de jeu (Apex, Riot) et fact-check.", no_args_is_help=True
)


def _fail(exc: MinavioError) -> None:
    typer.echo(f"Erreur : {exc}", err=True)
    raise typer.Exit(1)


@stats_app.command("apex")
def apex_cmd(
    pseudo: str = typer.Argument(..., help="Pseudo Apex."),
    platform: str = typer.Option("PC", "--platform", help="PC, PS4 ou X1."),
) -> None:
    """Profil Apex : niveau, rang, RP, légende."""
    try:
        player = apex_player(pseudo, platform)
    except MinavioError as exc:
        _fail(exc)
    typer.echo("\n".join(apex_lines(player)))


@stats_app.command("apex-map")
def apex_map_cmd() -> None:
    """Rotation des cartes Battle Royale."""
    try:
        rot = apex_map_rotation()
    except MinavioError as exc:
        _fail(exc)
    if rot.current_map:
        typer.echo(f"Carte actuelle : {rot.current_map}")
    if rot.remaining:
        typer.echo(f"Temps restant : {rot.remaining}")
    if rot.next_map:
        typer.echo(f"Carte suivante : {rot.next_map}")


@stats_app.command("riot")
def riot_cmd(
    riot_id: str = typer.Argument(..., metavar="NAME#TAG", help="Riot ID, ex. Faker#KR1."),
    region: str = typer.Option("europe", "--region", help="americas, asia ou europe."),
) -> None:
    """Compte Riot et dernier match (LoL ; Valorant si la clé le permet)."""
    if "#" not in riot_id:
        _fail(MinavioError("Format attendu : Pseudo#TAG (ex. Faker#KR1)."))
    name, _, tag = riot_id.rpartition("#")
    try:
        account = riot_account(name, tag, region)
        typer.echo(f"Compte : {account.game_name}#{account.tag_line}")
        lol = riot_last_lol_match(account.puuid, region)
        if lol is None:
            typer.echo("LoL : aucun match récent trouvé.")
        else:
            outcome = {True: "victoire", False: "défaite"}.get(lol.win, "issue inconnue")
            details = ", ".join(x for x in (lol.champion, lol.kda, outcome) if x)
            typer.echo(f"LoL, dernier match : {details}")
        val_id = riot_valorant_last_match_id(account.puuid, region)
        if val_id is None:
            typer.echo(
                "Valorant : indisponible avec une clé de développement (clé de production requise)."
            )
        else:
            typer.echo(f"Valorant, dernier match (id) : {val_id}")
    except MinavioError as exc:
        _fail(exc)


@stats_app.command("factcheck")
def factcheck_cmd(
    profile: Annotated[str, typer.Argument(help="Identifiant du profil (ex. bytetricks).")],
    text: Annotated[
        str | None, typer.Option("--text", help="Script à vérifier (texte brut).")
    ] = None,
    file_: Annotated[
        Path | None,
        typer.Option(
            "--file", exists=True, dir_okay=False, readable=True, help="Fichier script.txt."
        ),
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Écrit le rapport Markdown dans ce fichier.")
    ] = None,
    lang: Annotated[str, typer.Option("--lang", help="Langue Wikipédia/Wikidata.")] = "fr",
) -> None:
    """Vérifie les affirmations factuelles d'un script (LLM local + Wikipédia)."""
    if (text is None) == (file_ is None):
        _fail(MinavioError("Donne exactement une option parmi --text ou --file."))
    body = text if text is not None else file_.read_text(encoding="utf-8")  # type: ignore[union-attr]
    try:
        prof = load_profile(profile)
        llm = LocalLLM.from_settings(prof.llm) if prof.llm.enabled else None
        claims = check_claims(body, llm, lang=lang)
    except MinavioError as exc:
        _fail(exc)
    report = factcheck_report(claims)
    if out is None:
        typer.echo(report)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    typer.echo(f"Rapport écrit : {out}")
