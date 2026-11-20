"""SCD2 区域维度子系统的 SQLite 表结构。

只用标准库 sqlite3，表结构刻意贴近真实数仓：

- ``dim_region_hist``：区域维度历史表（缓慢变化维 Type 2）
- ``dim_refresh_batch``：字典刷新批次表，记录每次刷新的提交/回滚状态
- ``fact_sales_order``：销售事实表，金额与业务日期不可变
- ``report_snapshot`` / ``report_snapshot_item``：封账报表及冻结切片
- ``adjustment_entry``：迟到口径确认产生的调整分录
- ``report_period_close``：账期封账标记，封账后不允许再改该账期的快照
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# 开放区间的右端点哨兵：9999-12-31（ISO 日期字符串可直接比较）。
OPEN_ENDED = "9999-12-31"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- 区域维度历史表（SCD Type 2）
CREATE TABLE IF NOT EXISTS dim_region_hist (
    region_sk        INTEGER PRIMARY KEY AUTOINCREMENT,          -- 代理键（维度版本键）
    region_bk        TEXT    NOT NULL,                           -- 业务键（客户/区域字典主键）
    region_name      TEXT    NOT NULL,                           -- 区域名称（该版本口径）
    effective_from   TEXT    NOT NULL,                           -- 生效日（含）
    effective_to     TEXT    NOT NULL,                           -- 失效日（不含），OPEN_ENDED 表示当前版本
    is_current       INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    version_no       INTEGER NOT NULL,                           -- 同一业务键下的版本号，从 1 开始
    batch_id         INTEGER NOT NULL,                           -- 由哪个刷新批次写入
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (batch_id) REFERENCES dim_refresh_batch(batch_id)
);

-- 同一业务键的历史区间互不重叠、不留空档：
-- 相邻版本满足「上一版 effective_to = 下一版 effective_from」，半开区间保证分界日唯一命中。
CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_region_version
    ON dim_region_hist(region_bk, version_no);
CREATE INDEX IF NOT EXISTS ix_dim_region_lookup
    ON dim_region_hist(region_bk, effective_from, effective_to);

-- 字典刷新批次：原子性凭证
CREATE TABLE IF NOT EXISTS dim_refresh_batch (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_ref       TEXT    NOT NULL UNIQUE,                    -- 批次业务编号
    status          TEXT    NOT NULL CHECK (status IN ('COMMITTED', 'ROLLED_BACK')),
    change_count    INTEGER NOT NULL DEFAULT 0,                 -- 本批次含几条改区通知
    note            TEXT,
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT
);

-- 销售事实表（只追加，不随维度变化而改写）
CREATE TABLE IF NOT EXISTS fact_sales_order (
    order_id        INTEGER PRIMARY KEY,
    region_bk       TEXT    NOT NULL,                           -- 订单上的区域业务键（换区不改历史行）
    business_date   TEXT    NOT NULL,                           -- 业务日（事实发生日）
    amount          REAL    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_fact_date ON fact_sales_order(business_date);

-- 账期封账标记
CREATE TABLE IF NOT EXISTS report_period_close (
    period_type     TEXT    NOT NULL,                           -- 'DAILY' / 'WEEKLY'
    period_key      TEXT    NOT NULL,                           -- 如 2026-11-13 / 2026-W46
    closed_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (period_type, period_key)
);

-- 封账报表主表
CREATE TABLE IF NOT EXISTS report_snapshot (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type     TEXT    NOT NULL,                           -- 'DAILY' / 'WEEKLY'
    period_key      TEXT    NOT NULL,                           -- 账期键
    status          TEXT    NOT NULL DEFAULT 'CLOSED'
                        CHECK (status IN ('CLOSED', 'SUPERSEDED')),
    total_amount    REAL    NOT NULL,                           -- 封账总额（冻结）
    cube_batch_note TEXT,                                       -- 计算口径说明
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (report_type, period_key)
);

-- 封账报表的地区切片（按封账时点命中的维度版本冻结，事后不改写）
CREATE TABLE IF NOT EXISTS report_snapshot_item (
    item_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL,
    region_sk       INTEGER NOT NULL,                           -- 冻结的维度版本代理键
    region_bk       TEXT    NOT NULL,
    region_name     TEXT    NOT NULL,                           -- 冗余快照口径，版本变化也不影响
    amount          REAL    NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES report_snapshot(snapshot_id),
    FOREIGN KEY (region_sk) REFERENCES dim_region_hist(region_sk)
);

-- 封账报表的订单级冻结命中：每笔事实在封账时点命中的维度版本，供事后逐笔找回与调整
CREATE TABLE IF NOT EXISTS report_snapshot_order (
    snap_order_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL,
    order_id        INTEGER NOT NULL,
    region_sk       INTEGER NOT NULL,                           -- 封账时命中的版本（旧口径）
    region_name     TEXT    NOT NULL,
    amount          REAL    NOT NULL,
    business_date   TEXT    NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES report_snapshot(snapshot_id),
    FOREIGN KEY (order_id) REFERENCES fact_sales_order(order_id),
    UNIQUE (snapshot_id, order_id)
);

-- 迟到改区确认产生的调整分录：只追加，记入「确认日所在账期」
CREATE TABLE IF NOT EXISTS adjustment_entry (
    adjustment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_snapshot_id  INTEGER NOT NULL,                       -- 来源封账日报
    order_id        INTEGER NOT NULL,                           -- 被调整的事实行
    region_sk_old   INTEGER NOT NULL,                           -- 封账时命中的版本
    region_sk_new   INTEGER NOT NULL,                           -- 更正业务日重算命中的版本
    region_name_old TEXT    NOT NULL,
    region_name_new TEXT    NOT NULL,
    amount          REAL    NOT NULL,                           -- 转移金额（从旧地区挪到新地区）
    effective_date  TEXT    NOT NULL,                           -- 来源确认的更正生效日（上周五）
    posting_period  TEXT    NOT NULL,                           -- 实际记账账期（确认日）
    posted_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_snapshot_id) REFERENCES report_snapshot(snapshot_id),
    FOREIGN KEY (order_id) REFERENCES fact_sales_order(order_id)
);
"""


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开一个启用外键与 WAL 的连接；``db_path=None`` 时用内存库（测试隔离）。"""
    if db_path is None:
        conn = sqlite3.connect(":memory:")
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """建表（幂等）。"""
    conn.executescript(SCHEMA_SQL)
    conn.commit()
