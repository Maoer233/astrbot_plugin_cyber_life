"""Turn a video's soundtrack into text.

Two sources are used, in this order:

* Bilibili's own AI subtitles (fast, usually better on names and terms)
* a local faster-whisper model, for videos without a subtitle track

The result is cached under ``plugin_data/.../media/transcripts`` so watching
the same video twice does not pay the download/ASR cost again.  The model is
lazy: importing this module and starting AstrBot never pulls hundreds of MB of
weights; the first video that actually needs local ASR does.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .montage import VideoError, find_ffmpeg
from .util import atomic_write_json, read_json

PUNCTUATION = "，。！？；：、,.!?;: "
MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
CHINESE_LANGS = ("ai-zh", "zh-cn", "zh-hans", "zh-hant", "zh", "ai-zh-hans")


def _clean_size(value: str) -> str:
    value = (value or "small").strip().lower()
    return value if value in MODEL_SIZES else "small"


def simplify_chinese(text: str) -> str:
    """Whisper sometimes drifts into Traditional Chinese; normalise it.

    ``zhconv`` is tiny and optional: if it is unavailable the raw text is
    still usable, so speech recognition must not fail because of cosmetics.
    """
    text = (text or "").replace("﹐", "，").replace("﹑", "、").replace("\u3000", " ")
    try:
        from zhconv import convert  # noqa: PLC0415

        return convert(text, "zh-cn")
    except Exception:  # noqa: BLE001
        return text


def join_segments(segments: list[dict[str, Any]]) -> str:
    """Join ASR lines without gluing two sentences together.

    Whisper normally supplies punctuation, but Bilibili's AI subtitle lines do
    not, so a light-width comma is inserted only when the previous line has no
    terminal punctuation.
    """
    parts: list[str] = []
    for segment in segments:
        text = simplify_chinese(" ".join(str(segment.get("text") or "").split()).strip())
        if not text:
            continue
        if parts and parts[-1][-1:] not in PUNCTUATION:
            parts.append("，")
        parts.append(text)
    return "".join(parts).strip("， ")


def clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


@dataclass
class Transcript:
    ok: bool = False
    text: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""
    language: str = ""
    duration: float = 0.0
    error: str = ""
    cached: bool = False
    truncated: bool = False

    @property
    def characters(self) -> int:
        return len(self.text or "")

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "source": self.source,
            "language": self.language,
            "duration": round(self.duration, 1),
            "segments": len(self.segments),
            "characters": self.characters,
            "error": self.error,
            "cached": self.cached,
            "truncated": self.truncated,
        }

    def for_prompt(self, max_chars: int = 5200) -> str:
        """Timestamped excerpt that is small enough to sit in a vision prompt."""
        if not self.segments:
            text = (self.text or "").strip()
            return text[:max_chars] + ("…" if len(text) > max_chars else "")
        lines: list[str] = []
        used = 0
        for segment in self.segments:
            text = " ".join(str(segment.get("text") or "").split()).strip()
            if not text:
                continue
            line = f"[{clock(segment.get('start', 0))}] {text}"
            if used + len(line) > max_chars:
                self.truncated = True
                lines.append("…（后面还有，已截断）")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)


def pick_track(tracks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer a Chinese subtitle, then the first available track."""
    usable = [item for item in tracks if isinstance(item, dict) and item.get("subtitle_url")]
    if not usable:
        return None
    for wanted in CHINESE_LANGS:
        for item in usable:
            if str(item.get("lan") or "").lower() == wanted:
                return item
    for item in usable:
        if str(item.get("lan") or "").lower().startswith("zh"):
            return item
    return usable[0]


class Transcriber:
    def __init__(self, settings: Any, data_dir: Path) -> None:
        self.settings = settings
        self.data_dir = data_dir
        self.transcripts_dir = data_dir / "media" / "transcripts"
        self.audio_dir = data_dir / "media" / "audio"
        self.models_dir = data_dir / "models" / "asr"
        for directory in (self.transcripts_dir, self.audio_dir, self.models_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- status
    @property
    def installed(self) -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    @property
    def model_ready(self) -> bool:
        size = _clean_size(self.settings.str("subtitle_model"))
        return (self.models_dir / f"faster-whisper-{size}" / "model.bin").is_file()

    def status(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "model": _clean_size(self.settings.str("subtitle_model")),
            "model_ready": self.model_ready,
            "bilibili_subtitles": True,
            "cache": len(list(self.transcripts_dir.glob("*.json"))),
        }

    # ------------------------------------------------------------- Bilibili
    async def from_bilibili(self, post: Any, platform: Any) -> Transcript:
        if not self.settings.bool("subtitle_enabled"):
            return Transcript(error="语音转字幕未开启")
        key = f"bili:{post.bvid}:{post.cid or post.extra.get('cid', '')}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            client = platform.client()
            data = await client.fetch_subtitle(post)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[cyber-life] 读取B站字幕失败：{exc}")
            return Transcript(error=f"读取B站字幕失败：{exc}")
        if not data or not data.get("segments"):
            return Transcript(error="这个视频没有可用的B站字幕")
        segments = []
        for item in data["segments"]:
            text = simplify_chinese(str(item.get("text") or "").strip())
            if text:
                segments.append(
                    {
                        "start": float(item.get("start") or 0),
                        "end": float(item.get("end") or 0),
                        "text": text,
                    }
                )
        if not segments:
            return Transcript(error="B站字幕内容为空")
        transcript = Transcript(
            ok=True,
            text=join_segments(segments),
            segments=segments,
            source="B站AI字幕",
            language=str(data.get("language") or ""),
            duration=float(segments[-1].get("end") or 0),
        )
        if not transcript.text:
            return Transcript(error="B站字幕内容为空")
        self._cache_put(key, transcript)
        return transcript

    # ----------------------------------------------------------------- local
    async def from_file(
        self,
        video_path: Path,
        *,
        key: str = "",
        title: str = "",
        duration: float = 0.0,
    ) -> Transcript:
        if not self.settings.bool("subtitle_enabled"):
            return Transcript(error="语音转字幕未开启")
        key = key or f"local:{video_path.stem}:{video_path.stat().st_size}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        if not self.installed:
            return Transcript(
                error="未安装 faster-whisper；B站自带字幕仍可用，本地听写需要先安装语音识别依赖"
            )
        try:
            transcript = await asyncio.to_thread(
                self._transcribe_sync,
                video_path,
                key,
                title,
                duration,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] 本地语音识别失败《{title}》：{exc}")
            return Transcript(error=f"本地语音识别失败：{exc}")
        if transcript.ok:
            self._cache_put(key, transcript)
        return transcript

    def _transcribe_sync(
        self,
        video_path: Path,
        key: str,
        title: str,
        duration: float,
    ) -> Transcript:
        ffmpeg = find_ffmpeg(self.settings.str("ffmpeg_path"))
        if not ffmpeg:
            raise VideoError("找不到 ffmpeg，无法提取音轨")
        max_seconds = max(15, self.settings.int("subtitle_max_seconds"))
        audio = self.audio_dir / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}.wav"
        args = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_path), "-t", str(max_seconds),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(audio),
        ]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            raise VideoError(f"提取音轨失败：{exc}") from exc
        if proc.returncode != 0 or not audio.exists() or audio.stat().st_size < 2048:
            detail = (proc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
            raise VideoError("视频没有可识别的音轨" + (f"：{detail[-1]}" if detail else ""))
        started = time.time()
        result_path = audio.with_suffix(".json")
        try:
            size = _clean_size(self.settings.str("subtitle_model"))
            model_dir = self._ensure_model(size)
            worker = Path(__file__).with_name("asr_worker.py")
            timeout = max(120, min(900, int(max_seconds * 2.5) + 60))
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env.setdefault("OMP_NUM_THREADS", str(max(2, min(8, os.cpu_count() or 4))))
            result_path.unlink(missing_ok=True)
            command = [
                sys.executable,
                str(worker),
                "--audio",
                str(audio),
                "--model-dir",
                str(model_dir),
                "--output",
                str(result_path),
                "--language",
                self.settings.str("subtitle_language"),
                "--threads",
                str(max(2, min(8, os.cpu_count() or 4))),
            ]
            try:
                process = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=timeout,
                    env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as exc:
                raise VideoError(f"本地听写超过 {timeout} 秒，已停止（视频可能过长）") from exc

            data: dict[str, Any] = {}
            if result_path.is_file():
                try:
                    data = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise VideoError(f"本地听写结果损坏：{exc}") from exc
                if not isinstance(data, dict):
                    data = {}
            if process.returncode != 0 or not data.get("ok"):
                stderr = (process.stderr or b"").decode("utf-8", "ignore").strip()
                reason = data.get("error") or (stderr.splitlines()[-1] if stderr else "")
                raise VideoError(f"本地听写进程退出（code {process.returncode}）{('：' + reason) if reason else ''}")
            raw_segments = data.get("segments") or []
            segments = []
            for item in raw_segments:
                text = simplify_chinese(str(item.get("text") or "").strip())
                if text:
                    segments.append(
                        {
                            "start": float(item.get("start") or 0),
                            "end": float(item.get("end") or 0),
                            "text": text,
                        }
                    )
        finally:
            for path in (audio, result_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        text = join_segments(segments)
        if not text:
            raise VideoError("音轨里没有识别到人声")
        logger.info(
            "[cyber-life] 本地听写完成 %s（%s 秒音频，用时 %.1f 秒，独立进程）",
            title[:60],
            int(data.get("duration") or duration),
            time.time() - started,
        )
        return Transcript(
            ok=True,
            text=text,
            segments=segments,
            source=f"本地语音识别（Whisper {size}）",
            language=str(data.get("language") or "auto"),
            duration=float(data.get("duration") or duration),
        )

    # ----------------------------------------------------------------- model
    def _ensure_model(self, size: str) -> Path:
        target = self.models_dir / f"faster-whisper-{size}"
        if (target / "model.bin").is_file():
            return target
        endpoint = (self.settings.str("subtitle_hf_endpoint").strip() or "https://huggingface.co").rstrip("/")
        repo = f"Systran/faster-whisper-{size}"
        logger.info(f"[cyber-life] 首次使用，下载语音模型 {repo}（约数百 MB）…")
        try:
            self._download_model_files(endpoint, repo, target)
        except Exception as exc:  # noqa: BLE001
            raise VideoError(
                f"下载语音模型失败：{exc}。可检查网络，或在设置里换一个模型/镜像"
            ) from exc
        if not (target / "model.bin").is_file():
            raise VideoError("语音模型下载不完整，缺少 model.bin")
        return target

    @staticmethod
    def _download_model_files(endpoint: str, repo: str, target: Path) -> None:
        """Download only the four files faster-whisper actually needs.

        Using plain HTTP instead of huggingface_hub avoids the Xet CDN, which
        ignores the hf-mirror endpoint and returns 401 in some networks.
        """
        import httpx  # noqa: PLC0415

        target.mkdir(parents=True, exist_ok=True)
        files = ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin")
        timeout = httpx.Timeout(60.0, connect=30.0, read=600.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for filename in files:
                url = f"{endpoint}/{repo}/resolve/main/{filename}"
                destination = target / filename
                temporary = target / f"{filename}.part"
                try:
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        total = int(response.headers.get("content-length") or 0)
                        written = 0
                        with temporary.open("wb") as handle:
                            for chunk in response.iter_bytes(1024 * 1024):
                                handle.write(chunk)
                                written += len(chunk)
                                if total and written % (100 * 1024 * 1024) < 1024 * 1024:
                                    logger.info(
                                        "[cyber-life] 下载 %s：%d%%",
                                        filename,
                                        int(written / total * 100),
                                    )
                    if temporary.stat().st_size < 512:
                        raise VideoError(f"{filename} 下载内容为空")
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)

    # ----------------------------------------------------------------- cache
    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return self.transcripts_dir / f"{digest}.json"

    def _cache_get(self, key: str) -> Transcript | None:
        path = self._cache_path(key)
        raw = read_json(path, None)
        if not isinstance(raw, dict) or not raw.get("text"):
            return None
        created = float(raw.get("created_at") or 0)
        if created and time.time() - created > 90 * 86400:
            return None
        return Transcript(
            ok=True,
            text=str(raw.get("text") or ""),
            segments=list(raw.get("segments") or []),
            source=str(raw.get("source") or ""),
            language=str(raw.get("language") or ""),
            duration=float(raw.get("duration") or 0),
            cached=True,
        )

    def _cache_put(self, key: str, transcript: Transcript) -> None:
        atomic_write_json(
            self._cache_path(key),
            {
                "key": key,
                "text": transcript.text,
                "segments": transcript.segments,
                "source": transcript.source,
                "language": transcript.language,
                "duration": transcript.duration,
                "created_at": time.time(),
            },
        )
        try:
            files = sorted(
                self.transcripts_dir.glob("*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for old in files[160:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass
