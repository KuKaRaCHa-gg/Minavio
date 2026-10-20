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


def ping_providers(llm_client, settings) -> list[tuple[str, bool, str]]:
    """(nom, ok, détail) pour chaque fournisseur de la chaîne ; repli : le seul LLM local."""
    providers = getattr(llm_client, "providers", None)
    if callable(providers):
        providers = providers()
    if not providers:
        providers = [llm_client]
    rows = []
    for i, p in enumerate(providers, 1):
        name = str(getattr(p, "name", None) or f"{settings.model} @ {settings.base_url}")
        try:
            ok, detail = bool(p.ping()), ""
        except Exception as e:  # clé absente, réseau… : raison affichée, jamais de valeur
            ok, detail = False, str(e)
        if not ok and not detail:
            detail = "injoignable ou clé absente/refusée (voir `minavio llm keys`)"
        rows.append((f"{i}. {name}", ok, detail))
    return rows


@llm.command("ping")
def llm_ping(
    profile_id: Annotated[str | None, typer.Argument(help="Profil (défaut : le premier).")] = None,
    verbose: Verbose = False,
) -> None:
    """Teste chaque fournisseur LLM de la chaîne du profil (une ligne par fournisseur)."""
    _setup_logging(verbose)
    try:
        profile = load_profile(profile_id or (list_profiles() or ["?"])[0])
        from minavio.llm.client import LocalLLM

        rows = ping_providers(LocalLLM.from_settings(profile.llm), profile.llm)
    except (MinavioError, ImportError) as e:
        raise _fail(e, verbose) from e
    for name, ok, detail in rows:
        if ok:
            typer.secho(f"ok  {name}", fg="green")
        else:
            typer.secho(f"KO  {name} : {detail}", fg="red", err=True)
    if not any(ok for _, ok, _ in rows):
        raise typer.Exit(1)


@llm.command("keys")
def llm_keys() -> None:
    """État des clés d'API (env / fichier / absent), sans afficher de valeur."""
    from minavio.config.secrets import KNOWN_KEYS, load_secrets, secret_status, secrets_path

    load_secrets()
    for name, state in secret_status(KNOWN_KEYS).items():
        typer.echo(f"{name:<20} {state}")
    typer.echo(f"Fichier de secrets : {secrets_path()}")


@llm.command("set-key")
def llm_set_key(
    name: Annotated[str, typer.Argument(metavar="NOM", help="Ex. ANTHROPIC_API_KEY.")],
    delete: Annotated[bool, typer.Option("--delete", help="Supprime la clé du fichier.")] = False,
) -> None:
    """Enregistre (saisie masquée) ou supprime une clé dans le fichier de secrets local."""
    from minavio.config.secrets import delete_secret, mask, set_secret

    try:
        if delete:
            gone = delete_secret(name)
            typer.echo(f"{name} supprimée." if gone else f"{name} n'était pas enregistrée.")
            return
        value = typer.prompt(f"Valeur de {name}", hide_input=True)
        set_secret(name, value)
    except MinavioError as e:
        raise _fail(e) from e
    typer.secho(f"{name} enregistrée ({mask(value.strip())}).", fg="green")


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
    no_thumbnails: Annotated[
        bool, typer.Option("--no-thumbnails", help="Ne génère pas les miniatures.")
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
            thumbnails=not no_thumbnails,
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


@app.command("thumbnails")
def thumbnails_cmd(
    profile_id: Annotated[str, typer.Argument(metavar="PROFILE", help="Id du profil de chaîne.")],
    job_id: Annotated[str, typer.Argument(help="Job existant (dossier work/<job_id>).")],
    text: Annotated[str | None, typer.Option("--text", help="Texte imposé (sinon le LLM).")] = None,
    layout: Annotated[str | None, typer.Option("--layout", help="hero, split ou collage.")] = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Choix heuristique.")] = False,
    work: Annotated[Path, typer.Option("--work", help="Dossier work/.")] = Path("work"),
    out: Annotated[Path, typer.Option("--out", help="Dossier de sortie.")] = Path("out"),
    verbose: Verbose = False,
) -> None:
    """Régénère les miniatures d'un job existant (relit detect/rank/copy.json de work/)."""
    _setup_logging(verbose)
    if layout is not None and layout not in ("hero", "split", "collage"):
        raise _fail(MinavioError(f"Layout '{layout}' inconnu : hero, split ou collage."), verbose)
    from minavio.core.models import Highlight
    from minavio.pipeline import _Steps, build_thumbnails

    try:
        profile = load_profile(profile_id)
        wdir = work / job_id
        if not wdir.is_dir():
            raise MinavioError(
                f"Job '{job_id}' introuvable ({wdir}). Lance d'abord `minavio run {profile_id} …` "
                "ou vérifie --work."
            )
        hl_path = next(
            (wdir / f for f in ("rank.json", "detect.json") if (wdir / f).exists()), None
        )
        if hl_path is None:
            raise MinavioError(f"Ni rank.json ni detect.json dans {wdir} : relance `minavio run`.")
        highlights = [Highlight.model_validate(x) for x in json.loads(hl_path.read_text("utf-8"))]
        copy = None
        if (wdir / "copy.json").exists():
            copy = json.loads((wdir / "copy.json").read_text("utf-8"))
        warnings: list[str] = []
        steps = _Steps(wdir, False, warnings)
        files = build_thumbnails(
            steps,
            profile,
            job_id,
            highlights,
            copy,
            work=wdir,
            target=out / profile.id,
            use_llm=profile.llm.enabled and not no_llm,
            text=text,
            layout=layout,
        )
    except (MinavioError, json.JSONDecodeError, ValueError) as e:
        raise _fail(e, verbose) from e
    for w in warnings:
        typer.secho(f"Avertissement : {w}", fg="yellow", err=True)
    if not files:
        raise _fail(MinavioError("aucune miniature produite (voir les avertissements)"), verbose)
    for f in files:
        typer.echo(str(f))


@app.command("ui")
def ui(
    port: Annotated[int, typer.Option(help="Port local de l'interface.")] = 7860,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Ne pas ouvrir le navigateur.")
    ] = False,
    out: Annotated[Path | None, typer.Option("--out", help="Dossier de sortie par défaut.")] = None,
) -> None:
    """Ouvre l'interface graphique (navigateur, en local uniquement)."""
    try:
        from minavio.ui.app import launch
    except ImportError as e:
        typer.secho(
            f"Erreur : interface indisponible ({e}). Installe-la : uv sync --extra ui",
            fg="red",
            err=True,
        )
        raise typer.Exit(1) from e
    launch(port=port, open_browser=not no_browser, out_dir=out)
