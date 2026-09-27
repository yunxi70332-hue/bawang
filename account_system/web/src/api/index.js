import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '../router'
import { calibrate } from '../utils/clock'

const http = axios.create({ baseURL: '/', timeout: 30000 })

http.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

http.interceptors.response.use(
  (res) => {
    // 服务器时钟校准：响应体（一层或 data 包一层）携带 server_time（epoch ms）即刷新全局偏移，
    // 之后所有倒计时经 utils/clock.nowMs() 取时，消除客户端时钟漂移
    const st = res?.data?.server_time ?? res?.data?.data?.server_time ?? res?.server_time
    if (st) calibrate(st)
    return res.data
  },
  (err) => {
    const status = err.response?.status
    if (status === 401) {
      localStorage.removeItem('token')
      if (router.currentRoute.value.name !== 'login') {
        ElMessage.error('登录已过期，请重新登录')
        router.push({ name: 'login' })
      }
    } else if (!err.config?.silent) {
      // silent 由调用方显式传入：轮询类请求（如收银台铸造查询）失败由调用方静默重试，不弹全局错误
      ElMessage.error(formatError(err))
    }
    return Promise.reject(err)
  },
)

function formatError(err) {
  const detail = err.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join('; ')
  return err.message || '请求失败'
}

export default http

export const apiAuth = {
  login: (data) => http.post('/api/auth/login', data),
  me: () => http.get('/api/auth/me'),
  changePassword: (data) => http.post('/api/auth/change-password', data),
}

export const apiUsers = {
  list: () => http.get('/api/users'),
  create: (d) => http.post('/api/users', d),
  update: (id, d) => http.put(`/api/users/${id}`, d),
  remove: (id) => http.delete(`/api/users/${id}`),
  resetPassword: (id, d) => http.post(`/api/users/${id}/reset-password`, d),
}

export const apiRoles = {
  list: () => http.get('/api/roles'),
  create: (d) => http.post('/api/roles', d),
  update: (id, d) => http.put(`/api/roles/${id}`, d),
  remove: (id) => http.delete(`/api/roles/${id}`),
}

export const apiAccounts = {
  list: (params) => http.get('/api/accounts', { params }),
  create: (d) => http.post('/api/accounts', d),
  get: (id) => http.get(`/api/accounts/${id}`),
  token: (id) => http.get(`/api/accounts/${id}/token`),
  update: (id, d) => http.put(`/api/accounts/${id}`, d),
  remove: (id) => http.delete(`/api/accounts/${id}`),
  sendSms: (id) => http.post(`/api/accounts/${id}/send-sms`),
  login: (id, code) => http.post(`/api/accounts/${id}/login`, { code }),
  logout: (id) => http.post(`/api/accounts/${id}/logout`),
  check: (id) => http.post(`/api/accounts/${id}/check`),
}

export const apiOps = {
  cities: () => http.get('/api/ops/cities'),
  stores: (params) => http.get('/api/ops/stores', { params }),
  menu: (store) => http.get('/api/ops/menu', { params: { store } }),
  goods: (spuId, store) => http.get('/api/ops/goods', { params: { spu_id: spuId, store } }),
  pickupContext: () => http.get('/api/ops/pickup-context'),
  pickupStatus: () => http.get('/api/ops/pickup-status'),
  coupons: (accountId) => http.post(`/api/ops/accounts/${accountId}/coupons`),
  // 全量优惠券查询：遍历系统内所有账号 token 批量收集券ID（多账号串行，放宽超时）
  couponsSyncAll: () => http.post('/api/ops/coupons/sync-all', {}, { timeout: 300000 }),
  // 券档案模糊搜索：券码/名称/权益/使用范围等多维度 LIKE 匹配
  couponsSearch: (params) => http.get('/api/ops/coupons/search', { params }),
  // F5/F6 订单族（契约 docs/api_contract_f5f6.md）
  orderSettle: (accountId, payload) => http.post(`/api/ops/accounts/${accountId}/orders/settle`, payload),
  orderCreate: (accountId, payload) => http.post(`/api/ops/accounts/${accountId}/orders/create`, payload),
  orderList: (accountId, params) => http.get(`/api/ops/accounts/${accountId}/orders`, { params }),
  orderDetail: (accountId, orderNo) => http.get(`/api/ops/accounts/${accountId}/orders/${orderNo}`),
  orderStatus: (accountId, orderNo) => http.get(`/api/ops/accounts/${accountId}/orders/${orderNo}/status`),
  orderWaiting: (accountId, orderNo) => http.get(`/api/ops/accounts/${accountId}/orders/${orderNo}/waiting`),
  orderContinuePay: (accountId, orderNo) => http.post(`/api/ops/accounts/${accountId}/orders/${orderNo}/continue-pay`),
  orderCancel: (accountId, orderNo) => http.post(`/api/ops/accounts/${accountId}/orders/${orderNo}/cancel`),
  orderPay: (accountId, orderNo, mode) => http.post(`/api/ops/accounts/${accountId}/orders/${orderNo}/pay`, { mode }),
  // 官方收银台链接查询：后端在下单差额/pay manual/续付后异步铸造（云手机约 10-30 秒）写入
  // PaySession.alipay_cashier_url；无支付会话返回 404 —— 轮询场景传 silent，失败静默由调用方重试
  orderCashier: (accountId, orderNo) => http.get(`/api/ops/accounts/${accountId}/orders/${orderNo}/cashier`, { silent: true }),
  // 优惠券使用规则（券档案 / 使用日志）
  couponUsageLogs: (params) => http.get('/api/ops/coupon-usage-logs', { params }),
  couponRecords: (accountId, params) => http.get(`/api/ops/accounts/${accountId}/coupons/records`, { params }),
}

export const apiAudit = {
  list: (params) => http.get('/api/audit', { params }),
}

export const apiDashboard = {
  stats: () => http.get('/api/dashboard/stats'),
}
