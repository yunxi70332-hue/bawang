<template>
  <el-tag v-if="tag" :type="tag.type" :effect="tag.effect" size="small" class="validity-tag">
    {{ tag.label }}
  </el-tag>
  <span v-else-if="days !== null && days !== undefined && days >= 0" class="validity-plain">
    剩 {{ days }} 天
  </span>
  <span v-else class="validity-plain" title="券档案未标注截止时间（长期或未知）">长期</span>
</template>

<script setup>
/**
 * 券剩余有效期标签（「剩余 N 天」展示统一入口，2026-09-29）。
 * 天数与状态均由后端 services.decision.coupon_validity 计算（时区安全自然日口径），
 * 本组件只渲染：已过期=红 / 今日到期与剩≤3天=橙（醒目）/ 未生效=蓝 /
 * 剩>3天=灰字 / 无截止标注=「长期」。coupon_validity 未下发时按天数本地兜底推导。
 */
import { computed } from 'vue'

const props = defineProps({
  days: { type: Number, default: null },          // 剩余自然日数（null=无截止标注）
  status: { type: String, default: '' },           // active|expiring|expired|pending|unknown
})

const tag = computed(() => {
  const s = props.status || (props.days === null || props.days === undefined
    ? 'unknown'
    : props.days < 0 ? 'expired' : props.days <= 3 ? 'expiring' : 'active')
  if (s === 'expired') return { label: '已过期', type: 'danger', effect: 'dark' }
  if (s === 'pending') return { label: '未生效', type: 'info', effect: 'plain' }
  if (s === 'expiring') {
    return props.days === 0
      ? { label: '今日到期', type: 'warning', effect: 'dark' }
      : { label: `剩 ${props.days} 天`, type: 'warning', effect: 'dark' }
  }
  return null   // active / unknown 走纯文本分支
})
</script>

<style scoped>
.validity-tag {
  flex: none;
}
.validity-plain {
  color: var(--muted);
  font-size: 11.5px;
}
</style>
