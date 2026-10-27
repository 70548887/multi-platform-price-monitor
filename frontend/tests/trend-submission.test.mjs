import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { test } from 'node:test'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const transpile = (source) =>
  ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
const apiModule = { exports: {} }
vm.runInNewContext(transpile(readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8')), {
  module: apiModule,
  exports: apiModule.exports,
  require: () => vue,
  URLSearchParams,
})
function component(path, api, { admin = false, props = {} } = {}) {
  const { descriptor } = parse(readFileSync(new URL('../src/' + path, import.meta.url), 'utf8'))
  const code = compileScript(descriptor, { id: 'test-trend-submission' }).content
  const module = { exports: {} }
  const disposals = []
  const charts = []
  const chartEvents = []
  vm.runInNewContext(transpile(code), {
    module,
    exports: module.exports,
    AbortController,
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    require: (id) => {
      if (id === 'vue')
        return {
          ...vue,
          onMounted: () => {},
          watch: () => {},
          onBeforeUnmount: (fn) => disposals.push(fn),
        }
      if (id === 'vue-router') return { useRoute: () => ({ params: { id: '6' }, meta: { admin } }) }
      if (id === '../api')
        return {
          ...apiModule.exports,
          api,
          auth: vue.reactive({ user: { role: admin ? 'admin' : 'viewer' } }),
        }
      if (id === '../navigation')
        return { areaPath: (path, area) => (area ? '/admin' + path : path) }
      if (id === 'echarts/core')
        return {
          use: () => {},
          init: () => ({
            setOption: (value) => charts.push(value),
            resize() {
              chartEvents.push('resize')
            },
            clear() {
              chartEvents.push('clear')
            },
            dispose() {},
          }),
        }
      if (id.startsWith('echarts/')) return {}
      if (id.endsWith('.vue')) return { default: {} }
      throw new Error('Unexpected import ' + id)
    },
  })
  return {
    state: module.exports.default.setup(props, { expose: () => {} }),
    charts,
    chartEvents,
    dispose: () => disposals.forEach((fn) => fn()),
  }
}
test('quote sorting keeps decimal precision, separates currencies and leaves source order untouched', () => {
  const { state } = component('pages/ComparisonDetail.vue', async () => ({}))
  const items = [
    { id: 1, currency: 'CNY', price: '0.000000000000000002', display_price: '2', name: 'a' },
    { id: 2, currency: 'CNY', price: '0.000000000000000001', display_price: '3', name: 'b' },
    { id: 3, currency: 'USD', price: '0.0000000000000000001', display_price: '0.1', name: 'c' },
  ]
  state.comparison.value = { items }
  state.priceOrder.value = 'base-asc'
  assert.deepEqual(
    Array.from(state.visibleOffers.value, (p) => p.id),
    [2, 1, 3],
  )
  state.priceOrder.value = 'display-desc'
  assert.deepEqual(
    Array.from(state.visibleOffers.value, (p) => p.id),
    [2, 1, 3],
  )
  state.priceOrder.value = 'base-desc'
  assert.deepEqual(
    Array.from(state.visibleOffers.value, (p) => p.id),
    [1, 2, 3],
  )
  assert.deepEqual(
    items.map((p) => p.id),
    [1, 2, 3],
  )
})
test('aggregate chart retains exact tooltip values and missing hourly points', async () => {
  const basis = {
    key: 'cny',
    currency: 'CNY',
    unit: '',
    display_quantity: '1',
    unit_requires_review: true,
    current: { minimum: '0.007486', maximum: '0.009999', offer_count: 2, platform_count: 2 },
    items: [
      {
        at: '2026-10-26T10:00:00Z',
        minimum: null,
        maximum: null,
        offer_count: 0,
        platform_count: 0,
      },
      {
        at: '2026-10-26T11:00:00Z',
        minimum: '0.007486',
        maximum: '0.009999',
        offer_count: 2,
        platform_count: 2,
      },
    ],
  }
  const calls = []
  const { state, charts } = component(
    'components/ComparisonTrend.vue',
    async (path) => {
      calls.push(path)
      return { groups: [basis] }
    },
    { props: { mappingId: 6, refreshKey: 0 } },
  )
  state.el.value = {}
  await state.load()
  assert.equal(calls[0], '/mappings/6/trend?days=7')
  assert.equal(state.basis.value.current.minimum, '0.007486')
  assert.equal(state.selected.value, 'cny')
  const option = charts.at(-1)
  assert.equal(option.series[0].connectNulls, false)
  assert.equal(option.series[0].data[0][1], null)
  const tooltip = option.tooltip.formatter([{ dataIndex: 1 }])
  assert.match(tooltip, /0\.007486/)
  assert.match(tooltip, /0\.009999/)
})
test('trend permission failure clears a previously loaded series', async () => {
  let fail = false
  const { state } = component(
    'components/ComparisonTrend.vue',
    async () => {
      if (fail) throw new Error('记录不存在')
      return { groups: [{ key: 'c', current: { offer_count: 1 }, items: [] }] }
    },
    { props: { mappingId: 6, refreshKey: 0 } },
  )
  await state.load()
  assert.equal(state.groups.value.length, 1)
  fail = true
  await state.load()
  assert.equal(state.groups.value.length, 0)
  assert.equal(state.error.value, '记录不存在')
})
test('member submission and admin review use separate actions and preserve error input', async () => {
  const calls = []
  const { state } = component('pages/PlatformSubmissions.vue', async (path, options) => {
    calls.push([path, options])
    if (options?.method === 'POST') throw new Error('等待审核')
    return { items: [], total: 0 }
  })
  state.open()
  state.form.name = '测试平台'
  state.form.url = 'https://shop.example.org'
  await state.submit()
  assert.equal(calls[0][0], '/platform-submissions')
  assert.equal(JSON.parse(calls[0][1].body).url, 'https://shop.example.org')
  assert.equal(state.showForm.value, true)
  assert.equal(state.form.name, '测试平台')
  state.openReview({ id: 1 })
  await state.saveReview()
  assert.equal(calls.length, 1)
  const admin = component(
    'pages/PlatformSubmissions.vue',
    async (path, options) => {
      calls.push([path, options])
      return { items: [], total: 0 }
    },
    { admin: true },
  ).state
  admin.openReview({ id: 2 })
  admin.review.status = 'rejected'
  admin.review.admin_note = '暂不接入'
  await admin.saveReview()
  const action = calls.find(
    ([path, options]) => path === '/platform-submissions/2' && options?.method === 'PATCH',
  )
  assert.equal(JSON.parse(action[1].body).status, 'rejected')
  assert.equal(admin.reviewing.value, null)
})

test('reviewing the last record on a filtered final page returns to the valid previous page', async () => {
  const calls = []
  const { state } = component(
    'pages/PlatformSubmissions.vue',
    async (path) => {
      calls.push(path)
      const page = new URLSearchParams(path.split('?')[1]).get('page')
      return { items: page === '2' ? [] : [{ id: 20, status: 'pending' }], total: 20 }
    },
    { admin: true },
  )
  state.page.value = 2
  state.status.value = 'pending'
  await state.load()
  assert.equal(state.page.value, 1)
  assert.equal(state.items.value[0].id, 20)
  assert.equal(state.loading.value, false)
  assert.equal(calls.length, 2)
  assert.ok(calls[1].includes('page=1') && calls[1].includes('status=pending'))
})

test('successful quote refreshes emit a reactive trend update while failed reads do not', async () => {
  let fail = false
  const { state } = component('pages/ComparisonDetail.vue', async () => {
    if (fail) throw new Error('temporary failure')
    return { group: { id: 6 }, items: [], comparable: false, lowest_product_ids: [] }
  })
  const revisions = []
  const stop = vue.watch(
    () => state.trendRefresh.value,
    (value) => revisions.push(value),
  )
  await state.load()
  await vue.nextTick()
  await state.load()
  await vue.nextTick()
  fail = true
  await state.load()
  await vue.nextTick()
  stop()
  assert.deepEqual(revisions, [1, 2])
  assert.equal(state.comparison.value, null)
})

test('returning from an empty chart basis resizes the existing canvas before display', async () => {
  const points = [
    {
      at: '2026-10-27T00:00:00Z',
      minimum: '0.1',
      maximum: '0.2',
      offer_count: 2,
      platform_count: 2,
    },
  ]
  const { state, chartEvents } = component(
    'components/ComparisonTrend.vue',
    async () => ({
      groups: [
        {
          key: 'valid',
          currency: 'CNY',
          unit: '个',
          display_quantity: '1',
          current: { offer_count: 2 },
          items: points,
        },
        {
          key: 'empty',
          currency: 'USD',
          unit: '个',
          display_quantity: '1',
          current: { offer_count: 0 },
          items: [],
        },
      ],
    }),
    { props: { mappingId: 6, refreshKey: 0 } },
  )
  state.el.value = {}
  await state.load()
  const before = chartEvents.filter((x) => x === 'resize').length
  assert.ok(before >= 1)
  state.selected.value = 'empty'
  state.render()
  assert.equal(chartEvents.at(-1), 'clear')
  state.selected.value = 'valid'
  state.render()
  assert.equal(chartEvents.filter((x) => x === 'resize').length, before + 1)
})
