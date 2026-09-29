<template>
  <div class="page-card">
    <div class="toolbar">
      <div>
        <h2 class="page-title">订单中枢 · 异步下单</h2>
        <p class="page-subtitle">
          客户订单登记 → SQLite 持久化队列 → worker 全自动执行（决策选号选券 → 试算 → 下单 → 支付收口取餐码）；
          接收即返 202，进度经 SSE 实时推送（本页自动刷新）
        </p>
      </div>
      <div class="filters">
        <el-tag v-if="live" type="success" size="small" effect="plain">SSE 实时</el-tag>
        <el-tag v-else type="info" size="small" effect="plain">轮询模式</el-tag>
      </div>
    </div>

    <!-- 队列健康卡 -->
    <div class="stat-row">
      <div class="stat-pill">待消费 {{ stats?.pending ?? '—' }}</div>
      <div class="stat-pill warn">执行中 {{ stats?.processing ?? '—' }}</div>
      <div class="stat-pill bad" :class="{ blink: (stats?.dead ?? 0) > 0 }">死信 {{ stats?.dead ?? '—' }}</div>
      <div class="stat-pill ok">worker {{ stats?.workers_alive?.length ?? 0 }}/{{ stats?.workers_configured ?? '—' }}</div>
      <div class="stat-pill ded">已完成 {{ statusCounts.completed ?? 0 }}</div>
      <div class="stat-pill bad">失败 {{ statusCounts.failed ?? 0 }}</div>
    </div>

    <el-tabs v-model="tab">
      <!-- ================= 登记单 ================= -->
      <el-tab-pane label="登记单" name="orders">
        <div class="filters" style="margin-bottom: 10px">
          <el-input v-model="query.keyword" placeholder="客户单号 / 茶姬单号 / 取餐码 / 来源" clearable
            style="width: 250px" :prefix-icon="Search" @keyup.enter="loadOrders" @clear="loadOrders" />
          <el-select v-model="query.status" placeholder="全部状态" clearable style="width: 130px" @change="loadOrders">
            <el-option v-for="(label, st) in STATUS" :key="st" :label="label" :value="st" />
          </el-select>
          <el-button :icon="Refresh" circle @click="loadAll" />
        </div>
        <el-table v-loading="loading" :data="items" stripe>
          <el-table-column label="客户单号" min-width="170">
            <template #default="{ row }">
              <el-link type="primary" @click="openDetail(row)">{{ row.customer_order_no }}</el-link>
              <div class="mono muted">{{ row.source }}</div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="105">
            <template #default="{ row }">
              <el-tag :type="STATUS_TAG[row.status]?.type || 'info'" size="small" effect="dark">
                {{ row.status_label || STATUS[row.status] || row.status }}
              </el-tag>
              <div class="muted" style="font-size: 12px">{{ row.step || '' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="商品 / 门店" min-width="180">
            <template #default="{ row }">
              <div>{{ row.goods_snapshot?.spu_name || row.sku_id }}</div>
              <div class="muted">×{{ row.quantity }} · {{ row.store_no }} · 售价 ¥{{ row.customer_price }}</div>
            </template>
          </el-table-column>
          <el-table-column label="茶姬单号 / 取餐码" min-width="180">
            <template #default="{ row }">
              <template v-if="row.chagee_order_no">
                <div class="mono">{{ row.chagee_order_no }}</div>
                <div v-if="row.pickup_no" class="pickup">{{ row.pickup_no }}</div>
                <div v-else class="muted">取餐码待回填</div>
              </template>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="支付" width="120">
            <template #default="{ row }">
              <el-link v-if="row.pay_url" :href="row.pay_url" target="_blank" type="warning">
                收银台 ¥{{ row.pay_amount }}
              </el-link>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="尝试" width="60" align="center">
            <template #default="{ row }"><span :class="{ bad: row.attempts > 1 }">{{ row.attempts }}</span></template>
          </el-table-column>
          <el-table-column label="更新时间" width="165">
            <template #default="{ row }"><span class="mono">{{ fmtTime(row.updated_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="150" fixed="right">
            <template #default="{ row }">
              <!-- 取消/重放入队是管理动作（后端 intake:manage），仅登记权限用户不渲染 -->
              <el-button v-perm="'intake:manage'" v-if="['registered', 'enqueued'].includes(row.status)" size="small" @click="doCancel(row)">取消</el-button>
              <el-button v-perm="'intake:manage'" v-if="['failed', 'cancelled'].includes(row.status)" size="small" type="primary"
                @click="doRequeue(row)">重新入队</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination v-if="total > query.page_size" layout="total, prev, pager, next" :total="total"
          :page-size="query.page_size" v-model:current-page="query.page" @current-change="loadOrders" />
      </el-tab-pane>

      <!-- ================= 死信 ================= -->
      <el-tab-pane :label="`死信 (${stats?.dead ?? 0})`" name="dead">
        <el-table v-loading="deadLoading" :data="deadItems" stripe>
          <el-table-column label="消息" width="90">
            <template #default="{ row }"><span class="mono">#{{ row.id }}</span></template>
          </el-table-column>
          <el-table-column label="客户单号" min-width="160">
            <template #default="{ row }"><span class="mono">{{ row.customer_order_no || `co#${row.customer_order_id}` }}</span></template>
          </el-table-column>
          <el-table-column label="尝试" width="80" align="center">
            <template #default="{ row }">{{ row.attempts }} / {{ row.max_attempts }}</template>
          </el-table-column>
          <el-table-column label="最后错误" min-width="320">
            <template #default="{ row }"><span class="mono bad">{{ row.last_error || '—' }}</span></template>
          </el-table-column>
          <el-table-column label="死信时间" width="165">
            <template #default="{ row }"><span class="mono">{{ fmtTime(row.done_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="110" fixed="right">
            <template #default="{ row }">
              <el-button v-perm="'intake:manage'" size="small" type="primary" @click="doDeadRequeue(row)">重放</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ================= 接入密钥 ================= -->
      <el-tab-pane label="接入密钥" name="keys">
        <div class="filters" style="margin-bottom: 10px">
          <el-button v-perm="'intake:manage'" type="primary" :icon="Plus" @click="keyDialog = true">新建密钥</el-button>
          <el-button :icon="Refresh" circle @click="loadKeys" />
        </div>
        <el-table :data="keyItems" stripe>
          <el-table-column label="ID" width="60" prop="id" />
          <el-table-column label="备注" min-width="160" prop="label" />
          <el-table-column label="来源" width="130">
            <template #default="{ row }"><el-tag size="small">{{ row.source }}</el-tag></template>
          </el-table-column>
          <el-table-column label="指纹" min-width="150">
            <template #default="{ row }"><span class="mono muted">{{ row.key_preview }}</span></template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.active ? 'success' : 'danger'" size="small" effect="dark">
                {{ row.active ? '生效' : '已吊销' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="最近使用" width="165">
            <template #default="{ row }"><span class="mono">{{ fmtTime(row.last_used_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="110" fixed="right">
            <template #default="{ row }">
              <el-button v-perm="'intake:manage'" v-if="row.active" size="small" type="danger" @click="doKeyDisable(row)">吊销</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 登记单详情抽屉 -->
    <el-drawer v-model="detailVisible" :title="detail?.customer_order_no || '登记单详情'" size="480px">
      <template v-if="detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="状态">
            <el-tag :type="STATUS_TAG[detail.status]?.type || 'info'" size="small" effect="dark">
              {{ detail.status_label }}
            </el-tag>
            <span class="muted"> · {{ detail.step }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="进度">{{ detail.progress || '—' }}</el-descriptions-item>
          <el-descriptions-item label="来源">{{ detail.source }}</el-descriptions-item>
          <el-descriptions-item label="商品">
            {{ detail.goods_snapshot?.spu_name || detail.sku_id }} ×{{ detail.quantity }}（{{ detail.sku_id }}）
          </el-descriptions-item>
          <el-descriptions-item label="门店">{{ detail.store_no }}</el-descriptions-item>
          <el-descriptions-item label="客户支付价">¥{{ detail.customer_price }}</el-descriptions-item>
          <el-descriptions-item label="执行账号">{{ detail.account_id || '—' }}</el-descriptions-item>
          <el-descriptions-item label="茶姬单号">
            <span class="mono">{{ detail.chagee_order_no || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="取餐码">
            <span v-if="detail.pickup_no" class="pickup">{{ detail.pickup_no }}</span>
            <span v-else class="muted">待回填</span>
          </el-descriptions-item>
          <el-descriptions-item label="支付链接">
            <el-link v-if="detail.pay_url" :href="detail.pay_url" target="_blank" type="warning">
              收银台 ¥{{ detail.pay_amount }}
            </el-link>
            <span v-else class="muted">—</span>
          </el-descriptions-item>
          <el-descriptions-item label="用券">{{ detail.coupon_code || '—' }}</el-descriptions-item>
          <el-descriptions-item label="回调地址">{{ detail.callback_url || '—' }}</el-descriptions-item>
          <el-descriptions-item label="尝试次数">{{ detail.attempts }}</el-descriptions-item>
          <el-descriptions-item label="trace_id">
            <span class="mono muted">{{ detail.trace_id || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="登记 / 完成">
            {{ fmtTime(detail.created_at) }} → {{ fmtTime(detail.finished_at) }}
          </el-descriptions-item>
        </el-descriptions>
        <el-alert v-if="detail.error" :title="detail.error" type="error" :closable="false" style="margin-top: 12px" />
      </template>
    </el-drawer>

    <!-- 新建密钥 -->
    <el-dialog v-model="keyDialog" title="新建接入密钥" width="440px">
      <el-form label-width="70px">
        <el-form-item label="备注">
          <el-input v-model="keyForm.label" placeholder="如：XX平台对接" />
        </el-form-item>
        <el-form-item label="来源">
          <el-input v-model="keyForm.source" placeholder="external（登记单 source 前缀）" />
        </el-form-item>
      </el-form>
      <el-alert v-if="newKey" :title="`密钥（仅此一次显示）：${newKey}`" type="warning" :closable="false"
        style="margin-top: 8px; word-break: break-all" />
      <template #footer>
        <el-button @click="keyDialog = false">关闭</el-button>
        <el-button type="primary" :disabled="!keyForm.label" @click="doKeyCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { apiIntake } from '../api'
import { openEventStream } from '../utils/sse'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()

const STATUS = {
  registered: '已登记', enqueued: '已入队', processing: '处理中',
  awaiting_payment: '待支付', completed: '已完成', failed: '已失败', cancelled: '已取消',
}
const STATUS_TAG = {
  registered: { type: 'info' }, enqueued: { type: 'info' }, processing: { type: 'warning' },
  awaiting_payment: { type: 'warning' }, completed: { type: 'success' },
  failed: { type: 'danger' }, cancelled: { type: 'info' },
}

const tab = ref('orders')
const loading = ref(false)
const deadLoading = ref(false)
const items = ref([])
const total = ref(0)
const deadItems = ref([])
const keyItems = ref([])
const stats = ref(null)
const query = reactive({ keyword: '', status: '', page: 1, page_size: 20 })
const detail = ref(null)
const detailVisible = ref(false)
const keyDialog = ref(false)
const keyForm = reactive({ label: '', source: 'external' })
const newKey = ref('')
const live = ref(false)

const statusCounts = computed(() => {
  const out = {}
  // stats.by_status 为后端分组计数（随队列统计一起返回自 orders 列表接口）
  for (const [k, v] of Object.entries(byStatus.value || {})) out[k] = v
  return out
})
const byStatus = ref({})

function fmtTime(v) {
  if (!v) return '—'
  return String(v).replace('T', ' ').slice(0, 19)
}

async function loadOrders() {
  loading.value = true
  try {
    const r = await apiIntake.orders({ ...query })
    items.value = r.items
    total.value = r.total
    byStatus.value = r.stats?.by_status || {}
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  try {
    stats.value = await apiIntake.queueStats()
  } catch { /* 静默：统计失败不影响列表 */ }
}

async function loadDead() {
  deadLoading.value = true
  try {
    const r = await apiIntake.queueMessages({ status: 'dead', page_size: 50 })
    deadItems.value = r.items
  } finally {
    deadLoading.value = false
  }
}

async function loadKeys() {
  const r = await apiIntake.keys()
  keyItems.value = r.items
}

function loadAll() {
  loadOrders()
  loadStats()
  if (tab.value === 'dead') loadDead()
}

function openDetail(row) {
  detail.value = row
  detailVisible.value = true
}

async function doCancel(row) {
  await ElMessageBox.confirm(
    `确认取消登记单 ${row.customer_order_no}？（仅入队前可取消）`, '取消登记', { type: 'warning' })
  await apiIntake.cancel(row.customer_order_no)
  ElMessage.success('已取消')
  loadAll()
}

async function doRequeue(row) {
  await apiIntake.requeue(row.customer_order_no)
  ElMessage.success('已重新入队')
  loadAll()
}

async function doDeadRequeue(row) {
  await apiIntake.deadRequeue(row.id)
  ElMessage.success('死信已重放')
  loadDead()
  loadStats()
}

async function doKeyCreate() {
  const r = await apiIntake.keyCreate({ ...keyForm })
  newKey.value = r.api_key
  ElMessage.success('密钥已创建（明文仅显示一次）')
  loadKeys()
}

async function doKeyDisable(row) {
  await ElMessageBox.confirm(`确认吊销密钥「${row.label}」？吊销后该平台请求立即 401`, '吊销密钥', { type: 'warning' })
  await apiIntake.keyDisable(row.id)
  ElMessage.success('已吊销')
  loadKeys()
}

// SSE 实时刷新：topic "intake"（状态迁移逐帧推送）；无权限/断流自动回落手动刷新
let stream = null
let pollTimer = null

onMounted(() => {
  loadAll()
  if (auth.can('feature:order')) {
    stream = openEventStream({
      topics: ['intake'],
      events: {
        order_status: (data) => {
          // 轻量增量：当前页命中行直接刷新（首屏/翻页仍走全量拉取）
          if (data?.customer_order_no) {
            const hit = items.value.find((x) => x.customer_order_no === data.customer_order_no)
            if (hit) {
              hit.status = data.status
              hit.step = data.step
              hit.progress = data.progress
              hit.pickup_no = data.pickup_no || hit.pickup_no
              hit.chagee_order_no = data.chagee_order_no || hit.chagee_order_no
              hit.pay_url = data.pay_url || hit.pay_url
            }
          }
          loadStats()
        },
      },
      onOpen: () => { live.value = true },
      onError: () => { live.value = false },
    })
  }
  pollTimer = setInterval(() => { loadStats(); if (tab.value === 'dead') loadDead() }, 15000)
})

onBeforeUnmount(() => {
  stream?.close()
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.filters { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.stat-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.stat-pill { padding: 4px 12px; border-radius: 14px; font-size: 13px; background: #f0f2f5; color: #606266; }
.stat-pill.ok { background: #f0f9eb; color: #67c23a; }
.stat-pill.warn { background: #fdf6ec; color: #e6a23c; }
.stat-pill.bad { background: #fef0f0; color: #f56c6c; }
.stat-pill.ded { background: #f4f0ff; color: #7a5af8; }
.stat-pill.blink { animation: blink 1.2s infinite; }
@keyframes blink { 50% { opacity: 0.45; } }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12.5px; }
.muted { color: #909399; font-size: 12px; }
.pickup { color: #67c23a; font-weight: 700; font-size: 15px; letter-spacing: 1px; }
.bad { color: #f56c6c; }
</style>
