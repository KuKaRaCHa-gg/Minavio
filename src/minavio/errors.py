"""Exceptions communes. Chaque message doit dire quoi corriger."""


class MinavioError(Exception):
    """Base de toutes les erreurs Minavio."""


class ConfigError(MinavioError):
    """Profil YAML invalide ou introuvable."""


class FFmpegError(MinavioError):
    """Échec d'une commande FFmpeg/FFprobe."""


class LLMUnavailableError(MinavioError):
    """Le serveur LLM local ne répond pas ; le pipeline peut continuer sans."""


class MotionUnavailableError(MinavioError):
    """Rendu motion impossible (Node/Remotion absent et pas de fallback FFmpeg)."""
