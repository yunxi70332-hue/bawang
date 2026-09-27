<template>
  <div class="profile-grid">
    <div class="page-card">
      <h3 class="section-title">个人信息</h3>
      <el-descriptions :column="1" border>
        <el-descriptions-item label="用户名">{{ auth.user?.username }}</el-descriptions-item>
        <el-descriptions-item label="显示名">{{ auth.user?.display_name }}</el-descriptions-item>
        <el-descriptions-item label="角色">
          <el-tag type="success" effect="plain">{{ auth.roleName }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="最近登录">{{ fmtTime(auth.user?.last_login_at) }}</el-descriptions-item>
      </el-descriptions>
      <h3 class="section-title">我的权限（{{ auth.permissions.length }} 项）</h3>
      <div class="perm-tags">
        <el-tag v-for="p in auth.permissions" :key="p" class="perm-tag" effect="plain">{{ p }}</el-tag>
      </div>
    </div>

    <div class="page-card">
      <h3 class="section-title">修改密码</h3>
      <el-form ref="pwdRef" :model="pwd" :rules="pwdRules" label-width="90px" style="max-width: 420px">
        <el-form-item label="原密码" prop="old_password">
          <el-input v-model="pwd.old_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码" prop="new_password">
          <el-input v-model="pwd.new_password" type="password" show-password placeholder="至少 8 位，含字母与数字" />
        </el-form-item>
        <el-form-item label="确认新密码" prop="confirm">
          <el-input v-model="pwd.confirm" type="password" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="changePwd">保存修改</el-button>
        </el-form-item>
      </el-form>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiAuth } from '../api'
import { useAuthStore } from '../stores/auth'
import { fmtTime } from '../utils/format'

const auth = useAuthStore()
const pwdRef = ref()
const saving = ref(false)
const pwd = reactive({ old_password: '', new_password: '', confirm: '' })

const pwdRules = {
  old_password: [{ required: true, message: '请输入原密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 8, message: '至少 8 位', trigger: 'blur' },
    {
      validator: (_, v, cb) =>
        /[a-zA-Z]/.test(v) && /\d/.test(v) ? cb() : cb(new Error('须同时包含字母与数字')),
      trigger: 'blur',
    },
  ],
  confirm: [
    {
      validator: (_, v, cb) => (v === pwd.new_password ? cb() : cb(new Error('两次输入不一致'))),
      trigger: 'blur',
    },
  ],
}

async function changePwd() {
  await pwdRef.value.validate().catch(() => Promise.reject())
  saving.value = true
  try {
    await apiAuth.changePassword({ old_password: pwd.old_password, new_password: pwd.new_password })
    ElMessage.success('密码已修改')
    pwd.old_password = pwd.new_password = pwd.confirm = ''
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.profile-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
  align-items: start;
}
.section-title {
  margin: 0 0 14px;
  color: var(--tea-800);
  font-size: 15px;
}
.section-title + .el-descriptions {
  margin-bottom: 24px;
}
.perm-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.perm-tag {
  font-family: Consolas, monospace;
  font-size: 12px;
}
@media (max-width: 1000px) {
  .profile-grid {
    grid-template-columns: 1fr;
  }
}
</style>
