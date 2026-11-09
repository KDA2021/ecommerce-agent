import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {test,expect}=require(process.env.QA_PLAYWRIGHT_TEST || 'playwright/test');
const CONTROL={'X-QA-Control':'qa-local-control-20261105'};
const ALICE='qa-alice-credential-20261105';
const BOB='qa-bob-credential-20261105';
const input=page=>page.getByPlaceholder('例如：帮我找旅行三件套');

async function state(page){return (await page.request.get('/api/__qa/state',{headers:CONTROL})).json();}
async function send(page,text){
  await input(page).fill(text);
  await page.getByRole('button',{name:'发送',exact:true}).click();
  await expect(page.getByTestId('order-confirm')).toBeVisible();
}
async function create(page){
  await send(page,'qa:create');
  await page.getByTestId('order-confirm').click();
  await expect.poll(async()=> (await state(page)).orders.length).toBe(1);
  await expect(page.getByRole('button',{name:'发送中…',exact:true})).toHaveCount(0);
  const s=await state(page);
  await expect(page.getByTestId('order-card').filter({hasText:s.orders[0].order_id}).first()).toContainText('已确认');
  return s.orders[0];
}

test.beforeEach(async({page})=>{
  await page.request.post('http://127.0.0.1:8009/__qa/reset',{headers:CONTROL});
  await page.addInitScript(()=>localStorage.setItem('ecommerce-agent.lang','zh'));
  // No route.fulfill, no EventSource replacement: all API and SSE use the server.
  await page.goto('/');
  await page.getByTestId('access-key').fill(ALICE);
  await page.getByRole('button',{name:/Sign in/}).click();
  await expect.poll(async()=> (await state(page)).subscribers).toBeGreaterThan(0);
});

test('LIVE-01 真实确认→下单→查询→再次确认取消，金额及库存一致',async({page})=>{
  const order=await create(page);
  expect((await state(page)).stock).toBe(49);
  const response=await page.request.get('/api/commerce/orders/'+order.order_id);
  expect(response.status()).toBe(200); expect((await response.json()).status).toBe('CONFIRMED');
  const expected=order.lines.reduce((sum,line)=>sum+Math.round(line.unit_price_major*100)*line.quantity,0);
  expect(Math.round(order.total_amount_major*100)).toBe(expected);
  await send(page,'qa:cancel:'+order.order_id);
  await expect(page.getByTestId('order-confirm')).toHaveText('确认取消订单');
  await page.getByTestId('order-confirm').click();
  await expect.poll(async()=> (await state(page)).orders[0].status).toBe('CANCELLED');
  await expect(page.getByTestId('order-preview')).toContainText('订单已取消');
  expect((await state(page)).stock).toBe(50);
});

test('LIVE-02 拒绝真实预览后无订单、无库存变化',async({page})=>{
  await send(page,'qa:create'); await page.getByTestId('order-cancel').click();
  await expect(page.getByRole('button',{name:'发送中…',exact:true})).toHaveCount(0);
  expect((await state(page)).orders).toEqual([]); expect((await state(page)).stock).toBe(50);
});

test('LIVE-03 同会话再次确认返回同一订单，不再次扣库存',async({page})=>{
  const original=await create(page);
  await send(page,'qa:create'); await page.getByTestId('order-confirm').click();
  await expect(page.getByRole('button',{name:'发送中…',exact:true})).toHaveCount(0);
  const s=await state(page);
  expect(s.orders.map(o=>o.order_id)).toEqual([original.order_id]); expect(s.stock).toBe(49);
});

test('LIVE-04 真实写入故障回滚后可恢复，最终只有一个订单',async({page})=>{
  await page.request.post('/api/__qa/fault',{headers:CONTROL});
  await send(page,'qa:create'); await page.getByTestId('order-confirm').click();
  await expect(page.getByTestId('error-banner')).toBeVisible();
  await expect(page.getByRole('button',{name:'发送中…',exact:true})).toHaveCount(0);
  expect((await state(page)).orders).toEqual([]); expect((await state(page)).stock).toBe(50);
  await create(page); expect((await state(page)).stock).toBe(49);
});

test('LIVE-05 真实服务拒绝其他用户查询/取消，登出清理可见状态',async({page})=>{
  const order=await create(page);
  const other={headers:{'X-API-Key':BOB}};
  expect((await page.request.get('/api/commerce/orders/'+order.order_id,other)).status()).toBe(404);
  expect((await page.request.post('/api/commerce/orders/'+order.order_id+'/cancel',{...other,data:{reason:'foreign'}})).status()).toBe(404);
  expect((await state(page)).orders[0].status).toBe('CONFIRMED');
  await page.getByRole('button',{name:/Sign out/}).click();
  await expect(page.getByTestId('access-key')).toBeVisible();
  await expect(page.getByTestId('order-card')).toHaveCount(0);
  expect((await page.request.get('/api/commerce/orders/'+order.order_id)).status()).toBe(401);
});
