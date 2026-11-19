# 关注资料真实响应脱敏样本

取证日期：2026-07-17

来源：用户本人登录后的微博官方桌面页面。

取证边界：

- 只打开本人主页中的“我的关注”和“超话”栏目；
- 只读取页面自然产生的 GET 请求；
- 未执行关注、取消关注、签到、点赞、评论、转发、备注或分组修改；
- 未保存 Cookie、令牌、真实 UID、真实昵称、头像地址或正文；
- 列表对象只保留一个全字段脱敏样本，真实对象数量记录在 `observation.original_item_count`；
- 数值身份、链接和日期使用保持原类型与格式的测试值。

## 文件

| 文件 | 真实来源 | 用途 |
|---|---|---|
| `following_page_1.json` | `/ajax/profile/followContent?sortType=all&page=1` | 博主关注第一页、完整对象键名和下一页游标 |
| `following_page_2.json` | `/ajax/profile/followContent?sortType=all&page=2&next_cursor=50` | 博主关注终页和不完整性证据 |
| `followed_supertopics_page_1.json` | `/ajax/profile/topicContent?tabid=231093_-_chaohua` | 关注超话结构、稳定标识和固定入口 |

## 已确认语义

- 页面默认排序标签为“最新关注”，对应 `sortType=all`。
- “最新发博”对应 `sortType=timeDown`。
- 博主列表初始状态为 `page=1` 和空 `next_cursor`；加载更多时同时递增 `page` 并传入上一页的 `next_cursor`。
- `next_cursor=0` 表示博主列表终页。
- 当前真实样本中第一页 50 个对象，第二页 41 个对象。
- 报告总数比两页唯一可见对象数多 1；`has_filtered_attentions` 在同一终页参数的多次读取中出现过 `true` 和 `false`，不能单独用作完整性判定。
- 博主对象的 `id` 和 `idstr` 是稳定数字身份；自然主页路径为 `/u/{idstr}`。
- 博主对象中的 `created_at` 与该博主详情中的注册时间是同一时刻，不是关注日期。
- 超话对象提供 `oid`、`link`、`scheme`、`topic_name`、`title` 和 `following`。
- 当前超话响应 `max_page=1`，列表中没有关注日期或关注天数字段。

## 不能据此推导

- 不能根据博主 `created_at` 计算关注时长；
- 不能根据列表位置反推出精确关注日期；
- 不能仅依据 `next_cursor=0` 或 `has_filtered_attentions` 认定清单完整；
- 不能把超话名称当作稳定身份；
- 不能从当前样本推测空清单、未登录、验证页面或 `432` 的关注接口响应结构。
