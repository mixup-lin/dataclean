"""前端可访问性与离线约束的回归测试。

依据 uiux-pro-max-cn 技能的高优先级规则：
- 零外部 CDN / 离线完整可用（Performance + 产品定位）
- 统一可见焦点环（Accessibility, WCAG 2.4.7 / 2.4.11）
- 触摸目标 ≥ 44px（Touch & Interaction）
- 尊重 prefers-reduced-motion（Animation）
- 语义色不依赖颜色单独传达（Charts & Data / 1.4.1）
- 键盘跳转链接、导航 aria-current、错误 role=alert（Accessibility / Forms）
"""
import tempfile

import webapp
from webapp import AppState


def _css() -> str:
    return webapp.handle_request("GET", "/static/style.css", {}, {}, AppState(tempfile.mkdtemp())).body


def test_no_external_cdn_import():
    """样式不得依赖任何外部 CDN，保证离线完整可用。"""
    css = _css()
    assert "@import" not in css
    assert "cdn.jsdelivr" not in css
    assert "fonts.googleapis" not in css


def test_visible_focus_ring():
    css = _css()
    assert ":focus-visible" in css
    assert "outline" in css
    # 输入控件保留自身焦点提示，但不移除焦点可见性而无替代
    assert "input:focus-visible" in css


def test_touch_target_minimum():
    css = _css()
    assert "--tap-min" in css and "44px" in css
    assert ".btn {" in css and "min-height: var(--tap-min)" in css


def test_reduced_motion_respected():
    css = _css()
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "animation: none" in css


def test_responsive_breakpoints():
    css = _css()
    for bp in ("min-width: 1440px", "max-width: 1024px", "max-width: 980px", "max-width: 600px"):
        assert bp in css


def test_semantic_status_not_color_only():
    css = _css()
    for level in ("error", "warn", "ok", "info"):
        assert f".alert.{level}::before" in css


def test_nav_accessibility_on_all_pages():
    state = AppState(tempfile.mkdtemp())
    for path in ("/", "/extract", "/jobs"):
        body = webapp.handle_request("GET", path, {}, {}, state).body
        assert 'class="skip-link"' in body, path
        assert 'aria-label="主导航"' in body, path
        assert 'id="main"' in body, path
        assert 'aria-current="page"' in body, path
        assert 'role="status"' in body, path


def test_wait_overlay_decorative_spinner_hidden():
    state = AppState(tempfile.mkdtemp())
    body = webapp.handle_request("GET", "/", {}, {}, state).body
    assert 'class="dc-spinner" aria-hidden="true"' in body
