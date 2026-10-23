"""CLI Minavio : profils, jeux, LLM et pipeline `run`."""

from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path
from typing import Annotated

import typer
from typer.core import TyperGroup

from minavio.config import list_games, list_profiles, load_game, load_profile
from minavio.config.presets import create_preset
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


class _PublishGroup(TyperGroup):
    """`minavio publish auth` reste une sous-commande ; `minavio publish <profil> <job>` publie."""

    def resolve_command(self, ctx, args):
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["send", *args]
        return super().resolve_command(ctx, args)


publish = typer.Typer(
    cls=_PublishGroup,
    no_args_is_help=True,
    help="Publication YouTube : `publish auth`, puis `publish <profil> <job_id>`.",
)
app.add_typer(publish, name="publish")
memes = typer.Typer(
    no_args_is_help=True, help="Banque de memes « .exe » (local, API optionnelles)."
)
app.add_typer(memes, name="memes")

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


def _create_preset_cli(new_id: str, base: str, name: str | None) -> None:
    try:
        path = create_preset(new_id, base=base, display_name=name)
    except MinavioError as e:
        raise _fail(e) from e
    typer.secho(f"Profil créé : {path}", fg="green")
    typer.echo(
        "Éditez ce fichier (nom, détecteurs, theme_preset) puis lancez `minavio profiles validate`."
    )


@profiles.command("new")
def profiles_new(
    new_id: Annotated[str, typer.Argument(help="Identifiant : a-z, 0-9, '_'.")],
    from_: Annotated[str, typer.Option("--from", help="Profil de base à copier.")] = "default_vlog",
    name: Annotated[str | None, typer.Option("--name", help="Nom affiché de la chaîne.")] = None,
) -> None:
    """Crée `configs/channels/<id>.yaml` à partir d'une base."""
    _create_preset_cli(new_id, from_, name)


preset = typer.Typer(no_args_is_help=True, help="Alias de `profiles new`.")
app.add_typer(preset, name="preset")

# Intégrations facultatives (clés hors dépôt) : stats de jeu, fact-check, tendances, notifications.
from minavio.integrations.cli_growth import growth_app  # noqa: E402
from minavio.integrations.cli_stats import stats_app  # noqa: E402
from minavio.integrations.cli_trends import trends_app  # noqa: E402

app.add_typer(stats_app, name="stats")
app.add_typer(growth_app, name="growth")
app.add_typer(trends_app)


@preset.command("create")
def preset_create(
    new_id: Annotated[str, typer.Argument(help="Identifiant : a-z, 0-9, '_'.")],
    from_: Annotated[str, typer.Option("--from", help="Profil de base à copier.")] = "default_vlog",
    name: Annotated[str | None, typer.Option("--name", help="Nom affiché de la chaîne.")] = None,
) -> None:
    """Crée un préréglage (même fonction que `profiles new`)."""
    _create_preset_cli(new_id, from_, name)


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


def _meme_line(asset) -> str:
    tags = ",".join(asset.tags) or "-"
    return f"{asset.id:<28} {asset.kind:<6} {asset.license:<12} {tags:<30} {asset.path}"


@memes.command("scan")
def memes_scan() -> None:
    """Indexe les médias déposés dans la banque perso (sans toucher aux téléchargements)."""
    from minavio.memes.library import library_dir, refresh_index

    root = library_dir()
    try:
        found = refresh_index(root)
    except MinavioError as e:
        raise _fail(e) from e
    typer.secho(f"{len(found)} média(s) local(aux) indexé(s) dans {root}.", fg="green")


@memes.command("search")
def memes_search(
    tags: Annotated[list[str], typer.Argument(metavar="TAGS...", help="Tags recherchés.")],
    kind: Annotated[str | None, typer.Option(help="gif, image, video ou sound.")] = None,
    restricted: Annotated[
        bool, typer.Option("--restricted", help="Inclut les licences restreintes.")
    ] = False,
) -> None:
    """Cherche des médias par tags dans la banque (licences sûres par défaut)."""
    from minavio.config.schema import MemeSettings
    from minavio.memes.library import search

    settings = MemeSettings(allow_restricted_licenses=restricted)
    try:
        assets = search(tags, kind, settings)
    except MinavioError as e:
        raise _fail(e) from e
    if not assets:
        typer.echo("Aucun média correspondant.")
    for asset in assets:
        typer.echo(_meme_line(asset))


@memes.command("fetch")
def memes_fetch(
    provider: Annotated[str, typer.Argument(metavar="FOURNISSEUR")],
    query: Annotated[str, typer.Argument(metavar="REQUETE")],
    kind: Annotated[str | None, typer.Option(help="gif, image, video ou sound.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=50)] = 5,
) -> None:
    """Télécharge des médias depuis une API (tenor, giphy, freesound, pixabay, pexels, imgflip)."""
    from minavio.memes.providers import fetch

    try:
        assets = fetch(provider, query, kind, limit)
    except MinavioError as e:
        raise _fail(e) from e
    typer.secho(f"{len(assets)} média(s) disponible(s) pour « {query} ».", fg="green")
    for asset in assets:
        typer.echo(_meme_line(asset))


@memes.command("list")
def memes_list(
    kind: Annotated[str | None, typer.Option(help="gif, image, video ou sound.")] = None,
) -> None:
    """Liste la banque avec la licence de chaque média."""
    from minavio.memes.library import is_license_allowed, library_dir, load_index

    try:
        assets = load_index(library_dir())
    except MinavioError as e:
        raise _fail(e) from e
    shown = [a for a in assets if not kind or a.kind == kind]
    if not shown:
        typer.echo("Banque vide : `minavio memes scan` ou `minavio memes fetch ...`.")
    for asset in shown:
        restricted = not is_license_allowed(asset.license, allow_restricted=False)
        typer.echo(_meme_line(asset) + ("  [restreinte]" if restricted else ""))


@memes.command("set-key")
def memes_set_key(
    name: Annotated[str, typer.Argument(metavar="NOM", help="Ex. TENOR_API_KEY.")],
    delete: Annotated[bool, typer.Option("--delete", help="Supprime la clé du fichier.")] = False,
) -> None:
    """Enregistre (saisie masquée) ou supprime une clé de fournisseur de memes."""
    llm_set_key(name=name, delete=delete)


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


@app.command("watch")
def watch_cmd(
    profile_id: Annotated[str, typer.Argument(metavar="PROFILE", help="Id du profil de chaîne.")],
    folder: Annotated[Path, typer.Argument(help="Dossier surveillé (ex. Outplayed).")],
    stable: Annotated[
        float, typer.Option(help="Secondes sans changement avant qu'un clip soit stable.")
    ] = 30.0,
    batch: Annotated[
        float, typer.Option(help="Secondes sans nouveau clip avant de lancer le lot.")
    ] = 600.0,
    music: Annotated[Path | None, typer.Option(help="Fichier musique passé au montage.")] = None,
    once: Annotated[
        bool, typer.Option("--once", help="Scanne, traite les clips stables, puis quitte.")
    ] = False,
    verbose: Verbose = False,
) -> None:
    """Ingestion automatique : surveille FOLDER et lance le montage par lots de clips stables.

    Ne publie jamais : la publication YouTube reste manuelle (`minavio publish`).
    Un lot en échec est marqué « failed » dans work/.watch_<profile>.json et n'est pas relancé
    automatiquement. Ctrl+C arrête le démon.
    """
    _setup_logging(verbose)
    if not folder.is_dir():
        raise _fail(
            MinavioError(f"Dossier introuvable : {folder}. Indique un dossier existant de clips."),
            verbose,
        )
    from minavio.watch import Watcher, serve

    try:
        load_profile(profile_id)
        watcher = Watcher(profile_id, folder, stable_s=stable, batch_s=batch, music=music)
    except MinavioError as e:
        raise _fail(e, verbose) from e
    if once:
        watcher.scan()
        watcher.tick()
        watcher.flush()
        return
    serve(watcher)


@publish.command("auth")
def publish_auth(
    client_secret: Annotated[
        Path | None,
        typer.Option("--client-secret", help="client_secret.json téléchargé depuis Google Cloud."),
    ] = None,
    status: Annotated[
        bool, typer.Option("--status", help="Affiche l'état sans rien lancer.")
    ] = False,
    logout: Annotated[bool, typer.Option("--logout", help="Supprime le jeton local.")] = False,
    verbose: Verbose = False,
) -> None:
    """Connecte YouTube (OAuth local, navigateur) : copie le client_secret.json puis autorise."""
    _setup_logging(verbose)
    from minavio.publish import auth

    if status:
        typer.echo(f"YouTube : {auth.status_text()}")
        typer.echo(f"Dossier de config : {auth.config_dir()}")
        return
    if logout:
        typer.echo("Jeton supprimé." if auth.forget() else "Aucun jeton à supprimer.")
        return
    try:
        typer.echo("Ouverture du navigateur pour autoriser Minavio à publier sur ta chaîne…")
        auth.authenticate(client_secret)
    except MinavioError as e:
        raise _fail(e, verbose) from e
    typer.secho(f"YouTube connecté (jeton : {auth.token_path()}).", fg="green")


@publish.command("send")
def publish_send(
    profile_id: Annotated[str, typer.Argument(metavar="PROFILE", help="Id du profil de chaîne.")],
    job_id: Annotated[str, typer.Argument(help="Job rendu (dossier work/<job_id>).")],
    video: Annotated[
        str, typer.Option("--video", help="youtube, shorts, short<N> ou all.")
    ] = "youtube",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Affiche ce qui serait envoyé, sans réseau.")
    ] = False,
    privacy: Annotated[
        str | None, typer.Option("--privacy", help="private, unlisted ou public (sinon le profil).")
    ] = None,
    at: Annotated[
        str | None,
        typer.Option("--at", help="Heure locale HH:MM de mise en ligne ; 'now' = immédiat."),
    ] = None,
    work: Annotated[Path, typer.Option("--work", help="Dossier work/.")] = Path("work"),
    out: Annotated[Path, typer.Option("--out", help="Dossier de sortie.")] = Path("out"),
    verbose: Verbose = False,
) -> None:
    """Publie un job sur YouTube (jamais automatique). Une vidéo déjà publiée est ignorée."""
    _setup_logging(verbose)
    from minavio.publish.youtube import publish_job

    try:
        profile = load_profile(profile_id)
        results = publish_job(
            profile,
            job_id,
            video=video,
            dry_run=dry_run,
            work_root=work,
            out_dir=out,
            privacy=privacy,
            schedule_at=at,
        )
    except MinavioError as e:
        raise _fail(e, verbose) from e
    for r in results:
        when = r["publish_at"] or "immédiat"
        if r["status"] == "dry_run":
            typer.secho(f"[dry-run] {r['key']} : {r['file']}", fg="cyan")
            typer.echo(f"  titre           : {r['title']}")
            typer.echo(f"  confidentialité : {r['privacy']} (mise en ligne : {when})")
            typer.echo(f"  tags            : {', '.join(r['tags'])}")
            typer.echo(f"  miniature       : {r['thumbnail'] or 'aucune'}")
            typer.echo("  description     :")
            typer.echo("    " + r["description"].replace("\n", "\n    "))
        elif r["status"] == "already_published":
            typer.echo(f"{r['key']} : déjà publiée, ignorée ({r.get('url')})")
        else:
            typer.secho(f"{r['key']} : publiée -> {r['url']} (mise en ligne : {when})", fg="green")
        for w in r.get("warnings", []):
            typer.secho(f"Avertissement : {w}", fg="yellow", err=True)
