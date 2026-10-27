"""Playwright browser wrapper used for QR login and sites without a usable API.

Playwright is imported lazily so the plugin still loads (and shows a clear
dependency warning in its settings page) when the browser dependency has not
been installed yet.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .util import atomic_write_json, now_tz


class BrowserError(RuntimeError):
    pass


@dataclass
class BrowserLoginHandle:
    platform: str
    name: str
    context: Any
    page: Any
    screenshot: Path
    created_at: str = ""

    async def cookies(self) -> dict[str, str]:
        try:
            items = await self.context.cookies()
        except Exception:  # noqa: BLE001
            return {}
        return {item.get("name", ""): item.get("value", "") for item in items if item.get("name")}

    async def close(self) -> None:
        try:
            await self.context.close()
        except Exception:  # noqa: BLE001
            pass


class BrowserManager:
    def __init__(self, settings: Any, data_dir: Path) -> None:
        self.settings = settings
        self.data_dir = data_dir
        self.shots_dir = data_dir / "media" / "login"
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle
    @staticmethod
    def installed() -> bool:
        try:
            import playwright.async_api  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _ensure(self):
        if self._browser is not None:
            return self._browser
        async with self._lock:
            if self._browser is not None:
                return self._browser
            if not self.settings.bool("browser_enabled"):
                raise BrowserError("内置浏览器已在设置中关闭")
            try:
                from playwright.async_api import async_playwright
            except Exception as exc:  # noqa: BLE001
                raise BrowserError(
                    "Playwright 未安装。请在插件目录执行 pip install -r requirements.txt"
                ) from exc
            self._playwright = await async_playwright().start()
            kwargs: dict[str, Any] = {
                "headless": self.settings.bool("browser_headless"),
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            channel = self.settings.str("browser_channel").strip()
            executable = self.settings.str("browser_executable").strip()
            errors = []
            attempts = []
            if executable:
                attempts.append({"executable_path": executable})
            if channel:
                attempts.append({"channel": channel})
            for candidate in (
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ):
                if os.path.exists(candidate):
                    attempts.append({"executable_path": candidate})
            attempts.append({})
            for extra in attempts:
                try:
                    self._browser = await self._playwright.chromium.launch(**kwargs, **extra)
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{extra}: {exc}")
            if self._browser is None:
                await self._playwright.stop()
                self._playwright = None
                raise BrowserError(
                    "无法启动浏览器。请安装 Edge/Chrome，或在设置里填写 browser_channel / browser_executable。"
                    f" 详情：{'; '.join(errors[-2:])}"
                )
            return self._browser

    async def new_context(self, storage_path: Path | None = None, *, storage: bool = True):
        browser = await self._ensure()
        kwargs: dict[str, Any] = {
            "viewport": {
                "width": self.settings.int("browser_viewport_width"),
                "height": self.settings.int("browser_viewport_height"),
            },
            "locale": "zh-CN",
            "timezone_id": self.settings.str("timezone") or "Asia/Shanghai",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
            ),
            "extra_http_headers": {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        }
        if storage and storage_path and storage_path.exists():
            kwargs["storage_state"] = str(storage_path)
        context = await browser.new_context(**kwargs)
        context.set_default_timeout(self.settings.int("browser_timeout_seconds") * 1000)
        try:
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
        except Exception:  # noqa: BLE001
            pass
        return context

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        self._playwright = None

    # ----------------------------------------------------------- login pages
    async def begin_login(self, platform: str, name: str, spec: dict[str, Any]) -> BrowserLoginHandle:
        context = await self.new_context(storage=False)
        page = await context.new_page()
        try:
            await page.goto(spec["login_url"], wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:  # noqa: BLE001
            await context.close()
            raise BrowserError(f"打开登录页失败：{exc}") from exc
        await page.wait_for_timeout(1800)
        for action in spec.get("prep", []):
            try:
                selector = action.get("selector")
                if not selector:
                    continue
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=5000)
                await locator.click(timeout=5000)
                await page.wait_for_timeout(int(action.get("wait_ms", 800)))
            except Exception:  # noqa: BLE001
                continue
        await page.wait_for_timeout(1200)
        target = self.shots_dir / f"{platform}-{name}-{int(now_tz().timestamp())}.png"
        await self._screenshot_qr(page, spec.get("qr_selectors", []), target)
        return BrowserLoginHandle(
            platform=platform,
            name=name,
            context=context,
            page=page,
            screenshot=target,
            created_at=now_tz().isoformat(timespec="seconds"),
        )

    async def _screenshot_qr(self, page, selectors: list[str], target: Path) -> None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                await locator.wait_for(state="visible", timeout=4000)
                box = await locator.bounding_box()
                if not box or box.get("width", 0) < 80 or box.get("height", 0) < 80:
                    continue
                # add a little breathing room around the code
                clip = {
                    "x": max(0, box["x"] - 18),
                    "y": max(0, box["y"] - 18),
                    "width": box["width"] + 36,
                    "height": box["height"] + 36,
                }
                await page.screenshot(path=str(target), clip=clip, type="png")
                return
            except Exception:  # noqa: BLE001
                continue
        await page.screenshot(path=str(target), type="png")

    @staticmethod
    async def is_logged_in(handle: BrowserLoginHandle, spec: dict[str, Any]) -> bool:
        cookies = await handle.cookies()
        required = [name for name in spec.get("cookie_names", []) if name]
        if required and any(name in cookies for name in required):
            return True
        selectors = spec.get("logged_in_selectors", [])
        for selector in selectors:
            try:
                locator = handle.page.locator(selector).first
                if await locator.count() and await locator.is_visible(timeout=1000):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def save_storage(self, handle: BrowserLoginHandle, target: Path) -> None:
        try:
            state = await handle.context.storage_state()
            atomic_write_json(target, state)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"保存浏览器登录态失败：{exc}") from exc

    # ------------------------------------------------------------- activity
    async def browse(self, platform: str, name: str, url: str, selectors: dict[str, Any]) -> dict[str, Any]:
        """Open a feed page, scrape cards and take a viewport screenshot."""
        context = await self.new_context(self.storage_path(platform, name))
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            cards = []
            for selector in selectors.get("card_selectors", []):
                try:
                    items = await page.locator(selector).all()
                except Exception:  # noqa: BLE001
                    continue
                for item in items[:24]:
                    try:
                        title = (await item.inner_text(timeout=1500)).strip()
                        title_selector = selectors.get("card_title_selector")
                        if title_selector:
                            node = item.locator(title_selector).first
                            if not await node.count():
                                continue
                            title = (await node.inner_text(timeout=1200)).strip() or title
                        href = await item.get_attribute("href", timeout=1000)
                        body = ""
                        body_selector = selectors.get("card_body_selector")
                        if body_selector:
                            node = item.locator(body_selector).first
                            if await node.count():
                                body = (await node.inner_text(timeout=1200)).strip()
                        if not title:
                            continue
                        cards.append({"title": title[:200], "url": href or "", "body": body[:400]})
                    except Exception:  # noqa: BLE001
                        continue
                if cards:
                    break
            shot = self.data_dir / "media" / "browse" / f"{platform}-{int(now_tz().timestamp())}.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), type="png")
            return {"url": page.url, "title": await page.title(), "cards": cards, "screenshot": str(shot)}
        finally:
            await context.close()

    def storage_path(self, platform: str, name: str) -> Path:
        return self.data_dir / "sessions" / platform / f"{name}.storage.json"

    async def post_comment(
        self,
        platform: str,
        name: str,
        url: str,
        text: str,
        selectors: dict[str, Any],
    ) -> dict[str, Any]:
        """Best-effort DOM commenting. Returns ok/message/screenshot."""
        context = await self.new_context(self.storage_path(platform, name))
        page = await context.new_page()
        shot_dir = self.data_dir / "media" / "comments"
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot = shot_dir / f"{platform}-{int(now_tz().timestamp())}.png"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2200)
            for selector in selectors.get("comment_open_selectors", []):
                try:
                    opener = page.locator(selector).first
                    if await opener.count() and await opener.is_visible(timeout=1000):
                        await opener.click(timeout=4000)
                        await page.wait_for_timeout(800)
                        break
                except Exception:  # noqa: BLE001
                    continue
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(700)
            except Exception:  # noqa: BLE001
                pass
            editor = None
            for selector in selectors.get("editor_selectors", []):
                try:
                    locator = page.locator(selector).first
                    if await locator.count() and await locator.is_visible(timeout=1200):
                        editor = locator
                        break
                except Exception:  # noqa: BLE001
                    continue
            if editor is None:
                await page.screenshot(path=str(shot), type="png")
                return {"ok": False, "message": "没有找到评论输入框（页面结构可能变了）", "screenshot": str(shot)}
            await editor.click(timeout=5000)
            await page.wait_for_timeout(400)
            try:
                await editor.fill(text, timeout=4000)
            except Exception:  # noqa: BLE001
                await page.keyboard.type(text, delay=35)
            await page.wait_for_timeout(600)
            clicked = False
            for selector in selectors.get("submit_selectors", []):
                try:
                    button = page.locator(selector).first
                    if await button.count() and await button.is_visible(timeout=1000):
                        await button.click(timeout=5000)
                        clicked = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            await page.wait_for_timeout(2200)
            await page.screenshot(path=str(shot), type="png")
            return {
                "ok": clicked,
                "message": "已提交评论" if clicked else "没有找到发送按钮，请检查选择器",
                "screenshot": str(shot),
                "url": page.url,
            }
        except Exception as exc:  # noqa: BLE001
            try:
                await page.screenshot(path=str(shot), type="png")
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "message": f"浏览器评论失败：{exc}", "screenshot": str(shot)}
        finally:
            await context.close()

    async def screenshot(self, platform: str, name: str, url: str) -> Path:
        context = await self.new_context(self.storage_path(platform, name))
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1800)
            target = self.data_dir / "media" / "shots" / f"{platform}-{int(now_tz().timestamp())}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(target), type="png")
            return target
        finally:
            await context.close()
