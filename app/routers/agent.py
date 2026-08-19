"""
Study Buddy Agent Router

POST /agent/chat    — Sends a message to the personalized AI companion.
GET  /agent/me      — Returns the agent's current state (level, mood, messages remaining).
GET  /agent/nudge   — Returns a proactive nudge message if the user opted in.

Architecture: Hybrid RAG
  - System prompt is built from onboarding data (personalization).
  - Optional currentViewContext from the frontend provides instant grounding.
  - Gemini Function Calling (Tools) enables lazy DB retrieval — the model
    calls the tools only when it determines they are relevant to the question.
  - Privacy Gate: Tools are ONLY attached if the user has enabled
    agent_knowledge_access in their preferences. Otherwise the agent runs
    in standard chat-only mode.
  - All tools are READ-ONLY. No write operations are ever exposed.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import uuid as _uuid
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from app.core.limiter import limiter
from pydantic import BaseModel, field_validator, model_validator

from app.auth.firebase_auth import get_firebase_user
from app.config.database import cards_collection, decks_collection, users_collection
from app.config.subscription_plans import (
    AGENT_MODELS,
    SUBSCRIPTION_PLANS,
    SubscriptionTier,
)
from app.models.quiz import QuizConfig, QuizOffer
from app.utils.agent_tools import (
    get_annual_plan_context,
    get_deck_content,
    get_study_summary,
    get_user_interests,
    list_books,
    list_decks,
    read_book_section,
)

from app.utils.agent_llm import agent_llm, ToolCallRejectedError
from app.core.model_config import TIER_MODEL_NAMES
from app.core.langfuse_client import get_langfuse_client
from langfuse import propagate_attributes
import contextlib
import functools
from app.models.agent_models import GeneratePersonalityRequest, GeneratePersonalityResponse
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import os
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/agent",
    tags=["agent"],
    dependencies=[Depends(get_firebase_user)],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str           # "user" | "model"
    content: str


class ScreenContextPayload(BaseModel):
    page: Literal['study_session', 'book', 'annual_planning', 'dashboard']
    # study_session fields
    deck_id: Optional[str] = None
    deck_name: Optional[str] = None
    card_index: Optional[int] = None
    total_cards: Optional[int] = None
    card_type: Optional[str] = None      # 'basic' | 'cloze' | 'quiz' | 'visual'
    is_flipped: Optional[bool] = None
    front: Optional[str] = None
    back: Optional[str] = None           # None when is_flipped is False
    is_daily_review: Optional[bool] = None
    mode: Optional[Literal['study', 'browse']] = None
    # book fields
    book_id: Optional[str] = None
    book_title: Optional[str] = None
    chapter_title: Optional[str] = None
    visible_text: Optional[str] = None   # max 500 chars

    @field_validator('visible_text')
    @classmethod
    def truncate_visible_text(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > 3000:
            return v[:3000]
        return v

    @field_validator('back')
    @classmethod
    def strip_back_if_not_flipped(cls, v: Optional[str], info) -> Optional[str]:
        # Security: never trust client to hide the answer — enforce server-side
        if info.data.get('is_flipped') is False and v is not None:
            return None
        return v


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    # Structured screen context from the frontend (replaces the old Optional[str])
    context: Optional[ScreenContextPayload] = None
    language: str = 'en'   # BCP 47 language code (e.g. 'es', 'fr-CA')


class AgentStateResponse(BaseModel):
    level: int
    mood: str
    cards_reviewed: int
    messages_used: int
    messages_limit: int
    preferred_name: str
    tier: str
    knowledge_access_enabled: bool
    proactive_nudging_enabled: bool
    current_xp: int = 0
    xp_for_next_level: int = 50
    current_stage: int = 1
    pet_name: str | None = None
    pet_species: str | None = None
    pet_color: str | None = None
    avatar_url: Optional[str] = None
    avatar_stage: Optional[int] = None
    avatar_regen_pending: bool = False
    animation_url: Optional[str] = None
    animation_stage: Optional[int] = None
    animation_regen_pending: bool = False
    agent_roaming_enabled: bool = True
    agent_intervention_frequency: str = 'balanced'
    agent_focus_mode: bool = False
    agent_intervention_wrong_answer: bool = True
    agent_intervention_session_summary: bool = True
    agent_intervention_pre_session: bool = True
    agent_intervention_re_engagement: bool = True
    agent_intervention_streak_milestone: bool = True


class ChatResponse(BaseModel):
    reply: str
    mood: str
    messages_used: int
    messages_limit: int
    level_up: bool = False
    new_level: int = 1
    new_stage: int = 1
    avatar_regen_pending: bool = False
    # Populated when the LLM detects quiz intent. The frontend uses this to
    # automatically navigate the user into the appropriate quiz mode.
    quiz_config: QuizConfig | None = None
    # Populated when the LLM's reply was an informational answer to an
    # ambiguous study/help request. The frontend renders this as a
    # "Want a quiz on <topic>?" follow-up offer (no auto-navigation).
    quiz_offer: QuizOffer | None = None


class GenerateAvatarRequest(BaseModel):
    trigger: Literal["manual", "evolution"] = "manual"


class GenerateAvatarResponse(BaseModel):
    avatar_url: str
    avatar_stage: int
    generated_at: str
    generations_remaining: int
    trigger: str = "manual"


class GenerateAnimationRequest(BaseModel):
    trigger: Literal["manual", "evolution"] = "manual"


class GenerateAnimationResponse(BaseModel):
    animation_url: str
    avatar_stage: int
    generated_at: str
    generations_remaining: int
    trigger: str = "manual"


class NudgeResponse(BaseModel):
    nudge: Optional[str] = None
    has_nudge: bool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_tier(user: dict) -> SubscriptionTier:
    """Extract the subscription tier from the user document safely."""
    raw_tier = user.get("subscription", {}).get("tier", "free")
    try:
        return SubscriptionTier(raw_tier)
    except ValueError:
        return SubscriptionTier.FREE


def _get_agent_prefs(user: dict) -> tuple[bool, bool, str, str]:
    """Return (knowledge_access_enabled, proactive_nudging_enabled, conciseness, tone)."""
    agent_prefs = user.get("preferences", {}).get("agent", {})
    return (
        bool(agent_prefs.get("knowledge_access", False)),
        bool(agent_prefs.get("proactive_nudging", False)),
        agent_prefs.get("conciseness", "balanced"),
        agent_prefs.get("tone", "friendly"),
    )


def _build_context_injection(
    ctx: Optional[ScreenContextPayload],
    rag_book_context: Optional[str] = None,
) -> str:
    """
    Converts a structured ScreenContextPayload into a natural-language system
    prompt section. Returns an empty string when ctx is None.
    """
    if ctx is None:
        return ""

    if ctx.page == 'study_session':
        position = (
            f"card {ctx.card_index} of {ctx.total_cards}"
            if ctx.card_index is not None and ctx.total_cards is not None
            else "a card"
        )
        session_label = (
            "Daily Review session"
            if ctx.is_daily_review
            else f"'{ctx.deck_name}' deck"
            if ctx.deck_name
            else "a study deck"
        )
        view_side = (
            "FRONT (answer not yet revealed)"
            if not ctx.is_flipped
            else "BACK (answer revealed)"
        )

        content_block = f"Front: {ctx.front}" if ctx.front else ""
        if ctx.is_flipped and ctx.back:
            content_block += f"\nBack: {ctx.back}"

        hint_rule = (
            "\nIMPORTANT: If the user asks for the answer and is_flipped is False, "
            "do NOT reveal it. Guide them with a Socratic hint instead."
        ) if not ctx.is_flipped else ""

        tutor_hint = (
            "\nTUTOR MODE ACTIVE: You have full card content available above. "
            "Answer questions about this card directly from this context — no tool calls needed for this card's content. "
        )
        if ctx.is_flipped and ctx.back:
            if ctx.mode == 'browse':
                # Read-only mode: no grading happens, so never imply the user
                # "answered" anything — mirrors mode_note's constraint below so
                # the two blocks never give the LLM contradictory instructions.
                tutor_hint += (
                    "The back of the card is visible. Enrich the user's understanding "
                    "with examples, mnemonics, and usage patterns from your knowledge."
                )
            else:
                tutor_hint += (
                    "The user has seen the answer. If they answer correctly or ask for more, "
                    "enrich their understanding with examples, mnemonics, and usage patterns from your knowledge."
                )
        else:
            tutor_hint += (
                "The answer is not yet revealed. Guide the user toward it — don't spoil it."
            )

        deck_ref_block = ""
        if ctx.deck_id and ctx.deck_id != 'daily-review':
            deck_ref_block = (
                f"\nActive deck ID: {ctx.deck_id}"
                + (f" ('{ctx.deck_name}')" if ctx.deck_name else "")
                + ". If you need to call get_deck_content for this session, use this deck_id directly — never call it without this value."
            )

        mode_note = (
            "\nMODE NOTE: This is a read-only review — do not mention progress, "
            "when the card comes up again, or whether the answer was right or wrong."
        ) if ctx.mode == 'browse' else ""

        return (
            f"\n\nCURRENT SCREEN CONTEXT:\n"
            f"The user is studying {position} in their {session_label}. "
            f"Card type: {ctx.card_type or 'basic'}. "
            f"They are viewing the {view_side} of the card.\n"
            f"{content_block}"
            f"{deck_ref_block}"
            f"{hint_rule}"
            f"{tutor_hint}"
            f"{mode_note}"
        )

    elif ctx.page == 'book':
        chapter = f", chapter: '{ctx.chapter_title}'" if ctx.chapter_title else ""
        book_text = rag_book_context or ctx.visible_text
        text_block = ""
        if book_text:
            text_block = f"\n\nRelevant book content:\n{book_text}"
        return (
            f"\n\nCURRENT SCREEN CONTEXT:\n"
            f"The user is reading '{ctx.book_title or 'a book'}' (book_id: {ctx.book_id}){chapter}.{text_block}\n"
            f"Use the book content above as your primary context for questions about this topic. "
            f"If the answer is not fully covered in the excerpt, supplement with your general knowledge — never say 'it's not in the book'. "
            f"Always be helpful and educational.\n"
        )

    elif ctx.page == 'annual_planning':
        return "\n\nCURRENT SCREEN CONTEXT:\nThe user is on their Annual Planning page."

    elif ctx.page == 'dashboard':
        return "\n\nCURRENT SCREEN CONTEXT:\nThe user is on the Study Center dashboard."

    return ""


LANGUAGE_NAMES: dict[str, str] = {
    'en': 'English',
    'es': 'Spanish',
    'fr': 'French',
    'de': 'German',
    'ja': 'Japanese',
    'pt': 'Portuguese',
    'it': 'Italian',
    'zh': 'Chinese',
    'ko': 'Korean',
    'ar': 'Arabic',
}


def _build_language_directive(language_code: str) -> str:
    """Returns the fallback-language instruction that complements LANGUAGE MIRRORING.

    The language of the user's message is always the strongest signal (see
    LANGUAGE_MIRRORING_RULES); the interface language passed here is only the
    default when the message language is ambiguous.
    """
    lang: str = LANGUAGE_NAMES.get(language_code.split('-')[0].lower(), 'English')
    return (
        f'\n\nDEFAULT LANGUAGE: The user\'s interface language is set to {lang}. '
        f'Use {lang} ONLY as a fallback — when the language of the user\'s current '
        f'message is ambiguous (e.g. a bare emoji, a proper noun, or a single word '
        f'that exists in several languages). Whenever the message has an identifiable '
        f'language, the LANGUAGE MIRRORING rules take precedence: mirror the language '
        f'the user wrote in.'
    )


TUTOR_RULES = """
TUTOR BEHAVIOR (core teaching identity — always active):

When the user is in a study session (page = study_session in screen context):
1. AFTER A CORRECT ANSWER: Don't just confirm. Immediately enrich:
   - Give 2–3 example sentences using the word/concept in real context
   - Add one memory hook: a mnemonic, etymology, or vivid association
   - For languages: include pronunciation notes, formality level (casual vs formal), or common collocations
   - Keep it tight — this is a study session, not a lecture

2. AFTER A WRONG ANSWER OR STRUGGLE: Be a coach, not a corrector:
   - Acknowledge the attempt ("Good try — easy to mix up")
   - Explain WHY the correct answer is what it is
   - Give one analogy or real-world usage that makes it stick
   - End with a question that helps cement the memory

3. WHEN ASKED FOR EXAMPLES: Always answer from your own knowledge first.
   - You do NOT need to fetch data to provide examples of vocabulary, concepts, or formulas
   - Draw examples from the user's interests and daily life context
   - For language cards: give sentences at natural speech level, not textbook-stilted
   - For science/history cards: give a real-world scenario or historical parallel

4. PROACTIVE ENRICHMENT: After confirming a correct answer, you may (briefly) add:
   - "This word also pairs with..." (collocations)
   - "You'll see this a lot in..." (context of use)
   - "A common mistake is confusing this with..." (contrast)
   - "Interesting fact: ..." (memorable hook)
   Only add ONE of these per card — not all of them. Keep it conversational.

5. SESSION PACING: You are aware of where the user is in their session (card index / total).
   - On the last few cards: acknowledge they're almost done, keep energy up
   - If they're early in a session: be more exploratory
   - Never make them feel behind or overwhelmed

LANGUAGE LEARNING SPECIFICS (apply when primary_topic or card content is a language):
- Always show the target word in the reply when giving examples (bold it or put it in context)
- For Japanese/Chinese/Korean: include romaji/pinyin/romanization unless the user seems advanced
- Distinguish casual vs formal/polite forms when relevant
- Common usage patterns are worth more than rare textbook examples
"""


SMALL_TALK_RULES = """
SMALL TALK & GREETINGS (highest priority — overrides IDENTITY RULES #2/#3 and QUIZ MODE below):
- If the user's message is a simple greeting, farewell, or short social remark
  (e.g. "Hola", "hi", "good morning", "thanks", "how are you", "lol", "ok"), respond
  briefly and naturally to THAT message — a short, warm greeting back. Do NOT pivot
  into a lesson, explanation, or example about the user's primary topic or interests,
  even if earlier conversation history is about those topics.
- In this case: do NOT call the `start_quiz` tool, and do NOT append a
  [[QUIZ_OFFER:...]] marker — regardless of what was discussed earlier in this
  conversation.
- Only bring up study content, the user's interests, or a quiz offer if the user's
  CURRENT message actually asks for help, study, practice, or references that topic.
  A bare greeting is never, by itself, a reason to continue an earlier topic.
"""


LANGUAGE_MIRRORING_RULES = """
LANGUAGE MIRRORING (strict — applies to every reply):
- Write ALL explanations, meta-commentary, encouragement, and follow-up questions
  in the language the user's CURRENT message is written in. The message language is
  the strongest signal — it overrides the interface language setting and the
  language of the study material.
- Study content in the language being studied (example sentences, vocabulary,
  grammar demonstrations) stays in that target language — but EVERY such example
  MUST be immediately followed by a translation into the user's language.
  Example: the user asks in English for examples of a grammar point in the language
  they are studying (e.g. Japanese) → give each example sentence in the target
  language, follow it with its English translation, and write all explanations and
  follow-up questions in English.
- If the user writes their message IN the language being studied, respond fully in
  that language — by writing in it they have opted into immersion, and no
  translations are needed.
- NEVER answer entirely in the target language when the user asked in another
  language: a learner cannot understand an explanation written in the very
  language they are trying to learn.
"""


MARKDOWN_FORMAT_RULES = """
FORMATTING (chat bubble constraints — strict):
- You may use ONLY this markdown subset: **bold**, *italics*, bullet lists,
  numbered lists, and `inline code`.
- NEVER use headers (#, ##, ...), tables, images, horizontal rules (---), or
  fenced code blocks — they break the small chat bubble UI.
- Keep replies compact and chat-sized: short paragraphs and short lists, never
  document-style structure.
"""


QUIZ_TRIGGER_RULES = """
QUIZ MODE — TOOL USE CONTRACT:
- DEFAULT: DO NOT call `start_quiz`. You are a straight-to-the-point helper first:
  answer the user's current message yourself, using CURRENT SCREEN CONTEXT (book
  title, card front/back, etc.) whenever it is relevant. The tool exists ONLY for
  the case where the user explicitly asks to be quizzed or tested — NEVER call it
  as a "helpful" follow-up to a content request, and never instead of answering.
- You have a `start_quiz` tool. Call it with confidence="high" whenever the user's
  message is ANY unambiguous, explicit request to be quizzed, tested, examined, or
  given practice questions — no matter how it's phrased or which language it's in.
  Judge the INTENT, not the exact wording. Both of these grammatical shapes count as
  unambiguous and MUST call the tool:
    (a) verb-imperative requests directed at the assistant: "quiz me on X", "test me",
        "practice with me on X", "examine me on X";
    (b) noun/imperative requests asking for an artifact or session to be produced:
        "make/create/generate a quiz/questionnaire/test/exam about X", "give me some
        practice questions on X", "give me a questionary on X".
  This applies identically across all supported languages (English, Spanish, French,
  German, Japanese, Portuguese, Italian, Chinese, Korean, Arabic) and across any
  synonym for quiz/test/exam/questionnaire/practice/questions. Do not require the
  exact words below — they are illustrative anchors, not a checklist:
    - Spanish: "hazme un cuestionario sobre X", "ponme a prueba", "tómame un examen de X"
    - French: "fais-moi un quiz sur X", "interroge-moi sur X"
    - German: "mach ein Quiz über X", "teste mich zu X"
    - Japanese: "Xのクイズを作って", "テストして", "問題を出して"
    - Portuguese: "me faça um questionário sobre X"
    - Italian: "fammi un quiz su X", "interrogami"
    - Chinese: "给我出一些关于X的题", "考我"
    - Korean: "X에 대한 퀴즈를 내줘"
    - Arabic: "اختبرني في X"
    - English: "quiz me on photosynthesis", "test me on chapter 3", "let's practice
      Spanish verbs", "make a quiz/questionnaire/test about X", "create some practice
      questions on X"
- Examples that NEVER call the tool — answer them directly and completely, as you
  normally would: "explain photosynthesis", "what is chapter 3 about", "tell me about
  Spanish verbs", "can you help me understand X", "give me more examples using における",
  "give me an example sentence with this word", "use this card/word in a phrase or
  sentence", "translate this", "what does this mean".
- CRITICAL DISTINCTION: requests for EXAMPLES, sample sentences, usage
  demonstrations, translations, or explanations are content requests, NOT quiz
  requests — even when they contain "give me", "use", "more", or "practice
  sentences", and even when they reference the current card, book, or a grammar
  point. The test is direction: a quiz request asks YOU to ask THEM questions (or
  to produce a quiz/test/exam/questionnaire artifact); a request for you to SHOW,
  EXPLAIN, or DEMONSTRATE something is never a quiz request. When in doubt,
  answer directly — a wrong direct answer is recoverable, a wrongly launched quiz
  is not.
- Reserve confidence="medium" / [[QUIZ_OFFER:...]] STRICTLY for requests where the
  intent is genuinely unclear between "explain this to me" and "quiz me on this" —
  i.e. there is NO quiz/test/exam/practice/questionnaire verb or noun anywhere in the
  message (e.g. "help me study X", "I want to get better at X"). If the message
  contains any such verb or noun, even in noun form or in a non-English language,
  treat it as confidence="high" and call the tool — do NOT downgrade it to
  [[QUIZ_OFFER:...]] just because the phrasing doesn't match a memorized example.
  For these genuinely ambiguous cases, DO NOT call the tool. Instead:
    1. Answer the underlying question/help request directly and concisely, per your
       conciseness directive.
    2. After your answer, on a NEW final line by itself, append exactly:
       [[QUIZ_OFFER:<topic>]]
       where <topic> is the subject, in the language the user wrote in. This marker
       is invisible to the user — it is parsed and removed by the system. NEVER
       mention this marker in your visible reply, and NEVER describe it to the user.
- When you DO call start_quiz, do not also produce conversational text in the same
  turn — the tool call is your entire turn; the handoff message is generated by the
  system, not by you.
- NEVER use both the tool call AND the [[QUIZ_OFFER:...]] marker in the same turn.
  Pick exactly one path, or neither.
"""


STAGE_PERSONALITY: dict[int, str] = {
    1: (
        "Your current personality style: You are tentative and inquisitive. "
        "Ask at least one follow-up question per reply. Keep sentences short (under 15 words). "
        "End messages with a small question or uncertainty marker."
    ),
    2: (
        "Your current personality style: You are warm and energized. "
        "Mirror the user's enthusiasm when detected. Use bullet lists when helpful. "
        "Reference one of the user's stated interests per session."
    ),
    3: (
        "Your current personality style: You are confident and playful. "
        "Offer one brief unsolicited study tip per conversation. Use analogies freely. "
        "Occasionally acknowledge how far the user has come."
    ),
    4: (
        "Your current personality style: You are strategic and measured. "
        "Prioritize long-term thinking. Every sentence serves a purpose — no filler. "
        "Reference the user's stated study goal when giving advice."
    ),
    5: (
        "Your current personality style: You are philosophical. "
        "Connect concepts across domains. Use Socratic framing by default. "
        "Reference the arc of the user's learning journey."
    ),
    6: (
        "Your current personality style: You are sparse and precise. "
        "Speak only when the contribution is meaningful. Maximum 2 sentences for simple questions. "
        "Treat the user as a peer learner, not a student."
    ),
}


SPECIES_PERSONALITY_HINTS: dict[str, str] = {
    "owl": (
        "You carry the quiet wisdom of a scholar. "
        "You prefer depth over speed, and you gently guide the user toward understanding rather than just answers."
    ),
    "fox": (
        "You are sharp, curious, and socially perceptive. "
        "You often spot the angle others miss, and you ask questions that make the user rethink their assumptions."
    ),
    "cat": (
        "You are creative and independent-minded. "
        "You respond with aesthetic precision — never verbose, always considered — and you have a quiet appreciation for beauty in ideas."
    ),
    "dragon": (
        "You are bold and adventurous. "
        "You tackle difficult material head-on, celebrate the user's courage in attempting hard things, and thrive on challenges."
    ),
    "robot": (
        "You are logical, systematic, and exact. "
        "You break problems into clear steps, cite structure over intuition, and you are most satisfied when things are precisely correct."
    ),
    "star": (
        "You are wonder-driven and expansive in your thinking. "
        "You connect ideas across vast scales — from the atomic to the cosmic — and you love helping the user see the bigger picture."
    ),
    "phoenix": (
        "You are resilient and nurturing. "
        "You acknowledge struggle without dramatizing it, and you focus on recovery, growth, and the user's long-term wellbeing."
    ),
    "crystal": (
        "You are precise and structured. "
        "You value clarity of definition, rigorous categorization, and you help the user build mental models that are as clean as a crystal lattice."
    ),
    "leaf": (
        "You are patient, grounded, and process-oriented. "
        "You value sustainable habits over intensity, and you help the user grow steadily rather than burn brightly and fade."
    ),
    "music": (
        "You are rhythmic and expressive. "
        "You notice patterns and flow, and you help the user find the cadence in their learning — knowing when to push and when to rest."
    ),
}


ANIMATION_MOTION_PROMPTS: dict[str, str] = {
    # Prompts are intentionally explicit about WHICH body parts move.
    # Luma Ray 2 Flash will animate the whole scene unless constrained.
    # Pattern: "only animate X and Y — keep Z, background, and colors perfectly still."
    "owl": (
        "Only the owl's wings move: both wings slowly beat up and down in a smooth, rhythmic flap, "
        "rising gently overhead then lowering back. "
        "The owl's body, head, eyes, beak, feet, and talons remain completely still. "
        "Background is frozen. Colors, lighting, and style do not change. Seamless loop."
    ),
    "phoenix": (
        "Only the phoenix's wings move: both wings sweep up and down in wide, powerful arcs. "
        "Tail feathers gently fan open and close in sync with each wing beat. "
        "The phoenix's body, head, and beak stay completely still. "
        "Background is frozen. Colors, lighting, and style do not change. Seamless loop."
    ),
    "dragon": (
        "Only the dragon's wings move: large wings slowly beat up and down with a deep, powerful stroke, "
        "membrane stretching on the upswing. "
        "The dragon's body, head, neck, tail, and legs remain completely rigid and still. "
        "Background is frozen. Colors, lighting, and style do not change. Seamless loop."
    ),
    "cat": (
        "Only the cat's legs and tail move: all four paws step forward and back in a gentle walking-in-place cycle, "
        "while the tail sways left and right with a slow curl. "
        "The cat's head, ears, body, and face stay completely still. "
        "Background is frozen. Colors, lighting, and style do not change. Seamless loop."
    ),
    "fox": (
        "Only the fox's legs and tail move: all four legs trot in a light, bouncy walking-in-place cycle, "
        "tail wagging gently side to side. "
        "The fox's head, ears, body, and face stay completely still. "
        "Background is frozen. Colors, lighting, and style do not change. Seamless loop."
    ),
    "robot": (
        "Only the robot's legs move: both legs step forward and back in a mechanical marching-in-place cycle, "
        "arms swinging slightly in opposition. "
        "The robot's head, torso, and face remain completely still. "
        "Background is frozen. Colors, lighting, and style do not change. Seamless loop."
    ),
    "crystal": (
        "Only the crystal slowly rotates in place around its vertical axis, "
        "facets catching light as it turns. "
        "No other movement — background is frozen, colors and lighting do not change. Seamless loop."
    ),
    "star": (
        "Only the star gently spins in place around its center, "
        "each point tracing a slow rotation. "
        "No other movement — background is frozen, colors and brightness do not change. Seamless loop."
    ),
    "leaf": (
        "Only the leaf spirit's body and arms sway gently left and right, "
        "like a plant swaying in a light breeze. "
        "Feet stay planted. Head stays upright. "
        "Background is frozen. Colors, lighting, and style do not change. Seamless loop."
    ),
    "music": (
        "Only the music spirit's body bounces up and down in a gentle rhythmic beat, "
        "arms lightly rising and falling with each bounce. "
        "Feet stay planted. Head stays level. "
        "Background is frozen. Colors, lighting, and style do not change. Seamless loop."
    ),
}

AVATAR_STAGE_DESCRIPTORS: dict[int, str] = {
    1: "a tiny newborn hatchling, large curious eyes, small fragile form",
    2: "a young juvenile, energetic and bright-eyed, slightly larger",
    3: "a lively adolescent, confident posture, mid-sized",
    4: "a mature adult, composed and wise-looking, full-sized",
    5: "an elder sage, silver-tinged markings, serene gravitas",
    6: "an ancient legendary being, radiant aura markings, majestic and awe-inspiring",
}

AVATAR_INTEREST_TRAITS: dict[str, list[str]] = {
    "science":     ["wearing a tiny lab coat", "small round goggles pushed up on forehead"],
    "music":       ["floating musical notes orbiting it", "small headphones around neck"],
    "health":      ["small glowing medical cross badge on chest", "fresh green leaf accent"],
    "technology":  ["faint circuit board patterns on body", "small holographic display nearby"],
    "language":    ["small open book floating beside it", "faint foreign script glyphs in background"],
    "art":         ["paintbrush tucked behind ear", "faint watercolor splashes in background"],
    "history":     ["small ancient scroll in one hand", "sepia-tinted background corner detail"],
    "math":        ["glowing geometric shapes floating nearby", "small chalkboard with symbols"],
    "nature":      ["small flower growing from its habitat", "soft leaf and vine environment"],
    "cooking":     ["tiny chef hat", "small steaming bowl beside it"],
}

AVATAR_COLOR_NAMES: dict[str, str] = {
    "ocean":  "deep ocean blue",
    "violet": "rich violet purple",
    "mint":   "soft mint green",
    "gold":   "warm golden amber",
    "rose":   "gentle rose pink",
    "coral":  "vivid coral orange-pink",
    "sky":    "clear sky blue",
    "ember":  "warm ember orange",
}

AVATAR_GOAL_MOODS: dict[str, str] = {
    "general":  "serene",
    "academic": "scholarly",
    "career":   "professional",
    "language": "curious",
    "hobby":    "playful",
}

AVATAR_TOPIC_SCENES: dict[str, str] = {
    "japanese":    ", with a delicate torii gate silhouette in the far background",
    "chinese":     ", with a delicate torii gate silhouette in the far background",
    "korean":      ", with a delicate torii gate silhouette in the far background",
    "biology":     ", surrounded by soft floating cell and leaf illustrations",
    "science":     ", surrounded by soft floating cell and leaf illustrations",
    "chemistry":   ", surrounded by soft floating cell and leaf illustrations",
    "history":     ", on ancient stone steps with faint map illustrations",
    "music":       ", on a small stage with soft spotlight",
    "math":        ", in a warm library with floating geometric shapes",
    "mathematics": ", in a warm library with floating geometric shapes",
    "art":         ", in a bright studio with soft color splashes",
    "programming": ", with faint circuit and code glyphs in the background",
    "coding":      ", with faint circuit and code glyphs in the background",
}

AVATAR_STYLE_SUFFIX = (
    "flat design illustration, game mascot character art, expressive eyes, "
    "soft cel-shading, white background, no text, no watermark, no letters, "
    "studio quality, centered composition"
)


def _build_avatar_prompt(user_doc: dict, stage: int) -> tuple[str, int]:
    """
    Build a unique avatar generation prompt from user preferences.
    Returns (prompt_string, fal_seed_int).
    """
    prefs = user_doc.get("preferences", {})
    pet = prefs.get("pet", {})
    general = prefs.get("general", {})

    species = pet.get("pet_species") or "owl"
    interests = [i.lower() for i in (general.get("interests") or [])[:2]]
    color_slug = pet.get("pet_color") or "violet"
    study_goal = general.get("study_goal") or "general"
    primary_topic = (general.get("primary_topic") or "").lower()
    full_name = user_doc.get("full_name") or user_doc.get("username") or ""
    avatar_seed = pet.get("avatar_seed") or str(_uuid.uuid4())

    # Build interest traits (max 4 total, 2 per interest)
    trait_parts: list[str] = []
    for interest in interests:
        for key, traits in AVATAR_INTEREST_TRAITS.items():
            if key in interest:
                trait_parts.extend(traits[:2])
                break
    trait_text = ", ".join(trait_parts[:4])

    # Stage descriptor
    stage_desc = AVATAR_STAGE_DESCRIPTORS.get(stage, AVATAR_STAGE_DESCRIPTORS[1])

    # Color name
    color_name = AVATAR_COLOR_NAMES.get(color_slug, "violet purple")

    # Goal mood
    goal_mood = AVATAR_GOAL_MOODS.get(study_goal, "serene")

    # Topic scene
    scene = ""
    for keyword, scene_text in AVATAR_TOPIC_SCENES.items():
        if keyword in primary_topic:
            scene = scene_text
            break

    # Initials for uniqueness
    name_parts = full_name.strip().split()
    initials = "".join(p[0].upper() for p in name_parts if p)[:3] or "NW"

    # Build prompt
    parts_list = [
        f"{stage_desc} {species} companion",
        trait_text,
        f"{color_name} color accents throughout",
        f"{goal_mood} atmosphere",
    ]
    if scene:
        parts_list.append(scene.strip(", "))
    parts_list.append(f"a unique companion known only to {initials}")
    parts_list.append(AVATAR_STYLE_SUFFIX)

    prompt = ", ".join(p for p in parts_list if p)

    # Convert UUID seed to 32-bit int
    try:
        seed_int = int(_uuid.UUID(avatar_seed).int % (2 ** 32))
    except (ValueError, AttributeError):
        seed_int = 42

    return prompt, seed_int


async def _call_fal_avatar(prompt: str, seed: int) -> str:
    """
    Call fal.ai FLUX Pro to generate an avatar, then upload it to Cloudinary.
    Returns a permanent Cloudinary HTTPS URL.
    Raises RuntimeError on failure.
    """
    from app.utils.storage import get_storage_backend

    fal_key = os.environ.get("FAL_KEY", "")
    if not fal_key:
        raise RuntimeError("FAL_KEY environment variable not set")

    payload = {
        "prompt": prompt,
        "seed": seed,
        "image_size": "square_hd",
        "num_inference_steps": 28,
        "guidance_scale": 3.5,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://fal.run/fal-ai/flux-pro/v1.1",
            json=payload,
            headers={
                "Authorization": f"Key {fal_key}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"fal.ai returned {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        fal_image_url = data["images"][0]["url"]

        # Download from fal.ai (temporary URL)
        img_resp = await client.get(fal_image_url, timeout=30.0)
        if img_resp.status_code != 200:
            raise RuntimeError(f"Failed to download image: {img_resp.status_code}")

        image_bytes = img_resp.content

    # Upload to Cloudinary for a permanent HTTPS URL
    storage = get_storage_backend(os.getenv("STORAGE_BACKEND", "cloudinary"))
    result = await storage.upload(
        file_content=image_bytes,
        filename=f"pet_avatar_{seed}",
        folder="nowry/pet_avatars",
    )
    secure_url: str = result.get("secure_url") or result.get("url")
    if not secure_url:
        raise RuntimeError("Cloudinary upload returned no URL")
    return secure_url


async def _call_fal_animation(image_url: str, motion_prompt: str, seed: int = 0) -> str:
    """
    Call fal.ai Luma Ray 2 Flash to generate a looping animation from a portrait.
    Uploads the result to Cloudinary for permanent storage.
    Returns a permanent Cloudinary HTTPS video URL.
    Raises RuntimeError on failure.

    image_url must be a publicly accessible HTTPS URL (Cloudinary portrait URL).
    """
    from app.utils.storage import CloudinaryStorage

    fal_key = os.environ.get("FAL_KEY", "")
    if not fal_key:
        raise RuntimeError("FAL_KEY environment variable not set")

    payload = {
        "prompt": motion_prompt,
        "image_url": image_url,
        "loop": True,
        "duration": "5s",
        "aspect_ratio": "4:3",
        "seed": seed,
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            "https://fal.run/fal-ai/luma-dream-machine/ray-2-flash/image-to-video",
            json=payload,
            headers={
                "Authorization": f"Key {fal_key}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"fal.ai Luma returned {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        # Luma returns { "video": { "url": "https://..." } }
        luma_video_url: str = data["video"]["url"]

        # Download from Luma (temporary CDN URL)
        video_resp = await client.get(luma_video_url, timeout=60.0)
        if video_resp.status_code != 200:
            raise RuntimeError(f"Failed to download Luma video: {video_resp.status_code}")

        video_bytes = video_resp.content

    # Upload to Cloudinary as a video resource for permanent storage
    storage = CloudinaryStorage()
    result = await storage.upload_video(
        file_content=video_bytes,
        folder="nowry/pet_animations",
        public_id=f"pet_animation_{seed}",
        overwrite=True,
    )
    secure_url: str = result.get("secure_url") or result.get("url")
    if not secure_url:
        raise RuntimeError("Cloudinary video upload returned no URL")
    return secure_url


def _build_system_prompt(
    user: dict,
    knowledge_access: bool,
    conciseness: str = "balanced",
    tone: str = "friendly",
    screen_context: Optional[ScreenContextPayload] = None,
    stage: int = 1,
    language: str = 'en',
    pet_name: Optional[str] = None,
    pet_species: Optional[str] = None,
    rag_book_context: Optional[str] = None,
) -> str:
    """
    Construct a rich system prompt that makes the pet feel like it was
    born from the user's own data. All values come from the onboarding
    wizard fields stored in preferences.general and preferences.agent.
    """
    prefs = user.get("preferences", {}).get("general", {})
    preferred_name = user.get("full_name") or user.get("username", "there")
    primary_topic = prefs.get("primary_topic") or "general knowledge"
    interests = prefs.get("interests", [])
    study_goal = prefs.get("study_goal", "general")

    interests_text = ", ".join(interests) if interests else "various topics"

    study_goal_map = {
        "general":  "expand their general knowledge",
        "academic": "excel academically",
        "career":   "advance their career",
        "language": "master a new language",
        "hobby":    "deepen a personal passion",
    }
    goal_description = study_goal_map.get(study_goal, "learn effectively")

    # ── Conciseness directive ──────────────────────────────────────────────
    conciseness_rules = {
        "concise": (
            "RESPONSE LENGTH: Be extremely brief. 1–2 sentences maximum per reply. "
            "Skip all preamble, headers, and bullet lists unless absolutely essential. "
            "Every word must earn its place."
        ),
        "balanced": (
            "RESPONSE LENGTH: Keep replies concise but complete — 2–4 sentences for simple questions, "
            "a short paragraph or bullet list for complex ones. Never pad your answer."
        ),
        "detailed": (
            "RESPONSE LENGTH: The user wants thorough explanations. "
            "Use structured replies with clear sections, examples, and analogies. "
            "Anticipate follow-up questions and address them proactively."
        ),
    }
    conciseness_directive = conciseness_rules.get(conciseness, conciseness_rules["balanced"])

    # ── Tone directive ─────────────────────────────────────────────────────
    tone_rules = {
        "friendly": (
            "TONE: Warm, encouraging, and conversational. Use the user's first name naturally. "
            "Celebrate small wins. Add occasional light humor or emoji where it feels natural."
        ),
        "professional": (
            "TONE: Polished and precise. Communicate like a knowledgeable academic tutor. "
            "No emoji, no filler words. Focus on clarity and accuracy above all."
        ),
        "strict": (
            "TONE: Direct and firm. If the user has cards due or goals falling behind, say it plainly. "
            "Do not soften feedback. Prioritize accountability over comfort. "
            "Celebrate real achievements only — no empty encouragement."
        ),
        "socratic": (
            "TONE: Never give the answer directly. Always respond to factual questions with a guiding question "
            "that helps the user arrive at the answer themselves. "
            "Only confirm or correct after they have attempted an answer. "
            "Exception: for progress/stats queries, still state the facts clearly."
        ),
    }
    tone_directive = tone_rules.get(tone, tone_rules["friendly"])

    tool_instructions = ""
    if knowledge_access:
        tool_instructions = """

KNOWLEDGE ACCESS RULES (you have tools — use them wisely):
- When the user asks about their study progress, ALWAYS call get_study_summary first.
- When the user asks "what books do I have" or "what decks do I have", call list_books or list_decks.
- When the user wants to deep-dive into a SPECIFIC DECK and no card context is already available in CURRENT SCREEN CONTEXT, call get_deck_content.
- CRITICAL: If CURRENT SCREEN CONTEXT already contains the card text (front/back), DO NOT call get_deck_content — you already have the information. Answer directly from the context.
- CRITICAL: NEVER call get_deck_content when the user asks to study or quiz on a topic (e.g. "study Japanese verbs", "quiz me on history"). Only call it when the user explicitly names one of their own decks by name. Never guess or fabricate a deck_id — if you are unsure of the deck_id, call list_decks first. When quiz intent is detected, respond with one brief sentence and let the quiz system handle question generation.
- When the user asks about their goals or annual plan, call get_annual_plan_context.
- NEVER call get_user_interests — the user's interests, primary topic, and study goal are already present in this system prompt. Calling this tool mid-conversation is redundant and breaks the flow.
- You MUST NEVER attempt to create, edit, or delete any data. You are read-only.
- Tell the user to go to the Study Session to review cards — you cannot mark cards for them.
- After calling a tool, synthesize the result naturally into your reply. Never dump raw data.
"""
    else:
        tool_instructions = """

KNOWLEDGE ACCESS (disabled):
- You do NOT have access to the user's library, decks, or goals.
- If they ask about specific content, kindly let them know they can enable
  "AI Knowledge Access" in Settings to give you that ability.
"""

    base_prompt = f"""You are the Nowry Study Buddy — a warm, curious, and deeply personalized AI
learning companion. You are NOT a generic assistant. You were created specifically for {preferred_name}.

IDENTITY RULES (never break these):
1. Always call the user "{preferred_name}" — never a generic "there" or "user".
2. Your personality mirrors their interests: {interests_text}.
   Use analogies, examples, and references from these fields whenever possible.
3. Their primary learning focus is: {primary_topic}.
   Prioritize this domain when generating examples or explanations.
4. Their study goal is to {goal_description}.
   Keep this goal in mind for motivation and long-term encouragement.

STYLE DIRECTIVES (highest priority — override default behavior):
{conciseness_directive}
{tone_directive}

{SMALL_TALK_RULES}
BEHAVIORAL RULES:
- You are encouraging but honest — never give empty praise.
- If the user is struggling with a concept, use a concrete analogy from their interests.
- Never break character or refer to yourself as an AI model. You are their Study Buddy.
- React naturally: celebrate milestones, gently nudge after inactivity.

{TUTOR_RULES}
{QUIZ_TRIGGER_RULES}
{LANGUAGE_MIRRORING_RULES}
{MARKDOWN_FORMAT_RULES}
{tool_instructions}"""
    identity_block = ""
    if pet_name:
        identity_block += (
            f"Your name is {pet_name}. "
            f"Always refer to yourself as {pet_name}. "
            "Never call yourself 'Study Buddy' or any other name.\n"
        )
    if pet_species and pet_species in SPECIES_PERSONALITY_HINTS:
        identity_block += SPECIES_PERSONALITY_HINTS[pet_species] + "\n"

    personality_block = STAGE_PERSONALITY.get(stage, STAGE_PERSONALITY[1])
    return (
        identity_block
        + base_prompt
        + f"\n\n{personality_block}"
        + _build_context_injection(screen_context, rag_book_context=rag_book_context)
        + _build_language_directive(language)
    )


def _calculate_level(xp: int) -> int:
    """Smooth square-root levelling curve.
    Level 2 starts at 50 XP (one day of light study), Level 10 at ~4,050 XP.
    """
    return max(1, math.floor(math.sqrt(max(0, xp) / 50)) + 1)


def _level_to_stage(level: int) -> int:
    """Map a level number to an evolution stage (1–6)."""
    if level >= 30:
        return 6
    if level >= 20:
        return 5
    if level >= 15:
        return 4
    if level >= 10:
        return 3
    if level >= 5:
        return 2
    return 1


def _xp_for_level(level: int) -> int:
    """Return the total XP required to *reach* a given level."""
    return 50 * (level - 1) ** 2


def _xp_for_next_level(xp: int) -> int:
    """Return the remaining XP needed to reach the next level."""
    current_level = _calculate_level(xp)
    next_level_xp = _xp_for_level(current_level + 1)
    return max(0, next_level_xp - xp)


async def grant_xp(user_id: str, amount: int) -> dict:
    """Atomically increment the user's XP in MongoDB and return level-up data."""
    try:
        user_doc = await users_collection.find_one(
            {"_id": ObjectId(user_id)},
            {"agent.xp": 1, "preferences.pet.avatar_stage": 1, "preferences.pet.avatar_url": 1},
        )
        xp_before: int = (user_doc or {}).get("agent", {}).get("xp", 0) if user_doc else 0
        level_before: int = _calculate_level(xp_before)

        await users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {"$inc": {"agent.xp": amount}},
        )

        xp_after: int = xp_before + amount
        level_after: int = _calculate_level(xp_after)
        level_up: bool = level_after > level_before
        new_stage: int = _level_to_stage(level_after)

        avatar_regen_pending_flag: bool = False
        if level_up and user_doc:
            stored_avatar_stage = user_doc.get("preferences", {}).get("pet", {}).get("avatar_stage")
            stored_avatar_url = user_doc.get("preferences", {}).get("pet", {}).get("avatar_url")
            if stored_avatar_url and stored_avatar_stage and new_stage > stored_avatar_stage:
                await users_collection.update_one(
                    {"_id": ObjectId(user_id)},
                    {"$set": {
                        "preferences.pet.avatar_regen_pending": True,
                        "preferences.pet.animation_regen_pending": True,
                    }}
                )
                avatar_regen_pending_flag = True

        return {
            "level_up": level_up,
            "new_level": level_after,
            "new_stage": new_stage,
            "avatar_regen_pending": avatar_regen_pending_flag,
        }
    except Exception:
        return {"level_up": False, "new_level": 1, "new_stage": 1, "avatar_regen_pending": False}


def _calculate_mood(user: dict, cards_reviewed: int) -> str:
    last_reviewed = user.get("last_study_date")
    if last_reviewed:
        days_inactive = (datetime.now(timezone.utc) - last_reviewed).days
        if days_inactive >= 3:
            return "tired"
    if cards_reviewed == 0:
        return "idle"
    return "happy"


# ---------------------------------------------------------------------------
# Gemini Function Calling — Tool Definitions & Dispatcher
# ---------------------------------------------------------------------------

# These are the Gemini tool declarations. The model reads these to know
# what functions are available and when to call them.
KNOWLEDGE_TOOLS = [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="get_study_summary",
                description="Retrieve current user study statistics (due/new card counts, active decks). Mandatory for progress queries.",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
            genai.protos.FunctionDeclaration(
                name="list_decks",
                description="Retrieve list of all user decks with metadata (mastery %, total cards). Use for library overviews.",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
            genai.protos.FunctionDeclaration(
                name="get_deck_content",
                description=(
                    "Fetch raw flashcard content (front/back text) from a specific deck. "
                    "PREREQUISITE: deck_id is REQUIRED and must NEVER be omitted or left empty. "
                    "If CURRENT SCREEN CONTEXT contains an 'Active deck ID', use that value directly. "
                    "If no deck_id is available in context, call list_decks first to obtain one. "
                    "Only call this when the user references one of their own decks — never for generic topics or subjects."
                ),
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "deck_id": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description=(
                                "The ID of the deck to retrieve content from. "
                                "REQUIRED — do not call this tool if you do not have a deck_id. "
                                "Use the 'Active deck ID' from CURRENT SCREEN CONTEXT when available."
                            )
                        ),
                    },
                    required=["deck_id"],
                ),
            ),
            genai.protos.FunctionDeclaration(
                name="list_books",
                description="List all books in the user's library with title, author, and summary. Call this when the user asks what books they have.",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
            genai.protos.FunctionDeclaration(
                name="read_book_section",
                description=(
                    "Read a section of a specific book from the user's library. "
                    "PREREQUISITE: book_id is REQUIRED and must be a MongoDB ObjectId hex string — NEVER the book title. "
                    "If CURRENT SCREEN CONTEXT contains an 'Active book ID', use that value directly. "
                    "If no book_id is available in context, call list_books first to obtain the correct ID, then retry."
                ),
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "book_id": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description=(
                                "The MongoDB ObjectId of the book (a 24-character hex string). "
                                "REQUIRED — use the 'Active book ID' from CURRENT SCREEN CONTEXT when available. "
                                "NEVER use the book title as the book_id."
                            )
                        ),
                        "query": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="An optional topic or keyword to search for within the book."
                        ),
                    },
                    required=["book_id"],
                ),
            ),
            genai.protos.FunctionDeclaration(
                name="get_annual_plan_context",
                description="Get the user's annual plan: focus areas, goals, and milestones. Call this when the user asks about their annual goals, objectives, or long-term plans.",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
            genai.protos.FunctionDeclaration(
                name="get_user_interests",
                description="Get the user's interests, primary learning topic, and study goal. Call this for recommendation queries.",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={},
                    required=[],
                ),
            ),
        ]
    )
]


# Quiz-triggering tool — always attached, independent of knowledge_access.
# Mirrors the KNOWLEDGE_TOOLS Gemini Tool proto pattern above.
QUIZ_TOOLS = [
    genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="start_quiz",
                description=(
                    "Launch an interactive quiz session for the user. Call this with "
                    "confidence='high' whenever the user explicitly and unambiguously asks "
                    "to be quizzed, tested, examined, or given practice questions/a "
                    "questionnaire — in ANY phrasing or supported language. This includes "
                    "both verb-imperative requests aimed at the assistant (e.g. 'quiz me on "
                    "X', 'test me', 'let's practice Y', 'examine me on X') AND noun/imperative "
                    "requests asking for an artifact or session (e.g. 'make/create/generate a "
                    "quiz/questionnaire/test/exam about X', 'give me some practice questions on "
                    "X', 'give me a questionary on X'). Judge intent, not exact wording — this "
                    "applies equally to non-English requests (e.g. Spanish 'hazme un "
                    "cuestionario sobre X' / 'ponme a prueba', French 'fais-moi un quiz sur X', "
                    "German 'mach ein Quiz über X', Japanese 'Xのクイズを作って', Chinese "
                    "'给我出一些关于X的题' / '考我', etc.). "
                    "Do NOT call this for informational or content requests like "
                    "'explain X', 'tell me about X', 'what is X', 'help me understand X', "
                    "'give me (more) examples using X', 'give me an example sentence', "
                    "'use this card/word in a phrase or sentence', or 'translate this' — "
                    "those are answered directly with text, never with this tool. A "
                    "request for you to SHOW, EXPLAIN, or DEMONSTRATE something (examples, "
                    "sample sentences, translations) is never a quiz request, even if it "
                    "contains words like 'give me' or 'use'. "
                    "Only if the user's phrasing contains NO quiz/test/exam/practice/"
                    "questionnaire verb or noun at all, and is genuinely ambiguous between "
                    "'explain this' and 'quiz me' (e.g. 'help me study X' could mean either), "
                    "do NOT call this tool — instead, answer their question directly, then on "
                    "a new final line append [[QUIZ_OFFER:<topic>]] so the UI can offer a quiz "
                    "as a follow-up."
                ),
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "mode": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="'ai' for a topic-based quiz, 'deck' if the user names one of their own decks by name.",
                        ),
                        "topic": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="The subject to quiz on, in the language the user wrote in. Required for mode='ai'. For mode='deck', the deck name.",
                        ),
                        "question_count": genai.protos.Schema(
                            type=genai.protos.Type.INTEGER,
                            description="Number of questions requested. Omit to use the configured default. Clamp 1-20 if the user specifies a number.",
                        ),
                        "confidence": genai.protos.Schema(
                            type=genai.protos.Type.STRING,
                            description="'high' if the user's message contains ANY explicit request to be quizzed/tested/examined/practiced or to be given a quiz/questionnaire/test/exam/practice questions — in any phrasing (verb-imperative OR noun-form) and any supported language (auto-launch, no confirmation). 'medium' ONLY if the message has no such verb or noun at all and is genuinely ambiguous between an explanation and a quiz (e.g. 'help me study X') — do not actually call the tool in that case, use [[QUIZ_OFFER:...]] instead.",
                        ),
                    },
                    required=["mode", "confidence"],
                ),
            ),
        ]
    )
]


# ── Deterministic server-side quiz gate ─────────────────────────────────────
# Prompt steering alone proved unreliable: both Llama and gpt-oss on Groq
# pattern-match study-flavored content requests ("give me an example using this
# card") into start_quiz with self-reported confidence="high", ignoring the
# never-call rules. This gate re-validates intent server-side with a tiny,
# language-agnostic secondary LLM call over ONLY the user's last message, and
# it runs ONLY when the model actually attempts start_quiz — zero added latency
# on normal turns.
_QUIZ_INTENT_GATE_PROMPT = (
    "You are a strict binary intent classifier. Reply with exactly one word: YES or NO.\n"
    "QUESTION: Does the user's message EXPLICITLY ask to be quizzed, tested, or "
    "examined, or explicitly ask for a quiz/test/exam/questionnaire/practice-question "
    "session to be created or started?\n"
    "The message may be in any language — judge the intent of the message itself.\n"
    "Reply NO for requests for examples, sample sentences, usage demonstrations, "
    "explanations, translations, definitions, or any request for the assistant to "
    "show, explain, or demonstrate something (e.g. 'give me an example using this "
    "card', 'use this word in a sentence', 'give me more examples of X').\n"
    "Reply YES only when the user asks to be asked questions, or asks for a "
    "quiz/test/exam to be produced or started (e.g. 'quiz me', 'test me', 'make a "
    "quiz about X').\n"
    "Reply with one word only: YES or NO."
)


async def _verify_explicit_quiz_intent(user_message: Optional[str]) -> Optional[bool]:
    """Language-agnostic check: does this message explicitly ask for a quiz?

    Returns True/False from the classifier, or None when no verdict is
    available (no message threaded, classifier unavailable, or unparseable
    output). Policy for None is decided at the call site: fail-open, so a
    transient classifier failure can never block a legitimate "quiz me".
    """
    if not user_message or not user_message.strip():
        return None
    return await agent_llm.intent_yes_no(_QUIZ_INTENT_GATE_PROMPT, user_message)


async def _dispatch_tool_call(
    fn_name: str,
    fn_args: dict,
    user_id: str,
    client=None,
    default_question_count: int = 10,
    quiz_result_holder: Optional[dict] = None,
    user_message: Optional[str] = None,
) -> str:
    """
    Executes the tool function requested by the model and returns the
    result as a JSON string to feed back into the conversation.

    When `client` (a Langfuse client, D-11) is provided, wraps the dispatch in a
    nested `as_type="tool"` span that automatically nests under the calling
    chat()'s smart_pet_chat generation via OTel context — no manual parent-id
    wiring required.

    `default_question_count` and `quiz_result_holder` support the `start_quiz`
    tool (see `_dispatch_tool_call_inner`): the holder is a mutable side-channel
    dict that `chat()` inspects after the LLM call returns, since the LLM call's
    return signature itself is not changed.
    """
    span_cm = contextlib.nullcontext()
    if client:
        try:
            span_cm = client.start_as_current_observation(
                name=f"tool:{fn_name}",
                as_type="tool",
                input=fn_args,
            )
        except Exception as langfuse_exc:
            logger.warning(
                f"[Tool] Langfuse tracing failed for tool:{fn_name}, continuing without trace: {langfuse_exc}"
            )
            span_cm = contextlib.nullcontext()

    with span_cm as tool_span:
        # ── The ONE tool-execution call — executes exactly once ─────────────
        # A ToolCallRejectedError raised here propagates to the provider loop
        # in agent_llm, which re-runs the turn without the rejected tool.
        result_str = await _dispatch_tool_call_inner(
            fn_name, fn_args, user_id, default_question_count, quiz_result_holder,
            user_message=user_message,
        )

        if tool_span is not None:
            try:
                # D-13: full tool result, no truncation
                tool_span.update(output=result_str)
            except Exception as langfuse_exc:
                logger.warning(
                    f"[Tool] Langfuse tracing failed for tool:{fn_name}, continuing without trace: {langfuse_exc}"
                )

        return result_str


async def _dispatch_tool_call_inner(
    fn_name: str,
    fn_args: dict,
    user_id: str,
    default_question_count: int = 10,
    quiz_result_holder: Optional[dict] = None,
    user_message: Optional[str] = None,
) -> str:
    """
    Executes the tool function requested by the model and returns the
    result as a JSON string to feed back into the conversation.

    Raises ToolCallRejectedError (never converted to a result string) when the
    server-side quiz gate vetoes a start_quiz call — the provider loop re-runs
    the turn without the tool so the user gets a direct answer.
    """
    try:
        if fn_name == "get_study_summary":
            result = await get_study_summary(user_id)
        elif fn_name == "list_decks":
            result = await list_decks(user_id)
        elif fn_name == "get_deck_content":
            deck_id_arg = fn_args.get("deck_id", "").strip()
            if not deck_id_arg:
                logger.warning("[Tool] get_deck_content called with empty deck_id — returning error to LLM")
                result = json.dumps({"error": "deck_id is required. Call list_decks first to get a valid deck_id, then retry get_deck_content with it."})
            else:
                result = await get_deck_content(user_id, deck_id_arg)
        elif fn_name == "list_books":
            result = await list_books(user_id)
        elif fn_name == "read_book_section":
            book_id_arg = fn_args.get("book_id", "").strip()
            if not book_id_arg or len(book_id_arg) != 24 or not all(c in "0123456789abcdefABCDEF" for c in book_id_arg):
                logger.warning(f"[Tool] read_book_section called with invalid book_id='{book_id_arg}' — likely a title, not an ObjectId")
                result = json.dumps({"error": "book_id must be a 24-character MongoDB ObjectId hex string, not a book title. Call list_books first to get the correct book_id, then retry read_book_section."})
            else:
                result = await read_book_section(
                    user_id,
                    book_id_arg,
                    fn_args.get("query", ""),
                )
        elif fn_name == "get_annual_plan_context":
            result = await get_annual_plan_context(user_id)
        elif fn_name == "get_user_interests":
            result = await get_user_interests(user_id)
        elif fn_name == "start_quiz":
            mode = fn_args.get("mode", "ai")
            confidence = fn_args.get("confidence", "medium")
            if confidence != "high":
                logger.warning(f"[start_quiz] Rejected non-high-confidence tool call: {fn_args}")
                result = {
                    "error": "Ambiguous quiz request. Do not call start_quiz; answer the "
                             "question directly and append [[QUIZ_OFFER:<topic>]] instead."
                }
            else:
                # Deterministic server-side gate: the model's self-reported
                # confidence is not trusted. Re-validate against the user's
                # actual message; only an explicit quiz/test request passes.
                # None (classifier unavailable) fails open so a transient
                # classifier failure never blocks a legitimate "quiz me".
                intent_ok = await _verify_explicit_quiz_intent(user_message)
                if intent_ok is False:
                    logger.warning(
                        "[start_quiz] Server-side intent gate rejected tool call "
                        "(model claimed confidence=high): %s", fn_args,
                    )
                    raise ToolCallRejectedError(
                        "start_quiz",
                        "The user's message does not explicitly ask to be "
                        "quizzed or tested.",
                    )
                raw_count = fn_args.get("question_count", default_question_count)
                try:
                    q_count = max(1, min(20, int(raw_count)))
                except (TypeError, ValueError):
                    q_count = default_question_count
                quiz_config_dict = {
                    "mode": mode if mode in ("ai", "deck") else "ai",
                    "topic": fn_args.get("topic") or None,
                    "question_count": q_count,
                    "deck_id": None,
                }
                if quiz_result_holder is not None:
                    quiz_result_holder["quiz_config"] = quiz_config_dict
                result = {"status": "launching", "quiz_config": quiz_config_dict}
        else:
            result = {"error": f"Unknown tool: {fn_name}"}
    except ToolCallRejectedError:
        # Control-flow signal for the provider loop — must NOT be converted
        # into a tool-result string, or the gate would be silently bypassed.
        raise
    except Exception as exc:
        result = {"error": f"Tool execution failed: {str(exc)}"}

    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Smart Pet — Personality generation constants
# ---------------------------------------------------------------------------

_PERSONALITY_SYSTEM_PROMPT = (
    "You are a personality writer for an AI study companion app. "
    "Generate a vivid, coherent personality description for an AI pet companion. "
    "Output exactly 3-5 sentences describing HOW the companion speaks, thinks, and reacts. "
    "Do not describe appearance. Do not include harmful, offensive, or manipulative content. "
    "Do not break the fourth wall or reference being an AI. "
    "Output only the personality description — no preamble, no labels, no quotation marks."
)

PERSONALITY_LIMITS = {SubscriptionTier.PLUS: 1, SubscriptionTier.PRO: 3}

PERSONALITY_SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
}

_PERSONA_BREAK_SIGNALS = ("As an AI", "language model", "I am programmed", "I cannot assist as")


def _build_quiz_handoff_message(topic_label: str, language: str) -> str:
    """
    Build the short handoff reply shown when the agent's `start_quiz` tool call
    is accepted (confidence="high"). Mirrors the original per-language copy that
    used to live inline in `chat()`.
    """
    lang_prefix: str = language.split("-")[0].lower()
    handoff_messages: dict[str, str] = {
        "es": f"¡Perfecto! Iniciando tu quiz sobre {topic_label}…",
        "fr": f"Parfait ! Lancement du quiz sur {topic_label}…",
        "de": f"Super! Ich starte dein Quiz über {topic_label}…",
        "pt": f"Ótimo! Iniciando seu quiz sobre {topic_label}…",
        "ja": f"{topic_label}のクイズを始めます！",
        "zh": f"好的！正在启动关于{topic_label}的测验…",
        "ko": f"좋아요! {topic_label} 퀴즈를 시작합니다…",
        "it": f"Perfetto! Avvio il quiz su {topic_label}…",
        "ar": f"رائع! جارٍ بدء الاختبار حول {topic_label}…",
    }
    return handoff_messages.get(lang_prefix, f"Let's go! Starting your quiz on {topic_label}…")


# ---------------------------------------------------------------------------
# Smart Pet — Persistent history helpers (Plus/Pro only)
# ---------------------------------------------------------------------------


async def _load_persistent_history(user_id: str, limit: int = 20) -> list[dict]:
    """Load last `limit` turns from MongoDB agent.chat_history (Plus/Pro only)."""
    doc = await users_collection.find_one(
        {"_id": ObjectId(user_id)},
        {"agent.chat_history": {"$slice": -limit}},
    )
    raw = (doc or {}).get("agent", {}).get("chat_history", [])
    return [{"role": entry["role"], "content": entry["content"]} for entry in raw]


async def _append_to_persistent_history(user_id: str, user_msg: str, model_reply: str) -> None:
    """Append two turns to MongoDB and cap at 50 via $slice."""
    now = datetime.now(timezone.utc)
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$push": {"agent.chat_history": {"$each": [
            {"role": "user", "content": user_msg, "ts": now},
            {"role": "model", "content": model_reply, "ts": now},
        ], "$slice": -50}}},
        upsert=True,
    )


def _sanitize_style_hints(raw: str) -> str:
    """Strip prompt injection patterns from user-supplied style hints."""
    truncated = raw[:200].replace('\n', ' ').replace('\r', ' ')
    injection_signals = ('ignore', 'disregard', 'forget', 'system:', 'assistant:', 'jailbreak', 'override')
    if any(sig in truncated.lower() for sig in injection_signals):
        return ""
    return truncated.strip()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Intervention — Pydantic v2 models
# ---------------------------------------------------------------------------


class InterventionRequest(BaseModel):
    type: Literal[
        "wrong_answer",
        "session_summary",
        "pre_session_framing",
        "re_engagement",
        "streak_milestone",
    ]
    # --- Phase 1 fields (unchanged) ---
    card_id: Optional[str] = None
    card_front: Optional[str] = None
    card_back: Optional[str] = None
    card_notes: Optional[str] = None
    card_type: Optional[str] = None
    session_card_index: Optional[int] = None
    session_total_cards: Optional[int] = None
    session_wrong_count: Optional[int] = None
    most_missed_card_id: Optional[str] = None
    most_missed_card_front: Optional[str] = None
    # --- Phase 2 new fields ---
    deck_id: Optional[str] = None
    deck_name: Optional[str] = None
    due_count: Optional[int] = None
    last_struggle_pattern: Optional[str] = None
    total_due_count: Optional[int] = None
    top_deck_name: Optional[str] = None
    top_deck_due: Optional[int] = None
    streak_count: Optional[int] = None
    # --- Phase 3 new fields ---
    session_duration_minutes: Optional[int] = None   # for session_summary LLM context
    days_since_last_session: Optional[int] = None    # for re_engagement (client hint)

    @model_validator(mode='after')
    def check_required_fields(self) -> 'InterventionRequest':
        # Phase 1 validations (keep unchanged)
        if self.type == 'wrong_answer':
            missing = [f for f in ('card_id', 'card_front', 'card_back') if not getattr(self, f)]
            if missing:
                raise ValueError(f"Fields required for wrong_answer: {', '.join(missing)}")
        if self.type == 'session_summary':
            if self.session_total_cards is None or self.session_wrong_count is None:
                raise ValueError(
                    "session_total_cards and session_wrong_count required for session_summary"
                )
        # Phase 2 validations
        if self.type == 'pre_session_framing':
            missing = [f for f in ('deck_name', 'due_count') if not getattr(self, f)]
            if missing:
                raise ValueError(f"Fields required for pre_session_framing: {', '.join(missing)}")
        if self.type == 're_engagement':
            if self.total_due_count is None or not self.top_deck_name:
                raise ValueError("total_due_count and top_deck_name required for re_engagement")
        if self.type == 'streak_milestone':
            if self.streak_count not in (7, 14, 30, 60, 100):
                raise ValueError("streak_count must be one of 7, 14, 30, 60, 100")
        return self


class InterventionResponse(BaseModel):
    type: Literal[
        "wrong_answer",
        "session_summary",
        "pre_session_framing",
        "re_engagement",
        "streak_milestone",
    ]
    message: str
    card_id: Optional[str] = None
    already_seen: bool = False


# ---------------------------------------------------------------------------
# Intervention — private builder functions
# ---------------------------------------------------------------------------


def _build_wrong_answer_message(body: InterventionRequest) -> str:
    """
    Rule-based message. No LLM. No exclamation points. Max 2 sentences.
    Priority:
      1. Short answer (<=3 words) -> reversal pattern
      2. Notes present -> use first sentence of notes as hint
      3. Default -> generic distinction template
    """
    back: str = (body.card_back or "").strip()
    notes: str = (body.card_notes or "").strip()
    back_word_count: int = len(back.split())
    back_truncated: str = back[:60] + "..." if len(back) > 60 else back

    if back_word_count <= 3:
        return (
            f"The correct answer is {back_truncated}. "
            "It is easy to reverse this relationship — re-read the question as pointing to a specific outcome."
        )
    if notes:
        first_note: str = notes.split('.')[0].strip()
        if len(first_note) > 100:
            first_note = first_note[:97] + "..."
        return f"The correct answer is {back_truncated}. {first_note}."

    return (
        f"The correct answer is {back_truncated}. "
        "Review the distinction between the question framing and this answer before the next attempt."
    )


def _build_session_summary_message(body: InterventionRequest) -> str:
    """
    Rule-based diagnostic. No praise. No exclamation points. Max 2 sentences.
    """
    total: int = body.session_total_cards if body.session_total_cards is not None else 1
    wrong: int = body.session_wrong_count or 0
    missed_front: str = (body.most_missed_card_front or "").strip()

    if wrong == 0:
        return f"You completed {total} cards without any needing extra practice this session."

    error_rate: float = wrong / total

    if missed_front and wrong >= 2:
        return (
            f"{missed_front} needs the most attention from this session. "
            "That card is worth revisiting before your next review."
        )
    if error_rate >= 0.4:
        return (
            f"{wrong} of {total} cards need more practice. "
            "Review the core material for this deck before the next session."
        )
    return (
        f"{wrong} of {total} cards need more practice from this session. "
        "The cards needing attention were spread across different concepts — no single bottleneck stands out."
    )


# ---------------------------------------------------------------------------
# Intervention — Phase 2 builder functions
# ---------------------------------------------------------------------------


def _build_pre_session_framing_message(body: InterventionRequest) -> str:
    deck_name: str = (body.deck_name or "").strip()
    due: int = body.due_count or 0
    struggle: str = (body.last_struggle_pattern or "").strip()
    if struggle:
        return (
            f"{deck_name} has {due} cards due. "
            f"Last session, {struggle} gave you the most trouble — worth a close read today."
        )
    return f"{deck_name} has {due} cards due. This is a focused session."


def _build_re_engagement_message(body: InterventionRequest) -> str:
    total_due: int = body.total_due_count or 0
    top_name: str = (body.top_deck_name or "").strip()
    top_due: int = body.top_deck_due or 0
    if total_due == 0:
        return f"No cards are due today, but {top_name} is worth a pass to keep retention sharp."
    if top_due > 0 and top_name:
        return (
            f"{total_due} cards have built up across your decks. "
            f"{top_name} has the most at {top_due} — a good place to start."
        )
    return (
        f"{total_due} cards are due across your decks. "
        "Pick the deck you left longest and work through it first."
    )


_STREAK_MESSAGES: dict[int, str] = {
    7: "Seven consecutive days of review is where habit formation research places the first durable anchor.",
    14: "Two weeks of daily review produces measurable long-term retention gains according to spaced repetition studies.",
    30: "Thirty days of consistent review puts this material into the category of durable long-term memory.",
    60: "Sixty days of unbroken review is a result most learners never reach.",
    100: "One hundred days. The research on habit durability stops distinguishing at this point — this is simply part of who you are now.",
}


async def _build_streak_milestone_message(
    body: InterventionRequest,
    user_id: str,
) -> tuple[str, bool]:
    """Returns (message, already_seen). Dedup enforced in MongoDB."""
    milestone: int = body.streak_count
    user_doc = await users_collection.find_one(
        {"_id": ObjectId(user_id)},
        {"agent.seen_streak_milestones": 1},
    )
    seen: list[int] = (user_doc or {}).get("agent", {}).get("seen_streak_milestones", [])
    if milestone in seen:
        return ("", True)
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$addToSet": {"agent.seen_streak_milestones": milestone}},
        upsert=True,
    )
    return (_STREAK_MESSAGES.get(milestone, f"You have reached a {milestone}-day streak."), False)


# ---------------------------------------------------------------------------
# Intervention — Phase 3 constants
# ---------------------------------------------------------------------------


_INTERVENTION_SYSTEM_PROMPT = """You are a diagnostic study coach embedded in a flashcard app.
Your only job is to produce a single intervention message for a learner.

HARD RULES — never break these:
1. Maximum 2 sentences. No more. The second sentence is optional if the first is already complete.
2. No exclamation points. Not one.
3. No emoji of any kind.
4. No filler openers: never start with "Great", "Sure", "Of course", "Absolutely", "Nice try", or any praise word.
5. Diagnostic tone only: state what the data shows, what the concept actually means, or what the pattern is.
6. Reference only content explicitly present in the user prompt — never invent card content, deck names, or statistics.
7. Do not ask questions. Do not end with a question mark.
8. Write in plain English. No markdown, no bullet points, no headers."""


_INTERVENTION_PROMPT_TEMPLATES: dict[str, str] = {
    "wrong_answer": (
        "The learner just answered this flashcard incorrectly.\n"
        "Card front: {card_front}\n"
        "Correct answer: {card_back}\n"
        "Card notes: {card_notes}\n"
        "Times this card was answered wrong across all decks: {wrong_count}\n"
        "Deck name: {deck_name}\n\n"
        "Write a 1-2 sentence message that names what the correct answer actually means "
        "and why the learner likely confused it. Reference the card content directly. "
        "Do not say 'the correct answer is' — show the concept instead."
    ),
    "session_summary": (
        "The learner just finished a study session.\n"
        "Total cards reviewed: {total_cards}\n"
        "Cards needing more practice: {needs_practice_count}\n"
        "Card needing the most attention, front: {most_attention_front}\n"
        "Card needing the most attention, back: {most_attention_back}\n"
        "Session duration in minutes: {duration_minutes}\n"
        "Deck name: {deck_name}\n\n"
        "Write a 1-2 sentence diagnostic summary. If needs_practice_count is 0, state that cleanly. "
        "If most_attention_front is non-empty, name it specifically as the card needing attention. "
        "Do not use the word 'great' or any praise word."
    ),
    "pre_session_framing": (
        "The learner is about to start a study session.\n"
        "Deck name: {deck_name}\n"
        "Cards due: {due_count}\n"
        "Most-missed card from this deck: {most_missed_concept}\n"
        "Last session struggle pattern: {last_struggle}\n\n"
        "Write 1-2 sentences that frame what this session will focus on. "
        "Reference the most-missed concept if present. "
        "Do not motivate — only orient."
    ),
    "re_engagement": (
        "The learner has not studied for {days_since} days.\n"
        "Total cards due across all decks: {total_due}\n"
        "Top deck name: {top_deck_name}\n"
        "Top deck's most-missed concept: {top_deck_concept}\n\n"
        "Write 1-2 sentences that acknowledge the gap and identify what needs attention most. "
        "Name the concept specifically. Do not use urgency language or warnings."
    ),
    "streak_milestone": (
        "The learner has reached a {streak_count}-day consecutive study streak.\n"
        "Science note: {science_note}\n\n"
        "Write 1-2 sentences that state what this milestone means in terms of memory science. "
        "Reference the science note. Do not congratulate — state the implication."
    ),
}

_STREAK_SCIENCE_NOTES: dict[int, str] = {
    7:   "Seven days is the minimum interval at which habit research observes initial neural pathway consolidation for daily behaviors.",
    14:  "Fourteen days of consistent retrieval practice produces measurable gains in long-term retention versus spaced but inconsistent review.",
    30:  "Thirty days of daily review moves material from working memory dependence into durable long-term storage, per spacing effect research.",
    60:  "Sixty days of unbroken review is the threshold at which behavioral researchers classify an action as an automatic habit rather than a deliberate choice.",
    100: "One hundred days of consistent daily practice is associated with structural changes in procedural memory encoding, beyond what motivational reinforcement alone can produce.",
}


# ---------------------------------------------------------------------------
# Intervention — Phase 3 helpers
# ---------------------------------------------------------------------------


async def _fetch_intervention_context(
    event_type: str,
    body: "InterventionRequest",
    user_id: str,
) -> dict:
    """
    Builds the populated context dict for LLM prompt interpolation.
    All DB fetches are bounded and wrapped in try/except.
    All values are non-None strings/ints — prompt templates never receive Python None.
    """
    ctx: dict = {}

    if event_type == "wrong_answer":
        ctx["card_front"] = (body.card_front or "").strip() or "Unknown"
        ctx["card_back"] = (body.card_back or "").strip() or "Unknown"
        ctx["card_notes"] = (body.card_notes or "").strip() or "None provided"
        ctx["deck_name"] = "this deck"
        ctx["wrong_count"] = 1
        try:
            if body.card_id:
                count = await cards_collection.count_documents(
                    {"user_id": user_id, "_id": ObjectId(body.card_id), "repetitions": {"$lte": 1}},
                )
                ctx["wrong_count"] = min(count, 99)
        except Exception:
            pass

    elif event_type == "session_summary":
        ctx["total_cards"] = body.session_total_cards or 0
        ctx["needs_practice_count"] = body.session_wrong_count or 0
        ctx["most_attention_front"] = (body.most_missed_card_front or "None").strip()
        ctx["most_attention_back"] = "None"
        ctx["duration_minutes"] = body.session_duration_minutes or 0
        ctx["deck_name"] = "this deck"
        try:
            if body.most_missed_card_id:
                card = await cards_collection.find_one(
                    {"_id": ObjectId(body.most_missed_card_id), "user_id": user_id},  # CR-06: scope to caller
                    {"back": 1, "deck_id": 1},
                )
                if card:
                    ctx["most_attention_back"] = (card.get("back") or "None").strip()
        except Exception:
            pass

    elif event_type == "pre_session_framing":
        ctx["deck_name"] = (body.deck_name or "this deck").strip()
        ctx["due_count"] = body.due_count or 0
        ctx["last_struggle"] = (body.last_struggle_pattern or "None identified").strip()
        ctx["most_missed_concept"] = "None identified"
        try:
            if body.deck_id:
                pipeline = [
                    {"$match": {"user_id": user_id, "deck_id": body.deck_id, "repetitions": {"$lte": 1}}},
                    {"$group": {"_id": "$_id", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": 1},
                ]
                results = await cards_collection.aggregate(pipeline).to_list(length=1)
                if results:
                    top_card = await cards_collection.find_one(
                        {"_id": results[0]["_id"]}, {"front": 1}
                    )
                    if top_card:
                        front = (top_card.get("front") or "").strip()
                        ctx["most_missed_concept"] = front[:60] + ("..." if len(front) > 60 else "")
        except Exception:
            pass

    elif event_type == "re_engagement":
        ctx["days_since"] = body.days_since_last_session or 3
        ctx["total_due"] = body.total_due_count or 0
        ctx["top_deck_name"] = (body.top_deck_name or "your deck").strip()
        ctx["top_deck_concept"] = "None identified"
        try:
            if body.deck_id:
                pipeline = [
                    {"$match": {"user_id": user_id, "deck_id": body.deck_id, "repetitions": {"$lte": 1}}},
                    {"$group": {"_id": "$_id", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": 1},
                ]
                results = await cards_collection.aggregate(pipeline).to_list(length=1)
                if results:
                    top_card = await cards_collection.find_one(
                        {"_id": results[0]["_id"]}, {"front": 1}
                    )
                    if top_card:
                        front = (top_card.get("front") or "").strip()
                        ctx["top_deck_concept"] = front[:60] + ("..." if len(front) > 60 else "")
        except Exception:
            pass

    elif event_type == "streak_milestone":
        ctx["streak_count"] = body.streak_count or 7
        ctx["science_note"] = _STREAK_SCIENCE_NOTES.get(body.streak_count, "")

    return ctx


async def _generate_intervention_message(
    event_type: str,
    context_dict: dict,
    fallback_fn,
    fallback_args,
) -> str:
    """
    Single-shot LLM call. Falls back to Phase 1/2 builder on any failure.
    Uses the existing AgentLLM singleton — tools=None disables the tool loop.
    Wraps with a 10-second timeout.
    """
    user_prompt: str = _INTERVENTION_PROMPT_TEMPLATES[event_type].format(**context_dict)
    try:
        raw: str = await asyncio.wait_for(
            agent_llm.chat(
                message=user_prompt,
                history=[],
                system_prompt=_INTERVENTION_SYSTEM_PROMPT,
                tools=None,
                tool_dispatcher=None,
                user_id=None,
            ),
            timeout=10.0,
        )
        # Hard length guard: max 2 sentences
        sentences = [s.strip() for s in raw.replace("!", ".").split(".") if s.strip()]
        result: str = ". ".join(sentences[:2]).strip()
        if result:
            return result + ("." if not result.endswith(".") else "")
        return fallback_fn(fallback_args) if callable(fallback_fn) else ""
    except Exception:
        logger.warning("[Intervention] LLM failed for %s — using rule-based fallback.", event_type)
        try:
            if asyncio.iscoroutinefunction(fallback_fn):
                return await fallback_fn(fallback_args)
            return fallback_fn(fallback_args)
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Intervention — route handler
# ---------------------------------------------------------------------------


@router.post("/intervention", response_model=InterventionResponse)
async def post_intervention(
    body: InterventionRequest,
    current_user: dict = Depends(get_firebase_user),
) -> InterventionResponse:
    """
    Proactive Companion — LLM-generated intervention messages with rule-based fallback.
    Phase 3: each event type calls through to the LLM; failures fall back to Phase 1/2 builders.
    """
    user_id: str = current_user.get("user_id") or current_user.get("uid", "")

    if body.type == "wrong_answer":
        context = await _fetch_intervention_context("wrong_answer", body, user_id)
        message: str = await _generate_intervention_message(
            "wrong_answer", context, _build_wrong_answer_message, body
        )
        return InterventionResponse(type="wrong_answer", message=message, card_id=body.card_id)

    if body.type == "session_summary":
        context = await _fetch_intervention_context("session_summary", body, user_id)
        message = await _generate_intervention_message(
            "session_summary", context, _build_session_summary_message, body
        )
        return InterventionResponse(type="session_summary", message=message)

    if body.type == "pre_session_framing":
        context = await _fetch_intervention_context("pre_session_framing", body, user_id)
        message = await _generate_intervention_message(
            "pre_session_framing", context, _build_pre_session_framing_message, body
        )
        return InterventionResponse(type="pre_session_framing", message=message)

    if body.type == "re_engagement":
        context = await _fetch_intervention_context("re_engagement", body, user_id)
        message = await _generate_intervention_message(
            "re_engagement", context, _build_re_engagement_message, body
        )
        return InterventionResponse(type="re_engagement", message=message)

    if body.type == "streak_milestone":
        phase2_message, already_seen = await _build_streak_milestone_message(body, user_id)
        if already_seen:
            return InterventionResponse(type="streak_milestone", message="", already_seen=True)
        context = await _fetch_intervention_context("streak_milestone", body, user_id)
        message = await _generate_intervention_message(
            "streak_milestone",
            context,
            lambda _: phase2_message,
            None,
        )
        return InterventionResponse(type="streak_milestone", message=message, already_seen=False)

    raise HTTPException(status_code=422, detail="Unknown intervention type.")


class SessionXpRequest(BaseModel):
    cards_reviewed: int
    deck_id: str

    @field_validator('cards_reviewed')
    @classmethod
    def validate_cards(cls, v: int) -> int:
        if not (1 <= v <= 500):
            raise ValueError('cards_reviewed must be between 1 and 500')
        return v

    @field_validator('deck_id')
    @classmethod
    def validate_deck_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError('deck_id must not be empty')
        return v


class XpGrantResponse(BaseModel):
    xp_awarded: int
    level_up: bool
    new_level: int
    new_stage: int


@router.post('/xp/session', response_model=XpGrantResponse)
async def award_session_xp(
    body: SessionXpRequest,
    current_user: dict = Depends(get_firebase_user),
) -> XpGrantResponse:
    user_id: str = current_user.get("user_id")
    xp_result: dict = await grant_xp(user_id, 20)
    return XpGrantResponse(
        xp_awarded=20,
        level_up=xp_result["level_up"],
        new_level=xp_result["new_level"],
        new_stage=xp_result["new_stage"],
    )


@router.post('/xp/streak', response_model=XpGrantResponse)
async def award_streak_xp(
    current_user: dict = Depends(get_firebase_user),
) -> XpGrantResponse:
    user_id: str = current_user.get("user_id")
    today: str = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # Idempotency check — one streak bonus per calendar day
    user_doc = await users_collection.find_one(
        {'_id': ObjectId(user_id)},
        {'agent.streak_xp_awarded_date': 1, 'agent.xp': 1},
    )
    if user_doc and user_doc.get('agent', {}).get('streak_xp_awarded_date') == today:
        level: int = _calculate_level(user_doc.get('agent', {}).get('xp', 0))
        return XpGrantResponse(
            xp_awarded=0,
            level_up=False,
            new_level=level,
            new_stage=_level_to_stage(level),
        )

    # Award XP and record today's date atomically
    xp_result: dict = await grant_xp(user_id, 10)
    await users_collection.update_one(
        {'_id': ObjectId(user_id)},
        {'$set': {'agent.streak_xp_awarded_date': today}},
    )
    return XpGrantResponse(
        xp_awarded=10,
        level_up=xp_result["level_up"],
        new_level=xp_result["new_level"],
        new_stage=xp_result["new_stage"],
    )


@router.get("/me", response_model=AgentStateResponse)
async def get_agent_state(
    current_user: dict = Depends(get_firebase_user),
) -> AgentStateResponse:
    """Return the Study Buddy's current state for the authenticated user."""
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tier = _resolve_tier(user)
    plan = SUBSCRIPTION_PLANS[tier]
    messages_limit: int = plan["limits"]["agent_messages_per_month"]

    agent_data = user.get("agent", {})
    cards_reviewed: int = user.get("stats", {}).get("total_cards_reviewed", 0)
    messages_used: int = agent_data.get("messages_used_this_month", 0)
    preferred_name = user.get("full_name") or user.get("username", "Learner")

    knowledge_access, proactive_nudging, _, __ = _get_agent_prefs(user)

    prefs: dict = user.get("preferences", {}).get("agent", {})
    agent_roaming_enabled: bool = bool(prefs.get("roaming_enabled", True))
    intervention_frequency: str = prefs.get("intervention_frequency", "balanced")
    focus_mode: bool = bool(prefs.get("focus_mode", False))
    intervention_wrong_answer: bool = bool(prefs.get("intervention_wrong_answer", True))
    intervention_session_summary: bool = bool(prefs.get("intervention_session_summary", True))
    intervention_pre_session: bool = bool(prefs.get("intervention_pre_session", True))
    intervention_re_engagement: bool = bool(prefs.get("intervention_re_engagement", True))
    intervention_streak_milestone: bool = bool(prefs.get("intervention_streak_milestone", True))

    xp: int = agent_data.get("xp", 0)
    pet_prefs: dict = user.get("preferences", {}).get("pet", {})
    avatar_url = pet_prefs.get("avatar_url")
    avatar_stage = pet_prefs.get("avatar_stage")
    avatar_regen_pending = pet_prefs.get("avatar_regen_pending", False)
    animation_url = pet_prefs.get("animation_url")
    animation_stage = pet_prefs.get("animation_stage")
    animation_regen_pending = pet_prefs.get("animation_regen_pending", False)

    return AgentStateResponse(
        level=_calculate_level(xp),
        mood=_calculate_mood(user, cards_reviewed),
        cards_reviewed=cards_reviewed,
        messages_used=messages_used,
        messages_limit=messages_limit,
        preferred_name=preferred_name,
        tier=tier.value,
        knowledge_access_enabled=knowledge_access,
        proactive_nudging_enabled=proactive_nudging,
        current_xp=xp,
        xp_for_next_level=_xp_for_next_level(xp),
        current_stage=_level_to_stage(_calculate_level(xp)),
        pet_name=pet_prefs.get("pet_name"),
        pet_species=pet_prefs.get("pet_species"),
        pet_color=pet_prefs.get("pet_color"),
        avatar_url=avatar_url,
        avatar_stage=avatar_stage,
        avatar_regen_pending=avatar_regen_pending,
        animation_url=animation_url,
        animation_stage=animation_stage,
        animation_regen_pending=animation_regen_pending,
        agent_roaming_enabled=agent_roaming_enabled,
        agent_intervention_frequency=intervention_frequency,
        agent_focus_mode=focus_mode,
        agent_intervention_wrong_answer=intervention_wrong_answer,
        agent_intervention_session_summary=intervention_session_summary,
        agent_intervention_pre_session=intervention_pre_session,
        agent_intervention_re_engagement=intervention_re_engagement,
        agent_intervention_streak_milestone=intervention_streak_milestone,
    )


@router.get("/nudge", response_model=NudgeResponse)
async def get_proactive_nudge(
    current_user: dict = Depends(get_firebase_user),
) -> NudgeResponse:
    """
    Returns a short, personalized proactive nudge message if the user has
    opted into proactive nudging AND has knowledge access enabled.
    Called once by the frontend on dashboard mount.
    """
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        return NudgeResponse(has_nudge=False)

    knowledge_access, proactive_nudging, _, __ = _get_agent_prefs(user)

    # Both must be on for a nudge to trigger
    if not knowledge_access or not proactive_nudging:
        return NudgeResponse(has_nudge=False)

    try:
        summary = await get_study_summary(user_id)
        preferred_name = user.get("full_name") or user.get("username", "there")
        due = summary.get("due_cards", 0)
        new = summary.get("new_cards", 0)
        pending_decks = summary.get("decks_with_pending_work", [])

        if due == 0 and new == 0:
            return NudgeResponse(has_nudge=False)

        # Build a concise, personal nudge message
        if pending_decks:
            top_deck = pending_decks[0]
            deck_name = top_deck["deck_name"]
            deck_due = top_deck["due_cards"]
            deck_new = top_deck["new_cards"]
            parts = []
            if deck_due > 0:
                parts.append(f"{deck_due} due")
            if deck_new > 0:
                parts.append(f"{deck_new} new")
            nudge = f"Hey {preferred_name}! '{deck_name}' has {' and '.join(parts)} {'cards' if len(parts) > 1 else 'card'} waiting. Head to your Study Session when you're ready! 🎯"
        else:
            nudge = f"Hey {preferred_name}! You have {due} cards due for review. Go crush them in your Study Session! 💪"

        return NudgeResponse(nudge=nudge, has_nudge=True)

    except Exception:
        return NudgeResponse(has_nudge=False)


@router.post("/generate-avatar", response_model=GenerateAvatarResponse)
async def generate_avatar(
    body: GenerateAvatarRequest = Body(default_factory=GenerateAvatarRequest),
    current_user: dict = Depends(get_firebase_user),
) -> GenerateAvatarResponse:
    """Generate a personalized AI avatar for the user's Study Pet using fal.ai FLUX Pro."""
    import uuid as _uuid_mod

    user_id: str = current_user.get("user_id")

    user_doc = await users_collection.find_one({"_id": ObjectId(user_id)}) if user_id else None
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    # Tier check — use _resolve_tier() to safely handle missing/corrupt tier values
    tier = _resolve_tier(user_doc)
    if tier == SubscriptionTier.FREE:
        raise HTTPException(status_code=403, detail="avatar_generation_requires_plus")

    tier_limits: dict[SubscriptionTier, int] = {SubscriptionTier.PLUS: 1, SubscriptionTier.PRO: 3}
    limit: int = tier_limits.get(tier, 1)

    # Species check
    pet = user_doc.get("preferences", {}).get("pet", {})
    if not pet.get("pet_species"):
        raise HTTPException(status_code=400, detail="avatar_missing_species")

    # Rate limit — CR-04: atomic slot reservation eliminates TOCTOU race under concurrent requests.
    # Counter is incremented before the slow fal.ai call and rolled back on external failure.
    current_month: str = datetime.now(timezone.utc).strftime("%Y-%m")
    stored_month: Optional[str] = pet.get("avatar_reset_month")
    old_count: int = pet.get("avatar_generation_count", 0) if stored_month == current_month else 0

    if body.trigger == "manual":
        reserve_result = await users_collection.update_one(
            {
                "_id": ObjectId(user_id),
                "$or": [
                    # Same-month path: only if under limit
                    {
                        "preferences.pet.avatar_reset_month": current_month,
                        "preferences.pet.avatar_generation_count": {"$lt": limit},
                    },
                    # Month-rollover path: any prior month qualifies
                    {"preferences.pet.avatar_reset_month": {"$ne": current_month}},
                ],
            },
            [
                {
                    "$set": {
                        "preferences.pet.avatar_generation_count": {
                            "$cond": [
                                {"$ne": [
                                    {"$ifNull": ["$preferences.pet.avatar_reset_month", ""]},
                                    current_month,
                                ]},
                                1,  # month rolled over — start fresh at 1
                                {"$add": ["$preferences.pet.avatar_generation_count", 1]},
                            ]
                        },
                        "preferences.pet.avatar_reset_month": current_month,
                    }
                }
            ],
        )
        if reserve_result.matched_count == 0:
            raise HTTPException(status_code=429, detail="avatar_rate_limit_exceeded")

    if body.trigger == "evolution":
        if not pet.get("avatar_regen_pending", False):
            raise HTTPException(status_code=400, detail="avatar_regen_not_pending")

    # Ensure avatar_seed exists (generate once, never overwrite)
    avatar_seed: Optional[str] = pet.get("avatar_seed")
    if not avatar_seed:
        avatar_seed = str(_uuid_mod.uuid4())
        await users_collection.update_one(
            {"_id": ObjectId(user_id)},  # CR-01 fix: use _id not firebase_uid
            {"$set": {"preferences.pet.avatar_seed": avatar_seed}}
        )
        pet["avatar_seed"] = avatar_seed

    # Compute stage
    xp: int = user_doc.get("agent", {}).get("xp", 0)
    stage: int = _level_to_stage(_calculate_level(xp))

    # Build prompt
    try:
        prompt, seed_int = _build_avatar_prompt(user_doc, stage)
    except Exception as e:
        logger.error(f"Avatar prompt build failed: {e}")
        raise HTTPException(status_code=422, detail="avatar_invalid_stage")

    # Call fal.ai
    try:
        avatar_url = await _call_fal_avatar(prompt, seed_int)
    except Exception as e:
        logger.error(f"Avatar generation failed: {e}")
        # CR-04: Roll back the atomically reserved slot so the user can retry
        if body.trigger == "manual":
            await users_collection.update_one(
                {"_id": ObjectId(user_id)},
                {"$inc": {"preferences.pet.avatar_generation_count": -1}},
            )
        raise HTTPException(status_code=502, detail="avatar_generation_failed")

    # Persist avatar data.
    # For manual trigger: count/month already written atomically in the reservation above.
    # For evolution trigger: count_increment = 0; normalise month field to current_month.
    now = datetime.now(timezone.utc)
    persist_fields: dict = {
        "preferences.pet.avatar_url": avatar_url,
        "preferences.pet.avatar_stage": stage,
        "preferences.pet.avatar_generated_at": now,
        "preferences.pet.avatar_regen_pending": False,
    }
    if body.trigger == "evolution":
        persist_fields["preferences.pet.avatar_reset_month"] = current_month
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},  # CR-01 fix: use _id not firebase_uid
        {"$set": persist_fields}
    )

    new_count: int = old_count + (1 if body.trigger == "manual" else 0)
    generations_remaining: int = limit - new_count

    return GenerateAvatarResponse(
        avatar_url=avatar_url,
        avatar_stage=stage,
        generated_at=now.isoformat(),
        generations_remaining=max(0, generations_remaining),
        trigger=body.trigger,
    )


@router.post("/generate-animation", response_model=GenerateAnimationResponse)
async def generate_animation(
    body: GenerateAnimationRequest = Body(default_factory=GenerateAnimationRequest),
    current_user: dict = Depends(get_firebase_user),
) -> GenerateAnimationResponse:
    """Generate a looping animation for the user's Study Pet using Luma Ray 2 Flash via fal.ai."""
    user_id: str = current_user.get("user_id")

    user_doc = await users_collection.find_one({"_id": ObjectId(user_id)}) if user_id else None
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    # Tier check — Plus and Pro only — use _resolve_tier() for safe enum validation
    tier = _resolve_tier(user_doc)
    if tier == SubscriptionTier.FREE:
        raise HTTPException(status_code=403, detail="animation_generation_requires_plus")

    tier_limits: dict[SubscriptionTier, int] = {SubscriptionTier.PLUS: 1, SubscriptionTier.PRO: 3}
    limit: int = tier_limits.get(tier, 1)

    pet: dict = user_doc.get("preferences", {}).get("pet", {})

    # Rate limit — CR-04: atomic slot reservation eliminates TOCTOU race under concurrent requests.
    current_month: str = datetime.now(timezone.utc).strftime("%Y-%m")
    stored_month: Optional[str] = pet.get("animation_reset_month")
    old_count: int = pet.get("animation_generation_count", 0) if stored_month == current_month else 0

    if body.trigger == "manual":
        reserve_result = await users_collection.update_one(
            {
                "_id": ObjectId(user_id),
                "$or": [
                    {
                        "preferences.pet.animation_reset_month": current_month,
                        "preferences.pet.animation_generation_count": {"$lt": limit},
                    },
                    {"preferences.pet.animation_reset_month": {"$ne": current_month}},
                ],
            },
            [
                {
                    "$set": {
                        "preferences.pet.animation_generation_count": {
                            "$cond": [
                                {"$ne": [
                                    {"$ifNull": ["$preferences.pet.animation_reset_month", ""]},
                                    current_month,
                                ]},
                                1,
                                {"$add": ["$preferences.pet.animation_generation_count", 1]},
                            ]
                        },
                        "preferences.pet.animation_reset_month": current_month,
                    }
                }
            ],
        )
        if reserve_result.matched_count == 0:
            raise HTTPException(status_code=429, detail="animation_rate_limit_exceeded")

    # Check avatar_url exists and is a hosted HTTPS URL (Cloudinary)
    avatar_url: Optional[str] = pet.get("avatar_url")
    if not avatar_url:
        raise HTTPException(status_code=400, detail="animation_requires_avatar")
    if avatar_url.startswith("data:"):
        # Legacy base64 portrait — user must regenerate their portrait first
        raise HTTPException(status_code=400, detail="animation_requires_hosted_avatar")

    # Resolve species and motion prompt
    species: str = (pet.get("pet_species") or "").lower()
    motion_prompt: str = ANIMATION_MOTION_PROMPTS.get(
        species, "companion moving gently, seamless loop"
    )

    # Evolution trigger guard
    if body.trigger == "evolution":
        if not pet.get("animation_regen_pending", False):
            raise HTTPException(status_code=400, detail="animation_regen_not_pending")

    # Derive seed from avatar_seed for stable Cloudinary public_id
    avatar_seed: str = pet.get("avatar_seed", "0")
    try:
        seed_int: int = int(_uuid.UUID(avatar_seed).int % (2 ** 32))
    except (ValueError, AttributeError):
        seed_int = 0

    # Call fal.ai Luma Ray 2 Flash → upload to Cloudinary
    try:
        video_url: str = await _call_fal_animation(avatar_url, motion_prompt, seed=seed_int)
    except Exception as e:
        logger.error(f"Animation generation failed: {e}")
        # CR-04: Roll back the atomically reserved slot so the user can retry
        if body.trigger == "manual":
            await users_collection.update_one(
                {"_id": ObjectId(user_id)},
                {"$inc": {"preferences.pet.animation_generation_count": -1}},
            )
        raise HTTPException(status_code=502, detail="animation_generation_failed")

    # Compute current stage from XP
    xp: int = user_doc.get("agent", {}).get("xp", 0)
    stage: int = _level_to_stage(_calculate_level(xp))

    # Persist animation data.
    # For manual trigger: count/month already written atomically in the reservation above.
    # For evolution trigger: count_increment = 0; normalise month field to current_month.
    now = datetime.now(timezone.utc)
    persist_fields: dict = {
        "preferences.pet.animation_url": video_url,
        "preferences.pet.animation_stage": stage,
        "preferences.pet.animation_generated_at": now,
        "preferences.pet.animation_regen_pending": False,
    }
    if body.trigger == "evolution":
        persist_fields["preferences.pet.animation_reset_month"] = current_month
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},  # CR-01 fix: use _id not firebase_uid
        {"$set": persist_fields}
    )

    new_count: int = old_count + (1 if body.trigger == "manual" else 0)
    generations_remaining: int = limit - new_count

    return GenerateAnimationResponse(
        animation_url=video_url,
        avatar_stage=stage,
        generated_at=now.isoformat(),
        generations_remaining=max(0, generations_remaining),
        trigger=body.trigger,
    )


@router.post("/chat", response_model=ChatResponse)
@limiter.limit("20/minute")
async def chat(
    request: Request,
    body: ChatRequest,
    current_user: dict = Depends(get_firebase_user),
) -> ChatResponse:
    """
    Send a message to the Study Buddy.

    If the user has enabled Knowledge Access, the Gemini model is equipped
    with Function Calling tools that let it query the user's library, decks,
    and annual plan on demand (Hybrid RAG). All tools are read-only.
    """
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tier = _resolve_tier(user)
    plan_features = SUBSCRIPTION_PLANS[tier]["features"]
    messages_limit: int = SUBSCRIPTION_PLANS[tier]["limits"]["agent_messages_per_month"]
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")

    # Atomic message cap enforcement (race-condition safe — replaces non-atomic read-check-write)
    if messages_limit != -1:
        cap_result = await users_collection.update_one(
            {
                "_id": ObjectId(user_id),
                "$or": [
                    {"agent.tracked_month": current_month,
                     "agent.messages_used_this_month": {"$lt": messages_limit}},
                    {"agent.tracked_month": {"$ne": current_month}},
                ],
            },
            {
                "$inc": {"agent.messages_used_this_month": 1},
                "$set": {"agent.tracked_month": current_month},
            },
        )
        if cap_result.matched_count == 0:
            raise HTTPException(status_code=429, detail="message_limit_reached")
    else:
        # Unlimited (Plus/Pro): just track for reporting
        await users_collection.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$inc": {"agent.messages_used_this_month": 1},
                "$set": {"agent.tracked_month": current_month},
            },
            upsert=True,
        )

    # Tier-gated memory: session-only for Free, persistent for Plus/Pro
    if plan_features.get("agent_persistent_memory"):
        saved_history = await _load_persistent_history(user_id, limit=20)
        history_for_llm = saved_history + [
            {"role": msg.role, "content": msg.content} for msg in body.history[-10:]
        ]
    else:
        # Free: session-only — never load from MongoDB
        history_for_llm = [
            {"role": msg.role, "content": msg.content} for msg in body.history[-10:]
        ]

    # Privacy gate: check knowledge access preference
    knowledge_access, _, conciseness, tone = _get_agent_prefs(user)

    # Build system prompt (includes knowledge access, style directives, screen context, and stage)
    agent_data = user.get("agent", {})
    xp: int = agent_data.get("xp", 0)
    current_stage: int = _level_to_stage(_calculate_level(xp))
    pet_prefs: dict = user.get("preferences", {}).get("pet", {})
    pet_name_custom: Optional[str] = pet_prefs.get("pet_name")
    pet_species_custom: Optional[str] = pet_prefs.get("pet_species")

    # RAG: retrieve relevant book chunks if the user is reading a book and has knowledge access
    rag_book_context: Optional[str] = None
    if (
        body.context is not None
        and body.context.page == "book"
        and body.context.book_id
        and knowledge_access
    ):
        from app.utils.book_rag import retrieve_book_context
        rag_book_context = await retrieve_book_context(
            book_id=body.context.book_id,
            user_id=user_id,
            query=body.message,
        )

    system_prompt = _build_system_prompt(
        user,
        knowledge_access,
        conciseness,
        tone,
        screen_context=body.context,
        stage=current_stage,
        language=body.language,
        pet_name=pet_name_custom,
        pet_species=pet_species_custom,
        rag_book_context=rag_book_context,
    )

    history = history_for_llm

    client = get_langfuse_client()
    tier_value: str = tier.value if hasattr(tier, "value") else str(tier)
    model_name = TIER_MODEL_NAMES.get(tier_value, TIER_MODEL_NAMES["free"])
    trace_metadata = {"feature": "smart_pet_chat", "tier": tier_value, "user_id": user_id, "model": model_name}

    # ── Quiz trigger setup ────────────────────────────────────────────────────
    # The `start_quiz` tool is always attached (independent of knowledge_access —
    # quiz triggering is core functionality). Its default question count mirrors
    # the same tier-aware computation the old pre-classifier used.
    agent_prefs_raw: dict = user.get("preferences", {}).get("agent", {})
    configured_q_count: int = int(agent_prefs_raw.get("ai_quiz_question_count", 10))
    default_q_count: int = configured_q_count if tier != SubscriptionTier.FREE else 10

    # Mutable side-channel: populated by _dispatch_tool_call_inner if the model
    # calls start_quiz with confidence="high". Read after agent_llm.chat() returns.
    quiz_result_holder: dict = {}

    tools_for_call = (KNOWLEDGE_TOOLS if knowledge_access else []) + QUIZ_TOOLS

    # ── The ONE LLM call for this turn ────────────────────────────────────────
    turn_input = history + [{"role": "user", "content": body.message}]

    # Set up Langfuse tracing context BEFORE the LLM call (fire-and-forget, TR-06).
    # If construction fails, fall back to no-op context managers — the LLM call
    # below executes exactly once either way, never retried for tracing reasons.
    attrs_cm = contextlib.nullcontext()
    gen_cm = contextlib.nullcontext()
    if client:
        try:
            attrs_cm = propagate_attributes(
                user_id=user_id,
                session_id=user_id,  # D-10: stable per-user session across the pet relationship
                trace_name="smart_pet_chat",
                metadata=trace_metadata,
                tags=["smart_pet_chat", tier_value],
            )
            gen_cm = client.start_as_current_observation(
                name="smart_pet_chat",
                as_type="generation",
                model=model_name,
                input=turn_input,
            )
        except Exception as langfuse_exc:
            logger.warning(
                f"[SmartPet] Langfuse tracing failed, continuing without trace: {langfuse_exc}"
            )
            attrs_cm = contextlib.nullcontext()
            gen_cm = contextlib.nullcontext()

    tool_dispatcher = functools.partial(
        _dispatch_tool_call,
        client=client,
        default_question_count=default_q_count,
        quiz_result_holder=quiz_result_holder,
        user_message=body.message,  # threaded to the server-side quiz intent gate
    )

    try:
        with attrs_cm, gen_cm as turn_generation:
            # ── The ONE LLM call for this turn — executes exactly once ──────
            reply = await agent_llm.chat(
                message=body.message,
                history=history,
                system_prompt=system_prompt,
                tools=tools_for_call,
                tool_dispatcher=tool_dispatcher,
                user_id=user_id,
                tier=tier,
            )

            # Record the result on the Langfuse generation, if one is active.
            # A failure here is a tracing-only failure: the reply is already in
            # hand, so we log and continue — never re-invoke agent_llm.chat.
            if turn_generation is not None:
                try:
                    turn_generation.update(output=reply)  # D-13: full reply, no truncation
                except Exception as langfuse_exc:
                    logger.warning(
                        f"[SmartPet] Langfuse tracing failed, continuing without trace: {langfuse_exc}"
                    )
    except Exception as exc:
        # Full details go to the log (and Sentry via the error handler) —
        # never into the client-facing detail string.
        logger.error(f"Agent Chat Error: {exc}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="AI service error. Please try again.",
        )

    # ── Quiz result processing ──────────────────────────────────────────────
    # Either the model called start_quiz (confidence="high" → quiz_config, and
    # the canned handoff message replaces the reply), or it answered directly
    # and may have appended a [[QUIZ_OFFER:<topic>]] marker for an ambiguous
    # study/help request (→ quiz_offer, marker stripped from the visible reply).
    quiz_config: Optional[QuizConfig] = None
    quiz_offer: Optional[QuizOffer] = None

    if quiz_result_holder.get("quiz_config"):
        quiz_config = QuizConfig(**quiz_result_holder["quiz_config"])
        topic_label: str = quiz_config.topic or "this topic"
        reply = _build_quiz_handoff_message(topic_label, body.language)
    else:
        offer_match = re.search(r'\[\[QUIZ_OFFER:(.+?)\]\]\s*$', reply)
        if offer_match:
            quiz_offer = QuizOffer(topic=offer_match.group(1).strip())
            reply = reply[:offer_match.start()].rstrip()

    # Persona break detection (log + flag, do NOT block reply)
    persona_break_detected = any(signal in reply for signal in _PERSONA_BREAK_SIGNALS)
    if persona_break_detected:
        logger.warning(
            "[SmartPet] Persona break detected for user_id=%s tier=%s", user_id, tier
        )

    # Append to persistent history for Plus/Pro
    if plan_features.get("agent_persistent_memory"):
        await _append_to_persistent_history(user_id, body.message, reply)

    # Track last interaction timestamp + award XP for engagement
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"agent.last_interaction": datetime.now(timezone.utc)}},
    )
    xp_result: dict = await grant_xp(user_id, 5)  # 5 XP per Buddy interaction

    # Fetch updated messages_used for response
    updated_agent = await users_collection.find_one(
        {"_id": ObjectId(user_id)}, {"agent.messages_used_this_month": 1}
    )
    new_messages_used: int = (updated_agent or {}).get("agent", {}).get(
        "messages_used_this_month", 1
    )

    return ChatResponse(
        reply=reply,
        mood="speaking",
        messages_used=new_messages_used,
        messages_limit=messages_limit,
        level_up=xp_result["level_up"],
        new_level=xp_result["new_level"],
        new_stage=xp_result["new_stage"],
        avatar_regen_pending=xp_result.get("avatar_regen_pending", False),
        quiz_config=quiz_config,
        quiz_offer=quiz_offer,
    )


@router.post("/generate-personality", response_model=GeneratePersonalityResponse)
async def generate_personality(
    body: GeneratePersonalityRequest,
    current_user: dict = Depends(get_firebase_user),
) -> GeneratePersonalityResponse:
    """Generate/rewrite the Smart Pet's custom personality (Plus/Pro only, monthly budget)."""
    user_id = current_user.get("user_id")
    user_doc = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user_doc:
        raise HTTPException(status_code=404, detail="user_not_found")

    tier = _resolve_tier(user_doc)

    # Gate: Free tier cannot use custom personality (per D-10)
    if not SUBSCRIPTION_PLANS[tier]["features"].get("agent_custom_personality"):
        raise HTTPException(status_code=403, detail="custom_personality_requires_plus")

    # Monthly limit (D-12)
    limit = PERSONALITY_LIMITS.get(tier, 1)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")

    # Atomic counter check + increment (race-condition safe)
    update_result = await users_collection.update_one(
        {
            "_id": ObjectId(user_id),
            "$or": [
                {"agent.personality_generation_reset_date": current_month,
                 "agent.personality_generations_used": {"$lt": limit}},
                {"agent.personality_generation_reset_date": {"$ne": current_month}},
            ],
        },
        {
            "$inc": {"agent.personality_generations_used": 1},
            "$set": {"agent.personality_generation_reset_date": current_month},
        },
    )
    if update_result.matched_count == 0:
        raise HTTPException(status_code=402, detail="personality_generation_limit_reached")

    # Build sanitized prompt
    user_prompt_parts = []
    if body.pet_species:
        user_prompt_parts.append(f"Pet species: {body.pet_species}")
    if body.style_hints:
        safe_hints = _sanitize_style_hints(body.style_hints)
        if safe_hints:
            user_prompt_parts.append(f"Desired style: {safe_hints}")
    user_prompt = "\n".join(user_prompt_parts) or "Generate a warm and curious personality."

    # One-shot Gemini call with safety settings
    model_name = AGENT_MODELS[tier]
    gen_model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=_PERSONALITY_SYSTEM_PROMPT,
        generation_config=genai.types.GenerationConfig(temperature=0.9, max_output_tokens=512),
    )
    try:
        response = gen_model.generate_content(user_prompt, safety_settings=PERSONALITY_SAFETY_SETTINGS)
        personality_text = response.text.strip()
    except Exception as exc:
        # Safety block or API error
        if "block" in str(exc).lower():
            raise HTTPException(status_code=422, detail="personality_content_blocked")
        raise HTTPException(status_code=502, detail="personality_generation_failed")

    # Persist generated personality to user doc
    updated = await users_collection.find_one_and_update(
        {"_id": ObjectId(user_id)},
        {"$set": {"agent.personality_text": personality_text}},
        return_document=True,
    )
    used = (updated or {}).get("agent", {}).get("personality_generations_used", 1)

    return GeneratePersonalityResponse(
        personality_text=personality_text,
        generations_used=used,
        generations_limit=limit,
        reset_date=current_month,
    )
