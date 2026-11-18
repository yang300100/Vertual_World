/* 哥哥，执行实际通路界面脚本，所有接口和存储都在隔离模拟中。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../../world_engine/web/local-routes.js'), 'utf8');

function environment() {
  class Element {
    constructor(tag) {
      Object.assign(this, {tagName: tag, children: [], handlers: {}, attributes: {}, value: '', textContent: '', disabled: false, hidden: false});
    }
    get options() { return this.children; }
    append(...nodes) {
      this.children.push(...nodes);
      if (this.tagName === 'select' && this.children.length === nodes.length) this.value = nodes[0]?.value || '';
    }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, fn) { (this.handlers[name] ||= []).push(fn); }
    reportValidity() { return true; }
    async fire(name) {
      if (!this.disabled) await Promise.all((this.handlers[name] || []).map(fn => fn({preventDefault() {}})));
    }
  }
  const root = new Element('section');
  const worldSelect = new Element('select');
  const state = {worldId: 'A'};
  const descend = node => node.children.flatMap(child => [child, ...descend(child)]);
  const all = () => [root, ...descend(root)];
  const calls = [];
  const deferred = [];
  const stored = new Map();
  let mode = 'success';
  let routes = [];
  let serial = 0;
  const options = {
    datasets: [{id: 'ds', name: '已批准地图'}],
    locations: [{id: 'loc', name: '河岸', sources: [{id: 'event', occurred_at: '2040-01-01T10:00:00Z', summary: '勘察记录'}]}],
  };
  const api = (url, config = {}) => {
    calls.push({url, config});
    if (!config.method) {
      if (mode === 'defer') return new Promise(resolve => deferred.push(resolve));
      return Promise.resolve(JSON.parse(JSON.stringify(url.endsWith('/options') ? options : routes)));
    }
    if (mode === 'network') return Promise.reject(Error('暂时不可用'));
    if (mode === 'invalid') return Promise.reject(Object.assign(Error('参数错误'), {status: 422}));
    if (config.method === 'PATCH') {
      routes = routes.map(route => ({...route, is_open: !route.is_open, state_version: route.state_version + 1}));
      return Promise.resolve(routes[0]);
    }
    return Promise.resolve({id: 'request', element_type: 'local_route', status: 'proposed'});
  };
  const sandbox = {
    document: {createElement: tag => new Element(tag), querySelector: selector => selector === '#room-observer-registrations' ? root : null},
    elements: {worldSelect}, state, api, eventLabels: {},
    sessionStorage: {getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key)},
    crypto: {randomUUID: () => `request-${++serial}`},
    formatWorldTime: value => value,
    refreshWorldState: async () => {},
    escapeHtml: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  const field = label => all().find(node => node.attributes['aria-label'] === label);
  const button = label => all().find(node => node.tagName === 'button' && node.textContent === label);
  async function fill() {
    await button('读取通路与勘察来源').fire('click');
    field('通路名称').value = '河岸步道';
    field('已批准的导航数据集').value = 'ds';
    field('勘察地点').value = 'loc';
    await field('勘察地点').fire('change');
    field('同地点的来源事件').value = 'event';
    field('经纬度折线（JSON 数组）').value = '[[0,0],[0.001,0.001]]';
  }
  return {state, worldSelect, calls, deferred, stored, options, field, button, fill,
    form: () => all().find(node => node.tagName === 'form'),
    text: () => all().map(node => node.textContent).join(' '),
    mode: value => {mode = value;}, routes: value => {routes = value;},
    flush: () => new Promise(resolve => setImmediate(resolve)),
    details: payload => sandbox.localRouteProposalDetails(payload),
  };
}

async function candidateRequiresSeparateConfirmation() {
  const e = environment();
  await e.fill();
  await e.form().fire('submit');
  const writes = e.calls.filter(call => call.config.method);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].url, '/api/worlds/A/registrations');
  const body = JSON.parse(writes[0].config.body);
  assert.equal(body.payload.element_type, 'local_route');
  assert.equal(body.requested_by_character_id, null);
  assert.deepEqual(body.payload.polyline, [[0, 0], [0.001, 0.001]]);
  assert(e.text().includes('候选已提交'));
  assert.equal(e.stored.size, 0);
}

async function uncertainRequestReusesOriginal() {
  const e = environment();
  await e.fill();
  e.mode('network');
  await e.form().fire('submit');
  assert(e.field('通路名称').disabled);
  const first = e.calls.find(call => call.config.method === 'POST').config.body;
  e.mode('success');
  await e.button('核对上次提交结果').fire('click');
  assert.equal(e.calls.filter(call => call.config.method === 'POST')[1].config.body, first);
  assert.equal(e.stored.size, 0);
}

async function invalidSubmissionCanBeCorrected() {
  const e = environment();
  await e.fill();
  e.mode('invalid');
  await e.form().fire('submit');
  assert.equal(e.stored.size, 0);
  assert(!e.field('通路名称').disabled);
  assert.equal(e.field('通路名称').value, '河岸步道');
}

async function oldWorldResponseIsDiscarded() {
  const e = environment();
  e.mode('defer');
  const loading = e.button('读取通路与勘察来源').fire('click');
  await e.flush();
  e.state.worldId = 'B';
  await e.worldSelect.fire('change');
  e.state.worldId = 'A';
  await e.worldSelect.fire('change');
  e.deferred[0](e.options);
  e.deferred[1]([]);
  await loading;
  assert.equal(e.field('已批准的导航数据集').options.length, 0);
  assert.equal(e.calls.filter(call => call.config.method).length, 0);
}

async function refreshRetainsGeometryDraft() {
  const e = environment();
  await e.fill();
  await e.button('读取通路与勘察来源').fire('click');
  assert.equal(e.field('通路名称').value, '河岸步道');
  assert.equal(e.field('经纬度折线（JSON 数组）').value, '[[0,0],[0.001,0.001]]');
  assert.equal(e.field('同地点的来源事件').value, 'event');
}

async function availabilityUsesCurrentVersionAndSource() {
  const e = environment();
  e.routes([{id: 'road', name: '河岸步道', location_id: 'loc', polyline: [[0, 0], [1, 1]], bidirectional: true,
    speed_limit_kmh: 5, is_open: true, state_version: 3}]);
  await e.button('读取通路与勘察来源').fire('click');
  await e.button('关闭通路').fire('click');
  assert.equal(e.calls.filter(call => call.config.method).length, 0);
  e.field('变更 河岸步道 的来源事件').value = 'event';
  await e.button('关闭通路').fire('click');
  const call = e.calls.find(call => call.config.method === 'PATCH');
  assert.deepEqual(JSON.parse(call.config.body), {is_open: false, expected_state_version: 3,
    source_event_id: 'event', requested_by_character_id: null});
  assert(e.button('重新开放通路'));
}

async function malformedGeometryIsKeptForCorrection() {
  const e = environment();
  await e.fill();
  e.field('经纬度折线（JSON 数组）').value = '[[999,0],[1,1]]';
  await e.form().fire('submit');
  assert.equal(e.calls.filter(call => call.config.method).length, 0);
  assert(e.text().includes('有效经纬度'));
}

async function confirmationDetailsEscapeInput() {
  const e = environment();
  const html = e.details({dataset_id: '<script>', location_id: 'loc', polyline: [[0, 0], [1, 1]], speed_limit_kmh: 5});
  assert(!html.includes('<script>'));
  assert(html.includes('&lt;script&gt;'));
  assert(html.includes('全部 2 个'));
}

(async () => {
  const tests = [candidateRequiresSeparateConfirmation, uncertainRequestReusesOriginal, invalidSubmissionCanBeCorrected,
    oldWorldResponseIsDiscarded, refreshRetainsGeometryDraft, availabilityUsesCurrentVersionAndSource,
    malformedGeometryIsKeptForCorrection, confirmationDetailsEscapeInput];
  for (const test of tests) await test();
  console.log(JSON.stringify({status: 'verified', cases: tests.length, method: 'actual_UI_VM_mocked_API'}));
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
