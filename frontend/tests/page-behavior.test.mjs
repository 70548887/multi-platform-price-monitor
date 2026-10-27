import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { test } from 'node:test'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const categoryTreeModule = { exports: {} }
const comparisonPreviewModule = { exports: {} }
vm.runInNewContext(
  ts.transpileModule(
    readFileSync(new URL('../src/comparisonPreview.ts', import.meta.url), 'utf8'),
    {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    },
  ).outputText,
  { module: comparisonPreviewModule, exports: comparisonPreviewModule.exports },
)
vm.runInNewContext(
  ts.transpileModule(readFileSync(new URL('../src/categoryTree.ts', import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText,
  { module: categoryTreeModule, exports: categoryTreeModule.exports },
)

// Compile the actual page script, injecting only network and browser boundaries.
// Deferred requests exercise the same state mutations as the mounted page.
function page(name, api, { path = '/', admin = false, role = 'admin', query = {} } = {}) {
  const filename = new URL(`../src/pages/${name}.vue`, import.meta.url)
  const { descriptor } = parse(readFileSync(filename, 'utf8'))
  const compiled = compileScript(descriptor, { id: `test-${name}` })
  const code = ts.transpileModule(compiled.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const unmount = []
  const mounted = []
  const routeState = { path, fullPath: path, meta: { admin }, params: { id: '7' }, query }
  const navigations = []
  const apiModule = {
    api,
    auth: vue.reactive({ user: { id: 1, role } }),
    appVersion: vue.reactive({ value: '1.2.1' }),
    itemsOf: (value) => value.items ?? [],
    params: (values) => {
      const entries = Object.entries(values).filter(
        ([, value]) => value !== undefined && value !== null && value !== '',
      )
      return entries.length ? '?' + new URLSearchParams(entries) : ''
    },
    json: (method, body) => ({ method, body: JSON.stringify(body) }),
    loadVersion: async () => {},
    ApiError: class ApiError extends Error {},
    date: (value) => String(value ?? ''),
    price: (value) => String(value ?? ''),
    qualityText: (value) => (value ?? []).join(' / '),
    decimalText: (value) => String(value ?? ''),
    displayBasisLabel: () => '',
    criticalQuality: () => false,
    decimalCompare: (a, b) => Number(a) - Number(b),
  }
  const module = { exports: {} }
  vm.runInNewContext(code, {
    module,
    exports: module.exports,
    require: (id) => {
      if (id === 'vue')
        return {
          ...vue,
          onMounted: (callback) => mounted.push(callback),
          onBeforeUnmount: (callback) => unmount.push(callback),
          watch: () => {},
        }
      if (id === 'vue-router')
        return {
          useRoute: () => routeState,
          useRouter: () => ({
            replace: async (destination) => {
              navigations.push(destination)
              Object.assign(routeState, destination)
            },
          }),
        }
      if (id === '../api') return apiModule
      if (id === '../navigation')
        return {
          areaPath: (value, area) => (area ? '/admin' + (value === '/' ? '' : value) : value),
        }
      if (id === '../categoryTree') return categoryTreeModule.exports
      if (id === '../comparisonPreview') return comparisonPreviewModule.exports
      if (id.endsWith('.vue')) return { default: {} }
      throw new Error(`Unexpected page dependency: ${id}`)
    },
    AbortController,
    document: { hidden: false },
    setTimeout: () => 1,
    clearTimeout: () => {},
  })
  const state = module.exports.default.setup({}, { expose: () => {} })
  return {
    state,
    navigations,
    mount: () => Promise.all(mounted.map((callback) => callback())),
    renderText: () => {
      const template = compileTemplate({
        id: `test-${name}`,
        source: descriptor.template.content,
        filename: filename.pathname,
        compilerOptions: { bindingMetadata: compiled.bindings },
      })
      assert.deepEqual(template.errors, [])
      const output = ts.transpileModule(template.code, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
      }).outputText
      const rendered = { exports: {} }
      vm.runInNewContext(output, {
        module: rendered,
        exports: rendered.exports,
        require: (id) => {
          assert.equal(id, 'vue')
          // DOM directives do not affect the rendered text being checked here.
          return { ...vue, resolveComponent: (name) => name, withDirectives: (node) => node }
        },
      })
      const text = (node) => {
        if (typeof node === 'string') return node
        if (Array.isArray(node)) return node.map(text).join('')
        return node && typeof node === 'object' ? text(node.children) : ''
      }
      return text(rendered.exports.render({}, [], {}, vue.proxyRefs(state), {}, {}))
    },
    unmount: () => unmount.forEach((callback) => callback()),
  }
}

function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

test('late history failure cannot replace the latest successful query', async () => {
  const old = deferred()
  const latest = deferred()
  const requests = [old, latest]
  const { state } = page('ProductDetail', () => requests.shift().promise)
  const first = state.history()
  const second = state.history()
  latest.resolve({ items: [{ price: '0.008' }], daily: [], events: [] })
  await second
  old.reject(new Error('old request failed'))
  await first
  assert.equal(state.error.value, '')
  assert.equal(state.points.value[0].price, '0.008')
  assert.equal(state.chartLoading.value, false)
})

test('retry clears a previous history error and displays its successful result', async () => {
  const retry = deferred()
  let call = 0
  const { state } = page('ProductDetail', () => {
    if (++call === 1) return Promise.reject(new Error('temporary failure'))
    return retry.promise
  })
  await state.history()
  assert.equal(state.error.value, 'temporary failure')
  const pending = state.history()
  assert.equal(state.error.value, '')
  assert.equal(state.chartLoading.value, true)
  retry.resolve({ items: [{ price: '0.009' }] })
  await pending
  assert.equal(state.error.value, '')
  assert.equal(state.points.value[0].price, '0.009')
})

test('an unmounted product page ignores a pending history failure', async () => {
  const pending = deferred()
  const { state, unmount } = page('ProductDetail', () => pending.promise)
  const request = state.history()
  unmount()
  pending.reject(new Error('response after navigation'))
  await request
  assert.equal(state.error.value, '')
})

test('daily history identifies UTC natural days without changing returned prices or dates', async () => {
  const aggregate = { date: '2026-10-25', min: '0.007486', max: '0.008', avg: '0.0077' }
  const { state, renderText } = page('ProductDetail', async () => ({
    items: [],
    daily: [aggregate],
    daily_timezone: 'UTC',
  }))
  state.product.value = { name: 'Fixture product', currency: 'CNY', interval_seconds: 3600 }
  state.loading.value = false
  state.resolution.value = 'daily'
  await state.history()
  assert.equal(state.dailyTimezone.value, 'UTC')
  assert.equal(state.daily.value[0].date, '2026-10-25')
  assert.equal(state.daily.value[0].min, '0.007486')
  const displayed = renderText()
  assert.match(displayed, /日聚合（UTC 自然日）/)
  assert.match(displayed, /UTC 自然日的最低、均价与最高价格/)
  const app = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  assert.match(app, /时间默认北京时间 · 日聚合按 UTC 自然日统计/)
  assert.doesNotMatch(app, /所有时间均为北京时间/)
})

test('upgrade history can retrieve the twenty-first record and retains the selected page', async () => {
  const requests = []
  const { state } = page('SystemUpdate', async (path) => {
    requests.push(path)
    if (path === '/update/status')
      return { current_version: '1.2.1', status: 'idle', run: null, backups: [] }
    if (path === '/update/config') return { enabled: true }
    if (path.startsWith('/update/history?')) {
      const query = new URLSearchParams(path.split('?')[1])
      assert.equal(query.get('page_size'), '20')
      return {
        items:
          query.get('page') === '2'
            ? [{ id: 1, status: 'failed' }]
            : Array.from({ length: 20 }, (_, index) => ({ id: 21 - index })),
        total: 21,
      }
    }
    throw new Error(`Unexpected request: ${path}`)
  })
  await state.refresh(true)
  assert.equal(state.history.value.length, 20)
  assert.equal(state.historyTotal.value, 21)
  await state.changeHistoryPage(2)
  assert.equal(state.historyPage.value, 2)
  assert.equal(state.history.value[0].id, 1)
  await state.refresh(false, true)
  assert.equal(state.history.value[0].status, 'failed')
  assert.equal(requests.filter((path) => path.includes('page=2&')).length, 2)
})

test('mapping candidates paginate and retain products selected from different platforms', async () => {
  const calls = []
  const first = { id: 1, platform_id: 1, name: 'First' }
  const second = { id: 31, platform_id: 2, name: 'Second' }
  const { state } = page(
    'Mappings',
    async (path) => {
      calls.push(path)
      if (path.startsWith('/product-categories')) return { items: [] }
      const query = new URLSearchParams(path.split('?')[1])
      assert.equal(query.get('page_size'), '30')
      return { items: query.get('platform_id') === '2' ? [second] : [first], total: 61 }
    },
    { path: '/admin/mappings', admin: true },
  )
  state.open()
  await new Promise(setImmediate)
  state.toggle(state.candidates.value[0])
  state.changeCandidatePage(2)
  await new Promise(setImmediate)
  assert.ok(calls.some((path) => path.includes('page=2&page_size=30')))
  state.candidatePlatform.value = '2'
  state.candidateCategory.value = 'Overseas / Video'
  await state.find()
  state.toggle(state.candidates.value[0])
  assert.equal(state.picked.value.map((product) => product.id).join(','), '1,31')
  assert.ok(
    calls.some(
      (path) => path.includes('platform_id=2') && path.includes('category=Overseas+%2F+Video'),
    ),
  )
})

test('late mapping search failures cannot replace a newer result or closed form state', async () => {
  const old = deferred(),
    current = deferred(),
    pendingClose = deferred()
  const requests = [old, current, pendingClose]
  const { state } = page(
    'Mappings',
    (path) =>
      path.startsWith('/product-categories')
        ? Promise.resolve({ items: [] })
        : requests.shift().promise,
    { admin: true },
  )
  state.open()
  const latest = state.find()
  current.resolve({ items: [{ id: 7 }], total: 1 })
  await latest
  old.reject(new Error('stale failure'))
  await new Promise(setImmediate)
  assert.equal(state.candidateError.value, '')
  assert.equal(state.candidates.value[0].id, 7)
  const closedRequest = state.find()
  state.closeForm()
  pendingClose.reject(new Error('after close'))
  await closedRequest
  assert.equal(state.candidateError.value, '')
  assert.equal(state.showForm.value, false)
})

test('administrator identity stays read-only in the user frontend', async () => {
  let requests = 0
  const { state } = page(
    'Mappings',
    async () => {
      requests++
      return {}
    },
    { path: '/mappings', admin: false },
  )
  state.open()
  await state.save()
  assert.equal(state.admin.value, false)
  assert.equal(state.showForm.value, false)
  assert.equal(requests, 0)
  const { state: events } = page(
    'Events',
    async () => {
      requests++
      return {}
    },
    { path: '/events', admin: false },
  )
  await events.ack({ id: 9 })
  assert.equal(requests, 0)
})

test('source category options are scoped to the selected platform and keep exact names', async () => {
  const calls = []
  const { state } = page('Products', async (path) => {
    calls.push(path)
    return {
      items: [
        { platform_id: 2, category_id: '', category_name: 'Video / Views', product_count: 42 },
      ],
    }
  })
  state.platform.value = '2'
  await state.loadCategories()
  assert.equal(calls[0], '/product-categories?platform_id=2')
  assert.equal(state.categoryOptions.value[0].name, 'Video / Views')
  assert.equal(state.categoryOptions.value[0].count, 42)
})

test('editing member grants uses a copy until save, then sends the explicit viewer selection', async () => {
  let written
  const member = { id: 2, username: 'viewer', role: 'viewer', enabled: true, platform_ids: [1] }
  const { state } = page('Users', async (path, options) => {
    if (options?.method === 'PATCH') written = { path, ...JSON.parse(options.body) }
    return { items: [] }
  })
  state.open(member)
  state.form.platform_ids.push(2)
  assert.equal(member.platform_ids.join(','), '1')
  await state.save()
  assert.equal(written.path, '/users/2')
  assert.equal(written.role, 'viewer')
  assert.equal(written.platform_ids.join(','), '1,2')
})

test('saving an administrator clears hidden platform selections', async () => {
  let written
  const { state } = page('Users', async (_path, options) => {
    if (options?.method === 'POST') written = JSON.parse(options.body)
    return { items: [] }
  })
  state.open()
  state.form.role = 'admin'
  state.form.platform_ids.push(99)
  await state.save()
  assert.equal(written.role, 'admin')
  assert.equal(written.platform_ids.length, 0)
})

test('a viewer with no assigned platforms gets a permission message after platforms load', async () => {
  const { state, mount, renderText } = page('Products', async () => ({ items: [], total: 0 }), {
    role: 'viewer',
  })
  assert.equal(state.noAssignedPlatforms.value, false)
  await mount()
  assert.equal(state.noAssignedPlatforms.value, true)
  assert.match(renderText(), /尚未分配平台，请联系管理员/)
})

test('comparison categories filter server results, search and paginate unified products', async () => {
  const calls = []
  const { state } = page('Mappings', async (path) => {
    calls.push(path)
    return { items: [{ id: 9, name: '点赞', category_id: 4 }], total: 25 }
  })
  state.selectCategory('4')
  await new Promise(setImmediate)
  state.query.value = '点赞'
  state.changeGroupPage(2)
  await new Promise(setImmediate)
  assert.ok(calls.some((path) => path.includes('category_id=4') && path.includes('page=1')))
  assert.ok(calls.some((path) => path.includes('q=%E7%82%B9%E8%B5%9E&page=2&page_size=12')))
  state.selectCategory('uncategorized')
  await new Promise(setImmediate)
  assert.match(calls.at(-1), /uncategorized=true/)
  assert.doesNotMatch(calls.at(-1), /category_id=/)
  assert.equal(state.groupTotal.value, 25)
})

test('new similar unified products can be saved before platform offers are attached', async () => {
  let written
  const { state } = page(
    'Mappings',
    async (path, options) => {
      if (options?.method === 'POST') written = { path, ...JSON.parse(options.body) }
      return { items: [], total: 0 }
    },
    { admin: true },
  )
  state.selectedCategory.value = '4'
  state.open()
  state.form.name = '点赞'
  assert.equal(state.form.match_mode, 'similar')
  assert.equal(state.form.category_id, '4')
  await state.save()
  assert.equal(written.path, '/mappings')
  assert.equal(written.name, '点赞')
  assert.equal(written.category_id, 4)
  assert.equal(written.match_mode, 'similar')
  assert.equal(written.unit, '')
  assert.equal(written.product_ids.length, 0)
  assert.equal(state.showForm.value, false)
})

test('similar comparison saves differently priced and denominated platform offers without normalization', async () => {
  let written
  const { state } = page(
    'Mappings',
    async (_path, options) => {
      if (options?.method === 'POST') written = JSON.parse(options.body)
      return { items: [], total: 0 }
    },
    { admin: true },
  )
  state.open()
  state.form.name = '同类点赞'
  state.toggle({ id: 1, platform_id: 1, unit: '次', currency: 'CNY', price: '0.007486' })
  state.toggle({ id: 2, platform_id: 2, unit: '', currency: 'USD', price: '0.00001' })
  await state.save()
  assert.equal(written.match_mode, 'similar')
  assert.equal(written.product_ids.join(','), '1,2')
  assert.equal(state.picked.value[0].price, '0.007486')
})

test('a product assigned to another comparison is not silently moved', () => {
  const { state } = page('Mappings', async () => ({ items: [] }), { admin: true })
  state.editing.value = { id: 4 }
  state.toggle({ id: 8, canonical_id: 99 })
  assert.equal(state.picked.value.length, 0)
  state.toggle({ id: 9, canonical_id: 4 })
  assert.equal(state.picked.value[0].id, 9)
})

test('comparison selection enforces the shared 5000-offer limit and permits removal', () => {
  let networkCalls = 0
  const { state } = page(
    'Mappings',
    async () => {
      networkCalls++
      return { items: [] }
    },
    { admin: true },
  )
  state.picked.value = Array.from({ length: 4999 }, (_, index) => ({ id: index + 1 }))
  state.toggle({ id: 5000 })
  assert.equal(state.picked.value.length, 5000)
  state.toggle({ id: 5001 })
  assert.equal(state.picked.value.length, 5000)
  assert.match(state.formError.value, /5000/)
  state.toggle({ id: 5000 })
  state.toggle({ id: 5001 })
  assert.equal(state.picked.value.length, 5000)
  assert.equal(state.picked.value.at(-1).id, 5001)
  assert.equal(networkCalls, 0)
})

test('category creation selects the saved category and establishes the next product there', async () => {
  let written
  const { state } = page(
    'Mappings',
    async (path, options) => {
      if (options?.method === 'POST') {
        written = { path, ...JSON.parse(options.body) }
        return { id: 6, name: '视频互动' }
      }
      return {
        items:
          path === '/comparison-categories' ? [{ id: 6, name: '视频互动', product_count: 0 }] : [],
        total: 0,
      }
    },
    { admin: true },
  )
  state.openCategory()
  state.categoryForm.name = ' 视频互动 '
  await state.saveCategory()
  assert.equal(written.path, '/comparison-categories')
  assert.equal(written.name, '视频互动')
  assert.equal(state.selectedCategory.value, '6')
  state.open()
  assert.equal(state.form.category_id, '6')
})

test('manage-associations links fetch a product outside the first list page', async () => {
  const calls = []
  const { state, mount } = page(
    'Mappings',
    async (path) => {
      calls.push(path)
      if (path === '/mappings/95')
        return {
          id: 95,
          name: '远端商品',
          match_mode: 'similar',
          category_id: 8,
          products: [{ id: 901 }],
        }
      return { items: [], total: 0 }
    },
    { admin: true, query: { edit: '95' } },
  )
  await mount()
  assert.ok(calls.includes('/mappings/95'))
  assert.equal(state.editing.value.id, 95)
  assert.equal(state.form.category_id, '8')
  assert.equal(state.picked.value[0].id, 901)
})

test('closing an association deep link consumes edit while preserving the category filter', async () => {
  const { state, navigations } = page('Mappings', async () => ({ items: [] }), {
    path: '/admin/compare',
    admin: true,
    query: { edit: '95', category_id: '8', q: '点赞' },
  })
  state.open({ id: 95, name: '点赞', match_mode: 'similar', products: [] })
  await state.closeForm()
  assert.equal(state.showForm.value, false)
  assert.equal(navigations.length, 1)
  assert.equal(navigations[0].query.edit, undefined)
  assert.equal(navigations[0].query.category_id, '8')
  assert.equal(navigations[0].query.q, '点赞')
})

test('saving a deep-linked association consumes edit only after the update succeeds', async () => {
  let fail = true
  const { state, navigations } = page(
    'Mappings',
    async (path, options = {}) => {
      if (options.method === 'PUT' && fail) throw new Error('save failed')
      return { items: [], total: 0 }
    },
    { path: '/admin/compare', admin: true, query: { edit: '95', category_id: '8' } },
  )
  state.open({ id: 95, name: '点赞', match_mode: 'similar', products: [] })
  await state.save()
  assert.equal(navigations.length, 0)
  assert.equal(state.showForm.value, true)
  fail = false
  await state.save()
  assert.equal(navigations.length, 1)
  assert.equal(navigations[0].query.edit, undefined)
  assert.equal(navigations[0].query.category_id, '8')
})

function comparisonFixture() {
  return {
    group: {
      id: 7,
      name: '同类点赞',
      category_name: '视频互动',
      description: '',
      match_mode: 'similar',
    },
    items: [
      {
        id: 1,
        platform_id: 1,
        platform_name: '平台 A',
        source_id: 'A-1',
        name: '普通点赞',
        category_name: '点赞',
        price: '0.007486',
        display_price: '7.486',
        display_quantity: '1000',
        unit: '次',
        currency: 'CNY',
        status: 'active',
        freshness: 'fresh',
        quality: [],
        comparison_eligible: false,
      },
      {
        id: 2,
        platform_id: 2,
        platform_name: '平台 B',
        source_id: 'B-1',
        name: '极速点赞',
        category_name: '点赞',
        price: '0.00001',
        display_price: '0.00001',
        display_quantity: '1',
        unit: '',
        currency: 'USD',
        status: 'active',
        freshness: 'fresh',
        quality: [],
        comparison_eligible: false,
      },
    ],
    comparable: false,
    reason: '同类报价',
    eligible_product_ids: [],
    lowest_product_ids: [2],
  }
}

test('similar comparison details show all platform offers and ignore unapproved lowest-price labels', async () => {
  const { state, renderText } = page('ComparisonDetail', async () => comparisonFixture())
  await state.load()
  assert.equal(state.visibleOffers.value.length, 2)
  assert.equal(state.visibleOffers.value[0].price, '0.007486')
  assert.equal(state.lowestIds.value.size, 0)
  assert.match(renderText(), /普通点赞/)
  assert.match(renderText(), /极速点赞/)
  assert.match(renderText(), /未自动判定全网最低价/)
  assert.doesNotMatch(renderText(), /可比范围内最低/)
  state.selectedPlatform.value = '2'
  assert.equal(state.visibleOffers.value.length, 1)
  assert.equal(state.visibleOffers.value[0].id, 2)
  assert.equal(state.comparison.value.items.length, 2)
  assert.equal(state.lowestIds.value.size, 0)
})

test('comparison refresh failure clears offers after access is revoked', async () => {
  let calls = 0
  const { state, renderText } = page('ComparisonDetail', async () => {
    if (++calls === 1) return comparisonFixture()
    throw new Error('记录不存在')
  })
  await state.load()
  assert.equal(state.visibleOffers.value.length, 2)
  await state.load()
  assert.equal(state.comparison.value, null)
  assert.equal(state.visibleOffers.value.length, 0)
  assert.equal(state.error.value, '记录不存在')
  assert.doesNotMatch(renderText(), /普通点赞|极速点赞/)
})

function nestedCategories() {
  return [
    { id: 1, name: '抖音', parent_id: null, sort_order: 1, product_count: 2, children_count: 2 },
    { id: 2, name: '视频', parent_id: 1, sort_order: 1, product_count: 1, children_count: 1 },
    { id: 3, name: '直播', parent_id: 1, sort_order: 2, product_count: 1, children_count: 0 },
    { id: 4, name: '快手', parent_id: null, sort_order: 2, product_count: 0, children_count: 1 },
    { id: 5, name: '视频', parent_id: 4, sort_order: 1, product_count: 0, children_count: 0 },
    { id: 6, name: '互动', parent_id: 2, sort_order: 1, product_count: 1, children_count: 0 },
  ]
}

test('comparison cards summarize distinct platforms and an unlinked product prompts association', () => {
  const { state, renderText } = page('Mappings', async () => ({ items: [] }), { admin: true })
  state.loading.value = false
  state.groups.value = [
    {
      id: 4,
      name: '点赞',
      category_name: '视频',
      match_mode: 'similar',
      product_count: 4,
      platform_count: 2,
      products: [
        ...[1, 2, 3].map((id) => ({
          id,
          platform_id: 1,
          platform_name: '亿乐',
          price: '0.001',
          currency: 'CNY',
          unit: '',
          status: 'active',
          freshness: 'fresh',
          quality: ['unit_requires_review'],
        })),
        {
          id: 4,
          platform_id: 2,
          platform_name: 'Fans8',
          price: '0.002',
          currency: 'CNY',
          unit: '',
          status: 'active',
          freshness: 'fresh',
          quality: [],
        },
      ],
    },
  ]
  let text = renderText()
  assert.equal((text.match(/亿乐/g) ?? []).length, 1)
  assert.equal((text.match(/Fans8/g) ?? []).length, 1)
  assert.match(text, /基础报价范围/)
  state.groups.value = [
    { id: 5, name: '新商品', product_count: 0, products: [], match_mode: 'similar' },
  ]
  text = renderText()
  assert.match(text, /待关联/)
  assert.match(text, /关联平台商品/)
  assert.doesNotMatch(text, /查看各平台价格/)
})

test('choosing a category closes the compact category panel without losing the selected branch', async () => {
  const { state } = page('Mappings', async () => ({ items: [] }), { admin: true })
  state.categoryPanelOpen.value = true
  state.selectCategory('2')
  assert.equal(state.categoryPanelOpen.value, false)
  assert.equal(state.selectedCategory.value, '2')
})

test('category tree keeps repeated sibling names distinct and collapse never changes selection', () => {
  const { state } = page('Mappings', async () => ({ items: [] }), { admin: true })
  state.categories.value = nestedCategories()
  state.selectedCategory.value = '2'
  assert.equal(state.categoryLabel(state.categories.value[1]), '抖音 / 视频')
  assert.equal(state.categoryLabel(state.categories.value[4]), '快手 / 视频')
  assert.equal(
    state.categoryRows.value.map((item) => `${item.id}:${item.depth}`).join(','),
    '1:0,2:1,6:2,3:1,4:0,5:1',
  )
  state.toggleCategory(1)
  assert.equal(state.categoryRows.value.map((item) => item.id).join(','), '1,4,5')
  assert.equal(state.selectedCategory.value, '2')
  state.toggleCategory(1)
  assert.equal(state.categoryRows.value.length, 6)
  assert.equal(state.selectedCategoryName.value, '抖音 / 视频')
})

test('category edits exclude self and descendants while child creation submits its parent ID', async () => {
  const writes = []
  const { state } = page(
    'Mappings',
    async (_path, options) => {
      if (options?.method) {
        writes.push(JSON.parse(options.body))
        return { id: 9 }
      }
      return { items: nestedCategories(), total: 0 }
    },
    { admin: true },
  )
  state.categories.value = nestedCategories()
  state.openCategory(state.categories.value[0])
  assert.equal(state.categoryParentOptions.value.map((item) => item.id).join(','), '4,5')
  state.categoryForm.parent_id = '6'
  await state.saveCategory()
  assert.equal(writes.length, 0)
  assert.match(state.categoryFormError.value, /不能选择自身或下级/)
  state.openSubcategory(state.categories.value[3])
  state.categoryForm.name = '直播'
  await state.saveCategory()
  assert.equal(writes[0].parent_id, 4)
  state.openCategory()
  state.categoryForm.name = '小红书'
  await state.saveCategory()
  assert.equal(writes[1].parent_id, null)
})

test('deep category links select the branch and new products retain the complete chosen category', async () => {
  const calls = []
  const { state, mount } = page(
    'Mappings',
    async (path) => {
      calls.push(path)
      return { items: path === '/comparison-categories' ? nestedCategories() : [], total: 0 }
    },
    { admin: true, query: { category_id: '2' } },
  )
  await mount()
  assert.equal(state.selectedCategory.value, '2')
  assert.equal(state.selectedCategoryName.value, '抖音 / 视频')
  assert.ok(calls.some((path) => path.includes('category_id=2')))
  state.open()
  assert.equal(state.form.category_id, '2')
  assert.equal(state.categoryOptions.value.filter((item) => item.name === '视频').length, 2)
  assert.equal(state.cannotDeleteCategory(state.categories.value[3]), true)
  assert.equal(state.cannotDeleteCategory(state.categories.value[4]), false)
})

test('comparison details expose the full category breadcrumb and return to that category', async () => {
  const data = comparisonFixture()
  data.group.category_id = 2
  data.group.category_name = '视频'
  data.group.category_path = [
    { id: 1, name: '抖音' },
    { id: 2, name: '视频' },
  ]
  data.group.category_path_name = '抖音 / 视频'
  const { state } = page('ComparisonDetail', async () => data, { admin: true })
  await state.load()
  assert.equal(state.categoryBreadcrumb.value.map((item) => item.name).join(' / '), '抖音 / 视频')
  assert.equal(state.returnPath.value.path, '/admin/compare')
  assert.equal(state.returnPath.value.query.category_id, 2)
  assert.equal(state.visibleOffers.value.length, data.items.length)
})

test('update proxy readiness refreshes after overseas settings are repaired without revealing its address', async () => {
  let ready = false
  const { state, renderText } = page('SystemUpdate', async (path) => {
    if (path === '/update/status')
      return { current_version: '1.2.1', status: 'idle', run: null, backups: [] }
    if (path === '/update/config')
      return {
        enabled: true,
        proxy_route: 'overseas',
        proxy_source: '系统设置中的境外代理',
        proxy_ready: ready,
        proxy_configured: true,
        proxy_enabled: ready,
      }
    if (path.startsWith('/update/history?')) return { items: [], total: 0 }
    throw new Error('Unexpected request: ' + path)
  })
  await state.refresh(true)
  assert.equal(state.config.value.proxy_ready, false)
  assert.match(renderText(), /未就绪/)
  ready = true
  await state.refresh(false, true)
  assert.equal(state.config.value.proxy_ready, true)
  assert.match(renderText(), /已配置并启用/)
  assert.match(renderText(), /管理境外代理/)
})

test('smart recommendations submit aliases/exclusions/filter context and retain matching explanation', async () => {
  const calls = []
  const { state } = page(
    'Mappings',
    async (path) => {
      calls.push(path)
      return {
        items: [{ id: 11, name: 'BL-赞', platform_id: 2, canonical_id: null }],
        total: 67,
        keywords: [{ label: '哔哩哔哩', variants: ['bili', 'BL'] }],
        platform_counts: [{ platform_id: 2, name: '平台二', count: 67 }],
      }
    },
    { path: '/admin/compare', admin: true },
  )
  state.showForm.value = true
  state.editing.value = { id: 6 }
  state.searchMode.value = 'smart'
  state.search.value = 'B站 点赞'
  state.excludeKeywords.value = '评论 分享'
  state.candidatePlatform.value = '2'
  state.candidateCategory.value = '哔哩业务 / BL点赞'
  await state.find()
  const requested = new URLSearchParams(calls[0].split('?')[1])
  assert.equal(calls[0].split('?')[0], '/product-suggestions')
  assert.equal(requested.get('q'), 'B站 点赞')
  assert.equal(requested.get('exclude'), '评论 分享')
  assert.equal(requested.get('mode'), 'all')
  assert.equal(requested.get('mapping_id'), '6')
  assert.equal(requested.get('platform_id'), '2')
  assert.equal(requested.get('category'), '哔哩业务 / BL点赞')
  assert.equal(requested.get('active_only'), 'true')
  assert.equal(state.candidateTotal.value, 67)
  assert.equal(state.matchingPlatforms.value[0].count, 67)
  assert.equal(state.matchedKeywords.value[0].label, '哔哩哔哩')
})

test('page selection preserves prior choices, skips conflicts and removes only this page', () => {
  const { state } = page('Mappings', async () => ({}), { path: '/admin/compare', admin: true })
  state.searching.value = false
  state.editing.value = { id: 6 }
  state.picked.value = [
    { id: 1, name: 'old' },
    { id: 2, name: 'current' },
  ]
  state.candidates.value = [{ id: 2 }, { id: 3, canonical_id: null }, { id: 4, canonical_id: 8 }]
  state.selectCurrentPage()
  assert.deepEqual(
    Array.from(state.picked.value, (p) => p.id),
    [1, 2, 3],
  )
  state.removeCurrentPage()
  assert.deepEqual(
    Array.from(state.picked.value, (p) => p.id),
    [1],
  )
})

test('select all matching stages every page locally and never sends a write', async () => {
  const calls = []
  const { state } = page(
    'Mappings',
    async (path, options) => {
      calls.push({ path, options })
      const page = Number(new URLSearchParams(path.split('?')[1]).get('page'))
      return {
        result_token: 'stable-revision',
        total: 102,
        items:
          page === 1
            ? Array.from({ length: 100 }, (_, i) => ({
                id: i + 1,
                canonical_id: i === 2 ? 8 : null,
              }))
            : [{ id: 101 }, { id: 102 }],
      }
    },
    { path: '/admin/compare', admin: true },
  )
  state.showForm.value = true
  state.searchMode.value = 'smart'
  state.search.value = 'bili 点赞'
  state.candidateTotal.value = 102
  state.editing.value = { id: 6 }
  state.picked.value = [{ id: 999, name: 'prior page' }]
  await state.selectAllMatching()
  assert.equal(calls.length, 2)
  assert.ok(calls.every((call) => !call.options?.method))
  assert.equal(state.picked.value.length, 102)
  assert.equal(state.picked.value[0].id, 999)
  assert.ok(!state.picked.value.some((p) => p.id === 3))
  assert.ok(state.picked.value.some((p) => p.id === 102))
  assert.equal(state.bulkSelecting.value, false)
  assert.match(state.selectionNotice.value, /跳过 1/)
})

test('a cancelled or failed batch never commits a partially fetched page', async () => {
  for (const cancel of [false, true]) {
    const waiting = deferred()
    let call = 0
    const { state } = page(
      'Mappings',
      async () => {
        if (++call === 1)
          return {
            result_token: 'stable-revision',
            total: 101,
            items: Array.from({ length: 100 }, (_, i) => ({ id: i + 1 })),
          }
        return waiting.promise
      },
      { path: '/admin/compare', admin: true },
    )
    state.showForm.value = true
    state.searchMode.value = 'smart'
    state.search.value = 'B站 点赞'
    state.candidateTotal.value = 101
    state.picked.value = [{ id: 999 }]
    const pending = state.selectAllMatching()
    await Promise.resolve()
    await Promise.resolve()
    if (cancel) {
      state.invalidateBulk()
      waiting.resolve({ result_token: 'stable-revision', total: 101, items: [{ id: 101 }] })
    } else waiting.reject(new Error('second page failed'))
    await pending
    assert.deepEqual(
      Array.from(state.picked.value, (p) => p.id),
      [999],
    )
    if (!cancel) assert.match(state.selectionNotice.value, /未添加任何商品/)
  }
})

test('keyword changes invalidate late recommendations without disturbing previous selections', async () => {
  const waiting = deferred()
  let count = 0
  const { state } = page(
    'Mappings',
    async () => {
      if (++count === 1) return waiting.promise
      return { items: [{ id: 22 }], total: 1, keywords: [], platform_counts: [] }
    },
    { path: '/admin/compare', admin: true },
  )
  state.showForm.value = true
  state.searchMode.value = 'smart'
  state.search.value = 'B站 点赞'
  state.picked.value = [{ id: 999 }]
  const first = state.find()
  state.search.value = '快手 点赞'
  state.applyMatchOptions()
  await Promise.resolve()
  await Promise.resolve()
  waiting.resolve({ items: [{ id: 11 }], total: 1 })
  await first
  assert.equal(state.candidates.value[0].id, 22)
  assert.deepEqual(
    Array.from(state.picked.value, (p) => p.id),
    [999],
  )
})

test('batch rejects truncated or changed candidate pages instead of committing partial associations', async () => {
  for (const failure of ['empty', 'changed', 'duplicate']) {
    let call = 0
    const { state } = page(
      'Mappings',
      async () => {
        if (++call === 1)
          return {
            result_token: 'stable-revision',
            total: 101,
            items: Array.from({ length: 100 }, (_, i) => ({ id: i + 1 })),
          }
        return failure === 'empty'
          ? { result_token: 'stable-revision', total: 101, items: [] }
          : failure === 'changed'
            ? { result_token: 'stable-revision', total: 102, items: [{ id: 101 }, { id: 102 }] }
            : { result_token: 'stable-revision', total: 101, items: [{ id: 1 }] }
      },
      { path: '/admin/compare', admin: true },
    )
    state.showForm.value = true
    state.searchMode.value = 'smart'
    state.search.value = 'B站 点赞'
    state.candidateTotal.value = 101
    state.picked.value = [{ id: 999 }]
    await state.selectAllMatching()
    assert.deepEqual(
      Array.from(state.picked.value, (p) => p.id),
      [999],
    )
    assert.match(state.selectionNotice.value, /未添加任何商品/)
    assert.ok(state.candidateError.value)
  }
})

test('typing exclusion keywords immediately clears old totals and invalidates batch preparation', () => {
  const { state } = page('Mappings', async () => ({}), { path: '/admin/compare', admin: true })
  state.showForm.value = true
  state.searchMode.value = 'smart'
  state.candidateTotal.value = 56
  state.candidates.value = [{ id: 11 }]
  state.matchedKeywords.value = [{ label: 'old' }]
  state.bulkSelecting.value = true
  state.picked.value = [{ id: 999 }]
  state.excludeKeywords.value = '评论 分享'
  state.scheduleMatchOptions()
  assert.equal(state.candidateTotal.value, 0)
  assert.equal(state.candidates.value.length, 0)
  assert.equal(state.matchedKeywords.value.length, 0)
  assert.equal(state.bulkSelecting.value, false)
  assert.deepEqual(
    Array.from(state.picked.value, (p) => p.id),
    [999],
  )
})

test('a constant candidate count with changed metadata revision aborts every draft addition', async () => {
  let call = 0
  const { state } = page(
    'Mappings',
    async () =>
      ++call === 1
        ? {
            result_token: 'original',
            total: 101,
            items: Array.from({ length: 100 }, (_, i) => ({ id: i + 1 })),
          }
        : { result_token: 'metadata-changed', total: 101, items: [{ id: 101 }] },
    { path: '/admin/compare', admin: true },
  )
  state.showForm.value = true
  state.searchMode.value = 'smart'
  state.search.value = 'B站 点赞'
  state.candidateTotal.value = 101
  state.candidateResultToken.value = 'original'
  state.picked.value = [{ id: 999 }]
  await state.selectAllMatching()
  assert.deepEqual(
    Array.from(state.picked.value, (p) => p.id),
    [999],
  )
  assert.match(state.candidateError.value, /已变化/)
  assert.match(state.selectionNotice.value, /未添加任何商品/)
})

test('a failed candidate refresh clears previous candidate actions and counts', async () => {
  const { state } = page(
    'Mappings',
    async () => {
      throw Object.assign(new Error('权限已收回'), { status: 403 })
    },
    { path: '/admin/compare', admin: true },
  )
  state.showForm.value = true
  state.candidates.value = [{ id: 1 }]
  state.candidateTotal.value = 50
  state.candidateResultToken.value = 'old'
  state.picked.value = [{ id: 999 }]
  await state.find()
  assert.equal(state.candidates.value.length, 0)
  assert.equal(state.candidateTotal.value, 0)
  assert.equal(state.candidateResultToken.value, '')
  assert.deepEqual(
    Array.from(state.picked.value, (p) => p.id),
    [999],
  )
})

test('revoked product access removes old price history and product details from the page', async () => {
  const { state } = page('ProductDetail', async () => {
    throw Object.assign(new Error('记录不存在'), { status: 404 })
  })
  state.product.value = { id: 7, price: '0.008' }
  state.points.value = [{ price: '0.008' }]
  state.daily.value = [{ min: '0.008' }]
  state.events.value = [{ title: 'private' }]
  await state.history()
  assert.equal(state.product.value, null)
  assert.equal(state.points.value.length, 0)
  assert.equal(state.daily.value.length, 0)
  assert.equal(state.events.value.length, 0)
  assert.equal(state.error.value, '记录不存在')
})
