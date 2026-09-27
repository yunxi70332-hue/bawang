<template>
  <div class="page-card">
    <div class="toolbar">
      <div>
        <h2 class="page-title">券使用记录</h2>
        <p class="page-subtitle">
          优惠券使用全量日志：使用时间 / 订单号 / 券ID / 账号与操作人 / 抵扣金额 / 结果（成功·验证拒绝·下单失败·已回滚），供查询与统计分析
        </p>
      </div>
      <div class="filters">
        <el-input v-model="query.keyword" placeholder="券码 / 订单号 / 券名 / 操作人 / 账号 / 手机号" clearable
          style="width: 260px" :prefix-icon="Search" @keyup.enter="load" @clear="load" />
        <el-select v-model="query.result" placeholder="全部结果" clearable style="width: 130px" @change="load">
          <el-option label="使用成功" value="success" />
          <el-option label="验证拒绝" value="rejected" />
          <el-option label="下单失败" value="failed" />
          <el-option label="已回滚" value="rolled_back" />
        </el-select>
        <el-button :icon="Refresh" circle @click="load" />
      </div>
    </div>

    <div v-if="stats" class="stat-row">
      <div class="stat-pill ok">成功 {{ stats.success }} 笔</div>
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
          <el-tag :type="RESULT_TAG[row.result]?.type" :title="RESULT_TAG[row.result]?.tip" size="small"
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
          <span v-if="row.result === 'success' || row.result === 'rolled_back'" class="price">¥{{ row.deduction }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="订单 / 金额" min-width="170">
        <template #default="{ row }">
          <template v-if="row.order_no">
            <div class="mono">{{ row.order_no }}</div>
            <div class="muted">总额 ¥{{ row.total_amount || '?' }} → 实付 ¥{{ row.pay_amount || '?' }}</div>
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
import { Refresh, Search } from '@element-plus/icons-vue'
import { apiOps } from '../api'
import { fmtTime } from '../utils/format'

// 下单手机号：后端按权限下发（account:update 可见完整号，否则脱敏；账号已删则无此字段）
function phoneOf(row) {
  return row.account_phone_full || row.account_phone_masked || ''
}

const RESULT_TAG = {
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
  margin-top: 14px;
}
</style>
