"""Thin wrapper around AstrBot's current chat provider."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger


class LLMHelper:
    def __init__(self, context: Any) -> None:
        self.context = context

    def provider(self, umo: str | None = None):
        try:
            return self.context.get_using_provider(umo or None)
        except Exception:  # noqa: BLE001
            return None

    @property
    def available(self) -> bool:
        return self.provider() is not None

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[str | Path] | None = None,
        timeout: float = 120,
        umo: str | None = None,
    ) -> str | None:
        provider = self.provider(umo)
        if provider is None:
            return None
        kwargs: dict[str, Any] = {}
        if system:
            kwargs["system_prompt"] = system
        if images:
            kwargs["image_urls"] = [str(item) for item in images]
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, **kwargs),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] LLM 调用失败：{exc}")
            return None
        text = getattr(response, "completion_text", "") or ""
        return text.strip() or None

    async def complete_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[str | Path] | None = None,
        timeout: float = 120,
        umo: str | None = None,
    ) -> dict[str, Any] | None:
        text = await self.complete(
            prompt, system=system, images=images, timeout=timeout, umo=umo
        )
        if not text:
            return None
        return extract_json(text)


def extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
