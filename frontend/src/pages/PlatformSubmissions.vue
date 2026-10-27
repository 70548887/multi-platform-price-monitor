<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, auth, date, itemsOf, json, params } from '../api'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'
import Pagination from '../components/Pagination.vue'
import EmptyState from '../components/EmptyState.vue'

interface Submission {
  id: number
  name: string
  url: string
  notes: string
  status: string
  admin_note: string
  created_at: string
  reviewed_at: string | null
  submitted_by?: string
}
const route = useRoute()
const admin = computed(() => Boolean(route.meta?.admin) && auth.user?.role === 'admin')
const items = ref<Submission[]>([]),
  loading = ref(true),
  saving = ref(false),
  error = ref(''),
  notice = ref(''),
  formError = ref('')
const page = ref(1),
  total = ref(0),
  status = ref(''),
  showForm = ref(false),
  reviewing = ref<Submission | null>(null)
const form = reactive({ name: '', url: '', notes: '' })
const review = reactive({ status: 'accepted', admin_note: '' })
let sequence = 0,
  disposed = false
const labels: Record<string, string> = { pending: '待审核', accepted: '已采纳', rejected: '未采纳' }
async function load() {
  const request = ++sequence
  loading.value = true
  error.value = ''
  try {
    const result = await api(
      `/platform-submissions${params({ page: page.value, page_size: 20, status: status.value })}`,
    )
    if (!disposed && request === sequence) {
      items.value = itemsOf<Submission>(result)
      total.value = result.total
      if (page.value > 1 && !items.value.length && total.value > 0) {
        page.value = Math.ceil(total.value / 20)
        await load()
      }
    }
  } catch (e: any) {
    if (!disposed && request === sequence) {
      items.value = []
      error.value = e.message
    }
  } finally {
    if (!disposed && request === sequence) loading.value = false
  }
}
function open() {
  Object.assign(form, { name: '', url: '', notes: '' })
  formError.value = ''
  showForm.value = true
}
async function submit() {
  if (saving.value || disposed) return
  saving.value = true
  formError.value = ''
  try {
    await api('/platform-submissions', json('POST', form))
    if (disposed) return
    showForm.value = false
    notice.value = '平台已提交，管理员审核后会反馈结果。'
    page.value = 1
    await load()
  } catch (e: any) {
    if (!disposed) formError.value = e.message
  } finally {
    if (!disposed) saving.value = false
  }
}
function openReview(item: Submission) {
  if (!admin.value) return
  reviewing.value = item
  Object.assign(review, { status: 'accepted', admin_note: '' })
  formError.value = ''
}
async function saveReview() {
  if (!admin.value || !reviewing.value || saving.value || disposed) return
  saving.value = true
  formError.value = ''
  try {
    await api(`/platform-submissions/${reviewing.value.id}`, json('PATCH', review))
    if (disposed) return
    reviewing.value = null
    notice.value = '审核结果已保存。采纳后请在平台管理中配置适配版本与采集账号。'
    await load()
  } catch (e: any) {
    if (!disposed) formError.value = e.message
  } finally {
    if (!disposed) saving.value = false
  }
}
function changePage(value: number) {
  page.value = value
  load()
}
function filter() {
  page.value = 1
  load()
}
onMounted(load)
onBeforeUnmount(() => {
  disposed = true
  sequence++
})
</script>
<template>
  <div class="submission-tools">
    <p>
      {{
        admin
          ? '审核成员推荐的平台，采纳后再配置采集连接。'
          : '推荐希望收录的交易平台，并查看自己的提交结果。'
      }}
    </p>
    <button v-if="!admin" class="btn btn-primary" @click="open">
      <Icon name="plus" :size="16" />提交平台网址
    </button>
  </div>
  <div v-if="error" class="notice error" role="alert">{{ error }}</div>
  <div v-if="notice" class="notice success">{{ notice }}</div>
  <section class="panel submission-panel">
    <div class="submission-filters">
      <select v-model="status" aria-label="收录申请状态" @change="filter">
        <option value="">全部状态</option>
        <option value="pending">待审核</option>
        <option value="accepted">已采纳</option>
        <option value="rejected">未采纳</option></select
      ><button class="btn btn-white btn-sm" :disabled="loading" @click="load">刷新</button
      ><span>{{ total }} 条申请</span>
    </div>
    <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载收录申请…</div>
    <div v-else-if="items.length" class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>平台 / 网址</th>
            <th v-if="admin">提交成员</th>
            <th>收录说明</th>
            <th>状态 / 审核反馈</th>
            <th>提交时间</th>
            <th v-if="admin"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.id">
            <td class="submission-site">
              <strong>{{ item.name }}</strong
              ><a :href="item.url" target="_blank" rel="noopener noreferrer"
                >{{ item.url }}<Icon name="external" :size="12"
              /></a>
            </td>
            <td v-if="admin">{{ item.submitted_by }}</td>
            <td class="submission-notes">{{ item.notes || '—' }}</td>
            <td class="submission-notes">
              <span
                class="badge"
                :class="
                  item.status === 'accepted'
                    ? 'success'
                    : item.status === 'pending'
                      ? 'warning'
                      : 'neutral'
                "
                >{{ labels[item.status] || item.status }}</span
              >
              <p v-if="item.admin_note">{{ item.admin_note }}</p>
              <small v-if="item.reviewed_at">{{ date(item.reviewed_at) }}</small>
            </td>
            <td>{{ date(item.created_at) }}</td>
            <td v-if="admin">
              <button v-if="item.status === 'pending'" class="text-link" @click="openReview(item)">
                审核</button
              ><span v-else class="muted">已处理</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <EmptyState
      v-else
      icon="globe"
      title="暂无收录申请"
      :description="
        admin
          ? '成员提交的平台会在这里等待审核。'
          : '可以提交希望加入监控的平台，勿填写密码、Token 或代理凭据。'
      "
    />
    <Pagination v-if="total" :page="page" :total="total" :page-size="20" @change="changePage" />
  </section>
  <Modal v-if="showForm" title="提交平台网址" @close="!saving && (showForm = false)"
    ><form @submit.prevent="submit">
      <div v-if="formError" class="notice error">{{ formError }}</div>
      <label
        >平台名称<input
          v-model="form.name"
          required
          maxlength="120"
          placeholder="希望收录的平台名称"
          :disabled="saving" /></label
      ><label
        >平台网址<input
          v-model="form.url"
          type="url"
          required
          maxlength="2000"
          placeholder="https://平台域名"
          :disabled="saving" /></label
      ><label
        >收录说明<textarea
          v-model="form.notes"
          rows="3"
          maxlength="2000"
          placeholder="介绍平台类型、商品范围等，不要填写账号密码或 Token"
          :disabled="saving"
        />
      </label>
      <p class="form-help">
        仅保留网址的公开域名，路径和查询参数不会保存。每人最多 5
        条待审核申请，采纳不代表已经接入采集。
      </p>
      <div class="modal-actions">
        <button type="button" class="btn btn-white" :disabled="saving" @click="showForm = false">
          取消</button
        ><button class="btn btn-primary" :disabled="saving">
          {{ saving ? '提交中…' : '提交审核' }}
        </button>
      </div>
    </form></Modal
  >
  <Modal v-if="reviewing" title="审核平台收录" @close="!saving && (reviewing = null)"
    ><form @submit.prevent="saveReview">
      <div v-if="formError" class="notice error">{{ formError }}</div>
      <p>{{ reviewing.name }} · {{ reviewing.url }}</p>
      <label
        >审核结果<select v-model="review.status" :disabled="saving">
          <option value="accepted">采纳建议</option>
          <option value="rejected">暂不收录</option>
        </select></label
      ><label
        >回复成员<textarea
          v-model="review.admin_note"
          rows="3"
          maxlength="1000"
          placeholder="说明审核结果或后续接入安排"
          :disabled="saving"
        />
      </label>
      <p class="form-help">
        采纳建议只更新审核状态。接入、账号授权及采集规则仍需在平台管理中配置。
      </p>
      <div class="modal-actions">
        <button type="button" class="btn btn-white" :disabled="saving" @click="reviewing = null">
          取消</button
        ><button class="btn btn-primary" :disabled="saving">
          {{ saving ? '保存中…' : '保存审核结果' }}
        </button>
      </div>
    </form></Modal
  >
</template>
<style scoped>
.submission-tools,
.submission-filters {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
}
.submission-tools {
  margin-bottom: 20px;
}
.submission-tools p {
  font-size: 13px;
  color: #74849a;
}
.submission-filters {
  padding: 16px 20px;
  justify-content: flex-start;
  border-bottom: 1px solid #edf1f7;
}
.submission-filters select {
  width: 150px;
}
.submission-filters > span {
  margin-left: auto;
  font-size: 12px;
  color: #74849a;
}
.submission-site {
  min-width: 180px;
  max-width: 300px;
  white-space: normal;
  overflow-wrap: anywhere;
}
.submission-site strong,
.submission-site a {
  display: block;
}
.submission-site a {
  margin-top: 8px;
  font-size: 12px;
}
.submission-notes {
  white-space: pre-wrap;
  min-width: 140px;
  max-width: 300px;
  line-height: 1.8;
}
.submission-notes p {
  margin-top: 8px;
}
.submission-notes small {
  color: #74849a;
}
</style>
