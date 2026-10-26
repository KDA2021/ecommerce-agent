// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../src/App';
import { submitIntent, fetchOrder, getDashboard, confirmOrder } from '../src/api';

class Events {
  static instances: Events[] = [];
  static autoOpen = true;
  listeners = new Map<string, Set<EventListener>>();
  onerror: ((event?: Event) => void) | null = null;
  onopen: (() => void) | null = null;
  closed = false;
  constructor(public url: string) { Events.instances.push(this); if (Events.autoOpen) queueMicrotask(() => this.onopen?.()); }
  addEventListener(name: string, fn: EventListener) { this.listeners.set(name, new Set([...(this.listeners.get(name) ?? []), fn])); }
  removeEventListener(name: string, fn: EventListener) { this.listeners.get(name)?.delete(fn); }
  close() { this.closed = true; }
  emit(name: string, payload: object) { act(() => {
    const event = new MessageEvent(name, {data: JSON.stringify(payload)});
    this.listeners.get(name)?.forEach(fn => fn(event));
    if (name === 'error') this.onerror?.(event);
  }); }
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: {'Content-Type': 'application/json'} });
let intent: () => Promise<Response>;
let confirmation: () => Promise<Response>;
let auth: {auth_required: boolean; identity_mode: boolean; buyer_id: string | null};
let task: () => Promise<Response>;
beforeEach(() => {
  localStorage.clear(); localStorage.setItem('ecommerce-agent.lang','zh'); Events.instances = []; Events.autoOpen = true;
  vi.stubGlobal('EventSource', Events);
  auth = {auth_required: false, identity_mode: false, buyer_id: null};
  intent = async () => json({reply: '', task_id: 'task-1', status: 'queued'});
  confirmation = async () => json({ok: true, status: 'confirmed', token: 'token-1'});
  task = async () => json({task_id: 'task-1', state: 'running', found: true});
  vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
    if (url === '/api/auth/session') return json(auth);
    if (url === '/api/commerce/intents') return intent();
    if (url.startsWith('/api/commerce/intents/')) return task();
    if (url === '/api/commerce/orders/confirm') return confirmation();
    if (url === '/api/commerce/sessions') return json({sessions: []});
    return json({messages: []});
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
async function start() {
  render(<App />);
  await waitFor(() => expect(Events.instances.length).toBeGreaterThan(0));
  fireEvent.change(screen.getByPlaceholderText(/例如|e.g./), {target: {value: 'find shoes'}});
  fireEvent.submit(screen.getByPlaceholderText(/例如|e.g./).closest('form')!);
  await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/commerce/intents', expect.anything()));
  return Events.instances.at(-1)!;
}
const existingOrder = {order_id:'GBX-CANCEL',status:'CONFIRMED',total_amount_major:125,currency:'CNY',lines:[{product_id:'p',sku_id:'sku',quantity:1,title:'Existing item',unit_price_major:125}]};
const cancelPreview = {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',token:'cancel-token',preview:{order_id:'GBX-CANCEL',reason:'改买其他款',order:existingOrder}};
describe('cancellation approval', () => {
  it('renders a distinct bilingual cancellation decision and retries the same token after HTTP failure', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    const preview = await screen.findByTestId('order-preview');
    expect(preview.textContent).toContain('取消订单确认');
    expect(preview.textContent).toContain('GBX-CANCEL');
    expect(preview.textContent).toContain('改买其他款');
    expect(screen.getByTestId('order-confirm').textContent).toBe('确认取消订单');
    expect(screen.getByTestId('order-cancel').textContent).toBe('保留订单');
    confirmation = async () => json({detail:'retry cancellation'}, 500);
    fireEvent.click(screen.getByTestId('order-confirm'));
    await screen.findByRole('alert');
    expect((screen.getByTestId('order-confirm') as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole('button', {name:'English'}));
    expect(screen.getByTestId('order-confirm').textContent).toBe('Confirm cancellation');
    expect(screen.getByTestId('order-cancel').textContent).toBe('Keep order');
    let resolve!: (value: Response) => void;
    confirmation = () => new Promise(r => {resolve = r;});
    fireEvent.click(screen.getByTestId('order-confirm'));
    source.emit('order.preview', cancelPreview);
    expect((screen.getByTestId('order-confirm') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => resolve(json({ok:true,status:'confirmed',token:'cancel-token'})));
    expect(screen.getByTestId('order-preview').textContent).toContain('Waiting for the cancellation result');
    expect(screen.getByTestId('order-preview').textContent).not.toContain('awaiting result');
    const calls = vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/commerce/orders/confirm');
    expect(calls).toHaveLength(2);
    calls.forEach(([, options]) => expect(JSON.parse(options!.body as string)).toEqual({token:'cancel-token',action:'confirm'}));
    source.emit('tool.result', {task_id:'other',trace_id:'other',tool:'cancel_order',order:{...existingOrder,status:'CANCELLED'}});
    expect(screen.getByTestId('order-preview').textContent).toContain('Waiting for the cancellation result');
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',order:{...existingOrder,status:'CANCELLED'}});
    expect(screen.getByTestId('order-preview').textContent).toContain('Order cancelled');
    expect(screen.queryByTestId('order-confirm')).toBeNull();
  });
  it('rejects the cancellation by sending action cancel and says the existing order is retained', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    confirmation = async () => json({ok:true,status:'cancelled',token:'cancel-token'});
    fireEvent.click(await screen.findByTestId('order-cancel'));
    await waitFor(() => expect(screen.queryByTestId('order-cancel')).toBeNull());
    const call = vi.mocked(fetch).mock.calls.find(([url]) => url === '/api/commerce/orders/confirm')!;
    expect(JSON.parse(call[1]!.body as string)).toEqual({token:'cancel-token',action:'cancel'});
    expect(screen.getByTestId('order-preview').textContent).toContain('已保留订单');
    expect(screen.getByTestId('order-preview').textContent).not.toContain('不会创建订单');
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',confirmation_token:'cancel-token',order:{cancelled:true,reason:'user_rejected'}});
    expect(screen.getByTestId('order-preview').textContent).toContain('已保留订单');
  });
  it('replaces a query result from the same turn with a pending cancellation preview', async () => {
    const source = await start();
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'query_order',order:existingOrder});
    source.emit('order.preview', cancelPreview);
    expect((await screen.findByTestId('order-confirm')).textContent).toBe('确认取消订单');
  });
  it('does not let a query snapshot settle a pending cancellation', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'query_order',order:existingOrder});
    expect((await screen.findByTestId('order-confirm')).textContent).toBe('确认取消订单');
  });
  it('does not mistake a cancellation timeout for successfully cancelling the existing order', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',confirmation_token:'cancel-token',order:{cancelled:true,reason:'confirmation_timeout',order:null}});
    const preview = await screen.findByTestId('order-preview');
    expect(preview.textContent).toContain('取消确认已过期');
    expect(preview.textContent).not.toContain('订单已取消');
    expect(screen.queryByTestId('order-confirm')).toBeNull();
  });
  it('keeps a cancellation result when it arrives before the confirmation HTTP response', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    let resolve!: (value: Response) => void;
    confirmation = () => new Promise(r => {resolve = r;});
    fireEvent.click(await screen.findByTestId('order-confirm'));
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',order:{...existingOrder,status:'CANCELLED'}});
    await act(async () => resolve(json({ok:true,status:'confirmed',token:'cancel-token'})));
    expect(screen.getByTestId('order-preview').textContent).toContain('订单已取消');
    expect(screen.getByTestId('order-preview').textContent).not.toContain('正在等待后端取消结果');
  });
  it('settles a denied cancellation without presenting confirmation controls again', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',confirmation_token:'cancel-token',order:{executed:false,error:'操作未获得有效确认',error_code:'approval_expired'}});
    expect(screen.queryByTestId('order-confirm')).toBeNull();
    expect(screen.getByTestId('order-preview').textContent).toContain('操作未获得有效确认');
    expect(screen.getByTestId('order-preview').textContent).not.toContain('订单已取消');
  });
  it('opens a new cancellation after an earlier order was cancelled in the same turn', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',confirmation_token:'cancel-token',order:{...existingOrder,status:'CANCELLED'}});
    const next = {...cancelPreview,token:'token-B',preview:{...cancelPreview.preview,order_id:'GBX-B',order:{...existingOrder,order_id:'GBX-B'}}};
    source.emit('order.preview', next);
    expect(screen.getByTestId('order-preview').textContent).toContain('GBX-B');
    expect(screen.getByTestId('order-confirm').textContent).toBe('确认取消订单');
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',confirmation_token:'cancel-token',order:{cancelled:true,reason:'user_cancelled'}});
    expect(screen.getByTestId('order-confirm')).toBeTruthy();
  });
  it('ignores prior confirmation tokens for a new cancellation of the same order', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',confirmation_token:'cancel-token',order:{...existingOrder,status:'CANCELLED'}});
    source.emit('order.preview', {...cancelPreview,token:'new-token'});
    expect(screen.getByTestId('order-confirm')).toBeTruthy();
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',confirmation_token:'cancel-token',order:{...existingOrder,status:'CANCELLED'}});
    expect(screen.getByTestId('order-confirm')).toBeTruthy();
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',confirmation_token:'new-token',order:{...existingOrder,status:'CANCELLED'}});
    expect(screen.queryByTestId('order-confirm')).toBeNull();
  });
  it('settles an execution error and does not reopen a spent token after the HTTP request fails', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    let resolve!: (value: Response) => void;
    confirmation = () => new Promise(r => {resolve = r;});
    fireEvent.click(await screen.findByTestId('order-confirm'));
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',confirmation_token:'cancel-token',order_id:'GBX-CANCEL',error:'订单已发货，不能取消'});
    expect(screen.getByTestId('order-preview').textContent).toContain('取消订单失败');
    expect(screen.queryByTestId('order-confirm')).toBeNull();
    await act(async () => resolve(json({detail:'lost confirmation response'},500)));
    expect(screen.getByTestId('order-preview').textContent).toContain('订单已发货，不能取消');
    expect(screen.queryByTestId('order-confirm')).toBeNull();
    expect(screen.getByTestId('order-preview').textContent).not.toContain('等待后端取消结果');
  });
  it('settles a legacy cancellation execution error only for the matching target order', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',order_id:'another-order',error:'unrelated failure'});
    expect(screen.getByTestId('order-confirm')).toBeTruthy();
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',order_id:'GBX-CANCEL',error:'matching legacy error'});
    expect(screen.queryByTestId('order-confirm')).toBeNull();
    expect(screen.getByTestId('order-preview').textContent).toContain('matching legacy error');
  });
  it('shows a terminal cancellation failure after confirmation HTTP succeeded', async () => {
    const source = await start();
    source.emit('order.preview', cancelPreview);
    fireEvent.click(await screen.findByTestId('order-confirm'));
    await waitFor(() => expect(screen.getByTestId('order-preview').textContent).toContain('等待后端取消结果'));
    source.emit('tool.result', {task_id:'task-1',trace_id:'trace-1',tool:'cancel_order',confirmation_token:'cancel-token',order_id:'GBX-CANCEL',error:'执行失败，请查询订单状态'});
    expect(screen.getByTestId('order-preview').textContent).toContain('取消订单失败');
    expect(screen.getByTestId('order-preview').textContent).toContain('执行失败，请查询订单状态');
    expect(screen.queryByTestId('order-confirm')).toBeNull();
    expect(screen.getByTestId('order-preview').textContent).not.toContain('等待后端取消结果');
  });
});
describe('HTTP reliability', () => {
  for (const status of [401, 429, 500]) it(`rejects intent HTTP ${status} with server detail`, async () => {
    vi.stubGlobal('fetch', async () => json({detail: `failure-${status}`}, status));
    await expect(submitIntent({shopping_session_id:'s',buyer_id:'b',query:'q'})).rejects.toThrow(`failure-${status}`);
  });
  it('retains queue metadata', async () => {
    expect(await submitIntent({shopping_session_id:'s',buyer_id:'b',query:'q'})).toMatchObject({task_id:'task-1',status:'queued',reply:''});
  });
  it('surfaces HTTP details for other wrappers', async () => {
    vi.stubGlobal('fetch', async () => json({detail:'access denied'}, 403));
    await expect(fetchOrder('o')).rejects.toThrow('access denied');
    await expect(getDashboard()).rejects.toThrow('access denied');
    await expect(confirmOrder('t','confirm')).rejects.toThrow('access denied');
  });
});
describe('runtime commerce flow', () => {
  it('keeps an open SSE connection usable after an application error', async () => {
    const source = await start();
    await act(async () => {});
    source.emit('error', {task_id:'task-1',trace_id:'trace-1',message:'worker failed'});
    expect(screen.getByTestId('error-banner').textContent).toContain('worker failed');
    fireEvent.click(screen.getByTestId('retry-button'));
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/commerce/intents')).toHaveLength(2));
    expect(source.closed).toBe(false);
  });
  it('keeps queued requests busy, rejects unrelated events, then completes the matching turn', async () => {
    const source = await start();
    await act(async () => {});
    expect(screen.getByRole('button', {name:/发送中|Sending/})).toBeTruthy();
    source.emit('final.result', {task_id:'other',trace_id:'other',reply:'WRONG REPLY'});
    expect(screen.queryByText('WRONG REPLY')).toBeNull();
    source.emit('task.started', {task_id:'task-1',trace_id:'trace-1'});
    source.emit('final.result', {task_id:'task-1',trace_id:'trace-1',reply:'matching reply'});
    await screen.findByText('matching reply');
    expect(screen.queryByRole('button', {name:/发送中|Sending/})).toBeNull();
    source.emit('token.delta', {task_id:'task-1',trace_id:'trace-1',delta:'late token'});
    expect(screen.queryByText(/late token/)).toBeNull();
  });
  it('retains a final event received before the queued POST responds', async () => {
    let resolve!: (value: Response) => void;
    intent = () => new Promise(r => { resolve = r; });
    const source = await start();
    source.emit('final.result', {task_id:'task-1',trace_id:'trace-1',reply:'fast reply'});
    await act(async () => resolve(json({task_id:'task-1',status:'queued',reply:''})));
    await screen.findByText('fast reply');
    expect(screen.queryByRole('button', {name:/发送中|Sending/})).toBeNull();
  });
  it('keeps confirmation retryable after HTTP failure and settles only after server success', async () => {
    const source = await start();
    source.emit('task.started', {task_id:'task-1',trace_id:'trace-1'});
    source.emit('order.preview', {task_id:'task-1',trace_id:'trace-1',tool:'create_order',token:'token-1',preview:{items:[{product_id:'p',sku_id:'sku',quantity:1}],shipping_address:{city:'Shanghai'}}});
    confirmation = async () => json({detail:'temporarily unavailable'}, 500);
    fireEvent.click(await screen.findByTestId('order-confirm'));
    await screen.findByRole('alert', {name: ''});
    expect(screen.getByRole('alert').textContent).toContain('temporarily unavailable');
    expect((screen.getByTestId('order-confirm') as HTMLButtonElement).disabled).toBe(false);
    let resolve!: (value: Response) => void;
    confirmation = () => new Promise(r => {resolve = r;});
    fireEvent.click(screen.getByTestId('order-confirm'));
    expect((screen.getByTestId('order-confirm') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => resolve(json({ok:true,status:'confirmed',token:'token-1'})));
    expect(screen.queryByTestId('order-confirm')).toBeNull();
  });
  it('does not display saved history or open SSE before authentication', async () => {
    auth = {auth_required:true,identity_mode:true,buyer_id:null};
    localStorage.setItem('ecommerce-agent.sessions.v1',JSON.stringify([{id:'old',title:'private',messages:[{id:'m',role:'assistant',content:'private message'}]}]));
    render(<App />);
    await screen.findByTestId('access-key');
    expect(screen.queryByText('private message')).toBeNull();
    expect(Events.instances).toHaveLength(0);
  });
  it('finishes a disconnected queue from durable task polling', async () => {
    const source = await start();
    await act(async () => {});
    vi.useFakeTimers();
    task = async () => json({task_id:'task-1',state:'done',found:true,final_text:'recovered reply'});
    act(() => source.onerror?.());
    // The initial timer was scheduled before fake timers; reconnect is independent.
    vi.useRealTimers();
    await screen.findByText('recovered reply', {}, {timeout:3000});
    expect(screen.queryByRole('button', {name:/发送中|Sending/})).toBeNull();
    expect(screen.queryByTestId('error-banner')).toBeNull();
  });
  it('releases busy state on task failure and ignores late events', async () => {
    const source = await start();
    source.emit('error', {task_id:'task-1',trace_id:'trace-1',message:'worker failed'});
    expect(screen.getByTestId('error-banner').textContent).toContain('worker failed');
    expect(screen.queryByRole('button', {name:/发送中|Sending/})).toBeNull();
    source.emit('final.result', {task_id:'task-1',trace_id:'trace-1',reply:'stale reply'});
    expect(screen.queryByText('stale reply')).toBeNull();
  });
  it('expires an unknown confirmation token instead of offering a retry', async () => {
    const source = await start();
    source.emit('order.preview', {task_id:'task-1',trace_id:'trace-1',tool:'create_order',token:'token-1',preview:{items:[{product_id:'p',sku_id:'sku',quantity:1}],shipping_address:{city:'Shanghai'}}});
    confirmation = async () => json({ok:false,status:'unknown',token:'token-1'});
    fireEvent.click(await screen.findByTestId('order-cancel'));
    await waitFor(() => expect(screen.queryByTestId('order-cancel')).toBeNull());
    expect(screen.getByTestId('order-preview').textContent).toMatch(/过期|expired/);
  });
  it('correlates direct events before POST completes using client_request_id', async () => {
    let resolve!: (response: Response) => void;
    intent = () => new Promise(r => {resolve = r;});
    const source = await start();
    const call = vi.mocked(fetch).mock.calls.find(([url]) => url === '/api/commerce/intents')!;
    const request = JSON.parse(call[1]!.body as string);
    source.emit('token.delta', {client_request_id:'another-request',trace_id:'wrong',delta:'WRONG'});
    expect(screen.queryByText('WRONG')).toBeNull();
    source.emit('order.preview', {client_request_id:request.client_request_id,trace_id:'direct-trace',tool:'create_order',token:'token-1',preview:{items:[{product_id:'p',sku_id:'sku',quantity:1}],shipping_address:{city:'Shanghai'}}});
    expect(screen.getByTestId('order-confirm')).toBeTruthy();
    source.emit('final.result', {client_request_id:request.client_request_id,trace_id:'direct-trace',reply:'direct reply'});
    await act(async () => resolve(json({reply:'direct reply',trace_id:'direct-trace'})));
    expect(screen.getByText('direct reply')).toBeTruthy();
  });
  it('keeps a submitting preview locked when SSE replays the same token', async () => {
    const source = await start();
    const preview = {task_id:'task-1',trace_id:'trace-1',tool:'create_order',token:'token-1',preview:{items:[{product_id:'p',sku_id:'sku',quantity:1}],shipping_address:{city:'Shanghai'}}};
    source.emit('order.preview', preview);
    confirmation = () => new Promise(() => {});
    fireEvent.click(await screen.findByTestId('order-confirm'));
    source.emit('order.preview', preview);
    expect((screen.getByTestId('order-confirm') as HTMLButtonElement).disabled).toBe(true);
  });
  it('keeps a locally timed-out task busy and accepts its late final reply', async () => {
    render(<App />);
    await waitFor(() => expect(Events.instances.length).toBeGreaterThan(0));
    vi.useFakeTimers();
    const input = screen.getByPlaceholderText(/例如|e.g./);
    fireEvent.change(input, {target:{value:'first'}});
    fireEvent.submit(input.closest('form')!);
    await act(async () => {});
    task = () => new Promise(() => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(180_001); });
    expect(screen.getByTestId('error-banner').textContent).toMatch(/仍在处理|Still processing/);
    expect(screen.getByRole('button', {name:/发送中|Sending/})).toBeTruthy();
    fireEvent.change(input, {target:{value:'second'}});
    fireEvent.submit(input.closest('form')!);
    await act(async () => {});
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/commerce/intents')).toHaveLength(1);
    Events.instances.at(-1)!.emit('final.result', {task_id:'task-1',trace_id:'trace-1',reply:'late first reply'});
    expect(screen.getByText('late first reply')).toBeTruthy();
    expect(screen.queryByTestId('error-banner')).toBeNull();
    expect(screen.queryByRole('button', {name:/发送中|Sending/})).toBeNull();
    fireEvent.submit(input.closest('form')!);
    await act(async () => {});
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/commerce/intents')).toHaveLength(2);
  });
  it('keeps a direct request without a task ID active after local timeout', async () => {
    let resolve!: (response: Response) => void;
    intent = () => new Promise(r => {resolve = r;});
    render(<App />);
    await waitFor(() => expect(Events.instances.length).toBeGreaterThan(0));
    vi.useFakeTimers();
    const input = screen.getByPlaceholderText(/例如|e.g./);
    fireEvent.change(input, {target:{value:'direct first'}});
    fireEvent.submit(input.closest('form')!);
    await act(async () => {});
    const call = vi.mocked(fetch).mock.calls.find(([url]) => url === '/api/commerce/intents')!;
    const request = JSON.parse(call[1]!.body as string);
    await act(async () => { await vi.advanceTimersByTimeAsync(180_001); });
    expect(screen.getByRole('button', {name:/发送中|Sending/})).toBeTruthy();
    fireEvent.submit(input.closest('form')!);
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/commerce/intents')).toHaveLength(1);
    Events.instances.at(-1)!.emit('final.result', {client_request_id:request.client_request_id,trace_id:'direct-late',reply:'late direct reply'});
    await act(async () => resolve(json({reply:'late direct reply',trace_id:'direct-late'})));
    expect(screen.getByText('late direct reply')).toBeTruthy();
    expect(screen.queryByRole('button', {name:/发送中|Sending/})).toBeNull();
  });
  it('continues polling past the local deadline until backend completion', async () => {
    render(<App />);
    await waitFor(() => expect(Events.instances.length).toBeGreaterThan(0));
    vi.useFakeTimers();
    const input = screen.getByPlaceholderText(/例如|e.g./);
    fireEvent.change(input, {target:{value:'long running task'}});
    fireEvent.submit(input.closest('form')!);
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(180_001); });
    expect(screen.getByRole('button', {name:/发送中|Sending/})).toBeTruthy();
    const pollCount = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).startsWith('/api/commerce/intents/')).length;
    expect(pollCount).toBeGreaterThan(1);
    task = async () => json({task_id:'task-1',state:'done',found:true,final_text:'eventual durable reply'});
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(screen.getByText('eventual durable reply')).toBeTruthy();
    expect(screen.queryByRole('button', {name:/发送中|Sending/})).toBeNull();
    expect(screen.queryByTestId('error-banner')).toBeNull();
    const settledCount = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).startsWith('/api/commerce/intents/')).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).startsWith('/api/commerce/intents/'))).toHaveLength(settledCount);
  });
  it('logs in with a cookie session, locks buyer identity, then closes SSE and hides history on logout', async () => {
    auth = {auth_required:true,identity_mode:true,buyer_id:null};
    render(<App />);
    fireEvent.change(await screen.findByTestId('access-key'), {target:{value:'test-access-key'}});
    auth = {auth_required:true,identity_mode:true,buyer_id:'alice'};
    fireEvent.click(screen.getByRole('button', {name:/Sign in/}));
    await waitFor(() => expect(Events.instances.length).toBeGreaterThan(0));
    expect((screen.getByDisplayValue('alice') as HTMLInputElement).readOnly).toBe(true);
    const source = Events.instances.at(-1)!;
    fireEvent.change(screen.getByPlaceholderText(/例如|e.g./), {target:{value:'Alice private query'}});
    fireEvent.submit(screen.getByPlaceholderText(/例如|e.g./).closest('form')!);
    await act(async () => {});
    source.emit('final.result', {task_id:'task-1',trace_id:'trace-1',reply:'Alice private reply'});
    auth = {auth_required:true,identity_mode:true,buyer_id:null};
    fireEvent.click(screen.getByRole('button', {name:/Sign out/}));
    await screen.findByTestId('access-key');
    expect(source.closed).toBe(true);
    expect(screen.queryByText('Alice private reply')).toBeNull();
    expect(localStorage.getItem('ecommerce-agent.sessions.v1:buyer:alice')).toContain('Alice private reply');
    expect(JSON.stringify(localStorage)).not.toContain('test-access-key');
    auth = {auth_required:true,identity_mode:true,buyer_id:'bob'};
    fireEvent.change(screen.getByTestId('access-key'), {target:{value:'bob-key'}});
    fireEvent.click(screen.getByRole('button', {name:/Sign in/}));
    await screen.findByDisplayValue('bob');
    expect(screen.queryByText('Alice private reply')).toBeNull();
    source.emit('final.result', {task_id:'task-1',trace_id:'trace-1',reply:'late Alice reply'});
    expect(screen.queryByText('late Alice reply')).toBeNull();
  });
  it('waits for the event connection before accepting a direct request', async () => {
    Events.autoOpen = false;
    render(<App />);
    await waitFor(() => expect(Events.instances.length).toBeGreaterThan(0));
    const input = screen.getByPlaceholderText(/例如|e.g./);
    fireEvent.change(input, {target:{value:'needs live preview'}});
    fireEvent.submit(input.closest('form')!);
    expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/commerce/intents')).toBe(false);
    act(() => Events.instances.at(-1)!.onopen?.());
    fireEvent.submit(input.closest('form')!);
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/commerce/intents')).toBe(true));
  });
  it('lets demo buyers finish editing before switching their isolated session', async () => {
    render(<App />);
    const buyer = await screen.findByDisplayValue('buyer-1');
    buyer.focus();
    fireEvent.change(buyer, {target:{value:'buyer-12'}});
    expect(document.activeElement).toBe(buyer);
    fireEvent.blur(buyer);
    expect(screen.getByDisplayValue('buyer-12')).toBeTruthy();
  });
  it('locks session navigation and deletion until the active request finishes', async () => {
    localStorage.setItem('ecommerce-agent.sessions.v1:demo:buyer-1', JSON.stringify([
      {id:'current-session',title:'Current saved session',updatedAt:2000,messages:[{id:'u-current',role:'user',content:'Current history'}]},
      {id:'older-session',title:'Older saved session',updatedAt:1000,messages:[{id:'u-old',role:'user',content:'Old history'}]},
    ]));
    const source = await start();
    const older = screen.getByRole('button', {name:/Older saved session/});
    const newSession = screen.getByTestId('history-new');
    const currentDelete = screen.getAllByTestId('history-delete').find(button => button.closest('li')?.textContent?.includes('Current saved session'))!;
    expect((older as HTMLButtonElement).disabled).toBe(true);
    expect((newSession as HTMLButtonElement).disabled).toBe(true);
    expect((currentDelete as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(older);
    fireEvent.click(newSession);
    fireEvent.click(currentDelete);
    expect(screen.getByDisplayValue('current-session')).toBeTruthy();
    const input = screen.getByPlaceholderText(/例如|e.g./);
    fireEvent.change(input, {target:{value:'should remain blocked'}});
    fireEvent.submit(input.closest('form')!);
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/commerce/intents')).toHaveLength(1);
    source.emit('final.result', {task_id:'task-1',trace_id:'trace-1',reply:'completed before switching'});
    expect(screen.getByText('completed before switching')).toBeTruthy();
    expect((screen.getByTestId('history-new') as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole('button', {name:/Older saved session/}) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole('button', {name:/Older saved session/}));
    expect(screen.getByDisplayValue('older-session')).toBeTruthy();
    expect(screen.getByText('Old history')).toBeTruthy();
    fireEvent.click(screen.getByTestId('history-new'));
    expect(screen.queryByDisplayValue('older-session')).toBeNull();
  });
});
