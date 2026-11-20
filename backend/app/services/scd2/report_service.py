"""封账、重算与调整服务。

封账语义（不可变历史 + 调整入账）：

1. ``close_daily_report`` 把封账时点立方体按旧口径算出的地区切片与订单级命中
   整体冻结成快照，并标记账期封账；封账后快照行永不再改。
2. 迟到的改区确认经维度刷新写入 SCD2 后，``recompute_daily`` 用更正后的业务日
   重新命中维度版本，得到重算切片；它只用于对比，不覆盖封账快照。
3. 新旧口径差额生成只追加的调整分录：金额从旧地区转出、转入新地区，
   生效日取来源确认的更正业务日，记账账期取确认日（封账之后）。
4. 周报引用「封账日报快照 + 落在本周的调整分录」，既能找回封账金额，
   也能核对每笔旧订单差额最终记入了哪个账期。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from app.services.scd2.cube_service import CubeService, RegionSlice
from app.services.scd2.dim_service import RegionDimService


class ReportError(RuntimeError):
    """封账/重算/调整流程异常。"""


@dataclass
class DailyReport:
    snapshot_id: int
    period_key: str
    total_amount: float
    items: list[RegionSlice]
    order_hits: list[dict]


@dataclass
class RecomputeResult:
    period_key: str
    closed_snapshot_id: int
    closed_items: list[RegionSlice]
    recomputed_items: list[RegionSlice]
    adjustments: list[dict] = field(default_factory=list)


class ReportService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.cube = CubeService(conn)
        self.dim = RegionDimService(conn)

    # ------------------------------------------------------------ 日报封账
    def close_daily_report(self, business_day: str) -> DailyReport:
        """按封账时点口径冻结日报。已封账的账期重复调用直接返回原快照。"""
        existing = self.conn.execute(
            "SELECT snapshot_id FROM report_snapshot "
            "WHERE report_type = 'DAILY' AND period_key = ?",
            (business_day,),
        ).fetchone()
        if existing:
            return self.load_daily_report(existing["snapshot_id"])

        slices = self.cube.region_sales(business_day, business_day)
        hits = self.cube.order_hits(business_day, business_day)
        total = self.cube.total_amount(business_day, business_day)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO report_snapshot(report_type, period_key, status, total_amount, "
                "cube_batch_note) VALUES ('DAILY', ?, 'CLOSED', ?, ?)",
                (business_day, total, f"封账时点口径，订单数 {len(hits)}"),
            )
            snapshot_id = cur.lastrowid
            for s in slices:
                cur.execute(
                    "INSERT INTO report_snapshot_item(snapshot_id, region_sk, region_bk, "
                    "region_name, amount) VALUES (?, ?, ?, ?, ?)",
                    (snapshot_id, s.region_sk, s.region_bk, s.region_name, s.amount),
                )
            for h in hits:
                cur.execute(
                    "INSERT INTO report_snapshot_order(snapshot_id, order_id, region_sk, "
                    "region_name, amount, business_date) VALUES (?, ?, ?, ?, ?, ?)",
                    (snapshot_id, h["order_id"], h["region_sk"], h["region_name"],
                     h["amount"], h["business_date"]),
                )
            cur.execute(
                "INSERT INTO report_period_close(period_type, period_key) "
                "VALUES ('DAILY', ?)",
                (business_day,),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.load_daily_report(snapshot_id)

    def load_daily_report(self, snapshot_id: int) -> DailyReport:
        head = self.conn.execute(
            "SELECT * FROM report_snapshot WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if head is None:
            raise ReportError(f"快照 {snapshot_id} 不存在")
        items = [
            RegionSlice(
                region_sk=r["region_sk"], region_bk=r["region_bk"],
                region_name=r["region_name"], amount=r["amount"], order_count=0,
            )
            for r in self.conn.execute(
                "SELECT * FROM report_snapshot_item WHERE snapshot_id = ? ORDER BY item_id",
                (snapshot_id,),
            ).fetchall()
        ]
        hits = [dict(r) for r in self.conn.execute(
            "SELECT * FROM report_snapshot_order WHERE snapshot_id = ? ORDER BY order_id",
            (snapshot_id,),
        ).fetchall()]
        return DailyReport(
            snapshot_id=snapshot_id, period_key=head["period_key"],
            total_amount=head["total_amount"], items=items, order_hits=hits,
        )

    # ------------------------------------------------------------ 更正重算
    def recompute_daily(self, snapshot_id: int) -> RecomputeResult:
        """用更正后的 SCD2 口径重算封账日报对应的业务日，对比新旧地区金额。

        只计算对比结果，不修改封账快照（封账不可变）。
        """
        report = self.load_daily_report(snapshot_id)
        day = report.period_key
        new_slices = self.cube.region_sales(day, day)
        new_hits = {h["order_id"]: h for h in self.cube.order_hits(day, day)}
        adjustments: list[dict] = []
        for frozen in report.order_hits:
            current = new_hits[frozen["order_id"]]
            if current["region_sk"] != frozen["region_sk"]:
                adjustments.append({
                    "order_id": frozen["order_id"],
                    "amount": frozen["amount"],
                    "business_date": frozen["business_date"],
                    "region_sk_old": frozen["region_sk"],
                    "region_name_old": frozen["region_name"],
                    "region_sk_new": current["region_sk"],
                    "region_name_new": current["region_name"],
                })
        # 重算总额必须与封账总额一致：改区只搬运口径，不改变事实金额。
        new_total = round(sum(s.amount for s in new_slices), 2)
        if new_total != report.total_amount:
            raise ReportError(
                f"重算总额 {new_total} 与封账总额 {report.total_amount} 不一致"
            )
        return RecomputeResult(
            period_key=day, closed_snapshot_id=snapshot_id,
            closed_items=report.items, recomputed_items=new_slices,
            adjustments=adjustments,
        )

    # ------------------------------------------------------------ 调整入账
    def post_adjustments(
        self, recompute: RecomputeResult, confirmed_on: str
    ) -> list[int]:
        """把重算发现的差额记为调整分录，账期 = 确认日（封账之后的账期）。"""
        ids: list[int] = []
        posting_period = confirmed_on  # 日报粒度的账期键
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.cursor()
            for adj in recompute.adjustments:
                # 同一快照同一订单只允许调整一次（只追加、不可重复入账）。
                dup = cur.execute(
                    "SELECT 1 FROM adjustment_entry "
                    "WHERE source_snapshot_id = ? AND order_id = ?",
                    (recompute.closed_snapshot_id, adj["order_id"]),
                ).fetchone()
                if dup:
                    raise ReportError(
                        f"订单 {adj['order_id']} 的调整已入账，不允许重复记账"
                    )
                cur.execute(
                    "INSERT INTO adjustment_entry(source_snapshot_id, order_id, "
                    "region_sk_old, region_sk_new, region_name_old, region_name_new, "
                    "amount, effective_date, posting_period) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (recompute.closed_snapshot_id, adj["order_id"],
                     adj["region_sk_old"], adj["region_sk_new"],
                     adj["region_name_old"], adj["region_name_new"],
                     adj["amount"], adj["business_date"], posting_period),
                )
                ids.append(cur.lastrowid)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return ids

    # ------------------------------------------------------------ 周报引用
    def weekly_report(self, week_key: str) -> dict:
        """周报 = 本周封账日报快照（冻结口径）+ 本周记账的调整分录。

        ``reported_regions`` 给出周报实际呈现的地区金额：封账金额按调整方向
        从旧地区转出、转入新地区；总金额不变（调整净额为 0）。
        """
        snapshots = self.conn.execute(
            "SELECT * FROM report_snapshot "
            "WHERE report_type = 'DAILY' AND substr(period_key, 1, 10) "
            "BETWEEN ? AND ? ORDER BY period_key",
            (_week_start(week_key), _week_end(week_key)),
        ).fetchall()
        region_amount: dict[tuple[int, str], float] = {}
        frozen_total = 0.0
        for snap in snapshots:
            frozen_total += snap["total_amount"]
            for item in self.conn.execute(
                "SELECT * FROM report_snapshot_item WHERE snapshot_id = ?",
                (snap["snapshot_id"],),
            ).fetchall():
                key = (item["region_sk"], item["region_name"])
                region_amount[key] = round(
                    region_amount.get(key, 0.0) + item["amount"], 2
                )
        adjustments = [dict(r) for r in self.conn.execute(
            "SELECT * FROM adjustment_entry WHERE posting_period BETWEEN ? AND ? "
            "ORDER BY adjustment_id",
            (_week_start(week_key), _week_end(week_key)),
        ).fetchall()]
        for adj in adjustments:
            old_key = (adj["region_sk_old"], adj["region_name_old"])
            new_key = (adj["region_sk_new"], adj["region_name_new"])
            region_amount[old_key] = round(
                region_amount.get(old_key, 0.0) - adj["amount"], 2
            )
            region_amount[new_key] = round(
                region_amount.get(new_key, 0.0) + adj["amount"], 2
            )
        reported = [
            {"region_sk": sk, "region_name": name, "amount": round(amount, 2)}
            for (sk, name), amount in sorted(region_amount.items(), key=lambda kv: kv[0][0])
        ]
        reported_total = round(sum(r["amount"] for r in reported), 2)
        return {
            "week_key": week_key,
            "frozen_daily_total": round(frozen_total, 2),
            "adjustment_net": round(
                sum(a["amount"] for a in adjustments)
                - sum(a["amount"] for a in adjustments), 2
            ),
            "reported_total": reported_total,
            "reported_regions": reported,
            "referenced_snapshot_ids": [s["snapshot_id"] for s in snapshots],
            "adjustments": adjustments,
        }


# ------------------------------------------------------------ ISO 周工具
def iso_week_key(day: str) -> str:
    d = date.fromisoformat(day)
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _week_start(week_key: str) -> str:
    year, week = week_key.split("-W")
    return date.fromisocalendar(int(year), int(week), 1).isoformat()


def _week_end(week_key: str) -> str:
    year, week = week_key.split("-W")
    return date.fromisocalendar(int(year), int(week), 7).isoformat()
