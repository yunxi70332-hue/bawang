<template>
  <div>
    <!-- 账号选择 -->
    <div class="page-card">
      <h2 class="page-title">取餐查询 · 功能6</h2>
      <p class="page-subtitle">
        登录态调用 getOrderList / getOrderDetail / getOrderStatus / getWaitingInfo；
        待支付订单每 5 秒状态探针，制作中订单每 3 秒刷新取餐等待信息；
        全量模式遍历所有账号 token 批量收集取餐码，本地库多维模糊搜索（码值/订单号/饮品/门店/账号）
      </p>
      <div class="query-bar">
        <el-radio-group v-model="mode">
          <el-radio-button value="single">单账号</el-radio-button>
          <el-radio-button value="all">全量查询</el-radio-button>
        </el-radio-group>

        <!-- 单账号模式：账号选择（原有逻辑） -->
        <template v-if="mode === 'single'">
          <el-select v-model="accountId" filterable placeholder="选择茶姬账号（仅在线可选）" style="width: 340px">
            <el-option v-for="a in accounts" :key="a.id" :value="a.id" :disabled="a.status !== 'online'"
              :label="`${a.label}（${a.phone_masked}${a.status === 'online' ? ' · 在线' : ' · ' + a.status_label}）`">
              <span>{{ a.label }}</span>
              <span class="opt-sub">{{ a.phone_masked }} · {{ a.status_label }}{{ a.status === 'online' ? '' : '（不可选）' }}</span>
            </el-option>
          </el-select>
          <el-button v-if="accountId" :icon="Refresh" circle :loading="listLoading" @click="reload" />
          <span v-if="accountId && lastLoadedAt" class="run-at mono">更新于 {{ lastLoadedAt }}</span>
        </template>

        <!-- 全量模式：多维模糊搜索 + 筛选 + 全量扫描 -->
        <template v-else>
          <el-input v-model="allQuery.keyword" placeholder="模糊搜索：取餐码 / 订单号 / 饮品 / 门店 / 账号（回车执行）"
            clearable :prefix-icon="Search" style="width: 320px" @keyup.enter="onAllSearch" @clear="onAllSearch" />
          <el-select v-model="allQuery.status" placeholder="全部状态" clearable style="width: 118px" @change="onAllSearch">
            <el-option v-for="(v, k) in ORDER_STATUS" :key="k" :value="Number(k)" :label="v.label" />
          </el-select>
          <el-select v-model="allQuery.account_id" placeholder="全部账号" clearable filterable style="width: 168px" @change="onAllSearch">
            <el-option v-for="a in accounts" :key="a.id" :value="a.id" :label="`${a.label}（${a.phone_masked}）`" />
          </el-select>
          <el-select v-model="allQuery.scenario" placeholder="全部场景" clearable style="width: 108px" @change="onAllSearch">
            <el-option value="zero" label="0元单" />
            <el-option value="partial" label="差额单" />
          </el-select>
          <label class="pickup-only-switch">仅看有码 <el-switch v-model="allQuery.has_pickup" size="small" @change="onAllSearch" /></label>
          <el-button type="primary" :loading="scanning" :icon="Search" @click="startScan">
            {{ scanning ? `扫描中 ${scanProgress.done}/${scanProgress.total}` : '全量扫描' }}
          </el-button>
          <el-button :icon="Refresh" circle :loading="allLoading" @click="onAllSearch" />
        </template>
      </div>

      <!-- 全量模式：扫描进度与逐账号实时结果（SSE 推流） -->
      <div v-if="mode === 'all'" class="scan-panel">
        <el-progress v-if="scanning && scanProgress.total" :stroke-width="10"
          :percentage="Math.round((scanProgress.done / scanProgress.total) * 100)">
          <span class="mono small">{{ scanProgress.done }}/{{ scanProgress.total }}</span>
        </el-progress>
        <div v-if="lastScan" class="scan-meta small">
          上次扫描 {{ lastScan.summary.run_at }} · 成功 {{ lastScan.summary.ok }} / 失效 {{ lastScan.summary.expired }}
          / 失败 {{ lastScan.summary.failed }} · 订单 {{ lastScan.summary.orders }} · 取餐码 {{ lastScan.summary.distinct_pickup_codes }} 个
        </div>
        <div v-if="scanFeed.length" class="scan-feed">
          <div v-for="(r, i) in scanFeed" :key="i" class="scan-feed-row small" :class="'feed-' + r.result">
            <span class="mono">{{ r.index }}/{{ r.total }}</span>
            <span class="feed-account">{{ r.label }}（{{ r.phone_masked }}）</span>
            <el-tag size="small" :type="r.result === 'ok' ? 'success' : r.result === 'expired' ? 'danger' : 'warning'"
              effect="plain">{{ r.result === 'ok' ? `订单 ${r.orders} · 取餐码 ${r.pickups}` : r.error || r.result }}</el-tag>
          </div>
        </div>
      </div>
    </div>

    <!-- 订单列表（单账号模式） -->
    <div class="page-card" v-if="mode === 'single' && accountId">
      <div class="list-toolbar">
        <el-radio-group v-model="tab" @change="onTabChange">
          <el-radio-button value="today">今日订单</el-radio-button>
          <el-radio-button value="history">历史订单</el-radio-button>
        </el-radio-group>
        <div class="list-toolbar-right">
          <el-tag v-if="pendingOrders.length" type="warning" size="small" effect="plain">
            待支付 {{ pendingOrders.length }} 单 · 5s 状态探针
          </el-tag>
        </div>
      </div>

      <el-table v-loading="listLoading" :data="orders" stripe class="order-table"
        :row-class-name="rowClassName" @row-click="openDetail">
        <el-table-column label="取餐码" width="120" align="center">
          <template #default="{ row }">
            <span v-if="row.pickup_no" class="pickup-pill">{{ row.pickup_no }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="订单号" min-width="180">
          <template #default="{ row }">
            <span class="mono" :title="String(row.order_no)">{{ row.order_no }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="92" align="center">
          <template #default="{ row }">
            <el-tag :type="orderStatusTag(row.order_status).type" size="small" effect="dark">
              {{ row.status_label || orderStatusTag(row.order_status).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="应付" width="92" align="right">
          <template #default="{ row }">
            <span v-if="row.pay_amount !== null && row.pay_amount !== undefined && row.pay_amount !== ''"
              class="pay-amount">¥ {{ row.pay_amount }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="门店" min-width="190">
          <template #default="{ row }">
            <div>{{ row.store_name || '—' }}</div>
            <div v-if="row.store_no" class="mono muted">{{ row.store_no }}</div>
          </template>
        </el-table-column>
        <el-table-column label="下单时间" width="158">
          <template #default="{ row }">{{ fmtTime(row.order_time) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="158" align="center">
          <template #default="{ row }">
            <template v-if="Number(row.order_status) === 1">
              <el-button size="small" type="primary" plain @click.stop="rowPay(row)">支付</el-button>
              <el-button size="small" plain @click.stop="rowContinuePay(row)">续付</el-button>
            </template>
            <span v-else class="muted small">点击行查看</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="该账号暂无订单" />
        </template>
      </el-table>

      <div class="pager">
        <el-pagination background layout="total, prev, pager, next, sizes" :total="total"
          v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[10, 20, 50]"
          @current-change="onPageChange" @size-change="onSizeChange" />
      </div>
    </div>

    <!-- 全量模式：本地库搜索结果（所有账号的订单/取餐码） -->
    <div class="page-card" v-else-if="mode === 'all'">
      <div class="list-toolbar">
        <div class="all-stats">
          <span class="muted small">命中 {{ allTotal }} 条</span>
          <el-tag v-for="(v, k) in allStats.by_status" :key="k" size="small" effect="plain" round
            :type="orderStatusTag(Number(k)).type" class="stat-chip">
            {{ orderStatusTag(Number(k)).label }} {{ v }}
          </el-tag>
          <el-tag size="small" effect="plain" round type="success" class="stat-chip">有取餐码 {{ allStats.with_pickup }}</el-tag>
        </div>
        <span class="muted small">数据来自全量扫描落库的本地订单库 · 点击「全量扫描」收集最新取餐码</span>
      </div>

      <el-table v-loading="allLoading" :data="allOrders" stripe class="order-table">
        <el-table-column label="取餐码" width="110" align="center">
          <template #default="{ row }">
            <span v-if="row.pickup_no" class="pickup-pill">{{ row.pickup_no }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="账号" min-width="150">
          <template #default="{ row }">
            <div v-if="row.account">{{ row.account.label || row.account.nickname }}</div>
            <div v-if="row.account" class="mono muted small">{{ row.account.phone_masked }}</div>
          </template>
        </el-table-column>
        <el-table-column label="订单号" min-width="180">
          <template #default="{ row }">
            <span class="mono" :title="String(row.order_no)">{{ row.order_no }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="92" align="center">
          <template #default="{ row }">
            <el-tag :type="orderStatusTag(row.order_status).type" size="small" effect="dark">
              {{ row.status_label || orderStatusTag(row.order_status).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="饮品" min-width="170">
          <template #default="{ row }">
            <el-tooltip v-if="row.goods_desc" :content="row.goods_desc" placement="top">
              <span class="goods-desc">{{ row.goods_desc }}</span>
            </el-tooltip>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="门店" min-width="160">
          <template #default="{ row }">
            <div>{{ row.store_name || '—' }}</div>
            <div v-if="row.biz_type" class="muted small">{{ row.biz_type }}</div>
          </template>
        </el-table-column>
        <el-table-column label="实付" width="88" align="right">
          <template #default="{ row }">
            <span v-if="row.pay_amount !== ''" class="pay-amount">¥ {{ row.pay_amount }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="下单时间" width="158">
          <template #default="{ row }">{{ row.order_time || fmtTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="92" align="center">
          <template #default="{ row }">
            <el-button size="small" type="primary" plain @click.stop="openAllDetail(row)">详情</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="allQuery.keyword || allQuery.status ? '本地库无命中，可调整条件或先执行全量扫描' : '本地订单库为空，点击上方「全量扫描」收集所有账号的取餐码'" />
        </template>
      </el-table>

      <div class="pager">
        <el-pagination background layout="total, prev, pager, next, sizes" :total="allTotal"
          v-model:current-page="allQuery.page" v-model:page-size="allQuery.page_size" :page-sizes="[10, 20, 50]"
          @current-change="loadAll" @size-change="onAllSizeChange" />
      </div>
    </div>

    <!-- 未选账号引导（单账号模式） -->
    <div class="page-card" v-else>
      <el-empty description="请先在上方选择一个在线茶姬账号，再查询订单" />
      <el-alert v-if="accountsLoaded && !hasOnlineAccount" type="warning" :closable="false" class="no-online"
        title="暂无在线账号：请先到「账号管理」完成协议登录后再使用取餐查询" />
    </div>

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawerOpen" title="订单详情" size="640px" @close="onDrawerClose">
      <div v-loading="detailLoading" class="drawer-body">
        <template v-if="detail">
          <!-- 头部：状态 + 取餐码 -->
          <div class="detail-head">
            <div class="detail-head-row">
              <el-tag :type="orderStatusTag(detail.status).type" effect="dark">
                {{ detail.status_label || orderStatusTag(detail.status).label }}
              </el-tag>
              <span class="mono muted">{{ detail.order_no }}</span>
            </div>
            <div class="pickup-hero">
              <span class="pickup-hero-label">取餐码</span>
              <span class="pickup-hero-no">{{ detail.pickup_no || '—' }}</span>
            </div>
            <p v-if="Number(detail.status) === 1 && !detail.pickup_no" class="muted small pickup-hint">
              取餐码将在支付完成后生成
            </p>
          </div>

          <el-descriptions :column="2" border size="small" class="detail-desc">
            <el-descriptions-item label="门店" :span="2">
              {{ detail.store_name || '—' }} <span v-if="detail.store_no" class="mono muted">{{ detail.store_no }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="下单时间">{{ fmtTime(detail.order_time) }}</el-descriptions-item>
            <el-descriptions-item label="支付时间">{{ fmtTime(detail.pay_time) }}</el-descriptions-item>
            <el-descriptions-item label="支付方式">{{ detail.pay_type_text || '—' }}</el-descriptions-item>
            <el-descriptions-item label="总额">
              <span v-if="hasAmt(detail.total_amount)">¥ {{ detail.total_amount }}</span>
              <span v-else class="muted">—</span>
            </el-descriptions-item>
            <el-descriptions-item label="应付" :span="1">
              <span v-if="hasAmt(detail.pay_amount)" class="pay-amount">¥ {{ detail.pay_amount }}</span>
              <span v-else class="muted">—</span>
            </el-descriptions-item>
          </el-descriptions>

          <!-- 待支付：倒计时 + 续付 + 支付模式 -->
          <div v-if="Number(detail.status) === 1" class="panel pay-panel">
            <div class="panel-title">
              待支付
              <span class="countdown mono" :class="{ urgent: payRemainSec !== null && payRemainSec <= 120 }">
                支付窗口剩余 {{ payRemainDisplay }}
              </span>
            </div>
            <div class="pay-actions">
              <el-button size="small" type="primary" plain :loading="payBusy" @click="doContinuePay">
                续付（重铸支付串）
              </el-button>
              <el-radio-group v-model="payMode" size="small">
                <el-radio-button value="manual">人工支付</el-radio-button>
                <el-radio-button value="auto">自动支付 · 实验性</el-radio-button>
              </el-radio-group>
              <el-button v-if="payMode === 'manual'" size="small" type="primary" :loading="payBusy" @click="doManualPay">
                获取支付串与指引
              </el-button>
              <el-button v-else size="small" type="warning" :loading="autoBusy" @click="doAutoPay">
                发起自动支付
              </el-button>
            </div>
            <p class="note">支付宝侧扣款不在纯协议范围：人工模式请用手机完成支付；自动模式为实验性（服务端未配置接缝时返回 501）。</p>
          </div>

          <!-- 支付串面板 -->
          <div v-if="payLink" class="panel paylink-panel">
            <div class="panel-title">
              支付串
              <el-tag v-if="payLink.expire_at" size="small" effect="plain" type="info">有效至 {{ payLink.expire_at }}</el-tag>
            </div>
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="pay_no"><span class="mono">{{ payLink.pay_no || '—' }}</span></el-descriptions-item>
              <el-descriptions-item label="out_trade_no"><span class="mono">{{ payLink.out_trade_no || '—' }}</span></el-descriptions-item>
              <el-descriptions-item label="金额">¥ {{ payLink.total_amount ?? '—' }}</el-descriptions-item>
              <el-descriptions-item label="窗口">{{ payLink.pay_window_seconds ? payLink.pay_window_seconds + ' 秒' : '—' }}</el-descriptions-item>
              <el-descriptions-item label="h5_url" :span="2">
                <a v-if="payLink.h5_url" :href="payLink.h5_url" target="_blank" rel="noopener" class="mono link break">{{ payLink.h5_url }}</a>
                <span v-else class="muted">—</span>
              </el-descriptions-item>
              <!-- 官方收银台：后端异步铸造（云手机约 10-30 秒），就绪前展示铸造中/失败提示 -->
              <el-descriptions-item label="官方收银台" :span="2">
                <a v-if="cashierUrl" :href="cashierUrl" target="_blank" rel="noopener" class="mono link break">点击打开支付宝收银台付款</a>
                <span v-else-if="cashierState === 'minting'" class="muted small">铸造中…约10-30秒，完成后自动出现；可点续付重试</span>
                <span v-else-if="cashierState === 'failed'" class="cashier-warn small">铸造未成功，点「续付」重试</span>
                <span v-else class="muted">—</span>
              </el-descriptions-item>
            </el-descriptions>
            <div class="order-str">
              <div class="order-str-head">
                <span>支付串 order_str（完整复制使用）</span>
                <el-button size="small" text type="primary" @click="copyPayStr">复制</el-button>
              </div>
              <el-input :model-value="payLink.order_str || ''" type="textarea" :rows="5" readonly resize="none" class="mono" />
            </div>
            <p class="note">
              {{ payLink.note || '人工指引：复制支付串或打开 h5 链接，在手机支付宝完成扣款；支付完成后列表状态探针（5 秒）会自动刷新。' }}
            </p>
          </div>

          <!-- 制作中：等待信息卡 -->
          <div v-if="Number(detail.status) === 3" class="panel waiting-panel">
            <div class="panel-title">
              制作中 · 取餐等待
              <span class="waiting-ops">
                <span class="muted small">每 3 秒自动刷新</span>
                <el-switch v-model="waitingAuto" size="small" />
                <el-button size="small" text type="primary" :loading="waitingLoading" @click="fetchWaiting()">刷新</el-button>
              </span>
            </div>
            <div v-if="waitingInfo" class="waiting-grid">
              <div class="waiting-cell">
                <div class="waiting-value">{{ waitingInfo.waiting_cups ?? '—' }}</div>
                <div class="waiting-label">前方等待（杯）</div>
              </div>
              <div class="waiting-cell">
                <div class="waiting-value">{{ fmtWaiting(waitingInfo.waiting_time) }}</div>
                <div class="waiting-label">预计等待</div>
              </div>
              <div class="waiting-cell">
                <div class="waiting-value">{{ waitingInfo.queue_limit ?? '—' }}</div>
                <div class="waiting-label">队列上限（杯）</div>
              </div>
            </div>
            <el-alert v-else-if="waitingError" type="info" :closable="false" :title="waitingError" />
            <el-skeleton v-else :rows="2" animated />
          </div>

          <!-- 商品列表 -->
          <h3 class="sec-title">商品（{{ (detail.items || []).length }}）</h3>
          <el-table :data="detail.items || []" size="small" border>
            <el-table-column label="名称" min-width="210">
              <template #default="{ row }">
                <div>{{ itemName(row) }}</div>
                <div v-if="itemSpec(row)" class="muted small">{{ itemSpec(row) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="数量" width="70" align="center">
              <template #default="{ row }">× {{ itemQty(row) }}</template>
            </el-table-column>
            <el-table-column label="金额" width="96" align="right">
              <template #default="{ row }">
                <span v-if="itemAmount(row) !== null">¥ {{ itemAmount(row) }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>

          <!-- 券核销 -->
          <h3 class="sec-title">券核销（{{ promotions.length }}）</h3>
          <el-table v-if="promotions.length" :data="promotions" size="small" border>
            <el-table-column label="券名称" min-width="190">
              <template #default="{ row }">{{ promoName(row) }}</template>
            </el-table-column>
            <el-table-column label="优惠" width="92" align="right">
              <template #default="{ row }">- ¥ {{ promoAmount(row) ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="券码" min-width="200">
              <template #default="{ row }">
                <span v-if="promoId(row)" class="mono muted">{{ promoId(row) }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>
          <p v-else class="muted no-coupon">未使用优惠券（无券核销记录）</p>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { apiAccounts, apiOps } from '../api'
import { fmtTime, fmtWaiting, fmtCountdown, orderStatusTag, ORDER_STATUS } from '../utils/format'
import { nowMs } from '../utils/clock'
import { openEventStream } from '../utils/sse'

// ---------- 账号 ----------
const accounts = ref([])
const accountsLoaded = ref(false)
const accountId = ref(null)
const hasOnlineAccount = computed(() => accounts.value.some((a) => a.status === 'online'))

// ---------- 模式与全量查询 ----------
const route = useRoute()
const mode = ref('single')   // single 单账号实时查询 | all 全量查询（本地订单库多维模糊搜索）
const allQuery = reactive({
  keyword: '', status: null, account_id: null, scenario: null, has_pickup: false, page: 1, page_size: 20,
})
const allOrders = ref([])
const allTotal = ref(0)
const allLoading = ref(false)
const allStats = ref({ by_status: {}, with_pickup: 0 })

// 全量扫描（后台线程执行，进度经 SSE /api/events?topics=pickup_scan 实时推流）
const scanning = ref(false)
const scanProgress = reactive({ done: 0, total: 0 })
const scanFeed = ref([])      // 逐账号实时结果（新→旧，上限 50 条）
const lastScan = ref(null)    // 上次扫描摘要（scan-status）
let scanStream = null

// ---------- 列表 ----------
const tab = ref('today')
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)
const orders = ref([])
const listLoading = ref(false)
const lastLoadedAt = ref('')

const pendingOrders = computed(() => orders.value.filter((o) => Number(o.order_status) === 1))

// ---------- 详情抽屉 ----------
const drawerOpen = ref(false)
const detailLoading = ref(false)
const detail = ref(null)

// 支付（待支付单）
const payMode = ref('manual')
const payBusy = ref(false)
const autoBusy = ref(false)
const payLink = ref(null)
// 秒表取校准后的服务器时钟（utils/clock，axios 拦截器随 server_time 持续校准），
// 驱动 payRemainSec 对 payment_expiry_ts（服务端毫秒时间戳）的倒数，消除客户端时钟漂移
const nowTs = ref(nowMs())

// 官方收银台（后端在下单差额/pay manual/续付后异步铸造，约 10-30 秒完成）
const cashierUrl = ref('')
const cashierState = ref('idle') // idle=未轮询 | minting=铸造中 | failed=铸造超时

// 等待信息（制作中）
const waitingInfo = ref(null)
const waitingError = ref('')
const waitingLoading = ref(false)
const waitingAuto = ref(true)

// ---------- 定时器句柄（全部在 onUnmounted 清理） ----------
let probeTimer = null // 列表待支付单状态探针（5s）
let waitingTimer = null // 等待信息轮询（3s）
let tickTimer = null // 抽屉倒计时秒表（1s）
let autoPayTimer = null // 自动支付后状态轮询（5s）
let cashierTimer = null // 官方收银台链接铸造轮询（3s）
let probeBusy = false
let probeErrors = 0
let waitingErrors = 0
let autoPollBusy = false
let cashierBusy = false
let cashierPollCount = 0 // 收银台轮询已尝试次数（空结果/失败均计入，上限 20）
let cashierStartTs = 0 // 收银台轮询起点，用于 40s 铸造超时判定

// ---------- 初始化 ----------
onMounted(async () => {
  const data = await apiAccounts.list({ page: 1, page_size: 100 })
  accounts.value = data.items
  accountsLoaded.value = true
  const online = data.items.find((a) => a.status === 'online')
  if (online) accountId.value = online.id // 仅自动选中在线账号
  applyRouteQuery()
  if (mode.value === 'all') enterAllMode()
})

onUnmounted(() => {
  for (const t of [probeTimer, waitingTimer, tickTimer, autoPayTimer, cashierTimer]) clearInterval(t)
  probeTimer = waitingTimer = tickTimer = autoPayTimer = cashierTimer = null
  closeScanStream()
})

// 仪表盘统计卡联动入口：/ops/pickup?view=all&status=3&scenario=zero&keyword=…
function applyRouteQuery() {
  const q = route.query
  if (q.view === 'all') mode.value = 'all'
  if (q.status) allQuery.status = Number(q.status)
  if (q.scenario) allQuery.scenario = String(q.scenario)
  if (q.keyword) allQuery.keyword = String(q.keyword)
}

watch(mode, (m) => {
  if (m === 'all') {
    enterAllMode()
  } else {
    closeScanStream()
    if (accountId.value && !orders.value.length) load()
  }
})

function enterAllMode() {
  openScanStream()
  refreshScanStatus()
  if (!allOrders.value.length) loadAll()
}

async function refreshScanStatus() {
  try {
    const s = await apiOps.pickupScanStatus()
    scanning.value = !!s.running
    scanProgress.done = s.progress?.done || 0
    scanProgress.total = s.progress?.total || 0
    lastScan.value = s.last || null
  } catch { /* 静默：状态展示非关键路径 */ }
}

function openScanStream() {
  if (scanStream) return
  scanStream = openEventStream({
    topics: ['pickup_scan'],
    events: {
      account_done: (d) => {
        scanProgress.done = d.index
        scanProgress.total = d.total
        scanFeed.value.unshift(d)
        if (scanFeed.value.length > 50) scanFeed.value.length = 50
      },
      scan_done: (d) => {
        scanning.value = false
        ElMessage.success(`全量扫描完成：${d.ok}/${d.scanned} 账号成功，收集取餐码 ${d.distinct_pickup_codes} 个`)
        refreshScanStatus()
        loadAll()
      },
    },
  })
}

function closeScanStream() {
  if (scanStream) {
    scanStream.close()
    scanStream = null
  }
}

async function startScan() {
  try {
    const res = await apiOps.pickupScanAll()
    scanning.value = true
    scanProgress.done = 0
    scanProgress.total = res.accounts
    scanFeed.value = []
    openScanStream()   // 确保订阅在位（进入全量模式时通常已连上，此处幂等）
  } catch {
    refreshScanStatus()   // 409 扫描进行中 / 400 无可查账号：全局拦截器已提示
  }
}

async function loadAll() {
  allLoading.value = true
  try {
    const data = await apiOps.pickupSearch({
      keyword: allQuery.keyword || '',
      status: allQuery.status || 0,
      account_id: allQuery.account_id || 0,
      scenario: allQuery.scenario || '',
      has_pickup: allQuery.has_pickup,
      page: allQuery.page,
      page_size: allQuery.page_size,
    })
    allOrders.value = data.items || []
    allTotal.value = data.total || 0
    allStats.value = data.stats || { by_status: {}, with_pickup: 0 }
  } catch {
    allOrders.value = []
    allTotal.value = 0
  } finally {
    allLoading.value = false
  }
}

function onAllSearch() {
  allQuery.page = 1
  loadAll()
}

function onAllSizeChange() {
  allQuery.page = 1
  loadAll()
}

// 全量行详情：抽屉复用单账号详情链（accountId 临时指向该行账号；
// watch(accountId) 在全量模式下不重置抽屉、load 亦跳过，见两处 mode 守卫）
function openAllDetail(row) {
  if (row.account?.id) accountId.value = row.account.id
  openDetail({ order_no: row.order_no })
}

// 切换账号：清空订单数据、关抽屉，重新拉取（仅单账号模式；全量模式切账号=行详情临时指向）
watch(accountId, (id, old) => {
  if (old && mode.value === 'single') {
    drawerOpen.value = false // 触发 watch(drawerOpen) 停止全部抽屉轮询
    orders.value = []
    total.value = 0
    page.value = 1
    lastLoadedAt.value = ''
  }
  if (id && mode.value === 'single') load()
})

// ---------- 列表 ----------
async function load() {
  if (mode.value !== 'single' || !accountId.value) return
  listLoading.value = true
  try {
    const data = await apiOps.orderList(accountId.value, {
      tab: tab.value, page: page.value, page_size: pageSize.value,
    })
    orders.value = data.items || []
    total.value = data.total || 0
    lastLoadedAt.value = fmtTime(Date.now())
  } catch {
    orders.value = []
    total.value = 0
  } finally {
    listLoading.value = false
    syncProbe()
  }
}

function reload() {
  page.value = 1
  load()
}

function onTabChange() {
  page.value = 1
  load()
}

function onPageChange() {
  load()
}

function onSizeChange() {
  page.value = 1
  load()
}

function rowClassName({ row }) {
  return Number(row.order_status) === 1 ? 'row-pending' : ''
}

// ---------- 状态探针（列表存在待支付单时，每 5s 逐个轻探；仅页面可见时工作） ----------
function syncProbe() {
  const need = pendingOrders.value.length > 0 && !!accountId.value && mode.value === 'single'
  if (need && !probeTimer) {
    probeTimer = setInterval(probeTick, 5000)
  } else if (!need && probeTimer) {
    clearInterval(probeTimer)
    probeTimer = null
  }
}

async function probeTick() {
  if (probeBusy || document.hidden || !accountId.value || mode.value !== 'single') return
  probeBusy = true
  try {
    for (const o of pendingOrders.value) {
      const res = await apiOps.orderStatus(accountId.value, o.order_no)
      probeErrors = 0
      const st = Number(res?.status)
      if (st && st !== 1) {
        const label = res.status_label || orderStatusTag(st).label
        ElMessage.info(`订单 ${o.order_no} 状态已变更：${label}`)
        await refreshAfterStatusChange(o.order_no)
        break // 列表已重拉，pendingOrders 换新，下轮继续
      }
    }
  } catch {
    probeErrors += 1
    if (probeErrors >= 3 && probeTimer) {
      clearInterval(probeTimer)
      probeTimer = null
      ElMessage.warning('状态探针连续失败已暂停，请手动刷新列表')
    }
  } finally {
    probeBusy = false
  }
}

// 某单状态离开 1：静默重拉列表；若抽屉正展示该单则同步刷新详情
async function refreshAfterStatusChange(orderNo) {
  try {
    if (mode.value === 'all') {
      await loadAll()   // 抽屉从全量行打开时，刷新全量列表而非单账号页
    } else {
      const data = await apiOps.orderList(accountId.value, {
        tab: tab.value, page: page.value, page_size: pageSize.value,
      })
      orders.value = data.items || []
      total.value = data.total || 0
    }
  } finally {
    syncProbe()
  }
  if (drawerOpen.value && detail.value?.order_no === orderNo) {
    await openDetail({ order_no: orderNo })
  }
}

// ---------- 详情抽屉 ----------
async function openDetail(row) {
  if (!row?.order_no) return
  drawerOpen.value = true
  detailLoading.value = true
  detail.value = null
  payLink.value = null
  // 换单/刷新上下文：丢弃上一单的收银台轮询与链接，避免串单展示
  stopCashierPoll()
  cashierUrl.value = ''
  cashierState.value = 'idle'
  waitingInfo.value = null
  waitingError.value = ''
  waitingErrors = 0
  try {
    const d = await apiOps.orderDetail(accountId.value, row.order_no)
    detail.value = d
    if (Number(d.status) === 3) fetchWaiting()
  } finally {
    detailLoading.value = false
  }
}

// 抽屉开：启动秒表（驱动倒计时）；抽屉关：停止所有抽屉内轮询并丢弃过期支付串
watch(drawerOpen, (open) => {
  if (open) {
    if (!tickTimer) tickTimer = setInterval(() => { nowTs.value = nowMs() }, 1000)
  } else {
    stopTick()
    stopAutoPayPoll()
    stopCashierPoll() // 收银台铸造轮询随抽屉一起停止，避免后台空转
    payLink.value = null
    syncWaitingTimer()
  }
})

function onDrawerClose() {
  drawerOpen.value = false // 统一走 watch 清理（等待轮询在 watch(drawerOpen/status/waitingAuto) 停止）
}

function stopTick() {
  if (tickTimer) {
    clearInterval(tickTimer)
    tickTimer = null
  }
}

// 倒计时（payment_expiry_ts 为毫秒时间戳，契约 §2）
const payRemainSec = computed(() => {
  const d = detail.value
  const ts = Number(d?.payment_expiry_ts)
  if (!d || Number(d.status) !== 1 || !ts) return null
  return Math.floor((ts - nowTs.value) / 1000)
})

const payRemainDisplay = computed(() => {
  const s = payRemainSec.value
  if (s === null) return '—'
  if (s <= 0) return '已超时（待自动取消）'
  return fmtCountdown(s)
})

// ---------- 等待信息（制作中，3s 自动刷新，可暂停；仅抽屉打开且状态为 3 时轮询） ----------
watch([drawerOpen, () => detail.value?.status, waitingAuto], syncWaitingTimer)

function syncWaitingTimer() {
  const need = drawerOpen.value && Number(detail.value?.status) === 3 && waitingAuto.value
  if (need && !waitingTimer) {
    waitingTimer = setInterval(() => fetchWaiting(true), 3000)
  } else if (!need && waitingTimer) {
    clearInterval(waitingTimer)
    waitingTimer = null
  }
}

async function fetchWaiting(silent = false) {
  if (!accountId.value || !detail.value?.order_no) return
  if (silent && (document.hidden || waitingLoading.value)) return // 页面不可见或上一跳未完成时跳过本轮
  if (!silent) waitingLoading.value = true
  try {
    const info = await apiOps.orderWaiting(accountId.value, detail.value.order_no)
    waitingInfo.value = info
    waitingError.value = ''
    waitingErrors = 0
  } catch {
    if (silent) {
      waitingErrors += 1
      if (waitingErrors >= 3) {
        waitingAuto.value = false // 触发 syncWaitingTimer 停止轮询
        waitingError.value = '等待信息连续获取失败，已暂停自动刷新；可手动刷新重试'
      }
    } else {
      waitingError.value = '等待信息获取失败，请稍后重试'
    }
  } finally {
    waitingLoading.value = false
  }
}

// ---------- 支付（待支付单） ----------
async function doContinuePay() {
  if (!detail.value?.order_no) return
  payBusy.value = true
  try {
    const res = await apiOps.orderContinuePay(accountId.value, detail.value.order_no)
    payLink.value = res
    startCashierPoll() // 续付也会触发后端异步重铸官方收银台链接
    ElMessage.success('已重铸支付串（10 分钟支付窗口）')
  } finally {
    payBusy.value = false
  }
}

async function doManualPay() {
  if (!detail.value?.order_no) return
  payBusy.value = true
  try {
    // manual：返回该单最新 PayLink（continue_pay 重铸）+ 人工支付指引文案
    const res = await apiOps.orderPay(accountId.value, detail.value.order_no, 'manual')
    payLink.value = res
    startCashierPoll() // pay manual 触发后端异步铸造官方收银台链接，就绪后面板自动出现入口
  } finally {
    payBusy.value = false
  }
}

// ---------- 官方收银台铸造轮询（3s；后端云手机铸造约 10-30 秒，链接写入 PaySession 后即可取） ----------
function startCashierPoll() {
  stopCashierPoll()
  cashierUrl.value = ''
  cashierState.value = 'minting'
  cashierPollCount = 0
  cashierStartTs = Date.now()
  cashierTimer = setInterval(cashierTick, 3000)
}

function stopCashierPoll() {
  if (cashierTimer) {
    clearInterval(cashierTimer)
    cashierTimer = null
  }
}

async function cashierTick() {
  if (cashierBusy) return
  // 抽屉已关 / 订单已离开待支付 / 换单中：停止轮询（其余停止点见 watch(drawerOpen) 与 onUnmounted）
  if (!drawerOpen.value || Number(detail.value?.status) !== 1 || !detail.value?.order_no || !accountId.value) {
    stopCashierPoll()
    return
  }
  cashierBusy = true
  let gotUrl = ''
  try {
    const res = await apiOps.orderCashier(accountId.value, detail.value.order_no)
    gotUrl = res?.alipay_cashier_url || ''
  } catch {
    // 404（会话尚未建立）/ 网络抖动：视为本轮未就绪，静默重试
  } finally {
    cashierBusy = false
  }
  if (gotUrl) {
    cashierUrl.value = gotUrl
    stopCashierPoll() // 拿到链接即停，后续不再请求
    return
  }
  cashierPollCount += 1
  // 铸造超时感知：轮询 12 次仍空且已过 40s，提示续付重试（轮询继续到 20 次上限）
  if (cashierPollCount >= 12 && Date.now() - cashierStartTs >= 40000) {
    cashierState.value = 'failed'
  }
  if (cashierPollCount >= 20) stopCashierPoll() // 最多 20 次静默放弃
}

async function doAutoPay() {
  if (!detail.value?.order_no) return
  try {
    await ElMessageBox.confirm(
      '自动支付为实验性能力：依赖服务端支付宝会话接缝（scripts/alipay_autopay），未配置时返回 501。确定继续？',
      '自动支付 · 确认 1/2',
      { type: 'warning', confirmButtonText: '继续' },
    )
    await ElMessageBox.confirm(
      `将立即对订单 ${detail.value.order_no}（应付 ¥${detail.value.pay_amount ?? '—'}）触发支付宝侧扣款流程，请确认金额与账号无误。`,
      '自动支付 · 确认 2/2',
      { type: 'warning', confirmButtonText: '发起自动支付' },
    )
  } catch {
    return // 用户取消任一步确认
  }
  autoBusy.value = true
  try {
    const res = await apiOps.orderPay(accountId.value, detail.value.order_no, 'auto')
    ElMessage.success(res?.message || '自动支付已触发，每 5 秒轮询订单状态')
    startAutoPayPoll()
  } catch {
    // 失败提示由全局拦截器给出（如 501 未配置接缝）
  } finally {
    autoBusy.value = false
  }
}

function startAutoPayPoll() {
  stopAutoPayPoll()
  autoPayTimer = setInterval(autoPayTick, 5000)
}

function stopAutoPayPoll() {
  if (autoPayTimer) {
    clearInterval(autoPayTimer)
    autoPayTimer = null
  }
}

async function autoPayTick() {
  if (autoPollBusy || !detail.value?.order_no || !accountId.value) return
  autoPollBusy = true
  try {
    const res = await apiOps.orderStatus(accountId.value, detail.value.order_no)
    const st = Number(res?.status)
    if (st && st !== 1) {
      stopAutoPayPoll()
      ElMessage.success(
        `支付流程结束：订单 ${detail.value.order_no} → ${res.status_label || orderStatusTag(st).label}`,
      )
      refreshAfterStatusChange(detail.value.order_no)
    }
  } catch {
    // 单次轮询失败忽略；列表状态探针兜底
  } finally {
    autoPollBusy = false
  }
}

// 列表行操作：支付 = 打开抽屉并直接获取人工支付串；续付 = 打开抽屉并重铸
async function rowPay(row) {
  await openDetail(row)
  payMode.value = 'manual'
  await doManualPay()
}

async function rowContinuePay(row) {
  await openDetail(row)
  await doContinuePay()
}

// ---------- 支付串复制 ----------
async function copyPayStr() {
  const s = payLink.value?.order_str || ''
  if (!s) return
  try {
    await navigator.clipboard.writeText(s)
    ElMessage.success('支付串已复制')
  } catch {
    const ta = document.createElement('textarea')
    ta.value = s
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      ElMessage.success('支付串已复制')
    } catch {
      ElMessage.error('复制失败，请手动选择复制')
    }
    document.body.removeChild(ta)
  }
}

// ---------- 字段兜底取值 ----------
// items：契约 §2 未定死内层字段名，wire 实证为 camelCase（skuName/buyNum/totalItemAmount/
// specList[].specOptionName），同时兼容 snake_case 映射形态
function itemName(it) {
  return it.skuName || it.spuName || it.name || it.itemName || '—'
}

function itemQty(it) {
  const q = it.buyNum ?? it.quantity ?? it.num
  return q ?? 1
}

function itemAmount(it) {
  const a = it.totalItemAmount ?? it.salePrice ?? it.price ?? it.amount
  return a === undefined || a === null || a === '' ? null : a
}

function itemSpec(it) {
  const parts = []
  for (const s of it.specList || it.spec_list || []) {
    if (s?.specOptionName) parts.push(s.specOptionName)
  }
  for (const a of it.attributeList || it.attribute_list || []) {
    if (a?.attributeOptionName) parts.push(a.attributeOptionName)
  }
  return parts.join(' / ')
}

const promotions = computed(() => detail.value?.promotions || [])

function promoName(p) {
  return p.promotionName || p.promotion_name || '—'
}

function promoAmount(p) {
  const a = p.discountAmount ?? p.discount_amount
  return a === undefined || a === null || a === '' ? null : a
}

function promoId(p) {
  return p.promotionId ?? p.promotion_id ?? ''
}

function hasAmt(v) {
  return v !== undefined && v !== null && v !== ''
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
.opt-sub {
  float: right;
  color: var(--muted);
  font-size: 12px;
  margin-left: 14px;
}
.list-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.list-toolbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.order-table :deep(tbody tr) {
  cursor: pointer;
}
.order-table :deep(.row-pending) {
  background: #fffdf2;
}
.pickup-pill {
  display: inline-block;
  background: var(--tea-100);
  color: var(--tea-800);
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 1px;
  padding: 2px 10px;
  border-radius: 8px;
}
.pay-amount {
  font-weight: 700;
  color: var(--tea-800);
}
.muted {
  color: var(--muted);
}
.small {
  font-size: 12px;
}
/* 收银台铸造超时提示：Element Plus warning 色（带兜底） */
.cashier-warn {
  color: var(--el-color-warning, #e6a23c);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
.no-online {
  margin-top: 4px;
}

/* 全量模式：扫描进度面板与实时结果流（SSE 推流） */
.scan-panel {
  margin-top: 14px;
  border-top: 1px dashed #e3ebe7;
  padding-top: 12px;
}
.scan-meta {
  color: var(--muted);
  margin-top: 8px;
}
.scan-feed {
  margin-top: 8px;
  max-height: 168px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.scan-feed-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  background: #f7faf8;
  border: 1px solid #e8f0ec;
  border-radius: 8px;
  padding: 5px 10px;
}
.feed-account {
  min-width: 140px;
}
.pickup-only-switch {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 13px;
  white-space: nowrap;
}
.all-stats {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.stat-chip {
  border-radius: 999px;
}
.goods-desc {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}

/* 抽屉 */
.drawer-body {
  min-height: 120px;
}
.detail-head {
  text-align: center;
  padding: 6px 0 14px;
}
.detail-head-row {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.pickup-hero {
  margin-top: 10px;
}
.pickup-hero-label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  letter-spacing: 4px;
}
.pickup-hero-no {
  font-size: 38px;
  font-weight: 800;
  color: var(--tea-800);
  letter-spacing: 4px;
  line-height: 1.3;
}
.pickup-hint {
  margin: 4px 0 0;
}
.detail-desc {
  margin-bottom: 16px;
}
.sec-title {
  margin: 20px 0 10px;
  font-size: 15px;
  color: var(--tea-800);
}
.no-coupon {
  margin: 0;
  font-size: 13px;
}

/* 面板（待支付 / 支付串 / 等待） */
.panel {
  border: 1px solid #e4ece8;
  border-radius: 12px;
  padding: 14px 16px;
  margin: 0 0 14px;
  background: #fff;
}
.panel-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 15px;
  font-weight: 600;
  color: var(--tea-800);
  margin-bottom: 10px;
}
.countdown {
  font-size: 13px;
  color: var(--tea-700);
}
.countdown.urgent {
  color: #d24f28;
  font-weight: 700;
}
.pay-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.note {
  color: var(--muted);
  font-size: 12.5px;
  margin: 8px 0 0;
}
.paylink-panel {
  background: linear-gradient(180deg, #fdfcf7, #fff);
}
.order-str {
  margin-top: 12px;
}
.order-str-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  color: var(--tea-800);
  margin-bottom: 6px;
}
.link {
  color: var(--tea-600);
  word-break: break-all;
}
.break {
  word-break: break-all;
}

/* 等待信息 */
.waiting-panel {
  border-top: 3px solid var(--tea-600);
}
.waiting-ops {
  display: flex;
  align-items: center;
  gap: 8px;
}
.waiting-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
.waiting-cell {
  background: var(--tea-100);
  border-radius: 10px;
  padding: 12px 8px;
  text-align: center;
}
.waiting-value {
  font-size: 24px;
  font-weight: 700;
  color: var(--tea-800);
}
.waiting-label {
  font-size: 12px;
  color: var(--muted);
  margin-top: 2px;
}
@media (max-width: 720px) {
  .waiting-grid {
    grid-template-columns: 1fr;
  }
}
</style>
