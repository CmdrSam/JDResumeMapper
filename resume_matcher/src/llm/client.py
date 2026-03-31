import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_TIMEOUT_SECONDS = 60.0


def _load_env() -> None:
    """Load `.env` from nearest ancestor (repo root, etc.). Safe for shallow paths e.g. Docker `/app/...`."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        env = parent / ".env"
        if env.is_file():
            load_dotenv(env)
            break
    load_dotenv()


class LLMClient:
    """DeepSeek Chat Completions (OpenAI-compatible HTTP API)."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        _load_env()
        key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise ValueError("Set DEEPSEEK_API_KEY in your environment or .env file.")
        self.model = model or os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)
        url = base_url or os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
        timeout = timeout_seconds
        if timeout is None:
            raw = os.getenv("DEEPSEEK_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
            try:
                timeout = float(raw)
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT_SECONDS
        self._client = OpenAI(api_key=key, base_url=url, timeout=timeout, max_retries=2)

    def invoke(self, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return (response.choices[0].message.content or "").strip()


def get_llm_client() -> LLMClient:
    return LLMClient()
