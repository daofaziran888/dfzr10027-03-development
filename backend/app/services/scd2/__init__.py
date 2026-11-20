"""区域维度 SCD Type 2 历史与封账调整子系统。

设计要点（对应业务需求）：

1. ``dim_region_hist`` 用代理键 + 半开区间 ``[effective_from, effective_to)``
   保存区域字典的每一版口径，事实行凭「业务键 + 业务日期」命中唯一一条维度版本，
   客户换经营区域后旧订单仍按事实发生时的区域聚合。
2. 相邻区间在分界日只能命中一个区域：新版本生效日 ``D`` 同时是旧版本的
   ``effective_to``，半开区间保证 ``D`` 当天只命中新版本。
3. 每次字典刷新是一个事务批次（``dim_refresh_batch``），中途失败整体回滚，
   已确认的历史口径与已封账切片不会被写入一半的记录改写。
4. 封账日报（``report_snapshot`` / ``report_snapshot_item``）冻结的是封账时点
   命中的维度版本金额，永不修改；迟到的改区确认只生成调整分录
   （``adjustment_entry``），记入确认所在账期，周报引用快照 + 调整而不是改写历史。
"""
from __future__ import annotations

from app.services.scd2.schema import connect, init_schema
from app.services.scd2.dim_service import RegionDimService
from app.services.scd2.cube_service import CubeService
from app.services.scd2.report_service import ReportService

__all__ = [
    "connect",
    "init_schema",
    "RegionDimService",
    "CubeService",
    "ReportService",
]
