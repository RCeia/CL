import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from klkg.config import Settings


@dataclass
class Completion:
    texts: list[str]
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    finish_reasons: list[str] = field(default_factory=list)


class LlmClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        samples: int,
        supports_n: bool,
        seed: int | None,
        timeout: float,
        api_key: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.samples = samples
        self.supports_n = supports_n
        self.seed = seed
        self.timeout = timeout
        self.session = requests.Session()
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"

    @classmethod
    def from_settings(cls, settings: Settings) -> "LlmClient":
        llm = settings["llm"]
        return cls(
            base_url=settings.value("llm", "base_url", env="LLM_BASE_URL"),
            model=llm["model"],
            temperature=llm["temperature"],
            top_p=llm["top_p"],
            max_tokens=llm["max_tokens"],
            samples=llm["samples_per_synopsis"],
            supports_n=llm["supports_n"],
            seed=llm.get("seed"),
            timeout=llm["timeout_seconds"],
            api_key=os.environ.get("LLM_API_KEY"),
        )

    def models(self) -> list[str]:
        response = self.session.get(f"{self.base_url}/models", timeout=30)
        response.raise_for_status()
        return [model["id"] for model in response.json().get("data", [])]

    def request(self, messages: list[dict[str, str]], schema: dict[str, Any], n: int, seed: int | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "n": n,
            "response_format": {"type": "json_schema", "json_schema": {"name": "plot_facts", "schema": schema, "strict": True}},
        }
        if seed is not None:
            payload["seed"] = seed
        response = self.session.post(f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any], seed_offset: int = 0) -> Completion:
        started = time.perf_counter()
        base_seed = None if self.seed is None else self.seed + seed_offset
        if self.supports_n:
            responses = [self.request(messages, schema, self.samples, base_seed)]
        else:
            responses = [
                self.request(messages, schema, 1, None if base_seed is None else base_seed * 1000 + index)
                for index in range(self.samples)
            ]
        choices = [choice for response in responses for choice in response["choices"]]
        return Completion(
            texts=[choice["message"]["content"] or "" for choice in choices],
            prompt_tokens=sum(response.get("usage", {}).get("prompt_tokens", 0) for response in responses),
            completion_tokens=sum(response.get("usage", {}).get("completion_tokens", 0) for response in responses),
            seconds=time.perf_counter() - started,
            finish_reasons=[choice.get("finish_reason") or "" for choice in choices],
        )
