/* Run against a review deployment with synthetic sessions, never log bearer cookies. */
const path = require('node:path');
const args = process.argv.slice(2);
const option = (name, fallback) => args.includes(name) ? args[args.indexOf(name)+1] : fallback;
const system = option('--system', 'g85');
if (!['g8','g85'].includes(system)) throw new Error('Use --system g8 or g85');
const folder = system === 'g8' ? 'G8-Source-code-LLM-tu-van-hanh-chinh-cong' : 'G8.5-He-thong-LLM-reusable';
const modulePath = process.env.PLAYWRIGHT_TEST_MODULE || path.resolve(__dirname, '..', folder, 'frontend/node_modules/@playwright/test');
const {chromium, expect} = require(modulePath);
const origin = option('--origin', `http://localhost:${system === 'g8' ? 13008 : 13085}`);
const query = option('--query', '');
const expected = option('--expect', '').split('|').filter(Boolean);
process.env.NO_PROXY = [process.env.NO_PROXY,new URL(origin).hostname].filter(Boolean).join(',');
process.env.no_proxy = process.env.NO_PROXY;

async function ready(page) {
  await expect(page.getByRole('textbox',{name:'Câu hỏi của bạn',exact:true})).toBeEnabled({timeout:30000});
  await expect(page.getByRole('button',{name:'Đăng nhập',exact:true})).toHaveCount(0);
  return page.evaluate(()=>sessionStorage.getItem('hcc-account-id'));
}

(async () => {
  const browser = await chromium.launch({headless:true,args:['--no-proxy-server']});
  const errors=[];
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    page.on('pageerror', e=>errors.push(e.name));
    const base=origin+'/api/v1';
    expect((await context.request.get(base+'/conversations')).status()).toBe(401);
    await page.goto(origin);
    const identity = await ready(page);
    expect(Boolean(identity)).toBe(true);
    const headers={Origin:origin,'X-Account-ID':identity};
    expect((await context.request.post(base+'/auth/browser-session',{headers})).status()).toBe(200);
    const cookies=await context.cookies();
    expect(cookies.some(c=>c.httpOnly && c.sameSite==='Lax')).toBe(true);
    let thread;
    if (query) {
      await page.getByRole('textbox',{name:'Câu hỏi của bạn',exact:true}).fill(query);
      await page.getByRole('button',{name:'Gửi câu hỏi',exact:true}).click();
      await expect(page.locator('.message.assistant details.grounding').first()).toBeVisible({timeout:180000});
      for (const text of expected) await expect(page.locator('.message.assistant')).toContainText(text);
      const threads=await (await context.request.get(base+'/conversations',{headers})).json();
      thread=threads[0];
    } else {
      const response=await context.request.post(base+'/conversations',{headers,data:{title:'Synthetic review smoke'}});
      expect(response.status()).toBe(201);
      thread=await response.json();
    }
    await page.reload();
    expect(await ready(page)).toBe(identity);
    await expect(page.getByRole('button',{name:thread.title,exact:true})).toBeVisible();
    const tab=await context.newPage();
    await tab.goto(origin);
    expect(await ready(tab)).toBe(identity);
    await tab.close();
    const persisted=await context.cookies();
    await context.close();
    const reopened=await browser.newContext({storageState:{cookies:persisted,origins:[]}});
    const restored=await reopened.newPage();
    await restored.goto(origin);
    expect(await ready(restored)).toBe(identity);
    await expect(restored.getByRole('button',{name:thread.title,exact:true})).toBeVisible();
    expect(errors).toEqual([]);
    console.log(JSON.stringify({system,origin,visitor:true,authenticated_renewal:true,
      history_reload:true,new_tab:true,cookie_only_reopen:true,real_chat_tested:Boolean(query),js_errors:errors}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1;});
