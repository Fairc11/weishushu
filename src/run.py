"""v2.0.0 入口：dev/frozen 双路径 + 封包自检分派。

- dev 模式（`python run.py`）：走 desktop_app.main() 弹 pywebview 窗口
- frozen 模式（PyInstaller 打包后）：同上，但路径解析走运行时上下文
- 封包自检只识别：
  - `--packaged-self-test --self-test-output <绝对JSON路径>`
  - `--packaged-shell-smoke --self-test-output <绝对JSON路径>`
"""

from __future__ import annotations

import logging
import multiprocessing
import sys
from pathlib import Path

# ====== UTF-8 输出兜底（frozen 下 console=False 不会触发，但 dev 安全）======
if sys.stdout is not None:
    try:
        enc = sys.stdout.encoding
        if enc and enc.lower() not in ("utf-8", "utf8") and sys.stdout.buffer is not None:
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _self_test_mode() -> str | None:
    args = sys.argv[1:]
    modes = [arg for arg in args if arg in ("--packaged-self-test", "--packaged-shell-smoke")]
    if len(modes) > 1:
        return "conflict"
    if len(modes) == 1:
        return modes[0]
    return None


def _self_test_output() -> Path | None:
    args = sys.argv[1:]
    try:
        index = args.index("--self-test-output")
    except ValueError:
        return None
    if index + 1 >= len(args):
        return None
    value = args[index + 1]
    path = Path(value)
    if not path.is_absolute():
        return None
    return path


def _run_self_test(mode: str, output: Path) -> int:
    from backend.app.runtime_context import resolve_runtime_context

    context = resolve_runtime_context()
    if mode == "--packaged-shell-smoke":
        from desktop.self_test.shell import run_shell_smoke
        result = run_shell_smoke(context, output)
    else:
        from desktop.self_test.functional import run_functional_self_test
        result = run_functional_self_test(context, output)
    if result.get("error_kind") == "environment_unavailable":
        return 3
    if result.get("error_kind") is not None or any(
        step.get("status") == "failed" for step in result.get("steps", [])
    ):
        return 1
    return 0


def main() -> int:
    mode = _self_test_mode()
    if mode == "conflict":
        print("只能同时指定一种自检模式", file=sys.stderr)
        return 1
    if mode is not None:
        output = _self_test_output()
        if output is None:
            print("缺少绝对 --self-test-output 路径", file=sys.stderr)
            return 1
        if output.exists():
            print("自检输出路径已存在", file=sys.stderr)
            return 1
        return _run_self_test(mode, output)

    # 普通启动不得接受自检临时根。
    import os
    if os.environ.get("WEISHUSHU_SELF_TEST_ROOT"):
        print("普通启动不得设置 WEISHUSHU_SELF_TEST_ROOT", file=sys.stderr)
        return 1

    # B07 v1.2.0: 日志初始化由 desktop_app 统一负责（避免 handler 重复 + force=True 重建泄漏）。
    # 源码启动先解析唯一运行时上下文；源码态强制 dev，frozen 只认精确可执行文件名。
    from backend.app.runtime_context import resolve_runtime_context

    resolve_runtime_context()
    try:
        from desktop_app import main as desktop_main
    except Exception as e:
        # 兜底 logger（desktop_app 未 import 时也能记录）
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            force=True,
        )
        log = logging.getLogger("weishushu.run")
        log.exception("导入 desktop_app 失败: %s", e)
        return 1

    try:
        return desktop_main()
    except KeyboardInterrupt:
        logging.getLogger("weishushu.run").info("用户中断")
        return 0
    except Exception as e:
        logging.getLogger("weishushu.run").exception("未捕获异常: %s", e)
        return 1


if __name__ == "__main__":
    # PyInstaller 子进程必须先分派到 resource_tracker 等内部入口，
    # 否则会误执行 desktop_main() 并额外启动窗口与后端端口。
    multiprocessing.freeze_support()
    sys.exit(main())
