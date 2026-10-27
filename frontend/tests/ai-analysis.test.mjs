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
    URL,
    document: { hidden: false },
    setTimeout: () => 1,
    clearTimeout: () => {},
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
    require: (id) => {
      if (id === 'vue')
        return {
          ...vue,
          onMounted: () => {},
          watch: (...args) => {
            const stop = vue.watch(...args)
            disposals.push(stop)
            return stop
          },
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

test('AI configuration loads only key status and omits an empty replacement key from save', async () => {
  const calls = []
  const current = {
    enabled: false,
    base_url: 'https://ai.example.org/v1',
    model: 'fixture',
    daily_time: '09:00',
    proxy_route: 'direct',
    sample_limit: 100,
    json_mode: true,
    key_configured: true,
    configured: true,
  }
  const { state, dispose } = component(
    'components/AIConfigPanel.vue',
    async (path, options) => {
      calls.push({ path, options })
      return current
    },
    { admin: true },
  )
  await state.load()
  assert.equal(state.form.key, '')
  assert.equal(state.config.value.key_configured, true)
  await state.save()
  let body = JSON.parse(calls[1].options.body)
  assert.equal(Object.hasOwn(body, 'key'), false)
  state.form.key = 'new-fixture-secret'
  await state.save()
  body = JSON.parse(calls[2].options.body)
  assert.equal(body.key, 'new-fixture-secret')
  assert.equal(state.form.key, '')
  state.form.key = 'not-saved'
  dispose()
  assert.equal(state.form.key, '')
})

test('clearing the AI key explicitly disables daily execution and does not send old config secrets', async () => {
  const calls = []
  const { state } = component(
    'components/AIConfigPanel.vue',
    async (path, options) => {
      calls.push({ path, options })
      return { enabled: false, key_configured: false }
    },
    { admin: true },
  )
  state.config.value = { key_configured: true }
  state.form.enabled = true
  await state.clearKey()
  assert.deepEqual(JSON.parse(calls[0].options.body), { clear_key: true })
  assert.equal(state.form.enabled, false)
})

test('AI key input stays in the form when validation fails, and is never put into a URL', async () => {
  const calls = []
  const { state } = component(
    'components/AIConfigPanel.vue',
    async (path, options) => {
      calls.push({ path, options })
      throw new Error('接口配置无效')
    },
    { admin: true },
  )
  state.loading.value = false
  state.config.value = { key_configured: true, configured: true }
  state.form.key = 'fixture-private-key'
  await state.save()
  assert.equal(state.form.key, 'fixture-private-key')
  assert.equal(state.error.value, '接口配置无效')
  assert.equal(calls[0].path, '/ai-analysis/config')
  assert.equal(calls[0].options.method, 'PUT')
})

test('AI configuration cannot save before loading or after a failed load, and retry restores saving', async () => {
  const calls = []
  let failLoad = true
  const current = {
    enabled: false,
    base_url: 'https://ai.example.org/v1',
    model: 'fixture',
    daily_time: '09:00',
    proxy_route: 'direct',
    sample_limit: 100,
    json_mode: true,
    key_configured: true,
    configured: true,
  }
  const { state } = component(
    'components/AIConfigPanel.vue',
    async (path, options) => {
      calls.push({ path, options })
      if (!options?.method && failLoad) throw new Error('读取失败')
      return current
    },
    { admin: true },
  )
  await state.save()
  assert.equal(calls.length, 0)
  await state.load()
  assert.equal(state.config.value, null)
  assert.equal(state.loading.value, false)
  await state.save()
  assert.equal(calls.filter((call) => call.options?.method === 'PUT').length, 0)
  failLoad = false
  await state.load()
  await state.save()
  assert.equal(calls.filter((call) => call.options?.method === 'PUT').length, 1)
  assert.equal(state.error.value, '')
})

test('manual analysis only enqueues through the authenticated API, history never creates another task', async () => {
  const calls = []
  const { state } = component(
    'pages/AIAnalysis.vue',
    async (path, options) => {
      calls.push({ path, options })
      return path === '/ai-analysis/overview'
        ? {
            config: {
              configured: true,
              enabled: false,
              model: 'fixture',
              daily_product_limit: 1000,
              daily_request_limit: 20,
            },
            worker: { ready: true },
            today: { products_used: 0, requests_used: 0 },
            current_run: null,
          }
        : { items: [], total: 0 }
    },
    { admin: true },
  )
  await state.load()
  state.ask('run')
  assert.equal(calls.filter((c) => c.options?.method === 'POST').length, 0)
  await state.confirmAction()
  assert.equal(calls.filter((c) => c.options?.method === 'POST').length, 1)
  assert.equal(calls.find((c) => c.options?.method === 'POST').path, '/ai-analysis/run')
  await state.load()
  assert.equal(calls.filter((c) => c.options?.method === 'POST').length, 1)
})

test('late AI report responses do not replace the latest selected report', async () => {
  let resolveFirst
  const first = new Promise((resolve) => (resolveFirst = resolve))
  const { state } = component(
    'pages/AIAnalysis.vue',
    async (path) => (path.endsWith('/1') ? first : { id: 2, report: { summary: 'latest' } }),
    { admin: true },
  )
  const old = state.view({ id: 1 })
  await state.view({ id: 2 })
  resolveFirst({ id: 1, report: { summary: 'old' } })
  await old
  assert.equal(state.report.value.id, 2)
})

test('AI report access failure clears previously visible data', async () => {
  const { state } = component(
    'pages/AIAnalysis.vue',
    async () => {
      throw new Error('需要管理员权限')
    },
    { admin: true },
  )
  state.report.value = { id: 1, report: { summary: 'private' } }
  state.runs.value = [{ id: 1 }]
  await state.load()
  assert.equal(state.report.value, null)
  assert.equal(state.runs.value.length, 0)
})

test('local program preview requires no AI configuration and never submits a paid analysis task', async () => {
  const calls = []
  const { state } = component(
    'pages/AIAnalysis.vue',
    async (path, options) => {
      calls.push({ path, options })
      return { total_products: 12, sample_count: 12, platforms: [] }
    },
    { admin: true },
  )
  await state.preview()
  assert.equal(calls.filter((c) => c.path === '/ai-analysis/preview').length, 1)
  assert.equal(calls[0].path, '/ai-analysis/preview')
  assert.equal(calls[0].options?.method, undefined)
  assert.equal(
    calls.every((c) => !c.options?.method),
    true,
  )
  assert.equal(state.report.value.preview, true)
  assert.equal(state.report.value.status, 'preview')
  assert.equal(state.report.value.snapshot.total_products, 12)
})

const summary = (changes = {}) => ({
  config: { configured: true, daily_product_limit: 1000, daily_request_limit: 20 },
  today: { products_used: 0, requests_used: 0 },
  worker: { ready: true },
  current_run: null,
  ...changes,
})

test('manual submission uses global active run, worker and daily quota rather than current history page', async () => {
  const posts = []
  const { state } = component(
    'pages/AIAnalysis.vue',
    async (path, options) => {
      if (options?.method) posts.push(path)
      return path === '/ai-analysis/overview' ? summary() : { items: [], total: 0 }
    },
    { admin: true },
  )
  await state.load()
  assert.equal(state.canRun.value, true)
  state.overview.value = summary({ current_run: { id: 99, status: 'running' } })
  await state.run()
  assert.equal(posts.length, 0)
  state.overview.value = summary({ worker: { ready: false } })
  await state.run()
  assert.equal(posts.length, 0)
  state.overview.value = summary({ today: { products_used: 1000, requests_used: 1 } })
  await state.run()
  assert.equal(posts.length, 0)
  state.overview.value = summary({ today: { products_used: 1, requests_used: 20 } })
  await state.run()
  assert.equal(posts.length, 0)
})

test('stale application response is shown as not applied, not a success toast', async () => {
  const calls = []
  const { state } = component(
    'pages/AIAnalysis.vue',
    async (path, options) => {
      calls.push({ path, options })
      return options?.method
        ? { status: 'stale', blocked_reason: '数据已改变' }
        : path === '/ai-analysis/overview'
          ? summary()
          : { items: [], total: 0 }
    },
    { admin: true },
  )
  state.ask('apply', { id: 12, revision: 4 })
  await state.confirmAction()
  assert.equal(calls[0].path, '/ai-analysis/suggestions/12/apply')
  assert.deepEqual(JSON.parse(calls[0].options.body), { revision: 4 })
  assert.match(state.notice.value, /未应用.*数据已改变/)
  assert.equal(state.noticeKind.value, 'warning')
  assert.equal(state.confirm.value, null)
})

test('AI review preserves exact source quantities without inferring undeclared units', () => {
  const { state } = component('pages/AIAnalysis.vue', async () => ({}), { admin: true })
  assert.equal(state.quantityBasis({ display_quantity: '1000', unit: '人' }), '源计价数量：1000 人')
  assert.equal(state.quantityBasis({ display_quantity: '1', unit: '人' }), '源计价数量：1 人')
  assert.equal(
    state.quantityBasis({ display_quantity: '0.0000000000000000000000001', unit: '' }),
    '源计价数量：0.0000000000000000000000001（单位待核验）',
  )
  for (const quantity of [undefined, null, '', '0', '0.000', '-1', 'NaN'])
    assert.equal(
      state.quantityBasis({ display_quantity: quantity, unit: '人' }),
      '源计价数量：待核验',
    )
})

test('old reports keep category and grouping proposals visible as read-only evidence', () => {
  const calls = []
  const { state } = component('pages/AIAnalysis.vue', async (path) => calls.push(path), {
    admin: true,
  })
  state.report.value = {
    id: 1,
    batches: [],
    snapshot: { products: [{ id: 3, name: 'B站视频点赞', platform_id: 2 }] },
    report: {
      category_suggestions: [
        {
          product_id: 3,
          category_path: '哔哩哔哩 / 视频',
          group_name: '视频点赞',
          reason: '旧分类依据',
        },
      ],
      group_suggestions: [
        {
          product_ids: [3, 4],
          category_path: '哔哩哔哩 / 视频',
          name: 'B站点赞组',
          reason: '旧归总依据',
        },
      ],
    },
  }
  assert.equal(state.legacySuggestions.value.length, 2)
  assert.equal(state.legacySuggestions.value[0].category_path, '哔哩哔哩 / 视频')
  assert.equal(state.legacySuggestions.value[1].name, 'B站点赞组')
  assert.equal(state.productName(3), 'B站视频点赞')
  assert.equal(calls.length, 0)
  state.report.value = { ...state.report.value, batches: [{ id: 8 }] }
  assert.equal(state.legacySuggestions.value.length, 0)
})

test('stale suggestions cannot be edited or applied as fresh proposals', async () => {
  const calls = []
  const { state } = component(
    'pages/AIAnalysis.vue',
    async (path) => {
      calls.push(path)
      return {}
    },
    { admin: true },
  )
  await state.editSuggestion({ id: 1, status: 'stale', revision: 3 })
  assert.equal(calls.length, 0)
  assert.equal(state.editing.value, null)
  assert.equal(state.reviewable({ status: 'stale' }), false)
})

test('editing a suggestion loads all target pages and saves a revision without applying it', async () => {
  const calls = []
  const { state } = component(
    'pages/AIAnalysis.vue',
    async (path, options) => {
      calls.push({ path, options })
      if (path === '/comparison-categories') return { items: [{ id: 6, name: '视频' }] }
      if (path.startsWith('/mappings?page=1'))
        return { items: [{ id: 7, name: '抖音视频点赞', category_id: 6 }], total: 2 }
      if (path.startsWith('/mappings?page=2'))
        return { items: [{ id: 8, name: '抖音视频评论', category_id: 6 }], total: 2 }
      if (options?.method) return { status: 'pending' }
      return path === '/ai-analysis/overview' ? summary() : { items: [], total: 0 }
    },
    { admin: true },
  )
  await state.editSuggestion({
    id: 10,
    revision: 2,
    status: 'pending',
    category_id: 6,
    group_name: '抖音点赞',
  })
  assert.equal(state.groups.value.length, 2)
  state.edit.group_id = '7'
  state.chooseGroup()
  await state.saveSuggestion()
  const writes = calls.filter((c) => c.options?.method)
  assert.equal(writes.length, 1)
  assert.equal(writes[0].options.method, 'PATCH')
  assert.deepEqual(JSON.parse(writes[0].options.body), {
    revision: 2,
    category_id: 6,
    group_id: 7,
    group_name: '抖音视频点赞',
  })
  assert.equal(
    calls.some((c) => c.path.endsWith('/apply')),
    false,
  )
})

test('permission failure clears overview, suggestions, applications and reports', async () => {
  const { state } = component(
    'pages/AIAnalysis.vue',
    async () => {
      throw new Error('无权访问')
    },
    { admin: true },
  )
  state.overview.value = summary()
  state.suggestions.value = [{ id: 1 }]
  state.applications.value = [{ id: 2 }]
  state.report.value = { id: 3 }
  await state.load()
  assert.equal(state.overview.value, null)
  assert.equal(state.suggestions.value.length, 0)
  assert.equal(state.applications.value.length, 0)
  assert.equal(state.report.value, null)
})

test('filters and tab polling only read APIs, and stop retry undo require explicit confirmation', async () => {
  const calls = []
  const { state } = component(
    'pages/AIAnalysis.vue',
    async (path, options) => {
      calls.push({ path, options })
      return path === '/ai-analysis/overview' ? summary() : { items: [], total: 0 }
    },
    { admin: true },
  )
  state.tab.value = 'reports'
  state.filters.status = 'partial'
  state.filters.platform_id = '2'
  state.filters.date_from = '2026-10-01'
  await state.load()
  assert.ok(
    calls.some(
      (c) =>
        c.path.includes('status=partial') &&
        c.path.includes('platform_id=2') &&
        c.path.includes('date_from=2026-10-01'),
    ),
  )
  state.ask('stop', { id: 4 })
  assert.equal(calls.filter((c) => c.options?.method).length, 0)
  await state.confirmAction()
  assert.ok(calls.some((c) => c.path === '/ai-analysis/runs/4/stop'))
  state.ask('undo', { id: 5 })
  await state.confirmAction()
  assert.ok(calls.some((c) => c.path === '/ai-analysis/applications/5/undo'))
})

test('new configuration limits and automatic apply are explicit saved fields', async () => {
  const calls = []
  const current = {
    enabled: false,
    base_url: 'https://ai.example.org/v1',
    model: 'fixture',
    daily_time: '09:00',
    proxy_route: 'direct',
    sample_limit: 100,
    json_mode: true,
    daily_product_limit: 1000,
    daily_request_limit: 20,
    auto_apply: false,
    key_configured: true,
    configured: true,
  }
  const { state } = component(
    'components/AIConfigPanel.vue',
    async (path, options) => {
      calls.push({ path, options })
      return current
    },
    { admin: true },
  )
  await state.load()
  assert.equal(state.form.auto_apply, false)
  state.form.auto_apply = true
  state.form.daily_product_limit = 500
  state.form.daily_request_limit = 10
  await state.save()
  const payload = JSON.parse(calls[1].options.body)
  assert.equal(payload.auto_apply, true)
  assert.equal(payload.daily_product_limit, 500)
  assert.equal(payload.daily_request_limit, 10)
  assert.equal(Object.hasOwn(payload, 'key'), false)
})

const diagnosticConfig = () => ({
  enabled: false,
  base_url: 'https://ai.example.org/v1',
  model: 'fixture',
  daily_time: '09:00',
  proxy_route: 'direct',
  sample_limit: 100,
  json_mode: true,
  daily_product_limit: 1000,
  daily_request_limit: 20,
  auto_apply: false,
  key_configured: true,
  configured: true,
  revision: 7,
})
const connectedDiagnostic = () => ({
  status: 'connected',
  authenticated: true,
  model_available: true,
  models: ['fixture', 'other-model'],
  models_truncated: false,
  latency_ms: 42,
  checked_at: '2026-10-27T08:00:00Z',
  config_revision: 7,
  message: '已连接，当前模型在列表中。',
})

test('connection check uses saved configuration only and never submits model inference', async () => {
  const calls = []
  const { state } = component(
    'components/AIConfigPanel.vue',
    async (path, options) => {
      calls.push({ path, options })
      return path === '/ai-analysis/config' ? diagnosticConfig() : connectedDiagnostic()
    },
    { admin: true },
  )
  await state.load()
  assert.equal(state.canCheckConnection.value, true)
  await state.checkConnection()
  assert.equal(state.diagnostic.value.status, 'connected')
  assert.equal(state.diagnosticClass.value, 'success')
  assert.equal(state.form.model, 'fixture')
  const writes = calls.filter((c) => c.options?.method)
  assert.equal(writes.length, 1)
  assert.equal(writes[0].path, '/ai-analysis/connection-check')
  assert.deepEqual(JSON.parse(writes[0].options.body), {})
  assert.equal(
    calls.some((c) => c.path === '/ai-analysis/run'),
    false,
  )
  state.form.model = 'not-yet-saved'
  assert.equal(state.diagnostic.value, null)
  assert.equal(state.canCheckConnection.value, false)
  await state.checkConnection()
  assert.equal(calls.length, 2)
})

test('connection check discards a late response after form changes even if draft is restored', async () => {
  let finish
  const pending = new Promise((resolve) => (finish = resolve))
  const { state } = component(
    'components/AIConfigPanel.vue',
    async (path) => (path === '/ai-analysis/config' ? diagnosticConfig() : pending),
    { admin: true },
  )
  await state.load()
  const request = state.checkConnection()
  assert.equal(state.testing.value, true)
  state.form.key = 'unsaved-private-fixture'
  state.form.key = ''
  finish(connectedDiagnostic())
  await request
  assert.equal(state.testing.value, false)
  assert.equal(state.diagnostic.value, null)
})

test('connection result rejects wrong revision and filters secret-shaped model identifiers', async () => {
  let result = { ...connectedDiagnostic(), config_revision: 6 }
  const { state } = component(
    'components/AIConfigPanel.vue',
    async (path) => (path === '/ai-analysis/config' ? diagnosticConfig() : result),
    { admin: true },
  )
  await state.load()
  await state.checkConnection()
  assert.equal(state.diagnostic.value, null)
  result = {
    ...connectedDiagnostic(),
    status: 'warning',
    model_available: false,
    models: [
      'fixture',
      'fixture',
      'sk-private-fixture',
      'github_pat_fixture',
      'bad\nmodel',
      'allowed-model',
    ],
  }
  await state.checkConnection()
  assert.equal(state.diagnosticClass.value, 'warning')
  assert.deepEqual(Array.from(state.diagnostic.value.models), ['fixture', 'allowed-model'])
  assert.equal(JSON.stringify(state.diagnostic.value).includes('private-fixture'), false)
  state.resetForm()
  assert.equal(state.diagnostic.value, null)
})

test('latest provider issue explains old generic errors without automatically retrying', async () => {
  const calls = []
  const issue = {
    id: 2,
    error_code: 'AI_RATE_LIMITED',
    message: 'AI 服务暂时限流',
    action_hint: '请核对服务商状态后手动重试',
    created_at: '2026-10-27T08:00:00Z',
  }
  const { state } = component(
    'pages/AIAnalysis.vue',
    async (path, options) => {
      calls.push({ path, options })
      if (path === '/ai-analysis/overview') return { ...summary(), last_issue: issue }
      if (path === '/ai-analysis/runs/2')
        return {
          id: 2,
          status: 'failed',
          error_code: 'AI_RATE_LIMITED',
          message: '批次处理结束',
          report: {},
        }
      return { items: [], total: 0 }
    },
    { admin: true },
  )
  await state.load()
  assert.equal(state.lastIssue.value.message, issue.message)
  await state.view(state.lastIssue.value)
  assert.equal(state.reportIssue.value.action_hint, issue.action_hint)
  assert.equal(
    calls.some((c) => c.options?.method),
    false,
  )
})
