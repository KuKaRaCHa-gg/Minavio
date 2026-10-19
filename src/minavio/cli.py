"""CLI Minavio. Phase 1 : gestion des profils. `run` arrive en Phase 2 (cli-config-agent)."""

from __future__ import annotations

import json

import typer

from minavio.config import list_profiles, load_profile
from minavio.config.schema import ChannelProfile
from minavio.errors import MinavioError

app = typer.Typer(
    no_args_is_help=True, help="Montage automatique Minivitch / Byte Tricks / The Lost Chat."
)
profiles = typer.Typer(no_args_is_help=True, help="Profils de chaîne.")
app.add_typer(profiles, name="profiles")


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
