"""原料批号并档测试。

覆盖：
- 校验：源号与目标号相同、未知批号、已停用（已并档）批号、空白原因均整体拒绝，
  失败不留下部分引用、批号状态或任何裁决/出货资格版本；
- 并档语义：目标号保留自身名称/过敏原/待检锁定状态；源号置为 MERGED 留档可查，
  不再接受新批次引用、更正或锁定操作；
- 用料改指：有效批次对源号的直接用料改指目标号；同时引用两号的批次去重；
  已作废批次的用料（含 batch_material 与登记 JSON）一律不改写；
- 顺序重算：受影响产线从最早“用料改指”批次起按序重算并追加 MERGE_LOT 裁决版本；
  目标号更早的引用批次不在范围内；即使结论不变也追加版本；
- 出货资格：改指批次按目标号当前锁定状态更新、资格版本强制递增；
  已确认核验单（含已发运）随版本变化转待复核；
- 历史不改写：历次裁决版本、确认快照、交接/退回/复核记录均保留，已发运批次仍按原交接追溯；
- 并档链：源号可被二次并档；只追加并档记录，可按源号/目标号过滤查询。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def merge(client, source, target, reason):
    return client.post(
        "/api/material-lots/merge",
        json={"source_lot_no": source, "target_lot_no": target, "reason": reason},
    )


# ---------------------------------------------------------------- 拒绝路径


def test_merge_rejects_same_source_and_target(client):
    add_lot(client, "L-A")
    resp = merge(client, "L-A", "L-A", "同一批号")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SAME_SOURCE_TARGET_LOT"
    # 批号仍可使用
    assert client.get("/api/material-lots/L-A").json()["usable"] is True


def test_merge_rejects_unknown_lots(client):
    add_lot(client, "L-A")
    resp = merge(client, "NOPE", "L-A", "原因")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_NOT_FOUND"

    resp = merge(client, "L-A", "MISSING", "原因")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_NOT_FOUND"


def test_merge_rejects_empty_reason(client):
    add_lot(client, "L-A")
    add_lot(client, "L-B")
    for bad in ("", "   ", "\t"):
        resp = merge(client, "L-A", "L-B", bad)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"
    assert client.get("/api/material-lots/L-A").json()["status"] == "ACTIVE"


def test_merge_rejects_already_merged_lots(client):
    add_lot(client, "S1")
    add_lot(client, "T1")
    add_lot(client, "T2")
    assert merge(client, "S1", "T1", "首次并档").status_code == 200

    # 已并档源号不能再作为源或目标
    resp = merge(client, "S1", "T2", "再次")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"
    resp = merge(client, "T2", "S1", "反向")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"
    # 目标 T2 仍可使用，未被失败请求影响
    assert client.get("/api/material-lots/T2").json()["usable"] is True


def test_failed_merge_leaves_no_partial_changes(client):
    add_lot(client, "S1", "奶粉", ["milk"])
    add_lot(client, "ZZZ", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["S1"], declared=["milk"])
    before = client.get("/api/batches/B1").json()

    resp = merge(client, "S1", "ZZZ", "")
    assert resp.status_code == 400

    after = client.get("/api/batches/B1").json()
    assert after["material_lots"] == ["S1"]
    assert after["adjudication"]["version"] == before["adjudication"]["version"]
    assert (
        after["shipment_eligibility"]["version"]
        == before["shipment_eligibility"]["version"]
    )
    assert client.get("/api/material-lot-merges").json()["merges"] == []


# ---------------------------------------------------------------- 批号状态


def test_target_keeps_own_identity_and_lock_source_archived(client):
    add_lot(client, "S1", "误登记奶粉名", ["milk"])
    add_lot(client, "T1", "正确奶粉名", ["milk", "soy"])
    client.post("/api/material-lots/T1/lock", json={"reason": "待检锁定中"})

    resp = merge(client, "S1", "T1", "同一原料误登两号")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    target = body["target_lot"]
    assert target["lot_no"] == "T1"
    assert target["material"] == "正确奶粉名"
    assert target["allergens"] == ["milk", "soy"]
    assert target["lock"]["locked"] is True
    assert target["status"] == "ACTIVE"

    source = body["source_lot"]
    assert source["status"] == "MERGED"
    assert source["usable"] is False
    assert source["merged"] is True
    assert source["merged_into"] == "T1"
    assert source["merged_reason"] == "同一原料误登两号"
    assert source["merged_at"] is not None
    # 源号锁定状态随并档冻结（从未锁过，仍显示未锁），可查
    assert source["lock"]["locked"] is False

    detail = client.get("/api/material-lots/S1").json()
    assert detail["status"] == "MERGED"
    assert detail["merged_into"] == "T1"


def test_merged_lot_rejects_new_reference_correction_and_lock(client):
    add_lot(client, "S1", "奶粉", ["milk"])
    add_lot(client, "T1", "奶粉", ["milk"])
    merge(client, "S1", "T1", "并档")

    # 新批次引用源号
    resp = add_batch_raw(client, "BN1", ["S1"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"
    assert resp.json()["error"]["details"][0]["merged_into"] == "T1"

    # 更正批号档案
    resp = client.put("/api/material-lots/S1", json={"material": "新名"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"

    # 锁定 / 解除
    for path in ("lock", "unlock"):
        resp = client.post(f"/api/material-lots/S1/{path}", json={"reason": "x"})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"

    # 查询（GET）仍可
    assert client.get("/api/material-lots/S1").status_code == 200
    assert client.get("/api/material-lots/S1/lock").status_code == 200
    # 目标号一切照常
    assert client.post(
        "/api/material-lots/T1/unlock", json={"reason": "复检合格"}
    ).status_code == 200


def add_batch_raw(client, batch_no, lots):
    return client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": "L1",
            "material_lots": lots,
            "declared_allergens": [],
            "cleaned_allergens": [],
        },
    )


# ---------------------------------------------------------------- 用料改指/去重


def test_active_batches_repointed_and_deduplicated(client):
    add_lot(client, "S1", "奶粉", ["milk"])
    add_lot(client, "T1", "奶粉", ["milk"])
    b1 = add_batch(client, "B1", lots=["S1"], declared=["milk"])
    # B2 同时引用两号 + 另一个批号，验证去重后保序且其他批号不动
    add_lot(client, "P1", "花生", ["peanut"])
    b2 = add_batch(client, "B2", lots=["S1", "T1", "P1"], declared=["milk", "peanut"])
    # B3 仅引用目标号：不应出现在 changed_batches
    add_batch(client, "B3", lots=["T1"], declared=["milk"])

    resp = merge(client, "S1", "T1", "并档")
    assert resp.status_code == 200
    changed = {c["batch_no"]: c for c in resp.json()["changed_batches"]}
    assert changed["B1"]["operation"] == "REPOINTED"
    assert changed["B1"]["from_material_lots"] == ["S1"]
    assert changed["B1"]["to_material_lots"] == ["T1"]
    assert changed["B2"]["operation"] == "DEDUPLICATED"
    assert changed["B2"]["from_material_lots"] == ["S1", "T1", "P1"]
    assert changed["B2"]["to_material_lots"] == ["T1", "P1"]
    assert "B3" not in changed

    assert client.get("/api/batches/B1").json()["material_lots"] == ["T1"]
    assert client.get("/api/batches/B2").json()["material_lots"] == ["T1", "P1"]
    assert client.get("/api/batches/B3").json()["material_lots"] == ["T1"]

    # 登记中引用的版本号未因改指本身变化（版本由重算追加）
    assert b1["adjudication"]["version"] == 1
    assert b2["adjudication"]["version"] == 1


def test_voided_batches_are_not_rewritten(client):
    add_lot(client, "S1", "奶粉", ["milk"])
    add_lot(client, "T1", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["S1"], declared=["milk"])
    add_batch(client, "V1", line_id="L2", lots=["S1"], declared=["milk"])
    assert client.post("/api/batches/V1/void", json={"reason": "误登记"}).status_code == 200

    body = merge(client, "S1", "T1", "并档").json()
    untouched = body["untouched_voided_batches"]
    assert [u["batch_no"] for u in untouched] == ["V1"]

    v1 = client.get("/api/batches/V1").json()
    assert v1["material_lots"] == ["S1"]  # 用料 JSON 不改写
    # 作废批不产生新裁决/资格版本
    assert v1["adjudication"]["version"] == 1
    # 作废批次历史无 MERGE_LOT
    hist = client.get("/api/batches/V1/history").json()["history"]
    assert [h["trigger"] for h in hist] == ["REGISTER"]


# ---------------------------------------------------------------- 顺序重算


def test_recompute_from_earliest_reference_per_line(client):
    add_lot(client, "S1", "花生", ["peanut"])
    add_lot(client, "T1", "奶粉", ["milk"])
    # 目标号在更早批次先被使用：X1 不应被重算
    add_batch(client, "X1", lots=["T1"], declared=["milk"])
    add_batch(client, "X2", lots=["S1"], declared=["peanut"], cleaned=["milk"])
    add_batch(client, "X3", lots=[], declared=[], cleaned=[])
    # 另一条产线
    add_batch(client, "Y1", line_id="L2", lots=["S1"], declared=["peanut"])

    body = merge(client, "S1", "T1", "误登记").json()
    recomputed = {(r["batch_no"]): r for r in body["recomputed"]}
    # 过敏原不变（S 与 T 的并集 = milk+peanut 的引用被改指为 milk only）：
    # X2 改指后只用 milk（peanut 消失），X2/X3 结论可能变化，但无论如何都追加版本
    assert set(recomputed) == {"X2", "X3", "Y1"}
    assert all(r["version"] == 2 for r in recomputed.values())

    # X1 不在范围内：裁决版本仍为 1
    assert client.get("/api/batches/X1").json()["adjudication"]["version"] == 1
    # 历史版本 trigger 正确
    for bn in ("X2", "X3", "Y1"):
        hist = client.get(f"/api/batches/{bn}/history").json()["history"]
        assert hist[-1]["trigger"] == "MERGE_LOT"
        assert hist[-1]["version"] == 2


def test_merge_appends_version_even_when_disposition_unchanged(client):
    add_lot(client, "S1", "奶粉", ["milk"])
    add_lot(client, "T1", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["S1"], declared=["milk"])
    before = client.get("/api/batches/B1").json()
    assert before["adjudication"]["disposition"] == "RELEASE"

    body = merge(client, "S1", "T1", "同过敏原并档").json()
    assert body["recomputed"][0]["disposition"] == "RELEASE"
    after = client.get("/api/batches/B1").json()
    assert after["adjudication"]["version"] == 2  # 结论不变也追加版本


def test_merging_changes_allergens_recomputes_chain(client):
    # 源号含 peanut、目标号无过敏原：并档后引用批次失去 peanut
    add_lot(client, "S1", "花生酱", ["peanut"])
    add_lot(client, "T1", "可可块", [])
    add_batch(client, "B1", lots=["S1"], declared=["peanut"])  # RELEASE
    add_batch(client, "B2", lots=[], declared=[], cleaned=[])  # 继承 peanut -> HOLD
    merge(client, "S1", "T1", "误登")
    b1 = client.get("/api/batches/B1").json()
    b2 = client.get("/api/batches/B2").json()
    assert b1["adjudication"]["disposition"] == "RELEASE"
    assert b2["adjudication"]["disposition"] == "RELEASE"  # 无 peanut 可继承
    assert b2["adjudication"]["actual_allergens"] == []


# ---------------------------------------------------------------- 出货资格


def test_shipment_eligibility_follows_target_lock_and_advances(client):
    add_lot(client, "S1", "奶粉", ["milk"])
    add_lot(client, "T1", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["S1"], declared=["milk"])
    add_batch(client, "B2", lots=["S1", "T1"], declared=["milk"])

    # 目标号锁定：改指后批次应 BLOCKED 于 T1
    client.post("/api/material-lots/T1/lock", json={"reason": "待检"})
    body = merge(client, "S1", "T1", "并档").json()
    ship = {x["batch_no"]: x for x in body["shipment_eligibility"]}
    assert ship["B1"]["status"] == "BLOCKED"
    assert ship["B1"]["locked_lots"] == ["T1"]
    assert ship["B1"]["version"] == 2
    # B2 原本已经直接使用 T1（资格 v2=BLOCKED），并档仍强制再递一版
    assert ship["B2"]["version"] == 3
    assert ship["B2"]["locked_lots"] == ["T1"]

    # 目标号未锁定的场景：状态保持 SHIPPABLE，版本照样递增
    add_lot(client, "S2", "奶粉2", ["milk"])
    add_lot(client, "T2", "奶粉2", ["milk"])
    add_batch(client, "C1", line_id="L9", lots=["S2"], declared=["milk"])
    body2 = merge(client, "S2", "T2", "并档2").json()
    c1 = body2["shipment_eligibility"][0]
    assert c1["status"] == "SHIPPABLE"
    assert c1["version"] == 2
    assert client.get("/api/batches/C1").json()["shipment_eligibility"]["version"] == 2


# ---------------------------------------------------------------- 核验单/交接联动


def create_verify_confirm(client, no, batch_nos):
    assert client.post(
        "/api/verifications", json={"verification_no": no, "batch_nos": batch_nos}
    ).status_code == 201
    assert client.post(f"/api/verifications/{no}/confirm").status_code == 200


def test_confirmed_verification_flips_to_pending_review(client):
    add_lot(client, "S1", "奶粉", ["milk"])
    add_lot(client, "T1", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["S1"], declared=["milk"])
    add_batch(client, "B2", line_id="L2", lots=["T1"], declared=["milk"])  # 不受影响
    create_verify_confirm(client, "SH-1", ["B1", "B2"])

    body = merge(client, "S1", "T1", "并档").json()
    affected = {v["verification_no"]: v["status"] for v in body["affected_verifications"]}
    assert affected == {"SH-1": "PENDING_REVIEW"}
    assert client.get("/api/verifications/SH-1").json()["status"] == "PENDING_REVIEW"
    # 历次确认快照不改
    confirms = client.get("/api/verifications/SH-1").json()["confirmations"]
    assert len(confirms) == 1
    assert confirms[0]["batches"][0]["version"] == 1


def test_shipped_batch_handover_unchanged_and_traceable(client):
    add_lot(client, "S1", "奶粉", ["milk"])
    add_lot(client, "T1", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["S1"], declared=["milk"])
    create_verify_confirm(client, "SH-1", ["B1"])
    rev = client.get("/api/verifications/SH-1").json()["revision"]
    assert client.post(
        "/api/handovers",
        json={"handover_no": "HO-1", "verification_no": "SH-1", "expected_revision": rev},
    ).status_code == 201

    merge(client, "S1", "T1", "并档")

    # 交接记录冻结不变；批次仍按原交接追溯
    handover = client.get("/api/handovers/HO-1").json()
    assert handover["batches"][0]["batch_no"] == "B1"
    assert handover["batches"][0]["adjudication"]["version"] == 1
    trace = client.get("/api/batches/B1/handover").json()
    assert trace["shipped"] is True
    assert trace["handover"]["handover_no"] == "HO-1"

    # 发运后复核：版本变化形成待复核事项（即使结论仍 RELEASE）
    reviews = client.get("/api/handovers/HO-1/reviews").json()
    item = reviews["batches"][0]
    assert item["review_status"] == "PENDING_REVIEW"
    seen = item["seen_state"]
    resp = client.post(
        "/api/handovers/HO-1/reviews/B1/decision",
        json={
            "seen_adjudication_version": seen["adjudication_version"],
            "seen_shipment_version": seen["shipment_version"],
            "seen_batch_voided": seen["batch_voided"],
            "disposition": "NO_RECALL",
            "reason": "并档改指，实际同一原料",
        },
    )
    assert resp.status_code == 201, resp.text


def test_voided_batch_in_verification_blocks_reverify_after_merge(client):
    add_lot(client, "S1", "奶粉", ["milk"])
    add_lot(client, "T1", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["S1"], declared=["milk"])
    add_batch(client, "V1", line_id="L2", lots=["S1"], declared=["milk"])
    client.post("/api/verifications", json={"verification_no": "SH-1", "batch_nos": ["B1", "V1"]})
    client.post("/api/batches/V1/void", json={"reason": "误登记"})
    merge(client, "S1", "T1", "并档")
    # 含作废批（且其用料未被改写）的单据仍可查询，重新核验继续拒绝
    assert client.get("/api/verifications/SH-1").status_code == 200
    resp = client.post("/api/verifications/SH-1/reverify")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_CONTAINS_VOIDED_BATCH"


# ---------------------------------------------------------------- 并档记录与并档链


def test_merge_history_list_filter_and_get(client):
    add_lot(client, "S1")
    add_lot(client, "T1")
    add_lot(client, "S2")
    add_lot(client, "T2")
    merge(client, "S1", "T1", "原因一")
    merge(client, "S2", "T2", "原因二")

    assert len(client.get("/api/material-lot-merges").json()["merges"]) == 2
    filtered = client.get("/api/material-lot-merges?lot_no=T1").json()["merges"]
    assert [(m["source_lot_no"], m["target_lot_no"]) for m in filtered] == [("S1", "T1")]

    one = client.get("/api/material-lot-merges/1").json()
    assert one["source_lot_no"] == "S1"
    assert one["reason"] == "原因一"

    assert client.get("/api/material-lot-merges/999").status_code == 404
    assert client.get(
        "/api/material-lot-merges?lot_no=UNKNOWN"
    ).status_code == 404


def test_chained_merge(client):
    add_lot(client, "S1", "奶粉", ["milk"])
    add_lot(client, "T1", "奶粉", ["milk"])
    add_lot(client, "T2", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["S1"], declared=["milk"])

    assert merge(client, "S1", "T1", "第一次").status_code == 200
    body = merge(client, "T1", "T2", "第二次")
    assert body.status_code == 200
    changed = [c["batch_no"] for c in body.json()["changed_batches"]]
    assert changed == ["B1"]  # B1 当前引用 T1，再次改指 T2
    assert client.get("/api/batches/B1").json()["material_lots"] == ["T2"]

    # 源号 S1 的留档指向不变（只追加），T1 指向 T2
    assert client.get("/api/material-lots/S1").json()["merged_into"] == "T1"
    assert client.get("/api/material-lots/T1").json()["merged_into"] == "T2"
    assert client.get("/api/material-lots/T2").json()["usable"] is True
    merges = client.get("/api/material-lot-merges?lot_no=S1").json()["merges"]
    assert len(merges) == 1  # 第二次并档不回写第一次记录


def test_merge_without_any_batch_references(client):
    add_lot(client, "S1")
    add_lot(client, "T1")
    body = merge(client, "S1", "T1", "无人引用").json()
    assert body["changed_batches"] == []
    assert body["untouched_voided_batches"] == []
    assert body["recomputed"] == []
    assert body["shipment_eligibility"] == []
    assert client.get("/api/material-lots/S1").json()["status"] == "MERGED"
