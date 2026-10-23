"""Commandes de veille : `trends <profil> [--out trends.md]` et `notify-test`.

Application Typer séparée : le CLI principal la monte (`app.add_typer(trends_app, ...)`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

trends_app = typer.Typer(
    help="Veille de tendances et notifications.", no_args_is_help=True, add_completion=False
)


@trends_app.command("trends")
def trends_cmd(
    profile: Annotated[str, typer.Argument(help="Id du profil (ex. bytetricks).")],
    out: Annotated[Path, typer.Option("--out", help="Fichier Markdown de sortie.")] = Path(
        "trends.md"
    ),
) -> None:
    """Écrit le rapport de tendances du profil (sources publiques, idées LLM si disponible)."""
    from minavio.config import load_profile
    from minavio.errors import MinavioError
    from minavio.integrations.trends import trend_report

    try:
        prof = load_profile(profile)
    except MinavioError as e:
        typer.secho(f"Profil « {profile} » : {e}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from e
    report = trend_report(prof)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    typer.echo(f"Rapport écrit : {out.resolve()}")


@trends_app.command("notify-test")
def notify_test_cmd() -> None:
    """Envoie un message de test sur chaque canal configuré."""
    from minavio.integrations.notify import notify

    reached = notify("Minavio : test", "Notification de test. La configuration fonctionne.")
    if not reached:
        typer.secho(
            "Aucun canal n'a reçu le message. Configure DISCORD_WEBHOOK_URL, "
            "TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID ou NTFY_TOPIC (docs/GROWTH.md), "
            "puis relance. Les avertissements ci-dessus indiquent les canaux en échec.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    typer.echo("Notification envoyée : " + ", ".join(reached))
