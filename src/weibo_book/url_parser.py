"""微博 URL 解析工具。"""

from __future__ import annotations

import re
from urllib.parse import urlparse


def parse_uid_from_url(url: str) -> str:
    """从微博 URL 中提取 UID

    v1.1.2 修复 F4：支持分享文本（用户从微博 APP 复制粘贴时的"前缀文本 + URL + 后缀"格式）。
    先用正则提取真正 URL 子串，去掉尾部标点，再走原解析逻辑。

    支持格式：
    - 裸 URL：https://weibo.com/u/1234567890
    - 分享文本："麻花辫野生选手 https://weibo.com/u/1234567890"
    - 分享文本（含 at 用户）："@XX的微博 ... https://weibo.com/u/1234567890 0@0.com :0pm"
    - 短链：https://t.cn/xxx（会 fallthrough 到 ValueError，由调用方处理）
    """
    url = url.strip()

    # F4 v1.1.2：从分享文本里提取真正的 URL 子串
    m = re.search(r'https?://[^\s　]+', url)
    if m:
        url = m.group(0)
        # 去掉尾部标点（句号/逗号/分号/感叹号/问号/反引号/中文标点）
        url = re.sub(r'[.,;:!?`)\]}>，。；：！？」』】　]+$', '', url)

    parsed = urlparse(url)
    if 'm.weibo.cn' in parsed.netloc:
        path_match = re.search('/u/(\\d+)', parsed.path)
        if path_match:
            return path_match.group(1)
        path_match = re.search('/profile/(\\d+)', parsed.path)
        if path_match:
            return path_match.group(1)
    if 'weibo.com' in parsed.netloc:
        path_match = re.search('/u/(\\d+)', parsed.path)
        if path_match:
            return path_match.group(1)
        path_match = re.search('^/(\\d+)', parsed.path)
        if path_match:
            return path_match.group(1)
        nickname = parsed.path.strip('/')
        if nickname and nickname != 'u':
            return f'nickname:{nickname}'
    uid_param = re.search('[?&]uid=(\\d+)', url)
    if uid_param:
        return uid_param.group(1)
    raise ValueError(f'无法从 URL 中解析 UID: {url}')
