<script setup lang="ts">
import { onMounted, onBeforeUnmount, watch, ref } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { date, price } from '../api'
import { buildChartData } from '../chartData'
echarts.use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  CanvasRenderer,
])
const props = withDefaults(
  defineProps<{
    points: any[]
    height?: number
    display?: boolean
    daily?: boolean
    intervalSeconds?: number
  }>(),
  { height: 310, display: false, daily: false },
)
const el = ref<HTMLElement>()
let chart: echarts.ECharts | undefined
let observer: ResizeObserver | undefined
const escape = (text: unknown) =>
  String(text ?? '').replace(
    /[&<>"']/g,
    (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]!,
  )
function render() {
  if (!chart) return
  const makeSeries = (field: string, name: string, color: string, area = false) => ({
    name,
    type: 'line',
    symbol: 'circle',
    symbolSize: 5,
    showSymbol: props.points.length < 35,
    connectNulls: false,
    lineStyle: { width: field === 'avg' || !props.daily ? 2.5 : 1.4 },
    itemStyle: { color },
    ...(area
      ? {
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(59,110,245,.16)' },
              { offset: 1, color: 'rgba(59,110,245,.01)' },
            ]),
          },
        }
      : {}),
    data: buildChartData(props.points, {
      field,
      daily: props.daily,
      display: props.display,
      intervalSeconds: props.intervalSeconds,
    }),
  })
  chart.setOption(
    {
      animationDuration: 400,
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#fff',
        borderColor: '#e7eaf0',
        textStyle: { color: '#27344b' },
        formatter: (params: any) => {
          const point = params.find((p: any) => p.value?.[2] !== null)?.value ?? params?.[0]?.value
          if (!point) return ''
          const amount = point[2] === null ? '缺失 / 暂停销售' : escape(price(point[2], point[3]))
          const unit = point[4] ? ` / ${escape(point[4])}` : ''
          return props.daily && point[2] !== null
            ? `${escape(point[0])}<br/>最低 ${escape(price(point[5], point[3]))}${unit}<br/>最高 ${escape(price(point[6], point[3]))}${unit}<br/>均价 ${escape(price(point[7], point[3]))}${unit}<br/>采样 ${escape(point[8])} 次`
            : `${date(point[0])}<br/><strong>${amount}${unit}</strong>`
        },
      },
      ...(props.daily
        ? {
            legend: {
              data: ['最低价', '均价', '最高价'],
              top: 5,
              textStyle: { fontSize: 10, color: '#8c9bb3' },
              itemWidth: 12,
              itemHeight: 3,
            },
          }
        : {}),
      grid: { left: 67, right: 25, top: props.daily ? 44 : 24, bottom: 48 },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: '#e9edf4' } },
        axisLabel: {
          color: '#8993a7',
          hideOverlap: true,
          formatter: (value: number) =>
            new Intl.DateTimeFormat('zh-CN', {
              timeZone: 'Asia/Shanghai',
              month: '2-digit',
              day: '2-digit',
              ...(props.points.length < 30 && !props.daily ? { hour: '2-digit' } : {}),
            }).format(new Date(value)),
        },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: '#8993a7' },
        splitLine: { lineStyle: { color: '#eef1f6', type: 'dashed' } },
        axisLine: { show: false },
      },
      series: props.daily
        ? [
            makeSeries('min', '最低价', '#3eaa8d'),
            makeSeries('avg', '均价', '#3b6ef5'),
            makeSeries('max', '最高价', '#d5a04e'),
          ]
        : [makeSeries('price', '采样价格', '#3b6ef5', true)],
    },
    true,
  )
}
onMounted(() => {
  if (el.value) {
    chart = echarts.init(el.value)
    render()
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(el.value)
  }
})
watch(() => [props.points, props.display, props.daily, props.intervalSeconds], render, {
  deep: true,
})
onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
})
</script>
<template>
  <div ref="el" class="price-chart" :style="{ height: `${height}px` }" aria-label="价格趋势图" />
</template>
