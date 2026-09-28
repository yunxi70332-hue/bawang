// SSE 事件流封装（后端 GET /api/events）。
// EventSource 无法携带 Authorization 头，JWT 改走 query 参数（后端 security.user_from_token_str）。
// 断线重连交给浏览器原生机制（onerror 后自动重试）；401/403 等致命错误由后端直接断流，
// readyState 变为 CLOSED，调用方据 onError 回调自行降级（如回落轮询）。
export function openEventStream({ topics, events = {}, onOpen, onError }) {
  const token = localStorage.getItem('token') || ''
  const url = `/api/events?topics=${encodeURIComponent(topics.join(','))}&token=${encodeURIComponent(token)}`
  const es = new EventSource(url)
  const bound = Object.entries(events).map(([name, fn]) => {
    const handler = (ev) => {
      let data = null
      try {
        data = ev.data ? JSON.parse(ev.data) : null
      } catch (e) {
        console.warn('SSE 数据解析失败', name, e)
      }
      fn(data)
    }
    es.addEventListener(name, handler)
    return [name, handler]
  })
  es.onopen = () => onOpen && onOpen(es)
  es.onerror = (err) => onError && onError(err, es)
  return {
    raw: es,
    close() {
      es.close()
      bound.forEach(([name, handler]) => es.removeEventListener(name, handler))
    },
  }
}
