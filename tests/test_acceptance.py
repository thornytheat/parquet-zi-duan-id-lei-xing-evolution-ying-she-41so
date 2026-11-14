"""批次验收：整体验收 + 续跑/隔离/重试/指纹作废/清单一致性。"""
import pytest
from fastapi.testclient import TestClient

from app import acceptance, reconcile
from app.main import app

client = TestClient(app)


def create(payload):
    return client.post("/api/acceptances", json=payload)


def get_acc(acc_id):
    res = client.get(f"/api/acceptances/{acc_id}")
    assert res.status_code == 200
    return res.json()


def _schema(table, fields):
    return {"table": table, "fields": fields}


def _ok_batch(batch_id="batch-ok"):
    """两张无变更表 + 一个正常任务：整批通过。"""
    fields = [
        {"id": 1, "name": "user_id", "type": "int64", "nullable": False},
        {"id": 2, "name": "dt", "type": "date", "partition": True},
    ]
    return {
        "batch_id": batch_id,
        "label": "测试批次",
        "tables": [
            {"name": "t1", "old": _schema("t1", fields), "new": _schema("t1", fields)},
            {"name": "t2", "old": _schema("t2", fields), "new": _schema("t2", fields)},
        ],
        "tasks": [
            {"task": "job_ok", "joins": [{"left": "t1", "left_key": "user_id",
                                          "right": "t2", "right_key": "user_id"}],
             "projections": [{"table": "t1", "column": "user_id"}]},
        ],
    }


# ---------- 样例 D / E：整体验收 ----------

def test_sample_d_tables_compatible_but_batch_rejected():
    body = create({"sample": "D"}).json()
    assert body["status"] == "rejected"
    record = get_acc(body["acceptance_id"])
    # 各表单独看都显示兼容（full/backward 且可执行）
    assert record["table_verdicts"]
    for t in record["table_verdicts"]:
        assert t["level"] in ("full", "backward")
        assert t["can_execute"] is True
    # 合在一起：关联错列 + 数据丢失，逐项定位
    assert record["conclusion"]["publishable"] is False
    diffs = [d for t in record["tasks"] if t["result"] for d in t["result"]["diffs"]]
    codes = {d["code"] for d in diffs}
    assert "key_name_reoccupied" in codes  # payment.order_id 旧名被新列占用 → 关联错列
    assert "projection_removed" in codes   # payment.remark 被删 → 数据丢失
    blocks = [d for d in diffs if d["severity"] == "block"]
    assert len(blocks) == 2
    for d in blocks:
        assert d["location"] and d["message"] and d["task"]  # 逐项可定位
    reoccupied = next(d for d in diffs if d["code"] == "key_name_reoccupied")
    assert reoccupied["table"] == "dwd_payment"
    assert reoccupied["column"] == "order_id"


def test_sample_d_join_and_projection_reconciled_separately():
    record = get_acc(create({"sample": "D"}).json()["acceptance_id"])
    task = next(t for t in record["tasks"] if t["task"] == "rpt_settle_daily")
    result = task["result"]
    # 关联键对账：新旧口径分别记录
    join = result["join_checks"][0]
    assert join["old"]["joinable"] == "matched"
    assert join["new"]["left"]["status"] == "renamed"          # orders.order_id → order_no
    assert join["new"]["right"]["reoccupied"] is True          # payment 旧名被占用
    # 投影对账：独立成表
    assert len(result["projection_checks"]) == 4
    amount = next(p for p in result["projection_checks"] if p["column"] == "amount")
    assert amount["resolution"]["type_verdict"] == "compatible"


def test_sample_e_passed():
    body = create({"sample": "E"}).json()
    assert body["status"] == "passed"
    record = get_acc(body["acceptance_id"])
    assert record["conclusion"]["publishable"] is True
    assert record["conclusion"]["counts"]["block"] == 0
    assert record["conclusion"]["counts"]["confirm"] == 0


# ---------- 续跑：只跑未完成任务，半批不能标通过 ----------

def test_resume_only_runs_pending_tasks():
    body = create({"sample": "D", "simulate_interrupt_after": 1}).json()
    assert body["status"] == "interrupted"
    record = get_acc(body["acceptance_id"])
    assert record["conclusion"] is None  # 半批结果不能标通过
    done = [t for t in record["tasks"] if t["status"] == "done"]
    pending = [t for t in record["tasks"] if t["status"] == "pending"]
    assert len(done) == 1 and len(pending) == 2
    first_finished_at = done[0]["finished_at"]

    res = client.post(f"/api/acceptances/{body['acceptance_id']}/resume")
    assert res.status_code == 200
    assert res.json()["status"] == "rejected"  # 样例 D 跑完仍不可发布
    record = get_acc(body["acceptance_id"])
    assert all(t["status"] != "pending" for t in record["tasks"])
    for t in record["tasks"]:
        assert t["exec_count"] == 1  # 已完成的没有被重跑
    assert record["tasks"][0]["finished_at"] == first_finished_at
    assert record["conclusion"] is not None

    # 没有待跑任务后再续跑 → 400
    res = client.post(f"/api/acceptances/{body['acceptance_id']}/resume")
    assert res.status_code == 400


def test_interrupted_acceptance_page_shows_not_pass():
    body = create({"sample": "E", "simulate_interrupt_after": 0}).json()
    assert body["status"] == "interrupted"
    page = client.get(body["redirect"])
    assert page.status_code == 200
    assert "半批结果不能标为通过" in page.text


# ---------- 失败任务隔离 ----------

def test_failed_task_isolated_and_batch_rejected():
    batch = _ok_batch("batch-isolate")
    batch["tasks"].append({"task": "job_bad",
                           "joins": [{"left": "t1", "left_key": "user_id",
                                      "right": "ghost_table", "right_key": "user_id"}],
                           "projections": []})
    body = create({"batch": batch}).json()
    assert body["status"] == "rejected"
    record = get_acc(body["acceptance_id"])
    by_name = {t["task"]: t for t in record["tasks"]}
    assert by_name["job_ok"]["status"] == "done"       # 其他任务照常完成
    assert by_name["job_bad"]["status"] == "failed"    # 失败任务被隔离
    assert "ghost_table" in by_name["job_bad"]["error"]
    assert by_name["job_bad"]["result"] is None
    assert record["conclusion"]["counts"]["failed_tasks"] == 1
    assert record["conclusion"]["publishable"] is False


# ---------- 重试：新建验收，不混用两次结果 ----------

def test_retry_creates_new_acceptance_without_mixing():
    body = create({"sample": "D", "simulate_interrupt_after": 1}).json()
    old_id = body["acceptance_id"]
    res = client.post(f"/api/acceptances/{old_id}/retry")
    assert res.status_code == 200
    new_id = res.json()["acceptance_id"]
    assert new_id != old_id

    old = get_acc(old_id)
    new = get_acc(new_id)
    # 旧验收保持中断原状，不被新验收污染
    assert old["status"] == "interrupted"
    assert sum(1 for t in old["tasks"] if t["status"] == "pending") == 2
    # 新验收从头完整执行，结果只属于它自己
    assert new["status"] == "rejected"
    assert all(t["exec_count"] == 1 for t in new["tasks"])
    assert new["input_fingerprint"] == old["input_fingerprint"]  # 同一输入快照
    assert new["source"] == f"重试自 {old_id}"


# ---------- 输入指纹：失败保留旧版，输入变化旧结论作废 ----------

def test_failed_run_keeps_old_passed_result():
    batch = _ok_batch("batch-keep")
    passed_id = create({"batch": batch}).json()["acceptance_id"]
    # 同一输入再跑但中断（未完成）→ 旧版通过结果仍生效
    create({"batch": batch, "simulate_interrupt_after": 0})
    rows = client.get("/api/acceptances").json()["acceptances"]
    related = [r for r in rows if r["batch_id"] == "batch-keep"]
    state = acceptance.batch_state(related)
    assert state["last_passed"]["acceptance_id"] == passed_id
    assert state["last_passed_stale"] is False
    assert state["effective"]["acceptance_id"] == passed_id


def test_input_change_invalidates_old_conclusion():
    batch = _ok_batch("batch-stale")
    passed_id = create({"batch": batch}).json()["acceptance_id"]
    # 输入变化：t2.user_id 改成 string（关联键类型不再匹配 → 阻断）
    changed = _ok_batch("batch-stale")
    changed["tables"][1]["new"] = _schema("t2", [
        {"id": 1, "name": "user_id", "type": "string", "nullable": False},
        {"id": 2, "name": "dt", "type": "date", "partition": True},
    ])
    body = create({"batch": changed}).json()
    assert body["status"] == "rejected"
    assert body["summary"]["diffs"]["block"] >= 1  # key_type_mismatch

    page = client.get(f"/acceptance/{body['acceptance_id']}")
    assert "旧结论已作废" in page.text
    rows = client.get("/api/acceptances").json()["acceptances"]
    related = [r for r in rows if r["batch_id"] == "batch-stale"]
    state = acceptance.batch_state(related)
    assert state["last_passed"]["acceptance_id"] == passed_id
    assert state["last_passed_stale"] is True
    assert state["effective"] is None


def test_fingerprint_changes_with_input():
    from app import batchio
    batch = _ok_batch()
    fp1 = batchio.fingerprint(batchio.parse_batch(batch))
    batch["tables"][0]["new"]["fields"][0]["name"] = "uid"
    fp2 = batchio.fingerprint(batchio.parse_batch(batch))
    assert fp1 != fp2


# ---------- 复核记录与下载清单指向同一次验收 ----------

def test_manifest_and_review_point_to_same_acceptance():
    acc_id = create({"sample": "E"}).json()["acceptance_id"]
    review = client.get("/review")
    assert review.status_code == 200
    assert acc_id in review.text  # 复核记录引用该验收

    res = client.get(f"/api/acceptances/{acc_id}/manifest")
    assert res.status_code == 200
    assert "attachment" in res.headers.get("content-disposition", "")
    manifest = res.json()
    record = get_acc(acc_id)
    assert manifest["acceptance_id"] == acc_id                      # 清单指向同一次验收
    assert manifest["input_fingerprint"] == record["input_fingerprint"]
    assert manifest["status"] == record["status"]
    assert manifest["conclusion"]["publishable"] is True
    assert len(manifest["tasks"]) == len(record["tasks"])


# ---------- 页面渲染 ----------

def test_batch_pages_render():
    page = client.get("/batch?sample=D")
    assert page.status_code == 200
    assert "批次验收工作台" in page.text
    assert "settle-2026-11" in page.text
    acc_id = create({"sample": "D"}).json()["acceptance_id"]
    page = client.get(f"/acceptance/{acc_id}")
    assert page.status_code == 200
    assert "关联键对账" in page.text
    assert "投影对账" in page.text
    assert "各表单独验收" in page.text


# ---------- 非法输入与 404 ----------

@pytest.mark.parametrize("mutate,needle", [
    (lambda b: b.update(batch_id="!!"), "batch_id"),
    (lambda b: b.update(tables=[]), "tables 必须是非空数组"),
    (lambda b: b.update(tasks=[]), "tasks 必须是非空数组"),
    (lambda b: b["tables"].append(b["tables"][0]), "表名重复"),
    (lambda b: b["tasks"].append(b["tasks"][0]), "任务名重复"),
    (lambda b: b["tasks"][0].update(joins=[], projections=[]), "至少需要一个关联"),
    (lambda b: b["tasks"][0]["joins"].append({"left": "t1"}), "缺少字段"),
    (lambda b: b["tables"][0].update(old={"table": "other", "fields": [{"name": "a", "type": "string"}]}), "表名必须都等于"),
])
def test_bad_batch_inputs_return_400(mutate, needle):
    batch = _ok_batch()
    mutate(batch)
    res = create({"batch": batch})
    assert res.status_code == 400
    assert needle in res.json()["detail"]


def test_bad_simulate_param_returns_400():
    res = create({"batch": _ok_batch(), "simulate_interrupt_after": -1})
    assert res.status_code == 400
    res = create({"batch": _ok_batch(), "simulate_interrupt_after": "1"})
    assert res.status_code == 400


def test_unknown_acceptance_returns_404():
    assert client.get("/api/acceptances/acc-20990101T000000-abcdef").status_code == 404
    assert client.get("/acceptance/acc-20990101T000000-abcdef").status_code == 404
    assert client.get("/api/acceptances/acc-20990101T000000-abcdef/manifest").status_code == 404
    assert client.post("/api/acceptances/acc-20990101T000000-abcdef/resume").status_code == 404
    assert client.post("/api/acceptances/acc-20990101T000000-abcdef/retry").status_code == 404


def test_unknown_batch_sample_returns_404():
    assert client.get("/api/batch-samples/Z").status_code == 404


# ---------- 纯函数：关联键可关联性 ----------

def test_joinable_rules():
    assert reconcile.joinable("int64", "int64") == "matched"
    assert reconcile.joinable("int32", "int64") == "matched_with_cast"
    assert reconcile.joinable("string", "int64") == "mismatch"
    assert reconcile.joinable("date", "timestamp") == "mismatch"
