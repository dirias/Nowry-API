"""
Turn an email's name, a language and its values into a subject, a plain-text
part and an HTML part (MAIL-004, ADR-078 points 3 and 4). Pure: no network, no
database, no environment. The words live in `locales/*.json`; a locale that is
missing or unknown reads as English.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Mapping, Optional

HERE = Path(__file__).parent
LOCALES = ("en", "es", "fr", "de", "ja")
DEFAULT_LOCALE = "en"

# class: whose share of the day's budget it spends. sender: which From it carries.
KINDS: dict[str, dict[str, str]] = {
    "account.verify": {"class": "account", "sender": "account"},
    "account.reset": {"class": "account", "sender": "account"},
    # A reset asked for an address whose only sign-in is Google (ADR-083, FR-033).
    "account.google": {"class": "account", "sender": "account"},
    # The notice right after a deletion (ADR-089, FR-039): no button; a reply reaches support.
    "account.deleted": {"class": "account", "sender": "account", "button": "none"},
    # A letter to a person who asked to wait (ADR-078 point 1b): no button, and a way to leave.
    "waitlist.confirm": {"class": "confirmation", "sender": "personal"},
    # The form met an address that already has an account (ADR-084): the waitlist's look and sender, but not a list email, so no leave link.
    "waitlist.account": {"class": "confirmation", "sender": "personal", "leave": "none"},
}

_P = '<p style="margin:0 0 14px;">{}</p>'
_LINK = '<p class="muted" style="margin:0 0 14px;font-size:13px;color:#4A5558;word-break:break-all;">{}</p>'
_BUTTON = (
    '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 14px;"><tr>'
    '<td class="btn" style="background-color:#0B3E43;border-radius:8px;">'
    '<a href="{href}" style="display:inline-block;padding:12px 18px;font-size:15px;font-weight:600;'
    'color:#FCFBF8;text-decoration:none;">{label}</a></td></tr></table>'
)


_LEAVE = '{prompt} <a class="muted" href="{href}" style="color:#4A5558;text-decoration:underline;">{label}</a>'


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str
    locale: str


def resolve_locale(locale: Optional[str]) -> str:
    """`es-MX` reads as `es`; anything that is not one of the five reads as English."""
    code = (locale or "").strip().lower().replace("_", "-").split("-")[0]
    return code if code in LOCALES else DEFAULT_LOCALE


@lru_cache(maxsize=None)
def load_strings(locale: str) -> dict:
    return json.loads((HERE / "locales" / f"{locale}.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _layout(name: str) -> Template:
    return Template((HERE / name).read_text(encoding="utf-8"))


def _fill(text: str, values: Mapping[str, str]) -> str:
    """Substitute `$name`; a name with no value is a fault in the caller, not a blank."""
    return Template(text).substitute(values)


def _words(kind: str, locale: str) -> tuple[dict, dict]:
    strings = load_strings(locale)
    node: dict = strings
    for part in kind.split("."):
        node = node[part]
    return node, strings["common"]


def _footer(words: dict, common: dict, values: Mapping[str, str], postal: str) -> list[str]:
    lines = [f"{_fill(words['footer'], values)} {_fill(common['questions'], values)}"]
    if postal.strip():
        lines.append(postal.strip())
    return lines


def _text_part(words: dict, common: dict, values: Mapping[str, str], postal: str, visitor: bool) -> str:
    body = [_fill(p, values) for p in words["before"]]
    if "button" in words:
        body.append(values["link"])  # alone on its line, so plain-text clients keep it whole
    body += [_fill(p, values) for p in words.get("after", [])]
    body.append(words["signature"])
    lines = _footer(words, common, values, postal)
    if visitor:
        # The address alone on its line, like the button's.
        lines += [f"{common['leave_prompt']} {common['leave_label']}:", values["leave_link"]]
    footer = "\n".join(lines)
    return _layout("layout.txt").substitute(body="\n\n".join(body), footer=footer)


def _html_part(words: dict, common: dict, values: Mapping[str, str], postal: str, locale: str, logo_url: str, visitor: bool) -> str:
    safe = {key: html.escape(str(value), quote=True) for key, value in values.items()}
    blocks = [_P.format(_fill(html.escape(p), safe)) for p in words["before"]]
    if "button" in words:
        blocks.append(_BUTTON.format(href=safe["link"], label=html.escape(words["button"])))
        blocks.append(_LINK.format(_fill(html.escape(common["or_open"]), safe)))
    blocks += [_P.format(_fill(html.escape(p), safe)) for p in words.get("after", [])]
    blocks.append(_P.format(html.escape(words["signature"])))
    footer = "<br>".join(html.escape(line) for line in _footer(words, common, values, postal))
    if visitor:
        footer += "<br>" + _LEAVE.format(prompt=html.escape(common["leave_prompt"]), href=safe["leave_link"], label=html.escape(common["leave_label"]))
    return _layout("layout.html").substitute(
        lang=locale, subject=html.escape(words["subject"]), logo_url=html.escape(logo_url, quote=True),
        blocks="\n".join(blocks), footer=footer,
    )


def render_email(kind: str, locale: Optional[str], values: Mapping[str, str], *, logo_url: str, postal: str = "") -> RenderedEmail:
    """Both parts of one email. `values` needs `email` and `support`; `link` when the
    kind has a button; `leave_link` when it is a letter to a visitor.

    Raises KeyError for an unknown kind or a value the words ask for and were not given.
    """
    if kind not in KINDS:
        raise KeyError(kind)
    # A letter to a visitor always carries the way to leave (ADR-078 point 1b); account email never does.
    visitor = KINDS[kind]["sender"] == "personal" and KINDS[kind].get("leave") != "none"
    if visitor and not values.get("leave_link"):
        raise KeyError("leave_link")
    code = resolve_locale(locale)
    words, common = _words(kind, code)
    return RenderedEmail(
        subject=_fill(words["subject"], values),
        text=_text_part(words, common, values, postal, visitor),
        html=_html_part(words, common, values, postal, code, logo_url, visitor),
        locale=code,
    )
