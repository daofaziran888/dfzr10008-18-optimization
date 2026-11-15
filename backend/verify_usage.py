"""服务层验证：使用效果追踪 / 配额控制的过滤口径与快照规则（不依赖第三方库）。"""
import sys

sys.path.insert(0, "/workspace/backend")

from app.services.quota import QuotaService  # noqa: E402
from app.services.usage import usage_service  # noqa: E402

PRODUCT = "PROD-0031 渠道复购分析数据产品"
CALLER = "渠道复购分析"
PERIOD = "2026-06"

passed = []
failed = []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


# ---- 1. 列表三字段过滤：排除 PROD-0042 / 2026-07 ----
items, total = usage_service.list_entries(product=PRODUCT, caller=CALLER, period=PERIOD)
ids = sorted(i["id"] for i in items)
check("三字段过滤只返回 PROD-0031/2026-06 两条", ids == [1, 2], str(ids))
check("过滤后 total=2，不混入其他周期", total == 2, f"total={total}")

items2, _ = usage_service.list_entries(product="PROD-0031", period=PERIOD)
check("按产品编号+周期过滤排除 PROD-0042",
      all("PROD-0042" not in str(i.get("产品名称")) for i in items2))

all_rows, all_total = usage_service.list_entries()
check("无过滤时返回全部 3 条明细", all_total == 3, f"total={all_total}")

# 过滤范围隔离：单独按 2026-07 应只拿到 PROD-0042
july, july_total = usage_service.list_entries(period="2026-07")
check("2026-07 范围只有 PROD-0042 一条",
      july_total == 1 and july[0]["产品编号"] == "PROD-0042")

# ---- 2. 明细聚合覆盖旧快照（旧快照 400/8 是重复计数）----
snap, msg, ok = usage_service.summarize(product=PRODUCT, caller=CALLER, period=PERIOD)
check("首次重算成功", ok)
check("明细聚合调用次数=200（120+80），覆盖旧的400", snap["调用次数"] == 200, str(snap["调用次数"]))
check("明细聚合错误次数=4（3+1），覆盖旧的8", snap["错误次数"] == 4, str(snap["错误次数"]))
check("标记为冲突覆盖", snap.get("冲突覆盖") is True)
check("快照版本升到 2", snap["版本"] == 2)
expected_latency = round((84.5 * 120 + 76.0 * 80) / 200, 2)
check("平均耗时按调用量加权", snap["平均耗时"] == expected_latency, str(snap["平均耗时"]))

# ---- 3. 重复汇总不生成第二份结果 ----
before_version = snap["版本"]
snap2, msg2, ok2 = usage_service.summarize(product=PRODUCT, caller=CALLER, period=PERIOD)
check("重复汇总仍成功", ok2)
check("重复汇总版本号不增加", snap2["版本"] == before_version, f"v={snap2['版本']}")
check("重复汇总标记复用、不生成第二份", snap2.get("重复汇总") is True)
check("快照存储里该范围仍只有一份", len(usage_service._snapshots) == 1,
      f"快照数={len(usage_service._snapshots)}")

# ---- 4. 汇总失败：沿用上一份完整快照 ----
snap3, msg3, ok3 = usage_service.summarize(
    product=PRODUCT, caller=CALLER, period=PERIOD, fail=True
)
check("故障注入时返回失败", ok3 is False)
check("失败后仍返回上一份完整快照", snap3 is not None and snap3.get("完整") is True)
check("失败后指标保持上一份（200/4）", snap3["调用次数"] == 200 and snap3["错误次数"] == 4)
check("失败提示包含沿用快照说明", "沿用" in msg3, msg3)
check("失败后快照数量不增加", len(usage_service._snapshots) == 1)

# ---- 5. 效果分析只从快照读取 ----
an_snap, an_msg, an_ok = usage_service.analyze(product=PRODUCT, caller=CALLER, period=PERIOD)
analysis = an_snap["分析结果"]
check("效果分析成功", an_ok)
check("分析结果来自快照的 200/4",
      analysis["调用次数"] == 200 and analysis["错误次数"] == 4)
check("错误率=4/200=0.02", analysis["错误率"] == 0.02, str(analysis["错误率"]))
check("分析结论已生成", bool(analysis["结论"]))

# 只读快照端点背后的服务方法
ro_snap, ro_msg = usage_service.get_snapshot(product=PRODUCT, caller=CALLER, period=PERIOD)
check("只读快照与分析共用同一对象口径", ro_snap["分析结果"] is analysis)
check("只读快照调用次数=200", ro_snap["调用次数"] == 200)

# 汇总/分析/快照三处口径一致
check("汇总、分析、快照三处调用次数一致",
      snap2["调用次数"] == analysis["调用次数"] == ro_snap["调用次数"] == 200)

# ---- 6. 配额：读取调用方当前周期实际调用并算超限 ----
quota = QuotaService()
overlimit = quota.current_overlimit()
item = next((x for x in overlimit if x["调用方名称"] == CALLER), None)
check("配额读取到渠道复购分析当前周期", item is not None)
check("当前周期定位为 2026-07（最大周期）", item and item["统计周期"] == "2026-07", str(item))
check("当前调用次数=500（取最大周期，不含2026-06）", item and item["当前调用次数"] == 500, str(item))
check("日上限读取为 300", item and item["日调用上限"] == 300, str(item))
check("超限调用次数=200", item and item["超限调用次数"] == 200, str(item))
check("判定为超限", item and item["是否超限"] is True)

one = quota.current_overlimit(CALLER)
check("配额支持按调用方过滤", len(one) == 1 and one[0]["调用方名称"] == CALLER)

# 缺范围参数
none_snap, none_msg = usage_service.get_snapshot(product=PRODUCT, caller="", period=PERIOD)
check("缺调用方时读取快照被拒绝", none_snap is None)

print(f"\n通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    print("失败项：", failed)
    raise SystemExit(1)
print("全部通过")
