<template>
  <el-container class="layout">
    <!-- 移动端遮罩 -->
    <div v-if="isMobile && !collapsed" class="drawer-mask" @click="collapsed = true" />

    <el-aside :width="collapsed ? '64px' : '232px'" class="sidebar" :class="{ 'sidebar-mobile': isMobile }">
      <div class="brand">
        <div class="brand-logo">茶</div>
        <transition name="fade">
          <div v-if="!collapsed" class="brand-text">
            <div class="brand-name">多账号管理系统</div>
            <div class="brand-sub">CHAGEE · PROTOCOL</div>
          </div>
        </transition>
      </div>

      <el-scrollbar class="menu-scroll">
        <el-menu
          :default-active="activeMenu"
          :collapse="collapsed"
          :collapse-transition="false"
          background-color="transparent"
          text-color="#b8cec5"
          active-text-color="#ffffff"
          router
        >
          <el-menu-item index="/dashboard">
            <el-icon><Odometer /></el-icon><template #title>仪表盘</template>
          </el-menu-item>

          <el-sub-menu index="grp-account" v-if="auth.can('account:read')">
            <template #title><el-icon><User /></el-icon><span>账号管理</span></template>
            <el-menu-item index="/accounts">茶姬账号</el-menu-item>
          </el-sub-menu>

          <el-sub-menu index="grp-ops" v-if="auth.can('feature:menu') || auth.can('feature:coupon') || auth.can('feature:order') || auth.can('feature:pickup')">
            <template #title><el-icon><Coffee /></el-icon><span>协议功能</span></template>
            <el-menu-item v-if="auth.can('feature:menu')" index="/ops/menu">游客菜单 · F3</el-menu-item>
            <el-menu-item v-if="auth.can('feature:coupon')" index="/ops/coupons">优惠券查询 · F4</el-menu-item>
            <el-menu-item v-if="auth.can('feature:order')" index="/ops/order">下单 · F5</el-menu-item>
            <el-menu-item v-if="auth.can('feature:order')" index="/ops/coupon-logs">券使用记录</el-menu-item>
            <el-menu-item v-if="auth.can('feature:pickup')" index="/ops/pickup">取餐查询 · F6</el-menu-item>
          </el-sub-menu>

          <el-sub-menu index="grp-decision" v-if="auth.can('decision:manage')">
            <template #title><el-icon><Coin /></el-icon><span>下单决策</span></template>
            <el-menu-item index="/decision/packets">套餐配置</el-menu-item>
            <el-menu-item index="/decision/plans">下单方案</el-menu-item>
            <el-menu-item index="/decision/costs">券成本与阈值</el-menu-item>
          </el-sub-menu>

          <el-sub-menu index="grp-sys" v-if="auth.can('user:manage') || auth.can('role:manage') || auth.can('audit:read')">
            <template #title><el-icon><Setting /></el-icon><span>系统管理</span></template>
            <el-menu-item v-if="auth.can('user:manage')" index="/system/users">用户管理</el-menu-item>
            <el-menu-item v-if="auth.can('role:manage')" index="/system/roles">角色权限</el-menu-item>
            <el-menu-item v-if="auth.can('audit:read')" index="/system/audit">审计日志</el-menu-item>
          </el-sub-menu>
        </el-menu>
      </el-scrollbar>

      <div class="sidebar-footer">{{ collapsed ? 'v1' : 'v1.0 · 2026-09' }}</div>
    </el-aside>

    <el-container>
      <el-header class="topbar" height="58px">
        <div class="topbar-left">
          <el-icon class="collapse-btn" @click="collapsed = !collapsed">
            <Expand v-if="collapsed" /><Fold v-else />
          </el-icon>
          <el-breadcrumb separator="/">
            <el-breadcrumb-item :to="{ path: '/dashboard' }">首页</el-breadcrumb-item>
            <el-breadcrumb-item v-if="$route.meta.title">{{ $route.meta.title }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>

        <el-dropdown trigger="click" @command="onUserCommand">
          <div class="user-chip">
            <el-avatar :size="30" class="user-avatar">{{ avatarChar }}</el-avatar>
            <div class="user-meta">
              <div class="user-name">{{ auth.displayName }}</div>
              <div class="user-role">{{ auth.roleName }}</div>
            </div>
            <el-icon><ArrowDown /></el-icon>
          </div>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="profile"><el-icon><User /></el-icon>个人设置</el-dropdown-item>
              <el-dropdown-item divided command="logout"><el-icon><SwitchButton /></el-icon>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <el-main class="content">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" :key="$route.path" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const collapsed = ref(false)
const windowWidth = ref(window.innerWidth)

const isMobile = computed(() => windowWidth.value < 900)
const activeMenu = computed(() => route.path)
const avatarChar = computed(() => (auth.displayName || 'U').slice(0, 1).toUpperCase())

const onResize = () => {
  windowWidth.value = window.innerWidth
  if (windowWidth.value < 900) collapsed.value = true
  else collapsed.value = false
}
onMounted(() => {
  window.addEventListener('resize', onResize)
  onResize()
  auth.refresh()
})
onUnmounted(() => window.removeEventListener('resize', onResize))

function onUserCommand(cmd) {
  if (cmd === 'logout') {
    ElMessageBox.confirm('确定要退出登录吗？', '提示', { type: 'warning' })
      .then(() => {
        auth.logout()
        router.push({ name: 'login' })
      })
      .catch(() => {})
  } else if (cmd === 'profile') {
    router.push({ name: 'profile' })
  }
}
</script>

<style scoped>
.layout {
  height: 100%;
}
.sidebar {
  background: linear-gradient(180deg, var(--tea-900) 0%, #10251e 100%);
  display: flex;
  flex-direction: column;
  transition: width 0.22s ease, transform 0.22s ease;
  z-index: 120;
}
.sidebar-mobile {
  position: fixed;
  top: 0;
  bottom: 0;
  left: 0;
}
.drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(10, 20, 16, 0.45);
  z-index: 110;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 14px;
  min-height: 58px;
  box-sizing: border-box;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
}
.brand-logo {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  background: linear-gradient(135deg, var(--tea-500), var(--tea-700));
  color: #fff;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  flex-shrink: 0;
}
.brand-name {
  color: #fff;
  font-size: 14.5px;
  font-weight: 600;
  white-space: nowrap;
}
.brand-sub {
  color: var(--tea-300);
  font-size: 10px;
  letter-spacing: 1.5px;
  white-space: nowrap;
}
.menu-scroll {
  flex: 1;
}
.menu-scroll :deep(.el-menu) {
  border-right: none;
  padding: 8px;
}
.menu-scroll :deep(.el-menu-item.is-active) {
  background: var(--tea-600) !important;
  border-radius: 8px;
}
.menu-scroll :deep(.el-menu-item:hover) {
  background: rgba(255, 255, 255, 0.06);
  border-radius: 8px;
}
.menu-scroll :deep(.el-sub-menu__title:hover) {
  background: rgba(255, 255, 255, 0.06);
  border-radius: 8px;
}
.sidebar-footer {
  padding: 12px;
  color: var(--tea-300);
  font-size: 11px;
  text-align: center;
  opacity: 0.7;
}
.topbar {
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid #e8eeeb;
  padding: 0 18px;
  z-index: 100;
}
.topbar-left {
  display: flex;
  align-items: center;
  gap: 14px;
}
.collapse-btn {
  font-size: 20px;
  color: var(--tea-700);
  cursor: pointer;
}
.user-chip {
  display: flex;
  align-items: center;
  gap: 9px;
  cursor: pointer;
  padding: 5px 10px;
  border-radius: 10px;
}
.user-chip:hover {
  background: var(--tea-100);
}
.user-avatar {
  background: var(--tea-600);
  color: #fff;
  font-weight: 600;
}
.user-meta {
  text-align: left;
  line-height: 1.25;
}
.user-name {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--tea-800);
}
.user-role {
  font-size: 11px;
  color: var(--muted);
}
.content {
  padding: 20px;
  overflow-y: auto;
}
@media (max-width: 900px) {
  .content {
    padding: 12px;
  }
  .user-meta {
    display: none;
  }
}
</style>
