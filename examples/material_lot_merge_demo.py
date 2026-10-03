#!/usr/bin/env python3
"""食品换线放行服务 —— 原料批号并档端到端调用示例（仅使用 Python 标准库）。

业务背景：同一原料（一批奶粉）在来料登记时被误登记成两个编号
MG-SRC（误登记号）与 MG-DST（正确保留号），部分生产批次已引用源号。

演示流程（使用独立批号/批次号前缀，避免与其他示例冲突）：
  1. 登记两个原料批号与 4 个批次：
       MG-B1 产线 MGL1：直接使用 MG-SRC，声明 milk  -> RELEASE
       MG-B2 同线后续：同时使用 MG-SRC 与 MG-DST（验证并档去重）
       MG-B3 同线后续：不使用原料（验证从最早引用批次起顺序重算）
       MG-V1 产线 MGL2：引用 MG-SRC 后作废（验证已作废批次用料不改写）
  2. 创建并确认出货核验单 MG-SH-1：[MG-B1]，并登记整单装车交接 MG-HO-1
     （验证已发运批次仍按原交接追溯、交接冻结快照不改写）
  3. 拒绝场景：相同编号 409、未知批号 404、空白原因 400，且不留部分改动
  4. 并档前锁定 MG-DST：并档后改指批次的出货资格按目标号锁定状态更新
  5. POST /api/material-lots/merge 并档（非空原因）：
       MG-SRC 置 MERGED 留档、MG-DST 保留自身名称/过敏原/锁定状态；
       MG-B1 用料改指 MG-DST、MG-B2 去重；MG-V1 用料原样保留；
       MGL1 从 MG-B1 起重算并追加 MERGE_LOT 裁决版本；
       改指批次出货资格版本递增（BLOCKED 于 MG-DST）；
       已确认核验单（已发运）随版本转 PENDING_REVIEW，交接冻结快照不变。
  6. 并档后拒绝：新批次/更正/锁定使用 MG-SRC 全部 400/409；GET 仍可查。
  7. 查询并档记录（全部 / 按批号过滤 / 单条）与批次历史（MERGE_LOT 版本）。
  8. 发运后复核：交接 MG-HO-1 的 MG-B1 因版本变化成为待复核事项，
     凭所见版本提交 NO_RECALL 处置；单批交接归属仍指向原交接。

用法：
    python examples/material_lot_merge_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/material_lot_merge_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")

SRC = "MG-SRC"
DST = "MG-DST"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, body: dict) -> None:
    print(f"[{status}] {label}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def ensure_lot(lot_no: str, material: str, allergens: list[str]) -> None:
    status, body = call("POST", "/api/material-lots", {
        "lot_no": lot_no, "material": material, "allergens": allergens,
    })
    if status == 201:
        print(f"  已登记原料批号 {lot_no}（{material}，{allergens}）")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, line_id: str, payload_extra: dict) -> None:
    payload = {"batch_no": batch_no, "line_id": line_id, **payload_extra}
    status, body = call("POST", "/api/batches", payload)
    if status != 201:
        raise RuntimeError(f"登记批次 {batch_no} 失败: {body}")
    adj = body["adjudication"]
    print(f"  已登记批次 {batch_no}（{line_id}）=> {adj['disposition']}（v{adj['version']}）")


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    section("1. 登记两个被误登记的原料批号与生产批次")
    ensure_lot(SRC, "误登记：全脂奶粉（来料重复建档）", ["milk"])
    ensure_lot(DST, "正确保留号：全脂奶粉", ["milk"])

    ensure_batch("MG-B1", "MGL1", {
        "material_lots": [SRC], "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    ensure_batch("MG-B2", "MGL1", {
        "material_lots": [SRC, DST],
        "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    ensure_batch("MG-B3", "MGL1", {
        "material_lots": [], "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    ensure_batch("MG-V1", "MGL2", {
        "material_lots": [SRC], "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    status, body = call("POST", "/api/batches/MG-V1/void", {"reason": "计划误登记，实际未投产"})
    if status != 200:
        raise RuntimeError(f"作废 MG-V1 失败: {body}")
    print("  已作废批次 MG-V1（其用料记录将在并档时原样保留）")

    section("2. 已发运批次：创建/确认核验单并登记整单交接")
    status, body = call("POST", "/api/verifications", {
        "verification_no": "MG-SH-1", "batch_nos": ["MG-B1"],
    })
    if status != 201:
        raise RuntimeError(f"创建核验单失败: {body}")
    status, body = call("POST", "/api/verifications/MG-SH-1/confirm")
    if status != 200:
        raise RuntimeError(f"确认核验单失败: {body}")
    revision = body["revision"]
    print(f"  核验单 MG-SH-1 已确认（修订号 v{revision}）")

    status, body = call("POST", "/api/handovers", {
        "handover_no": "MG-HO-1",
        "verification_no": "MG-SH-1",
        "expected_revision": revision,
    })
    if status != 201:
        raise RuntimeError(f"装车交接失败: {body}")
    print("  交接 MG-HO-1 完成：MG-B1 已发运，交接快照冻结")

    section("3. 拒绝场景：相同编号 / 未知批号 / 空白原因（整体拒绝）")
    for label, payload in [
        ("源号与目标号相同", {"source_lot_no": DST, "target_lot_no": DST, "reason": "x"}),
        ("未知源批号", {"source_lot_no": "NO-SUCH", "target_lot_no": DST, "reason": "x"}),
        ("未知目标批号", {"source_lot_no": SRC, "target_lot_no": "NO-SUCH", "reason": "x"}),
        ("空白原因", {"source_lot_no": SRC, "target_lot_no": DST, "reason": "   "}),
    ]:
        st, bd = call("POST", "/api/material-lots/merge", payload)
        print(f"[{st}] {label}: {bd['error']['code']} - {bd['error']['message']}")

    section("4. 并档前锁定目标号 MG-DST（并档后出货资格按目标号锁定状态计算）")
    status, body = call("POST", f"/api/material-lots/{DST}/lock", {"reason": "供应商通报待检风险"})
    if status != 200:
        raise RuntimeError(f"锁定 {DST} 失败: {body}")
    print(f"  {DST} 已锁定：{body['locked']}（锁定版本 v{body['lock_version']}）")

    section("5. 执行并档：MG-SRC -> MG-DST（非空原因）")
    status, body = call("POST", "/api/material-lots/merge", {
        "source_lot_no": SRC,
        "target_lot_no": DST,
        "reason": "同一批全脂奶粉来料时被误登记成两个编号，合并为正确保留号",
    })
    if status != 200:
        raise RuntimeError(f"并档失败: {body}")
    show("并档结果", status, body)

    print("\n  关键核对：")
    print("  - 源号状态:", body["source_lot"]["status"], "| merged_into:",
          body["source_lot"]["merged_into"])
    print("  - 目标号保留自身名称/过敏原/锁定:",
          body["target_lot"]["material"], body["target_lot"]["allergens"],
          "locked =", body["target_lot"]["lock"]["locked"])
    for c in body["changed_batches"]:
        print(f"  - 用料改指 {c['batch_no']}（{c['operation']}）: "
              f"{c['from_material_lots']} -> {c['to_material_lots']}")
    print("  - 未改写的已作废批次:",
          [u["batch_no"] for u in body["untouched_voided_batches"]])
    print("  - 追加 MERGE_LOT 裁决版本的批次:",
          [(r["batch_no"], f"v{r['version']}", r["disposition"]) for r in body["recomputed"]])
    print("  - 出货资格更新:",
          [(r["batch_no"], f"v{r['version']}", r["status"], r["locked_lots"])
           for r in body["shipment_eligibility"]])
    print("  - 受影响核验单:",
          [(v["verification_no"], v["status"]) for v in body["affected_verifications"]])

    section("6. 并档后：源号留档可查，但拒绝新引用 / 更正 / 锁定")
    st, bd = call("POST", "/api/batches", {
        "batch_no": "MG-B4", "line_id": "MGL1",
        "material_lots": [SRC], "declared_allergens": [], "cleaned_allergens": [],
    })
    print(f"[{st}] 新批次引用源号: {bd['error']['code']}")
    st, bd = call("PUT", f"/api/material-lots/{SRC}", {"material": "试图改名"})
    print(f"[{st}] 更正源号档案: {bd['error']['code']}")
    st, bd = call("POST", f"/api/material-lots/{SRC}/lock", {"reason": "试图锁定"})
    print(f"[{st}] 锁定源号: {bd['error']['code']}")
    st, bd = call("GET", f"/api/material-lots/{SRC}")
    print(f"[{st}] 源号仍可查询: status={bd['status']} merged_into={bd['merged_into']}")
    st, bd = call("GET", "/api/batches/MG-V1")
    print(f"[{st}] 已作废批次 MG-V1 用料未改写: {bd['material_lots']}")

    section("7. 查询并档记录与批次历史裁决版本")
    st, bd = call("GET", "/api/material-lot-merges")
    print(f"[{st}] 并档记录数: {len(bd['merges'])}")
    st, bd = call("GET", f"/api/material-lot-merges?lot_no={SRC}")
    print(f"[{st}] 按源号过滤: {[(m['source_lot_no'], m['target_lot_no']) for m in bd['merges']]}")
    st, bd = call("GET", f"/api/material-lot-merges?lot_no={DST}")
    mid = bd["merges"][0]["merge_id"]
    st, bd = call("GET", f"/api/material-lot-merges/{mid}")
    print(f"[{st}] 单条并档记录 #{mid}: {bd['source_lot_no']} -> {bd['target_lot_no']}"
          f"（改指 {len(bd['changed_batches'])} 批，作废未改写 "
          f"{len(bd['untouched_voided_batches'])} 批）")

    st, bd = call("GET", "/api/batches/MG-B1/history")
    print(f"[{st}] MG-B1 历史裁决版本:")
    for v in bd["history"]:
        print(f"      v{v['version']} {v['trigger']} -> {v['disposition']}（{v['decided_at']}）")

    section("8. 已发运批次仍按原交接追溯；版本变化进入发运后复核")
    st, bd = call("GET", "/api/batches/MG-B1/handover")
    print(f"[{st}] MG-B1 当前去向: shipped={bd['shipped']} "
          f"handover={bd['handover']['handover_no']}")
    st, bd = call("GET", "/api/handovers/MG-HO-1")
    frozen = bd["batches"][0]
    print(f"[{st}] 交接冻结快照不改写: 裁决 v{frozen['adjudication']['version']} / "
          f"出货资格 v{frozen['shipment_eligibility']['version']}")
    st, bd = call("GET", "/api/handovers/MG-HO-1/reviews")
    item = bd["batches"][0]
    print(f"[{st}] 发运后复核状态: {item['review_status']}（needs_review={item['needs_review']}）")
    seen = item["seen_state"]
    st, bd = call("POST", "/api/handovers/MG-HO-1/reviews/MG-B1/decision", {
        "seen_adjudication_version": seen["adjudication_version"],
        "seen_shipment_version": seen["shipment_version"],
        "seen_batch_voided": seen["batch_voided"],
        "disposition": "NO_RECALL",
        "reason": "批号并档改指，实为同一批原料；过敏原与成品声明未变，无需召回",
    })
    print(f"[{st}] 提交复核处置: review_id={bd.get('review_id')}")

    section("完成")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
