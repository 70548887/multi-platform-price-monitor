export function isAdminPath(path: string) {
  return path === '/admin' || path.startsWith('/admin/')
}

export function isLoginPath(path: string) {
  return path === '/login' || path === '/admin/login'
}

export function areaPath(path: string, admin: boolean) {
  return admin ? `/admin${path === '/' ? '' : path}` : path
}

/** Login redirects stay within the application and never send viewers into management. */
export function loginDestination(value: unknown, role: string | undefined, adminLogin: boolean) {
  const fallback = role === 'admin' && adminLogin ? '/admin' : '/'
  if (
    typeof value !== 'string' ||
    !value.startsWith('/') ||
    value.startsWith('//') ||
    /[\\\u0000-\u0020]/.test(value)
  )
    return fallback
  const path = value.split(/[?#]/, 1)[0]
  if (isLoginPath(path) || (isAdminPath(path) && role !== 'admin')) return fallback
  return value
}
