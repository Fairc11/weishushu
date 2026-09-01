"""运行报告生成工具。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "-"
    return str(value)


def write_run_report(
    output_dir: str | Path,
    started_at: datetime,
    finished_at: datetime,
    url: str,
    params: Mapping[str, Any],
    result: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> str:
    """写入单次生成任务报告，返回报告路径。"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    report_path = output_path / "report.md"

    result = result or {}
    media_summary = result.get("media_summary") or {}
    failed_media = media_summary.get("failed") or []
    elapsed = (finished_at - started_at).total_seconds()
    status = "失败" if error else "成功"

    lines = [
        "# 微书薯运行报告",
        "",
        f"- 状态：{status}",
        f"- URL：{url}",
        f"- 开始时间：{started_at:%Y-%m-%d %H:%M:%S}",
        f"- 结束时间：{finished_at:%Y-%m-%d %H:%M:%S}",
        f"- 耗时：{elapsed:.1f} 秒",
        f"- 提取条数：{result.get('posts_count', 0)}",
        "",
        "## 参数",
        "",
    ]

    for key in sorted(params):
        lines.append(f"- {key}：{_format_value(params[key])}")

    lines.extend([
        "",
        "## 输出文件",
        "",
        f"- Markdown：{_format_value(result.get('markdown'))}",
        f"- PDF：{_format_value(result.get('pdf'))}",
        f"- HTML：{_format_value(result.get('html'))}",
        "",
        "## 媒体下载",
        "",
        (
            f"媒体下载：{media_summary.get('success', 0)} 成功 / "
            f"{media_summary.get('fail', 0)} 失败 / "
            f"{media_summary.get('total', 0)} 总计"
        ),
    ])

    if failed_media:
        lines.extend(["", "### 失败媒体", ""])
        for item in failed_media[:50]:
            url_value = item.get("url", "")
            dest_value = item.get("dest", "")
            lines.append(f"- {dest_value} <- {url_value}")
        if len(failed_media) > 50:
            lines.append(f"- 其余 {len(failed_media) - 50} 条已省略")

    if error:
        lines.extend(["", "## 错误", "", error])

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(report_path)
