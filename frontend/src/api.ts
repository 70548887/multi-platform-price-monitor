import { reactive } from 'vue'

export interface User {
  id: number
  username: string
  role: 'admin' | 'viewer'
  is_active?: boolean
}
export interface Collection<T = Record<string, any>> {
  items: T[]
  total: number
  page: number
  page_size: number
}
export const auth = reactive<{ user: User | null; csrf: string; initialized: boolean }>({
  user: null,
  csrf: '',
  initialized: false,
})

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}
export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData))
    headers.set('Content-Type', 'application/json')
  if (options.method && !['GET', 'HEAD'].includes(options.method.toUpperCase()) && auth.csrf)
    headers.set('X-CSRF-Token', auth.csrf)
  let response: Response
  try {
    response = await fetch(`/api${path}`, { ...options, headers, credentials: 'include' })
  } catch {
    throw new ApiError(0, '连接服务失败，请检查网络或后台服务。')
  }
  let result: any = null
  try {
    result = response.status === 204 ? null : await response.json()
  } catch {}
  if (!response.ok) {
    let detail = result?.detail ?? result?.message
    if (Array.isArray(detail))
      detail = detail
        .map((item: any) => `${(item.loc ?? []).slice(1).join('.')}: ${item.msg}`)
        .join('；')
    if (typeof detail !== 'string')
      detail =
        response.status === 401
          ? '登录已过期，请重新登录。'
          : response.status === 403
            ? '当前账号没有执行此操作的权限。'
            : `请求失败（${response.status}），请重试。`
    if (response.status === 401) {
      auth.user = null
      auth.csrf = ''
    }
    throw new ApiError(response.status, detail)
  }
  return result as T
}
export const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
})
export async function loadUser() {
  try {
    const result = await api('/auth/me')
    auth.user = result.user ?? result
    auth.csrf = result.csrf_token ?? ''
  } catch {
    auth.user = null
  } finally {
    auth.initialized = true
  }
}
export const itemsOf = <T = Record<string, any>>(data: any): T[] =>
  Array.isArray(data) ? data : (data?.items ?? [])
export const params = (values: Record<string, unknown>) => {
  const query = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  })
  return query.toString() ? `?${query}` : ''
}
export function date(value: unknown, short = false) {
  if (!value) return '—'
  const parsed = new Date(String(value))
  if (Number.isNaN(parsed.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    ...(short ? {} : { year: 'numeric' }),
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(parsed)
}
export const decimalText = (value: unknown) =>
  value === null || value === undefined || value === ''
    ? '—'
    : String(value).replace(/(\.\d*?[1-9])0+$|\.0+$/, '$1')
export const price = (value: unknown, currency = 'CNY') =>
  value === null || value === undefined || value === ''
    ? '—'
    : `${currency === 'CNY' ? '¥' : `${currency} `}${decimalText(value)}`
export function displayBasisLabel(quantity: unknown, unit: unknown) {
  const amount = decimalText(quantity)
  const unitText = String(unit ?? '').trim()
  const positive = /^\+?\d+(?:\.\d+)?$/.test(amount) && decimalCompare(amount, '0') > 0
  if (!positive) return unitText ? '计价数量待核验' : '平台未注明计价单位'
  return `每 ${amount}${unitText ? ` ${unitText}` : '（平台未注明单位）'}`
}
export const criticalQuality = (value: unknown) =>
  Array.isArray(value)
    ? value.some((item) => ['invalid_price', 'invalid_display_quantity'].includes(String(item)))
    : value === 'invalid'
export const qualityText = (value: unknown) => {
  const labels: Record<string, string> = {
    unit_requires_review: '计价单位待核验',
    invalid_price: '价格异常',
    invalid_display_quantity: '展示数量异常',
    invalid_stock: '库存异常',
    collection_gap: '采集缺失',
    invalid_unit: '计价单位异常',
  }
  return (Array.isArray(value) ? value : [value])
    .map((item) => labels[String(item)] ?? String(item))
    .join(' · ')
}
export function decimalCompare(a: string, b: string) {
  const split = (value: string) => {
    const [integer, fraction = ''] = value.split('.')
    return { integer, fraction }
  }
  const x = split(a),
    y = split(b),
    scale = Math.max(x.fraction.length, y.fraction.length)
  const normalize = (value: { integer: string; fraction: string }) =>
    BigInt(
      `${value.integer.startsWith('-') ? '-' : ''}${value.integer.replace(/^[+-]/, '')}${value.fraction.padEnd(scale, '0')}`,
    )
  const left = normalize(x),
    right = normalize(y)
  return left < right ? -1 : left > right ? 1 : 0
}
export function statusLabel(value: unknown) {
  const labels: Record<string, string> = {
    active: '在售',
    on_sale: '在售',
    available: '在售',
    stopped: '停售',
    off_sale: '停售',
    unavailable: '停售',
    suspended: '已暂停',
    pending: '排队中',
    queued: '排队中',
    running: '采集中',
    success: '成功',
    completed: '成功',
    partial: '不完整',
    incomplete: '不完整',
    failed: '失败',
    auth_expired: '登录过期',
    expired: '登录过期',
    enabled: '运行中',
    disabled: '已停用',
    healthy: '正常',
    warning: '需关注',
    valid: '正常',
    ok: '正常',
    invalid_unit: '单位异常',
    invalid: '待核验',
    draft: '草稿',
    published: '已发布',
    archived: '已归档',
    admin: '管理员',
    viewer: '查看者',
    price_increase: '价格上涨',
    price_decrease: '价格下降',
    price_change: '价格变化',
    product_stopped: '商品停售',
    product_resumed: '恢复在售',
    product_renamed: '商品更名',
    session_expired: '登录过期',
    collection_failed: '采集失败',
    new_product: '新增商品',
  }
  Object.assign(labels, {
    closed: '停售',
    resumed: '恢复在售',
    renamed: '商品更名',
    price_changed: '价格变化',
    basis_changed: '计价条件变化',
    missing: '缺失',
    connected: '已连接',
  })
  return labels[String(value)] ?? String(value ?? '—')
}
export function statusClass(value: unknown) {
  const v = String(value)
  if (
    [
      'active',
      'on_sale',
      'available',
      'success',
      'completed',
      'healthy',
      'valid',
      'ok',
      'published',
      'enabled',
      'product_resumed',
      'price_decrease',
      'resumed',
      'connected',
    ].includes(v)
  )
    return 'success'
  if (
    [
      'failed',
      'auth_expired',
      'expired',
      'session_expired',
      'collection_failed',
      'price_increase',
      'invalid',
    ].includes(v)
  )
    return 'danger'
  if (
    [
      'partial',
      'incomplete',
      'warning',
      'invalid_unit',
      'stopped',
      'off_sale',
      'unavailable',
      'product_stopped',
      'closed',
      'basis_changed',
    ].includes(v)
  )
    return 'warning'
  if (
    [
      'running',
      'queued',
      'pending',
      'draft',
      'price_change',
      'new_product',
      'price_changed',
      'renamed',
    ].includes(v)
  )
    return 'info'
  return 'neutral'
}
