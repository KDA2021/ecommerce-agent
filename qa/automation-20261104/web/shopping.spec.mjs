import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { test, expect } = require(process.env.QA_PLAYWRIGHT_TEST || 'playwright/test');

// Actual React DOM and HTTP requests; controllable API/SSE dependencies.
async function arrange(page, {authenticated = false} = {}) {
  const state = {auth: authenticated ? {auth_required:true,identity_mode:true,buyer_id:null} : {auth_required:false,identity_mode:false,buyer_id:null}, confirmations:[], intents:[], mode:'ok'};
  const unexpected = [];
  await page.addInitScript(() => {
    localStorage.setItem('ecommerce-agent.lang', 'zh');
    window.__qaEvents = [];
    class Events extends EventTarget {
      constructor(url) { super(); this.url=url; this.closed=false; window.__qaEvents.push(this); setTimeout(() => this.onopen?.(new Event('open')), 0); }
      close() { this.closed=true; }
    }
    window.EventSource = Events;
    window.__qaEmit = (name, payload) => window.__qaEvents.filter(source => !source.closed).forEach(source => source.dispatchEvent(new MessageEvent(name, {data:JSON.stringify(payload)})));
  });
  await page.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url());
    if (url.hostname !== '127.0.0.1') { unexpected.push(request.url()); return route.abort(); }
    if (!url.pathname.startsWith('/api/')) return route.continue();
    const json = (body, status=200) => route.fulfill({status, json:body});
    if (url.pathname === '/api/auth/session') {
      if (request.method() === 'POST') state.auth = {auth_required:true,identity_mode:true,buyer_id:'alice'};
      if (request.method() === 'DELETE') state.auth = {auth_required:true,identity_mode:true,buyer_id:null};
      return json(state.auth);
    }
    if (url.pathname === '/api/commerce/intents') { state.intents.push(request.postDataJSON()); return json({reply:'',task_id:'qa-task',status:'queued'}); }
    if (url.pathname.startsWith('/api/commerce/intents/')) return json({task_id:'qa-task',state:'running',found:true});
    if (url.pathname === '/api/commerce/orders/confirm') {
      const body = request.postDataJSON(); state.confirmations.push(body);
      if (state.mode === 'hold') await new Promise(resolve => {state.release=resolve;});
      if (state.mode === 'error') return json({detail:'QA temporary outage'}, 500);
      if (state.mode === 'expired') return json({token:body.token,status:'unknown',ok:false});
      return json({token:body.token,status:body.action==='confirm'?'confirmed':'cancelled',ok:true});
    }
    if (url.pathname === '/api/commerce/sessions') return json({sessions:[]});
    if (/\/sessions\/.*\/history$/.test(url.pathname)) return json({messages:[],source:'empty'});
    unexpected.push(request.method()+' '+url.pathname);
    return json({detail:'Unconfigured test dependency'}, 501);
  });
  await page.goto('/');
  return {state, unexpected};
}

class ShoppingPage {
  constructor(page) { this.page=page; this.input=page.getByPlaceholder('例如：帮我找旅行三件套'); this.confirm=page.getByTestId('order-confirm'); }
  async start(state) {
    await expect.poll(() => this.page.evaluate(() => window.__qaEvents.filter(source => !source.closed).length)).toBeGreaterThan(0);
    await this.input.fill('寻找旅行套装并准备订单');
    await this.page.getByRole('button', {name:'发送',exact:true}).click();
    await expect.poll(() => state.intents.length).toBe(1);
    // Wait for POST result handling, not only the route interceptor.
    await expect(this.page.getByRole('button', {name:'发送中…',exact:true})).toBeVisible();
    await this.preview();
    await expect(this.confirm).toBeVisible();
  }
  async emit(name, payload) { await this.page.evaluate(([n,p])=>window.__qaEmit(n,p), [name,payload]); }
  async preview() { await this.emit('order.preview', {task_id:'qa-task',trace_id:'qa-trace',tool:'create_order',token:'qa-token',preview:{items:[{product_id:'p',sku_id:'sku',quantity:1}],shipping_address:{city:'Guangzhou'}}}); }
}

test('WEB-01 确认只进入等待执行，不能冒充已经下单', async ({page}) => {
  const {state,unexpected}=await arrange(page); const app=new ShoppingPage(page); await app.start(state);
  await app.confirm.click();
  await expect(page.getByTestId('order-preview')).toContainText('已确认，正在等待后端下单结果');
  await expect(app.confirm).toHaveCount(0);
  expect(state.confirmations).toEqual([{token:'qa-token',action:'confirm'}]); expect(unexpected).toEqual([]);
});
test('WEB-02 HTTP 500 保留 Token 并允许重试', async ({page}) => {
  const {state,unexpected}=await arrange(page); const app=new ShoppingPage(page); await app.start(state);
  state.mode='error'; await app.confirm.click();
  await expect(page.getByTestId('order-preview').getByRole('alert')).toContainText('QA temporary outage');
  await expect(app.confirm).toBeEnabled(); state.mode='ok'; await app.confirm.click();
  await expect(app.confirm).toHaveCount(0);
  expect(state.confirmations).toEqual([{token:'qa-token',action:'confirm'},{token:'qa-token',action:'confirm'}]); expect(unexpected).toEqual([]);
});
test('WEB-03 提交期间禁用确认，事件重放不解锁', async ({page}) => {
  const {state,unexpected}=await arrange(page); const app=new ShoppingPage(page); await app.start(state); state.mode='hold';
  await app.confirm.click(); await expect(app.confirm).toBeDisabled();
  await expect.poll(() => state.confirmations.length).toBe(1);
  await app.preview(); await expect(app.confirm).toBeDisabled();
  // Real second DOM click while disabled must not send another HTTP request.
  await app.confirm.evaluate(button => button.click()); expect(state.confirmations).toHaveLength(1);
  state.mode='ok'; state.release(); await expect(app.confirm).toHaveCount(0); expect(unexpected).toEqual([]);
});
test('WEB-04 未知 Token 过期退出，不能无限重试', async ({page}) => {
  const {state,unexpected}=await arrange(page); const app=new ShoppingPage(page); await app.start(state); state.mode='expired';
  await app.confirm.click(); await expect(page.getByTestId('order-preview')).toContainText('确认已过期');
  await expect(app.confirm).toHaveCount(0); expect(state.confirmations).toHaveLength(1); expect(unexpected).toEqual([]);
});
test('WEB-05 取消预览提交 cancel，不能提交 confirm', async ({page}) => {
  const {state,unexpected}=await arrange(page); const app=new ShoppingPage(page); await app.start(state);
  await page.getByTestId('order-cancel').click(); await expect(app.confirm).toHaveCount(0);
  expect(state.confirmations).toEqual([{token:'qa-token',action:'cancel'}]); expect(unexpected).toEqual([]);
});
test('WEB-06 旧任务完成事件不能结束当前任务', async ({page}) => {
  const {state,unexpected}=await arrange(page); const app=new ShoppingPage(page); await app.start(state);
  await app.emit('final.result',{task_id:'old-task',trace_id:'old-trace',reply:'STALE QA REPLY'});
  await expect(page.getByText('STALE QA REPLY',{exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'发送中…',exact:true})).toBeVisible();
  await app.emit('final.result',{task_id:'qa-task',trace_id:'qa-trace',reply:'QA current task complete'});
  await expect(page.getByText('QA current task complete',{exact:true})).toBeVisible(); expect(unexpected).toEqual([]);
});
test('WEB-07 任务失败解除忙碌，迟到成功不能覆盖', async ({page}) => {
  const {state,unexpected}=await arrange(page); const app=new ShoppingPage(page); await app.start(state);
  await app.emit('error',{task_id:'qa-task',trace_id:'qa-trace',message:'QA worker failed'});
  await expect(page.getByTestId('error-banner')).toContainText('QA worker failed');
  await expect(page.getByRole('button',{name:'发送中…',exact:true})).toHaveCount(0);
  await app.emit('final.result',{task_id:'qa-task',trace_id:'qa-trace',reply:'LATE QA SUCCESS'});
  await expect(page.getByText('LATE QA SUCCESS',{exact:true})).toHaveCount(0); expect(unexpected).toEqual([]);
});
test('WEB-08 登录前不连接事件，退出后清理可见会话', async ({page}) => {
  const {state,unexpected}=await arrange(page,{authenticated:true}); const app=new ShoppingPage(page);
  await expect(page.getByTestId('access-key')).toBeVisible();
  expect(await page.evaluate(()=>window.__qaEvents.length)).toBe(0);
  await page.getByTestId('access-key').fill('qa-browser-test-key');
  await page.getByRole('button',{name:/Sign in/}).click(); await app.start(state);
  await app.emit('final.result',{task_id:'qa-task',trace_id:'qa-trace',reply:'QA private reply'});
  await expect(page.getByText('QA private reply',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:/Sign out/}).click();
  await expect(page.getByTestId('access-key')).toBeVisible();
  await expect(page.getByText('QA private reply',{exact:true})).toHaveCount(0);
  expect(await page.evaluate(()=>window.__qaEvents.every(source=>source.closed))).toBe(true);
  expect(await page.evaluate(()=>JSON.stringify(localStorage))).not.toContain('qa-browser-test-key'); expect(unexpected).toEqual([]);
});
