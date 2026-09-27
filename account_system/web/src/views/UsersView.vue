<template>
  <div class="page-card">
    <div class="toolbar">
      <div>
        <h2 class="page-title">系统用户管理</h2>
        <p class="page-subtitle">管理登录本系统的用户账号及其角色（RBAC：角色决定可执行的操作）</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openCreate">新建用户</el-button>
    </div>

    <el-table v-loading="loading" :data="users" stripe>
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="username" label="用户名" width="140">
        <template #default="{ row }">
          <span class="mono">{{ row.username }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="display_name" label="显示名" min-width="140" />
      <el-table-column label="角色" width="140">
        <template #default="{ row }">
          <el-tag :type="roleTag(row.role?.name)" effect="plain">{{ row.role?.name }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.is_active ? 'success' : 'danger'" size="small" effect="dark">
            {{ row.is_active ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近登录" width="170">
        <template #default="{ row }">{{ fmtTime(row.last_login_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="260" fixed="right">
        <template #default="{ row }">
          <el-button size="small" plain @click="openEdit(row)">编辑</el-button>
          <el-button size="small" plain @click="openReset(row)">重置密码</el-button>
          <el-button size="small" type="danger" plain :disabled="row.username === 'admin'"
            @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dlg.edit" :title="editingId ? '编辑用户' : '新建用户'" width="440px" destroy-on-close>
      <el-form ref="formRef" :model="form" :rules="rules" label-width="88px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" :disabled="!!editingId" placeholder="字母/数字/下划线" maxlength="32" />
        </el-form-item>
        <el-form-item v-if="!editingId" label="初始密码" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
        <el-form-item label="显示名" prop="display_name">
          <el-input v-model="form.display_name" maxlength="32" placeholder="可选" />
        </el-form-item>
        <el-form-item label="角色" prop="role_id">
          <el-select v-model="form.role_id" style="width: 100%">
            <el-option v-for="r in roles" :key="r.id" :value="r.id" :label="`${r.name} — ${r.description}`" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="editingId" label="状态">
          <el-switch v-model="form.is_active" active-text="启用" inactive-text="停用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg.edit = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="dlg.reset" title="重置密码" width="400px" destroy-on-close>
      <p class="reset-tip">为用户 <b>{{ resetRow?.username }}</b> 设置新密码（至少 8 位）：</p>
      <el-input v-model="resetPwd" type="password" show-password placeholder="新密码" />
      <template #footer>
        <el-button @click="dlg.reset = false">取消</el-button>
        <el-button type="primary" @click="doReset">确认重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { apiRoles, apiUsers } from '../api'
import { fmtTime } from '../utils/format'

const loading = ref(false)
const saving = ref(false)
const users = ref([])
const roles = ref([])
const dlg = reactive({ edit: false, reset: false })
const formRef = ref()
const editingId = ref(null)
const form = reactive({ username: '', password: '', display_name: '', role_id: null, is_active: true })
const rules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { pattern: /^[a-zA-Z0-9_]{2,32}$/, message: '2-32 位字母/数字/下划线', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入初始密码', trigger: 'blur' },
    { min: 8, message: '至少 8 位', trigger: 'blur' },
  ],
  role_id: [{ required: true, message: '请选择角色', trigger: 'change' }],
}
const resetRow = ref(null)
const resetPwd = ref('')

function roleTag(name) {
  return { admin: 'danger', operator: 'success', viewer: 'info' }[name] || 'warning'
}

async function load() {
  loading.value = true
  try {
    const [u, r] = await Promise.all([apiUsers.list(), apiRoles.list()])
    users.value = u
    roles.value = r.roles
  } finally {
    loading.value = false
  }
}
onMounted(load)

function openCreate() {
  editingId.value = null
  Object.assign(form, { username: '', password: '', display_name: '', role_id: null, is_active: true })
  dlg.edit = true
}

function openEdit(row) {
  editingId.value = row.id
  Object.assign(form, {
    username: row.username, password: '', display_name: row.display_name,
    role_id: row.role?.id, is_active: row.is_active,
  })
  dlg.edit = true
}

async function save() {
  await formRef.value.validate().catch(() => Promise.reject())
  saving.value = true
  try {
    if (editingId.value) {
      await apiUsers.update(editingId.value, {
        display_name: form.display_name, role_id: form.role_id, is_active: form.is_active,
      })
      ElMessage.success('已保存')
    } else {
      await apiUsers.create({ ...form })
      ElMessage.success('用户已创建')
    }
    dlg.edit = false
    load()
  } finally {
    saving.value = false
  }
}

function openReset(row) {
  resetRow.value = row
  resetPwd.value = ''
  dlg.reset = true
}

async function doReset() {
  if (!resetPwd.value || resetPwd.value.length < 8) {
    ElMessage.warning('密码至少 8 位')
    return
  }
  await apiUsers.resetPassword(resetRow.value.id, { new_password: resetPwd.value })
  ElMessage.success('密码已重置')
  dlg.reset = false
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除用户「${row.username}」？`, '删除确认', { type: 'warning' })
    .catch(() => Promise.reject())
  await apiUsers.remove(row.id)
  ElMessage.success('已删除')
  load()
}
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
.reset-tip {
  margin: 0 0 12px;
  font-size: 14px;
}
</style>
