import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .archive import Archive
from .client import Client, collect
from .decoding import records
from .lifecycle import build_view, load_observations, parse_observed_at, replacement_status
from .ledger import Ledger
from .origin import serve


def capture(base_url, archive_path, source, path):
    archive = Archive(archive_path)
    try:
        capture_id, response = collect(Client(base_url), archive, source, path)
        result = {"capture_id": capture_id, "status": response.status, "bytes": len(response.body), "url": response.url}
        if response.status == 200:
            result["source_records"] = records(response.body, response.headers.get("content-type", ""))
        result["archived_observations"] = len(archive.captures())
        return result
    finally:
        archive.close()


def _parse_instant(text):
    if text is None:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def derive(archive_path, business_time=None, observed_by=None, authority=None, notice_id=None):
    archive = Archive(archive_path)
    try:
        observations = load_observations(archive)
        view = build_view(observations, business_time=business_time, observed_by=observed_by)
        if authority is not None or notice_id is not None:
            view["threads"] = [
                thread for thread in view["threads"]
                if (authority is None or thread["authority"] == authority)
                and (notice_id is None or thread["notice_id"] == notice_id)
            ]
        return view, observations
    finally:
        archive.close()


def main():
    parser = argparse.ArgumentParser(description="来源响应接收、效力判断与进场依据留档")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("demo")
    collector = subcommands.add_parser("capture")
    collector.add_argument("--base-url", required=True)
    collector.add_argument("--archive", type=Path, required=True)
    collector.add_argument("--source", required=True)
    collector.add_argument("--path", default="/feed")
    server = subcommands.add_parser("serve")
    server.add_argument("--scenario", type=Path)

    derive_parser = subcommands.add_parser("derive", help="按当前或历史时点派生公告效力视图")
    derive_parser.add_argument("--archive", type=Path, required=True)
    derive_parser.add_argument("--business-time", help="业务时点（ISO 8601），默认当前时刻")
    derive_parser.add_argument("--observed-by", help="历史回放：只使用该时刻之前进入归档的观察")
    derive_parser.add_argument("--authority")
    derive_parser.add_argument("--notice-id")
    derive_parser.add_argument("--replacement-of", help="查询某 edition 的替代页存在性")

    snapshot = subcommands.add_parser("snapshot", help="进场依据留档（只追加）")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_command", required=True)
    save = snapshot_sub.add_parser("save")
    save.add_argument("--archive", type=Path, required=True)
    save.add_argument("--ledger", type=Path, required=True)
    save.add_argument("--purpose", required=True, help="留档用途，例如：2026-11-15 进场依据")
    save.add_argument("--business-time")
    save.add_argument("--observed-by")
    listing = snapshot_sub.add_parser("list")
    listing.add_argument("--ledger", type=Path, required=True)
    get = snapshot_sub.add_parser("get")
    get.add_argument("--ledger", type=Path, required=True)
    get.add_argument("--snapshot-id", type=int, required=True)
    verify = snapshot_sub.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--ledger", type=Path, required=True)
    verify.add_argument("--snapshot-id", type=int, required=True)

    args = parser.parse_args()
    if args.command == "demo":
        with tempfile.TemporaryDirectory() as temporary, serve() as origin:
            result = capture(origin.base_url, Path(temporary) / "archive.sqlite3", "local-source", "/feed")
            print(json.dumps({"operation": "raw_capture", **result}, ensure_ascii=False, indent=2))
    elif args.command == "capture":
        print(json.dumps(capture(args.base_url, args.archive, args.source, args.path),
                         ensure_ascii=False, indent=2))
    elif args.command == "derive":
        view, observations = derive(
            args.archive, _parse_instant(args.business_time),
            _parse_instant(args.observed_by), args.authority, args.notice_id)
        if args.replacement_of is not None:
            answers = []
            for thread in view["threads"]:
                answers.append({"authority": thread["authority"], "notice_id": thread["notice_id"],
                                **replacement_status(thread, args.replacement_of)})
            print(json.dumps({"replacement_of": args.replacement_of, "results": answers},
                             ensure_ascii=False, indent=2))
        else:
            print(json.dumps(view, ensure_ascii=False, indent=2))
    elif args.command == "snapshot":
        if args.snapshot_command == "save":
            view, observations = derive(
                args.archive, _parse_instant(args.business_time),
                _parse_instant(args.observed_by))
            capture_ids = sorted({probe["capture_id"] for probe in observations["probes"]})
            ledger = Ledger(args.ledger)
            try:
                record = ledger.save(view, args.purpose, capture_ids)
            finally:
                ledger.close()
            print(json.dumps({"operation": "snapshot_saved", **record},
                             ensure_ascii=False, indent=2))
        else:
            ledger = Ledger(args.ledger)
            try:
                if args.snapshot_command == "list":
                    print(json.dumps(ledger.list_snapshots(), ensure_ascii=False, indent=2))
                elif args.snapshot_command == "get":
                    print(json.dumps(ledger.get(args.snapshot_id), ensure_ascii=False, indent=2))
                else:
                    archive = Archive(args.archive)
                    try:
                        print(json.dumps(ledger.verify(args.snapshot_id, archive),
                                         ensure_ascii=False, indent=2))
                    finally:
                        archive.close()
            finally:
                ledger.close()
    else:
        from .origin import load_scenario
        with serve(load_scenario(args.scenario)) as origin:
            print(json.dumps({"base_url": origin.base_url}, ensure_ascii=False), flush=True)
            try:
                import threading
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
