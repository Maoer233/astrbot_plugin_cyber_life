"""Turn a video into a single contact sheet instead of recognising every frame.

The sheet is what gets sent to the vision model: 10–20 evenly spaced frames,
each labelled with its timestamp, stitched into one image.  Short clips simply
produce fewer frames and are still composed and recognised.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .util import safe_filename


class VideoError(RuntimeError):
    pass


@dataclass
class Frame:
    path: Path
    seconds: float


@dataclass
class Montage:
    path: Path
    duration: float
    frames: list[Frame] = field(default_factory=list)
    title: str = ""

    @property
    def count(self) -> int:
        return len(self.frames)

    @property
    def times(self) -> list[float]:
        return [round(frame.seconds, 2) for frame in self.frames]


def find_ffmpeg(explicit: str = "") -> str | None:
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate)
    env = os.environ.get("FFMPEG_PATH")
    if env and Path(env).is_file():
        return env
    which = shutil.which("ffmpeg")
    if which:
        return which
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def probe_duration(video_path: Path, ffmpeg: str) -> float:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(video_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VideoError(f"无法运行 ffmpeg：{exc}") from exc
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
    if not match:
        raise VideoError("无法读取视频时长（文件可能损坏或没有视频流）")
    hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
    return hours * 3600 + minutes * 60 + seconds


def choose_frame_count(duration: float, min_frames: int = 10, max_frames: int = 20) -> int:
    """10 frames for a normal clip, scaling up for long videos, never > max."""
    if duration <= 0:
        return 1
    min_frames = max(1, int(min_frames))
    max_frames = max(min_frames, int(max_frames))
    # one frame every ~6 seconds, clamped to the configured range
    target = int(round(duration / 6.0))
    target = max(min_frames, min(max_frames, target))
    # a 3-second clip cannot yield 10 *distinct* frames; don't ask for more
    # than roughly two frames per second.
    target = min(target, max(1, int(duration * 2)))
    return target


def extract_frames(
    video_path: Path,
    out_dir: Path,
    *,
    min_frames: int = 10,
    max_frames: int = 20,
    ffmpeg_path: str = "",
) -> tuple[list[Frame], float]:
    ffmpeg = find_ffmpeg(ffmpeg_path)
    if not ffmpeg:
        raise VideoError("找不到 ffmpeg，请安装 imageio-ffmpeg 或在设置里填写 ffmpeg 路径")
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video_path, ffmpeg)
    count = choose_frame_count(duration, min_frames, max_frames)
    frames: list[Frame] = []
    step = duration / count if count else duration
    for index in range(count):
        seconds = min(max(step * (index + 0.5), 0.0), max(duration - 0.05, 0.0))
        target = out_dir / f"frame-{index:02d}.jpg"
        args = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{seconds:.3f}", "-i", str(video_path),
            "-frames:v", "1", "-vf", "scale=640:-2",
            "-q:v", "3", str(target),
        ]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=45)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and target.exists() and target.stat().st_size > 0:
            frames.append(Frame(path=target, seconds=seconds))
    if not frames:
        raise VideoError("视频抽帧失败，没有拿到任何画面")
    return frames, duration


def make_contact_sheet(
    frames: list[Frame],
    out_path: Path,
    *,
    title: str = "",
    subtitle: str = "",
    cols: int = 4,
    thumb_w: int = 320,
    thumb_h: int = 180,
) -> Path:
    if not frames:
        raise VideoError("没有可用于合成的画面")
    cols = max(1, min(5, int(cols)))
    rows = (len(frames) + cols - 1) // cols
    pad = 12
    header_h = 76 if title else 16
    label_h = 24
    width = pad + cols * (thumb_w + pad)
    height = header_h + rows * (thumb_h + label_h + pad) + pad
    canvas = Image.new("RGB", (width, height), (13, 16, 23))
    draw = ImageDraw.Draw(canvas)

    title_font = _font(24)
    sub_font = _font(15)
    label_font = _font(15)
    if title:
        draw.text((pad + 2, 14), title[:52], font=title_font, fill=(238, 242, 248))
    if subtitle:
        draw.text((pad + 2, 46), subtitle[:110], font=sub_font, fill=(140, 154, 176))

    for index, frame in enumerate(frames):
        row, col = divmod(index, cols)
        x = pad + col * (thumb_w + pad)
        y = header_h + row * (thumb_h + label_h + pad)
        try:
            with Image.open(frame.path) as source:
                image = source.convert("RGB")
                image.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
                # letterbox instead of stretching
                tile = Image.new("RGB", (thumb_w, thumb_h), (24, 28, 38))
                tile.paste(image, ((thumb_w - image.width) // 2, (thumb_h - image.height) // 2))
        except (OSError, ValueError):
            tile = Image.new("RGB", (thumb_w, thumb_h), (40, 24, 28))
        canvas.paste(tile, (x, y))
        draw.rectangle([x, y, x + thumb_w - 1, y + thumb_h - 1], outline=(55, 65, 82))
        stamp = f"{int(frame.seconds) // 60:02d}:{int(frame.seconds) % 60:02d}"
        draw.rectangle([x, y + thumb_h - 26, x + 66, y + thumb_h], fill=(0, 0, 0))
        draw.text((x + 8, y + thumb_h - 22), stamp, font=label_font, fill=(255, 255, 255))
        draw.text((x + 2, y + thumb_h + 3), f"#{index + 1}", font=label_font, fill=(120, 134, 156))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "JPEG", quality=88)
    return out_path


async def compose_video(
    video_path: Path,
    out_dir: Path,
    *,
    title: str = "",
    subtitle: str = "",
    min_frames: int = 10,
    max_frames: int = 20,
    ffmpeg_path: str = "",
) -> Montage:
    frames, duration = await asyncio.to_thread(
        extract_frames,
        video_path,
        out_dir,
        min_frames=min_frames,
        max_frames=max_frames,
        ffmpeg_path=ffmpeg_path,
    )
    sheet = out_dir / f"{safe_filename(title or video_path.stem, 'video')}-sheet.jpg"
    await asyncio.to_thread(
        make_contact_sheet,
        frames,
        sheet,
        title=title or "视频关键帧",
        subtitle=subtitle,
    )
    # Only the contact sheet is needed by the vision model; keeping every
    # individual frame would fill the disk over a few days of browsing.
    for frame in frames:
        try:
            frame.path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        sheets = sorted(out_dir.glob("*-sheet.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in sheets[120:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    return Montage(path=sheet, duration=duration, frames=frames, title=title)
