"""build_exe.bat 用的版本号读取小工具。

从 backend/app/version.py 提取 VERSION = "x.y.z" 中的 x.y.z。
"""
import re
import sys
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "backend" / "app" / "version.py"
src = p.read_text(encoding="utf-8")
m = re.search(r'VERSION\s*=\s*["\']([^"\']+)["\']', src)
print(m.group(1) if m else "2.0.1")
