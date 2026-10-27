import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import ts from 'typescript'

const module = { exports: {} }
vm.runInNewContext(
  ts.transpileModule(
    readFileSync(new URL('../src/comparisonPreview.ts', import.meta.url), 'utf8'),
    {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    },
  ).outputText,
  { module, exports: module.exports },
)
const { platformPreviews } = module.exports
const offer = (id, platform, changes = {}) => ({
  id,
  platform_id: platform,
  platform_name: `平台${platform}`,
  price: '0.001',
  currency: 'CNY',
  unit: '',
  display_quantity: '1',
  status: 'active',
  freshness: 'fresh',
  quality: ['unit_requires_review'],
  ...changes,
})

test('previews show every platform once even when the first platform has many offers', () => {
  const result = platformPreviews([offer(1, 1), offer(2, 1), offer(3, 1), offer(4, 2)])
  assert.equal(result.length, 2)
  assert.equal(result[0].count, 3)
  assert.equal(result[1].count, 1)
  assert.equal(result[0].validCount, 3)
  assert.equal(result[0].unit, '')
  assert.equal(result[0].minimum, '0.001')
})

test('decimal ranges retain exact source precision and exclude stale, closed and invalid prices', () => {
  const result = platformPreviews([
    offer(1, 1, { price: '0.00000000000000000019' }),
    offer(2, 1, { price: '0.00000000000000000011' }),
    offer(3, 1, { price: '0', status: 'closed' }),
    offer(4, 1, { price: '9999', freshness: 'stale' }),
    offer(5, 1, { price: '0', quality: ['invalid_price'] }),
    offer(6, 1, { price: 'NaN' }),
  ])[0]
  assert.equal(result.validCount, 2)
  assert.equal(result.minimum, '0.00000000000000000011')
  assert.equal(result.maximum, '0.00000000000000000019')
})

test('mixed currency, unit or quantity suppresses a misleading numerical range', () => {
  for (const changes of [{ currency: 'USD' }, { unit: '个' }, { display_quantity: '1000' }]) {
    const result = platformPreviews([offer(1, 1), offer(2, 1, changes)])[0]
    assert.equal(result.mixedBasis, true)
    assert.equal(result.minimum, null)
    assert.equal(result.maximum, null)
  }
})

test('equivalent decimal quantities and prices do not create false mixed basis or ranges', () => {
  const result = platformPreviews([
    offer(1, 1, { price: '0.0100', display_quantity: '01.000' }),
    offer(2, 1, { price: '0.01', display_quantity: '1' }),
  ])[0]
  assert.equal(result.mixedBasis, false)
  assert.equal(result.minimum, result.maximum)
  assert.equal(platformPreviews([]).length, 0)
})
