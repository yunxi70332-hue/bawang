<template>
  <el-select :model-value="modelValue" class="ctype-select" :size="size" filterable remote
    :remote-method="onSearch" :loading="loading" clearable
    placeholder="绑定券类型：搜索券档案库（如：20元代金券）"
    @change="onChange" @clear="onClear" @visible-change="onVisible">
    <el-option v-for="t in options" :key="t.template_name" :value="t.template_name"
      :label="t.template_name">
      <div class="ctype-row">
        <span class="ctype-name" :title="t.template_name">{{ t.template_name }}</span>
        <span class="ctype-meta" :class="{ 'ctype-expiring': isExpiring(t) }">
          {{ t.benefit_text || '—' }} · 可用 {{ t.available_count }}/{{ t.total_count }} 张
          · {{ t.account_count }} 账号 · {{ validityLabel(t) }}
        </span>
      </div>
    </el-option>
    <template v-if="options.length" #footer>
      <div class="ctype-foot">
        <el-button v-if="options.length < total" link type="primary" size="small"
          :loading="loading" @click="loadMore">
          加载更多（{{ options.length }}/{{ total }}）
        </el-button>
        <span v-else class="ctype-done">已加载全部 {{ total }} 种券类型</span>
      </div>
    </template>
  </el-select>
</template>

<script setup>
/**
 * 券类型绑定下拉（下单方案 · 优先级层级「优惠券绑定」，2026-09-29）。
 * 数据源：GET /decision/coupon-types —— coupon_records 按模板名（券类型）实时聚合，
 * 与「优惠券查询·功能4」全量查询的落库记录保持同步：
 *  - 远程搜索：输入 300ms 防抖拉首页（可用/档案张数 + 权益 + 有效期一目了然）；
 *  - 分页：下拉底部「加载更多」逐页追加，避免全量拉取；
 *  - 同步：每次展开现查首页（visible-change 刷新，无本地缓存）。
 * 选中/清空不直接改规则字段，emit 给父组件决定回填（券名精确 + 匹配值 + 面额校验）。
 */
import { ref } from 'vue'
import { apiDecision } from '../api'

const props = defineProps({
  /* 当前绑定的券类型名（模板名）；层级行非名称类匹配（正则/券码前缀）传空串 */
  modelValue: { type: String, default: '' },
  size: { type: String, default: 'small' },
})
const emit = defineEmits(['select', 'clear'])

const PAGE_SIZE = 20
const options = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
let kw = ''
let timer = null
let seq = 0   // 竞态防护：慢请求回来时若已有更新的搜索/翻页，丢弃旧结果

async function fetchPage(reset) {
  const cur = ++seq
  loading.value = true
  try {
    const data = await apiDecision.couponTypes({ keyword: kw, page: page.value, page_size: PAGE_SIZE })
    if (cur !== seq) return
    total.value = data.total || 0
    options.value = reset ? (data.items || []) : [...options.value, ...(data.items || [])]
  } finally {
    if (cur === seq) loading.value = false
  }
}

function refresh() {
  page.value = 1
  return fetchPage(true)
}

function onSearch(q) {   // 远程搜索：防抖后回到第一页
  clearTimeout(timer)
  kw = String(q || '').trim()
  timer = setTimeout(refresh, 300)
}

function loadMore() {
  page.value += 1
  fetchPage(false)
}

function onVisible(open) {
  if (open) {
    kw = ''        // 重开时输入框已清空，同步重置本地关键字，首页展示全量首屏
    refresh()      // 每次展开现查首页：与券档案库最新落库记录同步
  }
}

function onChange(v) {
  const opt = options.value.find((t) => t.template_name === v)
  if (v && opt) emit('select', opt)
}

function onClear() {
  if (props.modelValue) emit('clear')
}

/* 剩余有效期文案（后端 coupon-types.days_remaining：最早到期的可用券剩余自然日）：
 * 无可用券=—，无截止标注=长期，负数=已过期，0=今日到期，≤3 天前端加橙色强调 */
function validityLabel(t) {
  if (!t.available_count) return '无可用'
  const d = t.days_remaining
  if (d === null || d === undefined) return '长期'
  if (d < 0) return '已过期'
  if (d === 0) return '今日到期'
  return `剩 ${d} 天`
}

function isExpiring(t) {
  const d = t.days_remaining
  return t.available_count && d !== null && d !== undefined && d >= 0 && d <= 3
}
</script>

<style scoped>
.ctype-select {
  width: 100%;
}
.ctype-row {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: space-between;
}
.ctype-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ctype-meta {
  flex: none;
  font-size: 11.5px;
  color: var(--muted);
}
/* 临期（最早到期的可用券剩 ≤3 天）：整段元信息橙色强调 */
.ctype-meta.ctype-expiring {
  color: var(--el-color-warning);
  font-weight: 600;
}
.ctype-foot {
  display: flex;
  justify-content: center;
  padding: 2px 0;
}
.ctype-done {
  font-size: 11.5px;
  color: var(--muted);
}
</style>
