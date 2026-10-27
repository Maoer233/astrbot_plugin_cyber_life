"""Video understanding pipeline: download -> audio transcript + frames -> vision."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .llm import LLMHelper
from .montage import compose_video
from .platforms.base import Post
from .transcribe import Transcript, Transcriber
from .util import shorten


@dataclass
class VideoInsight:
    ok: bool = False
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    sentiment: float = 0.0
    comment_hint: str = ""
    montage: Path | None = None
    frames: int = 0
    duration: float = 0.0
    error: str = ""
    source: str = "vision"
    transcript: str = ""
    transcript_source: str = ""
    transcript_language: str = ""
    transcript_segments: int = 0
    transcript_error: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "topics": self.topics,
            "sentiment": round(self.sentiment, 2),
            "comment_hint": self.comment_hint,
            "frames": self.frames,
            "duration": round(self.duration, 1),
            "montage": str(self.montage) if self.montage else "",
            "error": self.error,
            "source": self.source,
            "transcript": self.transcript,
            "transcript_source": self.transcript_source,
            "transcript_language": self.transcript_language,
            "transcript_segments": self.transcript_segments,
            "transcript_error": self.transcript_error,
        }


POSITIVE_TITLE = ("可爱", "治愈", "开心", "搞笑", "神级", "惊艳", "绝了", "宝藏", "推荐", "好看", "好听")
NEGATIVE_TITLE = ("翻车", "失败", "悲剧", "难过", "愤怒", "吐槽", "烂", "避雷", "恐怖", "抑郁")


def title_sentiment(title: str) -> float:
    score = 0.0
    for word in POSITIVE_TITLE:
        if word in title:
            score += 0.22
    for word in NEGATIVE_TITLE:
        if word in title:
            score -= 0.25
    return max(-1.0, min(1.0, score))


def bvid_from_url(url: str) -> str:
    match = re.search(r"(BV[0-9A-Za-z]{10})", url or "")
    return match.group(1) if match else ""


def _transcript_hint(transcript: Transcript | None, limit: int = 1600) -> str:
    if not transcript or not transcript.ok or not transcript.text:
        return ""
    return shorten(transcript.text, limit)


class VideoAnalyzer:
    def __init__(self, settings: Any, llm: LLMHelper, data_dir: Path) -> None:
        self.settings = settings
        self.llm = llm
        self.data_dir = data_dir
        self.videos_dir = data_dir / "media" / "videos"
        self.sheets_dir = data_dir / "media" / "sheets"
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.sheets_dir.mkdir(parents=True, exist_ok=True)
        self.transcriber = Transcriber(settings, data_dir)

    # --------------------------------------------------------------- public
    async def analyze(self, post: Post, platform: Any) -> VideoInsight:
        title = post.title or "未命名视频"
        # Tieba / Xiaoheihe items are forum posts, not videos. Reading their
        # title and body is the correct behavior; trying to download a video
        # would only produce a misleading error.
        if (
            post.platform != "bilibili"
            and not post.extra.get("local_video")
            and not post.extra.get("video_url")
        ):
            return self._metadata_insight(
                post,
                "像刷帖子一样，针对内容本身说一句真实感受",
            )

        want_vision = self.settings.bool("vision_enabled")
        want_audio = self.settings.bool("subtitle_enabled")
        if not want_vision and not want_audio:
            return self._metadata_insight(post, "根据标题聊一句真实的感受")

        transcript: Transcript | None = None
        if want_audio and post.platform == "bilibili" and post.bvid:
            transcript = await self.transcriber.from_bilibili(post, platform)

        # Audio-only mode still needs no video download if Bilibili already has
        # a subtitle track.
        if not want_vision:
            if transcript and transcript.ok:
                return await self._text_insight(post, transcript)
            if want_audio and self.transcriber.installed:
                try:
                    video_path = await self._fetch_video(post, platform)
                    try:
                        transcript = await self.transcriber.from_file(
                            video_path,
                            key=f"bili:{post.bvid}:{post.cid}:audio-only",
                            title=title,
                            duration=post.duration,
                        )
                    finally:
                        video_path.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    logger.info(f"[cyber-life] 音频模式下载/听写跳过：{exc}")
                if transcript and transcript.ok:
                    return await self._text_insight(post, transcript)
            insight = self._metadata_insight(post, "根据标题聊一句真实的感受")
            insight.transcript_error = transcript.error if transcript else "语音转字幕未开启"
            return insight

        video_path: Path | None = None
        try:
            video_path = await self._fetch_video(post, platform)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[cyber-life] 视频下载跳过：{exc}")
            if transcript and transcript.ok:
                insight = await self._text_insight(post, transcript)
                insight.error = ""
                return insight
            return VideoInsight(
                ok=False,
                summary=shorten(post.description or title, 200),
                topics=list(post.tags[:4]),
                sentiment=title_sentiment(title),
                error=f"视频下载失败：{exc}",
                source="metadata",
                transcript_error=transcript.error if transcript else "",
            )
        insight = await self.analyze_file(
            video_path,
            title=title,
            subtitle=f"{post.platform} · {post.author} · {shorten(post.description, 80)}",
            transcript=transcript,
            cache_key=f"bili:{post.bvid}:{post.cid or post.extra.get('cid', '')}:local",
        )
        try:
            video_path.unlink(missing_ok=True)
        except OSError:
            pass
        return insight

    async def analyze_bilibili_url(self, url: str, platform: Any) -> VideoInsight:
        bvid = bvid_from_url(url)
        if not bvid:
            raise ValueError("没有在链接里找到 BV 号")
        post = await platform.client().video_detail(bvid)
        return await self.analyze(post, platform)

    async def transcript_bilibili_url(self, url: str, platform: Any) -> tuple[Post, Transcript]:
        """Fetch just the spoken text, without asking the vision model to look."""
        bvid = bvid_from_url(url)
        if not bvid:
            raise ValueError("没有在链接里找到 BV 号")
        if not self.settings.bool("subtitle_enabled"):
            raise ValueError("语音转字幕已在设置里关闭")
        post = await platform.client().video_detail(bvid)
        transcript = await self.transcriber.from_bilibili(post, platform)
        if transcript.ok:
            return post, transcript
        # No Bilibili subtitle: download only when local ASR is available, so
        # a missing model does not waste bandwidth.
        if not self.transcriber.installed:
            return post, transcript
        video_path = await self._fetch_video(post, platform)
        try:
            transcript = await self.transcriber.from_file(
                video_path,
                key=f"bili:{post.bvid}:{post.cid}:manual",
                title=post.title,
                duration=post.duration,
            )
        finally:
            try:
                video_path.unlink(missing_ok=True)
            except OSError:
                pass
        return post, transcript

    # --------------------------------------------------------------- fetch
    async def _fetch_video(self, post: Post, platform: Any) -> Path:
        if post.extra.get("local_video") and Path(post.extra["local_video"]).exists():
            return Path(post.extra["local_video"])
        direct = str(post.extra.get("video_url") or "")
        client = None
        if post.platform == "bilibili":
            client = platform.client()
            detail = await client.video_detail(post.bvid)
            if not direct:
                direct = await client.play_url(detail.bvid, detail.cid)
            post.aid = detail.aid or post.aid
            post.cid = detail.cid or post.cid
            post.duration = detail.duration or post.duration
        if not direct:
            raise RuntimeError("没有可用的视频直链")
        max_seconds = self.settings.int("video_max_seconds")
        if post.duration and post.duration > max_seconds:
            raise RuntimeError(f"视频时长 {int(post.duration)} 秒超过上限 {max_seconds} 秒")
        target = self.videos_dir / f"{post.platform}-{re.sub(r'[^0-9A-Za-z]', '', post.id)[:24]}.mp4"
        if target.exists() and target.stat().st_size > 1024:
            return target
        if client is not None:
            await client.download(direct, target)
        else:
            import httpx

            async with httpx.AsyncClient(timeout=90, follow_redirects=True) as http:
                async with http.stream("GET", direct) as response:
                    response.raise_for_status()
                    with target.open("wb") as fh:
                        async for chunk in response.aiter_bytes(256 * 1024):
                            fh.write(chunk)
        return target

    # -------------------------------------------------------------- analyze
    async def analyze_file(
        self,
        video_path: Path,
        *,
        title: str,
        subtitle: str = "",
        transcript: Transcript | None = None,
        cache_key: str = "",
    ) -> VideoInsight:
        want_vision = self.settings.bool("vision_enabled")
        montage = None
        montage_error = ""
        if want_vision:
            try:
                montage = await compose_video(
                    video_path,
                    self.sheets_dir,
                    title=title,
                    subtitle=subtitle,
                    min_frames=self.settings.int("video_min_frames"),
                    max_frames=self.settings.int("video_max_frames"),
                    ffmpeg_path=self.settings.str("ffmpeg_path"),
                )
            except Exception as exc:  # noqa: BLE001
                montage_error = f"关键帧合成失败：{exc}"
                logger.info(f"[cyber-life] {montage_error}")

        if (transcript is None or not transcript.ok) and self.settings.bool("subtitle_enabled"):
            local = await self.transcriber.from_file(
                video_path,
                key=cache_key or f"local:{video_path.stem}:{video_path.stat().st_size}",
                title=title,
            )
            if local.ok or transcript is None:
                transcript = local
            else:
                # Keep both reasons so the UI can explain why there is no text.
                transcript = Transcript(error=f"{transcript.error}；{local.error}")

        if montage is None and not (transcript and transcript.ok):
            return VideoInsight(
                ok=False,
                summary=shorten(title, 200),
                sentiment=title_sentiment(title),
                error=montage_error or "没有可用的画面或语音",
                source="metadata",
                transcript_error=transcript.error if transcript else "",
            )

        insight = VideoInsight(
            ok=True,
            summary=shorten(transcript.text if transcript and transcript.ok else title, 200),
            topics=[],
            sentiment=0.0,
            montage=montage.path if montage else None,
            frames=montage.count if montage else 0,
            duration=montage.duration if montage else (transcript.duration if transcript else 0.0),
            error=montage_error if montage is None else "",
        )
        self._attach_transcript(insight, transcript)
        if not self.llm.available:
            self._fallback_summary(insight, title, transcript, bool(montage))
            return insight
        data = await self._ask_model(
            insight,
            title=title,
            frames=montage.count if montage else 0,
            transcript=transcript,
            images=[montage.path] if montage else None,
        )
        self._apply_model_data(insight, data, title, transcript, bool(montage))
        return insight

    async def _text_insight(self, post: Post, transcript: Transcript) -> VideoInsight:
        title = post.title or "未命名视频"
        insight = VideoInsight(
            ok=True,
            summary=shorten(transcript.text, 240),
            topics=list(post.tags[:4]),
            sentiment=title_sentiment(title),
            duration=transcript.duration,
            source="audio",
        )
        self._attach_transcript(insight, transcript)
        if not self.llm.available:
            insight.comment_hint = "根据视频里说的话聊一句真实的感受"
            return insight
        data = await self._ask_model(
            insight,
            title=title,
            frames=0,
            transcript=transcript,
            images=None,
        )
        self._apply_model_data(insight, data, title, transcript, False)
        return insight

    async def _ask_model(
        self,
        insight: VideoInsight,
        *,
        title: str,
        frames: int,
        transcript: Transcript | None,
        images: list[Path] | None,
    ) -> dict[str, Any] | None:
        if frames and images:
            intro = (
                f"这是一张由视频的 {frames} 张关键帧拼成的总览图（按时间顺序，左上到右下，"
                f"每格左下角是时间戳）。"
            )
        else:
            intro = "你没有拿到画面，只有从视频声音转写出来的文字。"
        speech = _transcript_hint(transcript)
        speech_block = (
            f"\n视频里说的话（语音转写，可能有同音错字，不要逐字照抄）：\n{speech}\n"
            if speech
            else "\n没有可用的语音字幕。\n"
        )
        prompt = (
            f"{intro}视频标题：{title}。{speech_block}\n"
            "请结合画面、标题和听到的内容，输出严格 JSON，不要 markdown：\n"
            '{"summary":"80字以内的内容概括","topics":["主题1","主题2"],'
            '"sentiment":-1到1的小数,"comment_hint":"如果要在评论区留一句自然、具体、不空洞的话，可以说什么"}'
        )
        try:
            return await self.llm.complete_json(
                prompt,
                system=(
                    "你是一个认真看视频的观众，只描述你真正看到或听到的内容，"
                    "不编造画面和台词里没有的东西。"
                ),
                images=images,
                timeout=180,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] 视频理解失败：{exc}")
            return None

    @staticmethod
    def _attach_transcript(insight: VideoInsight, transcript: Transcript | None) -> None:
        if not transcript or not transcript.ok:
            insight.transcript_error = transcript.error if transcript else ""
            return
        insight.transcript = transcript.text
        insight.transcript_source = transcript.source
        insight.transcript_language = transcript.language
        insight.transcript_segments = len(transcript.segments)
        insight.transcript_error = ""

    def _fallback_summary(
        self,
        insight: VideoInsight,
        title: str,
        transcript: Transcript | None,
        has_vision: bool,
    ) -> None:
        insight.source = "vision+audio" if has_vision and transcript and transcript.ok else (
            "audio" if transcript and transcript.ok else ("vision" if has_vision else "metadata")
        )
        if transcript and transcript.ok:
            insight.summary = shorten(transcript.text, 240)
            insight.comment_hint = "根据视频里说的话聊一句真实的感受"
        else:
            insight.summary = shorten(title, 200)
            insight.comment_hint = "根据标题和画面聊一句真实的感受"
        insight.sentiment = title_sentiment(title)

    def _apply_model_data(
        self,
        insight: VideoInsight,
        data: dict[str, Any] | None,
        title: str,
        transcript: Transcript | None,
        has_vision: bool,
    ) -> None:
        if not data:
            self._fallback_summary(insight, title, transcript, has_vision)
            return
        insight.summary = shorten(str(data.get("summary") or title), 240)
        topics = data.get("topics") or []
        insight.topics = [str(item)[:24] for item in topics if str(item).strip()][:6]
        try:
            insight.sentiment = max(-1.0, min(1.0, float(data.get("sentiment", 0))))
        except (TypeError, ValueError):
            insight.sentiment = 0.0
        insight.comment_hint = shorten(str(data.get("comment_hint") or ""), 120)
        if has_vision and transcript and transcript.ok:
            insight.source = "vision+audio"
        elif transcript and transcript.ok:
            insight.source = "audio"
        else:
            insight.source = "vision"

    def _metadata_insight(self, post: Post, comment_hint: str = "") -> VideoInsight:
        title = post.title or "未命名内容"
        return VideoInsight(
            ok=True,
            summary=shorten(post.description or title, 200),
            topics=list(post.tags[:4]),
            sentiment=title_sentiment(title),
            comment_hint=comment_hint,
            source="metadata",
        )
