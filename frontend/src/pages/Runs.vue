<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api, itemsOf, params, date } from '../api'
import Icon from '../components/Icon.vue'
import Badge from '../components/Badge.vue'
import EmptyState from '../components/EmptyState.vue'
import Pagination from '../components/Pagination.vue'
import Modal from '../components/Modal.vue'
const runs = ref<any[]>([]),
  platforms = ref<any[]>([]),
  loading = ref(true),
  error = ref(''),
  platform = ref(''),
  status = ref(''),
  page = ref(1),
  total = ref(0),
  selected = ref<any>(null)
let request = 0
async function load() {
  const n = ++request
  loading.value = true
  error.value = ''
  try {
    const result = await api(
      `/runs${params({ platform_id: platform.value, status: status.value, page: page.value, page_size: 20 })}`,
    )
    if (n === request) {
      runs.value = itemsOf(result)
      total.value = result.total ?? runs.value.length
    }
  } catch (e: any) {
    if (n === request) error.value = e.message
  } finally {
    if (n === request) loading.value = false
  }
}
watch([platform, status], () => {
  page.value = 1
  load()
})
onMounted(async () => {
  await Promise.all([
    load(),
    api('/platforms')
      .then((r) => (platforms.value = itemsOf(r)))
      .catch(() => {}),
  ])
})
function changePage(p: number) {
  page.value = p
  load()
}
function duration(r: any) {
  if (!r.started_at) return '—'
  const end = r.completed_at ? new Date(r.completed_at).getTime() : Date.now()
  const seconds = Math.max(0, Math.round((end - new Date(r.started_at).getTime()) / 1000))
  return seconds >= 60 ? `${Math.floor(seconds / 60)}分 ${seconds % 60}秒` : `${seconds}秒`
}
</script>
<template>
  <section class="panel">
    <div class="panel-toolbar">
      <div class="filters">
        <select v-model="platform" aria-label="筛选平台">
          <option value="">全部交易平台</option>
          <option v-for="p in platforms" :key="p.id" :value="p.id">{{ p.name }}</option></select
        ><select v-model="status" aria-label="任务状态">
          <option value="">全部任务状态</option>
          <option value="queued">排队中</option>
          <option value="running">采集中</option>
          <option value="success">成功</option>
          <option value="partial">不完整</option>
          <option value="failed">失败</option>
          <option value="auth_expired">登录过期</option>
        </select>
      </div>
      <button class="btn btn-white btn-sm" @click="load">
        <Icon name="refresh" :size="15" />更新状态
      </button>
    </div>
    <div v-if="error" class="notice error inset">{{ error }}</div>
    <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载任务…</div>
    <div v-else class="table-wrap">
      <table v-if="runs.length">
        <thead>
          <tr>
            <th>批次 / 交易平台</th>
            <th>计划时间</th>
            <th>执行耗时</th>
            <th>采集商品</th>
            <th>完整性</th>
            <th>任务状态</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in runs" :key="run.id">
            <td>
              <strong>{{ run.platform_name }}</strong>
              <div class="table-secondary mono">批次 #{{ run.id }}</div>
            </td>
            <td>{{ date(run.scheduled_at) }}</td>
            <td class="mono">{{ duration(run) }}</td>
            <td>
              <strong class="mono">{{ run.products_seen ?? 0 }}</strong
              ><span class="muted"> / {{ run.expected_total ?? '—' }}</span>
            </td>
            <td>
              <span v-if="run.error_count" class="danger-text">{{ run.error_count }} 个错误</span
              ><span v-else-if="run.status === 'success'" class="green-text">完整</span
              ><span v-else class="muted">待确认</span>
              <div class="table-secondary">已尝试 {{ run.attempts ?? 1 }} 次</div>
            </td>
            <td><Badge :value="run.status" /></td>
            <td>
              <button class="table-action" @click="selected = run">
                详情<Icon name="arrow" :size="14" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <EmptyState
        v-else
        icon="clock"
        title="暂无采集任务"
        description="平台接入后可手动发起首次采集，后续由定时任务自动执行。"
      />
    </div>
    <Pagination v-if="total" :page="page" :total="total" @change="changePage" />
  </section>
  <div class="page-note">
    <Icon
      name="shield"
      :size="15"
    />不完整批次不会把未采到的商品标记为下架。登录过期后会暂停平台采集。
  </div>
  <Modal v-if="selected" :title="`采集批次 #${selected.id}`" @close="selected = null"
    ><dl class="detail-list">
      <div>
        <dt>交易平台</dt>
        <dd>{{ selected.platform_name }}</dd>
      </div>
      <div>
        <dt>执行状态</dt>
        <dd><Badge :value="selected.status" /></dd>
      </div>
      <div>
        <dt>计划时间</dt>
        <dd>{{ date(selected.scheduled_at) }}</dd>
      </div>
      <div>
        <dt>开始时间</dt>
        <dd>{{ date(selected.started_at) }}</dd>
      </div>
      <div>
        <dt>结束时间</dt>
        <dd>{{ date(selected.completed_at) }}</dd>
      </div>
      <div>
        <dt>采集商品</dt>
        <dd>{{ selected.products_seen ?? 0 }} / {{ selected.expected_total ?? '未知' }}</dd>
      </div>
      <div>
        <dt>尝试次数</dt>
        <dd>{{ selected.attempts ?? 1 }}</dd>
      </div>
    </dl>
    <div v-if="selected.error" class="notice error">{{ selected.error }}</div>
    <p v-else class="form-help">当前批次未返回错误详情。</p></Modal
  >
</template>
