"""A tiny, explainable affect model for the bot.

The mood is deliberately readable rather than a black box: four axes in
``[-5, 5]`` plus short reasons.  Chat messages move the axes through a small
Chinese/English lexicon, videos move them through the vision result, and the
values drift back to neutral over time.
"""

from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import atomic_write_json, clamp, now_tz, read_json


POSITIVE = {
    "开心": 1.0, "高兴": 1.0, "喜欢": 1.1, "爱你": 1.4, "可爱": 1.0, "厉害": 0.9,
    "谢谢": 0.6, "感谢": 0.7, "棒": 0.8, "好耶": 1.0, "太强": 1.1, "温柔": 0.8,
    "抱抱": 0.9, "摸摸": 0.6, "乖": 0.6, "好看": 0.8, "好听": 0.7, "有趣": 0.7,
    "happy": 0.9, "love": 1.2, "thanks": 0.6, "great": 0.8, "nice": 0.6,
    "cute": 0.9, "good": 0.6, "awesome": 0.9,
}
NEGATIVE = {
    "难过": -0.9, "伤心": -1.0, "生气": -1.1, "讨厌": -1.2, "滚": -1.4, "闭嘴": -1.2,
    "笨": -0.9, "蠢": -1.1, "垃圾": -1.2, "烦": -0.8, "累": -0.5, "无聊": -0.5,
    "失望": -0.9, "哭": -0.7, "孤独": -0.8, "压力": -0.7, "焦虑": -0.8,
    "sad": -0.8, "angry": -1.0, "hate": -1.2, "stupid": -1.0, "bad": -0.6,
    "tired": -0.5, "boring": -0.5,
}
LAUGH = {"哈哈", "hhh", "233", "笑死", "嘿嘿", "lol", "lmao"}
CARE = {"抱抱", "摸摸", "辛苦了", "休息", "睡吧", "别累", "照顾好"}
TOPIC_BONUS = {
    "猫": 0.5, "狗": 0.45, "美食": 0.4, "游戏": 0.35, "音乐": 0.35,
    "电影": 0.3, "动画": 0.4, "旅行": 0.4, "手工": 0.3, "科技": 0.3,
}

MOOD_LABELS = [
    (82, "雀跃", "🥳", "#ffd166"),
    (66, "开心", "😊", "#ffb703"),
    (46, "平静", "🙂", "#8ecae6"),
    (30, "有点低落", "😔", "#90a4ae"),
    (0, "沮丧", "🥺", "#7b8fa1"),
]


@dataclass
class Mood:
    happy: float = 0.0
    energy: float = 0.0
    curiosity: float = 0.0
    stress: float = 0.0
    updated_at: str = ""
    reasons: deque[str] = field(default_factory=lambda: deque(maxlen=12))
    history: deque[dict] = field(default_factory=lambda: deque(maxlen=72))

    def score(self) -> int:
        raw = 50 + self.happy * 7 + self.energy * 3 + self.curiosity * 2 - self.stress * 4
        return int(round(clamp(raw, 0, 100)))

    def label(self) -> tuple[str, str, str]:
        score = self.score()
        if self.happy >= 2 and score < 66:
            return "开心", "😊", "#ffb703"
        if self.happy <= -2 and score > 30:
            return "有点低落", "😔", "#90a4ae"
        for threshold, label, emoji, color in MOOD_LABELS:
            if score >= threshold:
                return label, emoji, color
        return "沮丧", "🥺", "#7b8fa1"

    def reason(self, text: str) -> None:
        text = re.sub(r"\s+", " ", (text or "").strip())
        if text:
            self.reasons.appendleft(text[:120])

    def touch(self, dt: datetime) -> None:
        self.updated_at = dt.isoformat(timespec="seconds")
        self.history.append(
            {
                "t": self.updated_at,
                "score": self.score(),
                "happy": round(self.happy, 2),
                "energy": round(self.energy, 2),
                "curiosity": round(self.curiosity, 2),
                "stress": round(self.stress, 2),
            }
        )

    def clamp_all(self) -> None:
        self.happy = clamp(self.happy, -5, 5)
        self.energy = clamp(self.energy, -5, 5)
        self.curiosity = clamp(self.curiosity, -5, 5)
        self.stress = clamp(self.stress, -5, 5)

    def card(self) -> dict[str, Any]:
        label, emoji, color = self.label()
        return {
            "score": self.score(),
            "label": label,
            "emoji": emoji,
            "color": color,
            "happy": round(self.happy, 2),
            "energy": round(self.energy, 2),
            "curiosity": round(self.curiosity, 2),
            "stress": round(self.stress, 2),
            "happy_high": self.happy >= 2,
            "bars": {
                "高兴": round((self.happy + 5) / 10 * 100),
                "精力": round((self.energy + 5) / 10 * 100),
                "好奇": round((self.curiosity + 5) / 10 * 100),
                "压力": round((self.stress + 5) / 10 * 100),
            },
            "reasons": list(self.reasons),
            "updated_at": self.updated_at,
            "history": list(self.history)[-48:],
        }


class MoodStore:
    def __init__(self, path: Path, weight: float = 0.5, decay_per_hour: float = 0.06) -> None:
        self.path = path
        self.weight = weight
        self.decay_per_hour = decay_per_hour
        self.mood = Mood()
        self._dirty = 0
        self._load()

    def _load(self) -> None:
        raw = read_json(self.path, {})
        if not isinstance(raw, dict):
            return
        for key in ("happy", "energy", "curiosity", "stress"):
            try:
                setattr(self.mood, key, float(raw.get(key, 0)))
            except (TypeError, ValueError):
                pass
        self.mood.updated_at = str(raw.get("updated_at") or "")
        for reason in raw.get("reasons") or []:
            self.mood.reasons.append(str(reason))
        for item in raw.get("history") or []:
            if isinstance(item, dict):
                self.mood.history.append(item)
        self.mood.clamp_all()

    def save(self, force: bool = True) -> None:
        # The payload is small; writing on every change keeps the card correct
        # even if the process is stopped right after a chat message.
        self._dirty += 1
        self._dirty = 0
        payload = {
            "happy": self.mood.happy,
            "energy": self.mood.energy,
            "curiosity": self.mood.curiosity,
            "stress": self.mood.stress,
            "updated_at": self.mood.updated_at,
            "reasons": list(self.mood.reasons),
            "history": list(self.mood.history),
        }
        atomic_write_json(self.path, payload)

    # ------------------------------------------------------------- movement
    def apply_chat(self, text: str, dt: datetime | None = None) -> dict[str, Any]:
        dt = dt or now_tz()
        self.decay(dt)
        text = (text or "").strip()
        if not text:
            return self.card()
        low = text.lower()
        score = 0.0
        for word, value in POSITIVE.items():
            if word in low:
                score += value
        for word, value in NEGATIVE.items():
            if word in low:
                score += value
        if any(word in low for word in LAUGH):
            score += 0.7
        if any(word in low for word in CARE):
            score += 0.8
            self.mood.energy += 0.1
        if "?" in text or "？" in text:
            self.mood.curiosity += 0.18
        if "!" in text or "！" in text:
            self.mood.energy += 0.12
        length_bonus = min(len(text) / 120, 0.25)
        score += length_bonus if score >= 0 else -length_bonus * 0.5
        self.mood.happy += score * self.weight
        self.mood.stress = max(0.0, self.mood.stress - max(0.0, score) * 0.15)
        self.mood.clamp_all()
        if abs(score) >= 0.25:
            self.mood.reason(("被这句话暖到了：" if score > 0 else "被这句话戳到了：") + text[:60])
        self.mood.touch(dt)
        self.save(force=False)
        return self.card()

    def apply_video(
        self,
        title: str,
        sentiment: float,
        topics: list[str] | None = None,
        dt: datetime | None = None,
    ) -> dict[str, Any]:
        dt = dt or now_tz()
        self.decay(dt)
        sentiment = clamp(float(sentiment or 0), -1, 1)
        self.mood.happy += sentiment * 0.55
        self.mood.curiosity += 0.28 + min(len(topics or []) * 0.08, 0.4)
        self.mood.energy -= 0.08
        for topic in topics or []:
            for word, bonus in TOPIC_BONUS.items():
                if word in topic:
                    self.mood.happy += bonus * 0.25
                    break
        self.mood.clamp_all()
        self.mood.reason(f"刷到一个视频《{title[:50]}》，心情{'变好' if sentiment >= 0 else '有点沉'}")
        self.mood.touch(dt)
        self.save(force=False)
        return self.card()

    def apply_draw(self, prompt: str, ok: bool = True, dt: datetime | None = None) -> dict[str, Any]:
        dt = dt or now_tz()
        self.decay(dt)
        if ok:
            self.mood.happy += 0.5
            self.mood.curiosity += 0.45
            self.mood.energy -= 0.2
            self.mood.reason(f"画完了一张画：{prompt[:50]}")
        else:
            self.mood.stress += 0.4
            self.mood.reason("画画失败了，有点小挫败")
        self.mood.clamp_all()
        self.mood.touch(dt)
        self.save(force=False)
        return self.card()

    def note(self, text: str, dt: datetime | None = None) -> None:
        dt = dt or now_tz()
        self.mood.reason(text)
        self.mood.touch(dt)
        self.save(force=False)

    def decay(self, dt: datetime | None = None) -> None:
        dt = dt or now_tz()
        if not self.mood.updated_at:
            self.mood.updated_at = dt.isoformat(timespec="seconds")
            return
        try:
            last = datetime.fromisoformat(self.mood.updated_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=dt.tzinfo)
        except ValueError:
            return
        hours = max(0.0, (dt - last).total_seconds() / 3600)
        if hours < 0.05:
            return
        factor = math.exp(-self.decay_per_hour * hours)
        for key in ("happy", "energy", "curiosity", "stress"):
            setattr(self.mood, key, getattr(self.mood, key) * factor)
        self.mood.updated_at = dt.isoformat(timespec="seconds")
        self.mood.touch(dt)
        self.save(force=False)

    # ----------------------------------------------------------------- read
    def card(self) -> dict[str, Any]:
        return self.mood.card()

    def reset(self) -> dict[str, Any]:
        self.mood = Mood()
        self.save()
        return self.mood.card()

    def style_hint(self) -> str:
        label, _, _ = self.mood.label()
        if self.mood.happy >= 2:
            return "心情很好，语气轻快、会主动分享小发现"
        if self.mood.happy <= -2:
            return "心情低落，语气安静，不要强行热闹"
        if self.mood.energy <= -2:
            return "有点累，句子短一点，慢一点"
        if self.mood.curiosity >= 2:
            return f"好奇心旺盛（{label}），会追问细节"
        return f"整体{label}，自然就好"
