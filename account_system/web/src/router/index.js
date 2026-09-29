import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/LoginView.vue'), meta: { public: true, title: '登录' } },
  {
    path: '/',
    component: () => import('../layouts/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', name: 'dashboard', component: () => import('../views/DashboardView.vue'), meta: { title: '仪表盘' } },
      { path: 'accounts', name: 'accounts', component: () => import('../views/AccountsView.vue'), meta: { title: '账号管理', perm: 'account:read' } },
      { path: 'ops/menu', name: 'menu-explorer', component: () => import('../views/MenuExplorerView.vue'), meta: { title: '游客菜单浏览器', perm: 'feature:menu' } },
      { path: 'ops/coupons', name: 'coupons', component: () => import('../views/CouponQueryView.vue'), meta: { title: '优惠券查询', perm: 'feature:coupon' } },
      { path: 'ops/order', name: 'order-create', component: () => import('../views/OrderWorkbenchView.vue'), meta: { title: '下单工作台', perm: 'feature:order' } },
      { path: 'ops/coupon-logs', name: 'coupon-logs', component: () => import('../views/CouponUsageLogView.vue'), meta: { title: '券使用记录', perm: 'feature:order' } },
      { path: 'ops/pickup', name: 'pickup', component: () => import('../views/PickupView.vue'), meta: { title: '取餐查询', perm: 'feature:pickup' } },
      { path: 'ops/intake', name: 'intake', component: () => import('../views/IntakeOrdersView.vue'), meta: { title: '订单中枢', perm: 'feature:order' } },
      { path: 'ops/keepalive', name: 'keepalive', component: () => import('../views/KeepaliveView.vue'), meta: { title: '账号保活跃', perm: 'account:read' } },
      { path: 'decision/plans', name: 'decision-plans', component: () => import('../views/OrderPlanView.vue'), meta: { title: '下单方案', perm: 'decision:manage' } },
      { path: 'decision/costs', name: 'decision-costs', component: () => import('../views/VoucherCostView.vue'), meta: { title: '券成本与阈值', perm: 'decision:manage' } },
      { path: 'system/users', name: 'users', component: () => import('../views/UsersView.vue'), meta: { title: '用户管理', perm: 'user:manage' } },
      { path: 'system/roles', name: 'roles', component: () => import('../views/RolesView.vue'), meta: { title: '角色权限', perm: 'role:manage' } },
      { path: 'system/audit', name: 'audit', component: () => import('../views/AuditView.vue'), meta: { title: '审计日志', perm: 'audit:read' } },
      { path: 'system/settings', name: 'system-settings', component: () => import('../views/SettingsView.vue'), meta: { title: '系统设置', perm: 'settings:manage' } },
      { path: 'profile', name: 'profile', component: () => import('../views/ProfileView.vue'), meta: { title: '个人设置' } },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.public) return true
  if (!auth.isLoggedIn) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.perm && !auth.can(to.meta.perm)) {
    return { name: 'dashboard' }
  }
  return true
})

export default router
