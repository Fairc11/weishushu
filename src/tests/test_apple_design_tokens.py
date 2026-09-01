"""Kimi 现代极简 × macOS ②级立体质感生产守卫。"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS_ROOT = ROOT / "backend" / "app" / "static" / "css"
DESIGN_TOKENS = ROOT / "docs" / "frontend-design" / "tokens" / "tokens.css"
BASE_HTML = ROOT / "backend" / "app" / "templates" / "base.html"

CSS_FILES = (
    "tokens.css",
    "base.css",
    "shell.css",
    "components.css",
    "workflows.css",
    "responsive.css",
)
NON_TOKEN_FILES = CSS_FILES[1:]

REQUIRED_TOKENS = (
    "--wb-bg",
    "--wb-surface",
    "--wb-surface-subtle",
    "--wb-border",
    "--wb-border-strong",
    "--wb-text-1",
    "--wb-text-2",
    "--wb-text-3",
    "--wb-brand",
    "--wb-brand-pressed",
    "--wb-brand-soft",
    "--wb-brand-gradient",
    "--wb-shadow-card",
    "--wb-shadow-overlay",
    "--wb-inset-input",
    "--wb-focus-ring",
    "--wb-glass-bg",
    "--wb-glass-blur",
    "--wb-radius-badge",
    "--wb-radius-control",
    "--wb-radius-card",
    "--wb-radius-overlay",
    "--wb-radius-pill",
    "--wb-space-1",
    "--wb-space-8",
    "--wb-motion-fast",
    "--wb-motion-standard",
    "--wb-ease",
)

COLOR_LITERAL_RE = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|(?<![-\w])(?:rgb|hsl)a?\(",
)


def _read(name: str) -> str:
    return (CSS_ROOT / name).read_text(encoding="utf-8")


def test_production_uses_exact_six_file_css_architecture() -> None:
    for name in CSS_FILES:
        assert (CSS_ROOT / name).is_file(), name
    assert not (CSS_ROOT / "app.css").exists()

    source = BASE_HTML.read_text(encoding="utf-8")
    positions = []
    for name in CSS_FILES:
        marker = f'/static/css/{name}?v={{{{ version }}}}'
        position = source.find(marker)
        assert position >= 0, marker
        positions.append(position)
    assert positions == sorted(positions)


def test_production_tokens_are_exact_approved_asset() -> None:
    assert (CSS_ROOT / "tokens.css").read_bytes() == DESIGN_TOKENS.read_bytes()


def test_required_wb_tokens_and_themes_exist() -> None:
    tokens = _read("tokens.css")
    for token in REQUIRED_TOKENS:
        assert token in tokens
    assert "#fa7d3c" in tokens.lower()
    assert ":root," in tokens
    assert '[data-theme="light"]' in tokens
    assert '[data-theme="dark"]' in tokens
    assert "--ios-" not in tokens


def test_three_accessibility_media_queries_exist() -> None:
    tokens = _read("tokens.css")
    for query in (
        "prefers-reduced-transparency: reduce",
        "prefers-reduced-motion: reduce",
        "prefers-contrast: more",
    ):
        assert query in tokens


def test_non_token_css_has_no_color_literals_or_legacy_tokens() -> None:
    for name in NON_TOKEN_FILES:
        source = _read(name)
        assert COLOR_LITERAL_RE.search(source) is None, name
        assert "--ios-" not in source, name


def test_component_materials_use_semantic_tokens() -> None:
    components = _read("components.css")
    for token in (
        "var(--wb-brand-gradient)",
        "var(--wb-brand-highlight)",
        "var(--wb-brand-shadow)",
        "var(--wb-inset-input)",
        "var(--wb-shadow-card)",
        "var(--wb-shadow-overlay)",
    ):
        assert token in components


def test_workflow_contains_selected_preview_and_six_stage_nodes() -> None:
    workflows = _read("workflows.css")
    assert ".preview-item.is-selected" in workflows
    assert "var(--wb-brand-soft)" in workflows
    assert ".progress-stage-list" in workflows
    assert ".is-current" in workflows
    assert ".is-done" in workflows
    assert "@keyframes" in workflows


def test_shell_contains_glass_header_and_340px_history_drawer() -> None:
    shell = _read("shell.css")
    assert "var(--wb-glass-bg)" in shell
    assert "var(--wb-glass-blur)" in shell
    assert ".history-panel" in shell
    assert "width: 340px" in shell


def test_hidden_and_responsive_fallbacks_are_preserved() -> None:
    combined = "\n".join(_read(name) for name in NON_TOKEN_FILES)
    for selector in (
        ".user-card[hidden]",
        ".backup-self-block[hidden]",
        ".history-panel[hidden]",
        ".log-panel[hidden]",
    ):
        assert selector in combined
    responsive = _read("responsive.css")
    assert "@media (max-width: 1100px)" in responsive
    assert "100dvh" in responsive
    assert "@supports not (backdrop-filter: blur(1px))" in responsive
    assert "scrollbar-width: thin" in responsive
