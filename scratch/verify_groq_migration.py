import asyncio
import os
import json
from dotenv import load_dotenv

# Mocking parts of the app for testing
import sys
from unittest.mock import AsyncMock, MagicMock

# Load .env
load_dotenv("/Users/CVYV0H267P-didier/Nowry/Nowry-API/app/.env")

# Ensure app path in sys.path
sys.path.append("/Users/CVYV0H267P-didier/Nowry/Nowry-API")

from app.utils.agent_llm import AgentLLM
import google.generativeai as genai

async def test_groq_migration():
    llm = AgentLLM()
    print(f"Provider: {llm._get_provider()}")
    
    # Define a mock tool
    dummy_tool = [
        genai.protos.Tool(
            function_declarations=[
                genai.protos.FunctionDeclaration(
                    name="get_weather",
                    description="Get current weather",
                    parameters=genai.protos.Schema(
                        type=genai.protos.Type.OBJECT,
                        properties={
                            "location": genai.protos.Schema(type=genai.protos.Type.STRING)
                        },
                        required=["location"]
                    )
                )
            ]
        )
    ]
    
    # Test conversion
    openai_tools = llm._convert_tools_to_openai(dummy_tool)
    print("Converted Tools:")
    print(json.dumps(openai_tools, indent=2))
    
    # Mock dispatcher
    async def mock_dispatcher(name, args, user_id):
        print(f"  -> Dispatching {name} with {args}")
        return json.dumps({"temp": "22C", "status": "Sunny"})

    # Test chat (simulated)
    print("\nTesting Chat with Tool Use (Groq)...")
    try:
        reply = await llm.chat(
            message="What is the weather in London?",
            history=[],
            system_prompt="You are a helpful assistant with weather tools.",
            tools=dummy_tool,
            tool_dispatcher=mock_dispatcher,
            user_id="test_user"
        )
        print(f"\nFinal Reply: {reply}")
    except Exception as e:
        print(f"Error during chat: {e}")

if __name__ == "__main__":
    asyncio.run(test_groq_migration())
