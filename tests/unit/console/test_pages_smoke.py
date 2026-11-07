"""页面渲染 smoke 测试（S5b）：静态文件存在 + 关键元素 + 每页 JS ≤300 行。

深度 UI 交互不在本层测试范围（任务书 S5b：页面渲染 smoke 即可）——
路由行为由 test_*_api.py 覆盖。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from job_agent import console

STATIC_DIR = Path(console.__file__).resolve().parent / "static"

# 每页关键元素（id / 文案锚点）——对应任务书 S5b 五项验收要点
PAGE_EXPECTATIONS: dict[str, list[str]] = {
    "resume.html": [
        "upload-form",
        "resume-file",
        "regenerate-hint",
        "sec-education",
        "sec-experience",
        "sec-skills",
        "sec-projects",
        "btn-save-structured",
        "btn-generate-directions",
        "btn-confirm-directions",
        "btn-edit-directions",
        "directions-list",
        "/api/resume/upload",
        "/api/resume/structured",
        "/api/directions",
    ],
    "greetings.html": [
        "btn-generate-active",
        "btn-generate-passive",
        "candidates-active",
        "candidates-passive",
        "final-content-active",
        "final-content-passive",
        "not-finalized-active",
        "not-finalized-passive",
        "sync-checkbox",
        "btn-copy",
        "sync-guide",
        "【消息通知 - 设置招呼语】",
        "/api/greetings/generate",
        "/api/greetings/current",
        "/api/greetings/platform-synced",
    ],
    "config.html": [
        "sec-delivery",
        "sec-messaging",
        "sec-match",
        "sec-notify-ai",
        "rewrite-notice",
        "daily-cap-hint",
        "daily-cap-live",
        "safety-warnings",
        "auto-reply-limit",
        "polling-same",
        "min-detail",
        "min-search",
        "btn-save-config",
        "/api/config",
    ],
    "notify.html": [
        "sec-feishu",
        "sec-email",
        "sec-dnd",
        "level-high",
        "btn-save-notify",
        "btn-notify-test",
        "test-channel",
        "test-mode",
        'value="fake"',
        'value="real"',
        "/api/config/notify",
        "/api/notify/test",
    ],
    "ai.html": [
        "ai-form",
        "ai-api-key",
        "ai-base-url",
        "ai-model",
        "btn-save-ai",
        "btn-ai-test",
        "/api/config/ai",
        "/api/ai/test",
    ],
}

# 平台路径指引（F1：同步指引必须给出平台路径）
SYNC_GUIDE_PATH = "【消息通知 - 设置招呼语】"


def inline_script_lines(html: str) -> int:
    """页内 <script>（无 src）代码总行数（不含 <script>/</script> 标签行）。"""
    total = 0
    for block in re.findall(r"<script>(.*?)</script>", html, re.DOTALL):
        total += len(block.strip("\n").splitlines())
    return total


class PagesSmokeTest(unittest.TestCase):
    """五页静态文件：存在、关键元素齐备、无构建依赖、JS 行数受控。"""

    def test_static_dir_exists_with_assets(self) -> None:
        self.assertTrue(STATIC_DIR.is_dir())
        for asset in ("assets/app.css", "assets/common.js"):
            self.assertTrue((STATIC_DIR / asset).is_file(), asset)

    def test_all_five_pages_present_with_key_elements(self) -> None:
        for page, needles in PAGE_EXPECTATIONS.items():
            with self.subTest(page=page):
                path = STATIC_DIR / page
                self.assertTrue(path.is_file(), page)
                html = path.read_text(encoding="utf-8")
                for needle in needles:
                    self.assertIn(needle, html, f"{page} 缺少关键元素：{needle}")

    def test_inline_js_within_300_lines(self) -> None:
        """前端技术约束：每页 JS ≤300 行（共享 common.js 不计入页内行数）。"""
        for page in PAGE_EXPECTATIONS:
            with self.subTest(page=page):
                html = (STATIC_DIR / page).read_text(encoding="utf-8")
                self.assertLessEqual(inline_script_lines(html), 300)

    def test_common_js_sends_security_header(self) -> None:
        """非 GET 请求携带 S5a 安全头 X-Job-Agent: console（§10.1 第 3 项）。"""
        js = (STATIC_DIR / "assets/common.js").read_text(encoding="utf-8")
        self.assertIn("X-Job-Agent", js)
        self.assertIn("console", js)

    def test_greetings_page_binds_sync_state_to_content(self) -> None:
        """F1 呈现契约：同步状态随内容修改联动（dirty 检测 + 禁用勾选）。"""
        html = (STATIC_DIR / "greetings.html").read_text(encoding="utf-8")
        self.assertIn("platform_synced", html)
        self.assertIn("dirty", html)
        self.assertIn("cb.disabled", html)

    def test_no_build_tooling_referenced(self) -> None:
        """无构建步骤：页面不引用打包产物/CDN/module 脚本。"""
        for page in PAGE_EXPECTATIONS:
            with self.subTest(page=page):
                html = (STATIC_DIR / page).read_text(encoding="utf-8")
                self.assertNotIn("https://cdn", html)
                self.assertNotIn('type="module"', html)
                self.assertNotIn("node_modules", html)


if __name__ == "__main__":
    unittest.main()
