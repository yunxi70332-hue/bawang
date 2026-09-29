<template>
  <div class="settings-grid">
    <!-- ============ 状态卡片：实时出口 / 代理连接 ============ -->
    <div class="page-card">
      <div class="status-head">
        <h3 class="section-title">代理出口状态</h3>
        <div class="status-actions">
          <el-button size="small" :loading="refreshing" @click="refresh(true)">立即检测</el-button>
          <el-tag v-if="sseAlive" size="small" type="success" effect="plain">实时</el-tag>
          <el-tag v-else size="small" type="info" effect="plain">轮询</el-tag>
        </div>
      </div>

      <el-alert v-if="status.warning" :title="status.warning" type="warning" show-icon :closable="false" class="mb12" />

      <el-descriptions :column="isMobileLayout ? 1 : 2" border>
        <el-descriptions-item label="连接状态">
          <el-tag :type="statusTagType" effect="dark">{{ status.status_text || status.status || '—' }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="当前出口 IP">
          <span class="mono">{{ activeEgress.ip || '检测中…' }}</span>
          <el-tag v-if="activeEgress.mainland === true" size="small" type="success" effect="plain" class="ml4">大陆</el-tag>
          <el-tag v-else-if="activeEgress.mainland === false" size="small" type="danger" effect="plain" class="ml4">非大陆</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="出口归属地">{{ activeEgress.region || '—' }}</el-descriptions-item>
        <el-descriptions-item label="生效代理">
          <span class="mono">{{ status.route === 'proxy' ? (status.proxy?.addr || '—') : '（服务器直连）' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="代理协议">{{ status.route === 'proxy' ? protoText(status.proxy?.protocol) : '—' }}</el-descriptions-item>
        <el-descriptions-item label="出口判定来源">{{ status.route === 'proxy' ? (status.proxy?.egress_source === 'provider' ? '服务商元数据(realIp)' : status.proxy?.egress_source === 'echo' ? '出口探测' : '未知') : '—' }}</el-descriptions-item>
        <el-descriptions-item label="上次检测">{{ fmtTs(status.last_check_at) }}</el-descriptions-item>
        <el-descriptions-item label="直连基线（服务器）">
          <span class="mono">{{ status.direct?.ip || '—' }}</span>
          <span v-if="status.direct?.region" class="muted"> {{ status.direct.region }}</span>
        </el-descriptions-item>
      </el-descriptions>

      <div v-if="status.last_error" class="last-error">
        <el-icon><WarningFilled /></el-icon>
        {{ status.last_error }}
      </div>

      <div class="stats-row">
        <div class="stat"><div class="stat-num">{{ stats.requests_proxied }}</div><div class="stat-label">经代理请求</div></div>
        <div class="stat"><div class="stat-num">{{ stats.requests_direct }}</div><div class="stat-label">直连请求</div></div>
        <div class="stat"><div class="stat-num">{{ stats.fallback_retries }}</div><div class="stat-label">代理故障直连重试</div></div>
        <div class="stat"><div class="stat-num">{{ stats.rotations }}</div><div class="stat-label">代理轮换次数</div></div>
      </div>
      <div class="hint">账号相关请求（登录 / 查券 / 下单 / 取餐码）始终优先经大陆代理出口；代理不可用时自动无缝回退服务器直连，并在恢复后自动切回。</div>
    </div>

    <!-- ============ 配置卡片 ============ -->
    <div class="page-card">
      <h3 class="section-title">代理配置</h3>
      <el-form ref="formRef" :model="form" :rules="rules" label-width="110px" class="cfg-form">
        <el-form-item label="启用代理">
          <el-switch v-model="form.enabled" active-text="账号请求经代理" inactive-text="全部直连" />
        </el-form-item>

        <el-form-item label="代理来源">
          <el-radio-group v-model="form.mode" :disabled="!form.enabled">
            <el-radio-button value="api">提取 API（推荐）</el-radio-button>
            <el-radio-button value="manual">固定代理</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <template v-if="form.mode === 'api'">
          <el-form-item label="提取 API 地址" prop="api_url">
            <el-input v-model="form.api_url" placeholder="https://api.hailiangip.com:8522/api/getIpEncrypt?..." clearable />
          </el-form-item>
          <el-form-item label="数据格式">
            <el-radio-group v-model="form.api_format">
              <el-radio-button value="json">JSON</el-radio-button>
              <el-radio-button value="txt">TXT</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="代理有效期">
            <el-input-number v-model="form.ttl_seconds" :min="30" :max="86400" :step="30" controls-position="right" />
            <span class="unit">秒（到期前自动轮换）</span>
          </el-form-item>
          <div class="hint mb12">
            白名单模式无需认证；需账密认证的服务商在下方「认证信息」填写，将随每个代理一起使用。
            若服务商把 IP 探测站（如 myip.ipip.net）或业务域名加入黑名单，测试连接会原样透出（614 domain is black 等），需联系服务商授权。
          </div>
        </template>

        <template v-else>
          <el-form-item label="代理地址" prop="manual_host">
            <el-input v-model="form.manual_host" placeholder="代理服务器 IP 或域名" style="width: 240px" />
          </el-form-item>
          <el-form-item label="端口" prop="manual_port">
            <el-input-number v-model="form.manual_port" :min="1" :max="65535" controls-position="right" style="width: 160px" />
          </el-form-item>
        </template>

        <el-form-item label="代理协议">
          <el-radio-group v-model="form.protocol">
            <el-radio-button value="http">HTTP / HTTPS</el-radio-button>
            <el-radio-button value="socks5">SOCKS5</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item label="认证信息">
          <el-input v-model="form.username" placeholder="用户名（白名单模式留空）" style="width: 180px" class="mr8" />
          <el-input v-model="form.password" type="password" show-password placeholder="密码（留空=不改）" style="width: 180px" />
        </el-form-item>

        <el-form-item label="检测间隔">
          <el-input-number v-model="form.check_interval" :min="10" :max="3600" :step="10" controls-position="right" />
          <span class="unit">秒（后台健康检测周期）</span>
        </el-form-item>

        <el-form-item label="大陆校验">
          <el-switch v-model="form.require_mainland" active-text="非大陆出口视为无效" />
        </el-form-item>

        <el-collapse class="advanced">
          <el-collapse-item title="高级：经代理的域名后缀" name="domains">
            <el-select v-model="form.route_domains" multiple filterable allow-create default-first-option
                       placeholder="输入域名后缀回车添加" style="width: 100%">
              <el-option v-for="d in form.route_domains" :key="d" :label="d" :value="d" />
            </el-select>
            <div class="hint mt4">命中后缀的出站请求才经代理路由（默认茶姬 API 域 chagee.com / bwcj.com），其余一律直连。</div>
          </el-collapse-item>
        </el-collapse>

        <el-form-item class="form-actions">
          <el-button type="primary" :loading="saving" @click="save">保存并生效</el-button>
          <el-button :loading="testing" @click="runTest">测试连接（不保存）</el-button>
        </el-form-item>
      </el-form>

      <!-- 干跑诊断结果 -->
      <div v-if="testResult" class="test-result">
        <el-divider content-position="left">
          测试结果（{{ testResult.ok ? '通过' : '未通过' }}）
        </el-divider>
        <div v-for="(s, i) in testResult.steps" :key="i" class="test-step">
          <el-tag :type="s.ok ? 'success' : 'danger'" size="small" effect="plain" class="step-tag">{{ stepName(s.step) }}</el-tag>
          <span class="step-detail">{{ s.detail }}</span>
        </div>
      </div>
    </div>

    <!-- ============ 切换日志卡片 ============ -->
    <div class="page-card">
      <div class="status-head">
        <h3 class="section-title">代理操作日志</h3>
        <el-button size="small" @click="loadLogs">刷新</el-button>
      </div>
      <el-table :data="logs" size="small" stripe max-height="360">
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ row.ts }}</template>
        </el-table-column>
        <el-table-column label="动作" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="logTagType(row.action)" effect="plain">{{ logName(row.action) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="级别" width="70">
          <template #default="{ row }">
            <el-tag size="small" :type="row.level === 'ERROR' ? 'danger' : row.level === 'WARN' ? 'warning' : 'info'" effect="plain">{{ row.level }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="详情" min-width="320">
          <template #default="{ row }">
            <span class="mono log-detail">{{ logBrief(row) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiSettings } from '../api'
import { openEventStream } from '../utils/sse'
import { fmtTime } from '../utils/format'

const formRef = ref()
const saving = ref(false)
const testing = ref(false)
const refreshing = ref(false)
const status = ref({})
const testResult = ref(null)
const logs = ref([])
const sseAlive = ref(false)

const form = reactive({
  enabled: false, mode: 'api', api_url: '', api_format: 'json', protocol: 'http',
  username: '', password: '', ttl_seconds: 120, check_interval: 30,
  manual_host: '', manual_port: 1080, require_mainland: true,
  route_domains: ['chagee.com', 'bwcj.com'],
})

const rules = {
  api_url: [{ required: true, message: '请输入提取 API 地址', trigger: 'blur' }],
  manual_host: [{ required: true, message: '请输入代理服务器地址', trigger: 'blur' }],
}

const isMobileLayout = computed(() => window.innerWidth < 900)
const activeEgress = computed(() =>
  status.value.route === 'proxy'
    ? { ip: status.value.proxy?.egress_ip, region: status.value.proxy?.egress_region, mainland: status.value.proxy?.egress_mainland }
    : { ip: status.value.direct?.ip, region: status.value.direct?.region, mainland: status.value.direct?.mainland },
)
const stats = computed(() => status.value.stats || {})
const statusTagType = computed(() => ({
  proxy_active: 'success', fallback_direct: 'danger', disabled: 'info', not_configured: 'warning',
}[status.value.status] || 'info'))

let sse = null
let pollTimer = null

// 后端状态时间戳为 epoch 秒（fmtTime 需要 ms/ISO）
function fmtTs(v) { return v ? fmtTime(v < 1e12 ? v * 1000 : v) : '—' }

function protoText(p) { return p === 'socks5' ? 'SOCKS5' : 'HTTP / HTTPS' }
function stepName(s) {
  return { extract: '① 提取代理', tunnel: '② 隧道探测', request: '③ 经代理访问茶姬', mainland: '④ 大陆校验', direct: '⑤ 直连基线' }[s] || s
}
function logName(a) {
  return { 'proxy.config_save': '配置保存', 'proxy.fetch': '提取代理', 'proxy.switch': '出口切换', 'proxy.egress': '请求出口' }[a] || a
}
function logTagType(a) {
  if (a === 'proxy.switch' || a === 'proxy.fetch') return 'warning'
  if (a === 'proxy.egress') return 'success'
  return 'info'
}
function logBrief(row) {
  try { return JSON.stringify(JSON.parse(row.params || '{}')) } catch { return row.params || row.error || '' }
}

async function loadConfig() {
  const body = await apiSettings.getConfig()
  status.value = body.status || {}
  const cfg = body.config || {}
  Object.assign(form, {
    enabled: cfg.enabled, mode: cfg.mode, api_url: cfg.api_url,
    api_format: cfg.api_format, protocol: cfg.protocol,
    username: cfg.username || '', password: cfg.password || '',
    ttl_seconds: cfg.ttl_seconds, check_interval: cfg.check_interval,
    manual_host: cfg.manual_host, manual_port: cfg.manual_port || 1080,
    require_mainland: cfg.require_mainland,
    route_domains: cfg.route_domains || ['chagee.com', 'bwcj.com'],
  })
}

async function refresh(force = false) {
  refreshing.value = true
  try {
    // 拦截器已解包 body：端点直接返回状态对象
    status.value = force ? await apiSettings.refresh() : (await apiSettings.getConfig()).status
  } catch (e) { /* 拦截器已提示 */ }
  refreshing.value = false
}

async function save() {
  const valid = await formRef.value.validate().then(() => true).catch(() => false)
  if (!valid) return
  saving.value = true
  try {
    const body = await apiSettings.save({ ...form })
    status.value = body.status
    form.password = body.config?.password || ''
    ElMessage.success('代理配置已保存并即时生效')
    loadLogs()
  } catch (e) { /* 拦截器已提示 */ }
  saving.value = false
}

async function runTest() {
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await apiSettings.test({ ...form })
  } catch (e) { /* 拦截器已提示 */ }
  testing.value = false
}

async function loadLogs() {
  try {
    const body = await apiSettings.logs()
    logs.value = body.items || []
  } catch (e) { /* 静默 */ }
}

function startSse() {
  sse = openEventStream({
    topics: ['proxy'],
    events: {
      status: (snap) => { status.value = snap; if (snap.route === 'proxy' && snap.status === 'proxy_active') loadLogs() },
    },
    onOpen: () => { sseAlive.value = true },
    onError: () => { sseAlive.value = false },   // 断流降级轮询，浏览器自动重连
  })
}

onMounted(() => {
  loadConfig()
  loadLogs()
  startSse()
  pollTimer = setInterval(() => { if (!sseAlive.value) refresh() }, 30000)
})
onUnmounted(() => {
  sse?.close()
  clearInterval(pollTimer)
})
</script>

<style scoped>
.settings-grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
.status-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.status-actions { display: flex; gap: 8px; align-items: center; }
.mb12 { margin-bottom: 12px; }
.mt4 { margin-top: 4px; }
.ml4 { margin-left: 4px; }
.mr8 { margin-right: 8px; }
.mono { font-family: Consolas, Monaco, monospace; }
.muted { color: #8a968f; font-size: 12px; }
.unit { margin-left: 8px; color: #8a968f; font-size: 12px; }
.hint { color: #8a968f; font-size: 12px; line-height: 1.6; }
.cfg-form { max-width: 640px; }
.advanced { margin-bottom: 18px; border: none; }
.form-actions { margin-top: 4px; }
.last-error { margin-top: 10px; padding: 8px 12px; border-radius: 6px; background: #fef0f0; color: #c45656; font-size: 13px; display: flex; align-items: center; gap: 6px; }
.stats-row { display: flex; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
.stat { flex: 1; min-width: 120px; text-align: center; padding: 10px 8px; border-radius: 8px; background: #f5f8f6; }
.stat-num { font-size: 22px; font-weight: 600; color: #2f4f43; }
.stat-label { font-size: 12px; color: #8a968f; margin-top: 2px; }
.test-result .test-step { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 6px; }
.step-tag { flex-shrink: 0; width: 116px; text-align: center; }
.step-detail { font-size: 13px; word-break: break-all; }
.log-detail { font-size: 12px; word-break: break-all; }
@media (max-width: 900px) { .stats-row { flex-direction: column; } }
</style>
