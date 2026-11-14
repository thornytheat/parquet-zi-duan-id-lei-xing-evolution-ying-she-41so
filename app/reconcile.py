"""跨表对账（纯函数）：按读取任务的关联键与投影，分别对账新旧 schema 下的解析结果。

单表对比只回答「这一张表变得兼不兼容」；批次验收要回答「这些表合在一起，
下游读取任务按旧口径还能不能跑出一样的结果」。因此对账分两条线：

1. 关联键对账：新旧 schema 下，关联两侧键列各自解析到哪一列、什么类型、
   还能不能关联上（错列 / 键丢失 / 类型不再可关联）；
2. 投影对账：每个输出列在新 schema 下是否还在、类型是否还可读。

所有差异逐项定位到 任务 → 关联/投影 → 表.列，并给出严重度：
block（阻断，整批不可发布）/ confirm（需人工确认）/ warn（需下游同步）/ info（提示）。
"""
from __future__ import annotations

from . import report
from .matching import find_by_old_name, match_fields
from .types import promotion

SEVERITY_LABELS = {"block": "阻断", "confirm": "需确认", "warn": "需同步", "info": "提示"}
DIFF_ITEM_LABELS = {"join_key": "关联键", "projection": "投影"}
DIFF_CODE_PREFIX = {"join_key": "key", "projection": "projection"}
JOINABLE_LABELS = {"matched": "类型一致", "matched_with_cast": "类型不同但可无损提升", "mismatch": "类型不可关联"}
RESOLVE_STATUS_LABELS = {
    "ok": "正常",
    "renamed": "已改名",
    "removed": "已删除",
    "id_changed": "字段 ID 变更",
}
VERDICT_LABELS = {"pass": "通过", "warn": "有需同步项", "confirm": "待人工确认", "blocked": "有阻断差异"}
TASK_STATUS_LABELS = {"pending": "待执行", "done": "已完成", "failed": "已失败（隔离）"}


class TaskFailed(ValueError):
    """读取任务定义与批次输入不符；该任务被隔离，不影响批次内其他任务。"""


def joinable(type_a: str, type_b: str) -> str:
    """两类型作为关联键是否可关联：任一方向可无损提升（窄→宽）即可关联。"""
    if type_a == type_b:
        return "matched"
    if promotion(type_a, type_b)["verdict"] == "compatible":
        return "matched_with_cast"
    if promotion(type_b, type_a)["verdict"] == "compatible":
        return "matched_with_cast"
    return "mismatch"


def resolve_column(table_name: str, old_name: str, pairs: list[dict], new_schema: dict) -> dict:
    """旧口径的一列，在新 schema 下解析成什么（改名/删除/ID 变更/类型结论/名字被占用）。"""
    pair = find_by_old_name(pairs, old_name)
    if pair is None:
        raise TaskFailed(f"列 {table_name}.{old_name} 在旧 schema 中不存在，任务定义与批次输入不符")
    resolved = {
        "table": table_name,
        "old_name": old_name,
        "old_type": pair["old_type"],
        "kind": pair["kind"],
        "new_name": pair["new_name"],
        "new_type": pair["new_type"],
        "status": "ok",
        "status_label": "正常",
        "reoccupied": False,
        "occupied_by_id": None,
        "type_verdict": None,
        "type_reason": None,
    }
    kind = pair["kind"]
    if kind == "removed":
        resolved.update(status="removed", status_label=RESOLVE_STATUS_LABELS["removed"])
        return resolved
    if kind == "id_changed":
        resolved.update(status="id_changed", status_label=RESOLVE_STATUS_LABELS["id_changed"])
        return resolved
    if kind == "renamed":
        resolved.update(status="renamed", status_label=RESOLVE_STATUS_LABELS["renamed"])
        # 旧列名是否被另一个新列占用：占用会导致按旧名关联/投影命中错列
        for field in new_schema["fields"]:
            if field["name"] == old_name:
                resolved["reoccupied"] = True
                resolved["occupied_by_id"] = field.get("id")
                break
    if pair.get("type_changed"):
        resolved["type_verdict"] = pair["type_result"]["verdict"]
        resolved["type_reason"] = pair["type_result"]["reason"]
    return resolved


def evaluate_task(task: dict, tables: dict[str, dict], matches: dict[str, dict]) -> dict:
    """对单个读取任务做新旧对账；任务定义非法抛 TaskFailed（由编排层隔离）。"""
    diffs: list[dict] = []

    def add_diff(item: str, severity: str, code: str, table, column, message: str, location: str) -> None:
        diffs.append({
            "task": task["task"],
            "item": item,
            "item_label": DIFF_ITEM_LABELS[item],
            "code": code,
            "severity": severity,
            "severity_label": SEVERITY_LABELS[severity],
            "table": table,
            "column": column,
            "location": location,
            "message": message,
        })

    def check_type_change(item: str, res: dict, location: str) -> None:
        verdict = res.get("type_verdict")
        if verdict is None:
            return
        prefix = DIFF_CODE_PREFIX[item]
        where = f"{location}（{res['table']}.{res['old_name']}）"
        if verdict == "blocked":
            add_diff(item, "block", f"{prefix}_type_blocked", res["table"], res["old_name"],
                     f"{where}：类型 {res['old_type']}→{res['new_type']} 被矩阵阻断：{res['type_reason']}", location)
        elif verdict == "unknown":
            add_diff(item, "confirm", f"{prefix}_type_unknown", res["table"], res["old_name"],
                     f"{where}：类型 {res['old_type']}→{res['new_type']} 不在矩阵内，需人工确认", location)
        else:
            add_diff(item, "info", f"{prefix}_type_changed", res["table"], res["old_name"],
                     f"{where}：类型 {res['old_type']}→{res['new_type']} 兼容（{res['type_reason']}）", location)

    def check_resolution(item: str, res: dict, location: str, noun: str) -> None:
        prefix = DIFF_CODE_PREFIX[item]
        where = f"{location}（{res['table']}.{res['old_name']}）"
        if res["status"] == "removed":
            add_diff(item, "block", f"{prefix}_removed", res["table"], res["old_name"],
                     f"{where}：{noun}在新 schema 中被删除，按旧口径读取将缺列 → 数据丢失", location)
        elif res["status"] == "id_changed":
            add_diff(item, "confirm", f"{prefix}_id_changed", res["table"], res["old_name"],
                     f"{where}：{noun}的 field_id 发生变化，需人工确认是否同一列", location)
        elif res["status"] == "renamed":
            if res["reoccupied"]:
                add_diff(item, "block", f"{prefix}_name_reoccupied", res["table"], res["old_name"],
                         f"{where}：原列已改名为 {res['new_name']}，而旧列名 {res['old_name']} 被新列"
                         f"（field_id {res['occupied_by_id']}）占用；按旧名读取会命中错列 → 关联错列/取错数据",
                         location)
            else:
                add_diff(item, "warn", f"{prefix}_renamed", res["table"], res["old_name"],
                         f"{where}：{noun}改名为 {res['new_name']}（field_id 不变），按列名的 SQL/视图需同步",
                         location)

    join_checks: list[dict] = []
    for index, join in enumerate(task["joins"], start=1):
        location = f"关联#{index} {join['left']}.{join['left_key']} = {join['right']}.{join['right_key']}"
        left = tables.get(join["left"])
        right = tables.get(join["right"])
        if left is None or right is None:
            missing = join["left"] if left is None else join["right"]
            raise TaskFailed(f"{location}：表 {missing} 不在本批次内")
        l_res = resolve_column(join["left"], join["left_key"], matches[join["left"]]["pairs"], left["new"])
        r_res = resolve_column(join["right"], join["right_key"], matches[join["right"]]["pairs"], right["new"])

        old_joinable = joinable(l_res["old_type"], r_res["old_type"])
        if l_res["status"] == "removed" or r_res["status"] == "removed":
            new_joinable = "missing"
        else:
            new_joinable = joinable(l_res["new_type"], r_res["new_type"])

        check_resolution("join_key", l_res, location, "关联键")
        check_resolution("join_key", r_res, location, "关联键")
        check_type_change("join_key", l_res, location)
        check_type_change("join_key", r_res, location)
        if new_joinable == "mismatch" and old_joinable != "mismatch":
            add_diff("join_key", "block", "key_type_mismatch", None, None,
                     f"{location}：旧口径两侧键类型可关联（{l_res['old_type']} = {r_res['old_type']}），"
                     f"新口径变为 {l_res['new_type']} = {r_res['new_type']}，不可关联 → 关联不上",
                     location)

        join_checks.append({
            "index": index,
            "location": location,
            "left": join["left"],
            "left_key": join["left_key"],
            "right": join["right"],
            "right_key": join["right_key"],
            "old": {
                "left_type": l_res["old_type"],
                "right_type": r_res["old_type"],
                "joinable": old_joinable,
                "joinable_label": JOINABLE_LABELS[old_joinable],
            },
            "new": {
                "left": l_res,
                "right": r_res,
                "joinable": new_joinable,
                "joinable_label": JOINABLE_LABELS.get(new_joinable, "键列缺失"),
            },
        })

    projection_checks: list[dict] = []
    for index, proj in enumerate(task["projections"], start=1):
        location = f"投影#{index} {proj['table']}.{proj['column']}"
        table = tables.get(proj["table"])
        if table is None:
            raise TaskFailed(f"{location}：表 {proj['table']} 不在本批次内")
        res = resolve_column(proj["table"], proj["column"], matches[proj["table"]]["pairs"], table["new"])
        check_resolution("projection", res, location, "投影列")
        check_type_change("projection", res, location)
        projection_checks.append({
            "index": index,
            "location": location,
            "table": proj["table"],
            "column": proj["column"],
            "old_type": res["old_type"],
            "resolution": res,
        })

    counts = {sev: sum(1 for d in diffs if d["severity"] == sev) for sev in ("block", "confirm", "warn", "info")}
    if counts["block"]:
        verdict = "blocked"
    elif counts["confirm"]:
        verdict = "confirm"
    elif counts["warn"]:
        verdict = "warn"
    else:
        verdict = "pass"
    return {
        "task": task["task"],
        "owner": task.get("owner"),
        "join_checks": join_checks,
        "projection_checks": projection_checks,
        "diffs": diffs,
        "counts": counts,
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
    }


def build_matches(tables: list[dict]) -> dict[str, dict]:
    """每张表的字段匹配结果（evaluate_task 的解析依据）。"""
    return {t["name"]: match_fields(t["old"]["fields"], t["new"]["fields"]) for t in tables}


def table_verdicts(tables: list[dict]) -> list[dict]:
    """每张表「单独验收」的结论：用来对照说明 单看兼容 ≠ 合起来兼容。"""
    rows = []
    for t in tables:
        run = report.build_run(t["old"], t["new"], t["consumers"], f"批次内单表 {t['name']}")
        s = run["summary"]
        rows.append({
            "table": t["name"],
            "level": s["level"],
            "level_label": s["level_label"],
            "can_execute": s["can_execute"],
            "renamed": s["renamed"],
            "added": s["added"],
            "removed": s["removed"],
            "type_changed": s["type_changed"],
            "blocking_count": s["blocking_count"],
            "broken_consumers": s["broken_consumers"],
        })
    return rows
