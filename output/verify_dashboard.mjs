// 无头 Chrome (CDP) 验证 /ops/coupons 仪表盘「可用次数」渲染
// 用法: node verify_dashboard.mjs  （需已启动 chrome --remote-debugging-port=9223）
import { readFileSync } from 'node:fs'

const auth = JSON.parse(readFileSync('E:/霸王茶姬/output/chagee_auth.json', 'utf-8'))

const targets = await (await fetch('http://127.0.0.1:9223/json/list')).json()
const page = targets.find((t) => t.type === 'page')
if (!page) throw new Error('no page target: ' + JSON.stringify(targets))

const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })

let seq = 0
const pending = new Map()
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data)
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id) }
}
const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = ++seq
    pending.set(id, (m) => (m.error ? reject(new Error(method + ': ' + JSON.stringify(m.error))) : resolve(m.result)))
    ws.send(JSON.stringify({ id, method, params }))
  })
async function evaluate(expression) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true })
  if (r.exceptionDetails) throw new Error('page eval failed: ' + JSON.stringify(r.exceptionDetails))
  return r.result?.value
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

await send('Page.enable')
await send('Runtime.enable')

// 1) 登录页注入登录态（同源 localStorage）
await send('Page.navigate', { url: 'http://127.0.0.1:8000/login' })
await sleep(1500)
await evaluate(`(function(){
  localStorage.setItem('token', ${JSON.stringify(auth.token)});
  localStorage.setItem('user', JSON.stringify({id:1, username:'admin'}));
  localStorage.setItem('permissions', JSON.stringify(${JSON.stringify(auth.permissions)}));
  return 'auth injected';
})()`)

// 2) 进入优惠券页
await send('Page.navigate', { url: 'http://127.0.0.1:8000/ops/coupons' })
await sleep(2000)

// 2.5) 选中目标账号 199****0332（Element Plus 下拉渲染在 body 级 popper 中）
const picked = await evaluate(`(function(){
  const sel = document.querySelector('.query-bar .el-select');
  if (!sel) return 'select not found';
  sel.querySelector('.el-select__wrapper')?.click() || sel.click();
  return 'select opened';
})()`)
console.log('select:', picked)
await sleep(800)
const chose = await evaluate(`(function(){
  const opt = [...document.querySelectorAll('.el-select-dropdown__item')]
    .find(li => li.textContent.includes('199****0332'));
  if (!opt) return 'option not found';
  opt.click(); return 'option clicked';
})()`)
console.log('option:', chose)
await sleep(800)

// 3) 点击「查询优惠券」
const clicked = await evaluate(`(function(){
  const b = [...document.querySelectorAll('button')].find(x => x.textContent.includes('查询优惠券'));
  if (!b) return 'button not found';
  b.click(); return 'clicked';
})()`)
console.log('button:', clicked)

// 4) 轮询汇总卡片（生产查询最长 ~40s）
let cards = null
for (let i = 0; i < 20 && !cards; i++) {
  await sleep(2000)
  cards = await evaluate(`(function(){
    const cs = [...document.querySelectorAll('.sum-card')];
    if (!cs.length) return null;
    const runAt = document.querySelector('.run-at');
    if (!runAt || !runAt.textContent.includes('查询时间')) return null;
    return {
      runAt: runAt.textContent.trim(),
      cards: cs.map(c => ({
        value: c.querySelector('.sum-value')?.textContent.trim(),
        label: c.querySelector('.sum-label')?.textContent.trim(),
      })),
      rows: [...document.querySelectorAll('.el-table__body-wrapper tbody tr')].slice(0, 6).map(tr => tr.textContent.trim().slice(0, 80)),
    };
  })()`)
}
ws.close()

if (!cards) {
  console.log('FAIL: 汇总卡片未出现')
  process.exit(1)
}
console.log(JSON.stringify(cards, null, 2))
const usable = cards.cards.find((c) => c.label === '可用次数')
if (!usable || usable.value !== '20') {
  console.log('FAIL: 可用次数卡片 =', JSON.stringify(usable))
  process.exit(1)
}
console.log('PASS: 仪表盘「可用次数」显示 20')
