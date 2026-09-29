<template>
  <div>
    <!-- 状态卡：开关 / 下次定时 / 手动触发 / 最近一轮 -->
    <div class="page-card">
      <div class="toolbar" style="justify-content: space-between">
        <div>
          <h2 class="page-title">账号保活跃任务</h2>
          <p class="page-subtitle">
            每天 {{ form.run_at }} 用在线账号 token 间歇访问广东省内门店菜单接口维持活跃；
            每账号先做 token 鉴权校验（whoami），失效即标记账号并告警
          </p>
        </div>
        <div style="display: flex; gap: 12px; align-items: center">
          <el-tag v-if="status.running" type="warning" effect="dark">运行中 #{{ status.current_run_id }}</el-tag>
          <el-tag v-else-if="status.next_run_at" type="info">下次：{{ status.next_run_at }}</el-tag>
          <el-switch v-model="form.enabled" :loading="saving" active-text="定时开关" @change="saveConfig" />
          <el-button type="primary" :loading="status.running" @click="trigger">手动运行一轮</el-button>
        </div>
      </div>
      <el-descriptions v-if="status.last_run" :column="4" border size="small" style="margin-top: 8px">
        <el-descriptions-item label="最近一轮">
          #{{ status.last_run.id }}（{{ status.last_run.trigger === 'manual' ? '手动' : '定时' }}）
          <el-tag size="small" :type="runTagType(status.last_run.status)">{{ runStatusText(status.last_run.status) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="开始 / 结束">
          {{ status.last_run.started_at }} ~ {{ status.last_run.finished_at || '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="门店 / 账号">
          {{ status.last_run.stores_planned }}（共 {{ status.last_run.city_total }} 市 / {{ status.last_run.store_total }} 店）/ {{ status.last_run.accounts_total }} 账号
        </el-descriptions-item>
        <el-descriptions-item label="请求 成功/失败">
          {{ status.last_run.requests_ok }} / {{ status.last_run.requests_failed }}
          <span v-if="status.last_run.avg_ms" class="muted">（均 {{ status.last_run.avg_ms }}ms）</span>
          <el-tag v-if="status.last_run.accounts_expired" size="small" type="danger" style="margin-left: 6px">
            token 失效 {{ status.last_run.accounts_expired }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
    </div>

    <!-- 配置卡 -->
    <div class="page-card">
      <h3 class="sec-title">运行配置（保存后 30 秒内热生效）</h3>
      <el-form :model="form" label-width="180px" style="max-width: 720px">
        <el-form-item label="每天运行时刻">
          <el-input v-model="form.run_at" style="width: 120px" placeholder="10:30" />
          <span class="form-hint">HH:MM；已过时刻自动顺延到明天</span>
        </el-form-item>
        <el-form-item label="省份（cityCode 前缀）">
          <el-input v-model="form.province_city_prefix" style="width: 120px" placeholder="44" />
          <span class="form-hint">44=广东（行政区划码）；广东 21 市</span>
        </el-form-item>
        <el-form-item label="请求间歇（秒）">
          <el-input v-model="form.min_interval_seconds" style="width: 90px" /> ~
          <el-input v-model="form.max_interval_seconds" style="width: 90px" />
          <span class="form-hint">每次请求间随机停顿区间，避免集中请求</span>
        </el-form-item>
        <el-form-item label="每轮门店上限">
          <el-input v-model="form.max_stores_per_run" style="width: 120px" />
          <span class="form-hint">0 = 省内全部门店；>0 时随机取样截断</span>
        </el-form-item>
        <el-form-item label="失败重试次数">
          <el-input v-model="form.max_retries" style="width: 120px" />
          <span class="form-hint">瞬时失败退避重试（3s/6s/…）；token 失效不重试</span>
        </el-form-item>
        <el-form-item label="token 鉴权校验">
          <el-switch v-model="whoamiBool" />
          <span class="form-hint">每账号每轮先 whoami 验活（失效即标记 expired + 告警）</span>
        </el-form-item>
        <el-form-item label="失败告警阈值">
          失败数 ≥ <el-input v-model="form.alert_min_failures" style="width: 80px" /> 且 失败率 ≥
          <el-input v-model="form.alert_failure_rate" style="width: 80px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="saveConfig">保存配置</el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- 运行记录 -->
    <div class="page-card">
      <div class="toolbar">
        <h3 class="sec-title" style="margin: 0">运行记录</h3>
        <el-button :icon="Refresh" circle @click="loadRuns" />
      </div>
      <el-table :data="runs" size="small" stripe>
        <el-table-column prop="id" label="#" width="64" />
        <el-table-column label="触发" width="72">
          <template #default="{ row }">{{ row.trigger === 'manual' ? '手动' : '定时' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="84">
          <template #default="{ row }">
            <el-tag size="small" :type="runTagType(row.status)">{{ runStatusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="门店（计划/总）" width="120">
          <template #default="{ row }">{{ row.stores_planned }} / {{ row.store_total }}</template>
        </el-table-column>
        <el-table-column prop="accounts_total" label="账号" width="64" />
        <el-table-column label="请求 成/败" width="100">
          <template #default="{ row }">{{ row.requests_ok }} / {{ row.requests_failed }}</template>
        </el-table-column>
        <el-table-column prop="avg_ms" label="均耗ms" width="80" />
        <el-table-column label="开始" width="150">
          <template #default="{ row }">{{ row.started_at }}</template>
        </el-table-column>
        <el-table-column label="备注" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.note || '—' }}</template>
        </el-table-column>
        <el-table-column label="明细" width="88">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination v-if="runsTotal > 10" v-model:current-page="runsPage" :page-size="10"
                     :total="runsTotal" layout="total, prev, pager, next" style="margin-top: 10px"
                     @current-change="loadRuns" />
    </div>

    <!-- 逐请求明细抽屉 -->
    <el-drawer v-model="detailVisible" :title="`运行 #${detailRun?.id ?? ''} 逐请求明细`" size="70%">
      <el-radio-group v-model="detailFilter" size="small" style="margin-bottom: 10px" @change="loadDetail">
        <el-radio-button value="">全部</el-radio-button>
        <el-radio-button value="ok">成功</el-radio-button>
        <el-radio-button value="failed">失败</el-radio-button>
      </el-radio-group>
      <el-table :data="records" size="small" stripe>
        <el-table-column prop="created_at" label="时间" width="150" />
        <el-table-column prop="account_label" label="账号" width="130" show-overflow-tooltip />
        <el-table-column prop="action" label="类型" width="76">
          <template #default="{ row }">
            <el-tag size="small" :type="row.action === 'whoami' ? 'warning' : 'info'">
              {{ row.action === 'whoami' ? '验活' : '菜单' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="门店" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ row.store_no ? `${row.store_name}（${row.store_no}）` : '—' }}</template>
        </el-table-column>
        <el-table-column label="结果" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.ok ? 'success' : 'danger'">{{ row.ok ? '成功' : '失败' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="110" show-overflow-tooltip />
        <el-table-column prop="attempt" label="尝试" width="60" />
        <el-table-column label="耗时" width="86">
          <template #default="{ row }">{{ row.ms }}ms</template>
        </el-table-column>
        <el-table-column prop="error" label="错误" min-width="180" show-overflow-tooltip />
      </el-table>
      <el-pagination v-if="recordsTotal > 50" v-model:current-page="recordsPage" :page-size="50"
                     :total="recordsTotal" layout="total, prev, pager, next" style="margin-top: 10px"
                     @current-change="loadDetail" />
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { apiKeepalive } from '../api'

const status = reactive({ running: false, current_run_id: 0, next_run_at: '', enabled: true, last_run: null })
const form = reactive({
  enabled: true, run_at: '10:30', province_city_prefix: '44',
  min_interval_seconds: '5', max_interval_seconds: '12', max_stores_per_run: '0',
  max_retries: '2', whoami_check: 'true', alert_failure_rate: '0.5', alert_min_failures: '3',
})
const saving = ref(false)
const runs = ref([])
const runsTotal = ref(0)
const runsPage = ref(1)
const detailVisible = ref(false)
const detailRun = ref(null)
const records = ref([])
const recordsTotal = ref(0)
const recordsPage = ref(1)
const detailFilter = ref('')

const whoamiBool = computed({
  get: () => form.whoami_check === 'true',
  set: (v) => { form.whoami_check = v ? 'true' : 'false' },
})

const runTagType = (s) => ({ success: 'success', partial: 'warning', failed: 'danger', running: 'info' }[s] || 'info')
const runStatusText = (s) => ({ success: '成功', partial: '部分失败', failed: '失败', running: '运行中' }[s] || s)

async function loadStatus() {
  const s = await apiKeepalive.status()
  Object.assign(status, s)
}

async function loadConfig() {
  const c = await apiKeepalive.config()
  Object.assign(form, c)
}

async function saveConfig() {
  saving.value = true
  try {
    const saved = await apiKeepalive.configSave({ ...form })
    Object.assign(form, saved)
    ElMessage.success('配置已保存（30 秒内热生效）')
    await loadStatus()
  } finally {
    saving.value = false
  }
}

async function trigger() {
  await apiKeepalive.trigger()
  ElMessage.success('已触发手动运行（后台执行，可稍后刷新查看）')
  setTimeout(loadStatus, 1500)
}

async function loadRuns() {
  const r = await apiKeepalive.runs({ page: runsPage.value, page_size: 10 })
  runs.value = r.items
  runsTotal.value = r.total
}

async function openDetail(row) {
  detailRun.value = row
  detailFilter.value = ''
  recordsPage.value = 1
  detailVisible.value = true
  await loadDetail()
}

async function loadDetail() {
  if (!detailRun.value) return
  const r = await apiKeepalive.runDetail(detailRun.value.id, {
    records_page: recordsPage.value, page_size: 50, result: detailFilter.value,
  })
  records.value = r.records
  recordsTotal.value = r.records_total
}

onMounted(async () => {
  await Promise.all([loadStatus(), loadConfig(), loadRuns()])
})
</script>

<style scoped>
.form-hint {
  margin-left: 10px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.sec-title {
  margin: 4px 0 12px;
  font-size: 15px;
}
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
</style>
