import argparse
import json
import tempfile
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .decoding import records
from .derived import DerivedStore
from .origin import load_scenario, serve


def capture(base_url, archive_path, source, path, headers=None, derived_path=None, scope=None):
    if scope is not None and derived_path is None:
        raise ValueError("--scope requires --derived")
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path, headers=headers)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        if scope is not None:
            derived = DerivedStore(derived_path)
            try:
                derived.set_scope(capture_id, scope)
            finally:
                derived.close()
            result["scope"] = scope
        return result
    finally:
        archive.close()


def interpret(archive_path, derived_path):
    archive, derived = Archive(archive_path), DerivedStore(derived_path)
    try:
        return derived.incorporate(archive)
    finally:
        archive.close()
        derived.close()


def status(archive_path, derived_path, authority, notice_id,
           as_of_capture=None, as_of_time=None, explain=False):
    archive, derived = Archive(archive_path), DerivedStore(derived_path)
    try:
        cutoff = derived.resolve_cutoff(archive, as_of_capture, as_of_time)
        return derived.judgment(archive, authority, notice_id, cutoff=cutoff, with_originals=explain)
    finally:
        archive.close()
        derived.close()


def _fixture_path(name):
    """定位随包发行的合成资料；源码目录与已安装 wheel 布局不同。"""
    candidate = Path(__file__).parent / "fixtures" / name
    if candidate.is_file():
        return candidate
    return Path(__file__).parents[1] / "fixtures" / name


def lifecycle_demo():
    """按业务交错情形完整走一遍：采集 -> 解释 -> 判断 -> 留档。"""
    scenario = load_scenario(_fixture_path("lifecycle.json"))
    steps = []

    def brief(step, judgment):
        entry = {"step": step, "state": judgment["state"], "as_of_capture": judgment["as_of_capture"]}
        if judgment["head"]:
            entry["head"] = judgment["head"]["edition"]
        if judgment["chain"]:
            entry["chain"] = [node["edition"] for node in judgment["chain"]]
        entry["coverage"] = judgment["coverage"]["replacement"]
        if judgment["coverage"].get("horizon_capture"):
            entry["coverage_horizon_capture"] = judgment["coverage"]["horizon_capture"]
        if judgment["conflicts"]:
            entry["conflicts"] = [c["type"] for c in judgment["conflicts"]]
        steps.append(entry)
        return entry

    with tempfile.TemporaryDirectory() as temporary, serve(scenario) as origin:
        raw = Path(temporary) / "raw.sqlite3"
        derived_path = Path(temporary) / "derived.sqlite3"
        derived = DerivedStore(derived_path)
        archive = Archive(raw)
        client = Client(origin.base_url)
        try:
            def observe(path, scope=None, headers=None):
                capture_id, _ = collect(client, archive, "lifecycle", path, headers=headers)
                if scope:
                    derived.set_scope(capture_id, scope)
                derived.incorporate(archive)
                return capture_id

            def current(explain=False):
                return derived.judgment(archive, "north", "P26-8", with_originals=explain)

            observe("/stage/1", scope="enumeration")
            judgment = current()
            brief("1 首次发布：仅见到 first（开工 2026-11-10）", judgment)
            snapshot_id = derived.save_snapshot(archive, "north", "P26-8", "进场依据：示例项目甲")

            observe("/stage/1", headers={"If-None-Match": "lc-1"})
            judgment = current()
            brief("2 条件请求确认内容未变（304 延长覆盖口径）", judgment)

            observe("/stage/2", scope="enumeration")
            brief("3 抓到明确撤回文本", current())

            observe("/cache/first")
            judgment = current()
            entry = brief("4 搜索缓存晚到旧页：效力不恢复", judgment)
            entry["first_sightings"] = next(
                len(node["sightings"]) for node in judgment["chain"] if node["edition"] == "first"
            )

            observe("/stage/3", scope="enumeration")
            brief("5 真正重发：恢复效力并展示与撤回的关系", current())

            observe("/stage/4", scope="enumeration")
            judgment = current(explain=True)
            entry = brief("6 同日互不引用的新页出现：保持待核并展示双方原文", judgment)
            conflict = next(c for c in judgment["conflicts"] if c["type"] == "competing_heads")
            entry["competing_heads"] = sorted(conflict["heads"])
            entry["originals"] = [
                {"capture_id": o["capture_id"], "payload": o["payload"]}
                for o in judgment["originals"]
                if o["payload"].get("edition") in ("reissue", "reissue-b")
            ]

            saved = derived.snapshot(snapshot_id)
            steps.append({
                "step": "7 留档未被后续更正改写",
                "snapshot_id": snapshot_id,
                "state": saved["judgment"]["state"],
                "head": saved["judgment"]["head"]["edition"],
                "verify": derived.verify_snapshot(archive, snapshot_id)["intact"],
            })

            historical = derived.judgment(archive, "north", "P26-8", cutoff=1)
            steps.append({
                "step": "8 历史时点查询（知识截止于第 1 次捕获）",
                "state": historical["state"],
                "head": historical["head"]["edition"] if historical["head"] else None,
            })
        finally:
            archive.close()
            derived.close()
    return {"operation": "lifecycle_demo", "steps": steps}


def main():
    parser = argparse.ArgumentParser(description="来源响应接收、归档与公告生命周期解释")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    subcommands.add_parser("lifecycle-demo")

    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    collector.add_argument("--if-none-match")
    collector.add_argument("--derived", type=Path)
    collector.add_argument("--scope", choices=("enumeration", "singleton"))

    interpreter = subcommands.add_parser("interpret")
    interpreter.add_argument("--archive", type=Path, required=True)
    interpreter.add_argument("--derived", type=Path, required=True)

    query = subcommands.add_parser("status")
    query.add_argument("--archive", type=Path, required=True)
    query.add_argument("--derived", type=Path, required=True)
    query.add_argument("--authority", required=True)
    query.add_argument("--notice", required=True)
    moment = query.add_mutually_exclusive_group()
    moment.add_argument("--as-of-capture", type=int)
    moment.add_argument("--as-of-time")
    query.add_argument("--explain", action="store_true")

    snapshots = subcommands.add_parser("snapshot")
    snapshot_commands = snapshots.add_subparsers(dest="snapshot_command", required=True)
    save = snapshot_commands.add_parser("save")
    save.add_argument("--archive", type=Path, required=True)
    save.add_argument("--derived", type=Path, required=True)
    save.add_argument("--authority", required=True)
    save.add_argument("--notice", required=True)
    save.add_argument("--purpose", required=True)
    listing = snapshot_commands.add_parser("list")
    listing.add_argument("--derived", type=Path, required=True)
    for name in ("show", "verify"):
        command = snapshot_commands.add_parser(name)
        command.add_argument("--derived", type=Path, required=True)
        command.add_argument("--id", type=int, required=True)
        if name == "verify":
            command.add_argument("--archive", type=Path, required=True)

    marker = subcommands.add_parser("scope")
    marker.add_argument("--derived", type=Path, required=True)
    marker.add_argument("--capture", type=int, required=True)
    marker.add_argument("--scope", required=True, choices=("enumeration", "singleton", "unknown"))
    marker.add_argument("--note")

    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)

    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "lifecycle-demo":
        print(json.dumps(lifecycle_demo(), ensure_ascii=False, indent=2))
    elif args.command == "capture":
        headers = {"If-None-Match": args.if_none_match} if args.if_none_match else None
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path,
                                 headers=headers, derived_path=args.derived, scope=args.scope),
                         ensure_ascii=False, indent=2))
    elif args.command == "interpret":
        print(json.dumps(interpret(args.archive, args.derived), ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(json.dumps(status(args.archive, args.derived, args.authority, args.notice,
                                as_of_capture=args.as_of_capture, as_of_time=args.as_of_time,
                                explain=args.explain),
                         ensure_ascii=False, indent=2))
    elif args.command == "snapshot":
        if args.snapshot_command == "save":
            archive, derived = Archive(args.archive), DerivedStore(args.derived)
            try:
                snapshot_id = derived.save_snapshot(archive, args.authority, args.notice, args.purpose)
                result = {"snapshot_id": snapshot_id, **derived.snapshot(snapshot_id)}
            finally:
                archive.close()
                derived.close()
        elif args.snapshot_command == "list":
            derived = DerivedStore(args.derived)
            try:
                result = {"snapshots": derived.snapshots()}
            finally:
                derived.close()
        elif args.snapshot_command == "show":
            derived = DerivedStore(args.derived)
            try:
                result = derived.snapshot(args.id)
            finally:
                derived.close()
        else:
            archive, derived = Archive(args.archive), DerivedStore(args.derived)
            try:
                result = derived.verify_snapshot(archive, args.id)
            finally:
                archive.close()
                derived.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "scope":
        derived = DerivedStore(args.derived)
        try:
            derived.set_scope(args.capture, args.scope, note=args.note)
            result = {"capture_id": args.capture, "scope": args.scope}
        finally:
            derived.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
