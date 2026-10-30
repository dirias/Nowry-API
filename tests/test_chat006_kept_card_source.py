"""CHAT-006 — a sentence kept from the chat remembers which card it was asked about."""
import pytest
from pydantic import ValidationError

from app.models.StudyCard import StudyCard, StudyCardUpdate


def test_a_kept_card_carries_its_source() -> None:
    card = StudyCard(title="運動の効果が出てきた。", content="Undō no kōka ga dete kita.\nThe exercise is starting to pay off.",
                     source_card_id="c1", source_card_title="効果")
    assert card.source_card_id == "c1"
    assert card.source_card_title == "効果"


def test_a_hand_written_card_has_none() -> None:
    assert StudyCard(title="a", content="b").source_card_id is None


def test_the_source_cannot_be_edited_later() -> None:
    assert "source_card_id" not in StudyCardUpdate.model_fields
    assert "source_card_title" not in StudyCardUpdate.model_fields


def test_the_source_is_bounded() -> None:
    with pytest.raises(ValidationError):
        StudyCard(title="a", content="b", source_card_title="x" * 101)
