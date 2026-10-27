import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { test } from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
function harness() {
  const module = { exports: {} }
  let reloads = 0,
    routerHandler,
    removed = false
  const listeners = new Map()
  const target = {
    location: { reload: () => reloads++ },
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: (name) => listeners.delete(name),
  }
  const code = ts.transpileModule(
    readFileSync(new URL('../src/assetRecovery.ts', import.meta.url), 'utf8'),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
  ).outputText
  vm.runInNewContext(code, {
    module,
    exports: module.exports,
    require: (id) => {
      if (id === 'vue') return require('vue')
      throw Error(id)
    },
    window: target,
  })
  const cleanup = module.exports.installAssetRecovery(target, {
    onError: (handler) => {
      routerHandler = handler
      return () => (removed = true)
    },
  })
  return {
    state: module.exports,
    target,
    listeners,
    cleanup,
    routerError: (error) => routerHandler(error),
    reloads: () => reloads,
    routerRemoved: () => removed,
  }
}

test('Vite preload error preserves current state until explicit refresh and retains no payload', () => {
  const h = harness()
  let prevented = false
  const draft = { model: 'my-unsaved-model', key: 'unsaved-fixture' }
  h.listeners.get('vite:preloadError')({
    preventDefault: () => (prevented = true),
    payload: new Error('https://host/assets/private.js?token=fixture-secret'),
  })
  assert.equal(prevented, true)
  assert.equal(h.state.assetLoadFailed.value, true)
  assert.equal(h.reloads(), 0)
  assert.deepEqual(draft, { model: 'my-unsaved-model', key: 'unsaved-fixture' })
  assert.equal(h.state.assetRecoveryMessage.includes('fixture-secret'), false)
  h.state.refreshAfterAssetFailure()
  assert.equal(h.reloads(), 1)
})

test('router resource failures show the same notice, normal business errors do not', () => {
  const h = harness()
  for (const message of ['权限不足', 'HTTP 500', 'request timed out']) {
    h.routerError(new Error(message))
    assert.equal(h.state.assetLoadFailed.value, false)
  }
  for (const message of [
    'Failed to fetch dynamically imported module: https://host/assets/missing.js',
    'Unable to preload CSS for /assets/missing.css',
    'Importing a module script failed.',
    'Loading chunk 117 failed.',
  ]) {
    h.state.assetLoadFailed.value = false
    h.routerError(new Error(message))
    assert.equal(h.state.assetLoadFailed.value, true)
  }
  assert.equal(h.reloads(), 0)
  h.cleanup()
  assert.equal(h.listeners.size, 0)
  assert.equal(h.routerRemoved(), true)
})

test('refresh does nothing without an observed resource failure', () => {
  const h = harness()
  h.state.refreshAfterAssetFailure()
  assert.equal(h.reloads(), 0)
})
