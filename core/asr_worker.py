"""Standalone faster-whisper worker for long video soundtracks.

This file is executed in a child process, never imported by AstrBot itself.
Native crashes in CTranslate2, ONNX or a corrupted audio file therefore kill
only the worker; the bot process survives and can fall back to the Bilibili
subtitle or report a readable error.

Audio is transcribed in 30-second windows with a small look-ahead overlap, so a
two-minute video is handled as four short jobs instead of one large tensor.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import wave
from pathlib import Path
from typing import Any

CHUNK_SECONDS = 30.0
LOOKAHEAD_SECONDS = 0.75
SAMPLE_RATE = 16000


def read_wav(path: Path) -> Any:
    import numpy as np  # noqa: PLC0415

    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getframerate() != SAMPLE_RATE:
            raise RuntimeError("音轨格式异常，需要 16kHz 单声道 WAV")
        if handle.getsampwidth() != 2:
            raise RuntimeError("音轨格式异常，需要 16-bit PCM")
        raw = handle.readframes(handle.getnframes())
    if not raw:
        raise RuntimeError("音轨为空")
    return np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0


def write_result(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def transcribe(args: argparse.Namespace) -> dict[str, Any]:
    from faster_whisper import WhisperModel  # noqa: PLC0415

    audio = read_wav(Path(args.audio))
    total = len(audio) / SAMPLE_RATE
    language = args.language.strip()
    if language in ("", "auto"):
        language = ""
    prompt = "以下是普通话视频内容，请使用简体中文转写。" if language == "zh" else None
    model = WhisperModel(
        args.model_dir,
        device="cpu",
        compute_type="int8",
        cpu_threads=max(2, min(8, args.threads or os.cpu_count() or 4)),
        num_workers=1,
    )
    segments: list[dict[str, Any]] = []
    started = time.time()
    offset = 0.0
    while offset < total:
        end = min(offset + CHUNK_SECONDS + LOOKAHEAD_SECONDS, total)
        chunk = audio[int(offset * SAMPLE_RATE): int(end * SAMPLE_RATE)]
        iterator, _info = model.transcribe(
            chunk,
            language=language or None,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            beam_size=5,
            best_of=5,
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=prompt,
        )
        for item in iterator:
            text = " ".join((item.text or "").split()).strip()
            if not text:
                continue
            absolute_start = offset + float(item.start or 0)
            absolute_end = offset + float(item.end or 0)
            # The next chunk repeats a fraction of a second for context.  Drop
            # lines whose midpoint lies before the new chunk boundary so the
            # same sentence is not emitted twice.
            if offset > 0 and (absolute_start + absolute_end) / 2 < offset:
                continue
            segments.append(
                {
                    "start": round(absolute_start, 2),
                    "end": round(absolute_end, 2),
                    "text": text,
                }
            )
        offset += CHUNK_SECONDS
    return {
        "ok": True,
        "segments": segments,
        "language": language or "auto",
        "duration": round(total, 2),
        "elapsed": round(time.time() - started, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--threads", type=int, default=0)
    args = parser.parse_args()
    output = Path(args.output)
    try:
        result = transcribe(args)
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-3000:],
        }
    try:
        write_result(output, result)
    except Exception as exc:  # noqa: BLE001
        print(f"worker failed to write result: {exc}", file=sys.stderr)
        return 2
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
