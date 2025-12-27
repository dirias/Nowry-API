"""
SM-2 Spaced Repetition Algorithm

Based on the SuperMemo 2 algorithm for optimal flashcard scheduling.
Reference: https://www.supermemo.com/en/archives1990-2015/english/ol/sm2
"""

from datetime import datetime, timedelta
from typing import Literal

GradeType = Literal["again", "hard", "good", "easy"]


def calculate_next_review(
    grade: GradeType, ease_factor: float = 2.5, interval: int = 1, repetitions: int = 0
) -> dict:
    """
    Calculate the next review date and updated SM-2 parameters based on user grade.

    Args:
        grade: User's self-assessment ('again', 'hard', 'good', 'easy')
        ease_factor: Current ease factor (1.3-2.5, default 2.5)
        interval: Current interval in days
        repetitions: Number of consecutive successful reviews

    Returns:
        Dictionary with next_review, ease_factor, interval, and repetitions
    """

    # Map grades to quality scores (0-5 scale from SM-2)
    grade_map = {
        "again": 0,  # Complete blackout
        "hard": 3,  # Correct response recalled with serious difficulty
        "good": 4,  # Correct response after hesitation
        "easy": 5,  # Perfect response
    }

    quality = grade_map.get(grade, 0)

    # If quality < 3, reset the learning process
    if quality < 3:
        new_repetitions = 0
        new_interval = 1
        new_ease_factor = ease_factor
    else:
        # Calculate new ease factor
        new_ease_factor = ease_factor + (
            0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
        )

        # Clamp ease factor between 1.3 and 2.5
        new_ease_factor = max(1.3, min(2.5, new_ease_factor))

        # Calculate new interval
        if repetitions == 0:
            new_interval = 1
        elif repetitions == 1:
            new_interval = 6
        else:
            new_interval = round(interval * new_ease_factor)

        new_repetitions = repetitions + 1

    # Calculate next review date
    next_review = datetime.utcnow() + timedelta(days=new_interval)

    return {
        "next_review": next_review,
        "ease_factor": round(new_ease_factor, 2),
        "interval": new_interval,
        "repetitions": new_repetitions,
        "last_reviewed": datetime.utcnow(),
    }
