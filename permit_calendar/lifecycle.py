"""公告生命周期：从来源观察到效力判断。

本模块只读取 ``archive.py`` 中只追加的原始采集，不回写、不覆盖任何观察。
所有结论都携带证据（capture_id、source、observed_at、原文），并区分：

* 来源声明时间（published，带日/时/分/秒精度）与采集观察时间（observed_at）；
* “没有见到替代页”（not_seen，开放世界）与“有证据表明不存在”（confirmed_absent）；
* 可判定的先后与精度不足导致的不可判定（同日且互不引用 -> pending_verification）。

冲突是一等输出，模块不会挑选“看起来最新”的页面自行宣布解决。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

PRECISIONS = ("day", "hour", "minute", "second")
STATEMENT_KINDS = ("issued", "withdrawn", "corrected", "absent")
NEGATIVE_STATUSES = (404, 410)

# 效力状态的优先级仅用于汇总，不用于自动解决冲突。
THREAD_STATUSES = ("scheduled", "effective", "withdrawn", "pending_verification", "conflict")


def canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(payload: Any) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def parse_observed_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 时间戳：来源声明时间始终携带精度，同日范围重叠时拒绝编造先后
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stamp:
    value: str
    precision: str
    start: datetime
    end: datetime

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "precision": self.precision,
                "range_start": self.start.isoformat().replace("+00:00", "Z"),
                "range_end": self.end.isoformat().replace("+00:00", "Z")}


def parse_stamp(value: Any, precision: Any) -> Stamp:
    """按来源声明的精度解析时间。

    精度不是展示格式，而是不确定性区间：日级时间戳覆盖整个自然日，
    两个同日的日级时间戳区间重叠，先后关系不可判定。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    if precision not in PRECISIONS:
        raise ValueError("unsupported precision: " + repr(precision))
    text = value.strip()
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    # 声明精度不得细于文本实际给出的粒度，否则就是在伪造精度。
    granularity = _granularity(text)
    if PRECISIONS.index(precision) < PRECISIONS.index(granularity):
        raise ValueError(f"declared precision {precision} is finer than text granularity {granularity} "
                         "(cannot manufacture precision the source did not state)")
    if precision == "day":
        start = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif precision == "hour":
        start = parsed.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
    elif precision == "minute":
        start = parsed.replace(second=0, microsecond=0)
        end = start + timedelta(minutes=1)
    else:
        start = parsed.replace(microsecond=0)
        end = start + timedelta(seconds=1)
    return Stamp(value=text, precision=precision, start=start, end=end)


def _granularity(text: str) -> str:
    if "T" not in text and " " not in text:
        return "day"
    time_part = text.split("T", 1)[1] if "T" in text else text.split(" ", 1)[1]
    time_part = time_part.split("Z", 1)[0].split("+", 1)[0].split("-", 1)[0]
    parts = [part for part in time_part.split(":") if part]
    if len(parts) >= 3:
        return "second"
    if len(parts) == 2:
        return "minute"
    return "hour"


def compare_stamps(left: Stamp | None, right: Stamp | None) -> int | None:
    """返回 -1/0/1；区间重叠或缺位时返回 None 表示不可判定。"""
    if left is None or right is None:
        return None
    if left.start == right.start and left.end == right.end:
        return 0
    if left.end <= right.start:
        return -1
    if right.end <= left.start:
        return 1
    return None


def stamp_is_past(stamp: Stamp | None, now: Stamp) -> bool | None:
    """声明时间是否已到；边界落在精度区间内时返回 None。"""
    if stamp is None:
        return None
    if stamp.end <= now.start:
        return True
    if stamp.start >= now.end:
        return False
    return None


# ---------------------------------------------------------------------------
# 观察抽取：记录 -> 声明（statement），保留原文与双时间轴
# ---------------------------------------------------------------------------

def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("cites must be a string or a list of strings")


def extract_statements(records: Iterable[dict[str, Any]], source: str,
                       capture_id: int, observed_at: str, url: str) -> dict[str, list]:
    """把一次成功采集解出的来源记录规范化为声明。

    异常记录进入 rejections，整批不失败；任何声明都回指 capture_id，
    原文（raw）与来源声明时间一并保留。
    """
    statements: list[dict] = []
    rejections: list[dict] = []
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            rejections.append({"capture_id": capture_id, "position": position,
                               "reason": "record is not an object", "raw": record})
            continue
        try:
            authority = str(record["authority"]).strip()
            notice_id = str(record["notice_id"]).strip()
            edition = str(record["edition"]).strip()
            kind = str(record["kind"]).strip()
            if not authority or not notice_id or not edition:
                raise ValueError("authority, notice_id and edition are required")
            if kind not in STATEMENT_KINDS:
                raise ValueError("unknown kind: " + kind)
            published = None
            if record.get("published") is not None:
                published = parse_stamp(record["published"], record.get("precision", "day"))
            start_stamp = None
            if record.get("start") is not None:
                start_stamp = parse_stamp(record["start"], record.get("start_precision",
                                                                       record.get("precision", "day")))
            end_stamp = None
            if record.get("end") is not None:
                end_stamp = parse_stamp(record["end"], record.get("end_precision",
                                                                  record.get("precision", "day")))
            replaces = record.get("replaces")
            if replaces is not None:
                replaces = str(replaces).strip() or None
            statement = {
                "statement_id": digest([capture_id, source, authority, notice_id, edition, record]),
                "capture_id": capture_id,
                "source": source,
                "url": url,
                "observed_at": observed_at,
                "authority": authority,
                "notice_id": notice_id,
                "edition": edition,
                "kind": kind,
                "replaces": replaces,
                "cites": _as_list(record.get("cites")),
                "published": published,
                "effective_start": start_stamp,
                "effective_end": end_stamp,
                "text": record.get("text"),
                "scope": record.get("scope"),
                "raw": record,
            }
        except (KeyError, TypeError, ValueError) as error:
            rejections.append({"capture_id": capture_id, "position": position,
                               "reason": str(error), "raw": record})
            continue
        statements.append(statement)
    return {"statements": statements, "rejections": rejections}


def probe_from_capture(capture: dict[str, Any]) -> dict[str, Any]:
    """把一次采集本身转成覆盖度探针。

    传输层失败不会进入归档，因此这里只看得到真实收到的响应：
    200/304 是“已观察该入口”，404/410 是来源对该 URL 的否定性声明，
    其余状态码不构成否定证据，只能说明这次没拿到。
    """
    status = capture["status"]
    if status in (200, 304):
        kind = "feed_observed"
        negative = False
    elif status in NEGATIVE_STATUSES:
        kind = "origin_negative"
        negative = True
    else:
        kind = "transport_error"
        negative = False
    return {"capture_id": capture["id"], "source": capture["source"], "url": capture["url"],
            "status": status, "observed_at": capture["received_at"],
            "kind": kind, "is_negative_evidence": negative}


def load_observations(archive) -> dict[str, Any]:
    """扫描只追加归档，产出全部声明、拒绝记录与覆盖度探针。"""
    from .decoding import records as decode_records

    statements: list[dict] = []
    rejections: list[dict] = []
    probes: list[dict] = []
    for capture in archive.captures():
        probes.append(probe_from_capture(capture))
        if capture["status"] != 200:
            continue
        body = archive.body(capture["id"])
        content_type = capture["headers"].get("content-type", "")
        try:
            decoded = decode_records(body, content_type)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            # 解析失败不是“空来源”，留作探针而不是否定证据。
            probes[-1]["kind"] = "parse_failure"
            continue
        batch = extract_statements(decoded, capture["source"], capture["id"],
                                   capture["received_at"], capture["url"])
        statements.extend(batch["statements"])
        rejections.extend(batch["rejections"])
    return {"statements": statements, "rejections": rejections, "probes": probes}


# ---------------------------------------------------------------------------
# 效力判断
# ---------------------------------------------------------------------------

def _summary(statement: dict[str, Any]) -> dict[str, Any]:
    return {
        "statement_id": statement["statement_id"],
        "capture_id": statement["capture_id"],
        "source": statement["source"],
        "url": statement["url"],
        "observed_at": statement["observed_at"],
        "edition": statement["edition"],
        "kind": statement["kind"],
        "replaces": statement["replaces"],
        "cites": statement["cites"],
        "published": statement["published"].as_dict() if statement["published"] else None,
        "effective_start": statement["effective_start"].as_dict() if statement["effective_start"] else None,
        "text": statement["text"],
        "scope": statement["scope"],
        "raw": statement["raw"],
    }


def _conflict(code: str, message: str, statements: list[dict]) -> dict[str, Any]:
    return {"code": code, "message": message, "statements": [_summary(item) for item in statements]}


def _components(statements: list[dict]) -> list[set[str]]:
    edges = {item["edition"]: item["replaces"] for item in statements if item["replaces"]}
    nodes = {item["edition"] for item in statements}
    components: list[set[str]] = []
    for node in nodes:
        seen: set[str] = set()
        current = node
        while current is not None and current not in seen:
            seen.add(current)
            current = edges.get(current)
        joined = None
        for index, component in enumerate(components):
            if seen & component:
                joined = index
                break
        if joined is None:
            components.append(seen)
        else:
            components[joined] |= seen
    return components


def derive_thread(thread_statements: list[dict], identity: tuple[str, str], *,
                  business_time: Stamp, observed_by: datetime | None = None,
                  probes: list[dict] | None = None) -> dict[str, Any]:
    """对同一 (authority, notice_id) 的全部声明做效力判断。"""
    authority, notice_id = identity
    if observed_by is not None:
        thread_statements = [item for item in thread_statements
                             if parse_observed_at(item["observed_at"]) <= observed_by]
    conflicts: list[dict] = []
    unresolved_edges: list[dict] = []
    reobservations: list[dict] = []

    # absent 声明是覆盖度证据，不参与修订链与分支判定。
    absent_statements = [item for item in thread_statements if item["kind"] == "absent"]
    lifecycle_statements = [item for item in thread_statements if item["kind"] != "absent"]

    # 1) 同一 edition 标识：同载荷是重复观察，异载荷是身份冲突。
    by_edition: dict[str, list[dict]] = {}
    for item in lifecycle_statements:
        by_edition.setdefault(item["edition"], []).append(item)
    nodes: dict[str, dict] = {}
    for edition, group in by_edition.items():
        payloads = {digest(item["raw"]) for item in group}
        if len(payloads) > 1:
            conflicts.append(_conflict(
                "edition_identity_conflict",
                f"edition {edition} 存在互相矛盾的来源原文，无法视为同一份公告",
                group))
        nodes[edition] = group[0]
        if len(group) > 1:
            reobservations.append({"edition": edition,
                                   "occurrences": [_summary(item) for item in group]})

    # 2) 引用边：悬空与环。
    edges = {edition: item["replaces"] for edition, item in nodes.items() if item["replaces"]}
    for edition, target in edges.items():
        if target not in nodes:
            unresolved_edges.append({"from_edition": edition, "target_edition": target,
                                     "reason": "target_not_observed",
                                     "note": "被引用的公告未在归档中观察到，不能假设其存在或内容"})
    for edition in nodes:
        seen: set[str] = set()
        current = edition
        while current is not None and current not in seen:
            seen.add(current)
            current = edges.get(current) if current in nodes else None
        if current is not None:
            conflicts.append(_conflict(
                "reference_cycle", f"replaces 引用在 edition {current} 处成环",
                [nodes[node] for node in seen]))

    # 3) 多分支：同一 edition 被两个后继引用。
    incoming: dict[str, list[str]] = {}
    for edition, target in edges.items():
        incoming.setdefault(target, []).append(edition)
    for target, successors in incoming.items():
        if len(set(successors)) > 1:
            conflicts.append(_conflict(
                "multiple_successors",
                f"edition {target} 被 {len(set(successors))} 份公告同时替代，替代关系不唯一",
                [nodes[item] for item in successors]))

    # 4) 互不引用的并行分支。
    #    声明时间精度不足以排序（含同日）-> 待核；可判定先后时再结合
    #    观察时间识别“已撤回后晚到的旧页面”，迟到数据不得自动恢复效力。
    components = _components(list(nodes.values()))
    roots = [next(iter(component - set(edges.keys())), None) or sorted(component)[0]
             for component in components]
    if len(components) > 1:
        # 对每对并行分支，取各自链头（分支内最新版本）比较；
        # 链头沿着 replaces 边走到没有后继为止。
        def head_of(component: set[str]) -> str:
            current = sorted(component)[0]
            while True:
                successors = [edition for edition, target in edges.items()
                              if target == current and edition in component]
                if not successors:
                    return current
                current = successors[0]

        heads = [head_of(component) for component in components]
        for index, left_head in enumerate(heads):
            for right_head in heads[index + 1:]:
                left, right = nodes[left_head], nodes[right_head]
                ordering = compare_stamps(left["published"], right["published"])
                # None 表示精度区间互相重叠（含同一自然日的日级时间戳），
                # 排序证据不足，保持待核；完全相同区间（0）同样不可排序。
                if ordering is None or ordering == 0:
                    conflicts.append(_conflict(
                        "unlinked_same_day",
                        f"edition {left_head} 与 {right_head} 互不引用且声明时间精度不足以排序，保持待核",
                        [left, right]))
                    continue
                earlier, later = (left, right) if ordering < 0 else (right, left)
                earlier_component = next(component for component in components
                                         if earlier["edition"] in component)
                later_component = next(component for component in components
                                       if later["edition"] in component)
                # 晚到分支的最早观察仍晚于在先分支的最晚观察：
                # 这正是“先观察到撤回、之后搜索缓存才送来旧页面”的形态。
                earlier_observed = max(parse_observed_at(nodes[edition]["observed_at"])
                                       for edition in earlier_component)
                later_observed = min(parse_observed_at(nodes[edition]["observed_at"])
                                     for edition in later_component)
                observed_later = later_observed > earlier_observed
                if later["kind"] == "withdrawn":
                    code, message = "unlinked_withdrawal", "撤回声明没有引用任何在案版本，撤回范围无法确认"
                elif earlier["kind"] == "withdrawn" and observed_later:
                    code, message = "late_unlinked_issuance", (
                        "明确撤回之后才观察到、且未引用撤回件的发布：搜索缓存的迟到数据"
                        "不能让许可重新生效；只有引用撤回件的真正重发才能恢复效力")
                elif earlier["kind"] == "withdrawn":
                    code, message = "parallel_issuance", "撤回后出现互不引用的发布，无法确认其与撤回的关系"
                else:
                    code, message = "parallel_issuance", "同一公告编号下存在互不引用的并行发布，无法确定有效版本"
                conflicts.append(_conflict(code, message, [earlier, later]))

    # 5) 单链效力（无阻断性冲突时才给出业务状态）。
    chain: list[dict] = []
    status = "pending_verification"
    head_edition = None
    if not conflicts:
        chain_editions: list[str] = []
        if roots:
            current = roots[0]
            while current is not None and current in nodes and current not in chain_editions:
                chain_editions.append(current)
                successors = [edition for edition, target in edges.items()
                              if target == current and edition in nodes]
                current = successors[0] if successors else None
        chain = [_summary(nodes[edition]) for edition in chain_editions]
        head_edition = chain_editions[-1] if chain_editions else None
        head = nodes[head_edition] if head_edition else None
        if unresolved_edges or head is None:
            status = "pending_verification"
        else:
            past = stamp_is_past(head["published"], business_time)
            if past is not True:
                status = "pending_verification"
            elif head["kind"] == "withdrawn":
                status = "withdrawn"
            elif head["kind"] == "issued":
                start_past = stamp_is_past(head["effective_start"], business_time) \
                    if head["effective_start"] else True
                if start_past is False:
                    status = "scheduled"
                elif start_past is None:
                    status = "pending_verification"
                else:
                    status = "effective"
            else:  # corrected 不单独决定效力，继承前一版本判断
                predecessor = edges.get(head_edition)
                prev = nodes.get(predecessor) if predecessor else None
                status = "withdrawn" if prev and prev["kind"] == "withdrawn" else "effective"
    else:
        status = "conflict"
    # 同日且互不引用属于“无法判定先后”的待核情形，不是证据矛盾：
    # 仅当全部未决原因都是这一类时，线程才保持 pending_verification；
    # 若同时存在撤回后晚到等阻断性冲突，线程仍是 conflict。
    undecidable_same_day = any(item["code"] == "unlinked_same_day" for item in conflicts)
    hard_conflicts = [item for item in conflicts if item["code"] != "unlinked_same_day"]
    if undecidable_same_day and not hard_conflicts:
        status = "pending_verification"
    elif hard_conflicts:
        status = "conflict"

    # 6) 否定证据：absent 声明按 scope 归入本编号；覆盖度探针由视图层合并。
    negative_evidence = [_summary(item) for item in absent_statements]

    return {
        "authority": authority,
        "notice_id": notice_id,
        "status": status,
        "head_edition": head_edition,
        "chain": chain,
        "parallel_branches": [sorted(component) for component in components] if len(components) > 1 else [],
        "conflicts": conflicts,
        "unresolved_edges": unresolved_edges,
        "reobservations": reobservations,
        "negative_evidence": negative_evidence,
        "bounds": {
            "business_time": business_time.as_dict(),
            "observed_by": observed_by.isoformat().replace("+00:00", "Z") if observed_by else None,
            "precision_preserved": True,
            "probes_considered": [probe["capture_id"] for probe in (probes or [])],
        },
    }


def replacement_status(thread: dict[str, Any], edition: str) -> dict[str, Any]:
    """区分“未见到替代页”和“确实不存在替代页”。"""
    for node in thread["chain"]:
        if node["replaces"] == edition:
            return {"status": "observed", "replacement": node,
                    "note": "替代页已观察到，关系来自来源原文引用而非抓取顺序"}
    covering = [item for item in thread["negative_evidence"]
                if item.get("scope") in (edition, "replacement", "notice")]
    if covering:
        return {"status": "confirmed_absent", "evidence": covering,
                "note": "来源以明确 absent 声明覆盖该范围，结论仅在声明范围内成立"}
    return {"status": "not_seen",
            "note": "归档中没有观察到替代页；开放世界下“未见到”不等于“不存在”，需继续观察"}


def build_view(observations: dict[str, Any], *, business_time: datetime | None = None,
               observed_by: datetime | None = None) -> dict[str, Any]:
    """汇总当前时点或历史时点的效力视图。"""
    if business_time is None:
        business_time = datetime.now(timezone.utc)
    business_stamp = parse_stamp(business_time.strftime("%Y-%m-%dT%H:%M:%S"), "second")
    statements = observations["statements"]
    if observed_by is not None:
        statements = [item for item in statements
                      if parse_observed_at(item["observed_at"]) <= observed_by]
    probes = [probe for probe in observations["probes"]
              if observed_by is None or parse_observed_at(probe["observed_at"]) <= observed_by]

    grouped: dict[tuple[str, str], list[dict]] = {}
    for item in statements:
        grouped.setdefault((item["authority"], item["notice_id"]), []).append(item)
    threads = [derive_thread(bucket, identity, business_time=business_stamp,
                             observed_by=observed_by, probes=probes)
               for identity, bucket in sorted(grouped.items())]

    feed_probes = [probe for probe in probes if probe["kind"] == "feed_observed"]
    return {
        "business_time": business_stamp.as_dict(),
        "observed_by": observed_by.isoformat().replace("+00:00", "Z") if observed_by else None,
        "coverage": {
            "last_feed_observed_at": max((item["observed_at"] for item in feed_probes), default=None),
            "probe_count": len(probes),
            "negative_probe_count": sum(1 for item in probes if item["is_negative_evidence"]),
            "open_world": True,
        },
        "threads": threads,
        "probes": probes,
        "rejections": observations["rejections"],
    }


def view_fingerprint(view: dict[str, Any]) -> str:
    """对不含生成时刻的视图内容取摘要，供留档复核。"""
    payload = {key: value for key, value in view.items() if key != "generated_at"}
    return digest(payload)
