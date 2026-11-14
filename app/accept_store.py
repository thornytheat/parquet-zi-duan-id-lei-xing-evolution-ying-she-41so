"""批次验收记录持久化：data/acceptances/*.json（断点续跑、历史回看、清单下载）。"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from . import acceptance, settings

ACCEPT_ID_RE = re.compile(r"^acc-\d{8}T\d{6}-[0-9a-f]{6}$")


def new_acceptance_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"acc-{stamp}-{uuid.uuid4().hex[:6]}"


def _ensure_dir() -> None:
    settings.ACCEPT_DIR.mkdir(parents=True, exist_ok=True)


def _path(acceptance_id: str):
    if not ACCEPT_ID_RE.match(acceptance_id or ""):
        raise KeyError(f"验收记录 {acceptance_id!r} 不存在")
    return settings.ACCEPT_DIR / f"{acceptance_id}.json"


def save_acceptance(record: dict) -> dict:
    _ensure_dir()
    record = dict(record)
    record["updated_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    path = _path(record["acceptance_id"])
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _prune()
    return record


def load_acceptance(acceptance_id: str) -> dict:
    path = _path(acceptance_id)
    if not path.exists():
        raise KeyError(f"验收记录 {acceptance_id!r} 不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def list_acceptances(limit: int = 50) -> list[dict]:
    _ensure_dir()
    files = sorted(settings.ACCEPT_DIR.glob("acc-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for path in files[:limit]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rows.append({
            "acceptance_id": record.get("acceptance_id"),
            "batch_id": record.get("batch_id"),
            "batch_label": record.get("batch_label"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "source": record.get("source"),
            "status": record.get("status"),
            "input_fingerprint": record.get("input_fingerprint"),
            "summary": acceptance.summarize(record),
        })
    return rows


def _prune() -> None:
    files = sorted(settings.ACCEPT_DIR.glob("acc-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[settings.MAX_ACCEPTANCES_KEPT:]:
        try:
            path.unlink()
        except OSError:
            pass
