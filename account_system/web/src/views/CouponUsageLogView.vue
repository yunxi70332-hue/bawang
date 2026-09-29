<template>
  <div class="page-card">
    <div class="toolbar">
      <div>
        <h2 class="page-title">券使用记录</h2>
        <p class="page-subtitle">
          优惠券使用全量日志：使用时间 / 订单号 / 券ID / 账号与操作人 / 抵扣金额 / 结果（待支付·使用成功·验证拒绝·下单失败·已回滚），状态随订单实付进展实时流转，供查询与统计分析
        </p>
      </div>
      <div class="filters">
        <el-input v-model="query.keyword" placeholder="券码 / 订单号 / 券名 / 操作人 / 账号 / 手机号" clearable
          style="width: 260px" :prefix-icon="Search" @keyup.enter="load" @clear="load" />
        <el-select v-model="query.result" placeholder="全部结果" clearable style="width: 130px" @change="load">
          <el-option label="待支付" value="pending" />
          <el-option label="使用成功" value="success" />
          <el-option label="验证拒绝" value="rejected" />
          <el-option label="下单失败" value="failed" />
          <el-option label="已回滚" value="rolled_back" />
        </el-select>
        <el-button :icon="Refresh" circle @click="load" />
      </div>
    </div>

    <div v-if="stats" class="stat-row">
      <div class="stat-pill ok">成功（已核销） {{ stats.success }} 笔</div>
      <div class="stat-pill pend">待支付 {{ stats.pending ?? 0 }} 笔</div>
      <div class="stat-pill warn">拒绝 {{ stats.rejected }} 次</div>
      <div class="stat-pill bad">失败 {{ stats.failed }} 次</div>
      <div class="stat-pill roll">回滚 {{ stats.rolled_back ?? 0 }} 次</div>
      <div class="stat-pill ded">累计抵扣 ¥{{ stats.total_deduction }}</div>
    </div>

    <el-table v-loading="loading" :data="items" stripe>
      <el-table-column label="时间" width="165">
        <template #default="{ row }"><span class="mono">{{ fmtTime(row.used_at) }}</span></template>
      </el-table-column>
      <el-table-column label="结果" width="100">
        <template #default="{ row }">
          <el-tooltip v-if="histLines(row).length" placement="top" :show-after="300">
            <template #content>
              <div class="hist-tip">
                <div v-for="(line, i) in histLines(row)" :key="i">{{ line }}</div>
              </div>
            </template>
            <el-tag :type="RESULT_TAG[row.result]?.type" size="small" effect="dark" class="hist-tag">
              {{ row.result_label || RESULT_TAG[row.result]?.label }}*
            </el-tag>
          </el-tooltip>
          <el-tag v-else :type="RESULT_TAG[row.result]?.type" :title="RESULT_TAG[row.result]?.tip" size="small"
            effect="dark">
            {{ row.result_label || RESULT_TAG[row.result]?.label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="券名称" min-width="200">
        <template #default="{ row }">
          <div>{{ row.coupon_name || '—' }}</div>
          <div class="mono muted">{{ row.coupon_code }}</div>
        </template>
      </el-table-column>
      <el-table-column label="抵扣" width="95">
        <template #default="{ row }">
          <span v-if="['success', 'rolled_back', 'pending'].includes(row.result)" class="price">¥{{ row.deduction }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="订单 / 金额" min-width="190">
        <template #default="{ row }">
          <template v-if="row.order_no">
            <div class="mono">{{ row.order_no }}</div>
            <div class="muted">总额 ¥{{ row.total_amount || '?' }} → 实付 ¥{{ row.pay_amount || '?' }}</div>
            <div v-if="row.order_status_label" class="muted order-status">
              订单状态：{{ row.order_status_label }}
            </div>
            <el-button v-if="row.order_status === 6" class="view-order" size="small" plain
              @click="goOrder(row)">查看订单</el-button>
          </template>
          <span v-else class="muted">未成单</span>
        </template>
      </el-table-column>
      <el-table-column label="茶姬账号" min-width="150">
        <template #default="{ row }">
          <div>{{ row.account_label }}</div>
          <div v-if="phoneOf(row)" class="mono muted">{{ phoneOf(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="operator" label="操作人" width="100" />
      <el-table-column label="失败/拒绝原因" min-width="180">
        <template #default="{ row }">
          <span v-if="row.fail_reason" class="muted">{{ row.fail_reason }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无券使用记录——在下单工作台使用优惠券后会在此留痕" />
      </template>
    </el-table>

    <div class="pager">
      <el-pagination background layout="total, prev, pager, next" :total="total"
        v-model:current-page="query.page" :page-size="query.page_size" @current-change="load" />
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh, Search } from '@element-plus/icons-vue'
import { apiOps } from '../api'
import { fmtTime } from '../utils/format'

const router = useRouter()

/* 查看订单（财务/客服对账，2026-09-29）：仅「已下单且交易状态=已完成(6)」行展示按钮，
 * 跳转复用取餐查询页的订单详情抽屉（深链 view=all&order_no 自动打开）；
 * 未完成订单不展示（详情依赖支付/履约完结，对账口径以完成为准） */
function goOrder(row) {
  router.push({ path: '/ops/pickup', query: {
    view: 'all',
    order_no: row.order_no,
    ...(row.account_id ? { account_id: row.account_id } : {}),
  } })
}

// 下单手机号：后端按权限下发（account:update 可见完整号，否则脱敏；账号已删则无此字段）
function phoneOf(row) {
  return row.account_phone_full || row.account_phone_masked || ''
}

const RESULT_TAG = {
  // 差额单成单即预记「待支付」：支付确认自动转 success，超时取消原地转 rolled_back（§18）
  pending: {
    type: 'warning',
    label: '待支付',
    tip: '差额单已创建待支付——支付确认后转「使用成功」，超时取消自动回滚',
  },
  success: { type: 'success', label: '使用成功' },
  rejected: { type: 'warning', label: '验证拒绝' },
  failed: { type: 'danger', label: '下单失败' },
  // 订单超时未支付，券自动回滚为未使用（后端统计字段可能未部署，前端做空值兼容）
  rolled_back: {
    type: 'info',
    label: '已回滚',
    tip: '订单超时未支付，券状态已自动恢复为未使用',
  },
}

const STATE_LABEL = { pending: '待支付', success: '使用成功', rejected: '验证拒绝',
  failed: '下单失败', rolled_back: '已回滚' }

// 状态轨迹（§18）：把后端 state_history 渲染为「时间 由A→B（actor·原因）」行；带 * 标记的标签悬停可见
function histLines(row) {
  return (row.state_history || []).map(h =>
    `${h.at || ''} ${STATE_LABEL[h.from] || h.from || '—'} → ${STATE_LABEL[h.to] || h.to}（${h.by || 'system'}${h.reason ? ' · ' + h.reason : ''}）`)
}

const loading = ref(false)
const items = ref([])
const total = ref(0)
const stats = ref(null)
const query = reactive({ keyword: '', result: '', page: 1, page_size: 20 })

async function load() {
  loading.value = true
  try {
    const data = await apiOps.couponUsageLogs({ ...query })
    items.value = data.items
    total.value = data.total
    stats.value = data.stats
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.filters {
  display: flex;
  gap: 10px;
}
.stat-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.stat-pill {
  border-radius: 999px;
  padding: 5px 14px;
  font-size: 13px;
  font-weight: 600;
}
.stat-pill.ok {
  background: var(--tea-100);
  color: var(--tea-700);
}
.stat-pill.warn {
  background: #fdf3e3;
  color: #a86f14;
}
.stat-pill.bad {
  background: #fdecec;
  color: #c45656;
}
.stat-pill.ded {
  background: #eef3f9;
  color: #33658a;
}
.stat-pill.roll {
  background: #f0f2f5;
  color: #909399;
}
.stat-pill.pend {
  background: #eef6fb;
  color: #2d6fa8;
}
.hist-tag {
  cursor: help;
}
.hist-tip {
  max-width: 420px;
  line-height: 1.7;
}
.price {
  color: #c45656;
  font-weight: 600;
}
.order-status {
  margin-top: 2px;
}
.view-order {
  margin-top: 6px;
  margin-left: 0;
}
.muted {
  color: var(--muted);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}
</style>
