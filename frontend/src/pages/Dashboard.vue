<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { areaPath } from '../navigation'
import { api, date, statusLabel } from '../api'
import Icon from '../components/Icon.vue'
const route = useRoute()
const adminArea = computed(() => Boolean(route.meta?.admin))
import Badge from '../components/Badge.vue'
import EmptyState from '../components/EmptyState.vue'
const data = ref<any>(null),
  loading = ref(true),
  error = ref('')
onMounted(async () => {
  try {
    data.value = await api('/dashboard')
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
})
const metrics = computed(() => [
  {
    label: '接入交易平台',
    value: data.value?.platform_count ?? 0,
    icon: 'platforms',
    color: 'blue',
    hint: adminArea.value ? '独立规则 · 统一管理' : '仅显示已授权平台',
  },
  {
    label: '监控商品总数',
    value: data.value?.product_count ?? 0,
    icon: 'box',
    color: 'purple',
    hint: '持续积累每小时价格快照',
  },
  {
    label: '当前在售商品',
    value: data.value?.active_count ?? 0,
    icon: 'chart',
    color: 'green',
    hint: '参与有效价格比较',
  },
  {
    label: '停售商品',
    value: data.value?.closed_count ?? 0,
    icon: 'clock',
    color: 'orange',
    hint: '保留历史 · 跟踪恢复',
  },
])
const bars = computed(() => {
  const points = data.value?.daily_counts ?? []
  const max = Math.max(1, ...points.map((p: any) => p.count))
  return points.map((p: any) => ({
    ...p,
    percent: Math.max(p.count > 0 ? 4 : 0, (p.count / max) * 100),
  }))
})
</script>
<template>
  <div v-if="error" class="notice error">{{ error }}</div>
  <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载监控概况…</div>
  <template v-else-if="data"
    ><div class="overview-banner">
      <div class="banner-icon"><Icon name="chart" :size="24" /></div>
      <div>
        <h3>价格变化，尽在掌握</h3>
        <p>
          {{
            data.last_collection_at
              ? `最近采集：${date(data.last_collection_at)}`
              : adminArea
                ? '连接第一个平台，开始建立价格历史。'
                : '暂时没有可查看的价格数据，请联系管理员分配平台。'
          }}
        </p>
      </div>
      <RouterLink v-if="adminArea" to="/admin/platforms" class="btn btn-white"
        >管理平台<Icon name="arrow" :size="15"
      /></RouterLink>
    </div>
    <div v-if="adminArea && data.auth_expired_count" class="notice warning">
      <Icon name="alert" :size="18" />{{
        data.auth_expired_count
      }}
      个平台的采集会话需要连接或更新。请在平台管理中处理。<RouterLink
        v-if="adminArea"
        to="/admin/platforms"
        >前往处理 →</RouterLink
      >
    </div>
    <div class="metric-grid">
      <div v-for="metric in metrics" :key="metric.label" class="metric-card">
        <div class="metric-top">
          <span>{{ metric.label }}</span>
          <div class="metric-icon" :class="metric.color">
            <Icon :name="metric.icon" :size="20" />
          </div>
        </div>
        <strong class="metric-value">{{ Number(metric.value).toLocaleString() }}</strong>
        <div class="metric-hint">{{ metric.hint }}</div>
      </div>
    </div>
    <div class="dashboard-grid">
      <section class="panel">
        <div class="panel-heading">
          <div>
            <h2>采集运行情况</h2>
            <p>最近批次的执行状态</p>
          </div>
          <RouterLink v-if="adminArea" to="/admin/runs" class="text-link"
            >查看全部<Icon name="arrow" :size="14"
          /></RouterLink>
        </div>
        <div class="table-wrap">
          <table v-if="data.recent_runs?.length">
            <thead>
              <tr>
                <th>交易平台</th>
                <th>计划时间</th>
                <th>商品数</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="run in data.recent_runs" :key="run.id">
                <td class="strong">{{ run.platform_name ?? `平台 #${run.platform_id}` }}</td>
                <td>{{ date(run.scheduled_at, true) }}</td>
                <td class="mono">{{ run.products_seen ?? 0 }}</td>
                <td><Badge :value="run.status" /></td>
              </tr>
            </tbody>
          </table>
          <EmptyState
            v-else
            icon="clock"
            title="尚无采集任务"
            :description="
              adminArea
                ? '添加平台并连接采集会话后，即可运行首次采集。'
                : '已授权平台采集完成后，这里显示数据状态。'
            "
            ><RouterLink v-if="adminArea" to="/admin/platforms" class="btn btn-primary btn-sm"
              >接入平台</RouterLink
            ></EmptyState
          >
        </div>
      </section>
      <section class="panel">
        <div class="panel-heading">
          <div>
            <h2>采集数据量</h2>
            <p>每日成功写入的价格观测</p>
          </div>
          <span class="subtle-tag">最近 14 天</span>
        </div>
        <div v-if="bars.length" class="bar-chart">
          <div class="bar-columns">
            <div v-for="point in bars" :key="point.date" class="bar-column">
              <span class="bar-count">{{ point.count }}</span>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  :style="{ height: `${point.percent}%` }"
                  :title="`${point.date}：${point.count} 条`"
                ></div>
              </div>
              <span class="bar-date">{{ String(point.date).slice(5) }}</span>
            </div>
          </div>
        </div>
        <EmptyState
          v-else
          icon="chart"
          title="等待价格观测"
          description="采集成功后，这里将展示每日数据量。"
        />
      </section>
    </div>
    <section class="panel">
      <div class="panel-heading">
        <div>
          <h2>最近事件</h2>
          <p>价格异动与采集异常，及时知晓</p>
        </div>
        <RouterLink :to="areaPath('/events', adminArea)" class="text-link"
          >查看全部<Icon name="arrow" :size="14"
        /></RouterLink>
      </div>
      <div v-if="data.recent_events?.length" class="event-feed">
        <div v-for="event in data.recent_events" :key="event.id" class="feed-item">
          <span class="feed-icon" :class="event.severity === 'error' ? 'danger' : 'blue'"
            ><Icon
              :name="
                event.kind?.includes('price')
                  ? 'chart'
                  : event.kind?.includes('session')
                    ? 'key'
                    : 'bell'
              "
              :size="17"
          /></span>
          <div class="feed-text">
            <strong>{{ event.title ?? statusLabel(event.kind) }}</strong>
            <p>{{ event.message }}</p>
          </div>
          <time>{{ date(event.created_at, true) }}</time>
        </div>
      </div>
      <EmptyState
        v-else
        icon="bell"
        title="暂无新事件"
        description="首次采集建立价格基线，后续价格和状态变化会出现在这里。"
      /></section
  ></template>
</template>
