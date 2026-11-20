"""立方体服务：事实表装载与「按各事实行命中的维度版本」聚合地区销售额。

关联方式（业务键 + 业务日期命中 SCD2 版本）::

    fact f JOIN dim_region_hist d
      ON f.region_bk = d.region_bk
     AND f.business_date >= d.effective_from
     AND f.business_date <  d.effective_to

半开区间保证每条事实恰好命中一条维度版本，因此各地区切片之和必然等于事实总额
（代码在每次聚合后显式复检这条对账不变量）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


class CubeError(RuntimeError):
    """立方体计算异常（如事实行命中 0 条或多条维度版本、对账不平）。"""


@dataclass(frozen=True)
class RegionSlice:
    region_sk: int
    region_bk: str
    region_name: str
    amount: float
    order_count: int


class CubeService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -------------------------------------------------------------- 事实装载
    def add_order(self, order_id: int, region_bk: str, business_date: str, amount: float) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO fact_sales_order(order_id, region_bk, business_date, amount) "
                "VALUES (?, ?, ?, ?)",
                (order_id, region_bk, business_date, amount),
            )

    # -------------------------------------------------------------- 立方体聚合
    def region_sales(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> list[RegionSlice]:
        """按各事实行命中的维度版本计算地区销售额。

        可选日期范围为闭区间 ``[date_from, date_to]``（账期筛选）。
        """
        where = ["1 = 1"]
        params: list[object] = []
        if date_from is not None:
            where.append("f.business_date >= ?")
            params.append(date_from)
        if date_to is not None:
            where.append("f.business_date <= ?")
            params.append(date_to)
        sql = f"""
            SELECT d.region_sk, d.region_bk, d.region_name,
                   SUM(f.amount) AS amount, COUNT(*) AS order_count
            FROM fact_sales_order f
            JOIN dim_region_hist d
              ON f.region_bk = d.region_bk
             AND f.business_date >= d.effective_from
             AND f.business_date <  d.effective_to
            WHERE {' AND '.join(where)}
            GROUP BY d.region_sk
            ORDER BY d.region_sk
        """
        slices = [
            RegionSlice(
                region_sk=row["region_sk"],
                region_bk=row["region_bk"],
                region_name=row["region_name"],
                amount=round(row["amount"], 2),
                order_count=row["order_count"],
            )
            for row in self.conn.execute(sql, params).fetchall()
        ]
        self._assert_reconciles(slices, date_from, date_to)
        return slices

    def total_amount(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> float:
        where = ["1 = 1"]
        params: list[object] = []
        if date_from is not None:
            where.append("business_date >= ?")
            params.append(date_from)
        if date_to is not None:
            where.append("business_date <= ?")
            params.append(date_to)
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) AS total FROM fact_sales_order "
            f"WHERE {' AND '.join(where)}",
            params,
        ).fetchone()
        return round(row["total"], 2)

    def _assert_reconciles(
        self,
        slices: list[RegionSlice],
        date_from: str | None,
        date_to: str | None,
    ) -> None:
        """对账不变量：完整地区合计必须与本批事实总额完全一致。"""
        fact_total = self.total_amount(date_from, date_to)
        slice_total = round(sum(s.amount for s in slices), 2)
        # 同时检查是否有事实行命中不到任何维度版本（LEFT JOIN 反连接）。
        where = ["d.region_sk IS NULL"]
        params: list[object] = []
        if date_from is not None:
            where.append("f.business_date >= ?")
            params.append(date_from)
        if date_to is not None:
            where.append("f.business_date <= ?")
            params.append(date_to)
        orphan = self.conn.execute(
            "SELECT COUNT(*) AS c FROM fact_sales_order f "
            "LEFT JOIN dim_region_hist d ON f.region_bk = d.region_bk "
            "AND f.business_date >= d.effective_from AND f.business_date < d.effective_to "
            f"WHERE {' AND '.join(where)}",
            params,
        ).fetchone()["c"]
        if orphan:
            raise CubeError(f"有 {orphan} 笔事实无法命中任何区域维度版本")
        if slice_total != fact_total:
            raise CubeError(
                f"地区合计 {slice_total} 与事实总额 {fact_total} 不一致，立方体对账不平"
            )

    # -------------------------------------------------------------- 明细命中查询
    def order_hits(self, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        """返回每笔事实命中的维度版本，供重算切片与调整分录逐笔核对。"""
        where = ["1 = 1"]
        params: list[object] = []
        if date_from is not None:
            where.append("f.business_date >= ?")
            params.append(date_from)
        if date_to is not None:
            where.append("f.business_date <= ?")
            params.append(date_to)
        rows = self.conn.execute(
            f"""
            SELECT f.order_id, f.region_bk, f.business_date, f.amount,
                   d.region_sk, d.region_name, d.version_no
            FROM fact_sales_order f
            JOIN dim_region_hist d
              ON f.region_bk = d.region_bk
             AND f.business_date >= d.effective_from
             AND f.business_date <  d.effective_to
            WHERE {' AND '.join(where)}
            ORDER BY f.order_id
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
