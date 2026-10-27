"""Semantic intent routing for the companion.

The normal chat model gets ``cyber_life_*`` tools, but a chat model does not
always pick a tool when the user speaks casually.  This router gives the plugin
one reliable fallback: before the normal agent runs, a very small classification
call decides whether the message is a request to operate the built-in browser.

It is deliberately conservative.  Ordinary conversation, emotional statements,
questions about a platform, and technical questions all resolve to ``none`` and
the normal chat reply continues untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from .llm import LLMHelper
from .settings import SettingsStore


ACTIONS = {
    "login",
    "confirm_login",
    "logout",
    "browse",
    "stop",
    "resume",
    "status",
    "accounts",
    "mood",
    "draw",
    "analyze",
    "usage",
    "balance",
    "none",
}

PLATFORM_ALIASES = {
    "bilibili": "bilibili",
    "b站": "bilibili",
    "b 站": "bilibili",
    "哔哩哔哩": "bilibili",
    "哔哩": "bilibili",
    "xiaoheihe": "xiaoheihe",
    "小黑盒": "xiaoheihe",
    "黑盒": "xiaoheihe",
    "tieba": "tieba",
    "贴吧": "tieba",
    "百度贴吧": "tieba",
}

_SYSTEM = """\
你是个人 AstrBot 伴侣插件的意图路由器，不负责聊天。
用户已经长期授权这个插件自主登录、刷帖，并允许它自行调整使用时间。

判断用户这句话是否在表达以下“操作意图”，输出严格 JSON：
{"action":"none|login|confirm_login|logout|browse|stop|resume|status|accounts|mood|draw|analyze|usage|balance","platform":"bilibili|xiaoheihe|tieba|","minutes":0,"daily_minutes":-1,"session_minutes":0,"access_mode":"","peak_frequency_percent":0,"url":"","prompt":"","confidence":0.0}

规则：
- browse：用户想去逛、想看视频/帖子、想放松、无聊想找点事做、让你自己决定，或明确说去某个平台玩。
- login：用户想登录、扫码、换号、重新登录。
- confirm_login：用户正在扫码流程里，说“扫好了/确认/好了/可以了”。
- logout：用户明确要退出某个平台账号。
- stop / resume：明确让插件停下或继续。
- status / accounts / mood：问插件运行状态、账号、心情卡。
- draw：想要一张画。analyze：给了 B站视频链接或 BV 号并且想看内容。
- usage：用户要改“使用时间”——每天还能刷多久、本次多玩/少玩几分钟、以后每天只玩多久、改成全天开放或仅谷时。明确说“今天多玩半小时”时 session_minutes=30；说“以后每天一小时”时 daily_minutes=60；两个都说了就都填。daily_minutes 不明确就 -1，session_minutes 不明确就 0。访问模式只填 all_day / valley_only，否则留空；peak_frequency_percent 不明确就 0。
- balance：问 DeepSeek 余额、还有多少钱、账户还能不能用。
- none：普通聊天、情绪倾诉、观点讨论、技术问题、与插件无关的内容。
- “我心情不好”“好累”“陪我聊聊”是情绪倾诉，必须是 none，不是 mood，也不是 usage。
- “B站怎么样”“你觉得贴吧好玩吗”是讨论，必须是 none。
- 只有用户确实在请求插件行动时才路由；出现平台名不等于要操作。
- 平台不明确就留空；时长不明确就 0；不要编造链接。
- 犹豫时选 none，并把 confidence 降到 0.5 以下。
- confidence 必须反映真实把握，只有 >=0.7 才会执行。"""


@dataclass
class IntentResult:
    action: str
    platform: str = ""
    minutes: int = 0
    daily_minutes: int = -1
    session_minutes: int = 0
    access_mode: str = ""
    peak_frequency_percent: int = 0
    url: str = ""
    prompt: str = ""
    confidence: float = 0.0


def normalize_platform(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return ""
    if text in PLATFORM_ALIASES:
        return PLATFORM_ALIASES[text]
    for alias, name in PLATFORM_ALIASES.items():
        if alias and alias in text:
            return name
    return ""


def normalize_action(value: Any) -> str:
    action = str(value or "").strip().lower()
    return action if action in ACTIONS else "none"


class IntentRouter:
    """Small LLM classifier used as a reliable natural-language fallback."""

    def __init__(self, settings: SettingsStore, llm: LLMHelper) -> None:
        self.settings = settings
        self.llm = llm

    @property
    def enabled(self) -> bool:
        return self.settings.bool("agent_autonomy")

    async def route(
        self,
        text: str,
        *,
        context: str = "",
        umo: str = "",
    ) -> IntentResult | None:
        text = (text or "").strip()
        if not text or len(text) > 500 or not self.enabled:
            return None
        if not self.llm.available:
            return None

        prompt = f"当前情况：{context or '无'}\n用户消息：{text}"
        try:
            raw = await self.llm.complete_json(
                prompt,
                system=_SYSTEM,
                timeout=max(4, self.settings.int("agent_router_timeout")),
                umo=umo or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[cyber-life] 意图路由失败：{exc}")
            return None
        result = self.parse(raw)
        if result and result.action != "none":
            logger.info(
                "[cyber-life] 语义路由 %s platform=%s minutes=%s confidence=%.2f",
                result.action,
                result.platform or "-",
                result.minutes,
                result.confidence,
            )
            if result.action == "usage":
                logger.info(
                    "[cyber-life] 使用时间 daily=%s session=%s mode=%s peak=%s",
                    result.daily_minutes,
                    result.session_minutes,
                    result.access_mode or "-",
                    result.peak_frequency_percent,
                )
        return result

    def parse(self, raw: dict[str, Any] | None) -> IntentResult | None:
        if not isinstance(raw, dict):
            return None
        action = normalize_action(raw.get("action"))
        if action == "none":
            return None
        try:
            confidence = float(raw.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.7:
            return None

        try:
            minutes = int(float(raw.get("minutes", 0) or 0))
        except (TypeError, ValueError):
            minutes = 0
        minutes = max(0, min(240, minutes))

        try:
            daily_minutes = int(float(raw.get("daily_minutes", -1) or -1))
        except (TypeError, ValueError):
            daily_minutes = -1
        daily_minutes = max(-1, min(240, daily_minutes))

        try:
            session_minutes = int(float(raw.get("session_minutes", 0) or 0))
        except (TypeError, ValueError):
            session_minutes = 0
        session_minutes = max(0, min(240, session_minutes))

        mode = str(raw.get("access_mode") or "").strip().lower()
        mode = {
            "全天": "all_day", "全天开放": "all_day", "all": "all_day",
            "谷时": "valley_only", "仅谷时": "valley_only", "valley": "valley_only",
        }.get(mode, mode)
        if mode not in ("all_day", "valley_only"):
            mode = ""

        try:
            peak_percent = int(float(raw.get("peak_frequency_percent", 0) or 0))
        except (TypeError, ValueError):
            peak_percent = 0
        if not 5 <= peak_percent <= 100:
            peak_percent = 0

        url = str(raw.get("url") or "").strip()
        if not url:
            match = re.search(r"https?://[^\s<>\"']+", str(raw))
            if match:
                url = match.group(0)
        prompt = str(raw.get("prompt") or "").strip()[:300]
        return IntentResult(
            action=action,
            platform=normalize_platform(raw.get("platform")),
            minutes=minutes,
            daily_minutes=daily_minutes,
            session_minutes=session_minutes,
            access_mode=mode,
            peak_frequency_percent=peak_percent,
            url=url,
            prompt=prompt,
            confidence=confidence,
        )
