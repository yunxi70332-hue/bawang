<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-brand">
        <div class="logo">茶</div>
        <h1>霸王茶姬 · 多账号管理系统</h1>
        <p>基于六功能纯协议层 · 前后端分离 · 角色权限控制</p>
      </div>
      <el-form ref="formRef" :model="form" :rules="rules" size="large" @keyup.enter="submit">
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="用户名" :prefix-icon="User" autocomplete="username" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input
            v-model="form.password" type="password" placeholder="密码"
            :prefix-icon="Lock" show-password autocomplete="current-password"
          />
        </el-form-item>
        <el-form-item class="remember-row">
          <el-checkbox v-model="remember">记住账号密码（下次打开自动填入）</el-checkbox>
        </el-form-item>
        <el-button type="primary" size="large" class="login-btn" :loading="loading" @click="submit">
          登 录
        </el-button>
      </el-form>
      <div class="login-foot">默认管理员：admin / Admin@123（首次登录后请修改密码）</div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const formRef = ref()
const loading = ref(false)
const form = reactive({ username: '', password: '' })
const remember = ref(true)
const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

// 打开页面即预填：上次记住的账号密码，否则默认管理员（内网工具，页脚本就公示）
form.username = localStorage.getItem('remember_username') || 'admin'
form.password = localStorage.getItem('remember_password') || 'Admin@123'
remember.value = localStorage.getItem('remember_opt') !== 'off'

async function submit() {
  await formRef.value.validate().catch(() => Promise.reject())
  loading.value = true
  try {
    await auth.login({ ...form })
    if (remember.value) {
      localStorage.setItem('remember_username', form.username)
      localStorage.setItem('remember_password', form.password)
      localStorage.setItem('remember_opt', 'on')
    } else {
      localStorage.removeItem('remember_username')
      localStorage.removeItem('remember_password')
      localStorage.setItem('remember_opt', 'off')
    }
    ElMessage.success(`欢迎，${auth.displayName}`)
    router.push(route.query.redirect || { name: 'dashboard' })
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background:
    radial-gradient(1000px 500px at 15% -10%, rgba(61, 139, 114, 0.28), transparent 60%),
    radial-gradient(800px 480px at 110% 110%, rgba(33, 94, 76, 0.35), transparent 55%),
    linear-gradient(160deg, #122b23, #0d1f19);
  padding: 20px;
  box-sizing: border-box;
}
.login-card {
  width: 400px;
  max-width: 100%;
  background: rgba(255, 255, 255, 0.97);
  border-radius: 20px;
  padding: 40px 38px 26px;
  box-shadow: 0 24px 70px rgba(0, 0, 0, 0.35);
}
.login-brand {
  text-align: center;
  margin-bottom: 28px;
}
.logo {
  width: 56px;
  height: 56px;
  margin: 0 auto 14px;
  border-radius: 16px;
  background: linear-gradient(135deg, var(--tea-500), var(--tea-800));
  color: #fff;
  font-size: 26px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}
h1 {
  font-size: 19px;
  color: var(--tea-900);
  margin: 0 0 6px;
}
.login-brand p {
  color: var(--muted);
  font-size: 12.5px;
  margin: 0;
  letter-spacing: 0.5px;
}
.login-btn {
  width: 100%;
  margin-top: 4px;
  letter-spacing: 6px;
}
.login-foot {
  margin-top: 18px;
  text-align: center;
  color: var(--muted);
  font-size: 12px;
}
</style>

.remember-row {
  margin-bottom: 6px;
}
.remember-row :deep(.el-form-item__content) {
  justify-content: flex-start;
}
