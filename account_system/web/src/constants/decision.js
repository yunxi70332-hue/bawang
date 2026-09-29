/**
 * 下单决策域共享常量（2026-09-29 合并优化）：
 * 套餐配置 / 下单方案 / 券成本规则三页原本各自维护同构枚举，收敛为唯一出处。
 * 后端对应实现：services/decision.rule_satisfied（四类匹配 + 面额校验统一判定）。
 */

/* 券规则匹配方式（契约 §1 match_type 四枚举） */
export const MATCH_TYPES = [
  { value: 'template_exact', label: '券名精确', placeholder: '完整券名，如：伯牙绝弦兑换券', hint: 'template_name 完全相等才命中' },
  { value: 'template_contains', label: '券名包含', placeholder: '券名子串，如：代金券', hint: 'template_name 包含该子串即命中' },
  { value: 'benefit_regex', label: '权益正则', placeholder: '正则表达式，如：满\\d+减(\\d+)', hint: '对 券名+权益文本 做正则搜索' },
  { value: 'coupon_prefix', label: '券码前缀', placeholder: '券码前缀，如：CKKQ', hint: '券码以该前缀开头即命中' },
]
export const matchLabel = (t) => MATCH_TYPES.find((m) => m.value === t)?.label || t

/* 选券策略（下单方案 strategy 三枚举）：决定层内与兜底排序取向 */
export const PLAN_STRATEGIES = [
  { value: 'cost_first', label: '成本最优', tag: 'success', hint: '总成本最小优先' },
  { value: 'zero_pay', label: '零元优先', tag: 'warning', hint: '优先能全额覆盖的券，免补差' },
  { value: 'expiry_first', label: '临期优先', tag: 'primary', hint: '优先消耗快过期的券' },
]
export const strategyLabel = (s) => PLAN_STRATEGIES.find((x) => x.value === s)?.label || s
export const strategyTag = (s) => PLAN_STRATEGIES.find((x) => x.value === s)?.tag || 'info'
