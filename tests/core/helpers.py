"""Aides de génération de médias synthétiques."""

import subprocess

W, H = 320, 180


def ff(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
        check=True,
        capture_output=True,
    )
