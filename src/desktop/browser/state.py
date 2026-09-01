"""Pure layout state for the native split browser."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping


DEFAULT_RATIO = 0.62
LEFT_MIN_WIDTH = 560
RIGHT_MIN_WIDTH = 360
DIVIDER_WIDTH = 1
FRAME_FIELDS = frozenset({"x", "y", "width", "height", "visible"})


@dataclass(frozen=True)
class BrowserFrame:
    x: float
    y: float
    width: float
    height: float
    visible: bool

    def as_dict(self) -> dict[str, float | bool]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "visible": self.visible,
        }


def parse_browser_frame(value: Mapping[str, Any]) -> BrowserFrame:
    """Parse the exact browser-slot payload sent by the trusted local frontend."""
    if not isinstance(value, Mapping) or set(value) != FRAME_FIELDS:
        raise ValueError("浏览器区域字段不完整")
    if type(value["visible"]) is not bool:
        raise ValueError("浏览器区域 visible 必须是布尔值")

    coordinates: dict[str, float] = {}
    for name in ("x", "y", "width", "height"):
        raw = value[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"浏览器区域 {name} 必须是数字")
        number = float(raw)
        if not isfinite(number):
            raise ValueError(f"浏览器区域 {name} 必须是有限数")
        coordinates[name] = number

    if coordinates["x"] < 0 or coordinates["y"] < 0:
        raise ValueError("浏览器区域坐标不能为负数")
    if coordinates["width"] <= 0 or coordinates["height"] <= 0:
        raise ValueError("浏览器区域尺寸必须大于零")

    return BrowserFrame(**coordinates, visible=value["visible"])


def fit_browser_frame(
    frame: BrowserFrame,
    container_width: float,
    container_height: float,
) -> tuple[float, float, float, float] | None:
    """Clamp a top-left DOM rectangle and return its bottom-left Cocoa frame."""
    max_width = max(0.0, float(container_width) - frame.x)
    max_height = max(0.0, float(container_height) - frame.y)
    width = min(frame.width, max_width)
    height = min(frame.height, max_height)
    if not frame.visible or width <= 0 or height <= 0:
        return None
    cocoa_y = float(container_height) - frame.y - height
    return (frame.x, max(0.0, cocoa_y), width, height)


def clamp_split_position(
    total_width: float,
    ratio: float = DEFAULT_RATIO,
    left_min: int = LEFT_MIN_WIDTH,
    right_min: int = RIGHT_MIN_WIDTH,
    divider_width: int = DIVIDER_WIDTH,
) -> int:
    available = max(0, int(round(total_width)) - divider_width)
    if available < left_min + right_min:
        return max(0, min(available, int(round(available * ratio))))
    desired = int(round(available * ratio))
    return max(left_min, min(desired, available - right_min))


@dataclass
class BrowserLayoutState:
    ratio: float = DEFAULT_RATIO
    visible: bool = True

    def split_position(self, total_width: float) -> int:
        return clamp_split_position(total_width, self.ratio)

    def remember_split_position(self, left_width: float, total_width: float) -> None:
        available = max(0, int(round(total_width)) - DIVIDER_WIDTH)
        if available <= 0:
            return
        clamped = clamp_split_position(total_width, left_width / available)
        self.ratio = clamped / available
