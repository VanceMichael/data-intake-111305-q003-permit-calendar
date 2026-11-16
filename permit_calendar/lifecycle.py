"""公告生命周期：从来源声明到效力判断的纯函数层。

本模块不做任何 I/O，判断结果只取决于输入的声明集合与覆盖口径，
因此同一知识截止下重算结果确定不变（留档可校验）。

排序原则：公告之间的先后只来自原文中的更正/替代引用（replaces）。
抓取顺序、来源声明的发布日期都不参与排序；日级精度的同日公告
若互不引用，保持待核，由冲突清单展示各方原文。
"""

import json

IDENTITY_FIELDS = ("authority", "notice_id", "edition")

# 来源声明的 kind 只按原样保存；这里仅给出极性解释，未知种类不猜。
POSITIVE_KINDS = {"issued", "reissued", "corrected"}
NEGATIVE_KINDS = {"withdrawn"}


def polarity(kind):
    if kind in POSITIVE_KINDS:
        return "positive"
    if kind in NEGATIVE_KINDS:
        return "negative"
    return "unknown"


def statement_from_record(record, capture_id, item_index, observed_at):
    """把一条来源记录整理为声明。身份字段缺失时拒绝，其余字段原样保留。"""
    if not isinstance(record, dict):
        raise ValueError("source record must be an object")
    statement = {"capture_id": capture_id, "item_index": item_index, "observed_at": observed_at}
    for field in IDENTITY_FIELDS:
        value = record.get(field)
        if value is None or not str(value).strip():
            raise ValueError("identity field missing: " + field)
        statement[field] = str(value).strip()
    for field in ("kind", "replaces", "published", "precision"):
        value = record.get(field)
        statement[field] = None if value is None else str(value)
    statement["payload"] = record
    return statement


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _sid(statement):
    """声明的稳定标识：有数据库主键用主键，否则用 捕获号:序号。"""
    if statement.get("id") is not None:
        return statement["id"]
    return "%s:%s" % (statement["capture_id"], statement["item_index"])


def _shared(values):
    """所有观察一致的字段取值；不一致返回 None 并交由漂移冲突说明。"""
    return values.pop() if len(values) == 1 else None


def assemble_editions(statements):
    """按版本（edition）归并声明，返回版本表与版本级冲突。"""
    editions = {}
    for statement in sorted(statements, key=lambda s: (s["capture_id"], s["item_index"])):
        edition = editions.setdefault(statement["edition"], {"edition": statement["edition"], "sightings": []})
        edition["sightings"].append(statement)
    conflicts = []
    for name, edition in editions.items():
        sightings = edition["sightings"]
        edition["kind"] = _shared({s["kind"] for s in sightings})
        edition["replaces"] = _shared({s["replaces"] for s in sightings})
        edition["published"] = _shared({s["published"] for s in sightings})
        edition["precision"] = _shared({s["precision"] for s in sightings})
        edition["first_seen_capture"] = min(s["capture_id"] for s in sightings)
        if len({_canonical(s["payload"]) for s in sightings}) > 1:
            edition["drifted"] = True
            conflicts.append({
                "type": "content_drift",
                "detail": "同一版本标识的多次观察内容不一致",
                "statements": [_sid(s) for s in sightings],
                "edition": name,
            })
        else:
            edition["drifted"] = False
    return editions, conflicts


def _chain_from(head, editions):
    """从头部沿 replaces 向回走，返回（旧→新链，是否成环）。"""
    chain, seen, node = [], set(), head
    while node is not None and node not in seen:
        seen.add(node)
        chain.append(node)
        replaces = editions[node]["replaces"]
        node = replaces if replaces in editions else None
    return [editions[name] for name in reversed(chain)], node is not None


def _summary(edition):
    return {
        "edition": edition["edition"],
        "kind": edition["kind"],
        "replaces": edition["replaces"],
        "published": edition["published"],
        "precision": edition["precision"],
        "first_seen_capture": edition["first_seen_capture"],
        "sightings": [_sid(s) for s in edition["sightings"]],
    }


def judge(authority, notice_id, statements, enumerations=(), as_of_capture=None):
    """对一个（发布机关, 公告编号）在给定知识截止下的效力作出判断。

    statements: 该组的全部声明（已按截止过滤）。
    enumerations: 覆盖口径观察，每项 {"capture_id", "editions", "confirmations"}，
        editions 为该次完整列表中本组出现的版本集合，confirmations 为内容
        未变的后续确认（如 304）的 capture_id 列表。
    """
    judgment = {
        "authority": authority,
        "notice_id": notice_id,
        "as_of_capture": as_of_capture,
        "state": None,
        "head": None,
        "chain": [],
        "conflicts": [],
        "hints": [],
        "coverage": {"replacement": "not_observed", "basis_captures": [], "horizon_capture": None},
    }
    if not statements:
        judgment["state"] = "unobserved"
        absent = [e["capture_id"] for e in enumerations]
        if absent:
            judgment["coverage"] = {
                "replacement": "absent_as_of",
                "basis_captures": absent,
                "horizon_capture": max(absent),
                "detail": "完整列表观察中未出现该公告",
            }
        return judgment

    editions, conflicts = assemble_editions(statements)
    replaced = {e["replaces"] for e in editions.values() if e["replaces"]}
    heads = sorted(name for name in editions if name not in replaced)

    for name, edition in editions.items():
        if edition["replaces"] and edition["replaces"] not in editions:
            conflicts.append({
                "type": "dangling_reference",
                "severity": "notice",
                "detail": "引用的版本 %s 尚未观察到原文" % edition["replaces"],
                "edition": name,
                "statements": [_sid(s) for s in edition["sightings"]],
            })

    if not heads:
        conflicts.append({
            "type": "reference_cycle",
            "severity": "blocking",
            "detail": "更正引用构成环，无法确定头部",
            "statements": [_sid(s) for e in editions.values() for s in e["sightings"]],
        })
    elif len(heads) > 1:
        head_chains = {}
        for name in heads:
            chain, _ = _chain_from(name, editions)
            head_chains[name] = [_summary(e) for e in chain]
        conflicts.append({
            "type": "competing_heads",
            "severity": "blocking",
            "detail": "多份公告互不引用，先后关系不能凭抓取顺序推断",
            "heads": head_chains,
            "statements": [_sid(s) for name in heads for s in editions[name]["sightings"]],
        })
        declared = [editions[name]["published"] for name in heads]
        if len(set(declared)) > 1:
            judgment["hints"].append({
                "type": "declared_dates_differ",
                "detail": "来源声明的发布日期不相同，但日期不参与排序，仅供参考",
                "values": declared,
            })

    blocking = False
    for conflict in conflicts:
        conflict.setdefault("severity", "blocking")
        if conflict["type"] == "content_drift":
            conflict["severity"] = "blocking" if conflict["edition"] in heads else "notice"
        blocking = blocking or conflict["severity"] == "blocking"

    if blocking:
        judgment["state"] = "pending"
        judgment["conflicts"] = conflicts
        return judgment

    head = editions[heads[0]]
    chain, cycle = _chain_from(heads[0], editions)
    if cycle:
        conflicts.append({
            "type": "reference_cycle",
            "severity": "blocking",
            "detail": "更正引用构成环，无法确定头部",
            "statements": [_sid(s) for e in chain for s in e["sightings"]],
        })
        judgment["state"] = "pending"
        judgment["conflicts"] = conflicts
        return judgment

    head_polarity = polarity(head["kind"])
    if head_polarity == "unknown":
        conflicts.append({
            "type": "unrecognized_kind",
            "severity": "blocking",
            "detail": "头部版本种类 %r 不在已知范围内，不猜测其效力" % head["kind"],
            "statements": [_sid(s) for s in head["sightings"]],
        })
        judgment["state"] = "pending"
        judgment["conflicts"] = conflicts
        return judgment

    judgment["state"] = "effective" if head_polarity == "positive" else "withdrawn"
    judgment["head"] = _summary(head)
    judgment["chain"] = [_summary(e) for e in chain]
    judgment["conflicts"] = conflicts

    basis = [e for e in enumerations if heads[0] in e["editions"]]
    if basis:
        latest = max(basis, key=lambda e: e["capture_id"])
        horizon = max([latest["capture_id"], *latest.get("confirmations", [])])
        judgment["coverage"] = {
            "replacement": "absent_as_of",
            "basis_captures": [latest["capture_id"]],
            "horizon_capture": horizon,
        }
    return judgment
