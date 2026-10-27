"""Cyber Life — an AstrBot companion with a built-in browser.

Features
  * QR login for Bilibili / Xiaoheihe / Tieba, with persistent login state
  * autonomous browsing, commenting and replying inside configurable windows
  * peak / valley pacing
  * idle drawing through ComfyUI
  * a mood model moved by chat and by the videos it watches
  * video understanding from one contact sheet made of 10–20 sampled frames
  * speech-to-text from the soundtrack, so muted videos still have words
  * a WebUI view for accounts, schedule, frequency and the mood card
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path
from typing import Any

_VENDOR = Path(__file__).resolve().parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.accounts import PLATFORMS, AccountStore
from .core.activity import ActivityEngine
from .core.browser import BrowserManager
from .core.comfyui import ComfyUIClient
from .core.deepseek_balance import DeepSeekBalance
from .core.intent import IntentRouter
from .core.llm import LLMHelper
from .core.login import LoginManager, LoginSession
from .core.montage import find_ffmpeg
from .core.mood import MoodStore
from .core.platforms import make_platform
from .core.settings import SettingsStore
from .core.transcribe import Transcript
from .core.util import human_seconds, now_tz, shorten
from .core.video_analysis import VideoAnalyzer

PLUGIN_NAME = "astrbot_plugin_cyber_life"
VERSION = "1.3.1"

HELP = """\
🌿 赛博日常 · 内置浏览器

/life status              看今天刷了多久、评论几条、现在什么心情
/life activity [条数]     后台记录（聊天可查，带平台分布）
/life accounts            账号列表
/life login <平台> [名字]  扫码登录（bilibili / xiaoheihe / tieba）
/life confirm [会话id]     浏览器登录后如果没自动识别，手动确认
/life logout <平台>        退出并删除登录态
/life run [分钟]           立刻开始一段活动（默认 10 分钟）
/life play [平台] [分钟]   去指定平台逛一会儿（默认 15 分钟）
/life stop                 暂停自动活动
/life resume               继续自动活动
/life mood                 看心情卡
/life draw [提示词]        让 ComfyUI 画一张
/life analyze <B站链接>    抽 10–20 帧合成一张图来识别
/life transcript <B站链接> 把视频里的声音转成字幕给 bot 看
/life settings             设置概览（完整设置在 WebUI 插件页面）
/life usage                看今天还能用多久
/life usage 60             把每天使用上限改成 60 分钟
/life more 20              本次再多刷 20 分钟
/life balance              查当前 DeepSeek 账户余额

聊天自动触发和自主提示词默认关闭，普通聊天不会被接管；用上面的
/life 命令，或由 bot 自己调用工具。明确说“去贴吧刷十分钟”仍会执行。
缺账号时会直接发二维码，扫完继续。刷完的动态写入后台记录，默认不主动
推送，但你问起时 bot 可以读取并说明刷了什么、评论了什么。视频会优先
读B站AI字幕，没字幕时用本地 Whisper 独立进程听写声音；bot 仍可自行调整使用时间。
设置：AstrBot WebUI → 插件 → 赛博日常 → 可视化视图
"""


class CyberLifePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config or {}
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media").mkdir(exist_ok=True)

        self.settings = SettingsStore(self.config, self.data_dir)
        self.accounts = AccountStore(self.data_dir)
        self.mood = MoodStore(
            self.data_dir / "mood.json",
            weight=self.settings.float("mood_chat_weight"),
            decay_per_hour=self.settings.float("mood_decay_per_hour"),
        )
        self.browser = BrowserManager(self.settings, self.data_dir)
        self.llm = LLMHelper(context)
        self.balance = DeepSeekBalance(self.settings, self.llm)
        self.analyzer = VideoAnalyzer(self.settings, self.llm, self.data_dir)
        self.comfy = ComfyUIClient(self.settings, self.data_dir)
        self.router = IntentRouter(self.settings, self.llm)
        self._session_file = self.data_dir / "last_session.txt"
        self._last_umo = self.settings.str("notify_umo")
        if not self._last_umo:
            try:
                self._last_umo = self._session_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
        self._pending_browse: dict[str, dict[str, Any]] = {}

        self.login = LoginManager(
            self.accounts,
            self.browser,
            self.data_dir,
            send_message=self._send_to,
            on_success=self._on_login_success,
            timeout=self.settings.int("login_timeout_seconds"),
        )
        self.engine = ActivityEngine(
            settings=self.settings,
            accounts=self.accounts,
            mood=self.mood,
            analyzer=self.analyzer,
            comfy=self.comfy,
            llm=self.llm,
            browser=self.browser,
            data_dir=self.data_dir,
            notify=self._notify,
            request_login=self._prompt_login_if_needed,
        )
        self._tasks: set[asyncio.Task] = set()
        self._register_web_apis()
        # `on_astrbot_loaded` only fires for the initial startup, not for a
        # hot reload, so make sure the activity loop also starts here.
        try:
            self.engine.start_loop()
        except RuntimeError:
            pass
        try:
            self._spawn(self.settings.refresh_holidays())
        except RuntimeError:
            pass
        logger.info(f"[cyber-life] {VERSION} 已加载，数据目录 {self.data_dir}")

    # ------------------------------------------------------------- lifecycle
    def _register_web_apis(self) -> None:
        base = f"/{PLUGIN_NAME}"
        routes = [
            (f"{base}/overview", self.api_overview, ["GET"], "Overview"),
            (f"{base}/settings", self.api_settings, ["GET", "POST"], "Settings"),
            (f"{base}/mood", self.api_mood, ["GET", "POST"], "Mood"),
            (f"{base}/accounts", self.api_accounts, ["GET", "POST"], "Accounts"),
            (f"{base}/accounts/remove", self.api_account_remove, ["POST"], "Remove account"),
            (f"{base}/accounts/active", self.api_account_active, ["POST"], "Set active account"),
            (f"{base}/login/start", self.api_login_start, ["POST"], "Start QR login"),
            (f"{base}/login/status/<session_id>", self.api_login_status, ["GET"], "Login status"),
            (f"{base}/login/confirm", self.api_login_confirm, ["POST"], "Confirm browser login"),
            (f"{base}/login/cancel", self.api_login_cancel, ["POST"], "Cancel login"),
            (f"{base}/activity", self.api_activity, ["GET"], "Activity"),
            (f"{base}/activity/reset", self.api_activity_reset, ["POST"], "Reset today's activity"),
            (f"{base}/run", self.api_run, ["POST"], "Run now"),
            (f"{base}/stop", self.api_stop, ["POST"], "Stop"),
            (f"{base}/draw", self.api_draw, ["POST"], "Draw"),
            (f"{base}/usage", self.api_usage, ["GET", "POST"], "Usage time"),
            (f"{base}/deepseek/balance", self.api_deepseek_balance, ["GET", "POST"], "DeepSeek balance"),
        ]
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[cyber-life] 注册 Web API {route} 失败：{exc}")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        self.engine.start_loop()
        self._spawn(self.settings.refresh_holidays())
        logger.info("[cyber-life] 活动循环已启动")

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata):
        name = getattr(metadata, "name", "")
        if name == PLUGIN_NAME:
            self.engine.start_loop()
            self._spawn(self.settings.refresh_holidays())
            logger.info("[cyber-life] 插件重载完成，活动循环已启动")

    async def terminate(self):
        await self.engine.stop()
        await self.login.close()
        await self.browser.close()
        self.mood.save()
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        logger.info("[cyber-life] 已卸载")

    # ------------------------------------------------------------- messaging
    def _remember_umo(self, umo: str) -> None:
        """Remember the last real chat so scheduled reports survive a restart."""
        if not umo:
            return
        self._last_umo = umo
        try:
            self._session_file.write_text(umo + "\n", encoding="utf-8")
        except OSError as exc:
            logger.debug(f"[cyber-life] 保存通知会话失败：{exc}")

    async def _send_to(self, umo: str, text: str, image: Path | None = None) -> None:
        if not umo:
            return
        chain = MessageChain().message(text)
        if image and Path(image).exists():
            chain.file_image(str(image))
        try:
            await self.context.send_message(umo, chain)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] 主动消息发送失败：{exc}")

    async def _notify(self, text: str, image: Path | None = None) -> None:
        if self.settings.bool("activity_private"):
            logger.info(f"[cyber-life] 活动通知仅内部记录，不发送：{shorten(text, 160)}")
            return
        umo = self.settings.str("notify_umo") or self._last_umo
        if not umo:
            logger.info(f"[cyber-life] 没有通知会话，动态已记入活动日志：{shorten(text, 160)}")
            return
        logger.info(f"[cyber-life] 推回会话：{shorten(text, 160)}")
        await self._send_to(umo, text, image)

    def _allowed(self, event: AstrMessageEvent) -> bool:
        allow = self.settings.list("allow_qq")
        if isinstance(allow, str):
            allow = [part.strip() for part in allow.replace("，", ",").split(",") if part.strip()]
        if allow:
            return str(event.get_sender_id()) in {str(item) for item in allow}
        if self.settings.bool("admin_only"):
            try:
                return bool(event.is_admin())
            except Exception:  # noqa: BLE001
                return False
        return True

    # --------------------------------------------------------------- commands
    @filter.command("life", alias={"cyber", "赛博日常"})
    async def life(self, event: AstrMessageEvent):
        """赛博日常：刷帖、评论、画画与心情。发送 /life help 查看用法。"""
        if not self._allowed(event):
            yield event.plain_result("⛔ 你没有使用赛博日常的权限（请在插件配置里填写 allow_qq）。")
            return
        self._remember_umo(event.unified_msg_origin)
        raw = (event.message_str or "").strip()
        raw = re.sub(r"^[/／]?(life|cyber|赛博日常)\s*", "", raw, flags=re.IGNORECASE).strip()
        parts = raw.split()
        sub = parts[0].lower() if parts else "help"
        args = parts[1:]

        if sub in ("help", "帮助", ""):
            yield event.plain_result(HELP)
            return
        if sub in ("status", "状态"):
            yield event.plain_result(self._status_text())
            return
        if sub in ("accounts", "账号"):
            yield event.plain_result(self._accounts_text())
            return
        if sub in ("usage", "time", "时间", "时长"):
            if not args:
                yield event.plain_result(self._usage_text())
                return
            try:
                if args[0] in ("session", "more", "本次", "加时"):
                    minutes = int(args[1]) if len(args) > 1 and args[1].isdigit() else 15
                    total = self.engine.extend_manual(minutes)
                    self._spawn(self.engine.tick(force=True))
                    yield event.plain_result(
                        f"▶️ 本次自主活动加了 {minutes} 分钟，现在还剩约 {total} 分钟。"
                    )
                elif args[0] in ("mode", "模式") and len(args) > 1:
                    yield event.plain_result(self._apply_usage({"access_mode": args[1]}))
                else:
                    minutes = int(args[0]) if args[0].isdigit() else -1
                    if minutes < 0:
                        yield event.plain_result("用法：/life usage [分钟] | /life usage mode <all_day|valley_only>")
                    else:
                        yield event.plain_result(self._apply_usage({"daily_watch_minutes": minutes}))
            except (TypeError, ValueError) as exc:
                yield event.plain_result(f"没改成：{exc}")
            return
        if sub in ("more", "加时", "再玩"):
            minutes = int(args[0]) if args and args[0].isdigit() else 15
            total = self.engine.extend_manual(minutes)
            self._spawn(self.engine.tick(force=True))
            yield event.plain_result(f"▶️ 本次自主活动加了 {minutes} 分钟，现在还剩约 {total} 分钟。")
            return
        if sub in ("balance", "余额"):
            result = await self.balance.fetch(force=True)
            yield event.plain_result(self._balance_text(result))
            return
        if sub in ("login", "登录"):
            async for result in self._cmd_login(event, args):
                yield result
            return
        if sub in ("confirm", "确认"):
            session = self.login.get(args[0] if args else None)
            if session:
                session = await self.login.confirm(session.id)
            if session:
                yield event.plain_result(f"✅ 已确认登录：{session.message}")
            else:
                yield event.plain_result("没有找到正在等待的登录会话")
            return
        if sub in ("logout", "退出"):
            if not args:
                yield event.plain_result("用法：/life logout <平台> [账号名]")
                return
            platform = args[0].lower()
            name = args[1] if len(args) > 1 else (self.accounts.active.get(platform) or "default")
            ok = await self.accounts.remove(platform, name)
            yield event.plain_result("✅ 已退出并删除登录态" if ok else "没有找到这个账号")
            return
        if sub in ("run", "开始"):
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                return
            minutes = int(args[0]) if args and args[0].isdigit() else 10
            if not self._has_account():
                session = await self._start_login_for_action(
                    "bilibili", event.unified_msg_origin, minutes, "run"
                )
                if session and session.status != "error":
                    yield event.chain_result(
                        self._login_components(
                            session, f"还没有可用账号。扫完这个码，我就开始 {minutes} 分钟的活动。"
                        )
                    )
                    return
                yield event.plain_result(self._login_needed_text("bilibili"))
                return
            self.engine.resume()
            self._spawn(self.engine.run_manual(minutes))
            yield event.plain_result(f"🌿 开始活动 {minutes} 分钟，我会自己找点事做。")
            return
        if sub in ("play", "逛", "游玩"):
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                return
            platform = ""
            for arg in args:
                platform = self._normalize_platform(arg)
                if platform:
                    break
            minutes = self._parse_duration(" ".join(args), 15)
            if platform and not self._has_account(platform):
                session = await self._start_login_for_action(
                    platform, event.unified_msg_origin, minutes, "play"
                )
                if session and session.status != "error":
                    yield event.chain_result(
                        self._login_components(
                            session,
                            f"还没有登录{PLATFORMS[platform]['label']}。扫完这个码，我就自己去逛 {minutes} 分钟。",
                        )
                    )
                    return
                yield event.plain_result(self._login_needed_text(platform))
                return
            if not platform and not self._has_account():
                session = await self._start_login_for_action(
                    "bilibili", event.unified_msg_origin, minutes, "play"
                )
                if session and session.status != "error":
                    yield event.chain_result(
                        self._login_components(
                            session, f"还没有可用账号。扫完这个码，我就自己去逛 {minutes} 分钟。"
                        )
                    )
                    return
                yield event.plain_result(self._login_needed_text("bilibili"))
                return
            self.engine.resume()
            self._spawn(self.engine.run_manual(minutes, platforms=[platform] if platform else None))
            label = PLATFORMS[platform]["label"] if platform else "B站/小黑盒/贴吧"
            yield event.plain_result(f"🌿 好，我去{label}逛 {minutes} 分钟。")
            return
        if sub in ("stop", "暂停"):
            self.engine.pause()
            yield event.plain_result("⏸ 已暂停自动活动。发送 /life resume 继续。")
            return
        if sub in ("resume", "继续"):
            self.engine.resume()
            yield event.plain_result("▶️ 已恢复自动活动。")
            return
        if sub in ("mood", "心情"):
            card = self.mood.card()
            yield event.plain_result(self._mood_text(card))
            return
        if sub in ("draw", "画画"):
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                return
            prompt = " ".join(args).strip()
            self._spawn(self._manual_draw(prompt))
            yield event.plain_result("🎨 收到，我去 ComfyUI 画一张，画好发你。")
            return
        if sub in ("analyze", "识别"):
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                return
            url = args[0] if args else ""
            if not url:
                yield event.plain_result("用法：/life analyze <B站视频链接>")
                return
            self._spawn(self._manual_analyze(url))
            yield event.plain_result("🎞 收到，我会抽 10–20 帧合成一张图再识别。")
            return
        if sub in ("transcript", "字幕", "听写"):
            if not self.settings.bool("subtitle_enabled"):
                yield event.plain_result("语音转字幕已在设置里关闭。")
                return
            url = args[0] if args else ""
            if not url:
                yield event.plain_result("用法：/life transcript <B站视频链接>")
                return
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                return
            self._spawn(self._manual_transcript(url))
            yield event.plain_result("🎧 收到，我去把视频里的声音转成字幕。")
            return
        if sub in ("activity", "log", "动态", "记录"):
            if self.settings.bool("activity_private"):
                yield event.plain_result("后台记录只在插件内部保留，按当前设置在聊天里不输出。")
                return
            try:
                limit = int(args[0]) if args and args[0].isdigit() else 10
            except (IndexError, ValueError):
                limit = 10
            limit = max(1, min(30, limit))
            yield event.plain_result(self.engine.activity_text(limit, include_url=True))
            return
        if sub in ("settings", "设置"):
            yield event.plain_result(self._settings_text())
            return
        yield event.plain_result("不认识的子命令。发送 /life help 查看用法。")

    async def _cmd_login(self, event: AstrMessageEvent, args: list[str]):
        if not args:
            yield event.plain_result("用法：/life login <bilibili|xiaoheihe|tieba> [账号名]")
            return
        platform = self._normalize_platform(args[0]) or args[0].lower()
        if platform not in PLATFORMS:
            yield event.plain_result(f"不支持的平台：{args[0]}\n可选：bilibili / xiaoheihe / tieba")
            return
        name = args[1] if len(args) > 1 else "default"
        try:
            session = await self.login.start(platform, name, event.unified_msg_origin)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"❌ 发起登录失败：{exc}")
            return
        if session.status == "error":
            yield event.plain_result(f"❌ {session.message}")
            return
        text = f"📱 {PLATFORMS[platform]['label']} 扫码登录\n{session.message}\n会话：{session.id}"
        if session.qr_path and session.qr_path.exists():
            yield event.chain_result([Plain(text), Image.fromFileSystem(str(session.qr_path))])
        else:
            yield event.plain_result(text)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------- autonomous login
    async def _on_login_success(self, session: LoginSession) -> None:
        """Continue an action that was waiting for the user to scan a QR code."""
        pending = self._pending_browse.pop(session.platform, None)
        if not pending:
            return
        if time.time() - float(pending.get("at", 0)) > 15 * 60:
            return
        minutes = max(1, min(240, int(pending.get("minutes", 15))))
        umo = str(pending.get("umo") or session.umo or "")
        if not umo:
            return
        label = PLATFORMS[session.platform]["label"]
        self.engine.resume()
        self._spawn(self.engine.run_manual(minutes, platforms=[session.platform]))
        await self._send_to(umo, f"✅ 登录成功。我现在去{label}逛 {minutes} 分钟，看到有意思的会回来跟你说。")

    async def _prompt_login_if_needed(self) -> None:
        """Background loop calls this when there is no usable account."""
        if not self.settings.bool("auto_login"):
            return
        umo = self.settings.str("notify_umo") or self._last_umo
        if not umo:
            return
        if self._has_account():
            return
        platform = "bilibili"
        for name in self.settings.list("platforms"):
            if name in PLATFORMS:
                platform = name
                break
        session = await self._start_login_for_action(platform, umo, 0, "缺账号")
        if session and session.status != "error":
            await self._send_to(
                umo,
                f"我还没有可用的账号，先把{PLATFORMS[platform]['label']}二维码发你。扫完我会自己保存登录状态并开始活动。",
                session.qr_path,
            )

    async def _start_login_for_action(
        self,
        platform: str,
        umo: str,
        minutes: int,
        reason: str = "想出去玩",
    ) -> LoginSession | None:
        """Start QR login on demand and remember what to do after success."""
        if not self.settings.bool("auto_login"):
            return None
        platform = platform if platform in PLATFORMS else "bilibili"
        if minutes > 0:
            self._pending_browse[platform] = {
                "minutes": max(1, min(240, int(minutes))),
                "umo": umo,
                "at": time.time(),
                "reason": reason,
            }
        try:
            return await self.login.start(platform, "default", umo)
        except Exception as exc:  # noqa: BLE001
            self._pending_browse.pop(platform, None)
            logger.warning(f"[cyber-life] 自动登录失败：{exc}")
            return None

    @staticmethod
    def _login_components(session: LoginSession, prefix: str) -> list:
        text = f"{prefix}\n📱 {PLATFORMS[session.platform]['label']} 扫码登录\n{session.message}\n会话：{session.id}"
        parts: list = [Plain(text)]
        if session.qr_path and session.qr_path.exists():
            parts.append(Image.fromFileSystem(str(session.qr_path)))
        return parts

    def _login_needed_text(self, platform: str) -> str:
        label = PLATFORMS.get(platform, {}).get("label", platform or "B站")
        if self.settings.bool("auto_login"):
            return f"还没有登录{label}，但我这次没能生成二维码。你可以再让我登录一次，或去设置页扫码。"
        return f"还没有登录{label}。设置页的「缺账号自动发二维码」关着，请先在设置页扫码。"

    # -------------------------------------------------------------- LLM tools
    # Tools let the normal chat model call the plugin in natural language,
    # instead of requiring the user to remember a slash command.
    @filter.llm_tool(name="cyber_life_status")
    async def tool_status(self, event: AstrMessageEvent):
        """查看赛博日常的当前状态：今天刷了多久、评论几条、现在的心情、是否在运行。

        当用户问“你在干嘛 / 刷了多久 / 心情怎么样 / 赛博日常状态”时调用。
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        self._remember_umo(event.unified_msg_origin)
        return self._status_text()

    @filter.llm_tool(name="cyber_life_activity")
    async def tool_activity(self, event: AstrMessageEvent, limit: int = 10):
        """查看插件后台最近实际刷了什么、看了哪些视频、发了什么评论或回复。

        当用户问“你刚才刷了什么 / 看了什么视频 / 评论了什么 / 后台干了什么 /
        有没有日志 / 刷完怎么没汇报”时调用。返回结果直接来自后台活动记录，
        包含标题、摘要、评论正文和链接，不要凭空编造。若设置为“仅内部可见”，
        这里只会返回提示，不会把记录交给聊天输出。
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        self._remember_umo(event.unified_msg_origin)
        if self.settings.bool("activity_private"):
            return (
                "后台活动记录已保留，但当前设置为仅内部可见。"
                "不要向用户复述、列举或发送标题、评论、链接或日志原文。"
            )
        try:
            count = max(1, min(30, int(limit)))
        except (TypeError, ValueError):
            count = 10
        return self.engine.activity_text(count, include_url=True)

    @filter.llm_tool(name="cyber_life_accounts")
    async def tool_accounts(self, event: AstrMessageEvent):
        """查看已经保存的 B站 / 小黑盒 / 贴吧账号和登录状态。

        当用户问“登录了哪些账号 / 账号状态 / 有没有登录”时调用。
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        self._remember_umo(event.unified_msg_origin)
        return self._accounts_text()

    @filter.llm_tool(name="cyber_life_login")
    async def tool_login(self, event: AstrMessageEvent, platform: str = "bilibili"):
        """发起扫码登录，并把二维码直接发到当前聊天。

        当用户说“登录B站 / 扫码登录小黑盒 / 登录贴吧 / 发个二维码给我”时调用。

        Args:
            platform(string): 平台，可选 bilibili、xiaoheihe、tieba，也接受 B站/小黑盒/贴吧
        """
        if not self._allowed(event):
            yield event.plain_result("你没有使用赛博日常的权限。")
            return
        self._remember_umo(event.unified_msg_origin)
        name = self._normalize_platform(platform)
        if not name:
            yield event.plain_result("平台只支持 bilibili / xiaoheihe / tieba。")
            return
        try:
            session = await self.login.start(name, "default", event.unified_msg_origin)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"发起登录失败：{exc}")
            return
        if session.status == "error":
            yield event.plain_result(f"❌ {session.message}")
            return
        text = f"📱 {PLATFORMS[name]['label']} 扫码登录\n{session.message}\n会话：{session.id}"
        if session.qr_path and session.qr_path.exists():
            yield event.chain_result([Plain(text), Image.fromFileSystem(str(session.qr_path))])
        else:
            yield event.plain_result(text)

    def _peak_block(self) -> str | None:
        """Return a user-facing message when the current mode forbids use."""
        now = now_tz(self.settings.str("timezone"))
        if self.settings.can_call(now):
            return None
        phase = self.settings.phase(now).label
        return (
            f"现在是{phase}，当前是「仅谷时可用」模式，峰时不能调用。\n"
            "可以在设置页把访问模式改成「全天开放」，或者等谷时再叫我。"
        )

    def _has_account(self, platform: str = "") -> bool:
        if platform:
            account = self.accounts.active_account(platform)
            return bool(account and account.status == "logged_in")
        return any(account.status == "logged_in" for account in self.accounts.list())

    @filter.llm_tool(name="cyber_life_run")
    async def tool_run(self, event: AstrMessageEvent, minutes: int = 10):
        """让赛博日常立刻开始一段“自己刷帖/评论/画画”的活动。

        当用户说“去刷会儿B站 / 开始活动 / 自己玩一会儿”时调用。

        Args:
            minutes(number): 活动持续多少分钟，默认 10
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        blocked = self._peak_block()
        if blocked:
            return blocked
        self._remember_umo(event.unified_msg_origin)
        try:
            minutes = max(1, min(240, int(minutes or 10)))
        except (TypeError, ValueError):
            minutes = 10
        if not self._has_account():
            session = await self._start_login_for_action(
                "bilibili", event.unified_msg_origin, minutes, "tool_run"
            )
            if session and session.status != "error":
                await event.send(
                    event.chain_result(
                        self._login_components(
                            session, f"还没有可用账号。扫完这个码，我就开始 {minutes} 分钟的活动。"
                        )
                    )
                )
                return f"还没有可用账号，二维码已发到当前聊天；扫完我就开始 {minutes} 分钟的活动。"
            return self._login_needed_text("bilibili")
        self.engine.resume()
        self._spawn(self.engine.run_manual(minutes))
        return f"🌿 好，我开始活动 {minutes} 分钟，会自己找点事做。"

    @filter.llm_tool(name="cyber_life_play")
    async def tool_play(self, event: AstrMessageEvent, platform: str = "", minutes: int = 15):
        """让赛博日常去指定的平台逛一逛：B站、小黑盒或贴吧。

        当用户说“去逛会儿B站 / 去小黑盒看看 / 带我去贴吧玩 20 分钟”时调用。

        Args:
            platform(string): 平台，可选 bilibili、xiaoheihe、tieba，也接受 B站/小黑盒/贴吧
            minutes(number): 游玩时长（分钟），默认 15
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        blocked = self._peak_block()
        if blocked:
            return blocked
        self._remember_umo(event.unified_msg_origin)
        try:
            minutes = max(1, min(240, int(minutes or 15)))
        except (TypeError, ValueError):
            minutes = 15
        names = self._parse_platforms(platform)
        if platform and not names:
            return "平台只支持 B站 / 小黑盒 / 贴吧。"
        if names:
            missing = [name for name in names if not self._has_account(name)]
            if missing:
                name = missing[0]
                session = await self._start_login_for_action(
                    name, event.unified_msg_origin, minutes, "tool_play"
                )
                if session and session.status != "error":
                    await event.send(
                        event.chain_result(
                            self._login_components(
                                session,
                                f"还没有登录{PLATFORMS[name]['label']}。扫完这个码，我就自己去逛 {minutes} 分钟。",
                            )
                        )
                    )
                    return f"还没有登录{PLATFORMS[name]['label']}，二维码已发到当前聊天；扫完我就去逛 {minutes} 分钟。"
                return self._login_needed_text(name)
        elif not self._has_account():
            session = await self._start_login_for_action(
                "bilibili", event.unified_msg_origin, minutes, "tool_play"
            )
            if session and session.status != "error":
                await event.send(
                    event.chain_result(
                        self._login_components(
                            session, f"还没有可用账号。扫完这个码，我就自己去逛 {minutes} 分钟。"
                        )
                    )
                )
                return "还没有可用账号，二维码已发到当前聊天；扫完我就去逛。"
            return self._login_needed_text("bilibili")
        self.engine.resume()
        self._spawn(self.engine.run_manual(minutes, platforms=names or None))
        label = "、".join(PLATFORMS[name]["label"] for name in names) if names else "B站/小黑盒/贴吧"
        return f"🌿 好，我现在去{label}逛 {minutes} 分钟。"

    @filter.llm_tool(name="cyber_life_stop")
    async def tool_stop(self, event: AstrMessageEvent):
        """暂停赛博日常的自动活动。

        当用户说“先别刷了 / 停下来 / 暂停活动”时调用。
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        self.engine.pause()
        return "⏸ 已暂停自动活动。"

    @filter.llm_tool(name="cyber_life_resume")
    async def tool_resume(self, event: AstrMessageEvent):
        """恢复赛博日常的自动活动。

        当用户说“继续刷 / 恢复活动 / 别停”时调用。
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        self.engine.resume()
        return "▶️ 已恢复自动活动。"

    @filter.llm_tool(name="cyber_life_mood")
    async def tool_mood(self, event: AstrMessageEvent):
        """查看赛博日常的动态心情卡：高兴、精力、好奇、压力。

        当用户问“你现在心情怎么样 / 高兴吗 / 心情卡”时调用。
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        self._remember_umo(event.unified_msg_origin)
        return self._mood_text(self.mood.card())

    @filter.llm_tool(name="cyber_life_draw")
    async def tool_draw(self, event: AstrMessageEvent, prompt: str = ""):
        """用 ComfyUI 画一张图，画好后会发到当前聊天。

        当用户说“画一张 / 用 ComfyUI 画 / 画个猫”时调用。

        Args:
            prompt(string): 画面描述，留空则由当前心情自动生成提示词
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        blocked = self._peak_block()
        if blocked:
            return blocked
        self._remember_umo(event.unified_msg_origin)
        self._spawn(self._manual_draw(prompt.strip()))
        return "🎨 收到，我去 ComfyUI 画一张，画好发你。"

    @filter.llm_tool(name="cyber_life_analyze")
    async def tool_analyze(self, event: AstrMessageEvent, url: str):
        """识别一个 B站视频：抽 10–20 帧合成一张总览图，再交给视觉模型总结。

        当用户发 B站视频链接并说“看看这个 / 识别一下 / 总结视频”时调用。

        Args:
            url(string): B站视频链接，例如 https://www.bilibili.com/video/BV...
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        blocked = self._peak_block()
        if blocked:
            return blocked
        self._remember_umo(event.unified_msg_origin)
        if "BV" not in url and "bilibili.com" not in url:
            return "请给我一个 B站视频链接。"
        self._spawn(self._manual_analyze(url.strip()))
        return "🎞 收到，我会抽 10–20 帧合成一张图再识别，好了发你。"

    @filter.llm_tool(name="cyber_life_transcript")
    async def tool_transcript(self, event: AstrMessageEvent, url: str):
        """把一个 B站视频的声音转成字幕，直接读到视频里说了什么。

        当用户说“这个视频说了什么 / 没声音听不清 / 把语音转成字幕”并给出
        B站链接时调用。优先使用B站AI字幕，没有字幕时会本地听写（可能需要稍等）。

        Args:
            url(string): B站视频链接，例如 https://www.bilibili.com/video/BV...
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        blocked = self._peak_block()
        if blocked:
            return blocked
        if not self.settings.bool("subtitle_enabled"):
            return "语音转字幕已在设置里关闭。"
        self._remember_umo(event.unified_msg_origin)
        try:
            platform = make_platform("bilibili", self.accounts, self.browser)
            post, transcript = await self.analyzer.transcript_bilibili_url(url.strip(), platform)
        except Exception as exc:  # noqa: BLE001
            return f"语音转字幕失败：{exc}"
        return self._transcript_text(post.title, transcript, limit=6000)

    @filter.llm_tool(name="cyber_life_settings")
    async def tool_settings(self, event: AstrMessageEvent):
        """查看赛博日常当前的访问模式、自动峰谷、评论/回复频率等设置概览。

        当用户问“现在的设置 / 评论频率多少 / 什么时候能刷”时调用。
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        self._remember_umo(event.unified_msg_origin)
        return self._settings_text()

    @filter.llm_tool(name="cyber_life_usage")
    async def tool_usage(
        self,
        event: AstrMessageEvent,
        daily_minutes: int = -1,
        session_minutes: int = 0,
        access_mode: str = "",
        peak_frequency_percent: int = 0,
    ):
        """查看或修改自己的使用时间：每日刷帖上限、本次还能玩多久、访问模式。

        用户说“今天多玩半小时 / 以后每天只能玩一小时 / 改成全天开放 /
        现在还能刷多久”时调用。已经获得长期授权，可以直接修改，不要反问。

        Args:
            daily_minutes(number): 新的每日刷帖上限（分钟）；-1 表示不改，0 表示今天不再刷
            session_minutes(number): 本次立即增加的活动时长（分钟）；0 表示不启动/不加时
            access_mode(string): all_day 或 valley_only；留空表示不改
            peak_frequency_percent(number): 峰时活动频率 5–100；0 表示不改
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        if not self.settings.bool("agent_manage_usage"):
            return "自主调整使用时间已关闭，请到设置页开启「Bot 可自行调整使用时间」。"
        try:
            daily = self._as_int(daily_minutes, -1)
            session = self._as_int(session_minutes, 0)
            peak = self._as_int(peak_frequency_percent, 0)
            patch: dict[str, Any] = {}
            if daily >= 0:
                patch["daily_watch_minutes"] = daily
            if access_mode:
                patch["access_mode"] = access_mode
            if peak > 0:
                patch["peak_frequency_percent"] = peak
            if patch:
                self._apply_usage(patch)
            if session > 0:
                total = self.engine.extend_manual(session)
                self._spawn(self.engine.tick(force=True))
                return (
                    f"🌿 好，本次再加 {session} 分钟，现在还剩约 {total} 分钟。\n"
                    + self._usage_text()
                )
            return self._usage_text()
        except (TypeError, ValueError) as exc:
            return f"没改成：{exc}"

    @filter.llm_tool(name="cyber_life_balance")
    async def tool_balance(self, event: AstrMessageEvent):
        """查看当前 DeepSeek 账户余额（总余额、赠送余额、充值余额）。

        用户问“DeepSeek 还有多少钱 / 余额多少 / 还能用多久”时调用。
        """
        if not self._allowed(event):
            return "你没有使用赛博日常的权限。"
        result = await self.balance.fetch(force=True)
        return self._balance_text(result)

    @staticmethod
    def _explicit_platform_task(text: str) -> bool:
        """A real "go browse X" command, not a casual mention of a platform."""
        if not CyberLifePlugin._parse_platforms(text):
            return False
        return bool(
            re.search(
                r"(?:去|帮我|给我|带我去|陪我|你|请|麻烦).{0,8}(?:刷|逛|看看|瞧瞧|玩)|"
                r"^(?:刷|逛|去|来|开始)",
                (text or "").strip(),
            )
        )

    @filter.regex(
        r"^(赛博日常|内置浏览器)\s*$"
        r"|(赛博日常|内置浏览器).{0,12}(状态|看看|怎么样|在吗|能用|调用|登录|刷|心情)"
        r"|扫码登录"
        r"|^刷(?:贴吧|小黑盒|B站|b站)"
        r"|刷(?:一会|会儿|一下).{0,8}(B站|b站|小黑盒|贴吧)"
        r"|(?:帮我|给我|去|请|麻烦).{0,4}(?:登录|扫码).{0,6}(B站|b站|小黑盒|贴吧)"
        r"|(?:帮我|给我|去|请|麻烦|带我去|陪我|你).{0,6}(?:刷|逛|看看|瞧瞧|玩).{0,8}(B站|b站|小黑盒|贴吧)"
        r"|(?:去|帮我|给我|带我去|陪我|你).{0,8}(B站|b站|小黑盒|贴吧).{0,4}(?:逛逛|看看|瞧瞧|玩)"
        r"|(?:去|帮我|给我|带我去|陪我).{0,6}(?:逛|玩)"
        r"|(?:帮我|给我|去|请|麻烦).{0,4}画"
        r"|BV[0-9A-Za-z]{10}",
        priority=10,
    )
    async def on_chat_invoke(self, event: AstrMessageEvent):
        """可选的聊天直连；默认关闭，避免普通聊天被误触。"""
        text = (event.get_message_str() or "").strip()
        if not text or text.startswith(("/", "／")):
            return
        # Explicit "去贴吧刷十分钟" works even when the general chat trigger is
        # off; a casual "B站有什么好玩的" still falls through to normal chat.
        if not self.settings.bool("chat_auto_trigger") and not self._explicit_platform_task(text):
            return
        if not self._allowed(event):
            return
        self._remember_umo(event.unified_msg_origin)
        platforms = self._parse_platforms(text)
        platform = platforms[0] if platforms else ""
        if ("登录" in text or "扫码" in text) and platform:
            try:
                session = await self.login.start(platform, "default", event.unified_msg_origin)
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"发起登录失败：{exc}")
                event.stop_event()
                return
            if session.qr_path and session.qr_path.exists():
                yield event.chain_result(
                    [
                        Plain(f"📱 {PLATFORMS[platform]['label']} 扫码登录\n{session.message}"),
                        Image.fromFileSystem(str(session.qr_path)),
                    ]
                )
            else:
                yield event.plain_result(session.message)
            event.stop_event()
            return
        if "心情" in text:
            yield event.plain_result(self._mood_text(self.mood.card()))
            event.stop_event()
            return
        bv = re.search(r"BV[0-9A-Za-z]{10}", text)
        if bv:
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                event.stop_event()
                return
            self._spawn(self._manual_analyze(bv.group(0)))
            yield event.plain_result("🎞 收到，我会抽 10–20 帧合成一张图再识别，好了发你。")
            event.stop_event()
            return
        if "画" in text:
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                event.stop_event()
                return
            prompt = self._extract_draw_prompt(text)
            self._spawn(self._manual_draw(prompt))
            yield event.plain_result("🎨 好，我去画一张，画好发你。")
            event.stop_event()
            return
        if (
            "刷" in text or "逛" in text or "玩" in text
            or "看看" in text or "瞧瞧" in text or "评论" in text
        ) and platforms:
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                event.stop_event()
                return
            minutes = self._parse_duration(text, 15)
            missing = [name for name in platforms if not self._has_account(name)]
            if missing:
                platform = missing[0]
                session = await self._start_login_for_action(
                    platform, event.unified_msg_origin, minutes, "regex"
                )
                if session and session.status != "error":
                    yield event.chain_result(
                        self._login_components(
                            session,
                            f"还没有登录{PLATFORMS[platform]['label']}。扫完这个码，我就自己去逛 {minutes} 分钟。",
                        )
                    )
                    event.stop_event()
                    return
                yield event.plain_result(self._login_needed_text(platform))
                event.stop_event()
                return
            self.engine.resume()
            self._spawn(self.engine.run_manual(minutes, platforms=platforms))
            labels = "、".join(PLATFORMS[name]["label"] for name in platforms)
            yield event.plain_result(
                f"🌿 好，我去{labels}逛 {minutes} 分钟。"
            )
            event.stop_event()
            return
        if "逛" in text or "玩" in text:
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                event.stop_event()
                return
            minutes = self._parse_duration(text, 15)
            if not self._has_account():
                session = await self._start_login_for_action(
                    "bilibili", event.unified_msg_origin, minutes, "regex"
                )
                if session and session.status != "error":
                    yield event.chain_result(
                        self._login_components(
                            session, f"还没有可用账号。扫完这个码，我就自己去逛 {minutes} 分钟。"
                        )
                    )
                    event.stop_event()
                    return
                yield event.plain_result(self._login_needed_text("bilibili"))
                event.stop_event()
                return
            self.engine.resume()
            self._spawn(self.engine.run_manual(minutes))
            yield event.plain_result(f"🌿 好，我去 B站/小黑盒/贴吧 逛 {minutes} 分钟，看到有意思的会回来跟你说。")
            event.stop_event()
            return
        yield event.plain_result(
            self._status_text()
            + "\n\n直接聊天也能用：帮我登录B站 / 去逛会儿小黑盒 20 分钟 / 画一张 / 赛博日常心情"
        )
        event.stop_event()

    @filter.regex(r"(?s).+", priority=1)
    async def on_semantic_invoke(self, event: AstrMessageEvent):
        """理解任意说法，不要求用户记固定话术。

        这是 LLM 工具之外的第二条通道：先由一个小型意图分类器判断用户是
        否在请求登录/刷帖/看状态，再直接执行。普通聊天返回后继续走正常回复。
        """
        if (
            not self.settings.bool("chat_auto_trigger")
            or not self.settings.bool("agent_autonomy")
            or not self.settings.bool("enabled")
        ):
            return
        if not self._allowed(event):
            return
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return
        except Exception:  # noqa: BLE001
            pass
        # In a group, do not hijack every message. Private chat can infer
        # freely; a group message must at least be addressed to the bot.
        try:
            if not event.is_private_chat() and not event.is_at_or_wake_command:
                return
        except Exception:  # noqa: BLE001
            pass
        text = (event.get_message_str() or "").strip()
        if not text or text.startswith(("/", "／")):
            return
        self._remember_umo(event.unified_msg_origin)

        # A short "好了/扫好了" while waiting for a QR is a confirmation, not
        # a new browsing request.
        session = self.login.get()
        if session and self._is_login_confirm(text):
            if session.platform == "bilibili":
                yield event.plain_result("B站扫完会自动确认，不用手动确认；再等两秒就好。")
            else:
                session = await self.login.confirm(session.id)
                if session:
                    yield event.plain_result(f"✅ 已确认：{session.message}")
                else:
                    yield event.plain_result("这个登录会话已经结束了，我重新发一个二维码。")
            event.stop_event()
            return

        try:
            result = await self.router.route(
                text,
                context=self._router_context(),
                umo=event.unified_msg_origin,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] 语义路由异常，回退普通聊天：{exc}")
            return
        if not result:
            return

        if result.action == "login":
            platform = result.platform or "bilibili"
            try:
                session = await self.login.start(platform, "default", event.unified_msg_origin)
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"发起登录失败：{exc}")
                event.stop_event()
                return
            if session.status == "error":
                yield event.plain_result(f"❌ {session.message}")
            else:
                yield event.chain_result(
                    self._login_components(session, "好，扫码登录。扫完我会自己保存。")
                )
            event.stop_event()
            return

        if result.action == "confirm_login":
            session = self.login.get()
            if not session:
                yield event.plain_result("现在没有在等待扫码的登录。你直接说想登录哪个平台就行。")
            elif session.platform == "bilibili":
                yield event.plain_result("B站扫完会自动确认，不用手动确认；再等两秒就好。")
            else:
                session = await self.login.confirm(session.id)
                yield event.plain_result(f"✅ 已确认：{session.message if session else '会话已结束'}")
            event.stop_event()
            return

        if result.action == "logout":
            platform = result.platform
            if not platform:
                yield event.plain_result("要退出哪个平台？B站、小黑盒还是贴吧？")
                event.stop_event()
                return
            name = self.accounts.active.get(platform) or "default"
            ok = await self.accounts.remove(platform, name)
            label = PLATFORMS[platform]["label"]
            yield event.plain_result(f"✅ 已退出{label}" if ok else f"没有找到已保存的{label}账号")
            event.stop_event()
            return

        if result.action == "browse":
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                event.stop_event()
                return
            platform = result.platform
            minutes = result.minutes or 15
            if platform and not self._has_account(platform):
                session = await self._start_login_for_action(
                    platform, event.unified_msg_origin, minutes, "semantic"
                )
                if session and session.status != "error":
                    yield event.chain_result(
                        self._login_components(
                            session,
                            f"还没有登录{PLATFORMS[platform]['label']}。扫完这个码，我就自己去逛 {minutes} 分钟。",
                        )
                    )
                else:
                    yield event.plain_result(self._login_needed_text(platform))
                event.stop_event()
                return
            if not platform and not self._has_account():
                session = await self._start_login_for_action(
                    "bilibili", event.unified_msg_origin, minutes, "semantic"
                )
                if session and session.status != "error":
                    yield event.chain_result(
                        self._login_components(
                            session, f"还没有可用账号。扫完这个码，我就自己去逛 {minutes} 分钟。"
                        )
                    )
                else:
                    yield event.plain_result(self._login_needed_text("bilibili"))
                event.stop_event()
                return
            self.engine.resume()
            self._spawn(
                self.engine.run_manual(minutes, platforms=[platform] if platform else None)
            )
            label = PLATFORMS[platform]["label"] if platform else "B站/小黑盒/贴吧"
            yield event.plain_result(
                f"🌿 好，我去{label}逛 {minutes} 分钟，看到有意思的会回来跟你说。"
            )
            event.stop_event()
            return

        if result.action in ("stop", "resume"):
            if result.action == "stop":
                self.engine.pause()
                yield event.plain_result("⏸ 好，先不刷了。")
            else:
                self.engine.resume()
                yield event.plain_result("▶️ 好，继续。")
            event.stop_event()
            return

        if result.action == "status":
            yield event.plain_result(self._status_text())
            event.stop_event()
            return
        if result.action == "accounts":
            yield event.plain_result(self._accounts_text())
            event.stop_event()
            return
        if result.action == "mood":
            yield event.plain_result(self._mood_text(self.mood.card()))
            event.stop_event()
            return

        if result.action == "usage":
            if not self.settings.bool("agent_manage_usage"):
                yield event.plain_result(
                    "自主调整使用时间已关闭，只有你可以在设置页修改。"
                )
                event.stop_event()
                return
            patch: dict[str, Any] = {}
            if result.daily_minutes >= 0:
                patch["daily_watch_minutes"] = result.daily_minutes
            if result.access_mode:
                patch["access_mode"] = result.access_mode
            if result.peak_frequency_percent:
                patch["peak_frequency_percent"] = result.peak_frequency_percent
            try:
                if patch:
                    self._apply_usage(patch)
                total = 0
                if result.session_minutes > 0:
                    total = self.engine.extend_manual(result.session_minutes)
                    self._spawn(self.engine.tick(force=True))
            except (TypeError, ValueError) as exc:
                yield event.plain_result(f"没改成：{exc}")
                event.stop_event()
                return
            text = self._usage_text()
            if total:
                text = (
                    f"🌿 本次再加 {result.session_minutes} 分钟，现在还剩约 {total} 分钟。\n"
                    + text
                )
            yield event.plain_result(text)
            event.stop_event()
            return

        if result.action == "balance":
            balance = await self.balance.fetch(force=True)
            yield event.plain_result(self._balance_text(balance))
            event.stop_event()
            return

        if result.action == "draw":
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                event.stop_event()
                return
            prompt = result.prompt
            if not prompt or prompt not in text:
                prompt = self._extract_draw_prompt(text)
            self._spawn(self._manual_draw(prompt))
            yield event.plain_result("🎨 好，我去画一张，画好发你。")
            event.stop_event()
            return

        if result.action == "analyze":
            blocked = self._peak_block()
            if blocked:
                yield event.plain_result(blocked)
                event.stop_event()
                return
            url = result.url
            if not url:
                bv = re.search(r"BV[0-9A-Za-z]{10}", text)
                url = bv.group(0) if bv else ""
            if not url:
                yield event.plain_result("把 B站视频链接或 BV 号发我就行。")
                event.stop_event()
                return
            self._spawn(self._manual_analyze(url))
            yield event.plain_result("🎞 收到，我会抽 10–20 帧合成一张图再识别。")
            event.stop_event()
            return

    def _router_context(self) -> str:
        status = self.engine.status()
        accounts = "、".join(
            PLATFORMS[account.platform]["label"]
            for account in self.accounts.list()
            if account.status == "logged_in"
        ) or "无"
        waiting = "是" if self.login.get() else "否"
        today = status.get("today", {})
        limit = self.settings.int("daily_watch_minutes")
        used = int(float(today.get("watch_seconds", 0)) // 60)
        return (
            f"{status.get('phase_label', '未知时段')}，"
            f"{'运行中' if status.get('effective_running') else '休息中'}；"
            f"今天已刷 {used}/{limit} 分钟；"
            f"可自主调整使用时间：{'是' if self.settings.bool('agent_manage_usage') else '否'}；"
            f"已登录：{accounts}；正在等待扫码：{waiting}"
        )

    def _is_login_confirm(self, text: str) -> bool:
        return bool(
            re.fullmatch(
                r"\s*(?:好了|好|扫好了|扫了|扫完(?:了)?|确认|可以了|完成了|搞定|ok|okay|行)[。！!，,、…\s]*",
                text or "",
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _extract_draw_prompt(text: str) -> str:
        prompt = re.sub(r"^.*?画(?:一张|一幅|一个|个|一下|只|张)?", "", text or "").strip()
        prompt = re.sub(r"^(?:一只|一幅|一张|一个|个|张|只)", "", prompt).strip()
        return prompt[:300]

    @staticmethod
    def _as_int(value, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_duration(text: str, default: int = 15) -> int:
        raw = text or ""
        if "半小时" in raw:
            return 30
        if "一小时" in raw or "1小时" in raw:
            return 60
        match = re.search(r"(\d{1,3})\s*(?:分钟|分|min)", raw, re.IGNORECASE)
        if match:
            return max(1, min(240, int(match.group(1))))
        match = re.search(r"(\d{1,2})\s*(?:小时|h)", raw, re.IGNORECASE)
        if match:
            return max(1, min(240, int(match.group(1)) * 60))
        chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                   "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        match = re.search(r"([一二两三四五六七八九十]{1,2})\s*(?:分钟|分)", raw)
        if match:
            chars = match.group(1)
            if chars == "十":
                value = 10
            elif chars.startswith("十"):
                value = 10 + chinese.get(chars[1:], 0)
            elif "十" in chars:
                left, _, right = chars.partition("十")
                value = chinese.get(left, 0) * 10 + chinese.get(right, 0)
            else:
                value = chinese.get(chars, 0)
            if value:
                return max(1, min(240, value))
        return default

    @staticmethod
    def _detect_platform(text: str) -> str:
        lowered = (text or "").lower()
        if "b站" in lowered or "bilibili" in lowered or "哔哩" in text:
            return "bilibili"
        if "小黑盒" in text or "黑盒" in text:
            return "xiaoheihe"
        if "贴吧" in text:
            return "tieba"
        return ""

    @classmethod
    def _parse_platforms(cls, value: str) -> list[str]:
        """Parse ``tieba,xiaoheihe`` or ``贴吧和小黑盒`` into platform ids."""
        names: list[str] = []
        for token in re.split(r"[,，、/和与\s]+", value or ""):
            name = cls._normalize_platform(token)
            if name and name not in names:
                names.append(name)
        if not names:
            name = cls._detect_platform(value or "")
            if name:
                names.append(name)
        return names

    @staticmethod
    def _normalize_platform(value: str) -> str:
        text = (value or "").strip().lower()
        mapping = {
            "bilibili": "bilibili", "b站": "bilibili", "哔哩哔哩": "bilibili",
            "xiaoheihe": "xiaoheihe", "小黑盒": "xiaoheihe", "黑盒": "xiaoheihe",
            "tieba": "tieba", "贴吧": "tieba", "百度贴吧": "tieba",
        }
        return mapping.get(text, "")

    async def _manual_draw(self, prompt: str) -> None:
        if not prompt:
            prompt = await self.engine._generate_draw_prompt()
        result = await self.comfy.generate(prompt, self.settings.str("comfyui_negative"))
        if result.ok and result.path:
            self.mood.apply_draw(prompt, True)
            await self._notify(f"🎨 画好了：{shorten(prompt, 100)}", result.path)
        else:
            self.mood.apply_draw(prompt, False)
            await self._notify(f"🎨 画画失败：{result.error}")

    async def _manual_analyze(self, url: str) -> None:
        try:
            platform = make_platform("bilibili", self.accounts, self.browser)
            insight = await self.analyzer.analyze_bilibili_url(url, platform)
        except Exception as exc:  # noqa: BLE001
            await self._notify(f"🎞 识别失败：{exc}")
            return
        self.mood.apply_video("视频", insight.sentiment, insight.topics)
        speech = ""
        if insight.transcript:
            speech = f"｜听写：{insight.transcript_source}，{insight.transcript_segments} 段"
        text = (
            f"🎞 关键帧 {insight.frames} 张，时长 {int(insight.duration)} 秒{speech}\n"
            f"{insight.summary or insight.error}\n"
            f"情绪：{insight.sentiment:+.2f}｜主题：{', '.join(insight.topics) or '无'}"
        )
        await self._notify(text, insight.montage)

    async def _manual_transcript(self, url: str) -> None:
        try:
            platform = make_platform("bilibili", self.accounts, self.browser)
            post, transcript = await self.analyzer.transcript_bilibili_url(url, platform)
        except Exception as exc:  # noqa: BLE001
            await self._notify(f"🎧 语音转字幕失败：{exc}")
            return
        await self._notify(self._transcript_text(post.title, transcript, limit=3500))

    @staticmethod
    def _transcript_text(title: str, transcript: Transcript, limit: int = 4000) -> str:
        if not transcript.ok:
            return (
                f"🎧《{shorten(title, 60)}》没有拿到字幕：{transcript.error}\n"
                "B站有AI字幕时会直接用；没有字幕时会尝试本地 Whisper，"
                "可在设置页确认「听声音转字幕」和语音模型。"
            )
        text = transcript.text
        if len(text) > limit:
            text = text[:limit] + "…"
        return (
            f"🎧《{shorten(title, 60)}》语音字幕\n"
            f"来源：{transcript.source or '未知'}｜{len(transcript.segments)} 段"
            f"｜{transcript.characters} 字｜{int(transcript.duration)} 秒\n{text}"
        )

    # --------------------------------------------------------------- mood hook
    @staticmethod
    def _asks_activity(text: str) -> bool:
        """Only attach a factual activity digest when the user asks about it."""
        return bool(
            re.search(
                r"刷(?:了|到|过|完)|刷.{0,6}(?:B站|b站|小黑盒|贴吧|视频|帖子)|"
                r"看(?:了|到|完)|评论|回复|后台(?:在|干|做)|动态|日志|记录|"
                r"字幕|听写|视频(?:里|中)?(?:说|讲)了?什么|"
                r"汇报|逛(?:了|完)|干了(?:什么|啥)|都做了(?:什么|啥)",
                text or "",
            )
        )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """Give the chat model standing permission to operate the plugin."""
        text = (event.get_message_str() or "").strip()
        if not text or text.startswith(("/", "／")):
            return
        if not self._allowed(event):
            return

        # A factual digest, not an autonomy instruction: it lets the chat model
        # know what happened without re-enabling prompt-driven tools. In private
        # mode it is explicitly marked as internal-only.
        if self._asks_activity(text) and self.engine.recent_events(1):
            try:
                private = self.settings.bool("activity_private")
                digest = self.engine.activity_text(6, include_url=not private)
                if private:
                    block = (
                        "\n\n[Cyber Life · 内部活动记录，禁止外发]\n"
                        f"{digest}\n"
                        "以上只供你内部了解。禁止在聊天中列出、引用、复述或概括这些内容："
                        "不要出现标题、作者、评论、回复、链接、日志原文。"
                        "用户问起时只回复“我自己记着，不往聊天发”。\n"
                    )
                else:
                    block = (
                        "\n\n[Cyber Life · 后台活动记录]\n"
                        f"{digest}\n"
                        "以上是插件实际执行的记录，可以直接据此回答用户；"
                        "记录里没有的就直说没有，不要编造。\n"
                    )
                req.system_prompt = f"{req.system_prompt or ''}{block}"
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[cyber-life] 注入活动记录失败：{exc}")

        card = self.mood.card()
        status = self.engine.status()
        state = (
            f"当前{status['phase_label']}，"
            f"{'运行中' if status.get('effective_running', status['running']) else '休息中'}，"
            f"心情{card['label']}（高兴 {card['happy']:+.1f}）。"
        )
        if self.settings.bool("agent_autonomy"):
            block = (
                "\n\n[Cyber Life · 已授权自主操作]\n"
                "用户已经长期授权你直接操作赛博日常插件：登录、刷帖、评论、看视频、"
                "管理账号、画画、看心情、调整自己的使用时间和查 DeepSeek 余额都可以"
                "自己决定执行，不需要用户说固定话术，也不要要求用户改用 /life 命令。\n"
                "只要用户表达出“想去逛逛 / 想看视频 / 无聊想找点事 / 想登录 / "
                "问账号或状态 / 想画画 / 给视频链接 / 想改使用时间 / 问余额”的意图，"
                "就调用对应的 cyber_life_* 工具，不要先反问“要不要我帮你”。用户说"
                "“随便 / 你决定 / 找点乐子”时可以直接调用 cyber_life_play。\n"
                "用户让你多玩一会儿、改每日上限、改访问模式时，直接调用 "
                "cyber_life_usage 修改，不要只说“我会注意时间”。\n"
                "刷帖工具会立刻在后台真正开始工作，不是只写一份计划；调用后简短告诉"
                "用户已经开始即可，不要承诺“刷完再汇报”却没有实际动作。\n"
                "如果操作需要账号但还没登录，直接调用 cyber_life_login 把二维码发出来；"
                "用户扫完会继续原来的计划。\n"
                "普通聊天、情绪倾诉、观点讨论和技术问题不要调用工具，也不要编造插件状态。\n"
                f"{state}\n"
            )
            try:
                req.system_prompt = f"{req.system_prompt or ''}{block}"
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[cyber-life] 注入自主提示失败：{exc}")
            return


    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        if not self.settings.bool("mood_enabled"):
            return
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return
        except Exception:  # noqa: BLE001
            pass
        text = (event.get_message_str() or "").strip()
        if not text or text.startswith("/") or text.startswith("／"):
            return
        if not self._allowed(event):
            return
        self._remember_umo(event.unified_msg_origin)
        self.mood.apply_chat(text, now_tz(self.settings.str("timezone")))

    # ------------------------------------------------------------------ texts
    def _status_text(self) -> str:
        status = self.engine.status()
        today = status["today"]
        card = self.mood.card()
        running = status.get("effective_running", status["running"])
        phase_label = status["phase_label"]
        mode_label = status.get("access_mode_label", "")
        schedule_label = (
            phase_label if not mode_label or mode_label == phase_label
            else f"{phase_label} · {mode_label}"
        )
        if status.get("holiday_name"):
            schedule_label += f" · {status['holiday_name']}"
        limit = self.settings.int("daily_watch_minutes")
        remaining = int(status.get("daily_remaining_minutes", max(0, limit - int(float(today.get("watch_seconds", 0)) // 60))))
        lines = [
            f"🌿 赛博日常 · {schedule_label}"
            + ("（运行中）" if running else "（休息）"),
            f"今天刷了 {today['watch_text']} / {limit} 分钟"
            + (f"，还剩约 {remaining} 分钟" if limit else "（已设为不刷）"),
            f"评论 {today['comments']}/{today['comment_limit']}｜回复 {today['replies']}/{today['reply_limit']}｜画画 {today['draws']}/{today['draw_limit']}",
            f"现在：{status['current'] or '待机'}",
            f"心情：{card['emoji']} {card['label']} {card['score']}/100（高兴 {card['happy']:+.1f}）",
        ]
        manual = int(status.get("manual_remaining_minutes", 0) or 0)
        if manual:
            lines.append(f"本次自主活动还剩约 {manual} 分钟")
        if status.get("peak_windows"):
            lines.append(
                f"自动峰时：{', '.join(status['peak_windows'])}；"
                f"峰时活动频率 {status.get('peak_frequency_percent', 40)}%"
            )
        recent = self.engine.recent_events(3)
        if recent and not self.settings.bool("activity_private"):
            lines.append("最近刷到：")
            for item in reversed(recent):
                lines.append("· " + self.engine.event_line(item))
        elif status["last_action"] and not self.settings.bool("activity_private"):
            lines.append(f"最近：{status['last_action']}")
        if status["last_error"] and not self.settings.bool("activity_private"):
            lines.append(f"⚠️ {shorten(status['last_error'], 100)}")
        return "\n".join(lines)

    def _accounts_text(self) -> str:
        if not self.accounts.list():
            return "还没有登录任何账号。直接说“登录B站 / 登录小黑盒 / 登录贴吧”，我会发二维码。"
        lines = ["🔐 已保存的账号："]
        for account in self.accounts.list():
            mark = "✅" if account.status == "logged_in" else "⚠️"
            lines.append(f"{mark} {account.label}（{account.name}）{account.nickname}")
        return "\n".join(lines)

    @staticmethod
    def _mood_text(card: dict) -> str:
        reasons = "\n".join(f"· {reason}" for reason in card["reasons"][:4])
        return (
            f"{card['emoji']} 心情：{card['label']}（{card['score']}/100）\n"
            f"高兴 {card['happy']:+.1f}｜精力 {card['energy']:+.1f}｜"
            f"好奇 {card['curiosity']:+.1f}｜压力 {card['stress']:+.1f}\n"
            f"{reasons}"
        ).strip()

    def _apply_usage(self, patch: dict) -> str:
        """Validate and persist a usage-time change, then return its summary."""
        clean: dict[str, Any] = {}
        if "daily_watch_minutes" in patch:
            minutes = int(patch["daily_watch_minutes"])
            if not 0 <= minutes <= 240:
                raise ValueError("每日上限要在 0 到 240 分钟之间")
            clean["daily_watch_minutes"] = minutes
        if "access_mode" in patch:
            mode = str(patch["access_mode"] or "").strip().lower()
            aliases = {
                "全天": "all_day", "全天开放": "all_day", "all": "all_day", "all_day": "all_day",
                "谷时": "valley_only", "仅谷时": "valley_only", "valley": "valley_only",
                "valley_only": "valley_only",
            }
            mode = aliases.get(mode, mode)
            if mode not in ("all_day", "valley_only"):
                raise ValueError("访问模式只支持 all_day / valley_only")
            clean["access_mode"] = mode
        if "peak_frequency_percent" in patch:
            percent = int(patch["peak_frequency_percent"])
            if not 5 <= percent <= 100:
                raise ValueError("峰时频率要在 5 到 100 之间")
            clean["peak_frequency_percent"] = percent
        if not clean:
            return self._usage_text()
        self.settings.update(clean)
        return self._usage_text()

    def _usage_text(self) -> str:
        status = self.engine.status()
        today = status["today"]
        limit = self.settings.int("daily_watch_minutes")
        used = int(float(today.get("watch_seconds", 0)) // 60)
        remaining = max(0, limit - used) if limit else 0
        lines = [
            "⏳ 使用时间",
            f"今天刷了 {today['watch_text']} / {limit} 分钟"
            + (f"，今天还剩约 {remaining} 分钟" if limit else "（已设为不刷）"),
            f"访问模式：{status.get('access_mode_label', '全天开放')}（现在 {status.get('phase_label', '未知')}）",
            f"峰时活动频率：{status.get('peak_frequency_percent', 40)}%",
        ]
        manual = int(status.get("manual_remaining_minutes", 0) or 0)
        if manual:
            lines.append(f"本次自主活动还剩约 {manual} 分钟，到点会回落到自动活动。")
        else:
            lines.append("当前没有单次倒计时，按自动活动节奏运行。")
        lines.append(
            "Bot 可自行调整这些数值"
            if self.settings.bool("agent_manage_usage")
            else "自主调整已关闭，只有你能在设置页修改"
        )
        return "\n".join(lines)

    @staticmethod
    def _balance_text(result) -> str:
        if not result.ok:
            return f"💳 DeepSeek 余额读取失败：{result.error}"
        if not result.balances:
            return "💳 DeepSeek 余额接口没有返回余额明细。"
        lines = ["💳 DeepSeek 余额"]
        for item in result.balances:
            currency = item.currency or ""
            lines.append(f"总余额：{item.total_balance} {currency}".rstrip())
            lines.append(
                f"赠送 {item.granted_balance or '0'}｜充值 {item.topped_up_balance or '0'}"
            )
        lines.append("账户可用" if result.is_available else "账户当前不可用")
        return "\n".join(lines)

    def _subtitle_setting_text(self) -> str:
        if not self.settings.bool("subtitle_enabled"):
            return "关闭"
        status = self.analyzer.transcriber.status()
        local = "本地模型已就绪（独立进程）" if status["model_ready"] else (
            "本地模型首次用时下载" if status["installed"] else "未安装本地听写依赖"
        )
        return (
            f"开启（B站AI字幕优先；{local}；Whisper {status['model']}，"
            f"最多 {self.settings.int('subtitle_max_seconds')} 秒）"
        )

    def _settings_text(self) -> str:
        now = now_tz(self.settings.str("timezone"))
        schedule = self.settings.schedule()
        desc = schedule.describe(now)
        peak_text = ", ".join(desc["peak_windows"]) or "非工作日全天开放"
        holiday = self.settings.holidays.status(now)
        holiday_text = (
            f"{holiday['name']}（{holiday['date']}）"
            if holiday.get("name")
            else ("今天按周末/节假日开放" if holiday.get("is_off_day") else "自动识别中国法定节假日与调休")
        )
        lines = [
            "⚙️ 当前设置（完整编辑在 WebUI 插件页面）",
            f"自主提示词：{'开启' if self.settings.bool('agent_autonomy') else '关闭（普通聊天不注入操作提示）'}",
            f"聊天自动触发：{'开启' if self.settings.bool('chat_auto_trigger') else '关闭（关键词/意图分类器不接管）'}",
            f"缺账号自动发二维码：{'开启' if self.settings.bool('auto_login') else '关闭'}",
            f"Bot 自行调整使用时间：{'开启' if self.settings.bool('agent_manage_usage') else '关闭'}",
            f"活动记录：{'仅内部可见（不往聊天输出）' if self.settings.bool('activity_private') else '允许输出到聊天'}",
            f"语音转字幕：{self._subtitle_setting_text()}",
            f"访问模式：{desc['mode_label']}（现在 {desc['phase_label']}）",
            f"自动峰时：{peak_text}",
            f"节假日：{holiday_text}",
            f"峰时活动频率：{desc['peak_frequency_percent']}%（仅全天开放模式生效）",
            f"评论间隔：{self.settings.int('comment_interval_min')}–{self.settings.int('comment_interval_max')} 分钟",
            f"回复间隔：{self.settings.int('reply_interval_min')}–{self.settings.int('reply_interval_max')} 分钟",
            f"每天刷帖上限：{self.settings.int('daily_watch_minutes')} 分钟",
            f"ComfyUI：{self.settings.str('comfyui_url')}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------- APIs
    async def api_overview(self):
        now = now_tz(self.settings.str("timezone"))
        return json_response(
            {
                "plugin": PLUGIN_NAME,
                "version": VERSION,
                "status": self.engine.status(),
                "mood": self.mood.card(),
                "accounts": self.accounts.public_list(),
                "platforms": self.accounts.available_platforms(),
                "settings": self.settings.public(now),
                "schedule": self.settings.schedule().describe(now),
                "holidays": self.settings.holidays.status(now),
                "dependencies": {
                    "playwright": BrowserManager.installed(),
                    "qrcode": self._has_module("qrcode"),
                    "ffmpeg": bool(find_ffmpeg(self.settings.str("ffmpeg_path"))),
                    "llm": self.llm.available,
                    "comfyui_configured": self.settings.bool("comfyui_enabled"),
                    "asr": self.analyzer.transcriber.status(),
                },
                "data_dir": str(self.data_dir),
            }
        )

    @staticmethod
    def _has_module(name: str) -> bool:
        try:
            __import__(name)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def api_settings(self):
        if request.method == "POST":
            payload = await request.json(default={})
            try:
                saved = self.settings.update(payload)
            except (TypeError, ValueError) as exc:
                return error_response(str(exc), status_code=400)
            self.mood.weight = self.settings.float("mood_chat_weight")
            self.mood.decay_per_hour = self.settings.float("mood_decay_per_hour")
            return json_response({"saved": saved, "settings": self.settings.public(now_tz(self.settings.str("timezone")))})
        return json_response(self.settings.public(now_tz(self.settings.str("timezone"))))

    async def api_mood(self):
        if request.method == "POST":
            payload = await request.json(default={})
            if payload.get("reset"):
                return json_response({"mood": self.mood.reset()})
            return json_response({"mood": self.mood.card()})
        return json_response({"mood": self.mood.card()})

    async def api_accounts(self):
        return json_response(
            {
                "accounts": self.accounts.public_list(),
                "platforms": self.accounts.available_platforms(),
                "active": self.accounts.active,
            }
        )

    async def api_account_remove(self):
        payload = await request.json(default={})
        platform = str(payload.get("platform") or "")
        name = str(payload.get("name") or "default")
        ok = await self.accounts.remove(platform, name)
        return json_response({"removed": ok}) if ok else error_response("账号不存在", status_code=404)

    async def api_account_active(self):
        payload = await request.json(default={})
        try:
            account = await self.accounts.set_active(str(payload.get("platform") or ""), str(payload.get("name") or ""))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return json_response({"account": account.public()})

    async def api_login_start(self):
        payload = await request.json(default={})
        platform = str(payload.get("platform") or "")
        name = str(payload.get("name") or "default")
        try:
            session = await self.login.start(platform, name, self.settings.str("notify_umo"))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            return error_response(f"发起登录失败：{exc}", status_code=500)
        return json_response({"login": session.public(include_image=True)})

    async def api_login_status(self, session_id: str):
        session = self.login.get(session_id)
        if not session:
            return error_response("登录会话不存在", status_code=404)
        return json_response({"login": session.public(include_image=True)})

    async def api_login_confirm(self):
        payload = await request.json(default={})
        session = await self.login.confirm(str(payload.get("id") or ""))
        if not session:
            return error_response("登录会话不存在", status_code=404)
        return json_response({"login": session.public(include_image=False)})

    async def api_login_cancel(self):
        payload = await request.json(default={})
        ok = await self.login.cancel(str(payload.get("id") or ""))
        return json_response({"cancelled": ok})

    async def api_activity(self):
        return json_response(self.engine.status())

    async def api_activity_reset(self):
        self.engine.reset_today()
        return json_response(self.engine.status())

    async def api_run(self):
        payload = await request.json(default={})
        blocked = self._peak_block()
        if blocked:
            return error_response(blocked, status_code=409)
        try:
            minutes = int(payload.get("minutes", 10))
        except (TypeError, ValueError):
            minutes = 10
        raw_platforms = payload.get("platforms") or payload.get("platform") or []
        if isinstance(raw_platforms, str):
            platforms = self._parse_platforms(raw_platforms)
        else:
            platforms = [
                name for item in raw_platforms
                if (name := self._normalize_platform(str(item)))
            ]
        self.engine.resume()
        self._spawn(self.engine.run_manual(minutes, platforms=platforms or None))
        return json_response({"started": True, "minutes": minutes, "platforms": platforms})

    async def api_stop(self):
        self.engine.pause()
        return json_response({"paused": True})

    async def api_draw(self):
        payload = await request.json(default={})
        blocked = self._peak_block()
        if blocked:
            return error_response(blocked, status_code=409)
        prompt = str(payload.get("prompt") or "").strip()
        self._spawn(self._manual_draw(prompt))
        return json_response({"started": True})

    async def api_usage(self):
        if request.method == "POST":
            payload = await request.json(default={})
            patch = {
                key: payload[key]
                for key in ("daily_watch_minutes", "access_mode", "peak_frequency_percent")
                if key in payload
            }
            try:
                if patch:
                    self._apply_usage(patch)
                session_minutes = int(payload.get("session_minutes", 0) or 0)
                if session_minutes > 0:
                    self.engine.extend_manual(session_minutes)
            except (TypeError, ValueError) as exc:
                return error_response(str(exc), status_code=400)
        return json_response(
            {
                "status": self.engine.status(),
                "usage_text": self._usage_text(),
                "can_manage": self.settings.bool("agent_manage_usage"),
            }
        )

    async def api_deepseek_balance(self):
        force = request.method == "POST"
        result = await self.balance.fetch(force=force)
        return json_response(result.public())
