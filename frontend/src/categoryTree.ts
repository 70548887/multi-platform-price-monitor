export interface ComparisonCategoryNode {
  id: number
  name: string
  parent_id?: number | null
  sort_order?: number
  depth?: number
  path?: { id: number; name: string }[]
  path_name?: string
  children_count?: number
  product_count?: number
  [key: string]: any
}

/** Build display paths from stable IDs, so sibling categories may share names. */
export function categoryPath(
  category: ComparisonCategoryNode,
  categories: ComparisonCategoryNode[],
): { id: number; name: string }[] {
  if (category.path?.length) return category.path
  const byId = new Map(categories.map((item) => [item.id, item]))
  const result: { id: number; name: string }[] = []
  const seen = new Set<number>()
  let current: ComparisonCategoryNode | undefined = category
  while (current && !seen.has(current.id)) {
    seen.add(current.id)
    result.unshift({ id: current.id, name: current.name })
    current = current.parent_id ? byId.get(current.parent_id) : undefined
  }
  return result
}

export function categoryPathName(
  category: ComparisonCategoryNode,
  categories: ComparisonCategoryNode[],
) {
  return (
    category.path_name ||
    categoryPath(category, categories)
      .map((item) => item.name)
      .join(' / ')
  )
}

export function descendantCategoryIds(categories: ComparisonCategoryNode[], id: number) {
  const result = new Set<number>([id])
  const pending = [id]
  while (pending.length) {
    const parent = pending.pop()
    for (const item of categories) {
      if (item.parent_id === parent && !result.has(item.id)) {
        result.add(item.id)
        pending.push(item.id)
      }
    }
  }
  return result
}

/** The traversal depth reflects hierarchy, independent of response ordering. */
export function categoryTreeRows(
  categories: ComparisonCategoryNode[],
  collapsed: ReadonlySet<number> = new Set(),
) {
  const byId = new Map(categories.map((item) => [item.id, item]))
  const children = new Map<number | null, ComparisonCategoryNode[]>()
  for (const item of categories) {
    const parent = item.parent_id && byId.has(item.parent_id) ? item.parent_id : null
    const siblings = children.get(parent) ?? []
    siblings.push(item)
    children.set(parent, siblings)
  }
  for (const siblings of children.values())
    siblings.sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0) || a.id - b.id)
  const result: (ComparisonCategoryNode & { depth: number; hasChildren: boolean })[] = []
  const visited = new Set<number>()
  function visit(item: ComparisonCategoryNode, depth: number) {
    if (visited.has(item.id)) return
    visited.add(item.id)
    const nested = children.get(item.id) ?? []
    result.push({ ...item, depth, hasChildren: nested.length > 0 })
    if (!collapsed.has(item.id)) for (const child of nested) visit(child, depth + 1)
  }
  for (const item of children.get(null) ?? []) visit(item, 0)
  return result
}
