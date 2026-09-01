"""前端多文件资产的测试聚合辅助。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CSS_ROOT = ROOT / "backend" / "app" / "static" / "css"
JS_ROOT = ROOT / "backend" / "app" / "static" / "js"

PRODUCTION_CSS = (
    "tokens.css",
    "base.css",
    "shell.css",
    "components.css",
    "workflows.css",
    "responsive.css",
)

FRONTEND_MODULES = (
    "state.js",
    "feedback.js",
    "login.js",
    "archive.js",
    "tasks.js",
    "desktop.js",
)


@dataclass(frozen=True)
class TextAssetBundle:
    paths: tuple[Path, ...]

    def read_text(self, encoding: str = "utf-8") -> str:
        return "\n".join(path.read_text(encoding=encoding) for path in self.paths)

    def exists(self) -> bool:
        return all(path.exists() for path in self.paths)


@dataclass(frozen=True)
class FrontendModuleBundle(TextAssetBundle):
    def read_text(self, encoding: str = "utf-8") -> str:
        chunks = []
        for path in self.paths:
            source = path.read_text(encoding=encoding)
            source = re.sub(r"^import\s+[^;]+;\s*$", "", source, flags=re.MULTILINE)
            source = re.sub(r"^void\s+initFeedback;\s*$", "", source, flags=re.MULTILINE)
            init_name = f"init_{path.stem}"
            source = source.replace("export function init(", f"function {init_name}(")
            source = source.replace("init(Ptu);", f"{init_name}(Ptu);")
            source = re.sub(r"^export\s+", "", source, flags=re.MULTILINE)
            if path.name == "desktop.js":
                source = re.sub(
                    r"if \(!Ptu\.__domReadyBound\)[\s\S]*$",
                    "",
                    source,
                )
            chunks.append(source)
        return "\n".join(chunks)


def css_bundle_asset() -> TextAssetBundle:
    return TextAssetBundle(tuple(CSS_ROOT / name for name in PRODUCTION_CSS))


def frontend_bundle_asset() -> TextAssetBundle:
    legacy = JS_ROOT / "app.js"
    if legacy.exists():
        return TextAssetBundle((legacy,))
    return FrontendModuleBundle(
        tuple(JS_ROOT / "modules" / name for name in FRONTEND_MODULES)
    )
