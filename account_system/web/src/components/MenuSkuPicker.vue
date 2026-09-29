<template>
  <div class="sku-picker">
    <div class="sku-picker-bar">
      <el-input v-model="kw" clearable :prefix-icon="Search" :placeholder="placeholder"
        style="width: 320px" @input="onInput" @clear="results = []" />
      <el-button type="primary" :loading="searching" :disabled="!kw.trim()" @click="search">
        搜索
      </el-button>
      <el-button type="success" :disabled="!sel.length" @click="confirm">
        {{ addLabel }}（{{ sel.length }}）
      </el-button>
      <slot name="bar" />
    </div>
    <el-table :data="results" size="small" border stripe v-loading="searching"
      row-key="sku_id" max-height="260" @selection-change="sel = $event">
      <el-table-column type="selection" width="42" :selectable="(row) => !selectedSet.has(row.sku_id)" />
      <el-table-column prop="spu_name" label="饮品" min-width="150" show-overflow-tooltip />
      <el-table-column prop="spec_desc" label="规格" min-width="110" show-overflow-tooltip />
      <el-table-column label="面价" width="76">
        <template #default="{ row }"><span class="price">¥{{ row.price }}</span></template>
      </el-table-column>
      <el-table-column prop="store_no" label="菜单来源" width="96" />
      <el-table-column label="状态" width="80" align="center">
        <template #default="{ row }">
          <el-tag v-if="selectedSet.has(row.sku_id)" type="success" size="small" effect="plain">已选</el-tag>
          <el-tag v-else type="info" size="small" effect="plain">未选</el-tag>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty :description="kw.trim()
          ? '没有匹配的饮品，换个关键词试试（菜单库含已缓存门店）'
          : '输入关键词搜索饮品（本地菜单库实时反馈）'" :image-size="60" />
      </template>
    </el-table>
  </div>
</template>

<script setup>
/**
 * 菜单 SKU 选品器（套餐商品清单 / 下单方案饮品管理共用，2026-09-29 合并优化）。
 * 数据流统一：本地菜单库 menu_goods_cache 模糊搜索（GET /decision/sku-search），
 * 输入 300ms 防抖实时反馈；结果表多选 → confirm 一次性 emit「add」，去重与
 * 追加逻辑由父组件决定（父组件持有各自的已选清单结构）。
 */
import { computed, ref } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { apiDecision } from '../api'

const props = defineProps({
  placeholder: { type: String, default: '输入饮品关键词搜索（如：伯牙绝弦 / 青青糯山 / 桂花）' },
  addLabel: { type: String, default: '加入已选' },
  limit: { type: Number, default: 30 },
  /* 已选 sku_id 集合（数组即可，内部转 Set）：命中行禁选并标「已选」 */
  selected: { type: Array, default: () => [] },
})
const emit = defineEmits(['add'])

const selectedSet = computed(() => new Set(props.selected || []))

const kw = ref('')
const searching = ref(false)
const results = ref([])
const sel = ref([])   // 结果表当前勾选行
let timer = null

function onInput() {   // 输入防抖 300ms 实时反馈
  clearTimeout(timer)
  const k = kw.value.trim()
  if (!k) { results.value = []; return }
  timer = setTimeout(search, 300)
}

async function search() {
  const k = kw.value.trim()
  if (!k) return
  searching.value = true
  try {
    const data = await apiDecision.skuSearch({ keyword: k, limit: props.limit })
    results.value = data.items || []
  } finally {
    searching.value = false
  }
}

function confirm() {
  emit('add', sel.value)
  sel.value = []
}

defineExpose({ clear: () => { kw.value = ''; results.value = []; sel.value = [] } })
</script>

<style scoped>
.sku-picker-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.price {
  color: var(--el-color-danger);
  font-weight: 600;
}
</style>
