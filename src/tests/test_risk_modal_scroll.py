"""2026-08-24：首启风险须知从勾选同意改为滚动到底才可确认。

DOM/JS/CSS 三方契约：滚动区存在且无勾选框、确认按钮默认禁用、
须知文本自足（不引用仓库或外部文档）、滚动门禁逻辑与样式齐全。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "backend/app/templates/base.html").read_text(encoding="utf-8")
LOGIN_JS = (ROOT / "backend/app/static/js/modules/login.js").read_text(
    encoding="utf-8"
)
CSS = (ROOT / "backend/app/static/css/components.css").read_text(encoding="utf-8")

MODAL = BASE.split('id="risk-modal-overlay"', 1)[1]


def test_modal_has_scroll_region_and_no_checkbox():
    assert 'id="risk-scroll"' in MODAL
    assert 'id="risk-scroll-hint"' in MODAL
    assert 'id="risk-confirm-btn" disabled' in MODAL
    assert "risk-accept-checkbox" not in BASE


def test_modal_text_is_self_contained():
    assert "RISKS.md" not in MODAL
    assert "仓库" not in MODAL
    for item in ("用途与边界", "账号风险", "禁止行为", "数据与隐私", "责任声明"):
        assert item in MODAL
    for item in ("评论发布", "多账号池", "代理池", "验证码", "跨设备"):
        assert item in MODAL


def test_scroll_gating_logic_present():
    assert "risk-scroll" in LOGIN_JS
    assert "scrollHeight" in LOGIN_JS
    assert "addEventListener('scroll'" in LOGIN_JS
    assert "addEventListener('resize'" in LOGIN_JS
    assert "risk-accept-checkbox" not in LOGIN_JS


def test_scroll_css_present():
    assert re.search(r"\.risk-scroll\s*\{[^}]*overflow-y\s*:\s*auto", CSS)
    assert ".risk-scroll-hint" in CSS
    assert ".risk-accept-label" not in CSS
