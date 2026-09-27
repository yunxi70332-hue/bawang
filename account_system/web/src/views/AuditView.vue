<template>
  <div class="page-card">
    <div class="toolbar">
      <div>
        <h2 class="page-title">审计日志</h2>
        <p class="page-subtitle">登录、账号变更与协议外向动作（发短信/登录/登出）全量留痕</p>
      </div>
      <div class="filters">
        <el-input v-model="query.keyword" placeholder="搜索用户 / 对象 / 动作" clearable style="width: 220px"
          :prefix-icon="Search" @keyup.enter="load" @clear="load" />
        <el-select v-model="query.action" placeholder="动作类型" clearable filterable style="width: 190px" @change="load">
          <el-option v-for="a in actions" :key="a" :label="a" :value="a" />
        </el-select>
        <el-button :icon="Refresh" circle @click="load" />
      </div>
    </div>

    <el-table v-loading="loading" :data="items" stripe>
      <el-table-column label="时间" width="165">
        <template #default="{ row }"><span class="mono">{{ fmtTime(row.created_at) }}</span></template>
      </el-table-column>
      <el-table-column prop="username" label="用户" width="110" />
      <el-table-column label="动作" width="170">
        <template #default="{ row }">
          <el-tag :type="actionTag(row.action)" size="small" effect="plain">{{ row.action }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="target" label="对象" min-width="180" />
      <el-table-column prop="ip" label="来源 IP" width="130" />
      <el-table-column label="详情" min-width="220">
        <template #default="{ row }">
          <span v-if="row.detail" class="mono detail">{{ row.detail }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
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
import { apiAudit } from '../api'
import { fmtTime } from '../utils/format'

const loading = ref(false)
const items = ref([])
const total = ref(0)
const query = reactive({ keyword: '', action: '', page: 1, page_size: 20 })

const actions = [
  'auth.login', 'auth.login_failed', 'auth.change_password',
  'account.create', 'account.update', 'account.delete',
  'account.send_sms', 'account.login', 'account.logout', 'account.check',
  'feature.coupon', 'user.create', 'user.update', 'user.delete', 'user.reset_password',
  'role.create', 'role.update', 'role.delete',
]

function actionTag(a) {
  if (a.includes('failed') || a.includes('delete')) return 'danger'
  if (a.startsWith('account.send_sms') || a.startsWith('account.login')) return 'warning'
  if (a.startsWith('feature')) return 'success'
  return 'info'
}

async function load() {
  loading.value = true
  try {
    const data = await apiAudit.list({ ...query })
    items.value = data.items
    total.value = data.total
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
  margin-bottom: 16px;
}
.filters {
  display: flex;
  gap: 10px;
}
.detail {
  color: var(--muted);
  word-break: break-all;
}
.muted {
  color: var(--muted);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
