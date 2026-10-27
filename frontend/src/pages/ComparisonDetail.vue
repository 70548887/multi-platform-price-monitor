<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, auth, date, displayBasisLabel, price, qualityText, decimalCompare } from '../api'
import { areaPath } from '../navigation'
import Icon from '../components/Icon.vue'
import Badge from '../components/Badge.vue'
import EmptyState from '../components/EmptyState.vue'
import ComparisonTrend from '../components/ComparisonTrend.vue'

interface Offer {
  id: number
  platform_id: number
  platform_name: string
  source_id: string
  name: string
  category_name: string
  price: string
  display_price: string
  display_quantity: string
  unit: string
  currency: string
  status: string
  freshness: string
  quality: string[]
  last_observed_at: string
  comparison_eligible: boolean
  comparison_reason: string
}
interface Comparison {
  group: {
    id: number
    name: string
    description: string
    category_id: number | null
    category_name: string | null
    category_path?: { id: number; name: string }[]
    category_path_name?: string
    match_mode: 'exact' | 'similar'
    unit: string
    currency: string
  }
  items: Offer[]
  comparable: boolean
  reason: string
  eligible_product_ids: number[]
  lowest_product_ids: number[]
}

const route = useRoute()
const adminArea = computed(() => Boolean(route.meta?.admin))
const canEdit = computed(() => adminArea.value && auth.user?.role === 'admin')
const comparison = ref<Comparison | null>(null)
const loading = ref(true)
const error = ref('')
const selectedPlatform = ref('')
const query = ref('')
const saleStatus = ref('')
const priceOrder = ref('default')
const trendRefresh = ref(0)
let controller: AbortController | undefined
let requestNumber = 0
let disposed = false

async function load() {
  const number = ++requestNumber
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const result = await api<Comparison>(`/mappings/${route.params.id}/compare`, {
      signal: controller.signal,
    })
    if (number === requestNumber && !disposed) {
      comparison.value = result
      trendRefresh.value++
      if (
        selectedPlatform.value &&
        !result.items.some((offer) => String(offer.platform_id) === selectedPlatform.value)
      )
        selectedPlatform.value = ''
    }
  } catch (e: any) {
    if (number === requestNumber && !disposed) {
      comparison.value = null
      error.value = e.message
    }
  } finally {
    if (number === requestNumber && !disposed) loading.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => {
  disposed = true
  requestNumber++
  controller?.abort()
})

const platforms = computed(() => {
  const values = new Map<number, { id: number; name: string; count: number }>()
  for (const offer of comparison.value?.items ?? []) {
    const item = values.get(offer.platform_id)
    if (item) item.count++
    else
      values.set(offer.platform_id, { id: offer.platform_id, name: offer.platform_name, count: 1 })
  }
  return [...values.values()]
})
const visibleOffers = computed(() => {
  const search = query.value.trim().toLocaleLowerCase()
  const offers = (comparison.value?.items ?? []).filter(
    (offer) =>
      (!selectedPlatform.value || String(offer.platform_id) === selectedPlatform.value) &&
      (!saleStatus.value || offer.status === saleStatus.value) &&
      (!search ||
        [offer.name, offer.source_id, offer.category_name, offer.platform_name].some((value) =>
          String(value ?? '')
            .toLocaleLowerCase()
            .includes(search),
        )),
  )
  if (priceOrder.value !== 'default')
    offers.sort((a, b) => {
      // Currency groups stay separate; compare decimal strings within each currency.
      const currency = a.currency.localeCompare(b.currency)
      if (currency) return currency
      const left = priceOrder.value.startsWith('display') ? a.display_price : a.price
      const right = priceOrder.value.startsWith('display') ? b.display_price : b.price
      const difference = decimalCompare(left, right)
      return (priceOrder.value.endsWith('desc') ? -difference : difference) || a.id - b.id
    })
  return offers
})
const activeCount = computed(
  () => (comparison.value?.items ?? []).filter((p) => p.status === 'active').length,
)
const hasFilters = computed(() =>
  Boolean(query.value || selectedPlatform.value || saleStatus.value),
)
const lowestIds = computed(
  () => new Set(comparison.value?.comparable ? comparison.value.lowest_product_ids : []),
)
const categoryBreadcrumb = computed(
  () =>
    comparison.value?.group.category_path ??
    (comparison.value?.group.category_id
      ? [
          {
            id: comparison.value.group.category_id,
            name: comparison.value.group.category_name ?? '比价类目',
          },
        ]
      : []),
)
const returnPath = computed(() => ({
  path: areaPath('/compare', adminArea.value),
  query: comparison.value?.group.category_id
    ? { category_id: comparison.value.group.category_id }
    : {},
}))
const editPath = computed(() => ({
  path: '/admin/compare',
  query: comparison.value ? { edit: comparison.value.group.id } : {},
}))
function offerNote(offer: Offer) {
  if (
    comparison.value?.group.match_mode === 'similar' &&
    (!offer.comparison_reason ||
      offer.comparison_reason === '相似商品仅展示源站报价，规格和计价口径需人工核对')
  )
    return '同类参考'
  return offer.comparison_reason || (offer.comparison_eligible ? '符合比较口径' : '参考报价')
}
function clearFilters() {
  query.value = ''
  selectedPlatform.value = ''
  saleStatus.value = ''
  priceOrder.value = 'default'
}
</script>

<template>
  <div class="comparison-toolbar">
    <RouterLink :to="returnPath" class="text-link"
      ><Icon name="back" :size="16" />返回全网比价</RouterLink
    >
    <div>
      <button class="btn btn-white btn-sm" :disabled="loading" @click="load">
        <Icon name="refresh" :size="15" />刷新报价
      </button>
      <RouterLink
        v-if="canEdit && comparison?.items.length"
        :to="editPath"
        class="btn btn-primary btn-sm"
        >管理关联商品</RouterLink
      >
    </div>
  </div>
  <div v-if="error" role="alert" class="notice error">{{ error }}</div>
  <div v-if="loading && !comparison" class="loading-state">
    <span class="spinner"></span>正在读取各平台报价…
  </div>
  <template v-if="comparison">
    <section class="panel comparison-heading">
      <div class="comparison-identity">
        <nav
          v-if="categoryBreadcrumb.length"
          class="comparison-category comparison-breadcrumb"
          aria-label="商品类目路径"
        >
          <template v-for="(category, index) in categoryBreadcrumb" :key="category.id">
            <span v-if="index" aria-hidden="true">/</span>
            <RouterLink
              :to="{ path: areaPath('/compare', adminArea), query: { category_id: category.id } }"
              >{{ category.name }}</RouterLink
            >
          </template>
        </nav>
        <span v-else class="comparison-category">未分类</span>
        <h2>{{ comparison.group.name }}</h2>
        <p v-if="comparison.group.description" class="comparison-description">
          {{ comparison.group.description }}
        </p>
        <p class="comparison-scope">
          {{ comparison.group.match_mode === 'similar' ? '同类 / 相似商品' : '同规格商品' }} ·
          仅展示当前账号已获授权的平台报价
        </p>
      </div>
      <div class="comparison-counts">
        <div>
          <strong>{{ platforms.length }}</strong
          ><span>交易平台</span>
        </div>
        <div>
          <strong>{{ comparison.items.length }}</strong
          ><span>关联报价</span>
        </div>
        <div>
          <strong>{{ activeCount }}</strong
          ><span>在售报价</span>
        </div>
      </div>
    </section>
    <div
      v-if="comparison.items.length"
      class="notice comparison-basis"
      :class="comparison.comparable ? 'success' : ''"
    >
      {{
        comparison.group.match_mode === 'similar'
          ? '同类或相似商品按源站价格展示。请结合规格、单位和服务说明选择，未自动判定全网最低价。'
          : comparison.comparable
            ? '已标出符合当前比较口径的最低基础单价，其余关联报价仍完整展示。'
            : comparison.reason
      }}
    </div>
    <ComparisonTrend
      v-if="comparison.items.length"
      :mapping-id="comparison.group.id"
      :refresh-key="trendRefresh"
    />
    <section class="panel comparison-offers" :aria-busy="loading">
      <div v-if="comparison.items.length" class="comparison-filters">
        <div class="search-input">
          <Icon name="search" :size="16" /><input
            v-model="query"
            aria-label="搜索比价商品"
            placeholder="搜索名称、编号或源类目"
          />
        </div>
        <select v-model="selectedPlatform" aria-label="筛选比价平台">
          <option value="">全部交易平台</option>
          <option v-for="platform in platforms" :key="platform.id" :value="String(platform.id)">
            {{ platform.name }}（{{ platform.count }}）
          </option>
        </select>
        <select v-model="saleStatus" aria-label="筛选比价销售状态">
          <option value="">全部状态</option>
          <option value="active">在售</option>
          <option value="closed">停售</option>
        </select>
        <select v-model="priceOrder" aria-label="报价价格排序">
          <option value="default">默认排序</option>
          <option value="base-asc">基础单价：低到高</option>
          <option value="base-desc">基础单价：高到低</option>
          <option value="display-asc">展示价格：低到高</option>
          <option value="display-desc">展示价格：高到低</option>
        </select>
        <span v-if="priceOrder !== 'default'" class="table-secondary"
          >不同币种分别排序，规格及单位请逐项核对</span
        >
        <button v-if="hasFilters" class="text-link" @click="clearFilters">清除筛选</button>
        <span class="comparison-total">{{ visibleOffers.length }} 条报价</span>
      </div>
      <div v-if="visibleOffers.length" class="comparison-quote-list">
        <table class="comparison-table">
          <thead>
            <tr>
              <th>交易平台 / 源商品</th>
              <th>基础单价</th>
              <th>展示价格 / 数量</th>
              <th>状态 / 采样时间</th>
              <th><span class="sr-only">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="offer in visibleOffers" :key="offer.id">
              <td class="comparison-product" data-label="交易平台 / 源商品">
                <strong>{{ offer.platform_name }}</strong
                ><RouterLink :to="areaPath(`/products/${offer.id}`, adminArea)">{{
                  offer.name
                }}</RouterLink
                ><small>#{{ offer.source_id }} · {{ offer.category_name || '未分类' }}</small>
              </td>
              <td class="comparison-price" data-label="基础单价">
                <strong class="price-text">{{ price(offer.price, offer.currency) }}</strong>
                <div class="table-secondary">
                  {{ offer.unit ? `每 ${offer.unit}` : '源站未注明单位' }}
                </div>
                <span v-if="lowestIds.has(offer.id)" class="comparison-lowest">可比范围内最低</span>
              </td>
              <td class="comparison-display" data-label="展示价格 / 数量">
                <span class="mono">{{ price(offer.display_price, offer.currency) }}</span>
                <div class="table-secondary">
                  {{ displayBasisLabel(offer.display_quantity, offer.unit) }}
                </div>
              </td>
              <td class="comparison-status" data-label="状态 / 采样时间">
                <div class="comparison-status-line">
                  <Badge :value="offer.status" /><span
                    v-if="offer.freshness === 'stale'"
                    class="quality-warning"
                    >已过期</span
                  >
                </div>
                <time class="table-secondary" :datetime="offer.last_observed_at">{{
                  date(offer.last_observed_at)
                }}</time>
                <div class="comparison-reason">{{ offerNote(offer) }}</div>
                <div v-if="offer.quality?.length" class="quality-warning">
                  {{ qualityText(offer.quality) }}
                </div>
              </td>
              <td class="comparison-action">
                <RouterLink :to="areaPath(`/products/${offer.id}`, adminArea)" class="text-link"
                  >价格走势</RouterLink
                >
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <EmptyState
        v-else-if="comparison.items.length"
        icon="search"
        :title="hasFilters ? '没有符合筛选条件的报价' : '尚未关联平台商品'"
        :description="
          hasFilters
            ? '尝试其他平台、状态或搜索关键词。'
            : '在管理后台编辑该商品，选择各平台的同类或相似商品。'
        "
        ><button v-if="hasFilters" class="btn btn-white btn-sm" @click="clearFilters">
          清除筛选
        </button></EmptyState
      >
      <div v-else class="comparison-unlinked">
        <span class="comparison-unlinked-icon"><Icon name="link" :size="25" /></span>
        <h3>商品已建立，等待关联报价</h3>
        <p>
          {{
            canEdit
              ? '选择多个交易平台中的同类商品，关联后即可在这里查看各平台价格。'
              : '管理员关联商品并授权对应平台后，各平台报价会显示在这里。'
          }}
        </p>
        <RouterLink v-if="canEdit" :to="editPath" class="btn btn-primary"
          ><Icon name="plus" :size="16" />关联平台商品</RouterLink
        >
        <RouterLink v-else :to="returnPath" class="text-link">返回全网比价</RouterLink>
      </div>
    </section>
  </template>
</template>

<style scoped>
.comparison-toolbar,
.comparison-toolbar > div {
  display: flex;
  align-items: center;
  gap: 12px;
}
.comparison-toolbar {
  justify-content: space-between;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.comparison-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 20px 22px;
  gap: 24px;
  margin-bottom: 16px;
}
.comparison-identity {
  flex: 1;
  min-width: 0;
}
.comparison-category {
  font-size: 12px;
  color: #52678a;
}
.comparison-breadcrumb {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.comparison-heading h2 {
  font-size: 22px;
  line-height: 1.5;
  margin: 8px 0;
  overflow-wrap: anywhere;
}
.comparison-description {
  white-space: pre-wrap;
  line-height: 1.7;
  font-size: 13px;
  color: #4b5e79;
}
.comparison-scope {
  font-size: 12px;
  color: #78899e;
  margin-top: 10px;
}
.comparison-counts {
  display: flex;
  gap: 22px;
  padding-top: 8px;
  flex-shrink: 0;
}
.comparison-counts > div {
  display: grid;
  gap: 8px;
}
.comparison-counts strong {
  font-size: 22px;
  font-weight: 600;
}
.comparison-counts span {
  font-size: 12px;
  color: #78899e;
  white-space: nowrap;
}
.comparison-filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding: 16px 18px;
  border-bottom: 1px solid #edf1f7;
}
.comparison-filters select {
  width: auto;
  max-width: 220px;
  min-width: 0;
}
.comparison-filters .search-input {
  flex: 1 1 230px;
  min-width: 0;
  max-width: 290px;
}
.comparison-total {
  margin-left: auto;
  font-size: 12px;
  color: #78899e;
}
.comparison-heading,
.comparison-offers,
.comparison-quote-list {
  min-width: 0;
  max-width: 100%;
}
.comparison-table {
  width: 100%;
  table-layout: fixed;
}
.comparison-table th,
.comparison-table td {
  padding: 15px 16px;
  white-space: normal;
  overflow-wrap: anywhere;
  vertical-align: top;
}
.comparison-table th:first-child {
  width: 34%;
}
.comparison-table th:nth-child(2),
.comparison-table th:nth-child(3) {
  width: 18%;
}
.comparison-table th:nth-child(4) {
  width: 22%;
}
.comparison-table th:last-child {
  width: 8%;
}
.comparison-table .price-text,
.comparison-table .mono {
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.comparison-product {
  line-height: 1.6;
}
.comparison-product strong,
.comparison-product a,
.comparison-product small {
  display: block;
}
.comparison-product a {
  margin: 5px 0;
  line-height: 1.65;
}
.comparison-product small {
  color: #78899e;
}
.comparison-reason {
  font-size: 11px;
  color: #78899e;
  margin-top: 6px;
  line-height: 1.6;
}
.comparison-status-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.comparison-status time {
  display: block;
  margin-top: 7px;
}
.comparison-action .text-link {
  font-size: 12px;
  white-space: nowrap;
}
.comparison-unlinked {
  display: grid;
  justify-items: start;
  gap: 12px;
  padding: 28px;
}
.comparison-unlinked-icon {
  color: #637fa9;
}
.comparison-unlinked h3 {
  font-size: 16px;
  font-weight: 600;
}
.comparison-unlinked p {
  color: #78899e;
  font-size: 13px;
  line-height: 1.8;
  max-width: 560px;
}
.comparison-unlinked .btn {
  margin-top: 4px;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.comparison-lowest {
  display: inline-block;
  color: #08735b;
  background: #edf9f3;
  padding: 3px 7px;
  border-radius: 4px;
  font-size: 11px;
  margin-top: 7px;
}
@media (max-width: 1200px) {
  .comparison-table,
  .comparison-table tbody {
    display: block;
  }
  .comparison-table thead {
    display: none;
  }
  .comparison-table tbody {
    padding: 14px;
  }
  .comparison-table tr {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0 20px;
    border: 1px solid #e8edf5;
    border-radius: 8px;
    margin-bottom: 12px;
    padding: 16px;
    background: #fff;
    min-width: 0;
  }
  .comparison-table tr:last-child {
    margin-bottom: 0;
  }
  .comparison-table td {
    display: block;
    padding: 0;
    border: 0;
    min-width: 0;
  }
  .comparison-table td[data-label]::before {
    content: attr(data-label);
    display: block;
    color: #78899e;
    font-size: 11px;
    margin: 14px 0 7px;
  }
  .comparison-table .comparison-product {
    grid-column: 1 / -1;
  }
  .comparison-table .comparison-product::before {
    display: none;
  }
  .comparison-table .comparison-product strong {
    font-size: 12px;
    color: #637fa9;
  }
  .comparison-table .comparison-product a {
    font-size: 14px;
  }
  .comparison-table .comparison-status {
    grid-column: 1 / -1;
  }
  .comparison-table .comparison-action {
    grid-column: 1 / -1;
    padding-top: 13px;
    margin-top: 13px;
    border-top: 1px solid #edf1f7;
  }
  .comparison-table .comparison-status-line {
    display: inline-flex;
    margin-right: 12px;
  }
  .comparison-table .comparison-status time {
    display: inline;
  }
}
@media (max-width: 900px) {
  .comparison-heading {
    flex-direction: column;
    gap: 12px;
    padding: 18px;
  }
  .comparison-counts {
    width: 100%;
    justify-content: space-between;
  }
  .comparison-filters {
    align-items: stretch;
    padding: 14px;
  }
  .comparison-filters .search-input {
    width: 100%;
    max-width: none;
  }
}
@media (max-width: 520px) {
  .comparison-toolbar > div {
    width: 100%;
    justify-content: flex-end;
  }
  .comparison-filters select {
    max-width: none;
    flex: 1 1 150px;
  }
  .comparison-filters .search-input {
    flex-basis: 100%;
  }
  .comparison-counts {
    gap: 12px;
  }
  .comparison-table tbody {
    padding: 10px;
  }
  .comparison-table tr {
    padding: 13px;
    gap: 0 12px;
  }
  .comparison-unlinked {
    padding: 22px 18px;
  }
}
</style>
