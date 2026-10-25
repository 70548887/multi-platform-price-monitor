export interface PlatformAdapter {
  id: string
  name: string
  implemented: boolean
  default_base_url?: string
  description?: string
  note?: string
  login_path?: string
}

export interface PlatformType {
  id: string
  name: string
  adapters: PlatformAdapter[]
}

// Keep existing connections readable while the type directory is being deployed.
export const fallbackPlatformTypes: PlatformType[] = [
  {
    id: 'yile',
    name: '亿乐',
    adapters: [
      {
        id: 'xiaohong66',
        name: '亿乐 · 当前站点版本',
        implemented: true,
        default_base_url: 'https://www.xiaohong66.com',
      },
    ],
  },
  {
    id: 'yike',
    name: '易客',
    adapters: [
      {
        id: 'yike',
        name: '易客 · 标准版本',
        implemented: false,
        default_base_url: '',
        note: '需要对应站点或接口资料验证',
      },
    ],
  },
]

export function platformTypeId(platform: any, types = fallbackPlatformTypes) {
  if (platform?.platform_type) return String(platform.platform_type)
  return (
    types.find((type) => type.adapters.some((adapter) => adapter.id === platform?.adapter))?.id ??
    (platform?.adapter === 'xiaohong66' ? 'yile' : 'unclassified')
  )
}

export function platformTypeName(platform: any, types = fallbackPlatformTypes) {
  return (
    platform?.platform_type_name ??
    types.find((type) => type.id === platformTypeId(platform, types))?.name ??
    '待识别类型'
  )
}

export function platformAdapterName(platform: any, types = fallbackPlatformTypes) {
  return (
    platform?.adapter_name ??
    types
      .find((type) => type.id === platformTypeId(platform, types))
      ?.adapters.find((adapter) => adapter.id === platform?.adapter)?.name ??
    '当前站点版本（待验证）'
  )
}

export function platformSupported(platform: any, types = fallbackPlatformTypes) {
  if (typeof platform?.adapter_supported === 'boolean') return platform.adapter_supported
  return Boolean(
    types
      .find((type) => type.id === platformTypeId(platform, types))
      ?.adapters.find((adapter) => adapter.id === platform?.adapter)?.implemented,
  )
}

export function platformIdentityLocked(platform: any) {
  return Boolean(
    platform &&
      (platform.identity_locked ||
        platform.type_locked ||
        platform.platform_type_locked ||
        platform.has_products ||
        platform.has_session ||
        platform.has_published_rules ||
        Number(platform.product_count ?? 0) > 0 ||
        Number(platform.published_rule_count ?? 0) > 0 ||
        platform.active_rule_id ||
        ['connected', 'expired'].includes(platform.auth_status)),
  )
}
