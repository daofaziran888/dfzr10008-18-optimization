"""使用效果追踪业务规则。

口径约定（列表、调用统计汇总、效果分析、统计周期快照共用同一过滤范围）：
1. ``scoped_rows`` 是唯一的过滤入口：按产品名称、调用方名称、统计周期三个字段，
   叠加记录编号关键字与记录状态，任何汇总/分析都只在这份过滤后的明细上计算，
   因此其他周期（例如 PROD-0042 / 2026-07）不会混入 2026-06 的结果。
2. 周期汇总始终以过滤后的明细聚合为准；若与既有快照（可能重复计入过）冲突，
   直接覆盖旧快照并标记冲突，避免调用次数、错误次数被重复累计。
3. 同一范围重复汇总不产生第二份快照：指标未变化时原样返回上一份完整快照。
4. 重算失败（明细数值无法解析等）不提交半成品，继续保留上一份完整快照。
5. 效果分析结果只从统计周期快照读取，快照不存在时先按当前范围汇总生成。
"""
from __future__ import annotations

from typing import Any

from app.seed import USAGE_SNAPSHOT_SEED
from app.store import store

MODULE = "usage"
REQUIRED_FIELDS = ["记录编号", "产品名称", "调用方名称"]
STATUS_ORDER = ["采集中", "已汇总", "已分析", "已归档"]
ACTION_RULES = {"汇总数据": "采集中", "分析效果": "已汇总", "归档记录": "已分析"}
NEGATIVE_ACTIONS = []

# 效果分析的错误率分档阈值
ERROR_RATE_HEALTHY = 0.02
ERROR_RATE_WARN = 0.05


def _to_number(value: Any) -> float:
    """把明细里的调用次数/错误次数/平均耗时解析成数值；无法解析时显式失败。"""
    if isinstance(value, bool):  # bool 是 int 的子类，单独挡掉，避免被当成 0/1
        raise ValueError(f"非法数值：{value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        raise ValueError("空值无法参与统计")
    return float(text)


def _norm(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


class UsageService:
    def __init__(self) -> None:
        # 统计周期快照：key = (产品名称, 调用方名称, 统计周期)
        self._snapshots: dict[tuple[str | None, str | None, str | None], dict[str, Any]] = {
            (snap["产品名称"], snap["调用方名称"], snap["统计周期"]): dict(snap)
            for snap in USAGE_SNAPSHOT_SEED
        }

    # ------------------------------------------------------------------ 过滤口径
    def scoped_rows(
        self,
        *,
        product: str | None = None,
        caller: str | None = None,
        period: str | None = None,
        keyword: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """列表/汇总/分析/快照共用的过滤范围，返回的是同一份口径下的明细。"""
        product = _norm(product)
        caller = _norm(caller)
        period = _norm(period)
        rows = store.rows(MODULE)
        if product:
            # 产品名包含产品编号（PROD-0031）与中文名，按包含匹配两种写法都能定位
            rows = [row for row in rows if product in str(row.get("产品名称", ""))]
        if caller:
            rows = [row for row in rows if str(row.get("调用方名称", "")).strip() == caller]
        if period:
            rows = [row for row in rows if str(row.get("统计周期", "")).strip() == period]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows

    @staticmethod
    def _scope_key(
        product: str | None, caller: str | None, period: str | None
    ) -> tuple[str | None, str | None, str | None]:
        return _norm(product), _norm(caller), _norm(period)

    # ------------------------------------------------------------------ 列表
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        product: str | None = None,
        caller: str | None = None,
        period: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = self.scoped_rows(
            product=product, caller=caller, period=period, keyword=keyword, status=status
        )
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
            return None, f"使用记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于使用效果追踪可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"使用记录已{action}"

    # ------------------------------------------------------------- 明细聚合（唯一口径）
    def _aggregate(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """对过滤后的明细做聚合：任何无法解析的数值都会抛错，由上层保留旧快照。"""
        if not rows:
            raise ValueError("当前过滤范围没有使用记录，无法汇总")
        total_calls = 0
        total_errors = 0
        weighted_latency = 0.0
        for row in rows:
            calls = _to_number(row.get("调用次数"))
            errors = _to_number(row.get("错误次数"))
            latency = _to_number(row.get("平均耗时"))
            total_calls += int(calls)
            total_errors += int(errors)
            weighted_latency += latency * calls
        avg_latency = round(weighted_latency / total_calls, 2) if total_calls else 0.0
        return {
            "调用次数": total_calls,
            "错误次数": total_errors,
            "平均耗时": avg_latency,
            "明细条数": len(rows),
        }

    @staticmethod
    def _metrics(snap: dict[str, Any]) -> tuple[int, int, float, int]:
        return snap["调用次数"], snap["错误次数"], snap["平均耗时"], snap["明细条数"]

    # ------------------------------------------------------------- 周期汇总 / 快照
    def summarize(
        self,
        *,
        product: str,
        caller: str,
        period: str,
        fail: bool = False,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """按统一过滤范围重算周期汇总。

        返回 ``(快照, 说明, 是否成功)``。
        - 与既有快照冲突：以明细聚合覆盖，版本号 +1 并标记冲突；
        - 指标一致（重复汇总）：原样返回上一份完整快照，不生成第二份结果；
        - 重算失败：不覆盖，继续返回上一份完整快照。
        """
        key = self._scope_key(product, caller, period)
        if not all(key):
            return None, "汇总需要同时提供产品名称、调用方名称和统计周期", False
        previous = self._snapshots.get(key)
        try:
            if fail:
                # 故障注入：模拟重算过程异常，验证失败恢复仍读上一份完整快照
                raise RuntimeError("模拟汇总过程中断")
            rows = self.scoped_rows(product=key[0], caller=key[1], period=key[2])
            agg = self._aggregate(rows)
        except (ValueError, RuntimeError) as exc:
            if previous is not None and previous.get("完整"):
                return previous, f"汇总失败，继续沿用上一份完整快照：{exc}", False
            return None, f"汇总失败且没有可沿用的完整快照：{exc}", False

        if previous is not None and self._metrics(previous) == self._metrics(agg):
            # 同一范围重复过滤/汇总：指标没有变化，直接复用，不生成第二份结果
            previous["重复汇总"] = True
            return previous, "指标未变化，已复用上一份统计周期快照，未生成第二份结果", True

        conflict = previous is not None and self._metrics(previous) != self._metrics(agg)
        version = (previous["版本"] + 1) if previous else 1
        snapshot = {
            "产品名称": key[0],
            "调用方名称": key[1],
            "统计周期": key[2],
            **agg,
            "版本": version,
            "完整": True,
            "来源": "明细聚合重算",
            "冲突覆盖": conflict,
            "重复汇总": False,
            # 重算只更新统计指标；已生成的分析结果若失效会在分析步骤重建
            "分析结果": previous.get("分析结果") if previous else None,
        }
        self._snapshots[key] = snapshot  # 构建成功后才提交，失败不会留下半成品
        for row in rows:
            row["status"] = "已汇总"
            row["记录状态"] = "已汇总"
        message = (
            "既有周期汇总与明细聚合冲突，已按明细重算覆盖"
            if conflict else "统计周期快照已生成"
        )
        return snapshot, message, True

    def get_snapshot(
        self, *, product: str, caller: str, period: str
    ) -> tuple[dict[str, Any] | None, str]:
        """只读统计周期快照，不触发重算；效果分析结果也从这里读取。"""
        key = self._scope_key(product, caller, period)
        if not all(key):
            return None, "读取快照需要同时提供产品名称、调用方名称和统计周期"
        snapshot = self._snapshots.get(key)
        if snapshot is None:
            return None, "当前范围还没有统计周期快照，请先执行汇总数据"
        return snapshot, "已读取当前过滤范围的统计周期快照"

    # ------------------------------------------------------------- 效果分析
    def analyze(
        self, *, product: str, caller: str, period: str
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """效果分析：确保快照存在（按当前范围汇总），分析结果只基于快照计算并回写。"""
        snapshot, message, ok = self.summarize(product=product, caller=caller, period=period)
        if not ok or snapshot is None:
            return snapshot, message, False
        calls = snapshot["调用次数"]
        errors = snapshot["错误次数"]
        error_rate = round(errors / calls, 4) if calls else 0.0
        if error_rate >= ERROR_RATE_WARN:
            conclusion = "错误率偏高，需要排查下游稳定性"
        elif error_rate >= ERROR_RATE_HEALTHY:
            conclusion = "错误率处于观察区间，建议持续监控"
        else:
            conclusion = "调用质量稳定，可继续放量"
        snapshot["分析结果"] = {
            "错误率": error_rate,
            "平均耗时": snapshot["平均耗时"],
            "调用次数": calls,
            "错误次数": errors,
            "明细条数": snapshot["明细条数"],
            "结论": conclusion,
            "统计周期": snapshot["统计周期"],
        }
        key = self._scope_key(product, caller, period)
        for row in self.scoped_rows(product=key[0], caller=key[1], period=key[2]):
            row["status"] = "已分析"
            row["记录状态"] = "已分析"
        return snapshot, f"{message}；效果分析已基于统计周期快照生成", True

    # ------------------------------------------------------------- 给配额模块复用的调用量
    def caller_current_totals(self) -> list[dict[str, Any]]:
        """按调用方聚合其「当前周期（最大统计周期）」的实际调用量，供配额超限判定复用。"""
        latest_period: dict[str, str] = {}
        for row in store.rows(MODULE):
            caller = str(row.get("调用方名称", "")).strip()
            period = str(row.get("统计周期", "")).strip()
            if caller and period and period > latest_period.get(caller, ""):
                latest_period[caller] = period
        result: list[dict[str, Any]] = []
        for caller, period in latest_period.items():
            rows = self.scoped_rows(caller=caller, period=period)
            try:
                agg = self._aggregate(rows)
            except ValueError:
                continue
            result.append({
                "调用方名称": caller,
                "统计周期": period,
                "调用次数": agg["调用次数"],
                "错误次数": agg["错误次数"],
                "平均耗时": agg["平均耗时"],
                "明细条数": agg["明细条数"],
            })
        return sorted(result, key=lambda item: item["调用方名称"])


# 模块级单例：路由、配额控制等所有入口共享同一份统计周期快照状态
usage_service = UsageService()
