"""A title or a name as it is stored (IMPORT-049).

A document's title came straight from a file name or a form field: a
100,000-character title was stored, `.txt` made a document with no title at
all, and a name could carry a NUL or a right-to-left override that makes
`report‮txt.exe` read as something else. One rule, wherever a title or an
author is written.
"""
from __future__ import annotations

import re
from typing import Optional

TITLE_MAX_LENGTH: int = 200
AUTHOR_MAX_LENGTH: int = 100

#: Control characters, and the characters that override or embed text direction.
_UNPRINTABLE = re.compile("[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩﻿￾￿]")
_SPACES = re.compile(r"\s+")


def clean_label(value: Optional[str], limit: int) -> str:
    """One line of printable text, trimmed, at most `limit` characters."""
    text = _SPACES.sub(" ", _UNPRINTABLE.sub(" ", value or "")).strip()
    return text[:limit].rstrip()


def import_title(given: Optional[str], filename: str) -> str:
    """The title of an imported document: the one typed, else the file's name without — or, failing that, with — its extension."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    for candidate in (given, stem, filename):
        title = clean_label(candidate, TITLE_MAX_LENGTH)
        if title:
            return title
    return ""
