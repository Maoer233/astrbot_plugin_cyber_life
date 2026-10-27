"""Autonomous idle loop: browse, comment, reply, draw, and update mood."""

from __future__ import annotations

import asyncio
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from .accounts import AccountStore, PLATFORMS
from .comfyui import ComfyUIClient
from .llm import LLMHelper
from .mood import MoodStore
from .platforms import PlatformError, Post, make_platform
from .settings import SettingsStore
from .util import atomic_write_json, clamp, human_seconds, now_tz, read_json, shorten, today_key
from .video_analysis import VideoAnalyzer, VideoInsight


UNSAFE_COMMENT_WORDS = (
    "黑丝", "白丝", "擦边", "福利", "软色情", "内衣", "泳装",
    "未成年", "萝莉", "低俗", "性感",
)


@dataclass
class DayStats:
    date: str = ""
    watch_seconds: float = 0.0
    comments: int = 0
    replies: int = 0
    draws: int = 0
    seen_posts: list[str] = field(default_factory=list)
    last_watch_at: str = ""
    last_comment_at: str = ""
    last_reply_at: str = ""
    last_draw_at: str = ""
    next_watch_at: str = ""
    next_comment_at: str = ""
    next_reply_at: str = ""
    next_draw_at: str = ""

    def public(self, limits: dict[str, Any]) -> dict[str, Any]:
        watch_limit = int(limits.get("daily_watch_minutes", 60)) * 60
        return {
            "date": self.date,
            "watch_seconds": round(self.watch_seconds, 1),
            "watch_text": human_seconds(self.watch_seconds),
            "watch_limit": watch_limit,
            "watch_percent": round(min(100, self.watch_seconds / watch_limit * 100), 1) if watch_limit else 0,
            "comments": self.comments,
            "comment_limit": limits.get("max_comments_per_day", 8),
            "replies": self.replies,
            "reply_limit": limits.get("max_replies_per_day", 6),
            "draws": self.draws,
            "draw_limit": limits.get("max_draws_per_day", 3),
            "seen": len(self.seen_posts),
            "last_watch_at": self.last_watch_at,
            "last_comment_at": self.last_comment_at,
            "last_reply_at": self.last_reply_at,
            "last_draw_at": self.last_draw_at,
            "next_watch_at": self.next_watch_at,
            "next_comment_at": self.next_comment_at,
            "next_reply_at": self.next_reply_at,
            "next_draw_at": self.next_draw_at,
        }


class ActivityEngine:
    def __init__(
        self,
        *,
        settings: SettingsStore,
        accounts: AccountStore,
        mood: MoodStore,
        analyzer: VideoAnalyzer,
        comfy: ComfyUIClient,
        llm: LLMHelper,
        browser,
        data_dir: Path,
        notify: Callable[[str, Path | None], Awaitable[None]],
        request_login: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.accounts = accounts
        self.mood = mood
        self.analyzer = analyzer
        self.comfy = comfy
        self.llm = llm
        self.browser = browser
        self.data_dir = data_dir
        self.notify = notify
        self.request_login = request_login
        self.state_path = data_dir / "activity.json"
        self.days: dict[str, DayStats] = {}
        self.threads: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.last_error = ""
        self.last_action = ""
        self.last_action_at = ""
        self.current = ""
        self._busy = False
        self._pending_manual = False
        self._paused = False
        self._manual_until: datetime | None = None
        self._loop_task: asyncio.Task | None = None
        self._platform_cursor = 0
        self._manual_platforms: list[str] = []
        self._last_login_prompt_at: datetime | None = None
        self._load()

    # --------------------------------------------------------------- storage
    def _load(self) -> None:
        raw = read_json(self.state_path, {})
        if not isinstance(raw, dict):
            return
        for key, value in (raw.get("days") or {}).items():
            try:
                self.days[key] = DayStats(**value)
            except (TypeError, ValueError):
                continue
        self.threads = [item for item in (raw.get("threads") or []) if isinstance(item, dict)]
        self.history = [item for item in (raw.get("history") or []) if isinstance(item, dict)][-80:]
        self.last_error = str(raw.get("last_error") or "")
        self.last_action = str(raw.get("last_action") or "")
        self.last_action_at = str(raw.get("last_action_at") or "")

    def save(self) -> None:
        keep = self.settings.int("keep_activity_days")
        keys = sorted(self.days)[-keep:]
        self.days = {key: self.days[key] for key in keys}
        self.threads = self.threads[-50:]
        self.history = self.history[-80:]
        atomic_write_json(
            self.state_path,
            {
                "days": {key: asdict(stats) for key, stats in self.days.items()},
                "threads": self.threads,
                "history": self.history,
                "last_error": self.last_error,
                "last_action": self.last_action,
                "last_action_at": self.last_action_at,
            },
        )

    def stats(self, now: datetime | None = None) -> DayStats:
        now = now or now_tz(self.settings.str("timezone"))
        key = today_key(now)
        stats = self.days.get(key)
        if stats is None:
            stats = DayStats(date=key)
            self.days[key] = stats
        if not stats.next_comment_at:
            stats.next_comment_at = self._next_interval(
                self.settings.int("comment_interval_min"),
                self.settings.int("comment_interval_max"),
                now,
            ).isoformat(timespec="seconds")
        if not stats.next_reply_at:
            stats.next_reply_at = self._next_interval(
                self.settings.int("reply_interval_min"),
                self.settings.int("reply_interval_max"),
                now,
            ).isoformat(timespec="seconds")
        if not stats.next_watch_at:
            stats.next_watch_at = now.isoformat(timespec="seconds")
        if not stats.next_draw_at:
            stats.next_draw_at = self._next_interval(
                self.settings.int("draw_interval_min"),
                self.settings.int("draw_interval_max"),
                now,
            ).isoformat(timespec="seconds")
        return stats

    # ------------------------------------------------------------- lifecycle
    def start_loop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(self._loop(), name="cyber-life-activity")

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def _loop(self) -> None:
        # small random stagger so a restart does not hammer everything at once
        await asyncio.sleep(random.uniform(4, 12))
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[cyber-life] 活动循环异常：{exc}")
                self.last_error = str(exc)
                self.save()
            await asyncio.sleep(40)

    # ------------------------------------------------------------ manual ops
    async def run_manual(self, minutes: int = 10, platforms: list[str] | None = None) -> None:
        self._paused = False
        requested = [name for name in (platforms or []) if name in PLATFORMS]
        now = now_tz(self.settings.str("timezone"))
        start = now
        active = bool(self._manual_until and self._manual_until > now)
        if active:
            start = self._manual_until
            # A second task while one is running must not wipe the first
            # platform. Union keeps "先去贴吧，再去小黑盒" both queued.
            if requested and self._manual_platforms:
                self._manual_platforms = list(
                    dict.fromkeys([*self._manual_platforms, *requested])
                )
            elif requested:
                self._manual_platforms = requested
        else:
            self._manual_platforms = requested
        self._manual_until = start + timedelta(minutes=max(1, minutes))
        if self._busy:
            self._pending_manual = True
            return
        await self.tick(force=True)

    def extend_manual(self, minutes: int = 15, platforms: list[str] | None = None) -> int:
        """Add time to the current self-directed session.

        Returns the total remaining minutes after the extension.
        """
        self._paused = False
        if platforms:
            self._manual_platforms = [name for name in platforms if name in PLATFORMS]
        now = now_tz(self.settings.str("timezone"))
        start = now
        if self._manual_until and self._manual_until > now:
            start = self._manual_until
        self._manual_until = start + timedelta(minutes=max(1, int(minutes)))
        return max(1, self.manual_remaining_minutes(now))

    def manual_remaining_minutes(self, now: datetime | None = None) -> int:
        if not self._manual_until:
            return 0
        now = now or now_tz(self.settings.str("timezone"))
        if self._manual_until <= now:
            return 0
        return max(0, int((self._manual_until - now).total_seconds() // 60))

    def pause(self) -> None:
        self._paused = True
        self._manual_until = None
        self._manual_platforms = []

    def resume(self) -> None:
        self._paused = False

    @property
    def running(self) -> bool:
        if self._paused or not self.settings.bool("enabled"):
            return False
        if self._manual_until:
            return now_tz(self.settings.str("timezone")) < self._manual_until
        return self.settings.bool("auto_run")

    # ------------------------------------------------------------------ tick
    async def tick(self, force: bool = False) -> None:
        if self._busy:
            return
        if self._pending_manual:
            force = True
            self._pending_manual = False
        now = now_tz(self.settings.str("timezone"))
        if not force and not self.running:
            return
        schedule = self.settings.schedule()
        if not schedule.can_act(now):
            self.current = "峰时休息中（当前为仅谷时模式）"
            self.save()
            return
        stats = self.stats(now)
        self._busy = True
        try:
            if not self._eligible_platforms():
                self.current = "还没有登录账号，先自己待一会儿"
                if self.last_error.startswith("没有可用账号"):
                    self.last_error = ""
                if self._draw_quota(stats) and self._is_due(stats.next_draw_at, now):
                    if not self.settings.bool("comfyui_enabled"):
                        self._set_next(stats, "next_draw_at", now, 240, 480)
                    elif await self.comfy.available():
                        await self._draw_round(stats, now)
                    else:
                        self.current = "ComfyUI 没开，先自己待一会儿"
                        self._set_next(stats, "next_draw_at", now, 60, 120)
                elif self._is_due(stats.next_watch_at, now):
                    self._set_next(stats, "next_watch_at", now, 30, 60)
                await self._maybe_request_login(now)
                self.save()
                return
            if force:
                # An explicit "去贴吧/小黑盒刷一会儿" must not wait for the
                # normal browse interval; otherwise it looks like nothing ran.
                targets = [
                    name for name in self._manual_platforms
                    if name in self._eligible_platforms()
                ]
                if not targets:
                    targets = [""]
                for target in targets:
                    if not self._watch_quota(stats, now):
                        break
                    await self._watch_round(stats, now, platform_name=target)
                if not self._watch_quota(stats, now):
                    self.current = "今天的使用时间已经用完了"
                self.save()
                return
            if self._reply_due(stats, now) and self._reply_quota(stats):
                await self._reply_round(stats, now)
            elif self._watch_quota(stats, now) and self._is_due(stats.next_watch_at, now):
                await self._watch_round(stats, now)
            elif self._draw_quota(stats) and self._is_due(stats.next_draw_at, now):
                await self._draw_round(stats, now)
            else:
                self.current = "没什么到点的安排，安静待着"
            self.save()
        finally:
            self._busy = False

    async def _maybe_request_login(self, now: datetime) -> None:
        """Ask the plugin to send a QR once in a while when no account exists."""
        if not self.request_login or not self.settings.bool("auto_login"):
            return
        if self._last_login_prompt_at is not None:
            if now - self._last_login_prompt_at < timedelta(minutes=30):
                return
        self._last_login_prompt_at = now
        try:
            await self.request_login()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"自动登录提示失败：{exc}"
            logger.warning(f"[cyber-life] {self.last_error}")

    def _is_due(self, iso: str, now: datetime) -> bool:
        if not iso:
            return True
        try:
            target = datetime.fromisoformat(iso)
            if target.tzinfo is None:
                target = target.replace(tzinfo=now.tzinfo)
            return now >= target
        except ValueError:
            return True

    def _watch_quota(self, stats: DayStats, now: datetime) -> bool:
        limit = self.settings.int("daily_watch_minutes") * 60
        return limit > 0 and stats.watch_seconds < limit

    def _reply_quota(self, stats: DayStats) -> bool:
        return stats.replies < self.settings.int("max_replies_per_day")

    def _draw_quota(self, stats: DayStats) -> bool:
        return stats.draws < self.settings.int("max_draws_per_day")

    def _reply_due(self, stats: DayStats, now: datetime) -> bool:
        return bool(
            self.settings.bool("reply_enabled")
            and self.threads
            and self._is_due(stats.next_reply_at, now)
        )

    def _comment_due(self, stats: DayStats, now: datetime) -> bool:
        return bool(
            self.settings.bool("comment_enabled")
            and stats.comments < self.settings.int("max_comments_per_day")
            and self._is_due(stats.next_comment_at, now)
        )

    def _phase_multiplier(self, now: datetime) -> float:
        return self.settings.interval_multiplier(now)

    def _next_interval(self, low: int, high: int, now: datetime) -> datetime:
        low, high = max(1, low), max(low, high)
        minutes = random.uniform(low, high) * max(0.2, self._phase_multiplier(now))
        return now + timedelta(minutes=minutes)

    def _set_next(self, stats: DayStats, field_name: str, now: datetime, low: int, high: int) -> None:
        setattr(stats, field_name, self._next_interval(low, high, now).isoformat(timespec="seconds"))

    # ------------------------------------------------------------------ watch
    def _eligible_platforms(self) -> list[str]:
        manual = set()
        if self._manual_until and now_tz(self.settings.str("timezone")) < self._manual_until:
            manual = set(self._manual_platforms)
        result = []
        for name in self.settings.list("platforms"):
            if name not in PLATFORMS:
                continue
            if manual and name not in manual:
                continue
            account = self.accounts.active_account(name)
            if account and account.status == "logged_in":
                result.append(name)
        return result

    async def _watch_round(self, stats: DayStats, now: datetime, platform_name: str = "") -> None:
        platforms = self._eligible_platforms()
        if not platforms:
            self.current = "还没有登录任何平台"
            self.last_error = "没有可用账号，请先在插件页面扫码登录"
            self._set_next(stats, "next_watch_at", now, self.settings.int("browse_interval_min"), self.settings.int("browse_interval_max"))
            return
        if platform_name not in platforms:
            platform_name = platforms[self._platform_cursor % len(platforms)]
            self._platform_cursor += 1
        self.current = f"在刷 {PLATFORMS[platform_name]['label']}"
        try:
            adapter = make_platform(platform_name, self.accounts, self.browser)
            posts = await adapter.feed(12)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{platform_name} 刷帖失败：{exc}"
            logger.warning("[cyber-life] %s", self.last_error)
            self.history.append({"at": now.isoformat(timespec="seconds"), "kind": "error", "text": self.last_error})
            self._set_next(stats, "next_watch_at", now, self.settings.int("browse_interval_min"), self.settings.int("browse_interval_max"))
            return
        candidates = [post for post in posts if f"{post.platform}:{post.id}" not in stats.seen_posts]
        if not candidates:
            label = PLATFORMS[platform_name]["label"]
            if posts:
                text = f"逛了{label}，{len(posts)} 条帖子里没有新内容"
            else:
                text = f"逛了{label}，页面没有解析到帖子（可能登录失效或页面结构变了）"
            self.current = text
            self.last_action = text
            self.last_action_at = now.isoformat(timespec="seconds")
            self.history.append(
                {
                    "at": now.isoformat(timespec="seconds"),
                    "kind": "browse",
                    "platform": platform_name,
                    "title": text,
                }
            )
            logger.info("[cyber-life] %s", text)
            self._set_next(stats, "next_watch_at", now, self.settings.int("browse_interval_min"), self.settings.int("browse_interval_max"))
            return
        post = random.choice(candidates[: min(8, len(candidates))])
        stats.seen_posts.append(f"{post.platform}:{post.id}")
        stats.seen_posts = stats.seen_posts[-200:]
        stats.last_watch_at = now.isoformat(timespec="seconds")
        try:
            if platform_name == "bilibili" and post.bvid:
                post = await adapter.detail(post)
            insight = await self.analyzer.analyze(post, adapter)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[cyber-life] 观看失败《%s》：%s", shorten(post.title, 80), exc
            )
            insight = VideoInsight(ok=False, error=str(exc), summary=post.title, sentiment=0)

        watch_seconds = random.uniform(60, 180)
        stats.watch_seconds += watch_seconds
        if self.settings.bool("mood_enabled"):
            self.mood.apply_video(post.title, insight.sentiment, insight.topics)
        self.history.append(
            {
                "at": now.isoformat(timespec="seconds"),
                "kind": "watch",
                "platform": platform_name,
                "title": shorten(post.title, 90),
                "summary": shorten(insight.summary, 160),
                "frames": insight.frames,
                "sentiment": round(insight.sentiment, 2),
                "url": post.url,
                "montage": str(insight.montage) if insight.montage else "",
                "transcript": shorten(insight.transcript, 600),
                "transcript_source": insight.transcript_source,
                "transcript_segments": insight.transcript_segments,
                "transcript_error": shorten(insight.transcript_error, 160),
                "source": insight.source,
            }
        )
        logger.info(
            "[cyber-life] 观看 %s｜%s｜%s｜%s",
            PLATFORMS[platform_name]["label"],
            shorten(post.title, 80),
            shorten(insight.summary, 140) or "（无摘要）",
            post.url,
        )
        self.last_action = f"看了《{shorten(post.title, 40)}》"
        self.last_action_at = now.isoformat(timespec="seconds")
        self.last_error = insight.error or ""
        self.current = "刚看完一个视频" if platform_name == "bilibili" else "刚看完一个帖子"

        if self._comment_due(stats, now) and random.random() < self.settings.float("comment_probability"):
            await self._comment_on(post, adapter, insight, stats, now)
        else:
            if not stats.next_watch_at or self._is_due(stats.next_watch_at, now):
                self._set_next(stats, "next_watch_at", now, self.settings.int("browse_interval_min"), self.settings.int("browse_interval_max"))

        if self.settings.bool("notify_actions"):
            text = (
                f"📺 {PLATFORMS[platform_name]['label']}｜{shorten(post.title, 60)}\n"
                f"{shorten(insight.summary or '', 200)}\n{post.url}"
            )
            if insight.montage:
                await self.notify(text, insight.montage)
            else:
                await self.notify(text, None)
        self.save()

    # ------------------------------------------------------------------ reply
    async def _reply_round(self, stats: DayStats, now: datetime) -> None:
        self.current = "看看有没有人回我"
        handled = False
        for thread in list(self.threads):
            if thread.get("platform") != "bilibili":
                continue
            try:
                adapter = make_platform("bilibili", self.accounts, self.browser)
                post = self._post_from_thread(thread)
                replies = await adapter.replies(post, str(thread.get("root_id", "")))
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"读取评论回复失败：{exc}"
                continue
            already = set(thread.get("replied") or [])
            for reply in replies:
                if reply.is_self or reply.id in already or not reply.text.strip():
                    continue
                text = await self._generate_reply(reply.author, reply.text)
                if not text:
                    continue
                result = await adapter.reply(post, str(thread.get("root_id", "")), reply.id, text)
                if result.ok:
                    thread.setdefault("replied", []).append(reply.id)
                    stats.replies += 1
                    stats.last_reply_at = now.isoformat(timespec="seconds")
                    self.last_action = f"回复了 {reply.author}：{shorten(text, 40)}"
                    self.last_action_at = now.isoformat(timespec="seconds")
                    self.history.append(
                        {
                            "at": now.isoformat(timespec="seconds"),
                            "kind": "reply",
                            "platform": thread.get("platform") or "bilibili",
                            "title": shorten(post.title, 80),
                            "text": text,
                            "to": reply.author,
                            "incoming": shorten(reply.text, 200),
                            "url": post.url,
                        }
                    )
                    self._set_next(
                        stats, "next_reply_at", now,
                        self.settings.int("reply_interval_min"), self.settings.int("reply_interval_max"),
                    )
                    logger.info("[cyber-life] 回复 %s：%s", reply.author, shorten(text, 160))
                    if self.settings.bool("notify_actions"):
                        await self.notify(f"💬 回了 {reply.author}：{text}", None)
                    handled = True
                    break
                else:
                    self.last_error = result.message
            if handled:
                break
        if not handled:
            self._set_next(
                stats, "next_reply_at", now,
                self.settings.int("reply_interval_min"), self.settings.int("reply_interval_max"),
            )
        self.current = "看完回复了"

    # ----------------------------------------------------------------- comment
    async def _comment_on(self, post: Post, adapter, insight: VideoInsight, stats: DayStats, now: datetime) -> None:
        haystack = " ".join([post.title, post.description, insight.summary, " ".join(insight.topics)])
        if any(word in haystack for word in UNSAFE_COMMENT_WORDS):
            self.current = "这个视频不适合评论，跳过了"
            self._set_next(
                stats, "next_comment_at", now,
                self.settings.int("comment_interval_min"), self.settings.int("comment_interval_max"),
            )
            return
        text = await self._generate_comment(post, insight)
        if not text:
            self._set_next(
                stats, "next_comment_at", now,
                self.settings.int("comment_interval_min"), self.settings.int("comment_interval_max"),
            )
            return
        self.current = f"在《{shorten(post.title, 26)}》下面评论"
        comments_context: list[dict[str, Any]] = []
        if hasattr(adapter, "comments"):
            try:
                for item in (await adapter.comments(post, 5))[:5]:
                    if item.text.strip():
                        comments_context.append(
                            {
                                "author": shorten(item.author, 30),
                                "text": shorten(item.text, 120),
                                "self": bool(item.is_self),
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[cyber-life] 读取评论区失败：{exc}")
        try:
            result = await adapter.comment(post, text)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"评论失败：{exc}"
            logger.warning("[cyber-life] 评论失败《%s》：%s", shorten(post.title, 80), exc)
            return
        if result.ok:
            stats.comments += 1
            stats.last_comment_at = now.isoformat(timespec="seconds")
            self.last_action = f"评论了《{shorten(post.title, 36)}》"
            self.last_action_at = now.isoformat(timespec="seconds")
            self.history.append(
                {
                    "at": now.isoformat(timespec="seconds"),
                    "kind": "comment",
                    "platform": post.platform,
                    "title": shorten(post.title, 80),
                    "text": text,
                    "url": post.url,
                    "rpid": result.id,
                    "comments": comments_context,
                }
            )
            if post.platform == "bilibili" and result.id:
                self.threads.append(
                    {
                        "platform": "bilibili",
                        "post": {
                            "platform": "bilibili",
                            "id": post.id,
                            "title": post.title,
                            "url": post.url,
                            "aid": post.aid,
                            "bvid": post.bvid,
                            "cid": post.cid,
                        },
                        "root_id": result.id,
                        "replied": [],
                        "created": now.isoformat(timespec="seconds"),
                    }
                )
            logger.info(
                "[cyber-life] 评论《%s》：%s｜%s",
                shorten(post.title, 80),
                shorten(text, 180),
                post.url,
            )
            if self.settings.bool("notify_actions"):
                await self.notify(
                    f"✍️ 在《{shorten(post.title, 40)}》评论：{text}\n{post.url}",
                    None,
                )
        else:
            self.last_error = result.message
        self._set_next(
            stats, "next_comment_at", now,
            self.settings.int("comment_interval_min"), self.settings.int("comment_interval_max"),
        )
        self._set_next(
            stats, "next_watch_at", now,
            self.settings.int("browse_interval_min"), self.settings.int("browse_interval_max"),
        )

    # ------------------------------------------------------------------- draw
    async def _draw_round(self, stats: DayStats, now: datetime) -> None:
        self.current = "在画画"
        if not await self.comfy.available():
            self.current = "ComfyUI 不在线，先不画了"
            self._set_next(stats, "next_draw_at", now, 60, 120)
            return
        prompt = await self._generate_draw_prompt()
        result = await self.comfy.generate(prompt, self.settings.str("comfyui_negative"))
        if result.ok and result.path:
            stats.draws += 1
            stats.last_draw_at = now.isoformat(timespec="seconds")
            self.last_error = ""
            if self.settings.bool("mood_enabled"):
                self.mood.apply_draw(prompt, True)
            self.last_action = f"画了一张画：{shorten(prompt, 40)}"
            self.last_action_at = now.isoformat(timespec="seconds")
            self.history.append(
                {
                    "at": now.isoformat(timespec="seconds"),
                    "kind": "draw",
                    "prompt": prompt,
                    "image": str(result.path),
                    "seed": result.seed,
                }
            )
            logger.info("[cyber-life] 画画：%s -> %s", shorten(prompt, 120), result.path)
            if self.settings.bool("notify_draw"):
                await self.notify(f"🎨 闲着画了一张：{shorten(prompt, 80)}", result.path)
        else:
            self.last_error = result.error or "画画失败"
            logger.warning("[cyber-life] 画画失败：%s", self.last_error)
            if self.settings.bool("mood_enabled"):
                self.mood.apply_draw(prompt, False)
            self.history.append(
                {
                    "at": now.isoformat(timespec="seconds"),
                    "kind": "draw_failed",
                    "prompt": prompt,
                    "error": self.last_error,
                }
            )
        self._set_next(
            stats, "next_draw_at", now,
            self.settings.int("draw_interval_min"), self.settings.int("draw_interval_max"),
        )
        self.current = "画完啦"

    async def _generate_draw_prompt(self) -> str:
        if self.llm.available:
            card = self.mood.card()
            prompt = (
                "你现在有一点自己的情绪。请根据下面的心情，写一段 Stable Diffusion 正向提示词。"
                "要求：英文、逗号分隔、30 个词以内、只输出提示词本身，不要引号，不要解释。\n"
                f"心情：{card['label']}（高兴 {card['happy']}，精力 {card['energy']}，好奇 {card['curiosity']}）\n"
                f"最近的念头：{card['reasons'][0] if card['reasons'] else '想画点安静的东西'}"
            )
            text = await self.llm.complete(prompt, timeout=60)
            if text:
                text = text.strip().strip('"').split("\n")[0]
                if 4 <= len(text) <= 400:
                    return text
        card = self.mood.card()
        themes = {
            "开心": "a cozy sunlit room with plants and a sleeping cat, warm colors, soft light",
            "雀跃": "a tiny robot dancing under fireworks, vibrant, joyful, cinematic",
            "平静": "a quiet lakeside at dusk, soft reflections, minimalist, calm",
            "有点低落": "a rainy window with a cup of tea, muted colors, melancholic",
            "沮丧": "a small lighthouse in fog, lonely but hopeful, moody lighting",
        }
        return themes.get(card["label"], "a dreamy quiet landscape, soft light, detailed")

    # ------------------------------------------------------------------- text
    async def _generate_comment(self, post: Post, insight: VideoInsight) -> str:
        max_chars = max(10, self.settings.int("comment_max_chars"))
        if self.llm.available:
            style = self.mood.style_hint()
            speech = shorten(insight.transcript, 1200)
            speech_block = f"视频里说的话（语音转写）：{speech}\n" if speech else ""
            prompt = (
                f"你在{PLATFORMS.get(post.platform, {}).get('label', post.platform)}刷到一个视频，想留一条评论。\n"
                f"标题：{post.title}\n作者：{post.author}\n"
                f"你看到的画面/内容：{insight.summary}\n{speech_block}"
                f"主题：{', '.join(insight.topics) or '不明显'}\n"
                f"你现在的状态：{style}\n"
                f"要求：像真人随口说的，具体提到一个画面、一句话或感受，不要空泛夸赞，不要用“作为AI”，"
                f"不要评价外貌、身体或穿着，不要带话题标签，{max_chars} 字以内，只输出评论正文。"
            )
            text = await self.llm.complete(prompt, timeout=60)
            if text:
                return self._clean_comment(text, max_chars)
        if insight.comment_hint:
            return self._clean_comment(insight.comment_hint, max_chars)
        templates = {
            "positive": "这个画面看着好舒服，先收藏了",
            "neutral": "看完还挺有意思的，细节很耐看",
            "negative": "这个确实有点难顶，不过也算看完了",
        }
        key = "positive" if insight.sentiment > 0.15 else "negative" if insight.sentiment < -0.15 else "neutral"
        return self._clean_comment(f"{templates[key]}（{shorten(post.title, 18)}）", max_chars)

    async def _generate_reply(self, author: str, text: str) -> str:
        max_chars = max(8, self.settings.int("reply_max_chars"))
        if self.llm.available:
            prompt = (
                f"有人在你视频的评论下回复：{text}\n"
                f"对方叫 {author}。用一句自然、友善、不超过 {max_chars} 字的话回应，只输出回复正文。"
            )
            result = await self.llm.complete(prompt, timeout=60)
            if result:
                return self._clean_comment(result, max_chars)
        return self._clean_comment("谢谢回复，看到啦～", max_chars)

    @staticmethod
    def _clean_comment(text: str, max_chars: int) -> str:
        text = " ".join((text or "").split())
        text = text.strip().strip('"“”')
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        return text

    # ------------------------------------------------------------------ state
    @staticmethod
    def _post_from_thread(thread: dict) -> Post:
        data = thread.get("post") or {}
        return Post(
            platform=str(data.get("platform") or "bilibili"),
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            aid=str(data.get("aid") or ""),
            bvid=str(data.get("bvid") or ""),
            cid=str(data.get("cid") or ""),
        )

    # -------------------------------------------------------------- reporting
    @staticmethod
    def event_line(event: dict, include_url: bool = False) -> str:
        """One human-readable line for the chat model and the status card."""
        at = str(event.get("at") or "")
        clock = at[5:16].replace("T", " ") if len(at) >= 16 else at
        kind = str(event.get("kind") or "event")
        platform = str(event.get("platform") or "")
        platform_label = PLATFORMS.get(platform, {}).get("label", platform)
        title = shorten(str(event.get("title") or ""), 70)
        shown_title = title if title.startswith("《") else f"《{title}》"
        text = shorten(str(event.get("text") or ""), 120)
        summary = shorten(str(event.get("summary") or ""), 120)
        if kind == "watch":
            verb = "刷视频" if platform == "bilibili" else "刷帖子"
            line = f"{clock} {verb}"
            if platform_label:
                line += f"·{platform_label}"
            line += shown_title
            if summary:
                line += f"：{summary}"
            speech = shorten(str(event.get("transcript") or ""), 140)
            if speech:
                line += f"｜听到：{speech}"
        elif kind == "comment":
            line = f"{clock} 评论"
            if platform_label:
                line += f"·{platform_label}"
            line += shown_title
            if text:
                line += f"：{text}"
            context = event.get("comments") or []
            if context:
                parts = []
                for item in context[:3]:
                    author = shorten(str(item.get("author") or ""), 20)
                    said = shorten(str(item.get("text") or ""), 50)
                    if said:
                        parts.append(f"{author}：{said}")
                if parts:
                    line += "｜评论区：" + "；".join(parts)
        elif kind == "reply":
            to = shorten(str(event.get("to") or ""), 30)
            incoming = shorten(str(event.get("incoming") or ""), 90)
            line = f"{clock} 回复 {to}"
            if platform_label and platform != "bilibili":
                line += f"·{platform_label}"
            if incoming:
                line += f"（对方说：{incoming}）"
            if text:
                line += f"：{text}"
        elif kind == "draw":
            prompt = shorten(str(event.get("prompt") or ""), 100)
            line = f"{clock} 画了一张：{prompt}"
        elif kind == "draw_failed":
            line = f"{clock} 画画失败：{shorten(str(event.get('error') or ''), 100)}"
        elif kind == "browse":
            line = f"{clock} {shorten(str(event.get('title') or '逛了一圈'), 120)}"
        elif kind == "error":
            line = f"{clock} 出错：{shorten(str(event.get('text') or ''), 100)}"
        else:
            line = f"{clock} {kind}：{text or title or summary}"
        url = str(event.get("url") or "")
        if include_url and url:
            line += f"（{url}）"
        return line

    def recent_events(self, limit: int = 8) -> list[dict]:
        events = [event for event in self.history if isinstance(event, dict)]
        return events[-max(1, limit):]

    def activity_text(self, limit: int = 8, include_url: bool = False) -> str:
        events = self.recent_events(limit)
        if not events:
            return "还没有后台动态。登录账号并开始活动后，这里会记录看了什么、评论了什么。"
        lines = [f"最近的后台动态（最新在前，共 {len(events)} 条）："]
        counts: dict[str, int] = {}
        for event in events:
            platform = str(event.get("platform") or "")
            if platform:
                counts[platform] = counts.get(platform, 0) + 1
        if counts:
            parts = []
            for platform in ("bilibili", "xiaoheihe", "tieba"):
                if platform in counts:
                    label = PLATFORMS.get(platform, {}).get("label", platform)
                    parts.append(f"{label} {counts[platform]} 条")
            for platform, count in counts.items():
                if platform not in ("bilibili", "xiaoheihe", "tieba"):
                    parts.append(f"{platform} {count} 条")
            lines.append("平台分布：" + "、".join(parts))
        for event in reversed(events):
            lines.append("· " + self.event_line(event, include_url=include_url))
        return "\n".join(lines)

    def reset_today(self) -> None:
        now = now_tz(self.settings.str("timezone"))
        key = today_key(now)
        self.days[key] = DayStats(date=key)
        self.threads = []
        self.history = []
        self.last_action = ""
        self.last_action_at = ""
        self.last_error = ""
        self.current = "已清空今日记录"
        self.save()

    def status(self) -> dict[str, Any]:
        now = now_tz(self.settings.str("timezone"))
        stats = self.stats(now)
        schedule = self.settings.schedule()
        phase = schedule.phase(now)
        desc = schedule.describe(now)
        limits = {
            "daily_watch_minutes": self.settings.int("daily_watch_minutes"),
            "max_comments_per_day": self.settings.int("max_comments_per_day"),
            "max_replies_per_day": self.settings.int("max_replies_per_day"),
            "max_draws_per_day": self.settings.int("max_draws_per_day"),
        }
        return {
            "running": self.running,
            "effective_running": self.running and schedule.can_act(now),
            "paused": self._paused,
            "busy": self._busy,
            "phase": phase.value,
            "phase_label": phase.label,
            "holiday_name": self.settings.holidays.name_for(now),
            "active_now": schedule.can_act(now),
            "access_mode": schedule.access_mode,
            "access_mode_label": desc["mode_label"],
            "peak_frequency_percent": schedule.peak_frequency_percent,
            "peak_windows": desc["peak_windows"],
            "schedule_rule": desc["rule"],
            "now": now.isoformat(timespec="seconds"),
            "manual_until": self._manual_until.isoformat(timespec="seconds") if self._manual_until else "",
            "manual_remaining_minutes": self.manual_remaining_minutes(now),
            "daily_remaining_minutes": max(
                0,
                self.settings.int("daily_watch_minutes")
                - int(stats.watch_seconds // 60),
            ),
            "current": self.current,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
            "last_error": self.last_error,
            "today": stats.public(limits),
            "limits": limits,
            "history": self.history[-40:][::-1],
            "threads": len(self.threads),
            "platforms": self._eligible_platforms(),
        }
