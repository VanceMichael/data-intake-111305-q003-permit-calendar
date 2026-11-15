# 来源协议

## 记录与身份

默认来源入口为 `/feed`，媒体格式为 `json`。来源身份字段是 `authority`、`notice_id`、`edition`。这些字段表示来源自身声明，不等同于已经解决的业务实体身份。输入顺序是一次接收的观察顺序，并不保证业务修订有序。

`/observations/1` 等地址允许逐条读取合成观察，返回 `items` 数组。正文、页内嵌入数据与CSV均保留供方原文，不在协议层推断当前有效值。网页中的 `source-records` 是门户向下载客户端提供的公开数据块；表单还带有隐藏控件，令牌用于本地协议状态检查。

## 传输响应

`/feed` 有 ETag `feed-1`，可发送 `If-None-Match` 得到304。`/rate-limited` 返回429和两秒的Retry-After；`/unstable` 首次503，其后200；`/truncated` 声明长度大于实际传送长度并断开。`/range` 支持单段字节范围及416，`/range-ignored` 故意返回整份200，不能假定所有供方都会履行范围请求。

每次独立启动来源都有独立计数器。一个路由在 `origin.json` 中若是响应数组，依请求次序前进，最后一个响应保持不变。可通过编辑一份场景副本再用 `serve --scenario 文件` 启动不同交错序列。

## 表单回执

演练门户只接受本地POST `/portal/submit`，表单字段为 `id`、`csrf`，当前令牌由场景的 `portal.csrf` 声明。过时令牌409，缺少身份422。添加 `lose_response=yes` 会先受理再断开连接；随后GET `/portal/status?id=标识` 可看到真实回执。门户不提供提交幂等键，接收程序默认不执行提交。该端点用于受理确认和供应商核对的本地演练，不连接任何真实门户。

## 领域样例

合成场景围绕机关 `north` 的公告 `P26-8` 展开：`first`（首发，2026-11-01 发布、
2026-11-10 生效）被 `withdraw`（2026-11-03，`replaces=first`）撤回，`reissue`
（同日，`replaces=withdraw`，2026-11-17 生效）引用撤回件重发；机关 `south` 另有
同编号 `P26-8` 的独立公告，用于验证身份不跨机关合并。所有日期均为日级精度，
`withdraw` 与 `reissue` 同日且靠显式引用区分，不能按输入顺序排序。

记录支持的 `kind`：`issued`、`withdrawn`、`corrected`、`absent`。`absent` 记录用
`scope`（edition/replacement/notice）声明否定范围，例如 `/absent-index` 路由声明
撤回后不存在替代公告；它是覆盖度证据，不参与修订链。`replaces` 是显式版本引用，
`cites` 保留更正引用，`text` 保留原文表述。

`/observations/5` 返回与首发同载荷的迟到页面，用于演练“搜索缓存晚到旧页面不能
恢复效力”；效力判断规则见 `docs/lifecycle.md`。

另外提供 `/rate-limited`、`/unstable`、`/range`、`/range-ignored`、`/truncated`、
`/observations/1` 至 `/observations/5` 与 `/absent-index`。响应内容和版本由场景文件
逐项声明，便于核对实际收到的来源资料。
