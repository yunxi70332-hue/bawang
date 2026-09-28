<template>
  <div>
    <div class="page-card">
      <h2 class="page-title">优惠券查询 · 功能4</h2>
      <p class="page-subtitle">
        全量方案：遍历系统内所有账号的 token，批量拉取可用（effective-list）/ 历史（historical-list）双列表，
        收集全部优惠券 ID 并落库券档案；下方搜索支持券码 / 券名称 / 使用范围等多维度模糊匹配
      </p>

      <div class="query-bar">
        <el-button type="primary" :icon="Refresh" :loading="loadingAll" @click="queryAll">
          全量查询（所有账号）
        </el-button>
        <el-divider direction="vertical" />
        <el-select v-model="accountId" filterable placeholder="选择茶姬账号（单账号查询）" style="width: 300px" clearable>
          <el-option v-for="a in accounts" :key="a.id" :value="a.id"
            :label="`${a.label}（${a.phone_masked}${a.status === 'online' ? ' · 在线' : ' · ' + a.status_label}）`">
            <span>{{ a.label }}</span>
            <span class="opt-sub">{{ a.phone_masked }} · {{ a.status_label }}</span>
          </el-option>
        </el-select>
        <el-button :icon="Search" :loading="loadingOne" :disabled="!accountId" @click="queryOne">
          查询单账号
        </el-button>
        <span v-if="result" class="run-at mono">查询时间：{{ result.run_at }}</span>
      </div>

      <el-alert v-if="lastSyncError" :title="lastSyncError" type="warning" show-icon :closable="false" class="sync-alert" />
    </div>

    <template v-if="result">
      <div class="sum-grid">
        <div class="sum-card hero">
          <div class="sum-value">{{ result.distinct_coupon_ids ?? result.coupons.length }}</div>
          <div class="sum-label">{{ mode === 'all' ? '收集到的优惠券 ID（去重）' : '优惠券 ID' }}</div>
          <div v-if="mode === 'all'" class="sum-sub">
            扫描 {{ result.scanned }} 个账号 · 成功 {{ result.ok }}
            <template v-if="result.expired"> · 失效 {{ result.expired }}</template>
            <template v-if="result.failed"> · 失败 {{ result.failed }}</template>
          </div>
        </div>
        <div class="sum-card">
          <div class="sum-value">{{ result.summary.effective_usable_times }}</div>
          <div class="sum-label">可用次数</div>
          <div class="sum-sub">每张券权益次数之和（10次卡按10次计）</div>
        </div>
        <div class="sum-card">
          <div class="sum-value">{{ result.summary.effective_total }}</div>
          <div class="sum-label">可用券（张）</div>
        </div>
        <div class="sum-card">
          <div class="sum-value">{{ result.summary.historical_total }}</div>
          <div class="sum-label">历史券</div>
        </div>
        <div class="sum-card highlight">
          <div class="sum-value">{{ result.exchange_vouchers.length }}</div>
          <div class="sum-label">饮品兑换券</div>
        </div>
      </div>

      <div v-if="mode === 'all' && result.accounts?.length" class="page-card acc-card">
        <h3 class="sec-title">账号遍历结果</h3>
        <el-table :data="result.accounts" size="small" border>
          <el-table-column prop="label" label="账号" min-width="150">
            <template #default="{ row }">
              {{ row.label }}<span class="muted mono"> #{{ row.id }}</span>
              <el-tag v-if="row.nickname" size="small" effect="plain" class="nick-tag">{{ row.nickname }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="phone_masked" label="手机号" width="140" />
          <el-table-column label="结果" width="100" align="center">
            <template #default="{ row }">
              <el-tag :type="row.result === 'ok' ? 'success' : row.result === 'expired' ? 'danger' : 'warning'" size="small">
                {{ { ok: '成功', expired: '凭证失效', error: '失败' }[row.result] || row.result }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="coupons" label="收集券数" width="90" align="center" />
          <el-table-column prop="error" label="说明" min-width="220">
            <template #default="{ row }">
              <span :class="row.error ? 'err-text' : 'muted'">{{ row.error || '—' }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="page-card">
        <div class="search-bar">
          <el-input v-model="keyword" :prefix-icon="Search" placeholder="模糊搜索：券码 / 券名称 / 使用范围 / 权益 / 归属账号…"
            clearable style="max-width: 420px" />
          <el-select v-model="searchField" style="width: 130px">
            <el-option label="全部维度" value="all" />
            <el-option label="仅券码" value="code" />
            <el-option label="仅名称" value="name" />
            <el-option label="仅使用范围" value="scene" />
          </el-select>
          <span class="muted search-hit">
            命中 {{ filteredCoupons.length }} / {{ result.coupons.length }} 张
          </span>
        </div>

        <el-tabs v-model="activeTab">
          <el-tab-pane :label="`本次查询明细（${filteredCoupons.length}）`" name="live">
            <el-table :data="pagedLive" stripe size="small">
              <el-table-column label="归属" width="76">
                <template #default="{ row }">
                  <el-tag :type="row.bucket === '可用' ? 'success' : 'info'" size="small" effect="plain">{{ row.bucket }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column v-if="mode === 'all'" label="账号" min-width="120">
                <template #default="{ row }">{{ row.account_label || '—' }}</template>
              </el-table-column>
              <el-table-column prop="templateName" label="名称" min-width="190">
                <template #default="{ row }">
                  <span class="name-cell" :title="row.templateName">{{ row.templateName }}</span>
                  <el-tag v-if="row.isExchangeVoucher" type="warning" size="small" effect="dark">兑换券</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="券码" width="185">
                <template #default="{ row }">
                  <span v-if="row.couponCode" class="mono code-cell" :title="String(row.couponCode)" @click="copyCode(row.couponCode)">{{ row.couponCode }}</span>
                  <span v-else class="muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="权益" min-width="110">
                <template #default="{ row }">{{ row.benefitText }}{{ row.benefit2Text || '' }}</template>
              </el-table-column>
              <el-table-column label="次数" width="56" align="center">
                <template #default="{ row }">{{ row.benefitTimes ?? 1 }}</template>
              </el-table-column>
              <el-table-column prop="statusLabel" label="状态" width="70" />
              <el-table-column label="使用范围" width="110">
                <template #default="{ row }">
                  <template v-if="row.usableScenes">
                    <el-tag v-for="s in row.usableScenes.split('/')" :key="s" size="small" effect="plain" class="scene-tag">{{ s }}</el-tag>
                  </template>
                  <span v-else class="muted">不限</span>
                </template>
              </el-table-column>
              <el-table-column label="有效期" min-width="175">
                <template #default="{ row }">
                  <span v-if="row.useStartTimeStr || row.useEndTimeStr">
                    {{ row.useStartTimeStr || '?' }} ~ {{ row.useEndTimeStr || '?' }}
                  </span>
                  <span v-else class="muted">—</span>
                </template>
              </el-table-column>
            </el-table>
            <div class="pager-row">
              <el-pagination v-model:current-page="livePage" :page-size="livePageSize" :total="filteredCoupons.length"
                layout="total, prev, pager, next" background small />
            </div>
          </el-tab-pane>

          <el-tab-pane label="券档案库（全量模糊搜索）" name="archive">
            <div class="search-bar">
              <el-input v-model="archiveKeyword" :prefix-icon="Search" placeholder="档案库搜索：券码 / 名称 / 权益 / 使用范围 / 门槛 / token 指纹 / 账号"
                clearable style="max-width: 380px" @keyup.enter="searchArchive" />
              <el-select v-model="archiveBucket" style="width: 150px" placeholder="桶筛选">
                <el-option label="全部桶" value="" />
                <el-option label="可用 effective" value="effective" />
                <el-option label="历史 historical" value="historical" />
                <el-option label="试算 settle" value="settle_available" />
              </el-select>
              <el-select v-model="archiveScene" style="width: 130px" placeholder="使用范围">
                <el-option label="不限范围" value="" />
                <el-option label="自取" value="自取" />
                <el-option label="外卖" value="外卖" />
                <el-option label="团餐" value="团餐" />
              </el-select>
              <el-select v-if="categoryOptionsReady" v-model="archiveCategory" style="width: 150px" placeholder="成本子类">
                <el-option label="全部子类" value="" />
                <el-option label="未分类" :value="-1" />
                <el-option v-for="c in costCategories" :key="c.id" :label="c.name" :value="c.id" />
              </el-select>
              <el-button type="primary" :icon="Search" :loading="archiveLoading" @click="searchArchive">搜索档案</el-button>
              <span v-if="archiveTotal !== null" class="muted">
                命中 {{ archiveTotal }} 张 · 可用 {{ archiveStats.effective }} / 历史 {{ archiveStats.historical }} / 试算 {{ archiveStats.settle_available }} / 已使用 {{ archiveStats.used }}
              </span>
            </div>
            <el-table :data="archiveItems" stripe size="small">
              <el-table-column label="桶" width="110">
                <template #default="{ row }">
                  <el-tag :type="bucketTag(row.bucket)" size="small" effect="plain">{{ bucketLabel(row.bucket) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="account_label" label="归属账号" min-width="130">
                <template #default="{ row }">{{ row.account_label || '—' }}</template>
              </el-table-column>
              <el-table-column prop="template_name" label="名称" min-width="190" show-overflow-tooltip />
              <el-table-column label="券码" width="185">
                <template #default="{ row }">
                  <span class="mono code-cell" :title="row.coupon_code" @click="copyCode(row.coupon_code)">{{ row.coupon_code }}</span>
                </template>
              </el-table-column>
              <el-table-column label="面额" width="70" align="center">
                <template #default="{ row }">
                  {{ row.amount_display || (row.amount ? row.amount + '元' : '—') }}
                </template>
              </el-table-column>
              <el-table-column label="成本/子类" width="130">
                <template #default="{ row }">
                  <div class="cost-cell">
                    <div>
                      <span v-if="row.cost_price" class="price">¥{{ row.cost_price }}</span>
                      <span v-else class="muted">—</span>
                      <el-tag v-if="row.cost_source === 'fallback'" size="small" effect="plain" type="info"
                        class="pending-tag" title="成本未配置：按 面额 × 兜底系数 估算">待定</el-tag>
                    </div>
                    <div>
                      <el-tag v-if="row.cost_category_id" size="small" effect="plain"
                        :type="bizTag(row.biz_type)">{{ row.cost_category_name }}</el-tag>
                      <span v-else class="muted">未分类</span>
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="使用范围" width="110">
                <template #default="{ row }">
                  <template v-if="row.usable_scenes">
                    <el-tag v-for="s in row.usable_scenes.split('/')" :key="s" size="small" effect="plain" class="scene-tag">{{ s }}</el-tag>
                  </template>
                  <span v-else class="muted">不限</span>
                </template>
              </el-table-column>
              <el-table-column label="有效期" min-width="175">
                <template #default="{ row }">
                  <span v-if="row.use_start_time || row.use_end_time">
                    {{ fmtMs(row.use_start_time) || '?' }} ~ {{ fmtMs(row.use_end_time) || '?' }}
                  </span>
                  <span v-else class="muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="使用痕迹" width="150">
                <template #default="{ row }">
                  <span v-if="row.last_order_no" class="mono">{{ row.last_order_no }}</span>
                  <span v-else class="muted">未使用</span>
                </template>
              </el-table-column>
            </el-table>
            <div class="pager-row">
              <el-pagination v-model:current-page="archivePage" :page-size="archivePageSize" :total="archiveTotal || 0"
                layout="total, prev, pager, next" background small @current-change="searchArchive" />
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>

      <div class="page-card">
        <h3 class="sec-title">分类汇总</h3>
        <el-table :data="byTypeRows" size="small" border>
          <el-table-column prop="type" label="券类型" min-width="220" />
          <el-table-column prop="可用" label="可用" width="90" align="center" />
          <el-table-column prop="历史" label="历史" width="90" align="center" />
        </el-table>
        <p class="note">{{ result.summary.order_coupon_list }}</p>
      </div>
    </template>
    <div v-else class="page-card">
      <el-empty description="点击「全量查询（所有账号）」遍历收集所有券ID，或选择单账号查询" />
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { apiAccounts, apiOps, apiDecision } from '../api'

const route = useRoute()
const router = useRouter()

/* 业务类型配色：paid=采购付费(橙) / free=活动免费(绿) / bank=银行渠道(蓝) / other=其他(灰) */
const bizTag = (t) => ({ paid: 'warning', free: 'success', bank: 'primary', other: 'info' }[t] || 'info')

const accounts = ref([])
const accountId = ref(null)
const loadingAll = ref(false)
const loadingOne = ref(false)
const result = ref(null)
const mode = ref('all')            // all=全量遍历 / one=单账号
const lastSyncError = ref('')

// ---- 本次查询明细：本地多维度模糊过滤 ----
const keyword = ref('')
const searchField = ref('all')
const livePage = ref(1)
const livePageSize = 20

function fieldText(row, field) {
  switch (field) {
    case 'code': return String(row.couponCode || '')
    case 'name': return row.templateName || ''
    case 'scene': return row.usableScenes || ''
    default: return [
      row.couponCode, row.templateName, row.benefitText, row.benefit2Text,
      row.usableScenes, row.account_label, row.bucket,
    ].filter(Boolean).join(' ')
  }
}

const filteredCoupons = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw || !result.value) return result.value?.coupons || []
  return result.value.coupons.filter((row) => fieldText(row, searchField.value).toLowerCase().includes(kw))
})

const pagedLive = computed(() =>
  filteredCoupons.value.slice((livePage.value - 1) * livePageSize, livePage.value * livePageSize))

const byTypeRows = computed(() =>
  Object.entries(result.value?.summary?.by_type || {}).map(([type, c]) => ({
    type, 可用: c['可用'], 历史: c['历史'],
  })),
)

// ---- 券档案库：远端模糊搜索（/api/ops/coupons/search） ----
const activeTab = ref('live')
const archiveKeyword = ref('')
const archiveBucket = ref('')
const archiveScene = ref('')
const archiveItems = ref([])
const archiveTotal = ref(null)
const archiveStats = ref({ effective: 0, historical: 0, settle_available: 0, used: 0 })
const archiveLoading = ref(false)
const archivePage = ref(1)
const archivePageSize = 20

// ---- 成本子类筛选（fail-soft：接口不可用时静默隐藏该筛选，不弹错误） ----
const costCategories = ref([])
const categoryOptionsReady = ref(false)
const archiveCategory = ref('')   // ''=全部不筛 / -1=未分类 / 其他=子类 id

async function loadCostCategories() {
  try {
    const data = await apiDecision.costCategories({ silent: true })
    costCategories.value = data.items || []
    categoryOptionsReady.value = true
  } catch {
    /* fail-soft：保持筛选隐藏，不打扰用户 */
  }
}

async function searchArchive() {
  archiveLoading.value = true
  try {
    const params = {
      keyword: archiveKeyword.value.trim(),
      bucket: archiveBucket.value,
      scene: archiveScene.value,
      page: archivePage.value,
      page_size: archivePageSize,
    }
    // 子类筛选约定：'' 不传参 / -1=未分类 / 其他=子类 id（-1 由后端联调对齐）
    if (archiveCategory.value !== '' && archiveCategory.value !== null && archiveCategory.value !== undefined) {
      params.cost_category = archiveCategory.value
    }
    const data = await apiOps.couponsSearch(params)
    archiveItems.value = data.items
    archiveTotal.value = data.total
    archiveStats.value = data.stats
  } finally {
    archiveLoading.value = false
  }
}

function bucketLabel(b) {
  return { effective: '可用', historical: '历史', settle_available: '试算' }[b] || b
}
function bucketTag(b) {
  return { effective: 'success', historical: 'info', settle_available: 'warning' }[b] || 'info'
}
function fmtMs(ms) {
  if (!ms) return ''
  const d = new Date(Number(ms))
  if (Number.isNaN(d.getTime())) return ''
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
function copyCode(code) {
  navigator.clipboard?.writeText(String(code)).then(
    () => ElMessage.success(`券码已复制：${code}`),
    () => {},
  )
}

onMounted(async () => {
  const data = await apiAccounts.list({ page: 1, page_size: 100 })
  accounts.value = data.items
  const online = data.items.find((a) => a.status === 'online')
  if (online) accountId.value = online.id
  else if (data.items.length === 1) accountId.value = data.items[0].id
  loadCostCategories()   // 子类筛选选项（fail-soft，不阻塞首屏）
  // 联动入口：/ops/coupons?tab=archive&cost_category=N（来自「券成本与阈值 → 查看券」）
  if (route.query.tab === 'archive') activeTab.value = 'archive'
  const linkedCat = Number(route.query.cost_category)
  if (route.query.cost_category !== undefined && route.query.cost_category !== '' && Number.isFinite(linkedCat)) {
    archiveCategory.value = linkedCat
    // 应用后清掉深链参数，避免 URL 与页面实际状态脱节
    router.replace({ query: { ...route.query, tab: undefined, cost_category: undefined } })
  }
  searchArchive()   // 档案库首屏即载入（联动全量同步后的数据）
})

async function queryAll() {
  loadingAll.value = true
  result.value = null
  lastSyncError.value = ''
  try {
    result.value = await apiOps.couponsSyncAll()
    mode.value = 'all'
    livePage.value = 1
    activeTab.value = 'live'
    const errs = result.value.accounts?.filter((a) => a.result !== 'ok') || []
    if (errs.length) {
      lastSyncError.value = `遍历完成，但 ${errs.length} 个账号未成功：${errs.map((a) => `${a.label}（${{ expired: '凭证失效', error: '失败' }[a.result] || a.result}）`).join('、')}；其券数据未计入本次汇总`
    }
    ElMessage.success(`全量查询完成：扫描 ${result.value.scanned} 个账号，收集优惠券 ID ${result.value.distinct_coupon_ids} 个`)
    searchArchive()   // 刷新档案库（联动）
  } finally {
    loadingAll.value = false
  }
}

async function queryOne() {
  if (!accountId.value) {
    ElMessage.warning('请先选择账号')
    return
  }
  const acc = accounts.value.find((a) => a.id === accountId.value)
  if (acc && acc.status !== 'online') {
    ElMessage.warning('该账号当前非在线状态，查询大概率失败；请先在「账号管理」完成协议登录')
  }
  loadingOne.value = true
  result.value = null
  lastSyncError.value = ''
  try {
    result.value = await apiOps.coupons(accountId.value)
    mode.value = 'one'
    livePage.value = 1
    activeTab.value = 'live'
    ElMessage.success(`查询完成：可用 ${result.value.summary.effective_total} 张 / 可用次数 ${result.value.summary.effective_usable_times} 次 / 历史 ${result.value.summary.historical_total} 张`)
    searchArchive()
  } finally {
    loadingOne.value = false
  }
}
</script>

<style scoped>
.query-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.run-at {
  color: var(--muted);
}
.sync-alert {
  margin-top: 12px;
}
.sum-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 14px;
  margin: 16px 0;
}
.sum-card {
  background: #fff;
  border-radius: 14px;
  box-shadow: var(--card-shadow);
  padding: 18px;
  text-align: center;
  border-top: 3px solid var(--tea-600);
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.sum-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 22px rgba(30, 74, 62, 0.14);
}
.sum-card.hero {
  border-top-color: #33658a;
  background: linear-gradient(180deg, #eef4f8, #fff);
}
.sum-card.highlight {
  border-top-color: #c88a2a;
  background: linear-gradient(180deg, #fdf8ef, #fff);
}
.sum-value {
  font-size: 30px;
  font-weight: 700;
  color: var(--tea-800);
}
.hero .sum-value {
  color: #234a66;
}
.highlight .sum-value {
  color: #a86f14;
}
.sum-label {
  font-size: 13px;
  color: var(--muted);
  margin-top: 4px;
}
.sum-sub {
  font-size: 11px;
  color: var(--muted);
  margin-top: 2px;
  opacity: 0.85;
}
.acc-card {
  margin-bottom: 16px;
}
.search-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.search-hit {
  font-size: 12.5px;
}
.name-cell {
  word-break: break-all;
}
.code-cell {
  cursor: pointer;
  border-bottom: 1px dashed #b9cdc5;
}
.code-cell:hover {
  color: var(--tea-700);
}
.scene-tag {
  margin-right: 4px;
}
.cost-cell {
  line-height: 1.7;
}
.cost-cell .price {
  color: #c45656;
  font-weight: 600;
}
.pending-tag {
  margin-left: 4px;
}
.nick-tag {
  margin-left: 6px;
}
.err-text {
  color: #c45656;
  font-size: 12.5px;
}
.pager-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.sec-title {
  margin: 0 0 12px;
  font-size: 15px;
  color: var(--tea-800);
}
.note {
  color: var(--muted);
  font-size: 12.5px;
  margin: 10px 0 0;
}
.opt-sub {
  float: right;
  color: var(--muted);
  font-size: 12px;
  margin-left: 14px;
}
.muted {
  color: var(--muted);
}
@media (max-width: 1000px) {
  .sum-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
