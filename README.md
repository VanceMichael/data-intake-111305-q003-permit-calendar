# 施工许可公告的修订链采集

这是资料接收程序的基础版。它请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录。当前不生成面向业务的有效版本，也不自动执行门户提交；重复观察会分别保留，原始内容按摘要复用。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q permit_calendar tests
python3 -m permit_calendar demo
```

演示从空的临时目录启动本地来源，真实完成一次 HTTP 获取及归档，并输出原样记录；结束自动清理。本地测试、编译和演示合计一般不超过十秒，可直接展示，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m permit_calendar serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m permit_calendar capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。业务解释与原始接收独立演进。

## 公告生命周期与效力判断

在只追加归档之上，`lifecycle.py` 把来源记录抽取为带双时间轴的声明（来源声明时间含日/时/分/秒精度，采集观察时间来自归档），按 `(authority, notice_id)` 构建 `replaces` 修订链并给出效力状态；`ledger.py` 把某次查询固化为不可改写的进场依据。规则与衔接见 `docs/lifecycle.md`，要点：

- 日级时间戳按精度区间比较，同日且互不引用的公告保持 `pending_verification`，不用抓取顺序编造先后；
- 明确撤回后，迟到的同载荷旧页面只记为重复观察，互不引用的晚到新载荷产生 `late_unlinked_issuance` 冲突，许可不会自动恢复；只有显式引用撤回件的重发才能恢复效力，且视图保留撤回关系与原文；
- 替代页查询区分 `not_seen`（未见到，开放世界）、`observed`（已观察到引用页）与 `confirmed_absent`（来源 absent 声明且 scope 覆盖）；
- 冲突是一等输出，附原因码与全部相关原文，不挑选“看起来最新”的页面宣布解决；
- 快照只追加，边界是一组 capture_id 与视图指纹；`verify` 用同一批原始观察重算，后续更正不参与，进场依据不可被改写。

```sh
python3 -m permit_calendar derive --archive .runtime/raw.sqlite3
python3 -m permit_calendar derive --archive .runtime/raw.sqlite3 --observed-by 2026-11-10T08:00:01Z
python3 -m permit_calendar snapshot save --archive .runtime/raw.sqlite3 --ledger .runtime/ledger.sqlite3 --purpose "进场依据"
python3 -m permit_calendar snapshot verify --archive .runtime/raw.sqlite3 --ledger .runtime/ledger.sqlite3 --snapshot-id 1
```

## 开发资料

来源协议见 `docs/source-protocol.md`，生命周期衔接见 `docs/lifecycle.md`，领域观察关系见 `tests/test_source_contract.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径；`tests/test_lifecycle.py` 逐条固化这些口径（撤回-重发链、同日待核、迟到缓存、替代页三态、留档不可改写、跨机关与原文冲突）。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `permit_calendar` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m permit_calendar demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
