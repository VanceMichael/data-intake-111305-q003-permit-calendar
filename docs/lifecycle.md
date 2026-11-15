# 公告生命周期：从采集观察到效力判断

本文档描述原始采集（`archive.py`，只追加）与业务效力判断（`lifecycle.py`、`ledger.py`）
之间的衔接。设计前提是：**采集层不解释业务，判断层不回写观察**。任何效力结论都可以
回溯到具体 capture 与来源原文。

## 1. 分层与数据流

```
HTTP 响应
  └─ Archive（captures + bodies，只追加，摘要复用）
       └─ load_observations()
            ├─ probe_from_capture()  覆盖度探针（每次采集本身）
            └─ extract_statements()  来源记录 -> 声明 statement
                 └─ derive_thread()   按 (authority, notice_id) 做效力判断
                      └─ build_view()  当前时点 / 历史时点视图
                           └─ Ledger   进场依据快照（只追加，可复核）
```

* 原始库没有更新、删除路径；判断结果与快照存放在独立的留档库，不回写原始库。
* 每条声明携带 `capture_id / source / url / observed_at / raw`，结论永远可点开原文。
* 解析失败的响应不会被当成“空来源”：它只生成 `parse_failure` 探针，不产生否定证据。

## 2. 双时间轴与精度

| 时间 | 字段 | 含义 |
| --- | --- | --- |
| 来源声明时间 | `published`（及 `start/end`） | 公告自己声称的发布/生效时间，带 `precision` |
| 采集观察时间 | `observed_at`（来自 captures） | 本系统实际收到该页面的时刻 |

`precision` 不是展示格式，而是**不确定性区间**：日级时间戳覆盖整个自然日
`[当日 00:00, 次日 00:00)`。声明精度不得细于文本实际粒度（例如 `2026-11-03T14:00`
不能自称 hour 精度），否则拒绝并进入 rejections，杜绝伪造精度。

排序规则（`compare_stamps`）：

* 两区间首尾可分（`left.end <= right.start`）才返回先后；
* 区间重叠（含同一自然日的两份日级声明）返回不可判定；
* 缺位时间戳一律不可判定。

因此**同日两份互不引用的公告保持 `pending_verification`**，抓取顺序（observed_at 的先后）
只用于识别迟到数据，永远不会被用来给声明时间排序。

## 3. 身份与修订链

* 线程身份是 `(authority, notice_id)`。不同机关同编号是两个线程，不合并。
* 线程内用 `edition` 标识版本，`replaces` 是来源原文给出的显式引用边，
  `cites` 保留更正引用文本。边只来自原文，不来自抓取顺序或日期猜测。
* 同一 `edition` 出现不同载荷 → `edition_identity_conflict`；相同载荷再次出现 →
  `reobservation`（搜索缓存重发旧页面的典型形态），不产生新效力事件。
* 引用了未观察到的目标 → `unresolved_edges`，线程待核，不假设目标存在或内容。
* 引用成环、一个版本被多个后继同时替代 → 对应冲突码，线程置 `conflict`。

## 4. 效力状态机

无阻断性冲突时，沿单条 replaces 链找到 head，结合业务时点判断：

| head 情形 | 线程状态 |
| --- | --- |
| 发布时间未到 / 边界落在精度区间内 | `pending_verification` |
| `issued` 且生效起始已到 | `effective` |
| `issued` 但生效起始在未来 | `scheduled` |
| `withdrawn` | `withdrawn` |
| `corrected` | 继承前一版本的效力 |
| 存在悬空引用、无可用 head | `pending_verification` |
| 存在证据级冲突 | `conflict` |

### 撤回与重发的关系

* **撤回不被迟到数据推翻**：已观察到明确撤回文本后，晚到的旧页面若与在案版本同载荷，
  只是 reobservation；若是互不引用的新载荷且声明时间可判定晚于撤回，标记
  `late_unlinked_issuance`，线程 `conflict`，许可不会自动恢复。
* **真正的重发可以恢复效力**：重发件通过 `replaces` 显式引用撤回件，链
  `first -> withdraw -> reissue` 完整时 head 为 reissue，状态恢复 `effective` 或
  `scheduled`，视图同时保留撤回节点及其原文，关系可直接展示给项目人员。
* 互不引用且时间不可判定（含同日）→ `unlinked_same_day`，线程保持 `pending_verification`，
  冲突条目同时展示两份原文。

## 5. “未见到”与“不存在”

这是开放世界与封闭证据的区分，`replacement_status()` 给三态答案：

| 状态 | 成立条件 |
| --- | --- |
| `not_seen` | 归档里没有替代页。默认答案；明确说明“未见到 ≠ 不存在”，需要继续观察 |
| `observed` | 链上存在 `replaces` 指向该 edition 的声明，附该声明全文 |
| `confirmed_absent` | 存在来源 `kind=absent` 声明，且其 `scope` 覆盖询问范围（edition / replacement / notice） |

`confirmed_absent` 只在来源明确否定且范围匹配时给出，结论仅在声明范围内成立。
采集侧的 404/410 是来源对该 URL 的否定性探针（`origin_negative`），200/304 是
`feed_observed`；429/503 等既不是肯定也不是否定证据。视图的 `coverage.open_world`
始终为真，并给出最后一次成功观察入口的时间。

## 6. 冲突展示原则

冲突是一等输出，不是异常。每条冲突包含：

* 原因码（如 `edition_identity_conflict`、`late_unlinked_issuance`、
  `unlinked_same_day`、`multiple_successors`、`reference_cycle`）；
* 全部相关声明的来源、观察时间、声明时间（含精度区间）与原文 `raw`。

系统不会挑选“看起来最新”的页面宣布解决；同日不可判定与证据矛盾分别落到
`pending_verification` 与 `conflict`，由人工结合原文核处。

## 7. 查询入口：当前时点与历史留档

视图是双时间轴参数化的（`build_view`）：

* `business_time`：业务时点，决定 scheduled/effective 等判断；
* `observed_by`：知识时点，只纳入该时刻之前进入归档的声明，用于历史回放
  （例如“重发到达之前系统看到的是 withdrawn”）。

CLI 入口：

```sh
# 当前/指定业务时点的效力视图
python3 -m permit_calendar derive --archive raw.sqlite3
python3 -m permit_calendar derive --archive raw.sqlite3 \
    --business-time 2026-11-12T00:00:00Z --authority north --notice-id P26-8
# 历史回放：只使用该时刻之前观察到的页面
python3 -m permit_calendar derive --archive raw.sqlite3 --observed-by 2026-11-10T08:00:01Z
# 替代页三态查询
python3 -m permit_calendar derive --archive raw.sqlite3 --replacement-of withdraw
```

## 8. 进场依据留档（Ledger）

项目人员保存某次查询作为进场依据时，`Ledger.save()` 写入：

* 用途、业务时点、知识时点；
* 输入边界——一组 `capture_ids`；
* 完整视图 JSON 与内容指纹（`view_fingerprint`）。

留档表只追加，没有更新/删除接口。后续更正、撤回、重发进入原始归档后：

* `get` 返回的历史视图不变；
* `verify` 用快照声明的同一批 capture 重新派生视图并比对指纹——
  新增观察因 capture_id 不在边界内而不参与重算，因此**后续更正无法改写进场依据**；
* 指纹不符（库文件被外部篡改）时 `intact=false`。

```sh
python3 -m permit_calendar snapshot save --archive raw.sqlite3 \
    --ledger ledger.sqlite3 --purpose "2026-11-15 进场依据"
python3 -m permit_calendar snapshot list --ledger ledger.sqlite3
python3 -m permit_calendar snapshot get --ledger ledger.sqlite3 --snapshot-id 1
python3 -m permit_calendar snapshot verify --archive raw.sqlite3 \
    --ledger ledger.sqlite3 --snapshot-id 1
```

## 9. 与业务案例的对应

`fixtures/business_cases.json` 列出七个业务场景及其可观察口径，测试在
`tests/test_lifecycle.py` 中逐条固化，包括撤回-重发链、同日待核、迟到缓存、
not_seen/confirmed_absent、留档不可改写、跨机关同编号与原文冲突。
