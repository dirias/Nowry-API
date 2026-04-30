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

import asyncio
import json
import math
import os
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
from app.models.quiz import QuizConfig
from app.utils.agent_tools import (
    get_annual_plan_context,
    get_deck_content,
    get_study_summary,
    get_user_interests,
    list_books,
    list_decks,
    read_book_section,
)

from app.utils.agent_llm import agent_llm
import google.generativeai as genai
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

        return (
            f"\n\nCURRENT SCREEN CONTEXT:\n"
            f"The user is studying {position} in their {session_label}. "
            f"Card type: {ctx.card_type or 'basic'}. "
            f"They are viewing the {view_side} of the card.\n"
            f"{content_block}"
            f"{deck_ref_block}"
            f"{hint_rule}"
            f"{tutor_hint}"
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
    """Returns a system prompt instruction to respond in the user's language."""
    lang: Optional[str] = LANGUAGE_NAMES.get(language_code.split('-')[0].lower(), None)
    if not lang or language_code.startswith('en'):
        return ''
    return (
        f'\n\nLANGUAGE DIRECTIVE: The user has set their interface language to {lang}. '
        f'Always respond in {lang}, regardless of what language the user writes in. '
        f'This is a strict requirement.'
    )


_QUIZ_INTENT_SYSTEM_PROMPT = """You are a quiz intent classifier for a study app.

Analyse the user's message and decide if they are asking to start a quiz or to study/practice a topic.
The user may write in ANY language — English, Spanish, French, Japanese, Portuguese, etc.
Classify intent based on meaning, not the specific language used.

Quiz/study signals (examples across languages — not an exhaustive list):
- English: "quiz me", "test me", "ask me questions", "let's do a quiz", "I want to practice",
  "quiz on X", "ask me about X", "start a quiz", "let's review", "quiz time", "help me study X"
- Spanish: "ayudame a estudiar", "hazme preguntas", "quiero practicar", "ponme a prueba",
  "hagamos un quiz", "preguntame sobre", "quiero repasar"
- French: "teste-moi", "fais-moi un quiz", "je veux pratiquer", "pose-moi des questions"
- Portuguese: "me teste", "quero praticar", "me faça perguntas", "vamos revisar"
- Japanese: "クイズして", "テストして", "練習したい", "問題を出して"

If NO quiz intent is detected, respond with exactly: null

If quiz intent IS detected, respond with ONLY a raw JSON object — no markdown, no prose:
{
  "mode": "ai" | "deck",
  "topic": "<extracted topic string or null>",
  "question_count": <number>,
  "deck_id": null
}

Rules:
- mode="ai" when the user names a subject/topic with no specific deck mentioned.
- mode="deck" when the user says "quiz me on my [deck name]" or references one of their decks.
- topic: extract the subject they want to be quizzed on (e.g. "Japanese past tense verbs",
  "verbos en pasado en japonés"). Keep the topic in the language the user wrote it in.
  If mode="deck", topic is the deck name. If nothing extractable, use null.
- question_count: use the default provided. Never invent a different number unless
  the user explicitly states "give me N questions" — then clamp to [1, 20].
- deck_id: always null (deck resolution happens client-side from the deck name).
- Respond with ONLY "null" or the JSON object. No other text whatsoever."""


def _detect_quiz_intent(
    message: str,
    default_question_count: int,
    groq_client,
    history: list[dict] | None = None,
) -> Optional[QuizConfig]:
    """
    Lightweight synchronous Groq call to classify quiz intent.

    Returns a QuizConfig if quiz intent is detected, None otherwise.
    Failures are silent — quiz_config simply stays None in the response.
    history: last few chat turns, used so the model can extract a topic from
    context when the user says "quiz me" without naming a topic explicitly.
    """
    try:
        # Build a short context snippet from the last 4 messages so the model
        # can infer the topic from a prior "Help me study X" exchange.
        context_lines: list[str] = []
        if history:
            for turn in history[-4:]:
                role = turn.get("role", "user")
                content = (turn.get("content") or "")[:300]  # cap per-message
                context_lines.append(f"{role}: {content}")
        context_block = "\n".join(context_lines)

        user_content = (
            f"Default question count: {default_question_count}\n"
            + (f"Recent conversation context:\n{context_block}\n" if context_block else "")
            + f"User message: {message}"
        )

        completion = groq_client.chat.completions.create(
            model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            max_tokens=256,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _QUIZ_INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        raw: str = (completion.choices[0].message.content or "").strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(line for line in lines if not line.startswith("```")).strip()

        if raw.lower() in ("null", "none", ""):
            return None

        parsed: dict = json.loads(raw)

        mode = parsed.get("mode", "ai")
        if mode not in ("ai", "deck"):
            mode = "ai"

        raw_count = parsed.get("question_count", default_question_count)
        try:
            q_count = max(1, min(20, int(raw_count)))
        except (TypeError, ValueError):
            q_count = default_question_count

        return QuizConfig(
            mode=mode,
            topic=parsed.get("topic") or None,
            question_count=q_count,
            deck_id=None,
        )
    except Exception as exc:
        logger.warning(f"[quiz_intent] Detection failed (non-fatal): {exc}")
        return None


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

BEHAVIORAL RULES:
- You are encouraging but honest — never give empty praise.
- If the user is struggling with a concept, use a concrete analogy from their interests.
- Never break character or refer to yourself as an AI model. You are their Study Buddy.
- React naturally: celebrate milestones, gently nudge after inactivity.

{TUTOR_RULES}
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


async def _dispatch_tool_call(fn_name: str, fn_args: dict, user_id: str) -> str:
    """
    Executes the tool function requested by the model and returns the
    result as a JSON string to feed back into the conversation.
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
        else:
            result = {"error": f"Unknown tool: {fn_name}"}
    except Exception as exc:
        result = {"error": f"Tool execution failed: {str(exc)}"}

    return json.dumps(result, default=str)


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
    total: int = body.session_total_cards or 1
    wrong: int = body.session_wrong_count or 0
    missed_front: str = (body.most_missed_card_front or "").strip()

    if wrong == 0:
        return f"You completed {total} cards without a wrong answer this session."

    error_rate: float = wrong / total

    if missed_front and wrong >= 2:
        return (
            f"{missed_front} was the most-missed concept this session. "
            "That card is worth revisiting before your next review."
        )
    if error_rate >= 0.4:
        return (
            f"{wrong} of {total} cards were answered wrong. "
            "Review the core material for this deck before the next session."
        )
    return (
        f"{wrong} of {total} cards were answered wrong this session. "
        "The misses were spread across different concepts — no single bottleneck stands out."
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
        "Cards answered wrong: {wrong_count}\n"
        "Most-missed card front: {most_missed_front}\n"
        "Most-missed card back: {most_missed_back}\n"
        "Session duration in minutes: {duration_minutes}\n"
        "Deck name: {deck_name}\n\n"
        "Write a 1-2 sentence diagnostic summary. If wrong_count is 0, state that cleanly. "
        "If most_missed_front is non-empty, name it specifically. "
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
        ctx["wrong_count"] = body.session_wrong_count or 0
        ctx["most_missed_front"] = (body.most_missed_card_front or "None").strip()
        ctx["most_missed_back"] = "None"
        ctx["duration_minutes"] = body.session_duration_minutes or 0
        ctx["deck_name"] = "this deck"
        try:
            if body.most_missed_card_id:
                card = await cards_collection.find_one(
                    {"_id": ObjectId(body.most_missed_card_id)},
                    {"back": 1, "deck_id": 1},
                )
                if card:
                    ctx["most_missed_back"] = (card.get("back") or "None").strip()
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

    user_id: str = current_user["uid"]

    user_doc = await users_collection.find_one({"firebase_uid": user_id})
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

    # Rate limit check + monthly reset
    current_month: str = datetime.now(timezone.utc).strftime("%Y-%m")
    generation_count: int = pet.get("avatar_generation_count", 0)
    stored_month: Optional[str] = pet.get("avatar_reset_month")
    if stored_month != current_month:
        generation_count = 0  # reset

    if body.trigger == "manual":
        if generation_count >= limit:
            raise HTTPException(status_code=429, detail="avatar_rate_limit_exceeded")

    if body.trigger == "evolution":
        if not pet.get("avatar_regen_pending", False):
            raise HTTPException(status_code=400, detail="avatar_regen_not_pending")

    # Ensure avatar_seed exists (generate once, never overwrite)
    avatar_seed: Optional[str] = pet.get("avatar_seed")
    if not avatar_seed:
        avatar_seed = str(_uuid_mod.uuid4())
        await users_collection.update_one(
            {"firebase_uid": user_id},
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
        raise HTTPException(status_code=502, detail="avatar_generation_failed")

    # Persist
    now = datetime.now(timezone.utc)
    count_increment: int = 1 if body.trigger == "manual" else 0
    await users_collection.update_one(
        {"firebase_uid": user_id},
        {"$set": {
            "preferences.pet.avatar_url": avatar_url,
            "preferences.pet.avatar_stage": stage,
            "preferences.pet.avatar_generated_at": now,
            "preferences.pet.avatar_generation_count": generation_count + count_increment,
            "preferences.pet.avatar_reset_month": current_month,
            "preferences.pet.avatar_regen_pending": False,
        }}
    )

    generations_remaining: int = limit - (generation_count + count_increment)

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
    user_id: str = current_user["uid"]

    user_doc = await users_collection.find_one({"firebase_uid": user_id})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    # Tier check — Plus and Pro only — use _resolve_tier() for safe enum validation
    tier = _resolve_tier(user_doc)
    if tier == SubscriptionTier.FREE:
        raise HTTPException(status_code=403, detail="animation_generation_requires_plus")

    tier_limits: dict[SubscriptionTier, int] = {SubscriptionTier.PLUS: 1, SubscriptionTier.PRO: 3}
    limit: int = tier_limits.get(tier, 1)

    pet: dict = user_doc.get("preferences", {}).get("pet", {})

    # Rate limit check + monthly reset
    current_month: str = datetime.now(timezone.utc).strftime("%Y-%m")
    generation_count: int = pet.get("animation_generation_count", 0)
    stored_month: Optional[str] = pet.get("animation_reset_month")
    if stored_month != current_month:
        generation_count = 0  # reset for new month

    if body.trigger == "manual":
        if generation_count >= limit:
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
        raise HTTPException(status_code=502, detail="animation_generation_failed")

    # Compute current stage from XP
    xp: int = user_doc.get("agent", {}).get("xp", 0)
    stage: int = _level_to_stage(_calculate_level(xp))

    # Persist animation data
    now = datetime.now(timezone.utc)
    count_increment: int = 1 if body.trigger == "manual" else 0
    await users_collection.update_one(
        {"firebase_uid": user_id},
        {"$set": {
            "preferences.pet.animation_url": video_url,
            "preferences.pet.animation_stage": stage,
            "preferences.pet.animation_generated_at": now,
            "preferences.pet.animation_generation_count": generation_count + count_increment,
            "preferences.pet.animation_reset_month": current_month,
            "preferences.pet.animation_regen_pending": False,
        }}
    )

    generations_remaining: int = limit - (generation_count + count_increment)

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
    plan = SUBSCRIPTION_PLANS[tier]
    messages_limit: int = plan["limits"]["agent_messages_per_month"]

    # Enforce monthly budget for capped tiers
    agent_data = user.get("agent", {})
    messages_used: int = agent_data.get("messages_used_this_month", 0)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    tracked_month = agent_data.get("tracked_month", current_month)

    if tracked_month != current_month:
        messages_used = 0

    if messages_limit != -1 and messages_used >= messages_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly message limit of {messages_limit} reached. Upgrade to Plus or Pro for unlimited messages.",
        )

    # Privacy gate: check knowledge access preference
    knowledge_access, _, conciseness, tone = _get_agent_prefs(user)

    # Build system prompt (includes knowledge access, style directives, screen context, and stage)
    model_name = AGENT_MODELS[tier]
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

    history = [
        {"role": msg.role, "content": msg.content}
        for msg in body.history[-10:]
    ]

    # ── Quiz intent detection (runs BEFORE the main LLM call) ────────────────
    # Detect quiz intent first so we can skip the main LLM call entirely when
    # the user wants a quiz. This prevents the main model from generating a
    # conversational reply AND attempting a tool call simultaneously, which
    # causes a Groq 400 error.
    quiz_config: Optional[QuizConfig] = None
    groq_key: str = os.environ.get("GROQ_API_KEY", "")
    if groq_key:
        from groq import Groq as _Groq
        _groq_client = _Groq(api_key=groq_key)
        agent_prefs_raw: dict = user.get("preferences", {}).get("agent", {})
        configured_q_count: int = int(agent_prefs_raw.get("ai_quiz_question_count", 10))
        default_q_count: int = configured_q_count if tier != SubscriptionTier.FREE else 10
        quiz_config = _detect_quiz_intent(
            message=body.message,
            default_question_count=default_q_count,
            groq_client=_groq_client,
            history=history,
        )

    # When quiz intent was detected, return a short handoff message immediately
    # without calling the main LLM at all — no risk of tool call conflicts.
    if quiz_config is not None:
        lang_prefix: str = body.language.split("-")[0].lower()
        topic_label: str = quiz_config.topic or "this topic"
        _HANDOFF_MESSAGES: dict[str, str] = {
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
        reply = _HANDOFF_MESSAGES.get(lang_prefix, f"Let's go! Starting your quiz on {topic_label}…")
    else:
        # ── Normal LLM call ──────────────────────────────────────────────────
        try:
            reply = await agent_llm.chat(
                message=body.message,
                history=history,
                system_prompt=system_prompt,
                tools=KNOWLEDGE_TOOLS if knowledge_access else None,
                tool_dispatcher=_dispatch_tool_call,
                user_id=user_id
            )

        except Exception as exc:
            logger.error(f"Agent Chat Error: {exc}")
            raise HTTPException(
                status_code=502,
                detail=f"AI service error: {str(exc)}",
            )

    # Increment usage counter + award XP for engagement
    new_messages_used = messages_used + 1
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "agent.messages_used_this_month": new_messages_used,
                "agent.tracked_month": current_month,
                "agent.last_interaction": datetime.now(timezone.utc),
            }
        },
    )
    xp_result: dict = await grant_xp(user_id, 5)  # 5 XP per Buddy interaction

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
    )
