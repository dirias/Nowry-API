"""How many words a text has, in any script (ADR-064).

Counting words by splitting on spaces is right for scripts that put spaces
between words and wrong for those that do not: 600,000 characters of Japanese
were "1,100 words", every Japanese section was under the 40-word floor and was
dropped (so no cards could be made from a Japanese document), and the
200,000-word ceiling never applied to such text.

The count is therefore an estimate with one rule per kind of script:

- A token of a spaced script (Latin, Cyrillic, Greek, Arabic, Hebrew, Hangul,
  Devanagari …) is one word, exactly as before.
- A run of Han, Hiragana or Katakana is one word per two characters, rounded
  up. Chinese words average about 1.6 characters and Japanese about 2.
- A run of Thai, Lao, Khmer or Myanmar letters is one word per four letters,
  rounded up, combining vowel and tone marks not counted.
- CJK punctuation and full-width punctuation separate words, as spaces do.

The same rule, with the same test vectors, lives in the clients
(`packages/core/domain/text/wordCount.js`), so a count shown beside a document
is the count the server enforces.
"""
import re

CJK_CHARS_PER_WORD: int = 2
SEA_LETTERS_PER_WORD: int = 4

#: Han (with extensions and compatibility forms), Hiragana, Katakana, half-width Katakana.
_CJK = (
    "ぁ-ゖゝ-ゟ"        # Hiragana
    "ァ-ヺー-ヿ"        # Katakana (not the middle dot U+30FB)
    "ㇰ-ㇿ"                     # Katakana phonetic extensions
    "㐀-䶿一-鿿"        # CJK Unified Ideographs and Extension A
    "豈-﫿"                     # CJK Compatibility Ideographs
    "ｦ-ﾟ"                     # half-width Katakana
    "\U00020000-\U0002ffff"             # Extensions B and beyond
)
#: Thai, Lao, Myanmar, Khmer: written without spaces between words.
_SEA = "฀-๿຀-໿က-႟ក-៿"
#: The combining marks of those scripts, which are not letters of their own.
_SEA_MARKS = (
    "ัิ-ฺ็-๎"
    "ັິ-ຼ່-໎"
    "ါ-ှၖ-ၙၞ-ၠၢ-ၤၧ-ၭၱ-ၴႂ-ႍႏႚ-ႝ"
    "឴-៓៝"
)
#: CJK and full-width punctuation, the middle dot, Thai/Khmer/Myanmar sentence marks.
_SEPARATORS = "　-〿・＀-／：-＠［-｀｛-･ฯ๚๛၊။។-៚"

_SEPARATOR = re.compile(f"[{_SEPARATORS}]")
_UNSPACED = re.compile(f"[{_CJK}{_SEA}]")
_RUN = re.compile(f"([{_CJK}]+)|([{_SEA}]+)|([^{_CJK}{_SEA}]+)")
_SEA_MARK = re.compile(f"[{_SEA_MARKS}]")
_WORD_CHAR = re.compile(r"[^\W_]", re.UNICODE)


def _ceil_div(count: int, per: int) -> int:
    return -(-count // per)


def _mixed_token_words(token: str) -> int:
    """The words in a token that holds characters of an unspaced script."""
    words = 0
    for cjk, sea, other in _RUN.findall(token):
        if cjk:
            words += _ceil_div(len(cjk), CJK_CHARS_PER_WORD)
        elif sea:
            letters = len(_SEA_MARK.sub("", sea))
            words += _ceil_div(letters, SEA_LETTERS_PER_WORD) if letters else 0
        elif _WORD_CHAR.search(other):
            words += 1  # "DNA" beside kana is a word; a lone colon is not
    return words


def count_words(text: str) -> int:
    """The number of words in `text`: exact for spaced scripts, an estimate for unspaced ones."""
    if not text:
        return 0
    total = 0
    for token in _SEPARATOR.sub(" ", text).split():
        total += _mixed_token_words(token) if _UNSPACED.search(token) else 1
    return total
