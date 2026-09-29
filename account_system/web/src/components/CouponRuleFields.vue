<template>
  <div class="rule-fields" :class="{ compact }">
    <el-select :model-value="modelValue.match_type" :size="size" :clearable="clearable"
      placeholder="不限" style="width: 140px" @update:model-value="onType">
      <el-option v-for="m in MATCH_TYPES" :key="m.value" :label="m.label" :value="m.value" />
    </el-select>
    <el-input :model-value="modelValue.match_value" :size="size" :placeholder="valuePlaceholder"
      class="rule-value" @update:model-value="(v) => patch({ match_value: v })" />
    <el-input v-if="showFace" :model-value="modelValue.face_value" :size="size"
      placeholder="面额，空=不限" style="width: 104px" @update:model-value="(v) => patch({ face_value: v })" />
  </div>
</template>

<script setup>
/**
 * 券匹配规则编辑字段组（套餐 item 券规则 / 方案优先级层级共用，2026-09-29 合并优化）。
 * 结构统一为 { match_type, match_value, face_value }——与后端
 * services/decision.rule_satisfied 的单条规则判定一一对应（面额校验非空时须等于券面额）。
 * clearable=true 用于「空 = 不限」语义（套餐规则）；false 用于必选匹配方式（方案层级）。
 */
import { computed } from 'vue'
import { MATCH_TYPES } from '../constants/decision'

const props = defineProps({
  /* v-model：{ match_type, match_value, face_value }（原地 patch，不整对象替换） */
  modelValue: { type: Object, required: true },
  size: { type: String, default: 'small' },
  clearable: { type: Boolean, default: true },   // 套餐规则可清空=不限；方案层级必选
  showFace: { type: Boolean, default: true },    // 是否展示面额校验输入
  compact: { type: Boolean, default: false },    // 紧凑模式（表格展开行）
})
const emit = defineEmits(['update:modelValue'])

const valuePlaceholder = computed(() =>
  MATCH_TYPES.find((m) => m.value === props.modelValue.match_type)?.placeholder
    || (props.clearable ? '匹配值，留空 = 不限' : '匹配值'))

function patch(part) {
  emit('update:modelValue', { ...props.modelValue, ...part })
}

function onType(v) {
  patch({ match_type: v || '' })
}
</script>

<style scoped>
.rule-fields {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.rule-fields.compact {
  padding: 2px 0;
}
.rule-fields .rule-value {
  flex: 1;
  min-width: 110px;
}
</style>
