import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { test } from 'node:test'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const transpile = (source) =>
  ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
const treeModule = { exports: {} }
vm.runInNewContext(
  transpile(readFileSync(new URL('../src/categoryTree.ts', import.meta.url), 'utf8')),
  { module: treeModule, exports: treeModule.exports },
)

// Run the real management page, replacing only external I/O and lifecycle.
function page(api = async () => ({ items: [] }), { role = 'admin' } = {}) {
  const filename = new URL('../src/pages/ComparisonCategories.vue', import.meta.url)
  const { descriptor } = parse(readFileSync(filename, 'utf8'))
  const compiled = compileScript(descriptor, { id: 'category-admin-test' })
  const module = { exports: {} }
  const mounted = []
  const unmount = []
  vm.runInNewContext(transpile(compiled.content), {
    module,
    exports: module.exports,
    AbortController,
    setTimeout,
    clearTimeout,
    require: (id) => {
      if (id === 'vue')
        return {
          ...vue,
          onMounted: (callback) => mounted.push(callback),
          onBeforeUnmount: (callback) => unmount.push(callback),
        }
      if (id === 'vue-router')
        return {
          useRoute: () => ({ path: '/admin/comparison-categories', meta: { admin: true } }),
          useRouter: () => ({ replace: async () => {} }),
        }
      if (id === '../api')
        return {
          api,
          auth: vue.reactive({ user: { id: 1, role } }),
          itemsOf: (value) => value.items ?? [],
          json: (method, body) => ({ method, body: JSON.stringify(body) }),
        }
      if (id === '../categoryTree') return treeModule.exports
      if (id.endsWith('.vue')) return { default: {} }
      throw new Error(`Unexpected management dependency: ${id}`)
    },
  })
  const state = module.exports.default.setup({}, { expose: () => {} })
  const template = compileTemplate({
    id: 'category-admin-test',
    filename: filename.pathname,
    source: descriptor.template.content,
    compilerOptions: { bindingMetadata: compiled.bindings },
  })
  assert.deepEqual(template.errors, [])
  const renderer = { exports: {} }
  vm.runInNewContext(transpile(template.code), {
    module: renderer,
    exports: renderer.exports,
    require: (id) => {
      assert.equal(id, 'vue')
      return { ...vue, resolveComponent: (name) => name, withDirectives: (node) => node }
    },
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
    mount: () => Promise.all(mounted.map((callback) => callback())),
    unmount: () => unmount.forEach((callback) => callback()),
    renderText: () => text(renderer.exports.render({}, [], {}, vue.proxyRefs(state), {}, {})),
  }
}

function fixture() {
  const root = {
    id: 1,
    name: '抖音',
    parent_id: null,
    sort_order: 10,
    description: '业务说明',
    product_count: 2,
    direct_product_count: 0,
    children_count: 1,
  }
  const video = {
    id: 2,
    name: '视频',
    parent_id: 1,
    sort_order: 20,
    description: '保留视频说明',
    product_count: 2,
    direct_product_count: 1,
    children_count: 1,
  }
  const likes = {
    id: 3,
    name: '点赞',
    parent_id: 2,
    sort_order: 30,
    description: '点赞说明',
    product_count: 1,
    direct_product_count: 1,
    children_count: 0,
  }
  const other = {
    id: 4,
    name: '快手',
    parent_id: null,
    sort_order: 40,
    description: '另一业务',
    product_count: 0,
    direct_product_count: 0,
    children_count: 1,
  }
  const otherVideo = {
    id: 5,
    name: '视频',
    parent_id: 4,
    sort_order: 50,
    description: '',
    product_count: 0,
    direct_product_count: 0,
    children_count: 0,
  }
  return [root, video, likes, other, otherVideo]
}

function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

function catalogue(initial = fixture()) {
  let rows = initial.map((item) => ({ ...item }))
  const calls = []
  return {
    calls,
    api: async (path, options = {}) => {
      const method = options.method ?? 'GET'
      const body = options.body ? JSON.parse(options.body) : undefined
      calls.push({ path, method, body })
      if (method === 'GET') {
        assert.equal(path, '/comparison-categories')
        return { items: rows.map((item) => ({ ...item })) }
      }
      if (method === 'POST') {
        assert.equal(path, '/comparison-categories')
        const created = { id: 20, product_count: 0, children_count: 0, ...body }
        rows.push(created)
        return created
      }
      const id = Number(path.split('/').at(-1))
      if (method === 'PUT') {
        const item = rows.find((row) => row.id === id)
        assert.ok(item)
        Object.assign(item, body)
        return { ...item }
      }
      assert.equal(method, 'DELETE')
      rows = rows.filter((row) => row.id !== id)
      return { ok: true }
    },
  }
}

test('independent category page creates roots and children with explicit parent IDs', async () => {
  const store = catalogue()
  const { state } = page(store.api)
  await state.load()
  state.openCreate()
  assert.equal(state.showForm.value, true)
  state.form.name = ' 新业务 '
  state.form.description = '一级分类说明'
  state.form.sort_order = -7
  await state.save()
  let written = store.calls.find((call) => call.method === 'POST')
  assert.deepEqual(written.body, {
    name: '新业务',
    description: '一级分类说明',
    sort_order: -7,
    parent_id: null,
  })
  assert.equal(state.showForm.value, false)
  store.calls.length = 0
  state.openCreate(state.categories.value.find((item) => item.id === 4))
  assert.equal(state.form.parent_id, '4')
  state.form.name = '直播'
  await state.save()
  written = store.calls.find((call) => call.method === 'POST')
  assert.equal(written.body.parent_id, 4)
  assert.equal(written.body.name, '直播')
})

test('editing copies category fields and numeric ordering without changing local rows early', async () => {
  const store = catalogue()
  const { state } = page(store.api)
  await state.load()
  const original = state.categories.value.find((item) => item.id === 2)
  state.openEdit(original)
  state.form.name = '视频互动'
  state.form.description = '修改后的说明'
  state.form.sort_order = 8
  assert.equal(original.name, '视频')
  assert.equal(original.description, '保留视频说明')
  await state.save()
  const written = store.calls.find((call) => call.method === 'PUT')
  assert.equal(written.path, '/comparison-categories/2')
  assert.deepEqual(written.body, {
    name: '视频互动',
    description: '修改后的说明',
    sort_order: 8,
    parent_id: 1,
  })
  state.openSort(state.categories.value.find((item) => item.id === 2))
  state.form.sort_order = 1.5
  const writes = store.calls.filter((call) => call.method === 'PUT').length
  await state.save()
  assert.equal(store.calls.filter((call) => call.method === 'PUT').length, writes)
  assert.match(state.formError.value, /整数/)
  state.form.sort_order = -99
  await state.save()
  const ordered = store.calls.filter((call) => call.method === 'PUT').at(-1)
  assert.equal(ordered.body.sort_order, -99)
  assert.equal(ordered.body.name, '视频互动')
  assert.equal(ordered.body.description, '修改后的说明')
  assert.equal(ordered.body.parent_id, 1)
})

test('move choices exclude self and descendants and keep the complete category metadata', async () => {
  const store = catalogue()
  const { state } = page(store.api)
  await state.load()
  state.openMove(state.categories.value.find((item) => item.id === 2))
  const ids = Array.from(state.parentOptions.value, (item) => item.id)
  assert.ok(!ids.includes(2) && !ids.includes(3))
  assert.ok(ids.includes(4))
  state.form.parent_id = '3'
  await state.save()
  assert.equal(store.calls.filter((call) => call.method === 'PUT').length, 0)
  assert.match(state.formError.value, /自身|后代/)
  state.form.parent_id = '4'
  await state.save()
  const moved = store.calls.find((call) => call.method === 'PUT')
  assert.deepEqual(moved.body, {
    name: '视频',
    description: '保留视频说明',
    sort_order: 20,
    parent_id: 4,
  })
  state.openMove(state.categories.value.find((item) => item.id === 2))
  state.form.parent_id = ''
  await state.save()
  assert.equal(store.calls.filter((call) => call.method === 'PUT').at(-1).body.parent_id, null)
})

test('an API save rejection keeps the dialog, entered values and original rows', async () => {
  const calls = []
  const { state } = page(async (path, options = {}) => {
    calls.push({ path, method: options.method ?? 'GET' })
    if (!options.method) return { items: fixture() }
    throw new Error('同一父类目下已存在此名称')
  })
  await state.load()
  state.openEdit(state.categories.value.find((item) => item.id === 2))
  state.form.name = '重复名称'
  await state.save()
  assert.equal(state.showForm.value, true)
  assert.equal(state.form.name, '重复名称')
  assert.equal(state.categories.value.find((item) => item.id === 2).name, '视频')
  assert.match(state.formError.value, /已存在/)
  assert.equal(state.saving.value, false)
  assert.equal(calls.filter((call) => call.method === 'PUT').length, 1)
})

test('nonempty categories cannot send deletes and empty-category deletion refreshes the tree', async () => {
  const store = catalogue()
  const { state } = page(store.api)
  await state.load()
  state.confirmRemove(state.categories.value.find((item) => item.id === 1))
  assert.equal(state.deletingCategory.value, null)
  // Revalidate the current snapshot even if a stale confirmation was already open.
  state.deletingCategory.value = state.categories.value.find((item) => item.id === 3)
  await state.remove()
  assert.equal(store.calls.filter((call) => call.method === 'DELETE').length, 0)
  assert.match(state.deleteError.value, /子类目|商品/)
  state.confirmRemove(state.categories.value.find((item) => item.id === 5))
  assert.equal(state.deletingCategory.value.id, 5)
  await state.remove()
  assert.equal(
    store.calls.filter((call) => call.method === 'DELETE')[0].path,
    '/comparison-categories/5',
  )
  assert.equal(state.deletingCategory.value, null)
  assert.ok(!state.categories.value.some((item) => item.id === 5))
})

test('viewers neither load the management directory nor submit mutations', async () => {
  const store = catalogue()
  const { state } = page(store.api, { role: 'viewer' })
  await state.load()
  assert.equal(store.calls.length, 0)
  assert.match(state.error.value, /管理员权限/)
  state.openCreate()
  state.openEdit(fixture()[1])
  state.openMove(fixture()[1])
  state.openSort(fixture()[1])
  assert.equal(state.showForm.value, false)
  state.showForm.value = true
  state.form.name = 'must not save'
  await state.save()
  state.deletingCategory.value = fixture()[4]
  await state.remove()
  assert.equal(store.calls.length, 0)
})

test('search exposes matching full paths with ancestors while collapse preserves selected IDs', async () => {
  const store = catalogue()
  const { state, renderText } = page(store.api)
  await state.load()
  state.collapseAll()
  assert.deepEqual(
    Array.from(state.visibleRows.value, (item) => item.id),
    [1, 4],
  )
  state.query.value = '快手 / 视频'
  assert.deepEqual(
    Array.from(state.visibleRows.value, (item) => item.id),
    [4, 5],
  )
  const text = renderText()
  assert.match(text, /快手 \/ 视频/)
  assert.doesNotMatch(text, /抖音 \/ 视频/)
  state.query.value = ''
  state.expandAll()
  assert.deepEqual(
    Array.from(state.visibleRows.value, (item) => item.id),
    [1, 2, 3, 4, 5],
  )
})

test('late directory responses cannot restore data after navigation or replace a newer load', async () => {
  const old = deferred()
  const latest = deferred()
  const requests = [old, latest]
  const { state, unmount } = page(() => requests.shift().promise)
  const first = state.load()
  const second = state.load()
  latest.resolve({ items: [fixture()[3]] })
  await second
  old.resolve({ items: fixture() })
  await first
  assert.deepEqual(
    Array.from(state.categories.value, (item) => item.id),
    [4],
  )
  const pending = deferred()
  const navigated = page(() => pending.promise)
  const request = navigated.state.load()
  navigated.unmount()
  pending.resolve({ items: fixture() })
  await request
  assert.equal(navigated.state.categories.value.length, 0)
  unmount()
})

test('duplicate save clicks share one write and saving prevents the dialog from closing early', async () => {
  const response = deferred()
  let writes = 0
  const { state } = page(async (path, options = {}) => {
    if (!options.method) return { items: fixture() }
    writes++
    return response.promise
  })
  await state.load()
  state.openEdit(state.categories.value.find((item) => item.id === 2))
  const first = state.save()
  await state.save()
  state.closeForm()
  assert.equal(writes, 1)
  assert.equal(state.showForm.value, true)
  response.resolve({ id: 2 })
  await first
  assert.equal(state.showForm.value, false)
  assert.equal(state.saving.value, false)
})

test('the eight-level guard prevents creating children below the final level', async () => {
  const chain = Array.from({ length: 8 }, (_, index) => ({
    id: index + 1,
    name: `级别${index + 1}`,
    parent_id: index ? index : null,
    sort_order: index,
    children_count: index < 7 ? 1 : 0,
    product_count: 0,
  }))
  const { state } = page(catalogue(chain).api)
  await state.load()
  assert.equal(state.canAddChild(state.categories.value[7]), false)
  state.openCreate(state.categories.value[7])
  assert.equal(state.showForm.value, false)
  state.openMove(state.categories.value[5])
  assert.ok(!state.parentOptions.value.some((item) => item.id >= 6))
})
