"""关注资料真实响应的只读适配层。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from crawl4weibo.exceptions.base import CrawlError

from weibo_book.errors import WeiboError, WeiboErrorKind, classify_error

from .following import FollowingObjectRecord


BLOGGER_URL = "https://weibo.com/ajax/profile/followContent"
SUPERTOPIC_URL = "https://weibo.com/ajax/profile/topicContent"
SUPERTOPIC_TAB_ID = "231093_-_chaohua"


@dataclass(frozen=True)
class BloggerPage:
    items: list[FollowingObjectRecord]
    reported_total: int
    next_cursor: int
    has_filtered_attentions: bool


@dataclass(frozen=True)
class SupertopicPage:
    items: list[FollowingObjectRecord]
    reported_total: int
    max_page: int


@dataclass(frozen=True)
class FollowingListResult:
    items: list[FollowingObjectRecord]
    reported_total: int
    complete: bool


FollowingRequest = Callable[[str, dict[str, object], dict[str, str]], dict[str, Any]]


def _parse_root(payload: object) -> dict[str, object]:
    if (
        not isinstance(payload, dict)
        or type(payload.get("ok")) is not int
        or payload.get("ok") != 1
    ):
        raise WeiboError("关注资料响应状态无效", kind=WeiboErrorKind.PARSE)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise WeiboError("关注资料响应缺少 data 对象", kind=WeiboErrorKind.PARSE)
    return data


def _strict_nonnegative_int(value: object, message: str) -> int:
    if type(value) is not int or value < 0:
        raise WeiboError(message, kind=WeiboErrorKind.PARSE)
    return value


def parse_blogger_page(payload: object, *, source_offset: int) -> BloggerPage:
    if type(source_offset) is not int or source_offset < 0:
        raise WeiboError("关注博主返回次序起点无效", kind=WeiboErrorKind.PARSE)
    data = _parse_root(payload)
    follows = data.get("follows")
    if not isinstance(follows, dict):
        raise WeiboError("关注博主响应缺少 follows 对象", kind=WeiboErrorKind.PARSE)
    users = follows.get("users")
    if not isinstance(users, list):
        raise WeiboError("关注博主响应缺少 users 清单", kind=WeiboErrorKind.PARSE)
    if not users:
        raise WeiboError(
            "尚未取得关注博主空清单的真实响应证据",
            kind=WeiboErrorKind.PARSE,
        )
    total = _strict_nonnegative_int(
        follows.get("total_number"), "关注博主报告总数无效"
    )
    cursor = _strict_nonnegative_int(
        follows.get("next_cursor"), "关注博主下一页游标无效"
    )
    filtered = follows.get("has_filtered_attentions")
    if type(filtered) is not bool:
        raise WeiboError("关注博主过滤标志无效", kind=WeiboErrorKind.PARSE)
    items: list[FollowingObjectRecord] = []
    for index, user in enumerate(users):
        if not isinstance(user, dict):
            raise WeiboError("关注博主条目类型无效", kind=WeiboErrorKind.PARSE)
        identity = user.get("idstr")
        name = user.get("screen_name")
        if not isinstance(identity, str) or not identity or not identity.isdigit():
            raise WeiboError("关注博主稳定身份无效", kind=WeiboErrorKind.PARSE)
        if not isinstance(name, str) or not name:
            raise WeiboError("关注博主名称无效", kind=WeiboErrorKind.PARSE)
        items.append(
            FollowingObjectRecord(
                object_type="blogger",
                object_id=identity,
                display_name=name,
                page_url=f"https://weibo.com/u/{identity}",
                app_scheme="",
                source_order=source_offset + index,
            )
        )
    if len({item.object_id for item in items}) != len(items):
        raise WeiboError("关注博主页面包含重复稳定身份", kind=WeiboErrorKind.PARSE)
    return BloggerPage(items, total, cursor, filtered)


def parse_supertopic_page(payload: object, *, source_offset: int) -> SupertopicPage:
    if type(source_offset) is not int or source_offset < 0:
        raise WeiboError("关注超话返回次序起点无效", kind=WeiboErrorKind.PARSE)
    data = _parse_root(payload)
    entries = data.get("list")
    if not isinstance(entries, list):
        raise WeiboError("关注超话响应缺少 list 清单", kind=WeiboErrorKind.PARSE)
    if not entries:
        raise WeiboError(
            "尚未取得关注超话空清单的真实响应证据",
            kind=WeiboErrorKind.PARSE,
        )
    total = _strict_nonnegative_int(data.get("total_number"), "关注超话报告总数无效")
    max_page = _strict_nonnegative_int(data.get("max_page"), "关注超话总页数无效")
    if max_page != 1:
        raise WeiboError(
            "关注超话存在多页，但尚未取得精确翻页参数",
            kind=WeiboErrorKind.PARSE,
        )
    items: list[FollowingObjectRecord] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise WeiboError("关注超话条目类型无效", kind=WeiboErrorKind.PARSE)
        identity = entry.get("oid")
        name = entry.get("topic_name")
        link = entry.get("link")
        scheme = entry.get("scheme")
        following = entry.get("following")
        if not isinstance(identity, str) or not identity:
            raise WeiboError("关注超话稳定身份无效", kind=WeiboErrorKind.PARSE)
        if not isinstance(name, str) or not name:
            raise WeiboError("关注超话名称无效", kind=WeiboErrorKind.PARSE)
        if not isinstance(link, str) or not link:
            raise WeiboError("关注超话网页入口无效", kind=WeiboErrorKind.PARSE)
        if not isinstance(scheme, str) or not scheme:
            raise WeiboError("关注超话应用入口无效", kind=WeiboErrorKind.PARSE)
        if following is not True:
            raise WeiboError("关注超话关系标志无效", kind=WeiboErrorKind.PARSE)
        items.append(
            FollowingObjectRecord(
                object_type="supertopic",
                object_id=identity,
                display_name=name,
                page_url=link,
                app_scheme=scheme,
                source_order=source_offset + index,
            )
        )
    if len({item.object_id for item in items}) != len(items):
        raise WeiboError("关注超话清单包含重复稳定身份", kind=WeiboErrorKind.PARSE)
    return SupertopicPage(items, total, max_page)


class CrawlClientFollowingRequest:
    """将当前 `crawl4weibo` 客户端限制为单次、无代理的只读请求。"""

    def __init__(self, client: object) -> None:
        self._client = client

    def __call__(
        self,
        url: str,
        params: dict[str, object],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            result = self._client._request(
                url,
                params,
                max_retries=1,
                use_proxy=False,
                headers=headers,
            )
        except CrawlError as exc:
            raise WeiboError(
                "读取关注资料失败",
                kind=classify_error(exc),
                original=exc,
            ) from exc
        except Exception as exc:
            raise WeiboError(
                "读取关注资料失败",
                kind=classify_error(exc),
                original=exc,
            ) from exc
        if not isinstance(result, dict):
            raise WeiboError("关注资料响应类型无效", kind=WeiboErrorKind.PARSE)
        return result


class FollowingSource:
    def __init__(
        self,
        request: FollowingRequest,
        *,
        self_uid: str,
        session_probe: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(self_uid, str) or not self_uid or not self_uid.isdigit():
            raise WeiboError("当前登录账号标识无效", kind=WeiboErrorKind.AUTH)
        self._request = request
        self._headers = {"Referer": f"https://weibo.com/u/{self_uid}"}
        self._session_probe = session_probe

    def probe_session(self) -> None:
        """复用阶段 2 已验证的唤醒后会话检查，不定义新请求。"""

        if self._session_probe is not None:
            self._session_probe()

    def fetch_blogger_page(
        self,
        *,
        page: int,
        next_cursor: int | None,
        source_offset: int,
    ) -> BloggerPage:
        if type(page) is not int or page < 1:
            raise WeiboError("关注博主页码无效", kind=WeiboErrorKind.API)
        if page == 1 and next_cursor is not None:
            raise WeiboError("关注博主首页不得携带下一页游标", kind=WeiboErrorKind.API)
        if page > 1 and (type(next_cursor) is not int or next_cursor <= 0):
            raise WeiboError("关注博主后续页游标无效", kind=WeiboErrorKind.API)
        params: dict[str, object] = {"sortType": "all", "page": page}
        if next_cursor is not None:
            params["next_cursor"] = next_cursor
        return parse_blogger_page(
            self._request(BLOGGER_URL, params, dict(self._headers)),
            source_offset=source_offset,
        )

    def fetch_bloggers(self) -> FollowingListResult:
        page = 1
        cursor: int | None = None
        items: list[FollowingObjectRecord] = []
        reported_total: int | None = None
        while True:
            result = self.fetch_blogger_page(
                page=page,
                next_cursor=cursor,
                source_offset=len(items),
            )
            if reported_total is None:
                reported_total = result.reported_total
            elif reported_total != result.reported_total:
                raise WeiboError("关注博主跨页报告总数不一致", kind=WeiboErrorKind.PARSE)
            if result.has_filtered_attentions:
                raise WeiboError("关注博主结果包含被过滤条目，清单不完整", kind=WeiboErrorKind.PARSE)
            known = {item.object_id for item in items}
            if any(item.object_id in known for item in result.items):
                raise WeiboError("关注博主跨页出现重复稳定身份", kind=WeiboErrorKind.PARSE)
            items.extend(result.items)
            if result.next_cursor == 0:
                break
            cursor = result.next_cursor
            page += 1
        assert reported_total is not None
        if len(items) != reported_total:
            raise WeiboError("关注博主条目数与报告总数不一致", kind=WeiboErrorKind.PARSE)
        return FollowingListResult(items, reported_total, True)

    def fetch_supertopics(self) -> FollowingListResult:
        page = parse_supertopic_page(
            self._request(
                SUPERTOPIC_URL,
                {"tabid": SUPERTOPIC_TAB_ID},
                dict(self._headers),
            ),
            source_offset=0,
        )
        if len(page.items) != page.reported_total:
            raise WeiboError(
                "关注超话条目数与报告总数不一致",
                kind=WeiboErrorKind.PARSE,
            )
        return FollowingListResult(page.items, page.reported_total, True)
