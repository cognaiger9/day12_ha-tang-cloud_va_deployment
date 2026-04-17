"""
LLM client — uses real OpenAI API when OPENAI_API_KEY is set,
otherwise falls back to mock responses.
"""
import time
import random
import logging

logger = logging.getLogger(__name__)

MOCK_RESPONSES = {
    "default": [
        "I'm an AI assistant. In production this response comes from OpenAI GPT.",
        "Your question has been received. Set OPENAI_API_KEY to get real answers.",
    ],
    "docker": ["Containers package your app so it runs the same everywhere. Build once, run anywhere!"],
    "deploy": ["Deployment moves your code from local machine to a server so others can use it."],
    "health": ["All systems operational."],
}


def _mock_ask(question: str, history: list[dict] | None = None) -> str:
    time.sleep(0.1 + random.uniform(0, 0.05))
    if history:
        last_user_msg = next(
            (m["content"] for m in reversed(history) if m["role"] == "user"), None
        )
        if last_user_msg:
            return f"(mock, history len={len(history)}) Based on our conversation: {random.choice(MOCK_RESPONSES['default'])}"
    q = question.lower()
    for keyword, responses in MOCK_RESPONSES.items():
        if keyword in q:
            return random.choice(responses)
    return random.choice(MOCK_RESPONSES["default"])


def ask(question: str, history: list[dict] | None = None) -> str:
    from app.config import settings

    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY not set — using mock LLM")
        return _mock_ask(question, history)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)

        messages = [{"role": "system", "content": "You are a helpful AI assistant. Be concise."}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": question})

        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            max_tokens=500,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"OpenAI call failed: {e} — falling back to mock")
        return _mock_ask(question, history)
