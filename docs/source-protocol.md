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

`fixtures/origin.json` 的 `/feed` 提供一组修订链样例：`north` 机关的 `P26-8` 依次有首发（`first`）、撤回（`withdraw`，引用 `first`）、重发（`reissue`，引用 `withdraw`）三个版本，撤回与重发同为日级精度的同一日；`south` 机关另有同编号 `P26-8` 的独立公告，用于核对跨机关身份不混淆。`/observations/1` 至 `/observations/4` 逐条提供上述记录。

`fixtures/lifecycle.json` 把同一条链拆成交错序列，供生命周期演示与业务用例：`/stage/1` 至 `/stage/4` 是逐阶段推进的完整列表（带 ETag `lc-1` 至 `lc-4`，可演练 304），`/cache/first` 模拟搜索缓存晚到的旧页，`/single/reissue` 模拟无完整列表佐证的单页观察，`/south/feed` 提供另一机关的同编号公告。业务侧关注的交错情形及可观察口径以 `fixtures/business_cases.json` 为准。

另外提供 `/rate-limited`、`/unstable`、`/range`、`/range-ignored`、`/truncated`、`/observations/1`、`/observations/2`、`/observations/3`、`/observations/4`。响应内容和版本由场景文件逐项声明，便于核对实际收到的来源资料。
