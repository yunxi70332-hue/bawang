<template>
  <div>
    <div class="page-card">
      <div class="toolbar">
        <div>
          <h2 class="page-title">茶姬账号管理</h2>
          <p class="page-subtitle">
            每个账号独立设备身份（uuid）与隔离会话文件；登录走生产短信验证码（单会话语义，登录会使该账号旧 token 失效）
          </p>
        </div>
        <el-button v-perm="'account:create'" type="primary" :icon="Plus" @click="openCreate">新建账号</el-button>
      </div>

      <div class="filters">
        <el-input v-model="query.keyword" placeholder="搜索备注名 / 手机号 / 昵称 / 备注" clearable
          style="width: 260px" :prefix-icon="Search" @keyup.enter="load" @clear="load" />
        <el-select v-model="query.status" placeholder="全部状态" clearable style="width: 130px" @change="load">
          <el-option v-for="(v, k) in STATUS_TAG" :key="k" :label="v.label" :value="k" />
        </el-select>
        <el-select v-model="query.group" placeholder="全部分组" clearable style="width: 130px" @change="load">
          <el-option v-for="g in groups" :key="g" :label="g" :value="g" />
        </el-select>
        <el-button :icon="Refresh" circle @click="load" />
      </div>

      <el-table v-loading="loading" :data="items" stripe>
        <el-table-column label="备注名" min-width="140">
          <template #default="{ row }">
            <span class="acc-label">{{ row.label }}</span>
            <el-tag v-if="row.group && row.group !== '默认'" size="small" effect="plain" class="group-tag">{{ row.group }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="手机号" width="125">
          <template #default="{ row }">
            <span class="mono">{{ row.phone_full || row.phone_masked }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="STATUS_TAG[row.status]?.type" effect="dark" size="small">
              {{ STATUS_TAG[row.status]?.label || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="茶姬身份" min-width="150">
          <template #default="{ row }">
            <div v-if="row.customer_id || row.nickname">
              <div>{{ row.nickname || '—' }}</div>
              <div class="mono muted">ID {{ row.customer_id || '—' }}</div>
            </div>
            <span v-else class="muted">未登录</span>
          </template>
        </el-table-column>
        <el-table-column label="Token 指纹" min-width="170">
          <template #default="{ row }">
            <el-tooltip v-if="row.has_token" content="双击复制完整 Token" placement="top">
              <span class="mono token-copy" :class="{ copied: copiedId === row.id }" @dblclick="copyToken(row)">{{
                copiedId === row.id ? '✓ 已复制到剪贴板' : row.token_fingerprint
              }}</span>
            </el-tooltip>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="最近登录" width="160">
          <template #default="{ row }">{{ fmtTime(row.last_login_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="330" fixed="right">
          <template #default="{ row }">
            <el-button v-perm="'account:login'" size="small" type="primary" plain
              :disabled="row.status === 'disabled'" @click="openLogin(row)">
              {{ row.status === 'online' ? '重新登录' : '登录' }}
            </el-button>
            <el-button v-perm="'account:login'" size="small" plain :disabled="!row.has_token && row.status !== 'online'"
              @click="checkAccount(row)">检查</el-button>
            <el-button size="small" plain @click="openDetail(row)">详情</el-button>
            <el-button v-perm="'account:update'" size="small" plain @click="openEdit(row)">编辑</el-button>
            <el-button v-perm="'account:delete'" size="small" type="danger" plain @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无账号，点击右上角「新建账号」开始" />
        </template>
      </el-table>

      <div class="pager">
        <el-pagination background layout="total, prev, pager, next, sizes" :total="total"
          v-model:current-page="query.page" v-model:page-size="query.page_size"
          :page-sizes="[10, 20, 50]" @current-change="load" @size-change="load" />
      </div>
    </div>

    <!-- 新建 / 编辑（新建时同一弹窗内完成：表单 → 输入验证码登录） -->
    <el-dialog v-model="dlg.edit" :title="editDialogTitle" width="480px" destroy-on-close
      :close-on-click-modal="false">
      <template v-if="editingId || createStep === 'form'">
        <el-form ref="editRef" :model="editForm" :rules="editRules" label-width="88px">
          <el-form-item label="备注名" prop="label">
            <el-input v-model="editForm.label" placeholder="如：主号-杭州" maxlength="64" />
          </el-form-item>
          <el-form-item label="手机号" prop="phone">
            <el-input v-model="editForm.phone" placeholder="11 位大陆手机号（用于接收登录验证码）" maxlength="11" />
          </el-form-item>
          <el-form-item label="分组" prop="group">
            <el-input v-model="editForm.group" placeholder="默认" maxlength="32">
              <template #append>
                <el-select v-model="editForm.group" style="width: 110px" placeholder="选择已有">
                  <el-option v-for="g in groups" :key="g" :label="g" :value="g" />
                </el-select>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item v-if="editingId" label="状态">
            <el-radio-group v-model="editForm.status">
              <el-radio value="pending">待登录</el-radio>
              <el-radio value="disabled">停用</el-radio>
              <el-radio value="online" disabled>在线（由登录流程维护）</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="备注" prop="note">
            <el-input v-model="editForm.note" type="textarea" :rows="2" maxlength="500" show-word-limit />
          </el-form-item>
        </el-form>
        <el-alert v-if="!editingId" type="warning" :closable="false" class="create-alert">
          <p>点击「保存并发送验证码」将向该手机号<b>真实发送</b>登录验证码（生产环境，60s 内仅可发送一次）。</p>
          <p>单会话语义：协议登录成功后，该账号在其他端的登录态将立即失效。</p>
        </el-alert>
      </template>

      <div v-else-if="createdRow">
        <el-descriptions :column="2" border size="small" class="login-desc">
          <el-descriptions-item label="账号">{{ createdRow.label }}</el-descriptions-item>
          <el-descriptions-item label="手机号">{{ createdRow.phone_full || createdRow.phone_masked }}</el-descriptions-item>
        </el-descriptions>
        <el-steps :active="1" align-center finish-status="success" class="login-steps">
          <el-step title="创建并发送验证码" /><el-step title="输入验证码登录" />
        </el-steps>
        <el-form @submit.prevent>
          <el-form-item>
            <el-input v-model="smsCode" size="large" placeholder="请输入短信验证码" maxlength="6"
              class="code-input" @keyup.enter="doLogin(createdRow, finishCreate)">
              <template #append>
                <el-button :disabled="resendIn > 0" @click="startSendSms(createdRow)">
                  {{ resendIn > 0 ? `${resendIn}s 后可重发` : '重新发送' }}
                </el-button>
              </template>
            </el-input>
          </el-form-item>
        </el-form>
        <el-button type="primary" size="large" style="width: 100%" :loading="loginBusy"
          @click="doLogin(createdRow, finishCreate)">
          登录并保存会话
        </el-button>
        <p class="create-later muted">稍后登录：直接关闭即可，账号已保存为「待登录」，可随时从列表行「登录」继续。</p>
      </div>

      <template #footer>
        <template v-if="editingId || createStep === 'form'">
          <el-button @click="dlg.edit = false">取消</el-button>
          <el-button v-if="!editingId" type="success" plain :loading="saving" @click="saveEdit(true)">
            保存并发送验证码
          </el-button>
          <el-button type="primary" :loading="saving" @click="saveEdit()">保存</el-button>
        </template>
        <el-button v-else @click="finishCreate">完成，返回列表</el-button>
      </template>
    </el-dialog>

    <!-- 协议登录（已有账号，两步：确认发短信 → 输入验证码） -->
    <el-dialog v-model="dlg.login" title="协议登录 · 短信验证码" width="460px" destroy-on-close
      :close-on-click-modal="false">
      <div v-if="loginRow">
        <el-descriptions :column="2" border size="small" class="login-desc">
          <el-descriptions-item label="账号">{{ loginRow.label }}</el-descriptions-item>
          <el-descriptions-item label="手机号">{{ loginRow.phone_full || loginRow.phone_masked }}</el-descriptions-item>
        </el-descriptions>

        <template v-if="loginStep === 1">
          <el-alert type="warning" :closable="false" class="login-alert">
            <p>将向该手机号<b>真实发送</b>登录验证码（生产环境，60s 内仅可发送一次）。</p>
            <p>单会话语义：协议登录成功后，该账号在其他端的登录态将立即失效。</p>
          </el-alert>
          <el-button type="primary" :loading="loginBusy" @click="doSendSms">确认发送验证码</el-button>
        </template>

        <template v-else>
          <el-steps :active="1" align-center finish-status="success" class="login-steps">
            <el-step title="发送验证码" /><el-step title="输入验证码登录" />
          </el-steps>
          <el-form @submit.prevent>
            <el-form-item>
              <el-input v-model="smsCode" size="large" placeholder="请输入短信验证码" maxlength="6"
                class="code-input" @keyup.enter="doLogin(loginRow, closeLoginDlg)">
                <template #append>
                  <el-button :disabled="resendIn > 0" @click="startSendSms(loginRow)">
                    {{ resendIn > 0 ? `${resendIn}s 后可重发` : '重新发送' }}
                  </el-button>
                </template>
              </el-input>
            </el-form-item>
          </el-form>
          <el-button type="primary" size="large" style="width: 100%" :loading="loginBusy"
            @click="doLogin(loginRow, closeLoginDlg)">
            登录并保存会话
          </el-button>
        </template>
      </div>
    </el-dialog>

    <!-- 详情抽屉 -->
    <el-drawer v-model="dlg.detail" title="账号详情" size="420px">
      <el-descriptions v-if="detailRow" :column="1" border>
        <el-descriptions-item label="ID">#{{ detailRow.id }}</el-descriptions-item>
        <el-descriptions-item label="备注名">{{ detailRow.label }}</el-descriptions-item>
        <el-descriptions-item label="手机号">{{ detailRow.phone_full || detailRow.phone_masked }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="STATUS_TAG[detailRow.status]?.type" size="small">{{ detailRow.status_label }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="分组">{{ detailRow.group }}</el-descriptions-item>
        <el-descriptions-item label="设备 uuid"><span class="mono">{{ detailRow.device_uuid }}</span></el-descriptions-item>
        <el-descriptions-item label="customerId">{{ detailRow.customer_id || '—' }}</el-descriptions-item>
        <el-descriptions-item label="昵称">{{ detailRow.nickname || '—' }}</el-descriptions-item>
        <el-descriptions-item label="Token">
          <span v-if="detailRow.token_fingerprint" class="mono token-copy"
            :class="{ copied: copiedId === detailRow.id }" title="双击复制完整 Token"
            @dblclick="copyToken(detailRow)">{{
              copiedId === detailRow.id ? '✓ 已复制到剪贴板' : detailRow.token_fingerprint
            }}</span>
          <span v-else class="mono">—（未登录）</span>
        </el-descriptions-item>
        <el-descriptions-item label="最近登录">{{ fmtTime(detailRow.last_login_at) }}</el-descriptions-item>
        <el-descriptions-item label="最近检查">{{ fmtTime(detailRow.last_check_at) }}</el-descriptions-item>
        <el-descriptions-item label="创建人">{{ detailRow.created_by }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ fmtTime(detailRow.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="备注">{{ detailRow.note || '—' }}</el-descriptions-item>
      </el-descriptions>
      <div v-if="detailRow" class="drawer-actions">
        <el-button v-if="detailRow.status === 'online'" v-perm="'account:login'" type="danger" plain
          @click="doLogout(detailRow)">协议登出（作废会话）</el-button>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { apiAccounts } from '../api'
import { fmtTime, STATUS_TAG } from '../utils/format'

const loading = ref(false)
const saving = ref(false)
const items = ref([])
const total = ref(0)
const groups = ref([])
const query = reactive({ keyword: '', status: '', group: '', page: 1, page_size: 10 })

const dlg = reactive({ edit: false, login: false, detail: false })
const editRef = ref()
const editingId = ref(null)
const editForm = reactive({ label: '', phone: '', group: '默认', note: '', status: 'pending' })
const editRules = {
  label: [{ required: true, message: '请输入备注名', trigger: 'blur' }],
  phone: [
    { required: true, message: '请输入手机号', trigger: 'blur' },
    { pattern: /^1[3-9]\d{9}$/, message: '手机号格式不正确', trigger: 'blur' },
  ],
}

const loginRow = ref(null)
const loginStep = ref(1)
const loginBusy = ref(false)
const smsCode = ref('')
const resendIn = ref(0)
let resendTimer = null

// 新建账号弹窗内部阶段：'form' 表单 → 'code' 输入验证码登录（仅新建模式）
const createStep = ref('form')
const createdRow = ref(null)
const editDialogTitle = computed(() => {
  if (editingId.value) return '编辑账号'
  return createStep.value === 'code' ? '新建账号 · 登录' : '新建账号'
})

const detailRow = ref(null)

// 双击 Token 指纹 → 拉取完整 token 写入剪贴板，单元格短暂显示「已复制」
const copiedId = ref(null)
let copiedTimer = null

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // 非安全上下文（http 且非 localhost）时 clipboard API 不可用，回退 execCommand
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  }
}

async function copyToken(row) {
  if (!row || (!row.has_token && !row.token_fingerprint)) return
  const res = await apiAccounts.token(row.id)
  if (!(await copyText(res.token))) {
    ElMessage.error('复制失败，浏览器拒绝了剪贴板访问')
    return
  }
  copiedId.value = row.id
  clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => { copiedId.value = null }, 2000)
  ElMessage.success(`已复制「${row.label}」完整 Token（${res.fingerprint}）`)
}

async function load() {
  loading.value = true
  try {
    const data = await apiAccounts.list({ ...query })
    items.value = data.items
    total.value = data.total
    groups.value = data.groups
  } finally {
    loading.value = false
  }
}
onMounted(load)

function openCreate() {
  editingId.value = null
  createStep.value = 'form'
  createdRow.value = null
  smsCode.value = ''
  Object.assign(editForm, { label: '', phone: '', group: '默认', note: '', status: 'pending' })
  dlg.edit = true
}

function openEdit(row) {
  editingId.value = row.id
  Object.assign(editForm, {
    label: row.label,
    phone: row.phone_full || '',
    group: row.group,
    note: row.note,
    status: row.status === 'disabled' ? 'disabled' : 'pending',
  })
  dlg.edit = true
}

async function saveEdit(andLogin = false) {
  await editRef.value.validate().catch(() => Promise.reject())
  saving.value = true
  try {
    if (editingId.value) {
      await apiAccounts.update(editingId.value, { ...editForm })
      ElMessage.success('已保存')
      dlg.edit = false
    } else {
      const created = await apiAccounts.create({ ...editForm })
      load()
      if (andLogin) {
        // 同一弹窗内完成：创建成功 → 立即发送验证码 → 切到输码界面
        createdRow.value = created
        smsCode.value = ''
        createStep.value = 'code'
        // 发送失败不回退表单（账号已创建，回退会导致重复建号），停留输码界面可手动重发
        await startSendSms(created)
      } else {
        ElMessage.success('账号已创建，可发起协议登录')
        dlg.edit = false
      }
    }
  } finally {
    saving.value = false
  }
}

function finishCreate() {
  dlg.edit = false
}

async function remove(row) {
  await ElMessageBox.confirm(
    `确定删除账号「${row.label}」？将同时清除其本地会话文件。`,
    '删除确认', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' },
  ).catch(() => Promise.reject())
  await apiAccounts.remove(row.id)
  ElMessage.success('已删除')
  load()
}

function openLogin(row) {
  loginRow.value = row
  loginStep.value = 1
  smsCode.value = ''
  dlg.login = true
}

function startResendCountdown() {
  resendIn.value = 65
  clearInterval(resendTimer)
  resendTimer = setInterval(() => {
    resendIn.value -= 1
    if (resendIn.value <= 0) clearInterval(resendTimer)
  }, 1000)
}

// 共享发码：成功启动 65s 重发倒计时；失败由 axios 拦截器统一 toast，返回 false 由调用方决定停留界面
async function startSendSms(row) {
  loginBusy.value = true
  try {
    await apiAccounts.sendSms(row.id)
    startResendCountdown()
    ElMessage.success('验证码已发送，请查收短信')
    return true
  } catch {
    return false
  } finally {
    loginBusy.value = false
  }
}

// 协议登录弹窗第一步「确认发送验证码」（新建流程已合并进新建弹窗，不走这里）
async function doSendSms() {
  if (await startSendSms(loginRow.value)) loginStep.value = 2
}

function closeLoginDlg() {
  dlg.login = false
}

// 共享登录：onDone 负责关闭各自弹窗
async function doLogin(row, onDone) {
  if (!/^\d{4,6}$/.test(smsCode.value)) {
    ElMessage.warning('请输入 4-6 位数字验证码')
    return
  }
  loginBusy.value = true
  try {
    const res = await apiAccounts.login(row.id, smsCode.value)
    ElMessage.success(`登录成功：${res.nickname || '茶友'}（customerId ${res.customer_id || '—'}）`)
    onDone()
    load()
  } finally {
    loginBusy.value = false
  }
}

async function checkAccount(row) {
  const res = await apiAccounts.check(row.id)
  if (res.status === 'online') {
    ElMessage.success(`会话有效 · ${res.nickname || ''} · ID ${res.customer_id || '—'}`)
  } else {
    ElMessage.error(res.message || '凭证已失效')
  }
  load()
}

async function doLogout(row) {
  await ElMessageBox.confirm(
    '协议登出将作废当前 token（服务端会话注销），账号回到「待登录」状态。',
    '登出确认', { type: 'warning' },
  ).catch(() => Promise.reject())
  await apiAccounts.logout(row.id)
  ElMessage.success('已登出')
  dlg.detail = false
  load()
}

async function openDetail(row) {
  detailRow.value = await apiAccounts.get(row.id)
  dlg.detail = true
}
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}
.filters {
  display: flex;
  gap: 10px;
  margin: 4px 0 16px;
  flex-wrap: wrap;
}
.acc-label {
  font-weight: 600;
  color: var(--tea-800);
}
.group-tag {
  margin-left: 8px;
}
.muted {
  color: var(--muted);
}
.token-copy {
  cursor: copy;
  border-bottom: 1px dashed var(--muted);
  transition: color 0.2s;
}
.token-copy:hover {
  color: var(--tea-600);
}
.token-copy.copied {
  color: var(--el-color-success);
  border-bottom: none;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
.login-alert {
  margin: 14px 0;
}
.login-alert p {
  margin: 4px 0;
  font-size: 13px;
}
.create-alert p {
  margin: 4px 0;
  font-size: 13px;
}
.create-later {
  margin: 12px 0 0;
  font-size: 12px;
  text-align: center;
}
.login-steps {
  margin: 14px 0 18px;
}
.code-input :deep(.el-input__inner) {
  letter-spacing: 6px;
  font-size: 18px;
  text-align: center;
}
.login-desc {
  margin-bottom: 4px;
}
.drawer-actions {
  margin-top: 18px;
}
</style>
