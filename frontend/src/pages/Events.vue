<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { areaPath } from '../navigation'
import { api, itemsOf, params, json, auth, date, statusLabel } from '../api'
import Icon from '../components/Icon.vue'
const route = useRoute()
const adminArea = computed(() => Boolean(route.meta?.admin))
import Badge from '../components/Badge.vue'
import EmptyState from '../components/EmptyState.vue'
import Pagination from '../components/Pagination.vue'
const events = ref<any[]>([]),
  loading = ref(true),
  error = ref(''),
  kind = ref(''),
  acknowledged = ref(''),
  page = ref(1),
  total = ref(0),
  busy = ref<number | null>(null),
  admin = computed(() => auth.user?.role === 'admin' && adminArea.value)
let request = 0
async function load() {
  const n = ++request
  loading.value = true
  error.value = ''
  try {
    const result = await api(
      `/events${params({ kind: kind.value, acknowledged: acknowledged.value, page: page.value, page_size: 20 })}`,
    )
    if (n === request) {
      events.value = itemsOf(result)
      total.value = result.total ?? events.value.length
    }
  } catch (e: any) {
    if (n === request) error.value = e.message
  } finally {
    if (n === request) loading.value = false
  }
}
watch([kind, acknowledged], () => {
  page.value = 1
  load()
})
onMounted(load)
function changePage(p: number) {
  page.value = p
  load()
}
async function ack(event: any) {
  if (!admin.value) return
  busy.value = event.id
  error.value = ''
  try {
    await api(`/events/${event.id}/ack`, json('POST'))
    event.acknowledged = true
    if (acknowledged.value === 'false') await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = null
  }
}
</script>
<template>
  <section class="panel">
    <div class="panel-toolbar">
      <div class="filters">
        <select v-model="kind" aria-label="事件类型">
          <option value="">全部事件类型</option>
          <option value="price_changed">价格变化</option>
          <option value="closed">商品停售</option>
          <option value="resumed">恢复在售</option>
          <option value="renamed">商品更名</option>
          <option value="basis_changed">计价条件变化</option>
          <option value="auth_expired">登录过期</option>
          <option value="collection_failed">采集失败</option></select
        ><select v-model="acknowledged" aria-label="处理状态">
          <option value="">全部处理状态</option>
          <option value="false">待确认</option>
          <option value="true">已确认</option>
        </select>
      </div>
      <span class="result-count">{{ total.toLocaleString() }} 条事件</span>
    </div>
    <div v-if="error" class="notice error inset">{{ error }}</div>
    <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载事件…</div>
    <div v-else-if="events.length" class="events-list">
      <article
        v-for="event in events"
        :key="event.id"
        class="event-row"
        :class="{ acknowledged: event.acknowledged }"
      >
        <span
          class="event-status-icon"
          :class="
            event.severity === 'error' ? 'danger' : event.severity === 'warning' ? 'orange' : 'blue'
          "
          ><Icon
            :name="
              event.kind?.includes('price')
                ? 'chart'
                : event.kind === 'auth_expired' || event.kind?.includes('session')
                  ? 'key'
                  : 'bell'
            "
            :size="21"
        /></span>
        <div class="event-row-body">
          <div class="event-title">
            <h3>{{ event.title ?? statusLabel(event.kind) }}</h3>
            <Badge :value="event.kind" /><span v-if="event.acknowledged" class="ack-label"
              ><Icon name="check" :size="12" />已确认</span
            >
          </div>
          <p>{{ event.message }}</p>
          <div class="event-row-meta">
            <span>{{ date(event.created_at) }}</span
            ><span v-if="event.platform_name">{{ event.platform_name }}</span
            ><RouterLink
              v-if="event.product_id"
              :to="areaPath(`/products/${event.product_id}`, adminArea)"
              >查看商品 →</RouterLink
            >
          </div>
        </div>
        <button
          v-if="admin && !event.acknowledged"
          class="btn btn-white btn-sm"
          :disabled="busy === event.id"
          @click="ack(event)"
        >
          {{ busy === event.id ? '处理中…' : '标记已确认' }}
        </button>
      </article>
    </div>
    <EmptyState
      v-else
      icon="bell"
      title="暂无符合条件的事件"
      description="平台采集后的价格、名称与销售状态变化会自动记录。"
    /><Pagination v-if="total" :page="page" :total="total" @change="changePage" />
  </section>
</template>
