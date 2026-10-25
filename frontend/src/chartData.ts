import { criticalQuality } from './api'

/** Preserve exact decimal text for tooltips; only chart coordinates use Number. */
export function buildChartData(
  points: Record<string, any>[],
  options: { daily?: boolean; display?: boolean; field?: string; intervalSeconds?: number } = {},
) {
  const values: any[] = []
  let previousBasis: string | null = null
  let previousTime: number | null = null
  const expectedInterval = options.daily
    ? 86400000
    : options.intervalSeconds
      ? options.intervalSeconds * 1000
      : null
  for (const p of points) {
    const raw = options.daily
      ? p[options.field ?? 'avg']
      : options.display
        ? (p.display_price ?? p.price)
        : p.price
    const invalid =
      raw === null ||
      raw === undefined ||
      p.is_gap ||
      p.status === 'failed' ||
      criticalQuality(p.quality) ||
      ['closed', 'stopped', 'off_sale', 'unavailable', 'missing'].includes(p.status)
    const time = p.observed_at ?? p.scheduled_at ?? p.date
    const timestamp = new Date(time).getTime()
    const basis = `${p.currency ?? 'CNY'}|${p.unit ?? ''}|${p.display_quantity ?? ''}`
    if (
      expectedInterval &&
      previousTime !== null &&
      timestamp - previousTime > expectedInterval * 1.5
    )
      values.push([new Date(previousTime + expectedInterval).toISOString(), null, null])
    if (previousBasis !== null && previousBasis !== basis) values.push([time, null, null])
    values.push([
      time,
      invalid ? null : Number(raw),
      invalid ? null : String(raw),
      p.currency ?? 'CNY',
      p.unit ?? '',
      p.min,
      p.max,
      p.avg,
      p.count,
    ])
    previousBasis = basis
    previousTime = Number.isFinite(timestamp) ? timestamp : null
  }
  return values
}
