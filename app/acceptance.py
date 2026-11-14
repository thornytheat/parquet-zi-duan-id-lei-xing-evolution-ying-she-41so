"""批次验收编排：逐任务执行 + 断点续跑 + 整批结论 + 下载清单。

语义约定（与页面/文档一致）：
- 一次验收 = 一份输入快照 + 一个输入指纹；任务逐个执行，每完成一个就落盘（断点）。
- 中断后只能「续跑」：只执行仍待跑（pending）的任务，已完成/已失败的不重跑。
- 失败任务隔离：单个任务定义非法只标记该任务失败，其余任务照常执行；
  有失败任务的批次不能通过。
- 半批结果不能标通过：只要还有未完成任务，结论为空，状态为 interrupted。
- 重试 = 用同一份输入快照新建一次验收（新 id），从头执行；两次验收结果互不混用。
- 复核记录与下载清单都引用同一个 acceptance_id，指向同一次验收。
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import batchio, reconcile
from .reconcile import TaskFailed

STATUS_LABELS = {
    "running": "执行中",
    "interrupted": "已中断（待续跑）",
    "passed": "整批通过",
    "rejected": "整批未通过",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _log(record: dict, event: str, task: str | None = None, note: str = "") -> None:
    record["run_log"].append({"at": _now(), "event": event, "task": task, "note": note})


def create(batch: dict, source: str, acceptance_id: str) -> dict:
    """新建一次验收：全部任务置为待执行。纯组装，不落盘。"""
    record = {
        "acceptance_id": acceptance_id,
        "batch_id": batch["batch_id"],
        "batch_label": batch["label"],
        "source": source,
        "created_at": _now(),
        "updated_at": _now(),
        "status": "running",
        "input_fingerprint": batchio.fingerprint(batch),
        "input": batch,
        "table_verdicts": reconcile.table_verdicts(batch["tables"]),
        "tasks": [
            {
                "task": t["task"],
                "owner": t.get("owner"),
                "status": "pending",
                "result": None,
                "error": None,
                "exec_count": 0,
                "finished_at": None,
            }
            for t in batch["tasks"]
        ],
        "conclusion": None,
        "run_log": [],
    }
    _log(record, "created", note=f"验收创建：{len(batch['tables'])} 张表 / {len(batch['tasks'])} 个读取任务")
    return record


def run(record: dict, save=None, simulate_interrupt_after: int | None = None) -> dict:
    """执行仍待跑的任务。

    save：每完成一个任务调用一次（断点落盘）。
    simulate_interrupt_after：演示参数，执行 N 个任务后模拟进程中断（剩余任务保持待跑）。
    """
    tables = {t["name"]: t for t in record["input"]["tables"]}
    matches = reconcile.build_matches(record["input"]["tables"])
    task_defs = {t["task"]: t for t in record["input"]["tasks"]}

    finished_before = sum(1 for t in record["tasks"] if t["status"] != "pending")
    if finished_before:
        _log(record, "resumed", note=f"续跑：跳过 {finished_before} 个已有结果的任务，只执行待跑任务")
    record["status"] = "running"

    executed = 0
    for entry in record["tasks"]:
        if entry["status"] != "pending":
            continue
        if simulate_interrupt_after is not None and executed >= simulate_interrupt_after:
            record["status"] = "interrupted"
            _log(record, "interrupted",
                 note=f"演示中断：执行 {executed} 个任务后模拟进程崩溃，剩余任务待续跑")
            if save:
                save(record)
            return record
        task_def = task_defs[entry["task"]]
        try:
            result = reconcile.evaluate_task(task_def, tables, matches)
        except TaskFailed as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            _log(record, "task_failed", task=entry["task"], note=f"任务失败已隔离：{exc}")
        else:
            entry["status"] = "done"
            entry["result"] = result
            entry["error"] = None
            _log(record, "task_done", task=entry["task"],
                 note=f"对账完成：{result['verdict_label']}，"
                      f"阻断 {result['counts']['block']} / 需确认 {result['counts']['confirm']} / "
                      f"需同步 {result['counts']['warn']}")
        entry["exec_count"] += 1
        entry["finished_at"] = _now()
        executed += 1
        if save:
            save(record)  # 断点：每完成一个任务落盘一次
    conclude(record)
    if save:
        save(record)
    return record


def conclude(record: dict) -> dict:
    """整批结论：还有未完成任务 → 不判定（半批结果不能标通过）。"""
    pending = [t for t in record["tasks"] if t["status"] == "pending"]
    if pending:
        record["status"] = "interrupted"
        record["conclusion"] = None
        return record
    failed = [t for t in record["tasks"] if t["status"] == "failed"]
    counts = {"block": 0, "confirm": 0, "warn": 0, "info": 0}
    for entry in record["tasks"]:
        if entry["status"] == "done" and entry["result"]:
            for sev in counts:
                counts[sev] += entry["result"]["counts"][sev]
    reasons: list[str] = []
    if failed:
        reasons.append(f"{len(failed)} 个任务执行失败已隔离：" + "、".join(t["task"] for t in failed))
    if counts["block"]:
        reasons.append(f"存在 {counts['block']} 项阻断级差异（关联错列 / 数据丢失 / 类型阻断）")
    if counts["confirm"]:
        reasons.append(f"存在 {counts['confirm']} 项待人工确认差异")
    publishable = not failed and counts["block"] == 0 and counts["confirm"] == 0
    if publishable:
        reasons.insert(0, "全部任务完成，关联键与投影对账无阻断、无待确认项，整批可发布")
        if counts["warn"]:
            reasons.append(f"有 {counts['warn']} 项需下游同步（改名等），不阻断发布")
    record["status"] = "passed" if publishable else "rejected"
    record["conclusion"] = {
        "publishable": publishable,
        "decided_at": _now(),
        "reasons": reasons,
        "counts": {**counts, "failed_tasks": len(failed)},
    }
    _log(record, "concluded", note="整批通过，可发布" if publishable else "整批未通过，不可发布")
    return record


def summarize(record: dict) -> dict:
    """列表/页面用的派生统计（不改业务判定）。"""
    tasks = record["tasks"]
    counts = {"block": 0, "confirm": 0, "warn": 0, "info": 0}
    for entry in tasks:
        if entry["status"] == "done" and entry["result"]:
            for sev in counts:
                counts[sev] += entry["result"]["counts"][sev]
    conclusion = record.get("conclusion")
    return {
        "tasks": len(tasks),
        "done": sum(1 for t in tasks if t["status"] == "done"),
        "failed": sum(1 for t in tasks if t["status"] == "failed"),
        "pending": sum(1 for t in tasks if t["status"] == "pending"),
        "diffs": counts,
        "publishable": conclusion["publishable"] if conclusion else None,
        "status": record["status"],
        "status_label": STATUS_LABELS[record["status"]],
    }


def all_diffs(record: dict) -> list[dict]:
    """整批差异逐项汇总（仅来自已完成任务；失败任务看 error）。"""
    diffs: list[dict] = []
    for entry in record["tasks"]:
        if entry["status"] == "done" and entry["result"]:
            diffs.extend(entry["result"]["diffs"])
    return diffs


def build_manifest(record: dict) -> dict:
    """下载清单：与复核记录指向同一次验收（同一 acceptance_id + 输入指纹）。"""
    return {
        "acceptance_id": record["acceptance_id"],
        "batch_id": record["batch_id"],
        "batch_label": record["batch_label"],
        "input_fingerprint": record["input_fingerprint"],
        "status": record["status"],
        "status_label": STATUS_LABELS[record["status"]],
        "source": record["source"],
        "created_at": record["created_at"],
        "conclusion": record["conclusion"],
        "table_verdicts": record["table_verdicts"],
        "tasks": [
            {
                "task": entry["task"],
                "owner": entry["owner"],
                "status": entry["status"],
                "error": entry["error"],
                "verdict": entry["result"]["verdict"] if entry["result"] else None,
                "counts": entry["result"]["counts"] if entry["result"] else None,
                "finished_at": entry["finished_at"],
            }
            for entry in record["tasks"]
        ],
        "diffs": all_diffs(record),
    }


def batch_state(records: list[dict]) -> dict:
    """同一业务批次的多次验收推导（records 需按创建时间新→旧排序）。

    - 失败/中断时保留旧版通过结果：effective 指向最近一次通过的验收；
    - 输入变化后旧结论作废：最近通过的指纹 ≠ 最新一次验收的指纹 → 旧结论不再生效。
    """
    latest = records[0] if records else None
    last_passed = next((r for r in records if r["status"] == "passed"), None)
    stale = bool(
        last_passed and latest
        and last_passed["input_fingerprint"] != latest["input_fingerprint"]
    )
    return {
        "latest": latest,
        "last_passed": last_passed,
        "last_passed_stale": stale,
        "effective": None if stale else last_passed,
    }
