# 施工许可公告的修订链采集与生命周期解释

仓库分两层。接收层请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，能够从 JSON、CSV 或门户嵌入的数据块读回来源记录；重复观察分别保留，原始内容按摘要复用。解释层在原始归档之上重放出公告生命周期：发布机关、公告编号、原文更正引用与来源声明的时间精度都按原样保留，公告之间的先后只认原文中的更正引用；撤回后晚到的旧页不恢复效力，同日互不引用的公告保持待核，判断可留档且留档不被后续更正改写。当前不自动执行门户提交。

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
python3 -m permit_calendar lifecycle-demo
```

两个演示都从空的临时目录启动本地来源。`demo` 真实完成一次 HTTP 获取及归档并输出原样记录；`lifecycle-demo` 按撤回、缓存旧页晚到、重发、同日冲突的交错顺序走完整条生命周期，逐步输出效力判断与留档校验。结束自动清理，不依赖历史日志。

## 常驻来源与接收

```sh
python3 -m permit_calendar serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m permit_calendar capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

归档库只追加来源观察，不用最后一条响应覆盖先前响应。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`origin.py` 提供协议演练。业务解释可与原始接收独立演进。

## 生命周期解释

解释层把原始归档重放为声明（`derived.py`），再在查询时按知识截止计算效力（`lifecycle.py`）。派生库是归档的确定性重放，可删除重建；判断模型与状态口径见 `docs/lifecycle.md`。

```sh
# 采集时可声明观察口径：enumeration 表示一次完整列表观察
python3 -m permit_calendar capture --base-url 地址 --archive raw.sqlite3 --derived derived.sqlite3 \
    --source local-source --path /feed --scope enumeration
# 把尚未解释的捕获重放为声明（可反复执行，幂等）
python3 -m permit_calendar interpret --archive raw.sqlite3 --derived derived.sqlite3
# 当前时点判断；--explain 附全部相关原文
python3 -m permit_calendar status --archive raw.sqlite3 --derived derived.sqlite3 \
    --authority 发布机关 --notice 公告编号 [--explain]
# 历史知识时点：当时我们知道什么
python3 -m permit_calendar status --archive raw.sqlite3 --derived derived.sqlite3 \
    --authority 发布机关 --notice 公告编号 --as-of-capture 3
# 留档：保存进场依据，写入后不可改写
python3 -m permit_calendar snapshot save --archive raw.sqlite3 --derived derived.sqlite3 \
    --authority 发布机关 --notice 公告编号 --purpose "进场依据：某项目"
python3 -m permit_calendar snapshot list --derived derived.sqlite3
python3 -m permit_calendar snapshot show --derived derived.sqlite3 --id 1
python3 -m permit_calendar snapshot verify --archive raw.sqlite3 --derived derived.sqlite3 --id 1
# 事后补注某次捕获的观察口径
python3 -m permit_calendar scope --derived derived.sqlite3 --capture 2 --scope enumeration
```

判断输出中的 `coverage.replacement` 区分两种口径：`not_observed` 表示未见到替代页，`absent_as_of` 表示截至某次完整列表观察（含 304 内容未变确认）来源自己的列表中没有替代页，并给出依据捕获号。`state` 为 `pending` 时 `conflicts` 列出冲突各方，`--explain` 展示对应原文，不会替用户选定一方。

## 开发资料

来源协议见 `docs/source-protocol.md`，生命周期判断模型见 `docs/lifecycle.md`，领域观察关系见 `tests/test_source_contract.py`。`fixtures/business_cases.json` 保存业务侧关注的交错情况及可观察口径，`tests/test_lifecycle.py` 逐条执行这些情形并核对判断；原始采集与协议仍由基础测试覆盖。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `permit_calendar` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m permit_calendar demo` 与 `python3 -m permit_calendar lifecycle-demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
