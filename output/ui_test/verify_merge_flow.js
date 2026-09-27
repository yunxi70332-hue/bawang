// 无头 UI 验证：新建账号弹窗内完成「创建 + 发码 + 输码登录」全流程（对 mock 后端，安全）
const { chromium } = require('playwright-core');

const BASE = 'http://127.0.0.1:8800';
const SHOT_DIR = __dirname;
let pass = 0, fail = 0;
function check(name, ok, extra = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'} | ${name}${extra ? ' | ' + extra : ''}`);
  ok ? pass++ : fail++;
}

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.setDefaultTimeout(8000);

  // --- 登录 ---
  await page.goto(BASE + '/');
  await page.getByPlaceholder('用户名').fill('admin');
  await page.getByPlaceholder('密码').fill('Admin@123');
  await page.getByRole('button', { name: /登\s*录/ }).click();
  await page.waitForURL('**/dashboard**', { timeout: 8000 }).catch(() => {});
  await page.goto(BASE + '/accounts');
  await page.waitForSelector('.el-table');
  check('登录并进入账号管理页', await page.locator('.page-title').innerText() === '茶姬账号管理');

  // --- A. 新建账号弹窗 · 表单阶段 ---
  await page.getByRole('button', { name: '新建账号' }).click();
  const dlg = page.locator('.el-dialog:visible', { hasText: '新建账号' }).first();
  await dlg.waitFor();
  const dlgTitle = await dlg.locator('.el-dialog__title').innerText();
  check('A1 弹窗标题为「新建账号」', dlgTitle.trim() === '新建账号', `实际: ${dlgTitle}`);
  check('A2 表单字段齐全', await dlg.getByText('备注名').count() >= 1 && await dlg.getByText('手机号').count() >= 1);
  check('A3 警告文案已前置（真实发送/60s）', (await dlg.innerText()).includes('真实发送') && (await dlg.innerText()).includes('60s 内仅可发送一次'));
  check('A4 单会话警告文案', (await dlg.innerText()).includes('单会话语义'));
  for (const btn of ['取消', '保存并发送验证码', '保存']) {
    check(`A5 按钮存在: ${btn}`, await dlg.getByRole('button', { name: btn, exact: true }).count() === 1);
  }
  await page.screenshot({ path: SHOT_DIR + '/v1_form_stage.png' });

  // --- B. 保存并发送验证码 → 同一弹窗切到输码阶段 ---
  await dlg.getByPlaceholder('如：主号-杭州').fill('验证-合并流程');
  await dlg.getByPlaceholder('11 位大陆手机号（用于接收登录验证码）').fill('13977776666');
  await dlg.getByRole('button', { name: '保存并发送验证码' }).click();

  await dlg.getByPlaceholder('请输入短信验证码').waitFor();
  const title2 = (await dlg.locator('.el-dialog__title').innerText()).trim();
  check('B1 同一弹窗标题切换为「新建账号 · 登录」', title2 === '新建账号 · 登录', `实际: ${title2}`);
  // mock 发码有 0.6s 延迟，等待成功提示出现后再检查倒计时
  await page.locator('.el-message', { hasText: '验证码已发送' }).waitFor({ timeout: 8000 });
  check('B8 成功提示「验证码已发送」', true);
  const body = await dlg.innerText();
  check('B2 显示账号与手机号', body.includes('验证-合并流程') && body.includes('13977776666'));
  check('B3 步骤条: 创建并发送验证码 / 输入验证码登录', body.includes('创建并发送验证码') && body.includes('输入验证码登录'));
  check('B4 重发按钮进入倒计时', /后可重发/.test(body), body.match(/\d+s 后可重发/)?.[0] || '');
  check('B5「登录并保存会话」按钮', await dlg.getByRole('button', { name: '登录并保存会话' }).count() === 1);
  check('B6 footer 变为「完成，返回列表」', await dlg.getByRole('button', { name: '完成，返回列表' }).count() === 1);
  check('B7 未弹出第二个「协议登录」弹窗', await page.locator('.el-dialog__title', { hasText: '协议登录' }).count() === 0);
  await page.screenshot({ path: SHOT_DIR + '/v2_code_stage.png' });

  // --- C. 输入验证码 → 登录并保存会话 ---
  await dlg.getByPlaceholder('请输入短信验证码').fill('123456');
  await dlg.getByRole('button', { name: '登录并保存会话' }).click();
  await page.locator('.el-message', { hasText: '登录成功' }).waitFor();
  check('C1 登录成功提示', true);
  await dlg.waitFor({ state: 'hidden' });
  check('C2 弹窗自动关闭', !(await dlg.isVisible().catch(() => false)));
  const table = await page.locator('.el-table').innerText();
  check('C3 列表刷新出新账号且在线', table.includes('验证-合并流程') && table.includes('在线') && table.includes('模拟茶友'));
  await page.screenshot({ path: SHOT_DIR + '/v3_after_login.png' });

  // --- D. 回归: 已有账号行「登录」→ 协议登录弹窗（两步不变） ---
  const row = page.locator('.el-table__row', { hasText: '种子账号-甲' });
  await row.getByRole('button', { name: '登录', exact: true }).click();
  const dlg2 = page.locator('.el-dialog:visible', { hasText: '协议登录' }).first();
  await dlg2.waitFor();
  check('D1 已有账号仍弹出「协议登录 · 短信验证码」弹窗', (await dlg2.locator('.el-dialog__title').innerText()).includes('协议登录'));
  check('D2 第一步仍为「确认发送验证码」', await dlg2.getByRole('button', { name: '确认发送验证码' }).count() === 1);
  await dlg2.getByRole('button', { name: '确认发送验证码' }).click();
  await dlg2.getByPlaceholder('请输入短信验证码').waitFor();
  check('D3 确认后进入输码步骤', true);
  await page.screenshot({ path: SHOT_DIR + '/v4_relogin_dialog.png' });
  await page.keyboard.press('Escape');
  await dlg2.waitFor({ state: 'hidden' }).catch(() => {});

  // --- E. 回归: 编辑弹窗保持纯表单 ---
  await page.locator('.el-table__row', { hasText: '种子账号-甲' }).getByRole('button', { name: '编辑' }).click();
  const dlgE = page.locator('.el-dialog:visible', { hasText: '编辑账号' }).first();
  await dlgE.waitFor();
  check('E1 编辑弹窗标题「编辑账号」', (await dlgE.locator('.el-dialog__title').innerText()).trim() === '编辑账号');
  const ebody = await dlgE.innerText();
  check('E2 编辑模式无发码警告、无「保存并发送验证码」按钮', !ebody.includes('真实发送') && await dlgE.getByRole('button', { name: '保存并发送验证码' }).count() === 0);
  check('E3 编辑模式含状态单选', ebody.includes('待登录'));
  await page.screenshot({ path: SHOT_DIR + '/v5_edit_dialog.png' });

  await browser.close();
  console.log(`\n结果: ${pass} PASS / ${fail} FAIL`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('ERROR:', e.message); process.exit(2); });
