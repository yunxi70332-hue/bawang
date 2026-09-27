<template>
  <div class="page-card">
    <div class="toolbar">
      <div>
        <h2 class="page-title">角色与权限矩阵</h2>
        <p class="page-subtitle">
          角色持有权限点集合；接口与页面按钮按权限点双重校验。admin 为内置角色，权限全量锁定不可修改。
        </p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openCreate">新建角色</el-button>
    </div>

    <el-table v-loading="loading" :data="roles" stripe>
      <el-table-column prop="name" label="角色名" width="140">
        <template #default="{ row }">
          <b>{{ row.name }}</b>
          <el-tag v-if="row.is_builtin" size="small" effect="plain" style="margin-left: 6px">内置</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="说明" min-width="200" />
      <el-table-column label="权限数" width="90" align="center">
        <template #default="{ row }">
          <el-tag effect="plain" size="small">{{ row.permissions.length }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="用户数" width="90" align="center">
        <template #default="{ row }">{{ row.user_count }}</template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button size="small" plain :disabled="row.name === 'admin'" @click="openEdit(row)">编辑权限</el-button>
          <el-button size="small" type="danger" plain :disabled="row.is_builtin" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dlg" :title="editing ? `编辑角色：${editing.name}` : '新建角色'" width="640px" destroy-on-close>
      <el-form label-width="72px">
        <template v-if="!editing">
          <el-form-item label="角色名" required>
            <el-input v-model="newRole.name" maxlength="32" placeholder="如：auditor" />
          </el-form-item>
          <el-form-item label="说明">
            <el-input v-model="newRole.description" maxlength="128" />
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="说明">
            <el-input v-model="editing.description" maxlength="128" />
          </el-form-item>
        </template>

        <el-form-item label="权限矩阵">
          <div class="perm-groups">
            <div v-for="g in permissionTree" :key="g.group" class="perm-group">
              <div class="perm-group-title">{{ g.group }}</div>
              <el-checkbox-group v-model="selected">
                <el-checkbox v-for="p in g.items" :key="p.code" :value="p.code" class="perm-item">
                  {{ p.name }}
                  <span class="mono perm-code">{{ p.code }}</span>
                </el-checkbox>
              </el-checkbox-group>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { apiRoles } from '../api'

const loading = ref(false)
const saving = ref(false)
const roles = ref([])
const permissionTree = ref([])
const dlg = ref(false)
const editing = ref(null)
const selected = ref([])
const newRole = reactive({ name: '', description: '' })

async function load() {
  loading.value = true
  try {
    const data = await apiRoles.list()
    roles.value = data.roles
    permissionTree.value = data.permission_tree
  } finally {
    loading.value = false
  }
}
onMounted(load)

function openCreate() {
  editing.value = null
  Object.assign(newRole, { name: '', description: '' })
  selected.value = []
  dlg.value = true
}

function openEdit(row) {
  editing.value = { ...row }
  selected.value = [...row.permissions]
  dlg.value = true
}

async function save() {
  saving.value = true
  try {
    if (editing.value) {
      await apiRoles.update(editing.value.id, { description: editing.value.description, permissions: selected.value })
      ElMessage.success('角色已更新')
    } else {
      if (!newRole.name) {
        ElMessage.warning('请输入角色名')
        saving.value = false
        return
      }
      await apiRoles.create({ ...newRole, permissions: selected.value })
      ElMessage.success('角色已创建')
    }
    dlg.value = false
    load()
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  await ElMessageBox.confirm(
    `确定删除角色「${row.name}」？仍有用户使用时将无法删除。`, '删除确认', { type: 'warning' },
  ).catch(() => Promise.reject())
  await apiRoles.remove(row.id)
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
.perm-groups {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  width: 100%;
}
.perm-group {
  border: 1px solid #e5ece9;
  border-radius: 10px;
  padding: 10px 12px;
}
.perm-group-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--tea-700);
  margin-bottom: 6px;
}
.perm-item {
  display: flex;
  height: 26px;
}
.perm-code {
  color: var(--muted);
  margin-left: 6px;
  font-size: 11px;
}
@media (max-width: 760px) {
  .perm-groups {
    grid-template-columns: 1fr;
  }
}
</style>
