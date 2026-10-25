<script setup lang="ts">
import { onMounted, ref, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  api,
  itemsOf,
  price,
  decimalText,
  displayBasisLabel,
  date,
  statusLabel,
  decimalCompare,
  criticalQuality,
  qualityText,
} from '../api'
import Icon from '../components/Icon.vue'
import Badge from '../components/Badge.vue'
import EmptyState from '../components/EmptyState.vue'
import PriceChart from '../components/PriceChart.vue'
const route = useRoute(),
  product = ref<any>(null),
  points = ref<any[]>([]),
  daily = ref<any[]>([]),
  resolution = ref('hour'),
  events = ref<any[]>([]),
  days = ref(7),
  display = ref(false),
  loading = ref(true),
  chartLoading = ref(false),
  error = ref(''),
  intervalSeconds = ref<number | undefined>()
let request = 0
async function history() {
  const n = ++request
  chartLoading.value = true
  try {
    const result = await api(`/products/${route.params.id}/history?days=${days.value}`)
    if (n === request) {
      intervalSeconds.value = result.interval_seconds ?? product.value?.interval_seconds
      points.value = itemsOf(result)
      daily.value = result.daily ?? []
      events.value = result.events ?? []
    }
  } catch (e: any) {
    error.value = e.message
  } finally {
    if (n === request) chartLoading.value = false
  }
}
onMounted(async () => {
  try {
    product.value = await api(`/products/${route.params.id}`)
    await history()
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
})
watch(days, () => {
  if (days.value > 180) resolution.value = 'daily'
  history()
})
watch(resolution, (value) => {
  if (value === 'daily') display.value = false
})
const validPrices = computed(() =>
  points.value
    .filter(
      (p) =>
        p.price !== null &&
        p.price !== undefined &&
        !criticalQuality(p.quality) &&
        !['closed', 'stopped', 'off_sale', 'unavailable', 'missing'].includes(p.status),
    )
    .map((p) => String(display.value ? (p.display_price ?? p.price) : p.price))
    .sort(decimalCompare),
)
const minPrice = computed(() =>
  resolution.value === 'daily'
    ? (daily.value.map((p) => String(p.min)).sort(decimalCompare)[0] ?? null)
    : (validPrices.value[0] ?? null),
)
const maxPrice = computed(() =>
  resolution.value === 'daily'
    ? (daily.value
        .map((p) => String(p.max))
        .sort(decimalCompare)
        .at(-1) ?? null)
    : (validPrices.value.at(-1) ?? null),
)
const chartPoints = computed(() => (resolution.value === 'daily' ? daily.value : points.value))
const basisChanged = computed(
  () =>
    new Set(
      chartPoints.value
        .filter((p) => p.price !== null || resolution.value === 'daily')
        .map((p) => `${p.unit}|${p.currency}|${p.display_quantity}`),
    ).size > 1,
)
</script>
<template>
  <RouterLink to="/products" class="back-link"
    ><Icon name="back" :size="16" />返回商品中心</RouterLink
  >
  <div v-if="error" class="notice error">{{ error }}</div>
  <div v-if="loading" class="loading-state"><span class="spinner"></span>正在读取商品…</div>
  <template v-else-if="product"
    ><section class="panel product-summary">
      <div class="product-summary-left">
        <div class="product-symbol"><Icon name="box" :size="30" /></div>
        <div>
          <div class="product-title-row">
            <h2>{{ product.name }}</h2>
            <Badge :value="product.status" />
          </div>
          <div class="product-meta">
            <span>{{ product.platform_name }}</span
            ><span>#{{ product.source_id }}</span
            ><span>{{ product.category_name ?? '未分类' }}</span
            ><span>账号：{{ product.account_label ?? '默认账号' }}</span>
          </div>
        </div>
      </div>
      <div class="current-price">
        <span>当前基础单价</span
        ><strong
          >{{ price(product.price, product.currency)
          }}<small v-if="product.unit"> / {{ product.unit }}</small></strong
        ><span>最近观测 {{ date(product.last_observed_at) }}</span>
      </div>
    </section>
    <div v-if="product.freshness === 'stale'" class="notice warning">
      <Icon name="alert" :size="17" />当前价格已过期，请检查平台采集任务与登录会话。
    </div>
    <div v-if="product.quality?.length" class="notice warning">
      <Icon name="alert" :size="17" />数据待核验：{{
        qualityText(product.quality)
      }}。单位或库存待核验不影响同商品原始价格走势；参与跨平台比较前需核对。
    </div>
    <div class="detail-metrics">
      <div class="panel detail-stat">
        <span>展示价格</span><strong>{{ price(product.display_price, product.currency) }}</strong
        ><small>{{ displayBasisLabel(product.display_quantity, product.unit) }}</small>
      </div>
      <div class="panel detail-stat">
        <span>区间最低价</span
        ><strong class="green-text">{{
          basisChanged ? '计价条件变化' : price(minPrice, product.currency)
        }}</strong
        ><small>最近 {{ days }} 天有效采样</small>
      </div>
      <div class="panel detail-stat">
        <span>区间最高价</span
        ><strong>{{ basisChanged ? '计价条件变化' : price(maxPrice, product.currency) }}</strong
        ><small>{{ display ? '展示价格' : '基础单价' }}口径</small>
      </div>
      <div class="panel detail-stat">
        <span>库存状态</span
        ><strong>{{
          product.stock_unlimited
            ? '不限库存'
            : product.stock === null || product.stock === undefined
              ? '未知'
              : decimalText(product.stock)
        }}</strong
        ><small>{{ points.length }} 个观测点</small>
      </div>
    </div>
    <section class="panel">
      <div class="panel-heading">
        <div>
          <h2>价格走势</h2>
          <p>
            {{ resolution === 'daily' ? '每日最低、均价与最高价格' : '小时采样价格' }} ·
            缺失、停售和计价条件变化时保留断点
          </p>
        </div>
        <div class="chart-controls">
          <select v-model="resolution" aria-label="采样粒度">
            <option value="hour">小时采样</option>
            <option value="daily">日聚合</option></select
          ><select v-model="display" :disabled="resolution === 'daily'" aria-label="价格口径">
            <option :value="false">基础单价</option>
            <option :value="true">展示价格</option></select
          ><select v-model.number="days" aria-label="历史时间范围">
            <option v-for="d in [1, 7, 30, 90, 180, 365, 730]" :key="d" :value="d">
              最近 {{ d }} 天
            </option>
          </select>
        </div>
      </div>
      <div v-if="chartLoading" class="loading-state">
        <span class="spinner"></span>正在读取走势…
      </div>
      <template v-else-if="chartPoints.length"
        ><div v-if="basisChanged" class="notice warning inset">
          该区间的单位、币种或展示数量发生过变化，走势已断开；区间最高和最低价格不宜直接比较。
        </div>
        <PriceChart
          :points="chartPoints"
          :display="display"
          :daily="resolution === 'daily'"
          :interval-seconds="intervalSeconds ?? product.interval_seconds" /></template
      ><EmptyState
        v-else
        icon="chart"
        title="当前区间暂无价格记录"
        description="选择其他时间范围或切换日聚合；小时明细默认保留180天。"
      />
    </section>
    <section class="panel">
      <div class="panel-heading">
        <div>
          <h2>商品变化记录</h2>
          <p>当前时间范围内的价格与状态事件</p>
        </div>
      </div>
      <div v-if="events.length" class="event-feed">
        <div v-for="event in events" :key="event.id" class="feed-item">
          <span class="feed-icon blue"><Icon name="clock" :size="17" /></span>
          <div class="feed-text">
            <strong>{{ event.title ?? statusLabel(event.kind) }}</strong>
            <p>{{ event.message }}</p>
          </div>
          <time>{{ date(event.created_at) }}</time>
        </div>
      </div>
      <EmptyState
        v-else
        icon="clock"
        title="暂无变化记录"
        description="首次采样建立基线，后续变化会在这里持续记录。"
      />
    </section>
    <details class="panel observations">
      <summary>
        查看价格采样记录 <span class="muted">{{ points.length }} 条</span>
      </summary>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>观测时间</th>
              <th>基础单价</th>
              <th>展示价格</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(point, i) in [...points].reverse()" :key="i">
              <td>{{ date(point.observed_at ?? point.scheduled_at) }}</td>
              <td class="mono">{{ price(point.price, product.currency) }}</td>
              <td class="mono">{{ price(point.display_price, product.currency) }}</td>
              <td><Badge :value="point.status" /></td>
            </tr>
          </tbody>
        </table>
      </div></details
  ></template>
</template>
