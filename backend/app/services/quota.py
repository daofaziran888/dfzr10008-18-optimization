"""调用配额控制业务规则：状态流转、字段校验与筛选口径都收在这里。

超限判定不另造数据，直接复用使用效果追踪（usage）按调用方当前周期聚合出的实际调用量，
对照配额的「日调用上限」计算超限次数，保证配额控制读到的就是当前真实调用。
"""
from __future__ import annotations

from typing import Any

from app.services.usage import usage_service
from app.store import store

MODULE = "quota"
REQUIRED_FIELDS = ["配置编号", "调用方名称", "每秒上限"]
STATUS_ORDER = ["草稿", "已生效", "已超额", "已过期"]
ACTION_RULES = {"生效配额": "草稿", "调整配额": "已生效", "取消配额": "已超额"}
NEGATIVE_ACTIONS = []


def _to_int(value: Any) -> int | None:
    """把配额上限解析成整数；样例里占位字符串无法解析时返回 None，跳过该条判定。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


class QuotaService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"配额配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于调用配额控制可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"配额配置已{action}"

    # ------------------------------------------------- 当前周期超限调用（读取 usage 明细）
    def current_overlimit(self, caller: str | None = None) -> list[dict[str, Any]]:
        """返回每个调用方当前周期的实际调用量、日上限与超限次数。

        数据来源是 usage 明细按「调用方 + 当前（最大）统计周期」的聚合，
        而不是任何手工维护的计数，避免与使用记录口径不一致。
        """
        caller = (caller or "").strip() or None
        totals = usage_service.caller_current_totals()
        if caller:
            totals = [item for item in totals if item["调用方名称"] == caller]

        quotas = store.rows(MODULE)
        result: list[dict[str, Any]] = []
        for item in totals:
            name = item["调用方名称"]
            # 同一调用方取最新一条配额配置
            match = next((row for row in reversed(quotas)
                          if str(row.get("调用方名称", "")).strip() == name), None)
            daily_limit = _to_int(match.get("日调用上限")) if match else None
            used = item["调用次数"]
            over = max(used - daily_limit, 0) if daily_limit is not None else 0
            result.append({
                "调用方名称": name,
                "统计周期": item["统计周期"],
                "当前调用次数": used,
                "日调用上限": daily_limit,
                "超限调用次数": over,
                "是否超限": daily_limit is not None and used > daily_limit,
                "配额编号": match.get("配置编号") if match else None,
                "超限策略": match.get("超限策略") if match else None,
            })
        return result
