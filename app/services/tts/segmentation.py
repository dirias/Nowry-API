"""Canonical language segmentation for mixed-language text (ADR-001).

`segment_text()` is the single source of truth for splitting a block of text
into per-language chunks so each chunk can be routed to a TTS voice matching
its own language. It is a **pure function**: no FastAPI, no Mongo, no HTTP,
no network calls of any kind — it is safe to import and unit-test in total
isolation, and safe to call in-process from both the Books path (direct
Python import, see `app/routers/tts.py`) and the `/v1/tts/segment` HTTP
endpoint (`app/routers/tts_segment.py`).

Algorithm
---------
1. **Script-run splitting.** The text is split into contiguous runs of a
   single Unicode script (Latin, Han/Hiragana/Katakana, Hangul, Cyrillic,
   Arabic, Devanagari) using regex character-range tests. Script-neutral
   characters (whitespace, digits, punctuation, symbols) never start a new
   run — they stay attached to whichever run they're adjacent to. This
   resolves CJK vs. Cyrillic vs. Arabic vs. Latin with certainty and zero
   model/classifier cost.

   Han (Chinese ideographs) and Hiragana/Katakana are treated as ONE run
   category, not two. Real Japanese text freely mixes kanji (Han) and kana
   (Hiragana/Katakana) mid-sentence ("日本語を勉強しています"); splitting on
   every Han/Kana boundary would fragment ordinary Japanese into alternating
   micro-segments. Within a merged Han/Kana run, each sentence-level chunk
   (see step 2) is resolved to "ja" if it contains any Hiragana/Katakana
   character, otherwise "zh" — still zero-classifier-cost, since presence of
   a kana character is a deterministic signal, not a statistical guess.

2. **Sentence-level splitting.** Within each script run, text is further
   split on sentence-terminal punctuation (`. ! ? 。 ！ ？`, with an allowance
   for a following closing quote/paren before the break). This is
   SENTENCE-LEVEL GRANULARITY ONLY.

   Word-level / mid-sentence code-switching (e.g. a single sentence that
   interleaves two languages word-by-word) is explicitly OUT OF SCOPE for
   v1. This is a deliberate product decision (see `docs/decisions.md`
   ADR-001 and `docs/prd.md` Non-Goals), not an oversight — do not "fix" it
   by attempting word-level segmentation without re-reading ADR-001 first.

3. **Deterministic language assignment.** Script categories that map
   unambiguously to one language (Han/Kana, Hangul, Cyrillic, Arabic,
   Devanagari) are assigned that language directly — no classifier call.

4. **Statistical classification for Latin script.** Latin-script segments
   are ambiguous among many languages (English, German, French, Spanish,
   Italian, Portuguese, Dutch, ...), so each Latin-script sentence chunk is
   run through the `lingua` classifier (PyPI package `lingua-language-detector`
   — see note below), restricted to a curated set of common Latin-script
   languages. A curated, smaller language set is a deliberate choice: lingua
   splits its confidence mass across every candidate language, so building
   the detector from all ~49 Latin-script languages it knows about dilutes
   confidence scores badly enough that even unambiguous short phrases
   ("Guten Morgen") score under 0.25 — unusable against any reasonable
   confidence threshold. A curated set keeps confidence scores meaningful
   for the languages this product actually expects to see.

5. **Low-confidence short-segment fallback.** If a segment is short
   (< `_SHORT_SEGMENT_CHAR_THRESHOLD` characters) AND the classifier's top
   confidence is below `_LOW_CONFIDENCE_THRESHOLD`, the segment falls back
   to the caller-supplied `default_lang` rather than guessing. Short text
   ("OK", "Hi", a bare proper noun) is inherently ambiguous for statistical
   language ID; guessing wrong is worse than deferring to the deck/book's
   declared UI locale.

6. **Adjacent-segment merge.** Adjacent segments that resolve to the same
   `lang_code` are merged back together, so e.g. "Hello. How are you?" is
   not needlessly fragmented into two separate English segments.

PyPI package name
------------------
The classifier library is published on PyPI as **`lingua-language-detector`**
(imported as `import lingua`) — this is the actively maintained, official
package (github.com/pemistahl/lingua-py), NOT the similarly-named
`lingua-py` package (an unrelated, low-adoption third-party Rust binding).
Pinned to `lingua-language-detector==2.1.1` in `requirements.txt`, which
requires Python >=3.10 — satisfying both the production image
(`python:3.10-slim`, see `Nowry-API/dockerfile`) and the 3.13 build
environment. 2.2.0+ requires >=3.12 and would break the production image.
No release of this package supports Python 3.9.
"""
from __future__ import annotations

import re
from functools import lru_cache

from app.models.tts import TextSegment

# ---------------------------------------------------------------------------
# Unicode script ranges
# ---------------------------------------------------------------------------
_LATIN_RE = re.compile(r"[A-Za-zÀ-ɏḀ-ỿ]")
_HAN_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
_KANA_RE = re.compile(r"[぀-ゟ゠-ヿㇰ-ㇿ]")
_HANGUL_RE = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿԀ-ԯ]")
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

# Script-run categories. "han_kana" merges Han + Hiragana/Katakana (see
# module docstring, step 1) — the per-sentence "ja" vs "zh" call happens
# later, in `_resolve_deterministic_lang`.
_CATEGORY_LATIN = "latin"
_CATEGORY_HAN_KANA = "han_kana"
_CATEGORY_HANGUL = "hangul"
_CATEGORY_CYRILLIC = "cyrillic"
_CATEGORY_ARABIC = "arabic"
_CATEGORY_DEVANAGARI = "devanagari"
_CATEGORY_NEUTRAL = "neutral"  # no scripted characters at all (digits/punct only)

_DETERMINISTIC_LANG_BY_CATEGORY: dict[str, str] = {
    _CATEGORY_HANGUL: "ko",
    _CATEGORY_CYRILLIC: "ru",
    _CATEGORY_ARABIC: "ar",
    _CATEGORY_DEVANAGARI: "hi",
}

# ---------------------------------------------------------------------------
# Sentence-terminal punctuation (ASCII + full-width CJK variants), with an
# allowance for a closing quote/paren before the sentence boundary.
# ---------------------------------------------------------------------------
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?。！？][\"'”’)\]]*(?:\s+|$)")

# ---------------------------------------------------------------------------
# Latin-script classifier tuning
# ---------------------------------------------------------------------------
_SHORT_SEGMENT_CHAR_THRESHOLD = 15
_LOW_CONFIDENCE_THRESHOLD = 0.6

# Curated set of common Latin-script languages. See module docstring, step 4,
# for why this is deliberately NOT `Language.all_with_latin_script()`.
_CLASSIFIER_LANGUAGE_NAMES = (
    "ENGLISH",
    "GERMAN",
    "FRENCH",
    "SPANISH",
    "ITALIAN",
    "PORTUGUESE",
    "DUTCH",
)


@lru_cache(maxsize=1)
def _get_latin_detector():
    """Build (once, lazily) the lingua LanguageDetector for Latin script.

    Lazy + cached so importing this module never pays classifier build cost
    (module import must stay cheap and side-effect-free), and repeated calls
    to `segment_text()` reuse the same detector instance.
    """
    from lingua import Language, LanguageDetectorBuilder

    languages = [getattr(Language, name) for name in _CLASSIFIER_LANGUAGE_NAMES]
    return LanguageDetectorBuilder.from_languages(*languages).build()


def _char_category(char: str) -> str | None:
    """Classify a single character into a script-run category.

    Returns None for script-neutral characters (whitespace, digits,
    punctuation, symbols, emoji) — these never start a new run.
    """
    if _LATIN_RE.match(char):
        return _CATEGORY_LATIN
    if _KANA_RE.match(char) or _HAN_RE.match(char):
        return _CATEGORY_HAN_KANA
    if _HANGUL_RE.match(char):
        return _CATEGORY_HANGUL
    if _CYRILLIC_RE.match(char):
        return _CATEGORY_CYRILLIC
    if _ARABIC_RE.match(char):
        return _CATEGORY_ARABIC
    if _DEVANAGARI_RE.match(char):
        return _CATEGORY_DEVANAGARI
    return None


def _split_by_script(text: str) -> list[tuple[str, str]]:
    """Split `text` into contiguous (run_text, category) script runs.

    Script-neutral characters (see `_char_category`) attach to whichever run
    they're adjacent to and never start a new run on their own, so
    "Hello, 世界!" stays two runs — "Hello, " (latin) + "世界!" (han_kana) —
    rather than fragmenting on the comma/space.
    """
    if not text:
        return []

    runs: list[tuple[str, str]] = []
    current_category: str | None = None
    current_chars: list[str] = []

    for char in text:
        category = _char_category(char)
        if category is None:
            # Script-neutral: extends the current run (or, if no run has
            # started yet, is held until one does).
            current_chars.append(char)
            continue
        if current_category is None:
            current_category = category
            current_chars.append(char)
        elif category == current_category:
            current_chars.append(char)
        else:
            runs.append(("".join(current_chars), current_category))
            current_category = category
            current_chars = [char]

    if current_chars:
        runs.append(("".join(current_chars), current_category or _CATEGORY_NEUTRAL))

    return runs


def _split_sentences(text: str) -> list[str]:
    """Split `text` into sentence-level chunks, preserving every character.

    Terminal punctuation (plus any immediately-following closing quote/paren
    and trailing whitespace) stays attached to the sentence that precedes
    it, so `"".join(_split_sentences(text)) == text` always holds — no
    information is lost, which matters for the adjacent-merge step in
    `segment_text()` to reconstruct exact substrings.
    """
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        end = match.end()
        sentences.append(text[start:end])
        start = end

    if start < len(text):
        remainder = text[start:]
        if remainder.strip() or not sentences:
            sentences.append(remainder)
        else:
            # Whitespace-only tail with no terminal punctuation (e.g. a
            # trailing newline after the last real sentence) — attach to
            # the previous sentence rather than emitting an empty segment.
            sentences[-1] += remainder

    return sentences


def _resolve_latin_lang(sentence: str, default_lang: str) -> str:
    """Resolve the language of a Latin-script sentence chunk."""
    stripped = sentence.strip()
    if not stripped:
        return default_lang

    detector = _get_latin_detector()
    confidence_values = detector.compute_language_confidence_values(stripped)
    if not confidence_values:
        return default_lang

    top = confidence_values[0]
    if len(stripped) < _SHORT_SEGMENT_CHAR_THRESHOLD and top.value < _LOW_CONFIDENCE_THRESHOLD:
        return default_lang

    return top.language.iso_code_639_1.name.lower()


def _resolve_deterministic_lang(sentence: str, category: str) -> str:
    """Resolve the language of a script run whose script maps 1:1 to a
    language, except `han_kana`, which still needs a presence-of-kana check
    (see module docstring, step 1)."""
    if category == _CATEGORY_HAN_KANA:
        return "ja" if _KANA_RE.search(sentence) else "zh"
    return _DETERMINISTIC_LANG_BY_CATEGORY[category]


def _merge_adjacent_same_lang(segments: list[TextSegment]) -> list[TextSegment]:
    """Merge consecutive segments that share a `lang_code` into one."""
    if not segments:
        return []

    merged: list[TextSegment] = [segments[0]]
    for segment in segments[1:]:
        if segment.lang_code == merged[-1].lang_code:
            merged[-1] = TextSegment(
                text=merged[-1].text + segment.text,
                lang_code=merged[-1].lang_code,
            )
        else:
            merged.append(segment)
    return merged


def segment_text(text: str, default_lang: str = "en") -> list[TextSegment]:
    """Split `text` into per-language `TextSegment`s.

    Pure function — no I/O, no Mongo, no FastAPI/HTTP imports. See the
    module docstring for the full algorithm and its deliberate v1 scope
    limits (sentence-level granularity only; no word-level code-switching).

    Args:
        text: The text to segment. May be empty or whitespace-only, in
            which case an empty list is returned.
        default_lang: Fallback ISO 639-1 language code (e.g. "en", "de")
            used for script-neutral text and for short, low-confidence
            Latin-script segments (see `_LOW_CONFIDENCE_THRESHOLD`).

    Returns:
        A list of `TextSegment`, in original text order, with adjacent
        same-language segments already merged.
    """
    if not text or not text.strip():
        return []

    raw_segments: list[TextSegment] = []
    for run_text, category in _split_by_script(text):
        for sentence in _split_sentences(run_text):
            if category == _CATEGORY_LATIN:
                lang_code = _resolve_latin_lang(sentence, default_lang)
            elif category == _CATEGORY_NEUTRAL:
                lang_code = default_lang
            else:
                lang_code = _resolve_deterministic_lang(sentence, category)
            raw_segments.append(TextSegment(text=sentence, lang_code=lang_code))

    return _merge_adjacent_same_lang(raw_segments)
