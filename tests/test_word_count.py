"""ADR-064: one word count for every script. The vectors are the contract the
clients' `wordCount.js` is tested against too; change them in both places."""
import json

import pytest

from app.services.book_sections import document_stats, parse_lexical, split_sections
from app.utils.word_count import count_words

VECTORS = [
    ("", 0),
    ("  \n\t ", 0),
    ("The cell is the basic unit of life.", 8),
    ("word — word", 3),
    ("Tema 3: La célula", 4),
    ("세포는 생명의 기본 단위입니다.", 4),
    ("الخلية هي الوحدة الأساسية للحياة", 5),
    ("细胞是生命的基本单位。", 5),
    ("細胞は生命の基本単位です。ミトコンドリアはATPを産生します。", 14),
    ("DNAを含む", 3),
    ("・核:DNAを含む", 4),
    ("カタカナ ひらがな", 4),
    ("a　b", 2),
    ("เซลล์เป็นหน่วยพื้นฐานของชีวิต", 6),
    ("ສະບາຍດີ", 2),
    ("𠀋𠀋𠀋", 2),
    ("。、！？", 0),
]


@pytest.mark.parametrize("text, words", VECTORS)
def test_the_vectors(text, words):
    assert count_words(text) == words


def test_spaced_text_counts_exactly_as_splitting_on_spaces_did():
    text = "Año, niño — pingüino; 3.14 (x) ¿qué? e-mail  \n\n fin."
    assert count_words(text) == len(text.split())


def _state(*blocks):
    def node(kind, text, tag=None):
        base = {"type": kind, "children": [{"type": "text", "text": text}]}
        return {**base, "tag": tag} if tag else base
    return {"root": {"type": "root", "children": [node(*block) for block in blocks]}}


JAPANESE = "細胞は生命の基本単位です。ミトコンドリアはATPを産生します。核にはDNAが含まれています。" * 4


def test_a_japanese_document_has_sections_and_a_real_word_count():
    """The fault: two chapters of Japanese were 8 words and no section, so no cards could be made."""
    state = _state(("heading", "第3章 細胞の構造", "h1"), ("paragraph", JAPANESE), ("paragraph", JAPANESE),
                   ("heading", "第4章 代謝", "h1"), ("paragraph", JAPANESE))
    sections = split_sections(state, "ノート")
    assert [s.heading for s in sections] == ["第3章 細胞の構造", "第4章 代謝"]
    assert sections[0].words > 100 and sections[1].words > 50
    stats = document_stats(parse_lexical(json.dumps(state)))
    assert stats["section_count"] == 2 and stats["word_count"] > 200


def test_a_chinese_and_a_thai_section_pass_the_floor_a_one_line_section_does_not():
    chinese = "细胞是生命的基本单位。线粒体产生三磷酸腺苷。细胞核含有脱氧核糖核酸。" * 4
    thai = "เซลล์เป็นหน่วยพื้นฐานของชีวิต ไมโทคอนเดรียผลิตพลังงาน " * 8
    state = _state(("heading", "细胞", "h1"), ("paragraph", chinese), ("heading", "เซลล์", "h2"), ("paragraph", thai),
                   ("heading", "短い", "h2"), ("paragraph", "一行だけ。"))
    assert [s.heading for s in split_sections(state, "doc")] == ["细胞", "เซลล์"]
