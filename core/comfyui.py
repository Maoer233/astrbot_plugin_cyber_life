"""Minimal ComfyUI API client for idle drawing."""

from __future__ import annotations

import asyncio
import copy
import json
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import logger

from .util import safe_filename


DEFAULT_WORKFLOW: dict[str, Any] = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 24,
            "cfg": 7.0,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1.0,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "v1-5-pruned-emaonly.safetensors"},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 512, "height": 768, "batch_size": 1},
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "cyber-life", "images": ["8", 0]},
    },
}


@dataclass
class Generation:
    ok: bool
    path: Path | None = None
    error: str = ""
    prompt_id: str = ""
    seed: int = 0
    prompt: str = ""


class ComfyUIError(RuntimeError):
    pass


def _substitute(value: Any, mapping: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _substitute(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, mapping) for item in value]
    if isinstance(value, str):
        for key, replacement in mapping.items():
            value = value.replace("{{" + key + "}}", str(replacement))
            value = value.replace("__" + key.upper() + "__", str(replacement))
        return value
    return value


class ComfyUIClient:
    def __init__(self, settings: Any, data_dir: Path) -> None:
        self.settings = settings
        self.output_dir = data_dir / "media" / "art"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._workflow_cache: tuple[str, dict] | None = None

    @property
    def base_url(self) -> str:
        return self.settings.str("comfyui_url").rstrip("/")

    async def available(self) -> bool:
        if not self.settings.bool("comfyui_enabled"):
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.base_url}/system_stats")
                return response.status_code < 500
        except Exception:  # noqa: BLE001
            return False

    def _load_workflow(self) -> dict:
        path_text = self.settings.str("comfyui_workflow").strip()
        if not path_text:
            return copy.deepcopy(DEFAULT_WORKFLOW)
        if self._workflow_cache and self._workflow_cache[0] == path_text:
            return copy.deepcopy(self._workflow_cache[1])
        path = Path(path_text)
        if not path.is_file():
            raise ComfyUIError(f"ComfyUI 工作流文件不存在：{path_text}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ComfyUIError(f"读取工作流失败：{exc}") from exc
        if isinstance(data, dict) and isinstance(data.get("prompt"), dict):
            data = data["prompt"]
        if not isinstance(data, dict):
            raise ComfyUIError("工作流必须是 ComfyUI API 格式的 JSON 对象")
        self._workflow_cache = (path_text, data)
        return copy.deepcopy(data)

    def _build_workflow(self, prompt: str, negative: str, seed: int) -> dict:
        workflow = self._load_workflow()
        mapping = {
            "prompt": prompt,
            "positive": prompt,
            "negative": negative,
            "seed": seed,
            "width": self.settings.int("comfyui_width"),
            "height": self.settings.int("comfyui_height"),
            "steps": self.settings.int("comfyui_steps"),
            "cfg": self.settings.float("comfyui_cfg"),
            "sampler": self.settings.str("comfyui_sampler"),
            "scheduler": self.settings.str("comfyui_scheduler"),
            "checkpoint": self.settings.str("comfyui_checkpoint"),
            "filename_prefix": "cyber-life",
        }
        workflow = _substitute(workflow, mapping)
        # For the built-in workflow, set values directly as well so an
        # untouched default still works.
        if "3" in workflow and workflow["3"].get("class_type") == "KSampler":
            inputs = workflow["3"].setdefault("inputs", {})
            inputs.update(
                {
                    "seed": seed,
                    "steps": mapping["steps"],
                    "cfg": mapping["cfg"],
                    "sampler_name": mapping["sampler"],
                    "scheduler": mapping["scheduler"],
                }
            )
        if "4" in workflow and workflow["4"].get("class_type") == "CheckpointLoaderSimple":
            workflow["4"].setdefault("inputs", {})["ckpt_name"] = mapping["checkpoint"]
        if "5" in workflow and workflow["5"].get("class_type") == "EmptyLatentImage":
            workflow["5"].setdefault("inputs", {}).update({"width": mapping["width"], "height": mapping["height"]})
        for node in ("6", "7"):
            if node in workflow and workflow[node].get("class_type") == "CLIPTextEncode":
                workflow[node].setdefault("inputs", {})["text"] = prompt if node == "6" else negative
        return workflow

    async def generate(self, prompt: str, negative: str = "", seed: int | None = None) -> Generation:
        if not self.settings.bool("comfyui_enabled"):
            return Generation(ok=False, error="ComfyUI 已在设置中关闭", prompt=prompt)
        seed = int(seed if seed is not None else random.randint(1, 2**31 - 1))
        try:
            workflow = self._build_workflow(prompt, negative or self.settings.str("comfyui_negative"), seed)
        except ComfyUIError as exc:
            return Generation(ok=False, error=str(exc), seed=seed, prompt=prompt)
        timeout = self.settings.int("comfyui_timeout")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.base_url}/prompt",
                    json={"prompt": workflow, "client_id": uuid.uuid4().hex},
                )
                if response.status_code >= 400:
                    return Generation(
                        ok=False,
                        error=f"ComfyUI 拒绝工作流：HTTP {response.status_code} {response.text[:200]}",
                        seed=seed,
                        prompt=prompt,
                    )
                payload = response.json()
                prompt_id = str(payload.get("prompt_id") or "")
                if not prompt_id:
                    return Generation(ok=False, error=f"ComfyUI 没有返回 prompt_id：{payload}", seed=seed, prompt=prompt)
        except Exception as exc:  # noqa: BLE001
            return Generation(ok=False, error=f"连接 ComfyUI 失败：{exc}", seed=seed, prompt=prompt)

        deadline = time.monotonic() + timeout
        image_info = None
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    history = await client.get(f"{self.base_url}/history/{prompt_id}")
                data = history.json() if history.status_code == 200 else {}
                entry = data.get(prompt_id) if isinstance(data, dict) else None
                if entry:
                    status = entry.get("status") or {}
                    if status.get("status_str") == "error":
                        return Generation(
                            ok=False,
                            error="ComfyUI 执行出错，请查看 ComfyUI 控制台",
                            prompt_id=prompt_id,
                            seed=seed,
                            prompt=prompt,
                        )
                    outputs = entry.get("outputs") or {}
                    for node in outputs.values():
                        images = node.get("images") or []
                        if images:
                            image_info = images[0]
                            break
                    if image_info:
                        break
            except Exception:  # noqa: BLE001
                continue
        if not image_info:
            return Generation(
                ok=False,
                error=f"等待 ComfyUI 出图超时（{timeout} 秒）",
                prompt_id=prompt_id,
                seed=seed,
                prompt=prompt,
            )
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(
                    f"{self.base_url}/view",
                    params={
                        "filename": image_info.get("filename", ""),
                        "subfolder": image_info.get("subfolder", ""),
                        "type": image_info.get("type", "output"),
                    },
                )
            if response.status_code >= 400:
                return Generation(ok=False, error="下载 ComfyUI 图片失败", prompt_id=prompt_id, seed=seed, prompt=prompt)
            suffix = Path(str(image_info.get("filename") or "art.png")).suffix or ".png"
            target = self.output_dir / f"{int(time.time())}-{safe_filename(prompt, 'art')[:36]}{suffix}"
            target.write_bytes(response.content)
            return Generation(ok=True, path=target, prompt_id=prompt_id, seed=seed, prompt=prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cyber-life] 保存 ComfyUI 图片失败：{exc}")
            return Generation(ok=False, error=f"保存图片失败：{exc}", prompt_id=prompt_id, seed=seed, prompt=prompt)
