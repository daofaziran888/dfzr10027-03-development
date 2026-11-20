#!/usr/bin/env python3
"""场景回放：区域字典 SCD2 改造 · 周四 80 / 周五 120 改区案例。

用独立 SQLite 文件持久化，按题目时间线逐步执行并打印核对结果：

  周四 2026-11-12  订单 1001，80 元，客户 CUST-001 属华东区
  周五 2026-11-13  订单 1002，120 元，客户 CUST-001 属华东区（封账前只收到改区通知，生效日迟到）
  周五当日         两份日报按原地区封账
  周日 2026-11-15  来源确认改区从上周五算起 -> 刷新中断一次 -> 重新提交
  周日             按更正业务日重算，调整分录记入周日账期，周报引用快照+调整

运行（在 backend/ 目录下）：
    python3 -I scripts/scd2_region_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 脚本与 backend 包分离：显式把 backend 根目录加进 sys.path，python -I 下也能导入。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.scd2 import (  # noqa: E402
    CubeService,
    RegionDimService,
    ReportService,
    connect,
    init_schema,
)
from app.services.scd2.dim_service import RefreshError  # noqa: E402
from app.services.scd2.report_service import iso_week_key  # noqa: E402

THU, FRI, SUN = "2026-11-12", "2026-11-13", "2026-11-15"
BK, OLD, NEW = "CUST-001", "华东区", "华南区"
DB_PATH = BACKEND_ROOT / "scripts" / "_scd2_demo.db"


def hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def show_slices(title: str, slices) -> None:
    print(f"-- {title}")
    for s in slices:
        print(f"   region_sk={s.region_sk:<3} {s.region_name:<5} 金额={s.amount:>8.2f}")
    print(f"   地区合计 = {sum(s.amount for s in slices):.2f}")


def main() -> int:
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect(DB_PATH)
    init_schema(conn)
    dim, cube, reports = RegionDimService(conn), CubeService(conn), ReportService(conn)

    hr("阶段 0  初始区域字典（v1 开放区间）+ 两笔事实")
    dim.load_initial_regions([(BK, OLD, "2026-01-01")])
    cube.add_order(1001, BK, THU, 80.0)
    cube.add_order(1002, BK, FRI, 120.0)
    for v in dim.list_versions(BK):
        print(f"   v{v.version_no} {v.region_name} [{v.effective_from}, {v.effective_to})")
    show_slices("立方体地区销售额（换区前，全部命中 v1）", cube.region_sales())

    hr("阶段 1  周四、周五日报按原地区封账（封账后切片冻结）")
    thu_report = reports.close_daily_report(THU)
    fri_report = reports.close_daily_report(FRI)
    for r in (thu_report, fri_report):
        print(f"   日报 {r.period_key} 封账 snapshot={r.snapshot_id} "
              f"总额={r.total_amount:.2f} 切片="
              + ", ".join(f"{i.region_name}:{i.amount:.2f}" for i in r.items))

    hr("阶段 2  封账后来源确认改区自周五生效；第一次刷新中断，整体回滚")
    change = [{"region_bk": BK, "region_name": NEW, "effective_from": FRI}]
    try:
        dim.apply_refresh(change, batch_ref="BATCH-MOVE-LATE", fail_after=1)
    except RefreshError as exc:
        print(f"   刷新中断：{exc}")
    print("   回滚后维度版本（仍只有 v1，封账口径未被半成品改写）：")
    for v in dim.list_versions(BK):
        print(f"   v{v.version_no} {v.region_name} [{v.effective_from}, {v.effective_to})")
    print("   批次台账：", [(b["batch_ref"], b["status"]) for b in dim.batches()])

    hr("阶段 3  中断后用同一批次号重新提交，SCD2 v2 生效")
    dim.apply_refresh(change, batch_ref="BATCH-MOVE-LATE",
                      note="来源事后确认，生效日回溯至周五")
    for v in dim.list_versions(BK):
        flag = "当前" if v.is_current else "历史"
        print(f"   v{v.version_no} {v.region_name} [{v.effective_from}, {v.effective_to}) {flag}")
    boundary = dim.resolve_version(BK, FRI)
    print(f"   分界日 {FRI} 唯一命中：{boundary.region_name} v{boundary.version_no}")
    print(f"   分界日前 {THU} 命中：{dim.resolve_version(BK, THU).region_name}")

    hr("阶段 4  立方体按各事实行命中的维度版本重算（旧订单不被改写）")
    show_slices("全量地区销售额", cube.region_sales())
    show_slices(f"周四 {THU} 重算切片", cube.region_sales(THU, THU))
    show_slices(f"周五 {FRI} 重算切片", cube.region_sales(FRI, FRI))
    assert cube.total_amount() == sum(s.amount for s in cube.region_sales()) == 200.0
    print("   对账不变量：地区合计 == 事实总额 200.00 ✓")

    hr("阶段 5  封账快照找回 + 按更正业务日重算对比")
    thu_re = reports.recompute_daily(thu_report.snapshot_id)
    fri_re = reports.recompute_daily(fri_report.snapshot_id)
    print("   【封账冻结口径（永不改写）】")
    print(f"   周四：{[(i.region_name, i.amount) for i in thu_re.closed_items]}")
    print(f"   周五：{[(i.region_name, i.amount) for i in fri_re.closed_items]}")
    print("   【更正后重算口径】")
    print(f"   周四：{[(i.region_name, i.amount) for i in thu_re.recomputed_items]}（无调整）")
    print(f"   周五：{[(i.region_name, i.amount) for i in fri_re.recomputed_items]}")
    print(f"   重算发现调整 {len(fri_re.adjustments)} 笔：{fri_re.adjustments}")

    hr("阶段 6  调整分录入账：生效日=周五（更正业务日），账期=周日（确认日）")
    ids = reports.post_adjustments(fri_re, confirmed_on=SUN)
    entry = conn.execute(
        "SELECT * FROM adjustment_entry WHERE adjustment_id=?", (ids[0],)
    ).fetchone()
    for key in ("order_id", "region_name_old", "region_name_new", "amount",
                "effective_date", "posting_period"):
        print(f"   {key:<16} = {entry[key]}")

    hr("阶段 7  周报引用封账日报 + 调整分录")
    week = iso_week_key(FRI)
    weekly = reports.weekly_report(week)
    print(f"   周报账期 {week}，引用封账快照 {weekly['referenced_snapshot_ids']}")
    print(f"   封账日报冻结总额 = {weekly['frozen_daily_total']:.2f}")
    for r in weekly["reported_regions"]:
        print(f"   周报地区金额：{r['region_name']} = {r['amount']:.2f}")
    print(f"   调整净额 = {weekly['adjustment_net']:.2f}（地区间搬运，总额不变）")
    print(f"   周报最终总额 = {weekly['reported_total']:.2f}")
    a = weekly["adjustments"][0]
    print(f"   旧订单 {a['order_id']} 差额 {a['amount']:.0f} 元："
          f"{a['region_name_old']} -> {a['region_name_new']}，"
          f"调整日期(生效日)={a['effective_date']}，记账账期={a['posting_period']}")

    hr("核对结论")
    expected = {OLD: 80.0, NEW: 120.0}
    actual = {r["region_name"]: r["amount"] for r in weekly["reported_regions"]}
    checks = [
        ("封账日报保留旧口径 华东:120",
         {i.region_name: i.amount for i in fri_report.items} == {OLD: 120.0}),
        ("周五重算切片为新口径 华南:120",
         {i.region_name: i.amount for i in fri_re.recomputed_items} == {NEW: 120.0}),
        ("周报地区金额 华东:80 / 华南:120", actual == expected),
        ("周报总额与事实总额一致 200.00", weekly["reported_total"] == 200.0),
        ("调整记入确认日账期 2026-11-15", a["posting_period"] == SUN),
        ("调整生效日回溯至周五 2026-11-13", a["effective_date"] == FRI),
    ]
    ok = True
    for name, passed in checks:
        ok &= passed
        print(f"   [{'✓' if passed else '✗'}] {name}")
    conn.close()
    print("\n全部核对通过。" if ok else "\n存在失败项！")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
