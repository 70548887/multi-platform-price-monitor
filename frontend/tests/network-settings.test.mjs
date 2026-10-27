import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { test } from 'node:test'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')

function component(api) {
  const { descriptor } = parse(
    readFileSync(new URL('../src/pages/NetworkSettings.vue', import.meta.url), 'utf8'),
  )
  const script = compileScript(descriptor, { id: 'network-settings-test' }).content
  const code = ts.transpileModule(script, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(code, {
    module,
    exports: module.exports,
    AbortController,
    URL,
    require: (id) => {
      if (id === 'vue') return { ...vue, onMounted: () => {}, onBeforeUnmount: () => {} }
      if (id === 'vue-router') return { useRoute: () => ({ query: {} }) }
      if (id === '../api')
        return {
          api,
          auth: vue.reactive({ user: { role: 'admin' } }),
          date: (value) => value,
          json: (method, body) => ({ method, body: JSON.stringify(body) }),
        }
      if (id.endsWith('.vue')) return { default: {} }
      if (id === '../settings.css') return {}
      throw new Error('Unexpected import ' + id)
    },
  })
  return module.exports.default.setup({}, { expose: () => {} })
}

const configuration = () => ({
  tunnel: { enabled: false, configured: true },
  overseas: { enabled: true, configured: true },
  updated_at: '2026-10-27T01:00:00Z',
})

test('network configuration cannot save after loading fails', async () => {
  const calls = []
  const state = component(async (path, options) => {
    calls.push({ path, options })
    throw new Error('读取网络设置失败')
  })
  await state.load()
  assert.equal(state.state.value, null)
  assert.equal(state.loading.value, false)
  state.forms.tunnel.url = 'https://proxy.example.org:8080'
  await state.save('tunnel')
  assert.equal(calls.length, 1)
  assert.equal(state.error.value, '读取网络设置失败')
})

test('failed proxy saves retain both proxy drafts and allow a retry', async () => {
  const calls = []
  let failSave = true
  const state = component(async (path, options) => {
    calls.push({ path, options })
    if (options?.method === 'PUT' && failSave) throw new Error('网络请求失败')
    return configuration()
  })
  await state.load()
  state.forms.tunnel.url = 'https://tunnel.example.org:8080'
  state.forms.tunnel.enabled = true
  state.forms.overseas.url = 'https://overseas.example.org:8080'
  state.forms.overseas.enabled = false
  await state.save('tunnel')
  assert.equal(state.forms.tunnel.url, 'https://tunnel.example.org:8080')
  assert.equal(state.forms.tunnel.enabled, true)
  assert.equal(state.forms.overseas.url, 'https://overseas.example.org:8080')
  assert.equal(state.forms.overseas.enabled, false)
  assert.equal(state.busy.value, '')
  assert.equal(state.error.value, '网络请求失败')
  failSave = false
  await state.save('tunnel')
  assert.equal(state.forms.tunnel.url, '')
  assert.equal(state.forms.overseas.url, 'https://overseas.example.org:8080')
  assert.equal(state.forms.overseas.enabled, false)
  assert.equal(calls.filter((call) => call.options?.method === 'PUT').length, 2)
})

test('successful proxy saves clear only the saved card and submit only its values', async () => {
  const calls = []
  const state = component(async (path, options) => {
    calls.push({ path, options })
    return configuration()
  })
  await state.load()
  state.forms.tunnel.url = 'https://tunnel.example.org:8080'
  state.forms.tunnel.enabled = true
  state.forms.overseas.url = 'https://overseas.example.org:8080'
  state.forms.overseas.enabled = false
  await state.save('tunnel')
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    tunnel: { enabled: true, url: 'https://tunnel.example.org:8080' },
  })
  assert.equal(state.forms.tunnel.url, '')
  assert.equal(state.forms.tunnel.enabled, false)
  assert.equal(state.forms.overseas.url, 'https://overseas.example.org:8080')
  assert.equal(state.forms.overseas.enabled, false)
})

test('pending proxy saves reject duplicate submissions until the response arrives', async () => {
  let resolveSave
  const pendingSave = new Promise((resolve) => (resolveSave = resolve))
  const calls = []
  const state = component(async (path, options) => {
    calls.push({ path, options })
    return options?.method === 'PUT' ? pendingSave : configuration()
  })
  await state.load()
  state.forms.tunnel.url = 'https://tunnel.example.org:8080'
  const first = state.save('tunnel')
  await state.save('tunnel')
  await state.save('overseas')
  assert.equal(calls.filter((call) => call.options?.method === 'PUT').length, 1)
  assert.equal(state.busy.value, 'tunnel')
  resolveSave(configuration())
  await first
  assert.equal(state.busy.value, '')
})
