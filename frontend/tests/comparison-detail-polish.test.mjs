import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { test } from 'node:test'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const filename = new URL('../src/pages/ComparisonDetail.vue', import.meta.url)
const { descriptor } = parse(readFileSync(filename, 'utf8'))
const compiled = compileScript(descriptor, { id: 'comparison-detail-polish' })
const transpile = (source) =>
  ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText

function page({ admin = true, role = 'admin', api = async () => ({}) } = {}) {
  const module = { exports: {} }
  vm.runInNewContext(transpile(compiled.content), {
    module,
    exports: module.exports,
    AbortController,
    require: (id) => {
      if (id === 'vue') return { ...vue, onMounted: () => {}, onBeforeUnmount: () => {} }
      if (id === 'vue-router') return { useRoute: () => ({ params: { id: '6' }, meta: { admin } }) }
      if (id === '../api')
        return {
          api,
          auth: vue.reactive({ user: { role } }),
          date: (value) => value,
          price: (value, currency) => `${currency} ${value}`,
          displayBasisLabel: (quantity, unit) => `${quantity} ${unit || '单位待核验'}`,
          qualityText: (values) => values.join('、'),
        }
      if (id === '../navigation')
        return { areaPath: (path, area) => (area ? '/admin' + path : path) }
      if (id.endsWith('.vue')) return { default: {} }
      throw new Error(`Unexpected dependency: ${id}`)
    },
  })
  const state = module.exports.default.setup({}, { expose: () => {} })
  const template = compileTemplate({
    id: 'comparison-detail-polish',
    filename: filename.pathname,
    source: descriptor.template.content,
    compilerOptions: { bindingMetadata: compiled.bindings },
  })
  assert.deepEqual(template.errors, [])
  const renderer = { exports: {} }
  vm.runInNewContext(transpile(template.code), {
    module: renderer,
    exports: renderer.exports,
    require: () => ({ ...vue, resolveComponent: (name) => name, withDirectives: (node) => node }),
  })
  function text(node) {
    if (typeof node === 'string') return node
    if (Array.isArray(node)) return node.map(text).join(' ')
    if (!node || typeof node !== 'object') return ''
    if (node.children?.default) return text(node.children.default())
    return text(node.children)
  }
  return {
    state,
    renderText: () => text(renderer.exports.render({}, [], {}, vue.proxyRefs(state), {}, {})),
  }
}

function fixture() {
  const common = {
    category_name: '互动',
    source_id: 'fixture',
    unit: '',
    status: 'active',
    freshness: 'fresh',
    quality: [],
    last_observed_at: '2026-10-26T12:00:00Z',
    display_quantity: '1',
    comparison_eligible: false,
    comparison_reason: '相似商品仅展示源站报价，规格和计价口径需人工核对',
  }
  return {
    group: {
      id: 6,
      name: '抖音视频点赞',
      match_mode: 'similar',
      description: '逐个平台核对服务条件',
      category_id: 2,
      category_name: '视频',
      category_path: [
        { id: 1, name: '抖音' },
        { id: 2, name: '视频' },
      ],
    },
    items: [
      {
        ...common,
        id: 11,
        platform_id: 1,
        platform_name: '平台一',
        name: '精度报价',
        currency: 'CNY',
        price: '0.000000000000000001',
        display_price: '0.000000000000000001',
      },
      {
        ...common,
        id: 22,
        platform_id: 2,
        platform_name: '平台二',
        name: '美元报价',
        currency: 'USD',
        price: '0.007486',
        display_price: '0.007486',
      },
    ],
    comparable: false,
    reason: '相似参考',
    eligible_product_ids: [],
    lowest_product_ids: [11],
  }
}

test('similar detail shortens generic notes, keeps precise quotes and filters platforms', () => {
  const { state, renderText } = page()
  state.comparison.value = fixture()
  state.loading.value = false
  assert.equal(state.offerNote(state.comparison.value.items[0]), '同类参考')
  assert.equal(state.lowestIds.value.size, 0)
  const rendered = renderText()
  assert.match(rendered, /CNY 0\.000000000000000001/)
  assert.match(rendered, /USD 0\.007486/)
  assert.doesNotMatch(rendered, /相似商品仅展示源站报价，规格和计价口径需人工核对/)
  assert.match(rendered, /抖音/)
  assert.match(rendered, /视频/)
  state.selectedPlatform.value = '2'
  assert.equal(state.visibleOffers.value.length, 1)
  assert.equal(state.visibleOffers.value[0].platform_id, 2)
  state.clearFilters()
  assert.equal(state.visibleOffers.value.length, 2)
})

test('only server-confirmed comparable quotes receive lowest badges', () => {
  const { state } = page()
  const data = fixture()
  data.group.match_mode = 'exact'
  data.comparable = true
  data.items[0].comparison_reason = '计价单位或币种不同'
  state.comparison.value = data
  assert.equal(state.lowestIds.value.has(11), true)
  assert.equal(state.lowestIds.value.has(22), false)
  assert.equal(state.offerNote(data.items[0]), '计价单位或币种不同')
})

test('unlinked product provides an administrator association action and no quote filters', () => {
  const { state, renderText } = page()
  state.comparison.value = { ...fixture(), items: [] }
  state.loading.value = false
  const rendered = renderText()
  assert.match(rendered, /商品已建立，等待关联报价/)
  assert.match(rendered, /关联平台商品/)
  assert.doesNotMatch(rendered, /全部交易平台/)
  assert.doesNotMatch(rendered, /管理关联商品/)
  const viewer = page({ admin: false, role: 'viewer' })
  viewer.state.comparison.value = { ...fixture(), items: [] }
  viewer.state.loading.value = false
  assert.doesNotMatch(viewer.renderText(), /关联平台商品/)
  assert.match(viewer.renderText(), /管理员关联商品并授权对应平台/)
})

test('failed access refresh clears old quotes from the detail', async () => {
  const { state, renderText } = page({
    api: async () => {
      throw new Error('记录不存在')
    },
  })
  state.comparison.value = fixture()
  await state.load()
  assert.equal(state.comparison.value, null)
  assert.equal(state.error.value, '记录不存在')
  assert.doesNotMatch(renderText(), /精度报价|美元报价|0\.007486/)
})

test('refresh resets a selected platform removed from the authorized response', async () => {
  const result = fixture()
  result.items = result.items.filter((offer) => offer.platform_id === 1)
  const { state } = page({ api: async () => result })
  state.comparison.value = fixture()
  state.selectedPlatform.value = '2'
  assert.equal(state.visibleOffers.value[0].platform_id, 2)
  await state.load()
  assert.equal(state.selectedPlatform.value, '')
  assert.equal(state.visibleOffers.value.length, 1)
  assert.equal(state.visibleOffers.value[0].platform_id, 1)
})
