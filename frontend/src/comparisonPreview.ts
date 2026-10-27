export interface PreviewOffer {
  id: number
  platform_id: number
  platform_name?: string
  price: string
  unit?: string
  currency?: string
  display_quantity?: string
  status?: string
  freshness?: string
  quality?: string[]
}

export interface PlatformPreview {
  id: number
  name: string
  count: number
  validCount: number
  currency: string
  unit: string
  minimum: string | null
  maximum: string | null
  mixedBasis: boolean
}

// Only plain nonnegative decimal text participates. No Number conversion:
// catalogue prices can contain more significant digits than a JS float keeps.
function validPrice(value: unknown): value is string {
  return typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value)
}
function comparePrice(a: string, b: string) {
  const [ai, af = ''] = a.split('.')
  const [bi, bf = ''] = b.split('.')
  const left = ai.replace(/^0+(?=\d)/, '')
  const right = bi.replace(/^0+(?=\d)/, '')
  if (left.length !== right.length) return left.length < right.length ? -1 : 1
  if (left !== right) return left < right ? -1 : 1
  const length = Math.max(af.length, bf.length)
  const lf = af.padEnd(length, '0'),
    rf = bf.padEnd(length, '0')
  return lf === rf ? 0 : lf < rf ? -1 : 1
}
function quantityBasis(value: string | undefined) {
  if (!value || !validPrice(value)) return value ?? '1'
  const [integer, fraction = ''] = value.split('.')
  const remainder = fraction.replace(/0+$/, '')
  return integer.replace(/^0+(?=\d)/, '') + (remainder ? '.' + remainder : '')
}

/** A platform summary is a reference range, never a cross-platform minimum. */
export function platformPreviews(offers: PreviewOffer[]): PlatformPreview[] {
  const platforms = new Map<number, PreviewOffer[]>()
  for (const offer of offers) {
    const rows = platforms.get(offer.platform_id) ?? []
    rows.push(offer)
    platforms.set(offer.platform_id, rows)
  }
  return [...platforms].map(([id, rows]) => {
    const valid = rows.filter(
      (offer) =>
        offer.status === 'active' &&
        offer.freshness === 'fresh' &&
        validPrice(offer.price) &&
        !(offer.quality ?? []).some((value) =>
          ['invalid_price', 'invalid_display_quantity', 'invalid_unit', 'invalid_stock'].includes(
            value,
          ),
        ),
    )
    const bases = new Set(
      valid.map((offer) =>
        JSON.stringify([
          offer.currency?.trim() ?? '',
          offer.unit?.trim() ?? '',
          quantityBasis(offer.display_quantity),
        ]),
      ),
    )
    const mixedBasis = bases.size > 1
    const basis = valid[0]
    const comparable = !mixedBasis && Boolean(basis?.currency?.trim())
    let minimum: string | null = null,
      maximum: string | null = null
    if (comparable)
      for (const offer of valid) {
        if (minimum === null || comparePrice(offer.price, minimum) < 0) minimum = offer.price
        if (maximum === null || comparePrice(offer.price, maximum) > 0) maximum = offer.price
      }
    if (minimum !== null && maximum !== null && comparePrice(minimum, maximum) === 0)
      maximum = minimum
    return {
      id,
      name: rows[0].platform_name || `平台 #${id}`,
      count: rows.length,
      validCount: valid.length,
      currency: comparable ? basis.currency!.trim() : '',
      unit: comparable ? (basis.unit?.trim() ?? '') : '',
      minimum,
      maximum,
      mixedBasis,
    }
  })
}
