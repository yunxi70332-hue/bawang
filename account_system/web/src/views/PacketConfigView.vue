<template>
  <div class="page-card">
    <div class="toolbar">
      <div>
        <h2 class="page-title">套餐配置 · 下单决策</h2>
        <p class="page-subtitle">
          套餐 = 价格区间 + 可用时段 + 商品白名单（留空=全品类）+ 商品级券规则；
          decide 评估时按开放状态 / 金额区间 / 时段 / SKU 白名单命中
        </p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openCreate">新增套餐</el-button>
    </div>

    <div class="search-bar">
      <el-input v-model="query.keyword" placeholder="套餐名称关键字" clearable style="width: 220px"
        :prefix-icon="Search" @keyup.enter="search" @clear="search" />
      <el-select v-model="query.open" placeholder="开放状态" clearable style="width: 130px" @change="search">
        <el-option label="开放" :value="true" />
        <el-option label="关闭" :value="false" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="search">查询</el-button>
      <el-button :icon="Refresh" circle @click="load" />
    </div>

    <el-table v-loading="loading" :data="items" stripe>
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="name" label="名称" min-width="150">
        <template #default="{ row }">
          <span class="name-cell" :title="row.note">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="价格区间" width="150">
        <template #default="{ row }">
          <span class="price">¥{{ priceRange(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="开放状态" width="90" align="center">
        <template #default="{ row }">
          <el-switch :model-value="row.open_flag" :loading="row._toggling" @change="(v) => toggleOpen(row, v)" />
        </template>
      </el-table-column>
      <el-table-column label="商品数" width="110" align="center">
        <template #default="{ row }">
          <el-tag v-if="!row.item_count" size="small" effect="plain">全品类</el-tag>
          <span v-else>{{ row.item_count }}</span>
        </template>
      </el-table-column>
      <el-table-column label="时段" min-width="130">
        <template #default="{ row }">
          <span v-if="row.available_start || row.available_end" class="mono">
            {{ row.available_start || '00:00:00' }} ~ {{ row.available_end || '23:59:59' }}
          </span>
          <span v-else class="muted">不限</span>
        </template>
      </el-table-column>
      <el-table-column label="最低利润" width="100" align="center">
        <template #default="{ row }">
          <span v-if="row.min_profit" class="price">¥{{ row.min_profit }}</span>
          <span v-else class="muted">全局</span>
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
    </el-table>

    <div class="pager">
      <el-pagination background layout="total, prev, pager, next" :total="total"
        v-model:current-page="query.page" :page-size="query.page_size" @current-change="load" />
    </div>

    <!-- 新增 / 编辑弹窗 -->
    <el-dialog v-model="dlg" :title="editingId ? `编辑套餐 #${editingId}` : '新增套餐'" width="1080px"
      destroy-on-close :close-on-click-modal="false">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="108px">
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="套餐名称" prop="name">
              <el-input v-model="form.name" maxlength="64" placeholder="如：伯牙绝弦套餐" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="最小金额">
              <el-input v-model="form.min_order_amount" placeholder="客户支付价下限，默认 0" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="最大金额">
              <el-input v-model="form.max_order_amount" placeholder="0 = 不设上限" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="时段开始">
              <el-time-select v-model="form.available_start" start="00:00" end="23:59" step="00:15"
                placeholder="不限" clearable style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="时段结束">
              <el-time-select v-model="form.available_end" start="00:00" end="23:59" step="00:15"
                placeholder="不限" clearable style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="最低利润">
              <el-input v-model="form.min_profit" placeholder="元，留空 = 用全局配置" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="备注">
              <el-input v-model="form.note" maxlength="255" placeholder="可选" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>

      <div class="items-head">
        <h3 class="sec-title">
          商品清单
          <span class="muted sub">（{{ form.items.length }} 项，留空 = 全品类可命中；每行可设普通券 / 溢价券匹配规则，展开行编辑溢价券规则）</span>
        </h3>
        <el-button type="primary" size="small" :icon="Plus" @click="openPicker(null)">选择商品</el-button>
      </div>
      <el-table :data="form.items" size="small" border max-height="360">
        <el-table-column type="expand" width="36">
          <template #default="{ row }">
            <div class="rule-expand">
              <span class="rule-label">溢价券规则</span>
              <el-select v-model="row.premium_rule.match_type" size="small" clearable placeholder="不限" style="width: 150px">
                <el-option v-for="m in MATCH_TYPES" :key="m.value" :label="m.label" :value="m.value" />
              </el-select>
              <el-input v-model="row.premium_rule.match_value" size="small" placeholder="匹配值，留空 = 不限"
                style="width: 240px" />
              <span class="muted rule-tip">仅当该商品需溢价券（右侧开关）时生效</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="商品（SPU / SKU）" min-width="220">
          <template #default="{ row, $index }">
            <div class="prod-cell">
              <div class="prod-name">{{ row.product_name || '（未选择）' }}</div>
              <div class="mono muted prod-ids">SPU {{ row.spu_id || '—' }} · SKU {{ row.sku_id || '—' }}</div>
              <el-button link type="primary" size="small" @click="openPicker($index)">重选</el-button>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="面价" width="110">
          <template #default="{ row }">
            <el-input v-model="row.face_price" size="small" placeholder="面价" />
          </template>
        </el-table-column>
        <el-table-column label="溢价" width="110">
          <template #default="{ row }">
            <el-input v-model="row.premium_price" size="small" placeholder="可空" />
          </template>
        </el-table-column>
        <el-table-column label="溢价券商品" width="90" align="center">
          <template #default="{ row }">
            <el-switch v-model="row.is_premium" />
          </template>
        </el-table-column>
        <el-table-column label="普通券规则（match_type + 匹配值）" min-width="280">
          <template #default="{ row }">
            <div class="rule-cell">
              <el-select v-model="row.normal_rule.match_type" size="small" clearable placeholder="不限" style="width: 150px">
                <el-option v-for="m in MATCH_TYPES" :key="m.value" :label="m.label" :value="m.value" />
              </el-select>
              <el-input v-model="row.normal_rule.match_value" size="small" placeholder="匹配值，留空 = 不限" />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="70" align="center">
          <template #default="{ $index }">
            <el-button link type="danger" size="small" @click="removeItem($index)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="未限定商品（全品类）；点右上「选择商品」从菜单添加白名单商品" :image-size="60" />
        </template>
      </el-table>

      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 二级弹层：菜单浏览选商品（城市 → 门店 → SPU） -->
    <el-dialog v-model="dlgPicker" title="选择商品（游客菜单浏览，数据链同下单工作台）" width="980px"
      append-to-body destroy-on-close>
      <div class="cascader">
        <el-select v-model="cityCode" filterable placeholder="① 选择城市" :loading="loadingCities"
          style="width: 190px" @change="onCityChange">
          <el-option v-for="c in cities" :key="c.cityCode" :label="c.cityName" :value="c.cityCode">
            <span>{{ c.cityName }}</span>
            <span class="opt-sub">{{ c.provinceName }}</span>
          </el-option>
        </el-select>
        <el-select v-model="storeNo" filterable remote :remote-method="searchStores" :loading="loadingStores"
          placeholder="② 选择门店（可输入名称搜索）" style="width: 320px" @change="onStoreChange" @focus="searchStores('')">
          <el-option v-for="s in stores" :key="s.storeNo" :label="s.storeName" :value="s.storeNo">
            <span>{{ s.storeName }}</span>
            <span class="opt-sub">{{ s.storeNo }} · {{ s.address || s.cityName || '' }}</span>
          </el-option>
        </el-select>
        <el-input v-model="menuKeyword" :prefix-icon="Search" placeholder="本地过滤商品名" clearable
          style="width: 200px" />
        <el-tag v-if="menuMeta" type="success" effect="plain" class="menu-meta">
          {{ menuMeta.items.length }} SPU
        </el-tag>
      </div>

      <el-table v-loading="loadingMenu" :data="pagedMenu" stripe size="small" max-height="400"
        :row-class-name="pickerRowClass" @row-click="openGoods">
        <el-table-column label="分类" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.categoryName }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="spuName" label="商品（点击查看 SKU）" min-width="220" />
        <el-table-column label="价格" width="95">
          <template #default="{ row }">
            <span v-if="row.price != null" class="price">¥{{ row.price }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="85">
          <template #default="{ row }">
            <el-tag v-if="row.saleOut" type="danger" size="small" effect="plain">售罄</el-tag>
            <el-tag v-else type="success" size="small" effect="plain">在售</el-tag>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="storeNo ? '该门店暂无菜单数据' : '请先选择城市与门店'" :image-size="70" />
        </template>
      </el-table>
      <div v-if="filteredMenu.length > menuPageSize" class="pager-row">
        <el-pagination background layout="prev, pager, next" :total="filteredMenu.length"
          v-model:current-page="menuPage" :page-size="menuPageSize" />
      </div>
      <p class="muted picker-note">点击商品行加载 SKU 明细；选用某个 SKU 后自动回填 spu_id / sku_id / 商品名 / 面价。</p>
    </el-dialog>

    <!-- 三级弹层：SKU 选用 -->
    <el-dialog v-model="dlgSku" :title="`选择 SKU · ${goods.spuName || ''}`" width="640px"
      append-to-body destroy-on-close>
      <el-table v-loading="loadingGoods" :data="goods.skus" stripe size="small" max-height="400">
        <el-table-column label="规格" min-width="160">
          <template #default="{ row }">{{ row.specDesc || '默认' }}</template>
        </el-table-column>
        <el-table-column label="单价" width="90">
          <template #default="{ row }"><span class="price">¥{{ row.price }}</span></template>
        </el-table-column>
        <el-table-column label="库存" width="80">
          <template #default="{ row }">
            <el-tag :type="row.stock > 0 ? 'success' : 'danger'" size="small" effect="plain">
              {{ row.stock > 0 ? row.stock : '售罄' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" size="small" :disabled="row.stock <= 0" @click="pickSku(row)">选用</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="该商品暂无 SKU 数据" :image-size="70" />
        </template>
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { apiDecision, apiOps } from '../api'
import { fmtTime } from '../utils/format'

/* 券规则匹配类型（契约 §1 normal/premium_coupon_rule） */
const MATCH_TYPES = [
  { value: 'template_contains', label: '券名包含' },
  { value: 'template_exact', label: '券名精确' },
  { value: 'benefit_regex', label: '权益正则' },
  { value: 'coupon_prefix', label: '券码前缀' },
]

/* ---------------- 列表 ---------------- */
const loading = ref(false)
const items = ref([])
const total = ref(0)
const query = reactive({ keyword: '', open: '', page: 1, page_size: 20 })

async function load() {
  loading.value = true
  try {
    const params = { keyword: query.keyword.trim(), page: query.page, page_size: query.page_size }
    if (query.open === true || query.open === false) params.open = query.open
    const data = await apiDecision.packets(params)
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}
onMounted(load)

function search() {
  query.page = 1
  load()
}

function priceRange(row) {
  const min = row.min_order_amount || '0'
  const max = Number(row.max_order_amount) > 0 ? row.max_order_amount : '不限'
  return `${min} - ${max}`
}

async function toggleOpen(row, v) {
  row._toggling = true
  try {
    const res = await apiDecision.packetToggleOpen(row.id)
    Object.assign(row, res)   // 返回 packet_summary，原地刷新
    ElMessage.success(res.open_flag ? '套餐已开放' : '套餐已关闭')
  } catch {
    /* 失败保持原值（model-value 未变） */
  } finally {
    row._toggling = false
  }
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除套餐「${row.name}」？其商品清单将一并删除。`, '删除确认', { type: 'warning' })
    .catch(() => Promise.reject())
  await apiDecision.packetDelete(row.id)
  ElMessage.success('已删除')
  load()
}

/* ---------------- 新增 / 编辑 ---------------- */
const dlg = ref(false)
const saving = ref(false)
const formRef = ref()
const editingId = ref(null)
const form = reactive({
  name: '', min_order_amount: '0', max_order_amount: '0',
  available_start: '', available_end: '', min_profit: '', note: '', items: [],
})
const rules = {
  name: [{ required: true, message: '请输入套餐名称', trigger: 'blur' }],
}

function blankRule() {
  return { match_type: '', match_value: '' }
}

function newItemRow(base = {}) {
  return {
    spu_id: base.spu_id || '',
    sku_id: base.sku_id || '',
    product_name: base.product_name || '',
    face_price: base.face_price || '',
    premium_price: base.premium_price || '',
    is_premium: !!base.is_premium,
    normal_rule: { ...blankRule(), ...(base.normal_coupon_rule || {}) },
    premium_rule: { ...blankRule(), ...(base.premium_coupon_rule || {}) },
  }
}

/* 表单内编辑态 rule → 契约 JSON：match_type 与 match_value 均非空才下发，否则 null=不限 */
function rulePayload(rule) {
  return rule?.match_type && rule?.match_value
    ? { match_type: rule.match_type, match_value: rule.match_value }
    : null
}

function openCreate() {
  editingId.value = null
  Object.assign(form, {
    name: '', min_order_amount: '0', max_order_amount: '0',
    available_start: '', available_end: '', min_profit: '', note: '', items: [],
  })
  dlg.value = true
}

async function openEdit(row) {
  editingId.value = row.id
  const detail = await apiDecision.packetGet(row.id)
  Object.assign(form, {
    name: detail.name,
    min_order_amount: detail.min_order_amount ?? '0',
    max_order_amount: detail.max_order_amount ?? '0',
    // 后端 "HH:MM:SS" → el-time-select "HH:MM"
    available_start: detail.available_start ? String(detail.available_start).slice(0, 5) : '',
    available_end: detail.available_end ? String(detail.available_end).slice(0, 5) : '',
    min_profit: detail.min_profit || '',
    note: detail.note || '',
    items: (detail.items || []).map(newItemRow),
  })
  dlg.value = true
}

async function save() {
  await formRef.value.validate().catch(() => Promise.reject())
  const payload = {
    name: form.name.trim(),
    min_order_amount: String(form.min_order_amount).trim() || '0',
    max_order_amount: String(form.max_order_amount).trim() || '0',
    available_start: form.available_start ? `${form.available_start}:00` : '',
    available_end: form.available_end ? `${form.available_end}:00` : '',
    min_profit: String(form.min_profit).trim(),
    note: form.note,
    items: form.items
      .filter((it) => it.sku_id)   // 未选完的空行不下发
      .map((it) => ({
        spu_id: it.spu_id,
        sku_id: it.sku_id,
        product_name: it.product_name,
        face_price: it.face_price,
        premium_price: it.premium_price,
        is_premium: it.is_premium,
        normal_coupon_rule: rulePayload(it.normal_rule),
        premium_coupon_rule: rulePayload(it.premium_rule),
      })),
  }
  saving.value = true
  try {
    if (editingId.value) {
      await apiDecision.packetUpdate(editingId.value, payload)
      ElMessage.success('套餐已保存')
    } else {
      await apiDecision.packetCreate(payload)
      ElMessage.success('套餐已创建')
    }
    dlg.value = false
    load()
  } finally {
    saving.value = false
  }
}

function removeItem(index) {
  form.items.splice(index, 1)
}

/* ---------------- 商品选择器（城市 → 门店 → SPU → SKU，复用 apiOps 游客菜单链） ---------------- */
const dlgPicker = ref(false)
const dlgSku = ref(false)
const pickerIndex = ref(null)   // null=选中后新增行；数字=替换该行（「重选」）
const cities = ref([])
const stores = ref([])
const cityCode = ref('')
const storeNo = ref('')
const loadingCities = ref(false)
const loadingStores = ref(false)
const loadingMenu = ref(false)
const loadingGoods = ref(false)
const menuMeta = ref(null)
const menuKeyword = ref('')
const menuPage = ref(1)
const menuPageSize = 10
const goods = ref({ skus: [] })

async function openPicker(index) {
  pickerIndex.value = index
  dlgPicker.value = true
  if (!cities.value.length) {
    loadingCities.value = true
    try {
      const c = await apiOps.cities()
      cities.value = c.cities
    } finally {
      loadingCities.value = false
    }
  }
}

async function onCityChange() {
  storeNo.value = ''
  menuMeta.value = null
  await searchStores('')
}

async function searchStores(kw) {
  if (!cityCode.value) {
    ElMessage.warning('请先选择城市')
    return
  }
  loadingStores.value = true
  try {
    const data = await apiOps.stores({ city: cityCode.value, keyword: kw, page: 1, page_size: 100 })
    stores.value = data.items
  } finally {
    loadingStores.value = false
  }
}

function onStoreChange() {
  if (storeNo.value) loadMenu()
}

async function loadMenu() {
  loadingMenu.value = true
  menuPage.value = 1
  try {
    const data = await apiOps.menu(storeNo.value)
    data.items.sort((a, b) => Number(a.saleOut) - Number(b.saleOut)) // 在售靠前
    menuMeta.value = data
  } finally {
    loadingMenu.value = false
  }
}

const filteredMenu = computed(() => {
  const list = menuMeta.value?.items || []
  const kw = menuKeyword.value.trim().toLowerCase()
  return kw ? list.filter((i) => (i.spuName || '').toLowerCase().includes(kw)) : list
})
const pagedMenu = computed(() =>
  filteredMenu.value.slice((menuPage.value - 1) * menuPageSize, menuPage.value * menuPageSize))

function pickerRowClass({ row }) {
  return row.saleOut ? 'row-saleout' : ''
}

async function openGoods(row) {
  if (row.saleOut) return
  goods.value = { spuId: row.spuId, spuName: row.spuName, skus: [] }
  dlgSku.value = true
  loadingGoods.value = true
  try {
    const detail = await apiOps.goods(row.spuId, storeNo.value)
    goods.value = detail
  } catch {
    dlgSku.value = false
  } finally {
    loadingGoods.value = false
  }
}

/* 选用 SKU：回填当前行（重选）或追加新行；face_price 取 SKU 单价 */
function pickSku(sku) {
  const fill = {
    spu_id: String(goods.value.spuId || ''),
    sku_id: String(sku.skuId || ''),
    product_name: goods.value.spuName || '',
    face_price: sku.price != null ? String(sku.price) : '',
  }
  if (pickerIndex.value != null && form.items[pickerIndex.value]) {
    Object.assign(form.items[pickerIndex.value], fill)
  } else {
    form.items.push(newItemRow(fill))
  }
  dlgSku.value = false
  dlgPicker.value = false
  ElMessage.success(`已选择：${fill.product_name}（SKU ${fill.sku_id}）`)
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
.name-cell {
  word-break: break-all;
}
.price {
  color: #c45656;
  font-weight: 600;
}
.muted {
  color: var(--muted);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
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
.prod-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: flex-start;
}
.prod-name {
  font-weight: 600;
  color: var(--tea-800);
}
.prod-ids {
  font-size: 11.5px;
}
.rule-cell {
  display: flex;
  gap: 8px;
  align-items: center;
}
.rule-cell .el-input {
  flex: 1;
  min-width: 100px;
}
.rule-expand {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  padding: 6px 12px;
}
.rule-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--tea-800);
}
.rule-tip {
  font-size: 12px;
}
.cascader {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.opt-sub {
  float: right;
  color: var(--muted);
  font-size: 12px;
  margin-left: 14px;
}
.menu-meta {
  font-size: 12.5px;
}
.pager-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
.picker-note {
  font-size: 12.5px;
  margin: 10px 0 0;
}
:deep(.row-saleout) {
  cursor: not-allowed;
  opacity: 0.55;
}
</style>
