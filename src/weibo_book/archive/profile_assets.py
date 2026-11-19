"""资料图片身份：只取 URL 路径文件名，保留原始大小写。"""

from urllib.parse import urlsplit


def profile_asset_identity(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1] or url
