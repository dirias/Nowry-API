"""CHAT-003 — the subject block says the card's language and how it has gone (ADR-055)."""
from app.routers.agent import ScreenContextPayload, _build_context_injection


def _block(**fields) -> str:
    return _build_context_injection(ScreenContextPayload(page="study_session", front="効果", isFlipped=False, **fields))


def test_a_japanese_card_is_read_in_kana_never_pinyin() -> None:
    block = _block(language="ja")
    assert "Language: Japanese (give readings in kana and romaji, never pinyin)" in block


def test_a_language_without_a_reading_rule_is_named_plainly() -> None:
    assert "Language: Spanish\n" in _block(language="es-MX") + "\n"


def test_the_last_grade_and_lapses_are_said() -> None:
    block = _block(lastGrade="again", lapses=3)
    assert "Last grade: Again" in block
    assert "Forgotten 3 times recently" in block


def test_nothing_is_said_when_nothing_is_known() -> None:
    block = _block()
    assert "Language:" not in block and "Last grade" not in block and "Forgotten" not in block


def test_the_answer_stays_hidden_on_the_front() -> None:
    block = _build_context_injection(
        ScreenContextPayload(page="study_session", front="効果", back="effect", isFlipped=False, language="ja")
    )
    assert "effect" not in block.split("Front: 効果")[1].split("IMPORTANT")[0]
