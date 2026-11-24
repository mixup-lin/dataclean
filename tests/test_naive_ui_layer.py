"""Naive UI 渐进增强层契约测试（零浏览器依赖，纯静态 + 路由断言）。

验证：
1. 本地 bundle / app.js 路由可用、完全离线（无 CDN / 外链）；
2. 每页注入 data-dc-page 与本地脚本引用，SSR 骨架契约仍在；
3. 路径穿越防护；
4. app.js 关键提交契约（action / 字段名）与主题配置存在。
"""
from pathlib import Path

import webapp
from webapp import AppState

ROOT = Path(__file__).resolve().parents[1]


def _state(tmp_path):
    return AppState(str(tmp_path))


def test_vendor_bundle_route_offline(tmp_path):
    state = _state(tmp_path)
    r = webapp.handle_request("GET", "/static/vendor/naive-vue.js", {}, {}, state)
    assert r.status == 200
    assert r.content_type.startswith("application/javascript")
    assert len(r.body) > 100_000  # 真实打包产物（含 Vue + Naive UI）
    body = r.body.decode("utf-8", "ignore")
    # 运行时零 CDN：仅允许 W3C namespace / 文档链接等无害字面量
    assert "cdn.jsdelivr" not in body
    assert "unpkg.com" not in body
    assert "jsdelivr" not in body
    assert "fonts.googleapis" not in body


def test_app_script_route(tmp_path):
    state = _state(tmp_path)
    r = webapp.handle_request("GET", "/static/vendor/app.js", {}, {}, state)
    assert r.status == 200
    body = r.body.decode("utf-8", "ignore") if isinstance(r.body, bytes) else r.body
    assert "DataCleanUI" in body
    # 提交路由与关键字段名
    for token in ("/plan", "/run", "/execute", "/rules/save", "/extract",
                  "rule_json", "intent", "generation_path", "themeOverrides"):
        assert token in body, token


def test_vendor_path_traversal_blocked(tmp_path):
    state = _state(tmp_path)
    r = webapp.handle_request("GET", "/static/vendor/../webapp.py", {}, {}, state)
    assert r.status == 404


def test_pages_inject_vue_boot_and_keep_ssr(tmp_path):
    state = _state(tmp_path)
    cases = {
        "/": ("workbench", 'name="intent"'),
        "/extract": ("extract", 'name="schema_text"'),
        "/jobs": ("jobs", "后台任务"),
    }
    for path, (page_key, ssr_marker) in cases.items():
        body = webapp.handle_request("GET", path, {}, {}, state).body
        assert f'data-dc-page="{page_key}"' in body, path
        assert "/static/vendor/naive-vue.js" in body, path
        assert "/static/vendor/app.js" in body, path
        assert ssr_marker in body, (path, ssr_marker)
        # SSR 骨架可访问性标记未被 Vue 注入破坏
        assert 'class="skip-link"' in body
        assert 'aria-current="page"' in body


def test_css_keeps_vue_layer_scoped(tmp_path):
    state = _state(tmp_path)
    css = webapp.handle_request("GET", "/static/style.css", {}, {}, state).body
    assert "#dc-vue-root" in css
    assert "body.dc-vue-ready" in css
    # Vue 层仅在挂载成功后接管，打印时让位 SSR
    assert "dc-vue-ready #dc-vue-root" in css
    # 既有可访问性契约仍在
    assert ":focus-visible" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
