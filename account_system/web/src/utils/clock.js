// 服务器时钟偏移校正工具（支付倒计时时间源对齐）
// 约定：任何接口响应携带 server_time（epoch 毫秒，docs/pay_timesync_design_20260928.md §1）
// 即由 axios 响应拦截器调用 calibrate 刷新全局偏移；页面倒计时统一取 nowMs()，不再直接用 Date.now()
let offset = 0

// 用服务端权威时间刷新偏移（非法/空值忽略，保持上次校准结果）
export function calibrate(serverTimeMs) {
  if (Number(serverTimeMs) > 0) offset = Number(serverTimeMs) - Date.now()
}

// 校准后的"当前时间"（毫秒）；未校准过时等价于 Date.now()
export function nowMs() {
  return Date.now() + offset
}

// 当前偏移量（调试/展示用）
export function offsetMs() {
  return offset
}
