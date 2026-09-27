// 布局检查：两阶段弹窗无溢出/错位
const { chromium } = require('playwright-core');

(async () => {
  const browser = await chromium.launch({
    executablePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto('http://127.0.0.1:8800/');
  await page.getByPlaceholder('用户名').fill('admin');
  await page.getByPlaceholder('密码').fill('x');
  await page.getByRole('button', { name: /登\s*录/ }).click();
  await page.waitForTimeout(1500);
  await page.goto('http://127.0.0.1:8800/accounts');
  await page.waitForSelector('.el-table');

  const overflowCheck = () =>
    page.evaluate(() => {
      const dlg = [...document.querySelectorAll('.el-dialog')].find(d => d.offsetParent !== null);
      if (!dlg) return { err: 'no visible dialog' };
      const dr = dlg.getBoundingClientRect();
      const bad = [];
      for (const el of dlg.querySelectorAll('*')) {
        if (!el.offsetParent && el.tagName !== 'svg') continue;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (r.right > dr.right + 2 || r.left < dr.left - 2) {
          bad.push(`${el.tagName}.${el.className && el.className.toString().slice(0, 40)} [${Math.round(r.left)},${Math.round(r.right)}] vs dlg [${Math.round(dr.left)},${Math.round(dr.right)}]`);
        }
      }
      return {
        dlgBox: { x: Math.round(dr.x), y: Math.round(dr.y), w: Math.round(dr.width), h: Math.round(dr.height) },
        viewport: { w: innerWidth, h: innerHeight },
        dlgInViewport: dr.left >= 0 && dr.right <= innerWidth && dr.top >= 0 && dr.bottom <= innerHeight,
        scrollOverflow: dlg.scrollWidth > dlg.clientWidth + 2,
        overflowing: bad.slice(0, 5),
      };
    });

  let report = {};

  // 表单阶段
  await page.getByRole('button', { name: '新建账号' }).click();
  await page.locator('.el-dialog:visible .el-form').waitFor();
  report.formStage = await overflowCheck();

  // 切到输码阶段
  const dlg = page.locator('.el-dialog:visible').first();
  await dlg.getByPlaceholder('如：主号-杭州').fill('布局检查');
  await dlg.getByPlaceholder('11 位大陆手机号（用于接收登录验证码）').fill('13666667777');
  await dlg.getByRole('button', { name: '保存并发送验证码' }).click();
  await page.locator('.el-message', { hasText: '验证码已发送' }).waitFor({ timeout: 8000 });
  report.codeStage = await overflowCheck();

  console.log(JSON.stringify(report, null, 2));
  await browser.close();
})().catch((e) => { console.error('ERROR:', e.message); process.exit(2); });
