export function fmtTime(v) {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export const STATUS_TAG = {
  pending: { type: 'info', label: '待登录' },
  online: { type: 'success', label: '在线' },
  expired: { type: 'danger', label: '凭证失效' },
  disabled: { type: 'warning', label: '已停用' },
}

// 茶姬订单状态（wire 定案枚举：1/3/6/7；服务端 orderStatusText 优先，此表为本地兜底）
export const ORDER_STATUS = {
  1: { type: 'warning', label: '待支付' },
  3: { type: 'primary', label: '制作中' },
  6: { type: 'success', label: '已完成' },
  7: { type: 'info', label: '已取消' },
}

export function orderStatusTag(status) {
  return ORDER_STATUS[status] || { type: 'info', label: `状态${status}` }
}

export function fmtCountdown(seconds) {
  if (seconds == null || seconds < 0) return '—'
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

export function fmtWaiting(seconds) {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}
