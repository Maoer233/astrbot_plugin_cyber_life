from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import unittest
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))
ASTRBOT_CORE = Path.home() / ".astrbot_launcher" / "instances" / "6a6e076c-c526-4719-8818-680fd76aa582" / "core"
if ASTRBOT_CORE.exists():
    sys.path.insert(0, str(ASTRBOT_CORE))

from PIL import Image

from core.accounts import AccountStore
from core.activity import ActivityEngine
from core.login import render_qr
from core.montage import Frame, choose_frame_count, make_contact_sheet
from core.mood import MoodStore
from core.holidays import HolidayCalendar
from core.intent import IntentRouter, normalize_platform
from core.schedule import AutoSchedule, Phase
from core.settings import SettingsStore
from core.transcribe import Transcript, clock, join_segments, pick_track


TZ = ZoneInfo("Asia/Shanghai")
TEST_TMP = ROOT / ".test-tmp"


def newdir() -> Path:
    TEST_TMP.mkdir(parents=True, exist_ok=True)
    target = TEST_TMP / f"t-{uuid.uuid4().hex[:10]}"
    target.mkdir()
    return target


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = newdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class AutoScheduleTests(unittest.TestCase):
    def test_weekday_and_weekend_peak(self):
        weekday_peak = datetime(2026, 1, 1, 10, 0, tzinfo=TZ)  # Thursday
        weekday_valley = datetime(2026, 1, 1, 13, 0, tzinfo=TZ)
        weekend_morning = datetime(2026, 1, 3, 11, 0, tzinfo=TZ)  # Saturday
        schedule = AutoSchedule()
        self.assertEqual(schedule.phase(weekday_peak), Phase.PEAK)
        self.assertEqual(schedule.phase(weekday_valley), Phase.VALLEY)
        self.assertEqual(schedule.phase(weekend_morning), Phase.OPEN)

    def test_access_modes_and_peak_frequency(self):
        peak = datetime(2026, 1, 1, 10, 0, tzinfo=TZ)
        valley = datetime(2026, 1, 1, 13, 0, tzinfo=TZ)
        weekend = datetime(2026, 1, 3, 11, 0, tzinfo=TZ)
        valley_only = AutoSchedule("valley_only")
        self.assertFalse(valley_only.can_act(peak))
        self.assertFalse(valley_only.can_call(peak))
        self.assertTrue(valley_only.can_act(valley))
        self.assertTrue(valley_only.can_act(weekend))
        self.assertAlmostEqual(valley_only.interval_multiplier(peak), 1.0)
        all_day = AutoSchedule("all_day", 40)
        self.assertTrue(all_day.can_act(peak))
        self.assertAlmostEqual(all_day.interval_multiplier(peak), 2.5)
        self.assertAlmostEqual(all_day.interval_multiplier(valley), 1.0)
        self.assertAlmostEqual(all_day.interval_multiplier(weekend), 1.0)


class SettingsTests(TempDirCase):
    def test_merge_validate_and_persist(self):
        store = SettingsStore({"comment_interval_min": 20, "platforms": ["bilibili"]}, self.tmp)
        self.assertEqual(store.int("comment_interval_min"), 20)
        store.update({"comment_interval_max": 45, "access_mode": "valley_only", "peak_frequency_percent": 30})
        self.assertEqual(store.int("comment_interval_max"), 45)
        with self.assertRaises(ValueError):
            store.update({"comment_interval_max": 10})
        with self.assertRaises(ValueError):
            store.update({"access_mode": "sometimes"})
        with self.assertRaises(ValueError):
            store.update({"peak_frequency_percent": 0})
        reloaded = SettingsStore({}, self.tmp)
        self.assertEqual(reloaded.int("comment_interval_max"), 45)
        self.assertEqual(reloaded.str("access_mode"), "valley_only")
        self.assertEqual(reloaded.int("peak_frequency_percent"), 30)

    def test_chat_triggers_are_off_by_default(self):
        store = SettingsStore({}, self.tmp)
        self.assertFalse(store.bool("agent_autonomy"))
        self.assertFalse(store.bool("chat_auto_trigger"))
        self.assertTrue(store.bool("agent_manage_usage"))
        self.assertFalse(store.bool("notify_actions"))
        self.assertFalse(store.bool("activity_private"))
        self.assertTrue(store.bool("subtitle_enabled"))
        self.assertEqual(store.str("subtitle_model"), "small")

    def test_phase(self):
        store = SettingsStore({}, self.tmp)
        self.assertEqual(store.phase(datetime(2026, 1, 6, 8, 30, tzinfo=TZ)), Phase.VALLEY)
        store.update({"access_mode": "valley_only"})
        self.assertFalse(store.can_call(datetime(2026, 1, 6, 10, 0, tzinfo=TZ)))
        self.assertTrue(store.can_call(datetime(2026, 1, 6, 13, 0, tzinfo=TZ)))


class MoodTests(TempDirCase):
    def test_chat_and_video(self):
        store = MoodStore(self.tmp / "mood.json", weight=0.5)
        before = store.card()["score"]
        store.apply_chat("今天好开心，谢谢你，抱抱！")
        after = store.card()
        self.assertGreater(after["happy"], 0)
        self.assertGreater(after["score"], before)
        store.apply_video("一只很可爱的猫", 0.8, ["猫", "治愈"])
        self.assertGreater(store.card()["happy"], after["happy"])
        self.assertTrue(any("猫" in reason for reason in store.card()["reasons"]))

    def test_persistence_and_decay(self):
        path = self.tmp / "mood.json"
        store = MoodStore(path)
        store.apply_chat("太棒了，喜欢！")
        happy = store.card()["happy"]
        reloaded = MoodStore(path)
        self.assertAlmostEqual(reloaded.card()["happy"], happy, places=2)
        later = datetime.now(TZ) + timedelta(hours=30)
        reloaded.decay(later)
        self.assertLess(abs(reloaded.card()["happy"]), abs(happy))

    def test_high_happy_is_happy(self):
        store = MoodStore(self.tmp / "mood.json")
        store.mood.happy = 2.1
        self.assertEqual(store.card()["label"], "开心")
        self.assertTrue(store.card()["happy_high"])


class HolidayCalendarTests(TempDirCase):
    def test_official_days_and_makeup_workday(self):
        cache = self.tmp / "holidays"
        cache.mkdir(parents=True)
        (cache / "2026.json").write_text(
            json.dumps(
                {
                    "year": 2026,
                    "days": [
                        {"name": "国庆节", "date": "2026-10-01", "isOffDay": True},
                        {"name": "国庆节", "date": "2026-10-10", "isOffDay": False},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        calendar = HolidayCalendar(self.tmp)
        self.assertTrue(calendar.is_off_day(date(2026, 10, 1)))
        self.assertFalse(calendar.is_off_day(date(2026, 10, 10)))
        self.assertTrue(calendar.is_off_day(date(2026, 10, 11)))
        self.assertEqual(calendar.name_for(date(2026, 10, 1)), "国庆节")

        schedule = AutoSchedule("valley_only", is_off_day=calendar.is_off_day)
        self.assertEqual(schedule.phase(datetime(2026, 10, 1, 10, 0, tzinfo=TZ)), Phase.OPEN)
        self.assertEqual(schedule.phase(datetime(2026, 10, 10, 10, 0, tzinfo=TZ)), Phase.PEAK)

    def test_custom_overrides(self):
        calendar = HolidayCalendar(self.tmp)
        calendar.set_custom(["2026-12-25"], ["2026-12-26"])
        self.assertTrue(calendar.is_off_day(date(2026, 12, 25)))
        self.assertFalse(calendar.is_off_day(date(2026, 12, 26)))
        calendar.set_enabled(False)
        self.assertTrue(calendar.is_off_day(date(2026, 12, 25)))


class TranscriptTests(unittest.TestCase):
    def test_join_segments_adds_separator_but_keeps_punctuation(self):
        text = join_segments(
            [
                {"start": 0, "end": 1, "text": "今天讲三个问题"},
                {"start": 1, "end": 2, "text": "第一个是画面"},
                {"start": 2, "end": 3, "text": "第二个是声音。"},
                {"start": 3, "end": 4, "text": "最后是评论"},
            ]
        )
        self.assertEqual(text, "今天讲三个问题，第一个是画面，第二个是声音。最后是评论")

    def test_prefers_chinese_track(self):
        tracks = [
            {"lan": "ai-en", "subtitle_url": "https://example.com/en.json"},
            {"lan": "ai-zh", "subtitle_url": "https://example.com/zh.json"},
        ]
        self.assertEqual(pick_track(tracks)["lan"], "ai-zh")
        self.assertIsNone(pick_track([]))

    def test_prompt_has_timestamps_and_truncates(self):
        transcript = Transcript(
            ok=True,
            text="第一句。第二句。",
            segments=[
                {"start": 0, "end": 4, "text": "第一句。"},
                {"start": 65, "end": 70, "text": "第二句。"},
            ],
            source="B站AI字幕",
            language="ai-zh",
        )
        prompt = transcript.for_prompt()
        self.assertIn("[00:00] 第一句。", prompt)
        self.assertIn("[01:05] 第二句。", prompt)
        self.assertEqual(clock(367.9), "06:07")
        short = transcript.for_prompt(max_chars=10)
        self.assertIn("已截断", short)

    def test_public_has_useful_counts(self):
        transcript = Transcript(ok=True, text="你好", source="local", duration=12.34)
        data = transcript.public()
        self.assertTrue(data["ok"])
        self.assertEqual(data["characters"], 2)
        self.assertEqual(data["duration"], 12.3)


class VideoAnalysisTests(TempDirCase):
    def test_failed_bilibili_subtitle_still_falls_back_to_local_asr(self):
        from core.video_analysis import VideoAnalyzer

        class Settings:
            def bool(self, key):
                return key == "subtitle_enabled"

            def int(self, key):
                return 4

            def str(self, key):
                return ""

        class FakeTranscriber:
            called = False

            async def from_file(self, *args, **kwargs):
                self.called = True
                return Transcript(ok=True, text="这是本地听写的内容。", source="本地", duration=8)

        class NoLLM:
            available = False

        video = self.tmp / "clip.mp4"
        video.write_bytes(b"not a real video, compose is disabled in this test")
        analyzer = VideoAnalyzer.__new__(VideoAnalyzer)
        analyzer.settings = Settings()
        analyzer.transcriber = FakeTranscriber()
        analyzer.llm = NoLLM()
        analyzer.sheets_dir = self.tmp
        failed = Transcript(error="没有B站字幕")
        insight = asyncio.run(analyzer.analyze_file(video, title="测试", transcript=failed))
        self.assertTrue(analyzer.transcriber.called)
        self.assertEqual(insight.transcript_source, "本地")
        self.assertIn("本地听写", insight.summary)


class ASRWorkerTests(TempDirCase):
    def test_bad_audio_returns_json_instead_of_crashing_parent(self):
        output = self.tmp / "result.json"
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "core" / "asr_worker.py"),
                "--audio",
                str(self.tmp / "missing.wav"),
                "--model-dir",
                str(self.tmp / "model"),
                "--output",
                str(output),
                "--language",
                "zh",
            ],
            capture_output=True,
            timeout=30,
        )
        self.assertNotEqual(process.returncode, 0)
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


class MontageTests(TempDirCase):
    def test_choose_count(self):
        self.assertEqual(choose_frame_count(2, 10, 20), 4)
        self.assertEqual(choose_frame_count(30, 10, 20), 10)
        self.assertEqual(choose_frame_count(600, 10, 20), 20)

    def test_contact_sheet(self):
        frames = []
        for index in range(7):
            path = self.tmp / f"f{index}.jpg"
            Image.new("RGB", (640, 360), (30 + index * 20, 50, 90)).save(path)
            frames.append(Frame(path=path, seconds=index * 3.5))
        out = self.tmp / "sheet.jpg"
        make_contact_sheet(frames, out, title="测试视频", subtitle="10 帧合成")
        self.assertTrue(out.exists())
        with Image.open(out) as image:
            self.assertGreater(image.width, 1000)
            self.assertGreater(image.height, 400)


class ActivityManualTaskTests(TempDirCase):
    def test_explicit_multi_platform_task_runs_immediately(self):
        class Account:
            status = "logged_in"

        class Accounts:
            def active_account(self, name):
                return Account()

        settings = SettingsStore({"platforms": ["bilibili", "xiaoheihe", "tieba"]}, self.tmp)
        engine = ActivityEngine(
            settings=settings,
            accounts=Accounts(),
            mood=None,
            analyzer=None,
            comfy=None,
            llm=None,
            browser=None,
            data_dir=self.tmp,
            notify=self._notify,
        )
        calls = []

        async def fake_watch(stats, now, platform_name=""):
            calls.append(platform_name)
            stats.watch_seconds += 60

        engine._watch_round = fake_watch
        asyncio.run(engine.run_manual(1, ["tieba", "xiaoheihe"]))
        self.assertEqual(calls, ["tieba", "xiaoheihe"])
        self.assertTrue(engine._manual_until)

    def test_task_queued_while_busy_runs_after_release(self):
        class Account:
            status = "logged_in"

        class Accounts:
            def active_account(self, name):
                return Account()

        settings = SettingsStore({"platforms": ["bilibili", "xiaoheihe", "tieba"]}, self.tmp)
        engine = ActivityEngine(
            settings=settings,
            accounts=Accounts(),
            mood=None,
            analyzer=None,
            comfy=None,
            llm=None,
            browser=None,
            data_dir=self.tmp,
            notify=self._notify,
        )
        calls = []

        async def fake_watch(stats, now, platform_name=""):
            calls.append(platform_name)
            stats.watch_seconds += 60

        engine._watch_round = fake_watch
        engine._busy = True
        asyncio.run(engine.run_manual(1, ["tieba"]))
        self.assertEqual(calls, [])
        engine._busy = False
        asyncio.run(engine.tick())
        self.assertEqual(calls, ["tieba"])

    async def _notify(self, text, image=None):
        return None


class ActivityReportingTests(unittest.TestCase):
    def test_event_lines_include_what_happened(self):
        engine = ActivityEngine.__new__(ActivityEngine)
        engine.history = [
            {
                "at": "2026-10-26T02:26:01+08:00",
                "kind": "watch",
                "platform": "bilibili",
                "title": "《鸣潮》世界观PV",
                "summary": "从废墟到草原的视觉变化",
                "transcript": "镜头从废墟推到草原，旁白说这是新旧世界的交界。",
                "url": "https://www.bilibili.com/video/BV1p8nh61Efm",
            },
            {
                "at": "2026-10-26T02:26:01+08:00",
                "kind": "comment",
                "platform": "bilibili",
                "title": "《鸣潮》世界观PV",
                "text": "镜头一切到绿草原直接松口气。",
                "comments": [
                    {"author": "观众甲", "text": "这个转场确实神", "self": False},
                ],
            },
            {
                "at": "2026-10-26T02:30:00+08:00",
                "kind": "reply",
                "platform": "bilibili",
                "title": "随便聊聊",
                "to": "路人甲",
                "incoming": "你怎么看这个视频？",
                "text": "谢谢回复，看到啦。",
            },
            {
                "at": "2026-10-26T02:31:00+08:00",
                "kind": "browse",
                "platform": "xiaoheihe",
                "title": "逛了小黑盒，页面没有解析到帖子",
            },
            {
                "at": "2026-10-26T03:24:22+08:00",
                "kind": "watch",
                "platform": "tieba",
                "title": "今日份头像分享",
                "summary": "来源网络",
                "url": "https://tieba.baidu.com/p/11136036050",
            },
            {
                "at": "2026-10-26T03:25:12+08:00",
                "kind": "watch",
                "platform": "xiaoheihe",
                "title": "一代人有一代人的DNF",
                "summary": "地下城与勇士大肥鱼",
                "url": "https://www.xiaoheihe.cn/app/bbs/link/194881652",
            },
        ]
        text = engine.activity_text(6, include_url=True)
        self.assertIn("刷视频·B站《鸣潮》世界观PV", text)
        self.assertIn("从废墟到草原", text)
        self.assertIn("听到：镜头从废墟推到草原", text)
        self.assertIn("评论·B站《鸣潮》世界观PV", text)
        self.assertIn("镜头一切到绿草原", text)
        self.assertIn("评论区：观众甲：这个转场确实神", text)
        self.assertIn("回复 路人甲", text)
        self.assertIn("对方说：你怎么看这个视频？", text)
        self.assertIn("刷帖子·贴吧《今日份头像分享》", text)
        self.assertIn("刷帖子·小黑盒《一代人有一代人的DNF》", text)
        self.assertIn("平台分布：B站 3 条、小黑盒 2 条、贴吧 1 条", text)
        self.assertIn("逛了小黑盒", text)
        self.assertIn("BV1p8nh61Efm", text)
        self.assertIn("最新在前", text)
        latest_first = text.splitlines()[2]
        self.assertIn("一代人有一代人的DNF", latest_first)

    def test_empty_activity_is_explicit(self):
        engine = ActivityEngine.__new__(ActivityEngine)
        engine.history = []
        self.assertIn("还没有后台动态", engine.activity_text())


class AccountTests(TempDirCase):
    def test_save_and_remove(self):
        async def run():
            store = AccountStore(self.tmp)
            account = await store.save_session(
                "bilibili", "default", {"SESSDATA": "x", "bili_jct": "y"}, {"nickname": "小星"}
            )
            self.assertEqual(account.status, "logged_in")
            self.assertEqual(store.cookies("bilibili")["SESSDATA"], "x")
            self.assertTrue(store.storage_state_path("bilibili").parent.exists())
            self.assertTrue(await store.remove("bilibili", "default"))
            self.assertEqual(store.list(), [])

        asyncio.run(run())


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.available = True

    async def complete_json(self, *args, **kwargs):
        self.calls += 1
        return self.payload


class IntentRouterTests(TempDirCase):
    def test_routes_casual_browse_and_clamps(self):
        settings = SettingsStore({}, self.tmp)
        settings.update({"agent_autonomy": True})
        llm = FakeLLM(
            {
                "action": "browse",
                "platform": "B站",
                "minutes": "999",
                "confidence": 0.9,
            }
        )
        router = IntentRouter(settings, llm)
        result = asyncio.run(router.route("我有点无聊，随便找点事做"))
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "browse")
        self.assertEqual(result.platform, "bilibili")
        self.assertEqual(result.minutes, 240)
        self.assertEqual(llm.calls, 1)

    def test_ignores_low_confidence_and_emotion(self):
        settings = SettingsStore({}, self.tmp)
        settings.update({"agent_autonomy": True})
        router = IntentRouter(
            settings,
            FakeLLM({"action": "mood", "confidence": 0.4}),
        )
        self.assertIsNone(asyncio.run(router.route("我心情不好")))
        none_router = IntentRouter(
            settings,
            FakeLLM({"action": "none", "confidence": 0.99}),
        )
        self.assertIsNone(asyncio.run(none_router.route("你觉得B站怎么样")))

    def test_disabled_skips_llm(self):
        settings = SettingsStore({}, self.tmp)
        settings.update({"agent_autonomy": False})
        llm = FakeLLM({"action": "browse", "confidence": 0.9})
        router = IntentRouter(settings, llm)
        self.assertIsNone(asyncio.run(router.route("去B站逛逛")))
        self.assertEqual(llm.calls, 0)

    def test_platform_aliases(self):
        self.assertEqual(normalize_platform("小黑盒"), "xiaoheihe")
        self.assertEqual(normalize_platform("百度贴吧"), "tieba")
        self.assertEqual(normalize_platform("unknown"), "")

    def test_usage_time_and_balance(self):
        settings = SettingsStore({}, self.tmp)
        settings.update({"agent_autonomy": True})
        usage = IntentRouter(
            settings,
            FakeLLM(
                {
                    "action": "usage",
                    "daily_minutes": 60,
                    "session_minutes": 30,
                    "access_mode": "全天开放",
                    "peak_frequency_percent": 65,
                    "confidence": 0.92,
                }
            ),
        )
        result = asyncio.run(usage.route("以后每天一小时，今天再多玩半小时，全天开放"))
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "usage")
        self.assertEqual(result.daily_minutes, 60)
        self.assertEqual(result.session_minutes, 30)
        self.assertEqual(result.access_mode, "all_day")
        self.assertEqual(result.peak_frequency_percent, 65)
        balance = IntentRouter(
            settings,
            FakeLLM({"action": "balance", "confidence": 0.95}),
        ).parse({"action": "balance", "confidence": 0.95})
        self.assertIsNotNone(balance)
        self.assertEqual(balance.action, "balance")


class QRTests(TempDirCase):
    def test_render_qr(self):
        out = render_qr("https://example.com/login", self.tmp / "qr.png", "扫码登录")
        self.assertTrue(out.exists())
        with Image.open(out) as image:
            self.assertGreater(image.width, 200)


if __name__ == "__main__":
    unittest.main()
