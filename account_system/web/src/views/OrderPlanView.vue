<template>
  <div class="page-card">
    <div class="toolbar">
      <div>
        <h2 class="page-title">下单方案 · 下单决策</h2>
        <p class="page-subtitle">
          方案 = 下单时的选券控制单元（策略 + 券优先级层级）；decide / create 指定方案后按其层级链选券，
          不选方案时系统按成本最优自动选券
        </p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openCreate">新增方案</el-button>
    </div>

    <div class="search-bar">
      <el-button :icon="Refresh" circle @click="load" />
      <span v-if="items.length" class="muted total-line">共 {{ items.length }} 个 · 启用 {{ enabledCount }} 个</span>
    </div>

    <el-table v-loading="loading" :data="items" stripe>
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="name" label="名称" min-width="150">
        <template #default="{ row }">
          <span class="name-cell" :title="row.note">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="策略" width="100" align="center">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="strategyTag(row.strategy)">
            {{ strategyLabel(row.strategy) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="饮品信息" min-width="150">
        <template #default="{ row }">
          <span v-if="row.drink_info" :title="row.drink_info">{{ row.drink_info }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="优先级层数" width="100" align="center">
        <template #default="{ row }">
          <span v-if="row.priority_count" class="mono">{{ row.priority_count }}</span>
          <el-tag v-else size="small" effect="plain">纯策略</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="85" align="center">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" :loading="row._toggling" @change="(v) => toggleEnabled(row, v)" />
        </template>
      </el-table-column>
      <el-table-column label="备注" min-width="140">
        <template #default="{ row }">
          <span v-if="row.note">{{ row.note }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="170">
        <template #default="{ row }"><span class="mono">{{ fmtTime(row.updated_at) }}</span></template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button size="small" plain @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" plain @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无下单方案：不建方案时系统按成本最优自动选券" :image-size="70" />
      </template>
    </el-table>

    <!-- 新增 / 编辑弹窗 -->
    <el-dialog v-model="dlg" :title="editingId ? `编辑方案 #${editingId}` : '新增方案'" width="720px"
      destroy-on-close :close-on-click-modal="false">
      <el-tabs v-model="dlgTab">
        <!-- Tab 1：基础信息 -->
        <el-tab-pane label="基础信息" name="base">
          <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
            <el-form-item label="方案名称" prop="name">
              <el-input v-model="form.name" maxlength="64" placeholder="如：DN券优先方案" />
            </el-form-item>
            <el-form-item label="选券策略">
              <el-radio-group v-model="form.strategy" class="strategy-col">
                <el-radio v-for="s in STRATEGIES" :key="s.value" :value="s.value">
                  {{ s.label }}<span class="muted strategy-hint">{{ s.hint }}</span>
                </el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="饮品信息" prop="drink_info">
              <el-input v-model="form.drink_info" type="textarea" :rows="2" maxlength="200"
                show-word-limit clearable resize="none"
                placeholder="必填：饮品相关信息（如客户要求、口味备注、杯型说明等）；下单选此方案时自动带入订单，提交订单前强制非空" />
            </el-form-item>
            <el-form-item label="启用">
              <el-switch v-model="form.enabled" />
            </el-form-item>
            <el-form-item label="备注">
              <el-input v-model="form.note" maxlength="255" placeholder="可选" />
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <!-- Tab 2：优先级层级 -->
        <el-tab-pane :label="`优先级层级（${form.priorities.length}）`" name="tiers">
          <div class="items-head">
            <h3 class="sec-title">
              优先级层级
              <span class="muted sub">（{{ form.priorities.length }} 层；行序即优先级，第 1 行最优先；未匹配任何层的券按策略排序垫底）</span>
            </h3>
            <el-button type="primary" size="small" :icon="Plus" @click="addTier">添加优先级</el-button>
          </div>
          <el-table :data="form.priorities" size="small" border>
            <el-table-column label="层级" width="96">
              <template #default="{ $index }">
                <div class="tier-rank">
                  <el-tag size="small" effect="plain">第{{ $index + 1 }}优先</el-tag>
                  <span class="tier-move">
                    <el-button link size="small" :icon="ArrowUp" :disabled="$index === 0"
                      @click="moveTier($index, -1)" />
                    <el-button link size="small" :icon="ArrowDown" :disabled="$index === form.priorities.length - 1"
                      @click="moveTier($index, 1)" />
                  </span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="层名" width="126">
              <template #default="{ row, $index }">
                <el-input v-model="row.name" size="small" :placeholder="`如：20元DN券 · 留空=第${$index + 1}优先`" />
              </template>
            </el-table-column>
            <el-table-column label="匹配方式" width="120">
              <template #default="{ row }">
                <el-select v-model="row.match_type" size="small">
                  <el-option v-for="m in MATCH_TYPES" :key="m.value" :label="m.label" :value="m.value" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="匹配值" min-width="126">
              <template #default="{ row, $index }">
                <el-input v-model="row.match_value" size="small" :placeholder="tierPlaceholder(row, $index)" />
              </template>
            </el-table-column>
            <el-table-column label="面额校验" width="100">
              <template #default="{ row }">
                <el-input v-model="row.face_value" size="small" placeholder="留空 = 不限面额" />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="58" align="center">
              <template #default="{ $index }">
                <el-button link type="danger" size="small" @click="removeTier($index)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="未设置层级：纯策略排序（系统自动）" :image-size="60" />
            </template>
          </el-table>
          <p class="muted tier-note">
            典型用法：第一优先 券名包含「20元代金券-DN」，第二优先 券名包含「10元代金券-LT」；
            未填匹配值的空行保存时自动忽略，层级按行序重新编号（1..N）
          </p>
        </el-tab-pane>

        <!-- Tab 3：饮品管理（模糊搜索 + 多选关联，保存后方案仅可下单已关联饮品） -->
        <el-tab-pane :label="`饮品管理（${form.drinks.length}）`" name="drinks">
          <div class="drink-search-bar">
            <el-input v-model="drinkKw" size="default" clearable :prefix-icon="Search"
              placeholder="输入饮品关键词搜索（如：伯牙绝弦 / 青青糯山 / 桂花）"
              style="width: 320px" @input="onDrinkKwInput" @clear="drinkResults = []" />
            <el-button type="primary" size="default" :loading="drinkSearching"
              :disabled="!drinkKw.trim()" @click="searchDrinks">搜索</el-button>
            <el-button type="success" size="default" :disabled="!drinkSel.length" @click="addSelectedDrinks">
              加入已选（{{ drinkSel.length }}）
            </el-button>
            <span class="muted drink-count">已关联 {{ form.drinks.length }} 种饮品</span>
          </div>
          <el-table :data="drinkResults" size="small" border stripe v-loading="drinkSearching"
            row-key="sku_id" max-height="260" @selection-change="drinkSel = $event">
            <el-table-column type="selection" width="42" :selectable="(row) => !selectedSkuSet.has(row.sku_id)" />
            <el-table-column prop="spu_name" label="饮品" min-width="150" show-overflow-tooltip />
            <el-table-column prop="spec_desc" label="规格" min-width="110" show-overflow-tooltip />
            <el-table-column label="面价" width="76">
              <template #default="{ row }"><span class="price">¥{{ row.price }}</span></template>
            </el-table-column>
            <el-table-column prop="store_no" label="菜单来源" width="96" />
            <el-table-column label="状态" width="80" align="center">
              <template #default="{ row }">
                <el-tag v-if="selectedSkuSet.has(row.sku_id)" type="success" size="small" effect="plain">已关联</el-tag>
                <el-tag v-else type="info" size="small" effect="plain">未选</el-tag>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty :description="drinkKw.trim() ? '没有匹配的饮品，换个关键词试试（菜单库含已缓存门店）' : '输入关键词搜索饮品（本地菜单库实时反馈）'" :image-size="60" />
            </template>
          </el-table>

          <div class="items-head" style="margin-top: 12px">
            <h3 class="sec-title">
              已关联饮品
              <span class="muted sub">（{{ form.drinks.length }} 种；保存后该方案仅可下单这些饮品，留空 = 不限）</span>
            </h3>
          </div>
          <el-table :data="form.drinks" size="small" border max-height="220">
            <el-table-column prop="drink_name" label="饮品（含规格）" min-width="180" show-overflow-tooltip />
            <el-table-column label="面价" width="76">
              <template #default="{ row }">¥{{ row.face_price || '—' }}</template>
            </el-table-column>
            <el-table-column prop="spu_id" label="SPU" width="130" show-overflow-tooltip />
            <el-table-column prop="sku_id" label="SKU" width="150" show-overflow-tooltip />
            <el-table-column label="操作" width="64" align="center">
              <template #default="{ $index }">
                <el-button link type="danger" size="small" @click="form.drinks.splice($index, 1)">移除</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="未关联饮品：该方案不限制可点饮品" :image-size="60" />
            </template>
          </el-table>
        </el-tab-pane>
      </el-tabs>

      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, ArrowUp, Plus, Refresh, Search } from '@element-plus/icons-vue'
import { apiDecision } from '../api'
import { fmtTime } from '../utils/format'

/* 选券策略（strategy 三枚举）：决定层内与兜底排序取向 */
const STRATEGIES = [
  { value: 'cost_first', label: '成本最优', tag: 'success', hint: '总成本最小优先' },
  { value: 'zero_pay', label: '零元优先', tag: 'warning', hint: '优先能全额覆盖的券，免补差' },
  { value: 'expiry_first', label: '临期优先', tag: 'primary', hint: '优先消耗快过期的券' },
]
const strategyLabel = (s) => STRATEGIES.find((x) => x.value === s)?.label || s
const strategyTag = (s) => STRATEGIES.find((x) => x.value === s)?.tag || 'info'

/* 匹配方式（同成本规则页 match_type 四枚举） */
const MATCH_TYPES = [
  { value: 'template_exact', label: '券名精确', placeholder: '完整券名' },
  { value: 'template_contains', label: '券名包含', placeholder: '券名子串' },
  { value: 'benefit_regex', label: '权益正则', placeholder: '正则表达式' },
  { value: 'coupon_prefix', label: '券码前缀', placeholder: '券码前缀' },
]

/* ---------------- 列表 ---------------- */
const loading = ref(false)
const items = ref([])
const enabledCount = computed(() => items.value.filter((p) => p.enabled).length)

async function load() {
  loading.value = true
  try {
    const data = await apiDecision.orderPlans()
    items.value = data.items
  } finally {
    loading.value = false
  }
}
onMounted(load)

/* 行内启用开关：复用 PUT 全量更新（无独立 toggle 端点） */
async function toggleEnabled(row, v) {
  row._toggling = true
  try {
    await apiDecision.orderPlanUpdate(row.id, rowPayload(row, v))
    row.enabled = v
    ElMessage.success(v ? '方案已启用' : '方案已停用')
  } catch {
    /* 失败保持原值 */
  } finally {
    row._toggling = false
  }
}

/* 列表行 → PUT 全量 payload（level 按数组序重编 1..N，层名留空自动「第N优先」） */
function rowPayload(row, enabled) {
  return {
    name: row.name,
    strategy: row.strategy,
    note: row.note || '',
    enabled,
    priorities: (row.priorities || []).map((p, i) => ({
      level: i + 1,
      name: p.name || `第${i + 1}优先`,
      match_type: p.match_type,
      match_value: p.match_value,
      face_value: p.face_value || '',
    })),
  }
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除下单方案「${row.name}」？其优先级层级将一并删除。`, '删除确认', { type: 'warning' })
    .catch(() => Promise.reject())
  await apiDecision.orderPlanDelete(row.id)
  ElMessage.success('已删除')
  load()
}

/* ---------------- 新增 / 编辑 ---------------- */
const dlg = ref(false)
const saving = ref(false)
const formRef = ref()
const editingId = ref(null)
const form = reactive({
  name: '', strategy: 'cost_first', drink_info: '', enabled: true, note: '',
  priorities: [], drinks: [],
})
const rules = {
  name: [{ required: true, message: '请输入方案名称', trigger: 'blur' }],
  drink_info: [{ required: true, message: '饮品信息为必填项，请填写饮品相关信息后再保存方案', trigger: 'blur' }],
}

/* ---- 饮品管理 Tab：模糊搜索 + 多选关联 ---- */
const dlgTab = ref('base')
const drinkKw = ref('')
const drinkSearching = ref(false)
const drinkResults = ref([])
const drinkSel = ref([])   // 搜索结果表当前勾选行（多选）
const selectedSkuSet = computed(() => new Set(form.drinks.map((d) => d.sku_id)))
let drinkSearchTimer = null

function onDrinkKwInput() {   // 输入防抖 300ms 实时反馈
  clearTimeout(drinkSearchTimer)
  const kw = drinkKw.value.trim()
  if (!kw) { drinkResults.value = []; return }
  drinkSearchTimer = setTimeout(searchDrinks, 300)
}

async function searchDrinks() {
  const kw = drinkKw.value.trim()
  if (!kw) return
  drinkSearching.value = true
  try {
    const data = await apiDecision.planDrinkSearch({ keyword: kw, limit: 30 })
    drinkResults.value = data.items || []
  } finally {
    drinkSearching.value = false
  }
}

function addSelectedDrinks() {   // 多选批量加入已关联（按 sku 去重，回填名称与面价）
  const fresh = []
  for (const row of drinkSel.value) {
    if (selectedSkuSet.value.has(row.sku_id)) continue
    form.drinks.push({
      spu_id: String(row.spu_id || ''), sku_id: String(row.sku_id),
      drink_name: `${row.spu_name}${row.spec_desc && row.spec_desc !== '默认' ? `（${row.spec_desc}）` : ''}`,
      face_price: String(row.price || ''),
    })
    fresh.push(row.spu_name)
  }
  drinkSel.value = []
  if (fresh.length) {
    ElMessage.success(`已加入 ${fresh.length} 种饮品：${fresh.slice(0, 3).join('、')}${fresh.length > 3 ? ' 等' : ''}`)
  } else {
    ElMessage.info('所选饮品均已关联')
  }
}

function newTierRow() {
  return { name: '', match_type: 'template_contains', match_value: '', face_value: '' }
}

/* 匹配值占位：前两行给典型用法示例，其余按匹配方式提示 */
function tierPlaceholder(row, index) {
  if (index === 0 && !String(row.match_value).trim()) return '如：20元代金券-DN'
  if (index === 1 && !String(row.match_value).trim()) return '如：10元代金券-LT'
  return MATCH_TYPES.find((m) => m.value === row.match_type)?.placeholder || '匹配值'
}

function openCreate() {
  editingId.value = null
  dlgTab.value = 'base'
  Object.assign(form, {
    name: '', strategy: 'cost_first', drink_info: '', enabled: true, note: '',
    priorities: [newTierRow(), newTierRow()],   // 两行示例引导（占位符提示典型用法）
    drinks: [],
  })
  drinkKw.value = ''
  drinkResults.value = []
  drinkSel.value = []
  dlg.value = true
}

function openEdit(row) {
  editingId.value = row.id
  dlgTab.value = 'base'
  Object.assign(form, {
    name: row.name,
    strategy: row.strategy || 'cost_first',
    drink_info: row.drink_info || '',
    enabled: !!row.enabled,
    note: row.note || '',
    drinks: (row.drinks || []).map((d) => ({
      spu_id: d.spu_id || '', sku_id: d.sku_id,
      drink_name: d.drink_name || '', face_price: d.face_price || '',
    })),
    priorities: (row.priorities || []).map((p) => ({
      name: p.name || '',
      match_type: p.match_type || 'template_contains',
      match_value: p.match_value || '',
      face_value: p.face_value || '',
    })),
  })
  dlg.value = true
}

async function save() {
  await formRef.value.validate().catch(() => Promise.reject())
  const payload = {
    name: form.name.trim(),
    strategy: form.strategy,
    drink_info: form.drink_info.trim(),
    note: form.note,
    enabled: form.enabled,
    drinks: form.drinks.map((d) => ({
      spu_id: d.spu_id, sku_id: d.sku_id,
      drink_name: d.drink_name, face_price: d.face_price,
    })),
    priorities: form.priorities
      .filter((p) => p.match_type && String(p.match_value).trim())   // 未填匹配值的空行不下发
      .map((p, i) => ({
        level: i + 1,                                   // 按行序重编 level（1..N）
        name: String(p.name).trim() || `第${i + 1}优先`,
        match_type: p.match_type,
        match_value: String(p.match_value).trim(),
        face_value: String(p.face_value).trim(),
      })),
  }
  saving.value = true
  try {
    if (editingId.value) {
      await apiDecision.orderPlanUpdate(editingId.value, payload)
      ElMessage.success('方案已保存')
    } else {
      await apiDecision.orderPlanCreate(payload)
      ElMessage.success('方案已创建')
    }
    dlg.value = false
    load()
  } finally {
    saving.value = false
  }
}

/* ---------------- 优先级层级编辑 ---------------- */
function addTier() {
  form.priorities.push(newTierRow())
}

function removeTier(index) {
  form.priorities.splice(index, 1)
}

/* 上移 / 下移：交换行序，层级编号随行序自动变化 */
function moveTier(index, dir) {
  const target = index + dir
  if (target < 0 || target >= form.priorities.length) return
  const arr = form.priorities
  ;[arr[index], arr[target]] = [arr[target], arr[index]]
}
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.search-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.total-line {
  font-size: 12.5px;
}
.name-cell {
  word-break: break-all;
}
.muted {
  color: var(--muted);
}
.sec-title {
  margin: 0;
  font-size: 15px;
  color: var(--tea-800);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.sub {
  font-size: 12.5px;
  font-weight: 400;
}
.items-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 6px 0 10px;
  padding-top: 10px;
  border-top: 1px dashed #e3eae6;
}
/* 饮品管理 Tab：搜索栏与价格样式 */
.drink-search-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.drink-search-bar .drink-count {
  font-size: 12.5px;
}
.drink-search-bar .price,
.price {
  color: var(--el-color-danger);
  font-weight: 600;
}
.strategy-col {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
}
.strategy-col .el-radio {
  margin-right: 0;
}
.strategy-hint {
  font-size: 12px;
  margin-left: 6px;
  font-weight: 400;
}
.tier-rank {
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: flex-start;
}
.tier-move {
  display: flex;
  gap: 2px;
}
.tier-note {
  font-size: 12px;
  line-height: 1.6;
  margin: 8px 0 0;
}
</style>
