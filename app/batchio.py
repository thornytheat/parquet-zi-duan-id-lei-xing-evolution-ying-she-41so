"""业务批次输入的解析、校验、指纹与批次样例加载。

批次 = 一个业务发布单元：多张表的 old/new schema（+ 可选数据文件清单）
+ 读取任务清单（关联键 joins / 投影 projections）。整批验收的输入指纹
由解析后的规范化内容决定，输入任何变化都会得到新指纹（旧结论随之作废）。
"""
from __future__ import annotations

import hashlib
import json
import re

from . import settings
from .schemaio import SchemaError, _load_json, parse_consumers, parse_schema

BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")


def parse_batch(value, label: str = "批次定义") -> dict:
    doc = _load_json(value, label)
    if not isinstance(doc, dict):
        raise SchemaError(f"{label} 必须是 JSON 对象")
    batch_id = str(doc.get("batch_id") or "").strip()
    if not BATCH_ID_RE.match(batch_id):
        raise SchemaError(f"{label} 的 batch_id 需为 2-64 位字母/数字/._-，收到 {batch_id!r}")
    batch_label = str(doc.get("label") or "").strip() or batch_id

    raw_tables = doc.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise SchemaError(f"{label} 的 tables 必须是非空数组")
    tables: list[dict] = []
    seen_tables: set[str] = set()
    for index, raw in enumerate(raw_tables, start=1):
        if not isinstance(raw, dict):
            raise SchemaError(f"{label} 第 {index} 张表必须是 JSON 对象")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise SchemaError(f"{label} 第 {index} 张表缺少 name")
        if name in seen_tables:
            raise SchemaError(f"{label} 表名重复：{name}")
        seen_tables.add(name)
        old = parse_schema(raw.get("old"), f"表 {name} 旧 schema")
        new = parse_schema(raw.get("new"), f"表 {name} 新 schema")
        if old["table"] != name or new["table"] != name:
            raise SchemaError(f"表 {name} 的 old/new schema 表名必须都等于 {name}")
        consumers_raw = raw.get("consumers")
        if consumers_raw is None:
            consumers = {"table": name, "files": []}
        else:
            consumers = parse_consumers(consumers_raw, f"表 {name} 数据文件清单")
            if consumers.get("table") and consumers["table"] != name:
                raise SchemaError(f"表 {name} 数据文件清单的表名 {consumers['table']} 与批次声明不一致")
        tables.append({"name": name, "old": old, "new": new, "consumers": consumers})

    raw_tasks = doc.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise SchemaError(f"{label} 的 tasks 必须是非空数组（读取任务是整批验收的对账对象）")
    tasks: list[dict] = []
    seen_tasks: set[str] = set()
    for index, raw in enumerate(raw_tasks, start=1):
        if not isinstance(raw, dict):
            raise SchemaError(f"{label} 第 {index} 个任务必须是 JSON 对象")
        name = str(raw.get("task") or "").strip()
        if not name:
            raise SchemaError(f"{label} 第 {index} 个任务缺少 task 名称")
        if name in seen_tasks:
            raise SchemaError(f"{label} 任务名重复：{name}")
        seen_tasks.add(name)
        joins = _parse_joins(raw.get("joins"), name, label)
        projections = _parse_projections(raw.get("projections"), name, label)
        if not joins and not projections:
            raise SchemaError(f"任务 {name} 至少需要一个关联（joins）或投影（projections）")
        tasks.append({
            "task": name,
            "owner": str(raw.get("owner") or "").strip() or None,
            "joins": joins,
            "projections": projections,
        })
    return {"batch_id": batch_id, "label": batch_label, "tables": tables, "tasks": tasks}


def _parse_joins(value, task: str, label: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SchemaError(f"{label} 任务 {task} 的 joins 必须是数组")
    joins: list[dict] = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise SchemaError(f"任务 {task} 第 {index} 个关联必须是 JSON 对象")
        join = {k: str(raw.get(k) or "").strip() for k in ("left", "left_key", "right", "right_key")}
        missing = [k for k, v in join.items() if not v]
        if missing:
            raise SchemaError(f"任务 {task} 第 {index} 个关联缺少字段：{', '.join(missing)}")
        joins.append(join)
    return joins


def _parse_projections(value, task: str, label: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SchemaError(f"{label} 任务 {task} 的 projections 必须是数组")
    projections: list[dict] = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise SchemaError(f"任务 {task} 第 {index} 个投影必须是 JSON 对象")
        table = str(raw.get("table") or "").strip()
        column = str(raw.get("column") or "").strip()
        if not table or not column:
            raise SchemaError(f"任务 {task} 第 {index} 个投影缺少 table 或 column")
        projections.append({"table": table, "column": column})
    return projections


def fingerprint(batch: dict) -> str:
    """输入指纹：解析后规范化内容的 sha256；输入任何变化都会得到新指纹。"""
    canonical = json.dumps(batch, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_batch_sample_index() -> list[dict]:
    index_path = settings.SAMPLE_DIR / "index.json"
    if not index_path.exists():
        raise SchemaError(f"样例索引缺失：{index_path}")
    doc = json.loads(index_path.read_text(encoding="utf-8"))
    return doc.get("batch_samples", [])


def load_batch_sample(key: str) -> dict:
    key = (key or "").strip().upper()
    entry = None
    for item in load_batch_sample_index():
        if item.get("key") == key:
            entry = item
            break
    if entry is None:
        raise SchemaError(f"未知批次样例 {key!r}，可选：{' / '.join(settings.BATCH_SAMPLE_KEYS)}")
    path = settings.SAMPLE_DIR / entry["file"]
    if not path.exists():
        raise SchemaError(f"样例文件缺失：{entry['file']}")
    raw = path.read_text(encoding="utf-8")
    return {
        "key": key,
        "label": entry.get("label", key),
        "description": entry.get("description", ""),
        "expected": entry.get("expected", ""),
        "batch": parse_batch(raw, f"批次样例 {key}"),
        "raw": raw,
    }
