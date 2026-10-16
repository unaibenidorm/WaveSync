"""gettext setup for WaveSync (GUI-toolkit independent).

Core and UI both use ``_`` from here. The translation domain is ``wavesync``;
catalogs are looked up in ``WAVESYNC_LOCALEDIR`` or the package ``locale/``
directory, falling back to the source strings (Spanish).
"""

from __future__ import annotations

import gettext
import os
from pathlib import Path

DOMAIN = "wavesync"
_LOCALEDIR = Path(os.environ.get("WAVESYNC_LOCALEDIR", Path(__file__).resolve().parent.parent / "locale"))

SUPPORTED_LANGUAGES = frozenset({"es", "en", "ca"})
_language = "es"
_translation: gettext.NullTranslations = gettext.NullTranslations()


def set_language(language: str | None) -> str:
    """Select a supported UI language and reload its gettext catalog.

    Spanish is the source language and the safe fallback for missing or invalid
    preference values. Call this before constructing translated UI widgets.
    """
    global _language, _translation
    selected = (language or "es").split("_", 1)[0].split("-", 1)[0].lower()
    if selected not in SUPPORTED_LANGUAGES:
        selected = "es"
    _language = selected
    _translation = gettext.translation(
        DOMAIN,
        localedir=str(_LOCALEDIR),
        languages=[selected],
        fallback=True,
    )
    return _language


def language() -> str:
    """Return the currently selected UI language code."""
    return _language


set_language("es")


def _(message: str) -> str:
    """Translate ``message`` using the ``wavesync`` domain."""
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Plural-aware translation."""
    return _translation.ngettext(singular, plural, n)


def localedir() -> Path:
    """Directory where compiled ``.mo`` catalogs are searched."""
    return _LOCALEDIR

