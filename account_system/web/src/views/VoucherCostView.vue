<template>
  <div class="page-card">
    <div class="toolbar">
      <div>
        <h2 class="page-title">券成本与阈值 · 下单决策</h2>
        <p class="page-subtitle">
          成本规则按 priority 升序匹配券（名称/权益/券码/面额），命中即取 cost_price 作为采购成本；
          未命中按 面额 × 兜底系数 保守计；阈值用于 decide 盈利校验
        </p>
      </div>
    </div>

    <el-tabs v-model="tab">
      <!-- Tab1：成本子类（业务分类层） -->
      <el-tab-pane label="成本子类" name="categories">
        <p class="muted cat-note">
          子类 = 券资产业务分类：系统按下方匹配规则自动将券归类（优先级越小越先匹配）；
          子类只做分类，成本价仍由「成本规则」决定
        </p>
        <div class="search-bar">
          <el-button type="primary" :icon="Plus" @click="openCatCreate">新增子类</el-button>
          <el-button :icon="Refresh" circle @click="loadCategories" />
          <span class="muted total-line" v-if="categories.length">共 {{ categories.length }} 个 · 启用 {{ enabledCatCount }} 个</span>
        </div>

        <el-table v-loading="loadingCats" :data="categories" stripe>
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="name" label="名称" min-width="130">
            <template #default="{ row }">
              <span class="name-cell" :title="row.note">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column label="业务类型" width="100" align="center">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="bizTag(row.biz_type)">{{ bizLabel(row.biz_type) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="匹配方式" min-width="170">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ matchLabel(row.match_type) }}</el-tag>
              <span class="mono match-val" :title="row.match_value">{{ row.match_value }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="priority" label="优先级" width="80" align="center">
            <template #default="{ row }"><span class="mono">{{ row.priority }}</span></template>
          </el-table-column>
          <el-table-column label="启用" width="85" align="center">
            <template #default="{ row }">
              <el-switch :model-value="row.enabled" :loading="row._toggling" @change="(v) => toggleCategory(row, v)" />
            </template>
          </el-table-column>
          <el-table-column prop="coupon_count" label="券数" width="80" align="center" />
          <el-table-column label="面额合计" width="100" align="center">
            <template #default="{ row }">
              <span v-if="row.face_total">¥{{ row.face_total }}</span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="成本合计" width="100" align="center">
            <template #default="{ row }">
              <span v-if="row.cost_total" class="price">¥{{ row.cost_total }}</span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="215" fixed="right">
            <template #default="{ row }">
              <el-button size="small" plain @click="openCatEdit(row)">编辑</el-button>
              <el-button size="small" type="danger" plain @click="removeCategory(row)">删除</el-button>
              <el-button size="small" plain @click="viewCoupons(row)">查看券</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- Tab2：成本规则 -->
      <el-tab-pane label="成本规则" name="rules">
        <div class="search-bar">
          <el-button type="primary" :icon="Plus" @click="openCreate">新增规则</el-button>
          <el-button :icon="Upload" @click="openImport">导入</el-button>
          <el-button :icon="Refresh" circle @click="loadRules" />
          <span class="muted total-line" v-if="items.length">共 {{ items.length }} 条 · 启用 {{ enabledCount }} 条</span>
        </div>

        <el-table v-loading="loadingRules" :data="items" stripe>
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="name" label="规则名" min-width="140">
            <template #default="{ row }">
              <span class="name-cell" :title="row.note">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column label="匹配方式" width="110">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ matchLabel(row.match_type) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="match_value" label="匹配值" min-width="160">
            <template #default="{ row }">
              <span class="mono">{{ row.match_value }}</span>
            </template>
          </el-table-column>
          <el-table-column label="面额校验" width="95" align="center">
            <template #default="{ row }">
              <span v-if="row.face_value">¥{{ row.face_value }}</span>
              <span v-else class="muted">不限</span>
            </template>
          </el-table-column>
          <el-table-column label="采购成本" width="100" align="center">
            <template #default="{ row }">
              <span class="price">¥{{ row.cost_price }}</span>
            </template>
          </el-table-column>
          <el-table-column label="子类" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.category_id" size="small" effect="plain"
                :type="bizTag(ruleBizType(row.category_id))">{{ row.category_name }}</el-tag>
              <span v-else class="muted">未分类</span>
            </template>
          </el-table-column>
          <el-table-column prop="priority" label="优先级" width="85" align="center">
            <template #default="{ row }">
              <span class="mono">{{ row.priority }}</span>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="85" align="center">
            <template #default="{ row }">
              <el-switch :model-value="row.enabled" :loading="row._toggling" @change="(v) => toggleRule(row, v)" />
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
      </el-tab-pane>

      <!-- Tab3：决策配置 -->
      <el-tab-pane label="决策配置" name="config">
        <el-form v-loading="loadingConfig" :model="config" label-width="160px" class="config-form">
          <el-form-item label="全局最低利润（元）">
            <el-input v-model="config.min_profit" placeholder="如 2.00，每单利润低于该值阻断" style="width: 240px" />
          </el-form-item>
          <el-form-item label="全局最低利润率（%）">
            <el-input v-model="config.min_margin" placeholder="留空 = 不启用（不带 % 号）" style="width: 240px" />
          </el-form-item>
          <el-form-item label="全局最大承受下单金额（元）">
            <el-input v-model="config.max_order_cost" placeholder="留空 = 不限（成本上限，超过即拦截出单）" style="width: 320px" />
          </el-form-item>
          <el-form-item label="每单杂费（元）">
            <el-input v-model="config.overhead" placeholder="0" style="width: 240px" />
          </el-form-item>
          <el-form-item label="成本兜底系数">
            <el-input v-model="config.cost_fallback_ratio" placeholder="1.0（规则未命中时按 面额 × 该系数计成本）" style="width: 320px" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="savingConfig" @click="saveConfig">保存配置</el-button>
          </el-form-item>
        </el-form>
        <p class="muted config-note">套餐级 min_profit / max_order_cost 非空时覆盖全局同名项；其余全局生效（写入 data/decision_config.json）。</p>
      </el-tab-pane>
    </el-tabs>

    <!-- 规则新增 / 编辑弹窗 -->
    <el-dialog v-model="dlg" :title="editingId ? `编辑规则 #${editingId}` : '新增规则'" width="560px"
      destroy-on-close :close-on-click-modal="false">
      <el-form ref="formRef" :model="form" :rules="formRules" label-width="100px">
        <el-form-item label="规则名" prop="name">
          <el-input v-model="form.name" maxlength="64" placeholder="如：20元代金券-渠道A" />
        </el-form-item>
        <el-form-item label="匹配方式" prop="match_type">
          <el-select v-model="form.match_type" style="width: 100%">
            <el-option v-for="m in MATCH_TYPES" :key="m.value" :label="m.label" :value="m.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="匹配值" prop="match_value">
          <el-input v-model="form.match_value" maxlength="128"
            :placeholder="matchPlaceholder" />
          <p class="muted field-note">{{ matchHint }}</p>
        </el-form-item>
        <el-form-item label="面额校验">
          <el-input v-model="form.face_value" placeholder="元，留空 = 不校验面额" style="width: 200px" />
        </el-form-item>
        <el-form-item label="采购成本" prop="cost_price">
          <el-input v-model="form.cost_price" placeholder="元（该券的实际采购价）" style="width: 200px" />
        </el-form-item>
        <el-form-item label="所属子类">
          <el-select v-model="form.category_id" style="width: 100%" placeholder="选择子类">
            <el-option label="未分类" :value="0" />
            <el-option v-for="c in categories" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
          <p class="muted field-note">仅作业务归类标记，不影响规则匹配与成本计算</p>
        </el-form-item>
        <el-form-item label="优先级">
          <el-input-number v-model="form.priority" :min="1" :max="9999" controls-position="right" />
          <span class="muted field-note" style="margin-left: 10px">越小越优先</span>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.note" maxlength="255" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 子类新增 / 编辑弹窗 -->
    <el-dialog v-model="dlgCat" :title="editingCatId ? `编辑子类 #${editingCatId}` : '新增子类'" width="560px"
      destroy-on-close :close-on-click-modal="false">
      <el-form ref="catFormRef" :model="catForm" :rules="catFormRules" label-width="100px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="catForm.name" maxlength="64" placeholder="如：银行渠道券" />
        </el-form-item>
        <el-form-item label="业务类型" prop="biz_type">
          <el-select v-model="catForm.biz_type" style="width: 100%">
            <el-option v-for="b in BIZ_TYPES" :key="b.value" :label="b.label" :value="b.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="匹配方式" prop="match_type">
          <el-select v-model="catForm.match_type" style="width: 100%">
            <el-option v-for="m in MATCH_TYPES" :key="m.value" :label="m.label" :value="m.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="匹配值" prop="match_value">
          <el-input v-model="catForm.match_value" maxlength="128" :placeholder="catMatchPlaceholder" />
          <p class="muted field-note">{{ catMatchHint }}</p>
        </el-form-item>
        <el-form-item label="优先级">
          <el-input-number v-model="catForm.priority" :min="1" :max="9999" controls-position="right" />
          <span class="muted field-note" style="margin-left: 10px">越小越优先</span>
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="catForm.sort" :min="0" :max="9999" controls-position="right" />
          <span class="muted field-note" style="margin-left: 10px">展示顺序，越小越靠前</span>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="catForm.enabled" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="catForm.note" maxlength="255" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlgCat = false">取消</el-button>
        <el-button type="primary" :loading="savingCat" @click="saveCategory">保存</el-button>
      </template>
    </el-dialog>

    <!-- 导入弹窗：JSON 文本域 → costRuleImport -->
    <el-dialog v-model="dlgImport" title="导入成本规则（JSON）" width="640px" destroy-on-close>
      <p class="muted import-tip">
        粘贴规则对象数组（字段同新增弹窗：name / match_type / match_value / face_value / cost_price / priority / enabled / note）；
        name 与库内重复的条目将被跳过。
      </p>
      <el-input v-model="importText" type="textarea" :rows="10" class="mono import-area"
        placeholder='[
  {"name": "20元券-渠道A", "match_type": "template_contains", "match_value": "代金券", "face_value": "20", "cost_price": "8.00", "priority": 100, "enabled": true}
]' />
      <div v-if="importResult" class="import-result">
        <el-alert type="success" :closable="false" show-icon
          :title="`导入完成：成功 ${importResult.imported} 条 · 跳过 ${importResult.skipped} 条`" />
        <el-alert v-if="importResult.errors?.length" type="warning" :closable="false" show-icon class="import-err"
          title="以下条目存在错误：">
          <ul class="err-list">
            <li v-for="(e, i) in importResult.errors" :key="i" class="mono">{{ e }}</li>
          </ul>
        </el-alert>
      </div>
      <template #footer>
        <el-button @click="dlgImport = false">关闭</el-button>
        <el-button type="primary" :loading="importing" @click="doImport">解析并导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Upload } from '@element-plus/icons-vue'
import { apiDecision } from '../api'
import { fmtTime } from '../utils/format'
/* 匹配方式（契约 §1 match_type 四枚举）：决策域共享常量（三页统一出处） */
import { MATCH_TYPES, matchLabel } from '../constants/decision'

const router = useRouter()

/* 业务类型（biz_type 四枚举）：paid=采购付费 / free=活动免费 / bank=银行渠道 / other=其他 */
const BIZ_TYPES = [
  { value: 'paid', label: '采购付费', tag: 'warning' },
  { value: 'free', label: '活动免费', tag: 'success' },
  { value: 'bank', label: '银行渠道', tag: 'primary' },
  { value: 'other', label: '其他', tag: 'info' },
]
const bizLabel = (t) => BIZ_TYPES.find((b) => b.value === t)?.label || t
const bizTag = (t) => BIZ_TYPES.find((b) => b.value === t)?.tag || 'info'

const tab = ref('categories')

/* ---------------- Tab1：成本子类 ---------------- */
const loadingCats = ref(false)
const categories = ref([])
const dlgCat = ref(false)
const savingCat = ref(false)
const catFormRef = ref()
const editingCatId = ref(null)
const catForm = reactive({
  name: '', biz_type: 'paid', match_type: 'template_contains', match_value: '',
  priority: 100, sort: 0, enabled: true, note: '',
})
const catFormRules = {
  name: [{ required: true, message: '请输入子类名称', trigger: 'blur' }],
  biz_type: [{ required: true, message: '请选择业务类型', trigger: 'change' }],
  match_type: [{ required: true, message: '请选择匹配方式', trigger: 'change' }],
  match_value: [{ required: true, message: '请输入匹配值', trigger: 'blur' }],
}

const enabledCatCount = computed(() => categories.value.filter((c) => c.enabled).length)

const catMatchPlaceholder = computed(() =>
  MATCH_TYPES.find((m) => m.value === catForm.match_type)?.placeholder || '')
const catMatchHint = computed(() =>
  MATCH_TYPES.find((m) => m.value === catForm.match_type)?.hint || '')

async function loadCategories() {
  loadingCats.value = true
  try {
    const data = await apiDecision.costCategories()
    categories.value = data.items
  } finally {
    loadingCats.value = false
  }
}

function openCatCreate() {
  editingCatId.value = null
  Object.assign(catForm, {
    name: '', biz_type: 'paid', match_type: 'template_contains', match_value: '',
    priority: 100, sort: 0, enabled: true, note: '',
  })
  dlgCat.value = true
}

function openCatEdit(row) {
  editingCatId.value = row.id
  Object.assign(catForm, {
    name: row.name,
    biz_type: row.biz_type,
    match_type: row.match_type,
    match_value: row.match_value || '',
    priority: row.priority ?? 100,
    sort: row.sort ?? 0,
    enabled: !!row.enabled,
    note: row.note || '',
  })
  dlgCat.value = true
}

async function saveCategory() {
  await catFormRef.value.validate().catch(() => Promise.reject())
  const payload = {
    name: catForm.name.trim(),
    biz_type: catForm.biz_type,
    match_type: catForm.match_type,
    match_value: catForm.match_value.trim(),
    priority: catForm.priority,
    sort: catForm.sort,
    enabled: catForm.enabled,
    note: catForm.note,
  }
  savingCat.value = true
  try {
    if (editingCatId.value) {
      await apiDecision.costCategoryUpdate(editingCatId.value, payload)
      ElMessage.success('子类已保存')
    } else {
      await apiDecision.costCategoryCreate(payload)
      ElMessage.success('子类已创建')
    }
    dlgCat.value = false
    loadCategories()
  } finally {
    savingCat.value = false
  }
}

/* 行内启用开关：复用 PUT 全量更新（无独立 toggle 端点） */
async function toggleCategory(row, v) {
  row._toggling = true
  try {
    await apiDecision.costCategoryUpdate(row.id, {
      name: row.name,
      biz_type: row.biz_type,
      match_type: row.match_type,
      match_value: row.match_value,
      priority: row.priority,
      sort: row.sort ?? 0,
      enabled: v,
      note: row.note || '',
    })
    row.enabled = v
    ElMessage.success(v ? '子类已启用' : '子类已停用')
  } catch {
    /* 失败保持原值 */
  } finally {
    row._toggling = false
  }
}

async function removeCategory(row) {
  await ElMessageBox.confirm(`确定删除子类「${row.name}」？其名下券将不再命中该分类`, '删除确认', { type: 'warning' })
    .catch(() => Promise.reject())
  await apiDecision.costCategoryDelete(row.id)
  ElMessage.success('已删除')
  loadCategories()
}

/* 查看券：跳转券档案库并按该子类预筛 */
function viewCoupons(row) {
  router.push({ path: '/ops/coupons', query: { tab: 'archive', cost_category: row.id } })
}

/* ---------------- Tab2：成本规则 ---------------- */
const loadingRules = ref(false)
const items = ref([])
const dlg = ref(false)
const saving = ref(false)
const formRef = ref()
const editingId = ref(null)
const form = reactive({
  name: '', match_type: 'template_contains', match_value: '', face_value: '',
  cost_price: '', priority: 100, enabled: true, note: '', category_id: 0,
})
const formRules = {
  name: [{ required: true, message: '请输入规则名', trigger: 'blur' }],
  match_type: [{ required: true, message: '请选择匹配方式', trigger: 'change' }],
  match_value: [{ required: true, message: '请输入匹配值', trigger: 'blur' }],
  cost_price: [{ required: true, message: '请输入采购成本', trigger: 'blur' }],
}

const enabledCount = computed(() => items.value.filter((r) => r.enabled).length)

/* 规则行「子类」列配色：按 category_id 回查业务类型（响应只带 category_id/category_name） */
const ruleBizType = (cid) => categories.value.find((c) => c.id === cid)?.biz_type

const matchPlaceholder = computed(() =>
  MATCH_TYPES.find((m) => m.value === form.match_type)?.placeholder || '')
const matchHint = computed(() =>
  MATCH_TYPES.find((m) => m.value === form.match_type)?.hint || '')

async function loadRules() {
  loadingRules.value = true
  try {
    const data = await apiDecision.costRules()
    items.value = data.items
  } finally {
    loadingRules.value = false
  }
}

function openCreate() {
  editingId.value = null
  Object.assign(form, {
    name: '', match_type: 'template_contains', match_value: '', face_value: '',
    cost_price: '', priority: 100, enabled: true, note: '', category_id: 0,
  })
  dlg.value = true
}

function openEdit(row) {
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    match_type: row.match_type,
    match_value: row.match_value || '',
    face_value: row.face_value || '',
    cost_price: row.cost_price ?? '',
    priority: row.priority ?? 100,
    enabled: !!row.enabled,
    note: row.note || '',
    category_id: row.category_id ?? 0,
  })
  dlg.value = true
}

async function save() {
  await formRef.value.validate().catch(() => Promise.reject())
  const payload = {
    name: form.name.trim(),
    match_type: form.match_type,
    match_value: form.match_value.trim(),
    face_value: String(form.face_value).trim(),
    cost_price: String(form.cost_price).trim(),
    priority: form.priority,
    enabled: form.enabled,
    note: form.note,
    category_id: form.category_id ?? 0,
  }
  saving.value = true
  try {
    if (editingId.value) {
      await apiDecision.costRuleUpdate(editingId.value, payload)
      ElMessage.success('规则已保存')
    } else {
      await apiDecision.costRuleCreate(payload)
      ElMessage.success('规则已创建')
    }
    dlg.value = false
    loadRules()
  } finally {
    saving.value = false
  }
}

/* 行内启用开关：复用 PUT 全量更新（无独立 toggle 端点） */
async function toggleRule(row, v) {
  row._toggling = true
  try {
    await apiDecision.costRuleUpdate(row.id, {
      name: row.name,
      match_type: row.match_type,
      match_value: row.match_value,
      face_value: row.face_value || '',
      cost_price: row.cost_price,
      priority: row.priority,
      enabled: v,
      note: row.note || '',
      category_id: row.category_id ?? 0,
    })
    row.enabled = v
    ElMessage.success(v ? '规则已启用' : '规则已停用')
  } catch {
    /* 失败保持原值 */
  } finally {
    row._toggling = false
  }
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除成本规则「${row.name}」？`, '删除确认', { type: 'warning' })
    .catch(() => Promise.reject())
  await apiDecision.costRuleDelete(row.id)
  ElMessage.success('已删除')
  loadRules()
}

/* ---------------- 导入 ---------------- */
const dlgImport = ref(false)
const importText = ref('')
const importing = ref(false)
const importResult = ref(null)

function openImport() {
  importText.value = ''
  importResult.value = null
  dlgImport.value = true
}

async function doImport() {
  let arr
  try {
    arr = JSON.parse(importText.value)
  } catch {
    ElMessage.error('JSON 解析失败，请检查格式（应为对象数组）')
    return
  }
  if (!Array.isArray(arr) || !arr.length) {
    ElMessage.warning('内容须为非空的对象数组')
    return
  }
  importing.value = true
  try {
    importResult.value = await apiDecision.costRuleImport(arr)
    ElMessage.success(`导入完成：成功 ${importResult.value.imported} 条`)
    loadRules()
  } finally {
    importing.value = false
  }
}

/* ---------------- Tab3：决策配置 ---------------- */
const loadingConfig = ref(false)
const savingConfig = ref(false)
const config = reactive({ min_profit: '', min_margin: '', max_order_cost: '', overhead: '', cost_fallback_ratio: '' })

async function loadConfig() {
  loadingConfig.value = true
  try {
    const data = await apiDecision.configGet()
    Object.assign(config, {
      min_profit: data.min_profit ?? '',
      min_margin: data.min_margin ?? '',
      max_order_cost: data.max_order_cost ?? '',
      overhead: data.overhead ?? '0',
      cost_fallback_ratio: data.cost_fallback_ratio ?? '1.0',
    })
  } finally {
    loadingConfig.value = false
  }
}

async function saveConfig() {
  savingConfig.value = true
  try {
    const res = await apiDecision.configPut({
      min_profit: String(config.min_profit).trim(),
      min_margin: String(config.min_margin).trim(),
      max_order_cost: String(config.max_order_cost).trim(),
      overhead: String(config.overhead).trim() || '0',
      cost_fallback_ratio: String(config.cost_fallback_ratio).trim() || '1.0',
    })
    ElMessage.success('决策配置已保存')
    if (res) Object.assign(config, res)
  } finally {
    savingConfig.value = false
  }
}

onMounted(() => {
  loadCategories()
  loadRules()
  loadConfig()
})
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
.cat-note {
  font-size: 12.5px;
  line-height: 1.6;
  margin: 0 0 12px;
}
.match-val {
  margin-left: 6px;
  font-size: 12px;
  word-break: break-all;
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
.field-note {
  font-size: 12px;
  margin: 4px 0 0;
  line-height: 1.5;
}
.config-form {
  max-width: 640px;
  padding-top: 8px;
}
.config-note {
  font-size: 12.5px;
  margin-top: 4px;
}
.import-tip {
  font-size: 12.5px;
  margin: 0 0 10px;
  line-height: 1.6;
}
.import-area :deep(textarea) {
  font-size: 12px;
}
.import-result {
  margin-top: 12px;
}
.import-err {
  margin-top: 8px;
}
.err-list {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  word-break: break-all;
}
</style>
