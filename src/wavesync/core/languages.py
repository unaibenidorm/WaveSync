"""ISO 639-2 (B and T) language lookup and search, plus free-form codes.

Built on top of :mod:`wavesync.core.languages_data` (487 entries generated
from the *iso-codes* database). Every entry exposes:

* ``b_code`` (bibliographic, e.g. ``fre``, ``ger``, ``chi``)
* ``t_code`` (terminology, e.g. ``fra``, ``deu``, ``zho``)
* ``alpha_2`` (ISO 639-1 2-letter code when available)
* ``english``, ``spanish``, ``endonym`` (native name when available)
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from wavesync.core.languages_data import LANGUAGES


@dataclass(frozen=True, slots=True)
class Language:
    """One ISO 639-2 language entry."""

    b_code: str
    t_code: str
    alpha_2: str
    english: str
    spanish: str
    endonym: str

    @property
    def code(self) -> str:
        """Preferred 3-letter code for Matroska/MP4 tags (ISO 639-2/B or T)."""
        return self.b_code or self.t_code

    @property
    def codes_label(self) -> str:
        """Display string for codes, showing both B and T when they differ."""
        if self.b_code != self.t_code:
            return f"{self.b_code}/{self.t_code}"
        return self.t_code

    def display_name(self) -> str:
        """Human-readable label in Spanish + native endonym + code(s)."""
        base = self.spanish or self.english
        if self.endonym and self.endonym.lower() not in base.lower():
            return f"{base} — {self.endonym} [{self.codes_label}]"
        return f"{base} [{self.codes_label}]"


def _strip_accents(text: str) -> str:
    norm = unicodedata.normalize("NFD", text.casefold())
    return "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")


ALL_LANGUAGES: tuple[Language, ...] = tuple(
    Language(b, t, a2, en, es, endo) for (b, t, a2, en, es, endo) in LANGUAGES
)

_BY_CODE: dict[str, Language] = {}
for _lang in ALL_LANGUAGES:
    for _c in (_lang.b_code, _lang.t_code, _lang.alpha_2):
        if _c and _c.lower() not in _BY_CODE:
            _BY_CODE[_c.lower()] = _lang

_SEARCH_KEYS: tuple[tuple[str, Language], ...] = tuple(
    (
        _strip_accents(
            f"{lang.b_code} {lang.t_code} {lang.alpha_2} {lang.spanish} {lang.english} {lang.endonym}"
        ),
        lang,
    )
    for lang in ALL_LANGUAGES
)


def find_language(code: str) -> Language | None:
    """Look up a language by ISO 639-2/B, ISO 639-2/T, or ISO 639-1 code."""
    if not code:
        return None
    return _BY_CODE.get(code.strip().lower())


def normalize_language_code(code: str) -> str:
    """Normalise user input to a valid tag code.

    If ``code`` matches a known ISO 639-1/2 entry, returns its 3-letter
    ISO 639-2/T (or B) code; otherwise returns the stripped lowercase string
    (supporting custom/free-form tags) or ``"und"`` if empty.
    """
    cleaned = code.strip()
    if not cleaned:
        return "und"
    found = find_language(cleaned)
    if found is not None:
        return found.t_code
    return cleaned.lower()


def search_languages(query: str, limit: int | None = None) -> list[Language]:
    """Search ISO 639-2 languages by code, Spanish name, English name or endonym.

    Exact code matches come first, followed by prefix matches, then substring
    matches.
    """
    q = _strip_accents(query.strip())
    if not q:
        res = list(ALL_LANGUAGES)
        return res[:limit] if limit else res
    exact: list[Language] = []
    prefix: list[Language] = []
    sub: list[Language] = []
    for key, lang in _SEARCH_KEYS:
        if q in (lang.b_code.lower(), lang.t_code.lower(), lang.alpha_2.lower()):
            exact.append(lang)
        elif key.startswith(q) or any(tok.startswith(q) for tok in key.split()):
            prefix.append(lang)
        elif q in key:
            sub.append(lang)
    out = exact + prefix + sub
    return out[:limit] if limit else out

