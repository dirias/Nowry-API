"""
How the companion talks, from the learner's own settings (CHAT-011, ADR-056).

A learner with tone Friendly asked "Show it in a sentence" and got a question
back: the stage's personality ordered a question at the end of every reply,
stage 5 asked for Socratic framing, and stage 6 capped replies at two sentences
whatever length the learner had chosen. The settings reached the prompt; other
lines overrode them.

So the style section is written here, once: one line for reply length, one for
tone, one for teaching style, all three the learner's, and then the stage's
character, which describes how the companion feels and sets no behaviour.
Only the chosen option of each goes into the prompt.
"""
from dataclasses import dataclass

CONCISENESS = ("concise", "balanced", "detailed")
TONES = ("friendly", "professional", "strict")
TEACHING_STYLES = ("show", "guide")

#: The tone a learner had before Socratic became a teaching style (ADR-056 §2).
LEGACY_SOCRATIC = "socratic"

LENGTH_RULES: dict[str, str] = {
    "concise": (
        "RESPONSE LENGTH: Be extremely brief: 1–2 sentences per reply, plus any example rows asked for. "
        "Skip preamble, headers and bullet lists unless essential."
    ),
    "balanced": (
        "RESPONSE LENGTH: Concise but complete: 2–4 sentences for a simple question, a short paragraph or list for a "
        "complex one. Never pad."
    ),
    "detailed": (
        "RESPONSE LENGTH: The learner wants thorough explanations: clear sections, examples and analogies, and the "
        "obvious next thing they would ask, answered."
    ),
}

TONE_RULES: dict[str, str] = {
    "friendly": (
        "TONE: Warm, encouraging and conversational. Celebrate small wins; a light touch of humour or an emoji "
        "where it feels natural."
    ),
    "professional": (
        "TONE: Polished and precise, like a knowledgeable academic tutor. No emoji, no filler words; clarity and "
        "accuracy above all."
    ),
    "strict": (
        "TONE: Direct and firm. If cards are due or goals are falling behind, say it plainly. Don't soften "
        "feedback; celebrate real achievements only."
    ),
}

TEACHING_RULES: dict[str, str] = {
    "show": (
        "TEACHING STYLE — Show me: teach directly; give what was asked for (examples, readings, explanations, "
        "steps). Never answer a request with a question or end on one unless they ask to be quizzed. "
        "\"Let me try first\": one clue, then let them answer, for this subject."
    ),
    "guide": (
        "TEACHING STYLE — Let me work it out: for something they could reason out, give one clue and one guiding "
        "question, then wait; after their attempt, confirm or correct it and explain in full. A tapped ready "
        "question (examples, a sentence, a breakdown) gets that content first; one question may follow. State progress "
        "plainly. \"Just show me\": teach directly, for this subject."
    ),
}

#: Character only (ADR-056 §3): no length, no questions, no method.
STAGE_CHARACTER: dict[int, str] = {
    1: "You are new to this learner: curious, a little tentative, glad to help.",
    2: "You are warm and energized, and you mirror the learner's enthusiasm.",
    3: "You are confident and playful, and you enjoy a good analogy.",
    4: "You are strategic and measured, with the learner's long-term goal in mind.",
    5: "You are reflective: you connect ideas across subjects and notice how the learner has grown.",
    6: "You are calm and precise, and you treat the learner as a peer.",
}


@dataclass(frozen=True)
class CompanionStyle:
    """The learner's three style settings, resolved to known values."""

    conciseness: str = "balanced"
    tone: str = "friendly"
    teaching: str = "show"


def resolve_style(agent_prefs: dict) -> CompanionStyle:
    """Read `preferences.agent` into a style; unknown values fall back to the defaults.

    A stored `socratic` tone is Friendly with the Let-me-work-it-out style,
    unless the learner has since chosen a teaching style of their own.
    """
    tone = agent_prefs.get("tone")
    teaching = agent_prefs.get("teaching_style")
    if tone == LEGACY_SOCRATIC:
        tone = "friendly"
        teaching = teaching or "guide"
    conciseness = agent_prefs.get("conciseness")
    return CompanionStyle(
        conciseness=conciseness if conciseness in CONCISENESS else "balanced",
        tone=tone if tone in TONES else "friendly",
        teaching=teaching if teaching in TEACHING_STYLES else "show",
    )


def style_section(style: CompanionStyle, stage: int) -> str:
    """The prompt's style section: length, tone, teaching style, then the stage's character."""
    character = STAGE_CHARACTER.get(stage, STAGE_CHARACTER[1])
    return "\n".join(
        (
            LENGTH_RULES[style.conciseness],
            TONE_RULES[style.tone],
            TEACHING_RULES[style.teaching],
            "CHARACTER (colour only, never a rule): " + character,
        )
    )


def feedback_style_line(style: CompanionStyle) -> str:
    """The learner's tone and length for a reply outside the chat, such as quiz feedback (CHAT-016)."""
    return f"{TONE_RULES[style.tone]}\n{LENGTH_RULES[style.conciseness]}"
