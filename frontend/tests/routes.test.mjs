import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import { test } from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vueRouter = require('vue-router')

function compile(relative, dependencies) {
  const output = ts.transpileModule(readFileSync(new URL(relative, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(output, {
    module,
    exports: module.exports,
    require: dependencies,
    document: { title: '' },
  })
  return module.exports
}

const navigation = compile('../src/navigation.ts', () => {
  throw new Error('Unexpected dependency')
})
function app(role) {
  const auth = { initialized: true, user: role ? { id: 1, role } : null }
  const { router } = compile('../src/router.ts', (id) => {
    if (id === './api') return { auth, loadUser: async () => {} }
    if (id === './navigation') return navigation
    if (id === 'vue-router')
      return {
        createWebHistory: vueRouter.createMemoryHistory,
        createRouter: (options) =>
          vueRouter.createRouter({
            ...options,
            routes: options.routes.map((route) =>
              route.component ? { ...route, component: { render: () => null } } : route,
            ),
          }),
      }
    throw new Error(`Unexpected dependency: ${id}`)
  })
  return { router, auth }
}

test('every management page and legacy bookmark rejects a viewer', async () => {
  const { router } = app('viewer')
  for (const path of [
    '/admin',
    '/admin/products',
    '/admin/products/7',
    '/admin/platforms',
    '/admin/platforms/2/rules',
    '/admin/runs',
    '/admin/events',
    '/admin/compare',
    '/admin/compare/7',
    '/admin/comparison-categories',
    '/admin/platform-submissions',
    '/admin/ai-analysis',
    '/admin/mappings',
    '/admin/mappings/7',
    '/admin/users',
    '/admin/settings/network',
    '/admin/settings/update',
    '/users',
    '/platforms',
    '/platforms/2/rules',
    '/runs',
    '/settings/network',
    '/settings/update',
  ]) {
    await router.push(path)
    assert.equal(router.currentRoute.value.path, '/', path)
  }
})

test('anonymous management navigation uses the admin login and retains the destination', async () => {
  const { router } = app()
  await router.push('/users?focus=4#members')
  assert.equal(router.currentRoute.value.path, '/admin/login')
  assert.equal(router.currentRoute.value.query.redirect, '/admin/users?focus=4#members')
  await router.push('/products/7')
  assert.equal(router.currentRoute.value.path, '/login')
  assert.equal(router.currentRoute.value.query.redirect, '/products/7')
})

test('administrator bookmarks and page areas remain separate', async () => {
  const { router } = app('admin')
  await router.push('/settings/update')
  assert.equal(router.currentRoute.value.path, '/admin/settings/update')
  assert.equal(router.currentRoute.value.meta.admin, true)
  await router.push('/products/7')
  assert.equal(router.currentRoute.value.meta.admin, undefined)
  await router.push('/admin/login')
  assert.equal(router.currentRoute.value.path, '/admin')
})

test('member login from the admin entry never lands in management', async () => {
  const { router } = app('viewer')
  await router.push('/admin/login?redirect=/admin/users')
  assert.equal(router.currentRoute.value.path, '/')
  await router.push('/admin/login?redirect=/users')
  assert.equal(router.currentRoute.value.path, '/')
})

test('login redirects reject external, control and login-loop destinations', () => {
  for (const value of [
    'https://outside.example/',
    '//outside.example/',
    '/\\outside.example',
    '/\noutside',
    '/login',
    '/admin/login',
    null,
    ['/products'],
  ]) {
    assert.equal(navigation.loginDestination(value, 'admin', true), '/admin', String(value))
  }
  assert.equal(
    navigation.loginDestination('/products?platform=2#prices', 'viewer', false),
    '/products?platform=2#prices',
  )
  assert.equal(navigation.loginDestination('/admin/platforms', 'viewer', false), '/')
})

test('global comparison routes keep legacy links and details in the same area', async () => {
  const { router } = app('admin')
  await router.push('/mappings?category=4#offers')
  assert.equal(router.currentRoute.value.fullPath, '/compare?category=4#offers')
  assert.equal(router.currentRoute.value.meta.title, '全网比价')
  await router.push('/admin/mappings/19?platform=2')
  assert.equal(router.currentRoute.value.fullPath, '/admin/compare/19?platform=2')
  assert.equal(router.currentRoute.value.meta.admin, true)
  await router.push('/compare/19')
  assert.equal(router.currentRoute.value.meta.admin, undefined)
})

test('category management has its own administrator route without replacing comparison', async () => {
  const { router } = app('admin')
  await router.push('/admin/comparison-categories')
  assert.equal(router.currentRoute.value.path, '/admin/comparison-categories')
  assert.equal(router.currentRoute.value.meta.admin, true)
  assert.equal(router.currentRoute.value.meta.title, '比价类目管理')
  await router.push('/admin/compare')
  assert.equal(router.currentRoute.value.meta.title, '全网比价')
  const anonymous = app().router
  await anonymous.push('/admin/comparison-categories')
  assert.equal(anonymous.currentRoute.value.path, '/admin/login')
  assert.equal(anonymous.currentRoute.value.query.redirect, '/admin/comparison-categories')
})

test('members can submit platforms while review remains an administrator page', async () => {
  const member = app('viewer').router
  await member.push('/platform-submissions')
  assert.equal(member.currentRoute.value.path, '/platform-submissions')
  assert.equal(member.currentRoute.value.meta.admin, undefined)
  await member.push('/admin/platform-submissions')
  assert.equal(member.currentRoute.value.path, '/')
  const administrator = app('admin').router
  await administrator.push('/admin/platform-submissions')
  assert.equal(administrator.currentRoute.value.meta.admin, true)
})
