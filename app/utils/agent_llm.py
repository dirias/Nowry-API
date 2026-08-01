import os
import re
import json
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime
import google.generativeai as genai
from groq import Groq
from app.utils.logger import get_logger
from app.config.subscription_plans import AGENT_MODELS

logger = get_logger(__name__)


class ToolCallRejectedError(Exception):
    """Raised by a tool dispatcher to veto a model-initiated tool call.

    Signals the provider loop that the attempted call must be discarded and the
    turn re-run WITHOUT the rejected tool, so the model answers the user's
    message directly instead. This is a control-flow signal, not a failure —
    it must never surface to the end user.
    """

    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"{tool_name}: {reason}")


class AgentLLM:
    """
    Unified LLM interface for the Study Buddy.
    Supports Gemini (native) and Groq (OpenAI-compatible).
    """

    def __init__(self):
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.groq_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        
        if self.gemini_api_key:
            genai.configure(api_key=self.gemini_api_key)
            
        self.groq_client = Groq(api_key=self.groq_api_key) if self.groq_api_key else None

    def _get_provider(self) -> str:
        """Prioritize Groq if available to save Gemini quota, fallback to Gemini."""
        if self.groq_api_key:
            return "groq"
        if self.gemini_api_key:
            return "gemini"
        raise ValueError("No valid AI provider configuration found (Missing GEMINI_API_KEY or GROQ_API_KEY)")

    def _convert_tools_to_openai(self, gemini_tools: Optional[List[Any]]) -> Optional[List[Dict[str, Any]]]:
        """Convert Gemini Tool protos to OpenAI-style tool definitions.

        Raises ValueError if the tools list is non-empty but no function declarations
        could be extracted, so that tool loss is never silent.
        """
        if not gemini_tools:
            return None

        openai_tools = []
        # Gemini tools are a list containing one Tool object with multiple declarations
        for tool in gemini_tools:
            if hasattr(tool, 'function_declarations'):
                for decl in tool.function_declarations:
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": decl.name,
                            "description": decl.description,
                            "parameters": self._convert_schema_to_openai(decl.parameters)
                        }
                    })

        if not openai_tools:
            # A non-empty tools list that converts to nothing means the proto
            # structure was unexpected. Raise rather than silently drop tools,
            # which would cause the model to fall back to XML-format function calls.
            raise ValueError(
                "[AgentLLM] _convert_tools_to_openai produced 0 tools from a non-empty input — "
                "Gemini protos did not expose 'function_declarations'. "
                "Cannot proceed without tools; check KNOWLEDGE_TOOLS definition."
            )

        logger.info(f"[AgentLLM] Converted {len(openai_tools)} tools for Groq: {[t['function']['name'] for t in openai_tools]}")
        return openai_tools

    def _convert_schema_to_openai(self, gemini_schema: Any) -> Dict[str, Any]:
        """Deep convert Gemini Schema to JSON Schema."""
        if not gemini_schema:
            return {"type": "object", "properties": {}}
            
        schema = {"type": "object", "properties": {}, "required": []}
        
        # In Gemini SDK, parameters might be a Schema object
        properties = getattr(gemini_schema, 'properties', {})
        required = getattr(gemini_schema, 'required', [])
        
        for name, prop_schema in properties.items():
            # Basic type conversion
            prop_type = "string"
            # genai.protos.Type mapping
            if hasattr(prop_schema, 'type'):
                g_type = prop_schema.type
                if "STRING" in str(g_type): prop_type = "string"
                elif "NUMBER" in str(g_type): prop_type = "number"
                elif "INTEGER" in str(g_type): prop_type = "integer"
                elif "BOOLEAN" in str(g_type): prop_type = "boolean"
                elif "ARRAY" in str(g_type): prop_type = "array"
                elif "OBJECT" in str(g_type): prop_type = "object"

            schema["properties"][name] = {
                "type": prop_type,
                "description": getattr(prop_schema, 'description', '')
            }
            
        schema["required"] = list(required)
        return schema

    async def chat(
        self,
        message: str,
        history: List[Dict[str, str]],
        system_prompt: str,
        tools: Optional[Any] = None,
        tool_dispatcher: Optional[callable] = None,
        user_id: str = None,
        tier: str = None,
    ) -> str:
        provider = self._get_provider()

        if provider == "gemini":
            return await self._chat_gemini(message, history, system_prompt, tools, tool_dispatcher, user_id, tier=tier)
        else:
            return await self._chat_groq(message, history, system_prompt, tools, tool_dispatcher, user_id, tier=tier)

    async def _chat_gemini(self, message, history, system_prompt, tools, tool_dispatcher, user_id, tier: str = None):
        # Resolve model based on tier — falls back to gemini-flash-latest for unknown/None tiers
        model_name = AGENT_MODELS.get(tier, "models/gemini-flash-latest") if tier else "models/gemini-flash-latest"
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=system_prompt,
            tools=tools,
        )
        
        gemini_history = []
        for msg in history:
            # Gemini roles: "user", "model"
            role = msg["role"]
            if role == "assistant": role = "model"
            gemini_history.append({"role": role, "parts": [msg["content"]]})
            
        chat_session = model.start_chat(history=gemini_history)
        response = chat_session.send_message(message)
        
        # Function calling loop
        MAX_ROUNDS = 5
        rounds = 0
        while tools and rounds < MAX_ROUNDS:
            fn_calls = []
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if part.function_call.name:
                        fn_calls.append(part.function_call)
            
            if not fn_calls: break
            
            tool_results = []
            for fn_call in fn_calls:
                fn_name = fn_call.name
                fn_args = dict(fn_call.args)
                logger.info(f"[Gemini Tool] Calling {fn_name} with {fn_args}")
                try:
                    result_str = await tool_dispatcher(fn_name, fn_args, user_id)
                except ToolCallRejectedError as rej:
                    logger.warning(
                        "[Gemini] Server-side gate rejected tool '%s' (%s) — "
                        "instructing the model to answer directly.",
                        fn_name, rej.reason,
                    )
                    result_str = json.dumps({
                        "error": (
                            f"Tool call rejected: {rej.reason} "
                            "Do not call this tool again this turn. Answer the "
                            "user's message directly with text instead."
                        )
                    })
                tool_results.append(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=fn_name, response={"result": result_str}
                        )
                    )
                )
            
            response = chat_session.send_message(tool_results)
            rounds += 1
            
        return response.text

    async def intent_yes_no(self, instruction: str, user_message: str) -> Optional[bool]:
        """Tiny, cheap YES/NO classification of a single user message.

        Used for deterministic server-side tool gating (e.g. "does this message
        explicitly ask to be quizzed?"). Returns True for YES, False for NO, and
        None when the classifier is unavailable, errors, or produces an
        unparseable verdict — the CALLER decides the fail-open/fail-closed
        policy for None.

        Runs on Groq only (the provider this gate exists to police). Uses
        GROQ_INTENT_MODEL when set, otherwise the main chat model — same
        availability guarantees, negligible cost at ~5 output tokens.
        """
        if not self.groq_client:
            return None
        try:
            intent_model = os.getenv("GROQ_INTENT_MODEL") or self.groq_model
            completion = self.groq_client.chat.completions.create(
                model=intent_model,
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": user_message},
                ],
                temperature=0,
                max_completion_tokens=256,  # headroom for reasoning models (gpt-oss)
            )
            content = (completion.choices[0].message.content or "").strip()
            match = re.search(r"\b(YES|NO)\b", content.upper())
            if match:
                return match.group(1) == "YES"
            logger.warning(f"[AgentLLM] intent_yes_no unparseable verdict: {content!r}")
            return None
        except Exception as exc:
            logger.warning(f"[AgentLLM] intent_yes_no classifier failed: {exc}")
            return None

    @staticmethod
    def _is_tool_use_failed(exc: Exception) -> bool:
        """
        True when Groq rejected the MODEL'S OWN malformed tool call (HTTP 400,
        error code 'tool_use_failed').

        Known Groq/Llama failure mode: the model emits the arguments JSON inside
        the function NAME (e.g. '<function=start_quiz {"confidence": ...}>')
        instead of a structured tool_call, so Groq's server-side validator
        rejects it as "tool ... was not in request.tools" even though the tool
        WAS sent. This is a model-output defect, not a request defect — safe to
        retry once without tools. Any other error must propagate unchanged.
        """
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error", body)
            if isinstance(err, dict) and err.get("code") == "tool_use_failed":
                return True
        return "tool_use_failed" in str(exc)

    @staticmethod
    def _strip_function_calls(text: str) -> str:
        """
        Strip Llama/Groq text-based function call artifacts from the response.
        Llama sometimes outputs one of these forms instead of a structured tool call:
          - Self-closing:  <function=name/>
          - Paired:        <function=name>{...}</function>
          - Empty paired:  <function=name></function>
          - Truncated:     <function=name>{... (no closing tag, end of string)
        All four forms must be removed before the text is appended to history or
        returned to the caller, otherwise Groq returns a 400 on the next turn.
        """
        if not text:
            return text
        # 1. Self-closing tags: <function=name/> — the most common missed case
        cleaned = re.sub(r'<function=\w+\s*/>', '', text)
        # 2. Paired open/close tags with any content (including empty): <function=name>...</function>
        cleaned = re.sub(r'<function=\w+>.*?</function>', '', cleaned, flags=re.DOTALL)
        # 3. Unclosed/truncated tags at end of string: <function=name>{...
        cleaned = re.sub(r'<function=\w+>[^<]*$', '', cleaned)
        return cleaned.strip()

    async def _chat_groq(self, message, history, system_prompt, tools, tool_dispatcher, user_id, tier: str = None):
        openai_tools = self._convert_tools_to_openai(tools)

        # For Groq/OpenAI models, we append a strict tool-calling formatting directive
        # to ensure they don't hallucinate conversational text inside the arguments JSON.
        strict_tools_directive = (
            "\n\nSTRICT TOOL CALLING RULES:\n"
            "When you decide to call a tool, you MUST provide ONLY a valid JSON object in the 'arguments' field. "
            "NEVER include conversational text, preambles, or explanations inside the arguments object. "
            "The arguments must strictly follow the JSON schema provided."
        )
        messages = [{"role": "system", "content": system_prompt + strict_tools_directive}]
        # History
        for msg in history:
            # Groq/OpenAI roles: "user", "assistant"
            role = msg["role"]
            if role == "model": role = "assistant"
            messages.append({"role": role, "content": msg["content"]})
        # Current message
        messages.append({"role": "user", "content": message})
        
        MAX_ROUNDS = 5
        rounds = 0
        
        while rounds < MAX_ROUNDS:
            try:
                # Groq completion
                completion_kwargs = {
                    "model": self.groq_model,
                    "messages": messages,
                }
                if openai_tools:
                    completion_kwargs["tools"] = openai_tools
                    completion_kwargs["tool_choice"] = "auto"
                completion = self.groq_client.chat.completions.create(**completion_kwargs)
            except Exception as e:
                if openai_tools and self._is_tool_use_failed(e):
                    # Known Groq/Llama failure mode: the model emitted a malformed
                    # tool call (arguments embedded in the function name), which
                    # Groq's validator rejects with 400 'tool_use_failed'. Degrade
                    # gracefully: log it, then retry ONCE without tools so the user
                    # gets a plain-text answer instead of a 502. Tools are disabled
                    # for the remainder of this turn only — the next request starts
                    # fresh with the full tool set.
                    logger.error(
                        "[Groq] tool_use_failed — model emitted a malformed tool call; "
                        "retrying this turn without tools. Original error: %s", e,
                    )
                    openai_tools = None
                    try:
                        completion = self.groq_client.chat.completions.create(
                            model=self.groq_model,
                            messages=messages,
                        )
                    except Exception as retry_exc:
                        logger.error(f"Groq API Error (tool-less retry also failed): {retry_exc}")
                        raise retry_exc
                else:
                    logger.error(f"Groq API Error: {e}")
                    # Optional: try fallback to Gemini here?
                    raise e

            response_message = completion.choices[0].message

            # Sanitize any XML-format function call artifacts from the response
            # content before doing anything else with it.
            #
            # Llama 3.3 on Groq intermittently emits self-closing or paired
            # <function=name/> tags as plain text instead of a structured tool_call
            # object. If such content reaches the next Groq API call — either as an
            # appended assistant message or in the continuation of this turn — Groq
            # returns a 400 "tool_use_failed". We must strip ALL tag variants here,
            # before the early-return and before the append.
            #
            # Covered variants (see _strip_function_calls docstring):
            #   <function=name/>          ← self-closing (the reported bug)
            #   <function=name>...</function>   ← paired
            #   <function=name>{...       ← truncated/unclosed
            if response_message.content and not response_message.tool_calls:
                sanitized_content = self._strip_function_calls(response_message.content)
                if sanitized_content != response_message.content:
                    logger.warning(
                        "[Groq] XML-format function call artifact detected and stripped. "
                        "Tool name may have been: %s",
                        re.findall(r'<function=(\w+)', response_message.content),
                    )
                    # Return immediately — a message that contained only a function-call
                    # artifact has no useful text content to continue with, and we must
                    # not append the dirty content to history.
                    return sanitized_content if sanitized_content else (
                        "I wasn't able to retrieve that information right now. Please try again."
                    )

            assistant_msg_index = len(messages)
            messages.append(response_message)

            if not response_message.tool_calls:
                return self._strip_function_calls(response_message.content)

            # Handle tool calls
            rejected_tool: Optional[str] = None
            for tool_call in response_message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)

                logger.info(f"[Groq Tool] Calling {fn_name} with {fn_args}")
                try:
                    result_str = await tool_dispatcher(fn_name, fn_args, user_id)
                except ToolCallRejectedError as rej:
                    rejected_tool = fn_name
                    logger.warning(
                        "[Groq] Server-side gate rejected tool '%s' (%s) — "
                        "re-running the turn without that tool.",
                        fn_name, rej.reason,
                    )
                    break

                messages.append({
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": fn_name,
                    "content": result_str,
                })

            if rejected_tool is not None:
                # Discard the vetoed assistant tool-call message (and any tool
                # results appended this round), remove the rejected tool from the
                # request, and re-run the turn — the model then answers directly.
                # Tools are restored on the next request; this is per-turn only.
                del messages[assistant_msg_index:]
                openai_tools = [
                    t for t in (openai_tools or [])
                    if t["function"]["name"] != rejected_tool
                ] or None
                rounds += 1
                continue

            rounds += 1
            
        return self._strip_function_calls(
            response_message.content or "I processed the tools but couldn't generate a final response."
        )

agent_llm = AgentLLM()
