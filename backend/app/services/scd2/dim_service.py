"""区域维度 SCD Type 2 服务：版本区间管理与原子刷新。

区间约定（半开区间）::

    v1: [effective_from_1, effective_from_2)
    v2: [effective_from_2, effective_from_3)
    ...
    vn: [effective_from_n, 9999-12-31)         -- is_current = 1

分界日 ``effective_from_2`` 当天只命中 v2：旧版本右端点等于新版本左端点，
配合 ``effective_from <= d < effective_to`` 的命中条件，任何业务日有且仅有一条版本。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.services.scd2.schema import OPEN_ENDED


class RefreshError(RuntimeError):
    """字典刷新被拒绝（校验失败或中途故障，整批已回滚）。"""


@dataclass(frozen=True)
class DimVersion:
    region_sk: int
    region_bk: str
    region_name: str
    effective_from: str
    effective_to: str
    is_current: bool
    version_no: int


class RegionDimService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ 初始化
    def load_initial_regions(
        self, regions: list[tuple[str, str, str]], batch_ref: str = "BATCH-INIT"
    ) -> None:
        """首次装载区域字典，每个业务键写入 version_no=1 的当前版本。"""
        with self.conn:  # 自动提交/回滚
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO dim_refresh_batch(batch_ref, status, change_count, note) "
                "VALUES (?, 'COMMITTED', ?, '初始装载')",
                (batch_ref, len(regions)),
            )
            batch_id = cur.lastrowid
            for region_bk, region_name, effective_from in regions:
                cur.execute(
                    "INSERT INTO dim_region_hist(region_bk, region_name, effective_from, "
                    "effective_to, is_current, version_no, batch_id) "
                    "VALUES (?, ?, ?, ?, 1, 1, ?)",
                    (region_bk, region_name, effective_from, OPEN_ENDED, batch_id),
                )

    # -------------------------------------------------------------- 维度命中
    def resolve_version(self, region_bk: str, business_date: str) -> DimVersion:
        """按业务键 + 业务日期命中唯一维度版本（事实发生时的口径）。"""
        rows = self.conn.execute(
            "SELECT * FROM dim_region_hist "
            "WHERE region_bk = ? AND effective_from <= ? AND effective_to > ?",
            (region_bk, business_date, business_date),
        ).fetchall()
        if len(rows) != 1:
            raise RefreshError(
                f"业务键 {region_bk} 在 {business_date} 命中 {len(rows)} 条维度版本，"
                "区间不变量被破坏（应为恰好 1 条）"
            )
        row = rows[0]
        return DimVersion(
            region_sk=row["region_sk"],
            region_bk=row["region_bk"],
            region_name=row["region_name"],
            effective_from=row["effective_from"],
            effective_to=row["effective_to"],
            is_current=bool(row["is_current"]),
            version_no=row["version_no"],
        )

    # ------------------------------------------------------------ 原子刷新
    def apply_refresh(
        self,
        changes: list[dict[str, str]],
        batch_ref: str,
        note: str = "",
        fail_after: int | None = None,
    ) -> int:
        """在一个事务里提交整批改区通知，返回批次 id。

        ``changes`` 每项：``{"region_bk", "region_name", "effective_from"}``。
        任何一条校验失败、或在 ``fail_after`` 条后模拟故障，整批回滚：
        已确认的历史口径与封账切片不会被写入一半的记录改写。
        回滚后以同一 ``batch_ref`` 重新提交是合法的（幂等重试）。
        """
        if not changes:
            raise RefreshError("刷新批次不允许为空")
        cur = self.conn.cursor()
        try:
            # BEGIN IMMEDIATE：立刻拿库锁，避免两个刷新批次交叉写入。
            cur.execute("BEGIN IMMEDIATE")
            # 批次头与维度版本在同一事务：提交失败时整头一起回滚，
            # 不会留下状态为「半成品」的批次。
            cur.execute(
                "INSERT INTO dim_refresh_batch(batch_ref, status, change_count, note) "
                "VALUES (?, 'COMMITTED', ?, ?)",
                (batch_ref, len(changes), note),
            )
            batch_id = cur.lastrowid
            written = 0
            for change in changes:
                self._apply_one_change(cur, change, batch_id)
                written += 1
                if fail_after is not None and written >= fail_after:
                    # 模拟刷新中断（进程崩溃 / 连接断开）：异常跳出，with 块回滚。
                    raise RuntimeError("模拟字典刷新中断：连接在写入中途断开")
            # 提交前对本批次涉及的业务键做区间不变量复检。
            for change in changes:
                self._assert_chain_integrity(cur, change["region_bk"])
            cur.execute(
                "UPDATE dim_refresh_batch SET finished_at = datetime('now') "
                "WHERE batch_id = ?",
                (batch_id,),
            )
            self.conn.commit()
            return batch_id
        except Exception as exc:
            self.conn.rollback()
            self._record_rollback(batch_ref, changes, exc)
            if isinstance(exc, RefreshError):
                raise
            raise RefreshError(f"刷新批次 {batch_ref} 中断，已整体回滚：{exc}") from exc

    def _apply_one_change(
        self, cur: sqlite3.Cursor, change: dict[str, str], batch_id: int
    ) -> None:
        region_bk = change["region_bk"]
        region_name = change["region_name"]
        effective_from = change["effective_from"]
        current = cur.execute(
            "SELECT * FROM dim_region_hist WHERE region_bk = ? AND is_current = 1",
            (region_bk,),
        ).fetchall()
        if len(current) != 1:
            raise RefreshError(
                f"业务键 {region_bk} 的当前版本数为 {len(current)}，无法承接改区通知"
            )
        cur_row = current[0]
        if effective_from <= cur_row["effective_from"]:
            raise RefreshError(
                f"业务键 {region_bk} 新生效日 {effective_from} 不能早于/等于当前版本生效日 "
                f"{cur_row['effective_from']}（不允许改写已存在版本的起点）"
            )
        next_version = cur_row["version_no"] + 1
        # 1) 关闭旧版本：右端点 = 新版本生效日（分界日不再命中旧版）
        cur.execute(
            "UPDATE dim_region_hist SET effective_to = ?, is_current = 0 "
            "WHERE region_sk = ?",
            (effective_from, cur_row["region_sk"]),
        )
        # 2) 写入新版本，挂到本批次头
        cur.execute(
            "INSERT INTO dim_region_hist(region_bk, region_name, effective_from, "
            "effective_to, is_current, version_no, batch_id) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (region_bk, region_name, effective_from, OPEN_ENDED, next_version, batch_id),
        )

    def _assert_chain_integrity(self, cur: sqlite3.Cursor, region_bk: str) -> None:
        """区间链复检：相邻版本首尾相接、无重叠无空档、恰好一个当前版本。"""
        rows = cur.execute(
            "SELECT * FROM dim_region_hist WHERE region_bk = ? ORDER BY version_no",
            (region_bk,),
        ).fetchall()
        if not rows:
            raise RefreshError(f"业务键 {region_bk} 没有任何维度版本")
        currents = [r for r in rows if r["is_current"] == 1]
        if len(currents) != 1 or rows[-1]["is_current"] != 1:
            raise RefreshError(f"业务键 {region_bk} 当前版本不唯一")
        for prev, nxt in zip(rows, rows[1:]):
            if prev["effective_to"] != nxt["effective_from"]:
                raise RefreshError(
                    f"业务键 {region_bk} 版本 v{prev['version_no']}/v{nxt['version_no']} "
                    f"区间未首尾相接：{prev['effective_to']} != {nxt['effective_from']}"
                )
        if rows[-1]["effective_to"] != OPEN_ENDED:
            raise RefreshError(f"业务键 {region_bk} 最新版本必须是开放区间")
        # SQL 级重叠检测：任何一天都不允许命中两条版本。
        overlap = cur.execute(
            "SELECT COUNT(*) AS c FROM dim_region_hist a JOIN dim_region_hist b "
            "ON a.region_bk = b.region_bk AND a.region_sk < b.region_sk "
            "AND a.effective_from < b.effective_to AND b.effective_from < a.effective_to "
            "WHERE a.region_bk = ?",
            (region_bk,),
        ).fetchone()["c"]
        if overlap:
            raise RefreshError(f"业务键 {region_bk} 存在重叠的维度区间")

    def _record_rollback(
        self, batch_ref: str, changes: list[dict[str, str]], exc: Exception
    ) -> None:
        """回滚后留一条 ROLLED_BACK 审计痕迹（独立事务，不污染原批次号的重试）。"""
        audit_ref = f"{batch_ref}#rolledback"
        suffix = 1
        while self.conn.execute(
            "SELECT 1 FROM dim_refresh_batch WHERE batch_ref = ?", (audit_ref,)
        ).fetchone():
            suffix += 1
            audit_ref = f"{batch_ref}#rolledback{suffix}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO dim_refresh_batch(batch_ref, status, change_count, note, finished_at) "
                "VALUES (?, 'ROLLED_BACK', ?, ?, datetime('now'))",
                (audit_ref, len(changes), f"回滚原因：{exc}"),
            )

    # -------------------------------------------------------------- 查询辅助
    def list_versions(self, region_bk: str) -> list[DimVersion]:
        rows = self.conn.execute(
            "SELECT * FROM dim_region_hist WHERE region_bk = ? ORDER BY version_no",
            (region_bk,),
        ).fetchall()
        return [
            DimVersion(
                region_sk=r["region_sk"],
                region_bk=r["region_bk"],
                region_name=r["region_name"],
                effective_from=r["effective_from"],
                effective_to=r["effective_to"],
                is_current=bool(r["is_current"]),
                version_no=r["version_no"],
            )
            for r in rows
        ]

    def batches(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM dim_refresh_batch ORDER BY batch_id"
        ).fetchall()
