<template>
  <div class="dash">
    <!-- 欢迎横幅：渐变底 + 数据刷新控制（联动统计的实时同步入口） -->
    <div class="hero-card">
      <div class="hero-left">
        <div class="hero-hi">{{ greeting }}，{{ auth.displayName || '朋友' }}</div>
        <div class="hero-date">{{ todayText }} · 多账号协议系统运行中</div>
      </div>
      <div class="hero-right">
        <div class="hero-fresh">
          <span class="pulse-dot" :class="{ paused: !autoRefresh && !sseLive }" />
          <span class="hero-fresh-text">{{ liveText }}</span>
          <span v-if="stats.generated_at" class="mono hero-gen">数据源：{{ stats.generated_at }}</span>
        </div>
        <div class="hero-actions">
          <el-switch v-model="autoRefresh" size="small" active-text="自动" inactive-text="暂停" />
          <el-button size="small" :icon="Refresh" :loading="refreshing" circle @click="refresh" />
        </div>
      </div>
    </div>

    <!-- 统计卡（账号 / 券 / 订单三域，点击联动跳转对应页面；订单/券使用卡带 7 日迷你趋势） -->
    <div class="stat-grid">
      <div class="stat-card g1" :class="{ 'card-link': auth.can('account:read') }" @click="go('account:read', '/accounts')">
        <div class="stat-value">{{ stats.total_accounts }}</div>
        <div class="stat-label">茶姬账号</div>
        <div class="stat-foot">在线 {{ stats.online }} · 待登/失效 {{ stats.pending + stats.expired }}</div>
        <el-icon class="stat-icon"><User /></el-icon>
      </div>
      <div class="stat-card g2" :class="{ 'card-link': auth.can('feature:coupon') }" @click="go('feature:coupon', '/ops/coupons')">
        <div class="stat-value">{{ stats.coupons_total }}</div>
        <div class="stat-label">券ID 档案（全量收集）</div>
        <div class="stat-foot">可用 {{ stats.coupons_effective }} · 历史 {{ stats.coupons_historical }} · 试算 {{ stats.coupons_settle_available }}</div>
        <el-icon class="stat-icon"><Ticket /></el-icon>
      </div>
      <div class="stat-card g3" :class="{ 'card-link': auth.can('feature:order') }" @click="go('feature:order', '/ops/coupon-logs')">
        <div class="stat-value">{{ stats.coupon_used_success }}</div>
        <div class="stat-label">券使用成功（次）</div>
        <svg class="spark" viewBox="0 0 100 26" preserveAspectRatio="none" aria-hidden="true">
          <polyline :points="sparkUsage" fill="none" stroke="rgba(255,255,255,0.85)" stroke-width="1.6" />
        </svg>
        <div class="stat-foot">累计抵扣 ¥{{ stats.coupon_deduction_total }} · 拒/败 {{ stats.coupon_rejected + stats.coupon_failed }}</div>
        <el-icon class="stat-icon"><CircleCheck /></el-icon>
      </div>
      <div class="stat-card g4" :class="{ 'card-link': auth.can('feature:pickup') }" @click="go('feature:pickup', '/ops/pickup?view=all')">
        <div class="stat-value">{{ stats.orders_total }}</div>
        <div class="stat-label">订单总数</div>
        <svg class="spark" viewBox="0 0 100 26" preserveAspectRatio="none" aria-hidden="true">
          <polyline :points="sparkOrders" fill="none" stroke="rgba(255,255,255,0.85)" stroke-width="1.6" />
        </svg>
        <div class="stat-foot">今日 {{ stats.orders_today }} · 待支付 {{ stats.orders_pending_pay }} · 制作中 {{ stats.orders_making }}</div>
        <el-icon class="stat-icon"><ShoppingCart /></el-icon>
      </div>
      <div class="stat-card g5" :class="{ 'card-link': auth.can('feature:pickup') }" @click="go('feature:pickup', '/ops/pickup?view=all&scenario=zero')">
        <div class="stat-value">{{ stats.orders_zero }}</div>
        <div class="stat-label">0 元单（券抵扣闭环）</div>
        <div class="stat-foot">差额支付单 {{ stats.orders_partial }}</div>
        <el-icon class="stat-icon"><Present /></el-icon>
      </div>
      <div class="stat-card g6" :class="{ 'card-link': auth.can('audit:read') }" @click="go('audit:read', '/system/audit')">
        <div class="stat-value">{{ stats.logins_7d }}</div>
        <div class="stat-label">近 7 日协议登录</div>
        <div class="stat-foot">系统用户 {{ stats.users }} 个</div>
        <el-icon class="stat-icon"><TrendCharts /></el-icon>
      </div>
      <!-- 盈利概览（决策系统，decision:manage 可见）：整行横带卡，profit-report 近 7 日 summary；
           SSE stats 帧携带同口径 profit 域时实时同步（REST 首拉 + SSE 增量双通道，同源） -->
      <div v-if="auth.can('decision:manage')" class="stat-card g7 profit-band"
        :class="{ 'card-link': auth.can('decision:manage') }" @click="go('decision:manage', '/decision/costs')">
        <div class="profit-cell main">
          <div class="stat-value">¥{{ profit.profit_total }}</div>
          <div class="stat-label">近 7 日总利润</div>
        </div>
        <div class="profit-cell">
          <div class="stat-value">{{ profit.margin_avg || '0' }}%</div>
          <div class="stat-label">平均利润率</div>
        </div>
        <div class="profit-cell">
          <div class="stat-value">{{ profit.blocked_count }}</div>
          <div class="stat-label">拦截（blocked）</div>
        </div>
        <div class="profit-cell">
          <div class="stat-value">{{ profit.orders }}</div>
          <div class="stat-label">决策成单数</div>
        </div>
        <div class="profit-cell">
          <div class="stat-value">¥{{ profit.cost_total }}</div>
          <div class="stat-label">总成本</div>
        </div>
        <div class="profit-note">决策流水近 7 日成单口径 · 点击进入「下单决策」</div>
        <el-icon class="stat-icon"><Coin /></el-icon>
      </div>
    </div>

    <!-- 图表区：券使用趋势（双轴）+ 订单趋势 -->
    <div class="dash-grid">
      <div class="page-card chart-card">
        <div class="chart-head">
          <h3 class="sec-title">券使用趋势 · 近 7 日</h3>
          <div v-if="sceneTags.length" class="scene-tags">
            <el-tag v-for="s in sceneTags" :key="s.scene" size="small" effect="plain" round>
              {{ s.scene }} {{ s.value }}
            </el-tag>
          </div>
        </div>
        <div ref="usageChartRef" class="chart usage" />
      </div>
      <div class="page-card chart-card">
        <div class="chart-head">
          <h3 class="sec-title">订单趋势 · 近 7 日</h3>
          <div class="scene-tags">
            <el-tag size="small" effect="plain" round>已完成 {{ stats.orders_done }}</el-tag>
            <el-tag size="small" type="warning" effect="plain" round>待支付 {{ stats.orders_pending_pay }}</el-tag>
          </div>
        </div>
        <div ref="ordersChartRef" class="chart" />
      </div>
    </div>

    <div class="dash-grid">
      <div class="page-card chart-card">
        <h3 class="sec-title">账号状态分布</h3>
        <div ref="chartRef" class="donut" />
      </div>

      <div class="page-card">
        <h3 class="sec-title">六功能协议进度</h3>
        <div class="feature-list">
          <div v-for="f in features" :key="f.name" class="feature-row" :class="{ done: f.type === 'success' }">
            <el-tag :type="f.type" size="small" effect="dark" class="feature-no">{{ f.name }}</el-tag>
            <span class="feature-desc">{{ f.desc }}</span>
            <el-icon v-if="f.type === 'success'" class="feature-ok"><CircleCheckFilled /></el-icon>
          </div>
        </div>
        <p class="feature-note">
          进度依据 <b>docs/protocol_six_features_20260923.md</b>：F1/F3/F4 已生产闭环；
          F5 下单 / F6 取餐已于 2026-09-26 集成开放。
        </p>
      </div>
    </div>

    <div class="page-card recent-card">
      <h3 class="sec-title">最近操作</h3>
      <el-table :data="stats.recent_audit" size="small" :show-header="false">
        <el-table-column width="150">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.action }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column>
          <template #default="{ row }">
            <div>{{ row.username }} · {{ row.target || '—' }}</div>
            <div class="mono muted">{{ fmtTime(row.created_at) }}</div>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { Refresh } from '@element-plus/icons-vue'
import { apiDashboard, apiDecision } from '../api'
import { fmtTime } from '../utils/format'
import { useAuthStore } from '../stores/auth'
import { openEventStream } from '../utils/sse'

const auth = useAuthStore()
const router = useRouter()
const REFRESH_MS = 15000   // 轮询兜底间隔（SSE 实时通道正常时停用）

const stats = reactive({
  total_accounts: 0, online: 0, pending: 0, expired: 0, disabled: 0,
  logins_7d: 0, users: 0,
  coupons_total: 0, coupons_effective: 0, coupons_historical: 0, coupons_settle_available: 0,
  coupons_used: 0, coupon_used_success: 0, coupon_rejected: 0, coupon_failed: 0,
  coupon_deduction_total: '0.00',
  orders_total: 0, orders_today: 0, orders_pending_pay: 0, orders_making: 0,
  orders_done: 0, orders_canceled: 0, orders_zero: 0, orders_partial: 0,
  status_distribution: [], coupon_scenes: [], coupon_usage_trend: [], orders_trend: [],
  recent_audit: [], generated_at: '',
})

const features = [
  { name: 'F1', desc: '设备注册 + 短信登录（单会话，生产闭环）', type: 'success' },
  { name: 'F2', desc: '门店自取路径参数族（saleType=1 贯穿）', type: 'success' },
  { name: 'F3', desc: '游客城市 / 门店 / 菜品 SKU 查询', type: 'success' },
  { name: 'F4', desc: '优惠券全量查询 + 多维模糊搜索', type: 'success' },
  { name: 'F5', desc: '下单试算 / 0 元闭环 / 差额支付', type: 'success' },
  { name: 'F6', desc: '取餐码 / 等待杯数 / 状态轮询', type: 'success' },
]

/* ---- 盈利概览（决策系统）：profit-report 近 7 日 summary，decision:manage 可见 ----
 * 首拉走 REST profitReport；其后 SSE stats 帧自带同口径 profit 域实时覆盖（双通道同源）。
 * 低频数据：仅挂载时拉一次，不随 15s 轮询刷新。 */
const profit = reactive({
  orders: 0, revenue_total: '0.00', cost_total: '0.00',
  profit_total: '0.00', margin_avg: '0', blocked_count: 0,
})

function applyProfitSummary(s) {
  profit.orders = s.orders || 0
  profit.revenue_total = s.revenue_total || '0.00'
  profit.cost_total = s.cost_total || '0.00'
  profit.profit_total = s.profit_total || '0.00'
  profit.margin_avg = s.margin_avg || '0'
  profit.blocked_count = s.blocked_count || 0
}

// YYYY-MM-DD HH:MM:SS（仓内响应风格的时间格式；整天覆盖：from 00:00:00 ~ to 23:59:59）
function fmtDayOffset(days, endOfDay = false) {
  const d = new Date(Date.now() + days * 86400000)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${endOfDay ? '23:59:59' : '00:00:00'}`
}

async function loadProfit() {
  if (!auth.can('decision:manage')) return
  try {
    const data = await apiDecision.profitReport({ from: fmtDayOffset(-6), to: fmtDayOffset(0, true) })
    applyProfitSummary(data?.summary || {})
  } catch {
    /* 决策报表端点未就绪（波2-C 未合并）/ 网络失败：保持零值展示，不打断仪表盘 */
  }
}

const autoRefresh = ref(true)
const refreshing = ref(false)
const sseLive = ref(false)   // SSE 实时推送在位（true 时轮询兜底停用）
let pollTimer = null
let liveStream = null

const liveText = computed(() => {
  if (sseLive.value) return 'SSE 实时推送中 · 数据一变即同步'
  return autoRefresh.value ? `轮询同步中 · 每 ${REFRESH_MS / 1000}s（SSE 断开回落）` : '实时联动已暂停'
})

// 统计卡联动跳转：点击卡片带筛选参数跳到对应页面（无权限的卡片不响应、不显示手型）
function go(perm, path) {
  if (auth.can(perm)) router.push(path)
}

// 迷你趋势线（统计卡内 7 点 SVG sparkline，数据复用已拉取的趋势序列，零额外请求）
function sparkPoints(values, w = 100, h = 26, pad = 3) {
  const nums = values.map((v) => Number(v) || 0)
  const max = Math.max(1, ...nums)
  const step = nums.length > 1 ? (w - pad * 2) / (nums.length - 1) : 0
  return nums.map((v, i) => {
    const x = pad + i * step
    const y = h - pad - (v / max) * (h - pad * 2)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}
const sparkOrders = computed(() => sparkPoints(stats.orders_trend.map((d) => d.count)))
const sparkUsage = computed(() => sparkPoints(stats.coupon_usage_trend.map((d) => d.success)))

const greeting = computed(() => {
  const h = new Date().getHours()
  if (h < 6) return '夜深了'
  if (h < 12) return '早上好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
})
const todayText = computed(() => {
  const d = new Date()
  const week = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()]
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日 · 星期${week}`
})
const sceneTags = computed(() => stats.coupon_scenes || [])

const chartRef = ref()
const usageChartRef = ref()
const ordersChartRef = ref()
let statusChart = null
let usageChart = null
let ordersChart = null

const INK = '#22302c'
const MUTED = '#7a8a84'
const TEA = '#2a6e59'

function renderStatusChart() {
  if (!chartRef.value) return
  statusChart = statusChart || echarts.init(chartRef.value)
  const dist = stats.status_distribution.filter((d) => d.value > 0)
  statusChart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}: {c}（{d}%）' },
    legend: { bottom: 0, icon: 'circle', itemWidth: 10, textStyle: { color: MUTED, fontSize: 12 } },
    color: ['#2a6e59', '#90a8a0', '#c45656', '#b8860b'],
    series: [{
      type: 'pie',
      radius: ['50%', '72%'],
      center: ['50%', '44%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 8, borderColor: '#fff', borderWidth: 3 },
      label: { show: false },
      emphasis: {
        label: { show: true, fontSize: 14, fontWeight: 600, color: INK },
        itemStyle: { shadowBlur: 14, shadowColor: 'rgba(20,51,42,0.25)' },
      },
      data: dist.length ? dist : [{ name: '暂无账号', value: 1, itemStyle: { color: '#e3ebe7' } }],
    }],
  })
}

function renderUsageChart() {
  if (!usageChartRef.value) return
  usageChart = usageChart || echarts.init(usageChartRef.value)
  const days = stats.coupon_usage_trend.map((d) => d.day)
  usageChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 0, right: 0, itemWidth: 12, textStyle: { color: MUTED, fontSize: 12 } },
    grid: { left: 42, right: 48, top: 34, bottom: 26 },
    xAxis: { type: 'category', data: days, axisLine: { lineStyle: { color: '#d8e3de' } }, axisLabel: { color: MUTED } },
    yAxis: [
      { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#eef3f0' } }, axisLabel: { color: MUTED } },
      { type: 'value', name: '元', nameTextStyle: { color: MUTED }, splitLine: { show: false }, axisLabel: { color: MUTED } },
    ],
    series: [
      {
        name: '使用成功', type: 'bar', stack: 'use', barWidth: 18, itemStyle: { borderRadius: [4, 4, 0, 0], color: TEA },
        emphasis: { itemStyle: { color: '#215e4c' } },
        data: stats.coupon_usage_trend.map((d) => d.success),
      },
      {
        name: '拒/败', type: 'bar', stack: 'use', barWidth: 18, itemStyle: { borderRadius: [4, 4, 0, 0], color: '#c9a227' },
        data: stats.coupon_usage_trend.map((d) => d.rejected),
      },
      {
        name: '抵扣金额', type: 'line', yAxisIndex: 1, smooth: true, symbolSize: 7,
        lineStyle: { width: 2.5, color: '#c88a2a' }, itemStyle: { color: '#c88a2a' },
        areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(200,138,42,0.22)' }, { offset: 1, color: 'rgba(200,138,42,0)' },
        ]) },
        data: stats.coupon_usage_trend.map((d) => Number(d.deduction) || 0),
      },
    ],
  })
}

function renderOrdersChart() {
  if (!ordersChartRef.value) return
  ordersChart = ordersChart || echarts.init(ordersChartRef.value)
  ordersChart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 42, right: 20, top: 30, bottom: 26 },
    xAxis: {
      type: 'category', boundaryGap: false,
      data: stats.orders_trend.map((d) => d.day),
      axisLine: { lineStyle: { color: '#d8e3de' } }, axisLabel: { color: MUTED },
    },
    yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#eef3f0' } }, axisLabel: { color: MUTED } },
    series: [{
      name: '下单数', type: 'line', smooth: true, symbolSize: 7,
      lineStyle: { width: 2.5, color: '#33658a' }, itemStyle: { color: '#33658a' },
      areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
        { offset: 0, color: 'rgba(51,101,138,0.20)' }, { offset: 1, color: 'rgba(51,101,138,0)' },
      ]) },
      data: stats.orders_trend.map((d) => d.count),
    }],
  })
}

function renderAll() {
  renderStatusChart()
  renderUsageChart()
  renderOrdersChart()
}

function applyStats(data) {
  Object.assign(stats, data.cards, {
    status_distribution: data.status_distribution,
    coupon_scenes: data.coupon_scenes,
    coupon_usage_trend: data.coupon_usage_trend,
    orders_trend: data.orders_trend,
    recent_audit: data.recent_audit,
    generated_at: data.generated_at,
  })
  if (data.profit && auth.can('decision:manage')) applyProfitSummary(data.profit)
  renderAll()
}

async function refresh() {
  refreshing.value = true
  try {
    applyStats(await apiDashboard.stats())
  } finally {
    refreshing.value = false
  }
}

// ---- 实时通道：SSE 主通道（服务端指纹变化即推）+ 轮询兜底（SSE 断开自动接管、恢复即停） ----
function startPollTimer() {
  if (pollTimer) return
  pollTimer = setInterval(() => {
    if (autoRefresh.value) refresh()
  }, REFRESH_MS)
}

function stopPollTimer() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function startLive() {
  liveStream = openEventStream({
    topics: ['dashboard'],
    events: {
      stats: (d) => { if (d?.cards) applyStats(d) },
    },
    onOpen: () => {
      sseLive.value = true
      stopPollTimer()
    },
    onError: () => {
      sseLive.value = false
      startPollTimer()
    },
  })
}

const onResize = () => {
  statusChart?.resize()
  usageChart?.resize()
  ordersChart?.resize()
}
const onFocus = () => {
  if (autoRefresh.value) refresh()   // 窗口切回立即同步（联动关键路径）
}

onMounted(async () => {
  await refresh()
  renderAll()
  loadProfit()         // 盈利概览：REST 首拉一次（其后靠 SSE stats.profit 实时覆盖）
  startLive()        // SSE 实时主通道（连上即推当前 stats）
  startPollTimer()   // 轮询兜底：SSE onOpen 后自动停用，断开自动接管
  window.addEventListener('resize', onResize)
  window.addEventListener('focus', onFocus)
})
onBeforeUnmount(() => {
  stopPollTimer()
  liveStream?.close()
  window.removeEventListener('resize', onResize)
  window.removeEventListener('focus', onFocus)
  statusChart?.dispose()
  usageChart?.dispose()
  ordersChart?.dispose()
})
</script>

<style scoped>
/* ---- 欢迎横幅 ---- */
.hero-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  flex-wrap: wrap;
  border-radius: 16px;
  padding: 22px 26px;
  color: #fff;
  background: linear-gradient(120deg, #14332a 0%, #215e4c 55%, #3d8b72 100%);
  box-shadow: 0 10px 26px rgba(20, 51, 42, 0.28);
  position: relative;
  overflow: hidden;
  margin-bottom: 18px;
}
.hero-card::after {
  content: '';
  position: absolute;
  right: -60px;
  top: -80px;
  width: 260px;
  height: 260px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(255, 255, 255, 0.14), transparent 65%);
}
.hero-hi {
  font-size: 21px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.hero-date {
  margin-top: 6px;
  font-size: 12.5px;
  opacity: 0.82;
}
.hero-right {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
  position: relative;
  z-index: 1;
}
.hero-fresh {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  opacity: 0.95;
}
.hero-fresh-text {
  white-space: nowrap;
}
.hero-gen {
  opacity: 0.72;
  white-space: nowrap;
}
.hero-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #7ce3b8;
  box-shadow: 0 0 0 rgba(124, 227, 184, 0.7);
  animation: pulse 2s infinite;
}
.pulse-dot.paused {
  background: #d9c68a;
  animation: none;
}
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 rgba(124, 227, 184, 0.6); }
  70% { box-shadow: 0 0 0 9px rgba(124, 227, 184, 0); }
  100% { box-shadow: 0 0 0 0 rgba(124, 227, 184, 0); }
}

/* ---- 统计卡 ---- */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 14px;
  margin-bottom: 18px;
}
.stat-card {
  border-radius: 14px;
  padding: 16px 18px;
  color: #fff;
  position: relative;
  overflow: hidden;
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.stat-card:hover {
  transform: translateY(-3px);
  box-shadow: 0 10px 24px rgba(20, 51, 42, 0.22);
}
.stat-card .stat-value {
  font-size: 28px;
  font-weight: 700;
  line-height: 1.15;
}
.stat-card .stat-label {
  font-size: 12.5px;
  opacity: 0.94;
  margin-top: 2px;
}
.stat-card .stat-foot {
  font-size: 11px;
  opacity: 0.75;
  margin-top: 8px;
  border-top: 1px solid rgba(255, 255, 255, 0.18);
  padding-top: 7px;
}
.stat-card .stat-icon {
  position: absolute;
  right: 12px;
  top: 12px;
  font-size: 36px;
  opacity: 0.2;
}
.stat-card.card-link {
  cursor: pointer;
}
.spark {
  display: block;
  width: 100%;
  height: 26px;
  margin-top: 6px;
  opacity: 0.92;
}
.g1 { background: linear-gradient(135deg, #2a6e59, #1b433c); }
.g2 { background: linear-gradient(135deg, #3d8b72, #2a6e59); }
.g3 { background: linear-gradient(135deg, #b8860b, #8a6508); }
.g4 { background: linear-gradient(135deg, #33658a, #234a66); }
.g5 { background: linear-gradient(135deg, #8a5a8f, #5f3a66); }
.g6 { background: linear-gradient(135deg, #4a7a5c, #35604a); }
.g7 { background: linear-gradient(135deg, #215e4c, #8a6508); }

/* ---- 盈利概览横带卡（整行占满，多个数字单元横排；对齐统计卡渐变/配色语言） ---- */
.profit-band {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 34px;
  flex-wrap: wrap;
  padding: 14px 22px;
}
.profit-cell .stat-value {
  font-size: 24px;
}
.profit-cell.main .stat-value {
  font-size: 30px;
}
.profit-cell.main .stat-label {
  font-size: 13px;
}
.profit-note {
  margin-left: auto;
  font-size: 11px;
  opacity: 0.75;
  white-space: nowrap;
}

/* ---- 图表 ---- */
.dash-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.chart-card {
  min-width: 0;
}
.chart-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.scene-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.chart {
  height: 250px;
}
.donut {
  height: 280px;
}
.sec-title {
  margin: 0 0 12px;
  font-size: 15px;
  color: var(--tea-800);
}
.chart-head .sec-title {
  margin-bottom: 0;
}
.chart-card .chart,
.chart-card .donut {
  margin-top: 12px;
}

/* ---- 功能进度 ---- */
.feature-list {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px 18px;
  margin-top: 14px;
}
.feature-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: 10px;
  background: #f7faf8;
  border: 1px solid #e8f0ec;
  transition: border-color 0.15s ease, background 0.15s ease;
}
.feature-row:hover {
  border-color: var(--tea-300);
  background: #f0f7f3;
}
.feature-no {
  width: 34px;
  justify-content: center;
  flex-shrink: 0;
}
.feature-desc {
  font-size: 13px;
  color: var(--ink);
  flex: 1;
}
.feature-ok {
  color: var(--tea-600);
  flex-shrink: 0;
}
.feature-note {
  margin: 16px 0 0;
  font-size: 12.5px;
  color: var(--muted);
  border-top: 1px dashed #e3ebe7;
  padding-top: 12px;
}

/* ---- 最近操作 ---- */
.recent-card {
  margin-top: 2px;
}
.muted {
  color: var(--muted);
}

@media (max-width: 1400px) {
  .stat-grid {
    grid-template-columns: repeat(3, 1fr);
  }
}
@media (max-width: 1100px) {
  .stat-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .dash-grid {
    grid-template-columns: 1fr;
  }
  .feature-list {
    grid-template-columns: 1fr;
  }
}
</style>
