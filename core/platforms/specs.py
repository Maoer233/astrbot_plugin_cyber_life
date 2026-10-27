"""Per-site selectors and URLs for the browser-driven platforms.

These are intentionally conservative: every list has fallbacks, and when a
site changes its DOM the plugin reports which step failed instead of silently
doing the wrong thing.
"""

from __future__ import annotations


SITES: dict[str, dict] = {
    "tieba": {
        "label": "贴吧",
        "login_url": "https://passport.baidu.com/v2/?login&tpl=tb&u=https%3A%2F%2Ftieba.baidu.com%2F",
        "prep": [
            {"selector": "text=二维码登录", "wait_ms": 900},
            {"selector": ".tang-pass-qrcode-img", "wait_ms": 400},
        ],
        "qr_selectors": [
            ".tang-pass-qrcode-img",
            "img[src*='qrcode']",
            "#TANGRAM__PSP_11__QrcodeImg",
            ".pass-qrcode img",
            "canvas",
        ],
        "cookie_names": ["BDUSS", "BDUSS_BFESS"],
        "logged_in_selectors": [".userinfo", ".u_menu_username", "a[href*='logout']"],
        "feed_url": "https://tieba.baidu.com/index.html",
        "card_selectors": [
            "a.j_th_tit",
            ".threadlist_title a",
            "a[href*='/p/']",
        ],
        "card_title_selector": ".thread-title",
        "card_body_selector": ".thread-content",
        "comment_open_selectors": [
            ".core_title_btns a:has-text('回复')",
            "a:has-text('发表回复')",
            "button:has-text('评论')",
        ],
        "editor_selectors": [
            "#ueditor_replace",
            ".reply_textarea textarea",
            "textarea[placeholder*='评论']",
            "div[contenteditable='true']",
        ],
        "submit_selectors": [
            ".poster_submit",
            "button:has-text('发表')",
            "button:has-text('评论')",
            "a:has-text('发表')",
        ],
    },
    "xiaoheihe": {
        "label": "小黑盒",
        "login_url": "https://www.xiaoheihe.cn/",
        "prep": [
            {"selector": "text=登录", "wait_ms": 1000},
            {"selector": "text=扫码登录", "wait_ms": 900},
        ],
        "qr_selectors": [
            ".website-login__qr-box",
            ".website-login__qr-canvas",
            "canvas.website-login__qr-canvas",
            "[class*='website-login'] canvas",
            "[class*='login'] [class*='qr'] img",
            "img[src*='qrcode']",
        ],
        "cookie_names": [
            "xiaoheihe_token",
            "heybox_token",
            "user_token",
            "web_token",
            "heybox_access_token",
        ],
        "logged_in_selectors": [
            "[class*='avatar'] img",
            "[class*='user-info']",
            "a[href*='/user/']",
        ],
        "feed_url": "https://www.xiaoheihe.cn/app/bbs/home",
        "card_selectors": [
            "a[href*='/app/bbs/link/']",
            "a[href*='/bbs/link/']",
            "a[href*='/bbs/app/link/']",
            "[class*='link-item'] a",
        ],
        "card_title_selector": ".bbs-content__title",
        "card_body_selector": ".bbs-content__content",
        "comment_open_selectors": [
            "[class*='comment'] button:has-text('评论')",
            "button:has-text('写评论')",
            "button:has-text('评论')",
            "[class*='comment-input']",
        ],
        "editor_selectors": [
            "textarea",
            "[contenteditable='true']",
            ".comment-input textarea",
        ],
        "submit_selectors": [
            "button:has-text('发送')",
            "button:has-text('评论')",
            "button:has-text('发布')",
            ".submit-btn",
        ],
    },
}


def site_spec(platform: str) -> dict:
    return SITES.get(platform, {})
