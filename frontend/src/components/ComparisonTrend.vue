<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { api, date, price, displayBasisLabel } from '../api'
import Icon from './Icon.vue'

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])
interface Point {
  at: string
  minimum: string | null
  maximum: string | null
  average: string | null
  offer_count: number
  platform_count: number
}
interface Basis {
  key: string
  currency: string
  unit: string
  display_quantity: string
  unit_requires_review: boolean
  current: {
    minimum: string | null
    maximum: string | null
    offer_count: number
    platform_count: number
  }
  items: Point[]
}
const props = defineProps<{ mappingId: number; refreshKey: number }>()
const groups = ref<Basis[]>([])
const selected = ref('')
const days = ref(7)
const loading = ref(false)
const error = ref('')
const el = ref<HTMLElement>()
let chart: echarts.ECharts | undefined
let observer: ResizeObserver | undefined
let controller: AbortController | undefined
let sequence = 0
let disposed = false
const basis = computed(() => groups.value.find((item) => item.key === selected.value))
const hasHistory = computed(() => basis.value?.items.some((item) => item.minimum !== null))
const escape = (value: unknown) =>
  String(value ?? '').replace(
    /[&<>"']/g,
    (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]!,
  )
async function load() {
  const request = ++sequence
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const result = await api<{ groups: Basis[] }>(
      `/mappings/${props.mappingId}/trend?days=${days.value}`,
      { signal: controller.signal },
    )
    if (disposed || request !== sequence) return
    groups.value = result.groups
    if (!groups.value.some((item) => item.key === selected.value))
      selected.value =
        [...groups.value].sort((a, b) => b.current.offer_count - a.current.offer_count)[0]?.key ??
        ''
    await nextTick()
    render()
  } catch (e: any) {
    if (!disposed && request === sequence) {
      groups.value = []
      error.value = e.message
      chart?.clear()
    }
  } finally {
    if (!disposed && request === sequence) loading.value = false
  }
}
function render() {
  if (!el.value || disposed) return
  if (!hasHistory.value) {
    chart?.clear()
    return
  }
  if (!chart) {
    chart = echarts.init(el.value)
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(el.value)
  }
  const item = basis.value
  const points = item?.items ?? []
  chart.setOption(
    {
      animation: false,
      legend: {
        data: ['最低参考价', '最高参考价'],
        top: 6,
        textStyle: { color: '#6f8099', fontSize: 11 },
      },
      tooltip: {
        trigger: 'axis',
        confine: true,
        formatter: (values: any[]) => {
          const point = points[values[0]?.dataIndex]
          if (!point || !item) return ''
          return `${escape(date(point.at))}<br/>最低 ${escape(price(point.minimum, item.currency))}<br/>最高 ${escape(price(point.maximum, item.currency))}<br/>${point.offer_count} 条有效报价 · ${point.platform_count} 个平台`
        },
      },
      grid: { left: 65, right: 20, top: 42, bottom: 35 },
      xAxis: {
        type: 'time',
        name: '北京时间',
        nameLocation: 'middle',
        nameGap: 25,
        axisLabel: {
          fontSize: 10,
          formatter: (value: number) => date(new Date(value).toISOString(), true),
        },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        scale: true,
        name: item?.currency ?? '基础单价',
        axisLabel: { fontSize: 10 },
        splitLine: { lineStyle: { type: 'dashed', color: '#edf1f7' } },
      },
      series: [
        ['minimum', '最低参考价', '#19866f'],
        ['maximum', '最高参考价', '#3b6ef5'],
      ].map(([field, name, color]) => ({
        type: 'line',
        name,
        showSymbol: points.length < 30,
        connectNulls: false,
        lineStyle: { width: 2 },
        itemStyle: { color },
        data: points.map((p) => [
          p.at,
          p[field as 'minimum' | 'maximum'] === null
            ? null
            : Number(p[field as 'minimum' | 'maximum']),
        ]),
      })),
    },
    true,
  )
  // The chart can have been hidden by an empty basis. Its container must be
  // measured again after switching back to a basis with actual observations.
  chart.resize()
}
watch([days, () => props.mappingId, () => props.refreshKey], load)
watch(selected, async () => {
  await nextTick()
  render()
})
onMounted(load)
onBeforeUnmount(() => {
  disposed = true
  sequence++
  controller?.abort()
  observer?.disconnect()
  chart?.dispose()
})
</script>
<template>
  <section class="panel aggregate-trend" :aria-busy="loading">
    <div class="trend-heading">
      <div>
        <h2>汇总价格走势</h2>
        <p>当前关联商品的最低、最高基础报价 · 每小时采样</p>
      </div>
      <div class="trend-controls">
        <select v-model="days" aria-label="汇总走势时间范围">
          <option :value="1">最近1天</option>
          <option :value="7">最近7天</option>
          <option :value="30">最近30天</option></select
        ><button class="btn btn-white btn-sm" :disabled="loading" @click="load">
          <Icon name="refresh" :size="14" />刷新
        </button>
      </div>
    </div>
    <div v-if="error" class="notice error" role="alert">{{ error }}</div>
    <template v-if="groups.length">
      <label class="basis-select"
        >统计口径<select v-model="selected" aria-label="汇总价格统计口径">
          <option v-for="item in groups" :key="item.key" :value="item.key">
            {{ item.currency }} · {{ displayBasisLabel(item.display_quantity, item.unit) }}
          </option>
        </select></label
      >
      <div class="trend-extremes">
        <div>
          <span>当前最低参考价</span
          ><strong>{{ price(basis?.current.minimum, basis?.currency) }}</strong>
        </div>
        <div>
          <span>当前最高参考价</span
          ><strong>{{ price(basis?.current.maximum, basis?.currency) }}</strong>
        </div>
        <div>
          <span>有效在售报价</span
          ><strong
            >{{ basis?.current.offer_count ?? 0 }}
            <small>条 / {{ basis?.current.platform_count ?? 0 }} 个平台</small></strong
          >
        </div>
      </div>
      <p class="trend-note">
        相同币种、源单位和展示数量分别统计；{{
          basis?.unit_requires_review
            ? '当前口径单位待核验，价格仅供参考。'
            : '同类商品的规格、品质和售后仍需核对。'
        }}
        停售、陈旧或价格异常报价不计入当前极值。
      </p>
    </template>
    <div
      v-show="hasHistory"
      ref="el"
      class="aggregate-chart"
      aria-label="统一商品最低最高价格走势图"
    ></div>
    <p v-if="!hasHistory && !error" class="trend-empty">
      {{ loading ? '正在读取价格观测…' : '该口径暂无有效历史报价，后续成功采集后会持续积累。' }}
    </p>
    <p class="trend-note">
      按当前关联及已授权商品回看历史；每小时每件商品采用最后一次观测。缺失时段留空，不补造历史价格。
    </p>
  </section>
</template>
<style scoped>
.aggregate-trend {
  padding: 20px;
  margin-bottom: 20px;
  min-width: 0;
}
.trend-heading,
.trend-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.trend-heading h2 {
  font-size: 16px;
  margin: 0 0 7px;
}
.trend-heading p,
.trend-note {
  font-size: 12px;
  color: #74849a;
  line-height: 1.8;
}
.trend-controls select {
  width: auto;
}
.basis-select {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  margin: 18px 0;
}
.basis-select select {
  width: auto;
  max-width: 100%;
}
.trend-extremes {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin: 18px 0;
}
.trend-extremes span {
  display: block;
  color: #74849a;
  font-size: 12px;
  margin-bottom: 8px;
}
.trend-extremes strong {
  font-size: 22px;
  overflow-wrap: anywhere;
}
.trend-extremes small {
  font-size: 12px;
  font-weight: 400;
}
.aggregate-chart {
  height: 290px;
  width: 100%;
}
.trend-empty {
  padding: 30px;
  text-align: center;
  color: #74849a;
  font-size: 13px;
}
.trend-note {
  margin: 10px 0 0;
}
@media (max-width: 600px) {
  .aggregate-trend {
    padding: 15px;
  }
  .trend-extremes {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .trend-extremes strong {
    font-size: 18px;
  }
  .basis-select {
    display: block;
  }
  .basis-select select {
    width: 100%;
    margin-top: 7px;
  }
}
</style>
