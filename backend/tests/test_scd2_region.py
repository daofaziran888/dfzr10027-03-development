"""SCD Type 2 区域维度历史的端到端测试。

覆盖需求：
1. 业务键 + 日期命中唯一维度版本，半开区间分界日只命中一个区域；
2. 客户换区后旧订单仍按事实发生时的区域聚合，地区合计 = 事实总额；
3. 字典刷新中断整体回滚，已确认口径/封账切片不被半成品记录改写，重试可成功；
4. 封账日报不可变，迟到改区确认经重算生成调整分录，周报引用快照+调整；
5. 题目场景：周四 80 元、周五 120 元，先按原地区封账，再补录周五起改区。
"""
from __future__ import annotations

import pytest

from app.services.scd2 import (
    CubeService,
    RegionDimService,
    ReportService,
    connect,
    init_schema,
)
from app.services.scd2.dim_service import RefreshError
from app.services.scd2.report_service import iso_week_key

# 题目时间线（2026-11-12 周四 / 2026-11-13 周五 / 2026-11-15 周日确认迟到改区）
THU = "2026-11-12"
FRI = "2026-11-13"
SUN = "2026-11-15"
BK = "CUST-001"
REGION_OLD = "华东区"
REGION_NEW = "华南区"


@pytest.fixture()
def conn():
    c = connect(None)
    init_schema(c)
    yield c
    c.close()


@pytest.fixture()
def dim(conn):
    return RegionDimService(conn)


@pytest.fixture()
def cube(conn):
    return CubeService(conn)


@pytest.fixture()
def reports(conn):
    return ReportService(conn)


def _seed_orders(cube):
    cube.add_order(1001, BK, THU, 80.0)
    cube.add_order(1002, BK, FRI, 120.0)


# ---------------------------------------------------------------- 区间不变量
class TestSCD2Intervals:
    def test_boundary_day_hits_exactly_one_version(self, dim):
        dim.load_initial_regions([(BK, REGION_OLD, "2026-01-01")])
        dim.apply_refresh(
            [{"region_bk": BK, "region_name": REGION_NEW, "effective_from": FRI}],
            batch_ref="BATCH-MOVE-1",
        )
        # 分界日（周五）前：旧区；分界日当天及之后：新区，且任何日期恰好一条
        assert dim.resolve_version(BK, THU).region_name == REGION_OLD
        friday = dim.resolve_version(BK, FRI)
        assert friday.region_name == REGION_NEW and friday.version_no == 2
        assert dim.resolve_version(BK, "2030-01-01").is_current is True

        versions = dim.list_versions(BK)
        assert versions[0].effective_to == versions[1].effective_from == FRI
        assert versions[0].is_current is False and versions[1].is_current is True

    def test_overlapping_chain_rejected(self, dim):
        dim.load_initial_regions([(BK, REGION_OLD, "2026-01-01")])
        with pytest.raises(RefreshError):
            dim.apply_refresh(
                [{"region_bk": BK, "region_name": REGION_NEW,
                  "effective_from": "2025-01-01"}],
                batch_ref="BATCH-BAD",
            )
        # 拒绝后仍只有初始版本
        assert [v.version_no for v in dim.list_versions(BK)] == [1]


# ---------------------------------------------------------------- 立方体聚合
class TestCubeAggregation:
    def test_facts_keep_region_at_occurrence_time(self, dim, cube):
        dim.load_initial_regions([(BK, REGION_OLD, "2026-01-01")])
        _seed_orders(cube)
        # 换区前：两笔都在华东区，合计 200
        slices = cube.region_sales()
        assert len(slices) == 1
        assert slices[0].region_name == REGION_OLD and slices[0].amount == 200.0

        dim.apply_refresh(
            [{"region_bk": BK, "region_name": REGION_NEW, "effective_from": FRI}],
            batch_ref="BATCH-MOVE-1",
        )
        # 换区后：周四 80 留在旧区，周五 120 命中新区——旧订单不被改写
        slices = cube.region_sales()
        by_name = {s.region_name: s.amount for s in slices}
        assert by_name == {REGION_OLD: 80.0, REGION_NEW: 120.0}
        # 完整地区合计与这批事实总额一致
        assert sum(s.amount for s in slices) == cube.total_amount() == 200.0

    def test_daily_slices_split_on_boundary(self, dim, cube):
        dim.load_initial_regions([(BK, REGION_OLD, "2026-01-01")])
        _seed_orders(cube)
        dim.apply_refresh(
            [{"region_bk": BK, "region_name": REGION_NEW, "effective_from": FRI}],
            batch_ref="BATCH-MOVE-1",
        )
        thu = {s.region_name: s.amount for s in cube.region_sales(THU, THU)}
        fri = {s.region_name: s.amount for s in cube.region_sales(FRI, FRI)}
        assert thu == {REGION_OLD: 80.0}
        assert fri == {REGION_NEW: 120.0}


# ---------------------------------------------------------------- 原子刷新
class TestAtomicRefresh:
    def test_interrupted_refresh_rolls_back_fully(self, dim, conn):
        dim.load_initial_regions([(BK, REGION_OLD, "2026-01-01"),
                                  ("CUST-002", "华北区", "2026-01-01")])
        changes = [
            {"region_bk": BK, "region_name": REGION_NEW, "effective_from": FRI},
            {"region_bk": "CUST-002", "region_name": "西南区", "effective_from": FRI},
        ]
        # 第一条写完后模拟中断
        with pytest.raises(RefreshError):
            dim.apply_refresh(changes, batch_ref="BATCH-MOVE-X", fail_after=1)

        # 两个业务键都停留在初始版本：没有写入一半的新记录
        assert [v.version_no for v in dim.list_versions(BK)] == [1]
        assert [v.version_no for v in dim.list_versions("CUST-002")] == [1]
        assert dim.resolve_version(BK, FRI).region_name == REGION_OLD
        # 没有 COMMITTED 半成品批次，只有回滚审计痕迹
        committed = [b for b in dim.batches() if b["status"] == "COMMITTED"]
        rolled = [b for b in dim.batches() if b["status"] == "ROLLED_BACK"]
        assert len(committed) == 1  # 初始装载
        assert len(rolled) == 1 and rolled[0]["change_count"] == 2

    def test_closed_snapshot_survives_interrupted_refresh(self, dim, cube, reports):
        dim.load_initial_regions([(BK, REGION_OLD, "2026-01-01")])
        _seed_orders(cube)
        report = reports.close_daily_report(FRI)
        frozen = {i.region_name: i.amount for i in report.items}
        assert frozen == {REGION_OLD: 120.0}

        with pytest.raises(RefreshError):
            dim.apply_refresh(
                [{"region_bk": BK, "region_name": REGION_NEW,
                  "effective_from": FRI}],
                batch_ref="BATCH-MOVE-X", fail_after=1,
            )
        # 封账切片原封不动，且仍按旧口径可重查
        again = reports.load_daily_report(report.snapshot_id)
        assert {i.region_name: i.amount for i in again.items} == {REGION_OLD: 120.0}

    def test_retry_same_batch_ref_succeeds(self, dim):
        dim.load_initial_regions([(BK, REGION_OLD, "2026-01-01"),
                                  ("CUST-002", "华北区", "2026-01-01")])
        changes = [
            {"region_bk": BK, "region_name": REGION_NEW, "effective_from": FRI},
            {"region_bk": "CUST-002", "region_name": "西南区", "effective_from": FRI},
        ]
        with pytest.raises(RefreshError):
            dim.apply_refresh(changes, batch_ref="BATCH-MOVE-2", fail_after=1)
        # 中断后用同一批次号重新提交
        batch_id = dim.apply_refresh(changes, batch_ref="BATCH-MOVE-2")
        assert dim.resolve_version(BK, FRI).region_name == REGION_NEW
        assert dim.resolve_version("CUST-002", FRI).region_name == "西南区"
        committed = [b for b in dim.batches() if b["status"] == "COMMITTED"
                     and b["batch_ref"] == "BATCH-MOVE-2"]
        assert len(committed) == 1 and committed[0]["batch_id"] == batch_id


# ---------------------------------------------------------------- 封账与调整
class TestCloseRecomputeAdjust:
    def test_full_scenario_close_then_late_move(self, dim, cube, reports):
        dim.load_initial_regions([(BK, REGION_OLD, "2026-01-01")])
        _seed_orders(cube)

        # 1) 周四、周五日报先后按原地区封账
        thu_report = reports.close_daily_report(THU)
        fri_report = reports.close_daily_report(FRI)
        assert {i.region_name: i.amount for i in thu_report.items} == {REGION_OLD: 80.0}
        assert {i.region_name: i.amount for i in fri_report.items} == {REGION_OLD: 120.0}

        # 封账前只收到改区通知但生效日迟到，账期已锁
        period = conn_execute(cube, "SELECT 1 FROM report_period_close "
                                    "WHERE period_type='DAILY' AND period_key=?", (FRI,))
        assert period is not None

        # 2) 封账后来源确认：改区从上周五（即 FRI）算起
        dim.apply_refresh(
            [{"region_bk": BK, "region_name": REGION_NEW, "effective_from": FRI}],
            batch_ref="BATCH-MOVE-LATE",
            note="来源事后确认，生效日回溯至周五",
        )

        # 3) 按更正后的业务日重算两份日报；封账快照本身不变
        thu_re = reports.recompute_daily(thu_report.snapshot_id)
        fri_re = reports.recompute_daily(fri_report.snapshot_id)
        assert {i.region_name: i.amount for i in thu_re.recomputed_items} == {REGION_OLD: 80.0}
        assert {i.region_name: i.amount for i in fri_re.recomputed_items} == {REGION_NEW: 120.0}
        assert len(thu_re.adjustments) == 0          # 周四订单不受影响
        assert len(fri_re.adjustments) == 1
        adj = fri_re.adjustments[0]
        assert adj["order_id"] == 1002 and adj["amount"] == 120.0
        assert adj["region_name_old"] == REGION_OLD
        assert adj["region_name_new"] == REGION_NEW

        # 封账快照仍是旧口径（不可变），财务可找回封账金额
        frozen_fri = reports.load_daily_report(fri_report.snapshot_id)
        assert {i.region_name: i.amount for i in frozen_fri.items} == {REGION_OLD: 120.0}
        assert frozen_fri.total_amount == 120.0

        # 4) 调整分录记入确认日（周日）所在账期，生效日记录为更正业务日周五
        ids = reports.post_adjustments(fri_re, confirmed_on=SUN)
        assert len(ids) == 1
        entry = conn_execute(cube, "SELECT * FROM adjustment_entry WHERE adjustment_id=?",
                             (ids[0],))
        assert entry["effective_date"] == FRI
        assert entry["posting_period"] == SUN
        assert entry["amount"] == 120.0
        assert entry["region_name_old"] == REGION_OLD
        assert entry["region_name_new"] == REGION_NEW

        # 重复入账被拒绝（只追加）
        with pytest.raises(Exception):
            reports.post_adjustments(fri_re, confirmed_on=SUN)

        # 5) 周报引用封账快照 + 调整：华东 200-120=80，华南 0+120=120，总额仍 200
        week = iso_week_key(FRI)
        weekly = reports.weekly_report(week)
        by_name = {r["region_name"]: r["amount"] for r in weekly["reported_regions"]}
        assert by_name == {REGION_OLD: 80.0, REGION_NEW: 120.0}
        assert weekly["reported_total"] == weekly["frozen_daily_total"] == 200.0
        assert weekly["adjustment_net"] == 0.0
        assert set(weekly["referenced_snapshot_ids"]) == {
            thu_report.snapshot_id, fri_report.snapshot_id
        }
        # 周报里能直接核对差额记入的账期与调整日期
        assert weekly["adjustments"][0]["posting_period"] == SUN
        assert weekly["adjustments"][0]["effective_date"] == FRI

    def test_close_is_idempotent_and_immutable(self, dim, cube, reports):
        dim.load_initial_regions([(BK, REGION_OLD, "2026-01-01")])
        _seed_orders(cube)
        first = reports.close_daily_report(THU)
        second = reports.close_daily_report(THU)
        assert first.snapshot_id == second.snapshot_id


def conn_execute(cube, sql, params):
    return cube.conn.execute(sql, params).fetchone()
