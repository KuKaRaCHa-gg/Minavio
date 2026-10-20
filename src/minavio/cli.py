"""CLI Minavio : profils, jeux, LLM et pipeline `run`."""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path
from typing import Annotated

import typer

from minavio.config import list_games, list_profiles, load_game, load_profile
from minavio.config.schema import ChannelProfile
from minavio.errors import MinavioError

app = typer.Typer(
    no_args_is_help=True, help="Montage automatique Minivitch / Byte Tricks / The Lost Chat."
)
profiles = typer.Typer(no_args_is_help=True, help="Profils de chaîne.")
app.add_typer(profiles, name="profiles")
llm = typer.Typer(no_args_is_help=True, help="Serveur LLM local.")
app.add_typer(llm, name="llm")
games = typer.Typer(no_args_is_help=True, help="Jeux (templates de détection).")
app.add_typer(games, name="games")

Verbose = Annotated[bool, typer.Option("--verbose", "-v", help="Logs DEBUG et traceback.")]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


def _fail(e: Exception, verbose: bool = False) -> typer.Exit:
    if verbose:
        traceback.print_exc()
    typer.secho(f"Erreur : {e}", fg="red", err=True)
    return typer.Exit(1)


@profiles.command("list")
def profiles_list() -> None:
    for pid in list_profiles():
        typer.echo(pid)


@profiles.command("show")
def profiles_show(profile_id: str) -> None:
    try:
        typer.echo(load_profile(profile_id).model_dump_json(indent=2))
    except MinavioError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e


@profiles.command("validate")
def profiles_validate() -> None:
    failed = False
    for pid in list_profiles():
        try:
            load_profile(pid)
            typer.secho(f"ok    {pid}", fg="green")
        except MinavioError as e:
            failed = True
            typer.secho(f"FAIL  {pid}\n{e}", fg="red", err=True)
    if failed:
        raise typer.Exit(1)


@app.command("schema")
def schema() -> None:
    """Exporte le JSON Schema des profils (utile pour l'autocomplétion YAML)."""
    typer.echo(json.dumps(ChannelProfile.model_json_schema(), indent=2))


@games.command("list")
def games_list() -> None:
    """Liste les jeux configurés."""
    for gid in list_games():
        try:
            g = load_game(gid)
            typer.echo(f"{gid}\t{g.name}\t{len(g.ui_templates)} template(s)")
        except MinavioError as e:
            typer.secho(f"{gid}\tINVALIDE : {e}", fg="red", err=True)
            raise typer.Exit(1) from e


@llm.command("ping")
def llm_ping(
    profile_id: Annotated[str | None, typer.Argument(help="Profil (défaut : le premier).")] = None,
    verbose: Verbose = False,
) -> None:
    """Teste la connexion au serveur LLM local du profil."""
    _setup_logging(verbose)
    try:
        profile = load_profile(profile_id or (list_profiles() or ["?"])[0])
        from minavio.llm.client import LocalLLM

        ok = LocalLLM.from_settings(profile.llm).ping()
    except (MinavioError, ImportError) as e:
        raise _fail(e, verbose) from e
    if not ok:
        typer.secho(f"KO  {profile.llm.base_url} ({profile.llm.model})", fg="red", err=True)
        raise typer.Exit(1)
    typer.secho(f"ok  {profile.llm.base_url} ({profile.llm.model})", fg="green")


@app.command("run")
def run(
    profile_id: Annotated[str, typer.Argument(metavar="PROFILE", help="Id du profil de chaîne.")],
    inputs: Annotated[list[Path], typer.Argument(help="Clips ou dossiers de clips (Outplayed).")],
    format_: Annotated[
        list[str] | None,
        typer.Option("--format", "-f", help="Nom (shorts), aspect (9:16) ou all. Répétable."),
    ] = None,
    music: Annotated[Path | None, typer.Option(help="Fichier musique (sinon music_dir).")] = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Sans ranking ni copy LLM.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Stop après timeline.json.")] = False,
    resume: Annotated[bool, typer.Option("--resume", help="Réutilise les JSON de work/.")] = False,
    out: Annotated[Path, typer.Option("--out", help="Dossier de sortie.")] = Path("out"),
    shorts: Annotated[
        int | None,
        typer.Option(
            "--shorts",
            min=0,
            max=10,
            help="Shorts par meilleur moment (0 = désactivé). Défaut : shorts.per_highlight.",
        ),
    ] = None,
    no_chapters: Annotated[
        bool, typer.Option("--no-chapters", help="Ne génère pas les chapitres YouTube.")
    ] = False,
    verbose: Verbose = False,
) -> None:
    """Détecte les moments forts, monte et rend les vidéos."""
    _setup_logging(verbose)
    from minavio.pipeline import run_job

    try:
        result = run_job(
            profile_id,
            inputs,
            formats=format_,
            music=music,
            use_llm=not no_llm,
            dry_run=dry_run,
            resume=resume,
            out_dir=out,
            shorts=shorts,
            chapters=not no_chapters,
        )
    except MinavioError as e:
        raise _fail(e, verbose) from e
    for w in result.warnings:
        typer.secho(f"Avertissement : {w}", fg="yellow", err=True)
    if dry_run:
        typer.echo(f"Timeline : {result.timeline_path}")
    for o in result.outputs:
        typer.echo(str(o))
    for x in result.extras:
        typer.echo(str(x))
