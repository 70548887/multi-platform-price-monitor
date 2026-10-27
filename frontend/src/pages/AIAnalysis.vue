<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api, auth, date, itemsOf, json, params, price } from '../api'
import Pagination from '../components/Pagination.vue'
import EmptyState from '../components/EmptyState.vue'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'
type Tab = 'overview' | 'suggestions' | 'reports' | 'applications'
type Action = 'run' | 'stop' | 'retry' | 'apply' | 'reject' | 'undo'
const tab = ref<Tab>('overview')
const tabs: { key: Tab; label: string; icon: string }[] = [
  { key: 'overview', label: '运行概览', icon: 'dashboard' },
  { key: 'suggestions', label: '待审核建议', icon: 'shield' },
  { key: 'reports', label: '分析报告', icon: 'file' },
  { key: 'applications', label: '处理记录', icon: 'clock' },
]
const overview = ref<any>(null),
  config = ref<any>(null),
  runs = ref<any[]>([]),
  report = ref<any>(null)
const suggestions = ref<any[]>([]),
  applications = ref<any[]>([]),
  platforms = ref<any[]>([])
const total = ref(0),
  page = ref(1),
  loading = ref(true),
  busy = ref(false),
  detailLoading = ref(false),
  error = ref(''),
  notice = ref('')
const noticeKind = ref<'success' | 'warning'>('success')
const filters = reactive({ status: '', platform_id: '', date_from: '', date_to: '' }),
  suggestionStatus = ref('pending'),
  applicationStatus = ref('')
const confirm = ref<{ action: Action; item?: any } | null>(null),
  editing = ref<any>(null),
  editLoading = ref(false),
  editError = ref('')
const categories = ref<any[]>([]),
  groups = ref<any[]>([]),
  edit = reactive({ category_id: '', group_id: '', group_name: '' })
let disposed = false,
  request = 0,
  detailRequest = 0,
  editRequest = 0,
  timer: ReturnType<typeof setTimeout> | undefined
const labels: Record<string, string> = {
  queued: '排队中',
  running: '分析中',
  reviewing: '整理建议中',
  succeeded: '已完成',
  success: '成功',
  failed: '失败',
  partial: '部分完成',
  stopped: '已停止',
  cancelled: '已取消',
  pending: '待审核',
  applied: '已应用',
  rejected: '已拒绝',
  stale: '数据已变化',
  undone: '已撤销',
  preview: '程序核对（未调用 AI）',
  uncertain: '请求结果待确认',
  skipped: '已跳过',
}
const stageLabels: Record<string, string> = {
  queued: '等待 AI 工作进程',
  planning: '分配检查商品',
  snapshot: '核对数据库统计',
  checking: '核对数据库统计',
  analyzing: 'AI 分批分析',
  reviewing: '报告已保存，整理建议',
  validating: '校验建议',
  applying: '按规则处理建议',
  succeeded: '处理完成',
  failed: '任务失败',
  stopped: '已停止后续批次',
}
const currentRun = computed(() => overview.value?.current_run),
  cfg = computed(() => overview.value?.config ?? config.value),
  today = computed(() => overview.value?.today ?? {}),
  coverage = computed(() => overview.value?.coverage ?? {})
const workerReady = computed(() => overview.value?.worker?.ready === true)
const lastIssue = computed(() => overview.value?.last_issue ?? null)
const issueHints: Record<string, { message: string; action_hint: string }> = {
  AI_RATE_LIMITED: {
    message: 'AI 服务返回限流响应，本次分析未完成。',
    action_hint: '请核对服务商可用额度、模型权限或服务状态，再手动重试。系统不会自动反复请求。',
  },
  AI_AUTH_FAILED: {
    message: 'AI 接口鉴权未通过。',
    action_hint: '请在 AI 配置中检查已保存连接、Key、模型和访问权限。',
  },
  AI_HTTP_FAILED: {
    message: 'AI 接口返回错误，未生成有效分析报告。',
    action_hint: '请检查已保存连接与模型兼容格式；连接检查通过也不代表模型推理可用。',
  },
  AI_JSON_MODE_UNSUPPORTED: {
    message: 'AI 接口不接受当前 JSON 模式参数。',
    action_hint: '请在 AI 配置中关闭 JSON 模式，保存后再手动重试。',
  },
  AI_TOKEN_LIMIT_UNSUPPORTED: {
    message: 'AI 接口不接受当前输出长度参数。',
    action_hint: '请核对服务商支持的模型和 Chat Completions 兼容格式。',
  },
  AI_NETWORK_FAILED: {
    message: 'AI 网络请求未完成。',
    action_hint: '请检查接口地址与请求线路；需要境外出口时使用已配置的境外代理。',
  },
  AI_REPORT_INVALID: {
    message: 'AI 返回的结构或商品引用未通过校验。',
    action_hint: '商品未因该无效报告被自动整理。请核对模型与 JSON 模式兼容性后再重试。',
  },
}
const reportIssue = computed(() => {
  const item = report.value
  if (!item || item.preview || !['failed', 'partial'].includes(item.status)) return null
  return lastIssue.value?.id === item.id
    ? lastIssue.value
    : (issueHints[item.error_code] ?? {
        message: item.message || '分析未完成',
        action_hint: '请展开批次查看失败原因，核对配置后再手动重试。',
      })
})
const hasActiveRun = computed(() =>
  Boolean(currentRun.value && ['queued', 'running'].includes(currentRun.value.status)),
)
const quotaAvailable = computed(
  () =>
    Boolean(cfg.value) &&
    (today.value.products_used ?? 0) < (cfg.value.daily_product_limit ?? 1000) &&
    (today.value.requests_used ?? 0) < (cfg.value.daily_request_limit ?? 20),
)
const canRun = computed(
  () =>
    !loading.value &&
    !busy.value &&
    auth.user?.role === 'admin' &&
    cfg.value?.configured &&
    workerReady.value &&
    !hasActiveRun.value &&
    quotaAvailable.value,
)
const selectableGroups = computed(() =>
  groups.value.filter(
    (g) => !edit.category_id || String(g.category_id ?? '') === String(edit.category_id),
  ),
)
const reportProducts = computed(() => {
  const values: any[] = [...(report.value?.snapshot?.products ?? [])]
  for (const b of report.value?.batches ?? []) values.push(...(b.snapshot?.products ?? []))
  return new Map(values.map((p) => [p.id, p]))
})
const reportPlatforms = computed(
  () => new Map((report.value?.snapshot?.platforms ?? []).map((p: any) => [p.platform_id, p.name])),
)
const legacySuggestions = computed(() => {
  if ((report.value?.batches?.length ?? 0) > 0) return []
  return [
    ...(report.value?.report?.category_suggestions ?? []).map((item: any) => ({
      kind: 'category',
      name: item.group_name,
      category_path: item.category_path,
      product_ids: [item.product_id],
      reason: item.reason,
    })),
    ...(report.value?.report?.group_suggestions ?? []).map((item: any) => ({
      kind: 'group',
      name: item.name,
      category_path: item.category_path,
      product_ids: item.product_ids ?? [],
      reason: item.reason,
    })),
  ]
})
const reviewable = (item: any) => item.status === 'pending'
const eventLabels: Record<string, string> = {
  resumed: '恢复上架',
  closed: '停售',
  renamed: '名称变更',
  price_changed: '价格变更',
  basis_changed: '计价口径变更',
  collection_failed: '采集失败',
  collection_gap: '采集缺口',
  new_product: '新增商品',
  created: '新增商品',
  stock_changed: '库存变更',
}
const eventLabel = (key: unknown) => eventLabels[String(key)] ?? String(key)
const label = (value: string) => labels[value] ?? value ?? '—'
const badge = (value: string) =>
  ['succeeded', 'success', 'applied'].includes(value)
    ? 'success'
    : ['failed', 'stale', 'uncertain'].includes(value)
      ? 'danger'
      : ['running', 'queued', 'partial'].includes(value)
        ? 'warning'
        : 'neutral'
const stage = (item: any) => stageLabels[item?.stage] ?? label(item?.status)
function protocolLabel(value: unknown) {
  return (
    (
      {
        chat_completions: 'Chat Completions',
        responses: 'Responses',
        anthropic_messages: 'Anthropic Messages',
      } as Record<string, string>
    )[String(value)] ?? '旧版格式未记录'
  )
}
function reasoningLabel(value: unknown) {
  return value === 'auto' ? '自动' : typeof value === 'string' && value ? value : '旧版等级未记录'
}
function usage(value: any) {
  if (!value || !Object.values(value).some((v) => typeof v === 'number')) return 'Token 用量未知'
  return value.total_tokens != null
    ? `${value.total_tokens.toLocaleString()} Token`
    : `输入 ${value.prompt_tokens ?? value.input_tokens ?? '未知'} / 输出 ${value.completion_tokens ?? value.output_tokens ?? '未知'} Token`
}
function productName(id: number) {
  return reportProducts.value.get(id)?.name ?? `商品 #${id}`
}
function productPlatform(id: number) {
  const p = reportProducts.value.get(id)
  return p?.platform_name ?? reportPlatforms.value.get(p?.platform_id) ?? ''
}
function quantityBasis(item: any) {
  const value = item?.display_quantity == null ? '' : String(item.display_quantity).trim()
  if (!/^\+?(?:\d+(?:\.\d*)?|\.\d+)$/.test(value) || !/[1-9]/.test(value))
    return '源计价数量：待核验'
  return `源计价数量：${value}${item.unit ? ` ${item.unit}` : '（单位待核验）'}`
}
function elapsed(item: any) {
  if (!item?.started_at || !item?.completed_at) return '—'
  const s = Math.max(
    0,
    Math.round((Date.parse(item.completed_at) - Date.parse(item.started_at)) / 1000),
  )
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`
}
function clearVisibleData() {
  overview.value = null
  config.value = null
  runs.value = []
  suggestions.value = []
  applications.value = []
  report.value = null
  total.value = 0
  editing.value = null
  confirm.value = null
  categories.value = []
  groups.value = []
  detailRequest++
  editRequest++
}
async function load() {
  const n = ++request,
    t = tab.value
  error.value = ''
  try {
    const path =
      t === 'suggestions'
        ? '/ai-analysis/suggestions' +
          params({ status: suggestionStatus.value, page: page.value, page_size: 20 })
        : t === 'applications'
          ? '/ai-analysis/applications' +
            params({ status: applicationStatus.value, page: page.value, page_size: 20 })
          : '/ai-analysis/runs' +
            params({
              page: t === 'overview' ? 1 : page.value,
              page_size: t === 'overview' ? 5 : 20,
              ...(t === 'reports' ? filters : {}),
            })
    const [summary, result] = await Promise.all([api('/ai-analysis/overview'), api(path)])
    if (disposed || n !== request) return
    overview.value = summary
    config.value = summary.config
    if (t === 'suggestions') suggestions.value = itemsOf(result)
    else if (t === 'applications') applications.value = itemsOf(result)
    else runs.value = itemsOf(result)
    total.value = result.total ?? itemsOf(result).length
    if (page.value > 1 && !itemsOf(result).length && total.value <= (page.value - 1) * 20) {
      page.value = Math.max(1, Math.ceil(total.value / 20))
      await load()
      return
    }
    if (report.value && !report.value.preview) {
      const id = report.value.id,
        d = detailRequest,
        next = await api(`/ai-analysis/runs/${id}`)
      if (!disposed && n === request && d === detailRequest && report.value?.id === id)
        report.value = next
    }
  } catch (e: any) {
    if (!disposed && n === request) {
      error.value = e.message
      clearVisibleData()
    }
  } finally {
    if (!disposed && n === request) {
      loading.value = false
      clearTimeout(timer)
      timer = setTimeout(load, document.hidden ? 30000 : 10000)
    }
  }
}
async function loadPlatforms() {
  try {
    const value = await api('/platforms')
    if (!disposed) platforms.value = itemsOf(value)
  } catch {
    if (!disposed) platforms.value = []
  }
}
function selectTab(value: Tab) {
  if (tab.value === value) return
  tab.value = value
  page.value = 1
  total.value = 0
  loading.value = true
  load()
}
function change(value: number) {
  page.value = value
  loading.value = true
  load()
}
function filterChanged() {
  page.value = 1
  loading.value = true
  load()
}
async function mutate(path: string, body: any, success: string, method = 'POST') {
  if (busy.value || auth.user?.role !== 'admin') return
  busy.value = true
  error.value = ''
  notice.value = ''
  noticeKind.value = 'success'
  try {
    const result = await api(path, json(method, body))
    if (disposed) return
    notice.value =
      result?.status === 'stale'
        ? `建议未应用：${result.blocked_reason || result.message || '商品内容已变化，请重新分析或修改建议。'}`
        : success
    noticeKind.value = result?.status === 'stale' ? 'warning' : 'success'
    confirm.value = null
    await load()
    return result
  } catch (e: any) {
    if (!disposed) error.value = e.message
  } finally {
    if (!disposed) busy.value = false
  }
}
async function run() {
  if (!canRun.value) return
  return mutate('/ai-analysis/run', {}, '分析任务已提交。进度会自动刷新。')
}
function ask(action: Action, item?: any) {
  if (!busy.value) confirm.value = { action, item }
}
const confirmation = computed(() => {
  const a = confirm.value?.action
  return a === 'run'
    ? {
        title: '开始 AI 分批分析',
        text: '将把商品名称、类目、价格与统计摘要发送给已配置的 AI 服务，并使用今日剩余额度。开启自动应用后，符合规则的建议会在已有类目内整理尚未关联的商品。',
        button: '开始分析',
      }
    : a === 'stop'
      ? {
          title: '停止后续批次',
          text: '已发送的模型请求仍会完成；系统停止发送后续批次，已成功处理的建议保留。',
          button: '停止后续批次',
        }
      : a === 'retry'
        ? {
            title: '重试未完成分析',
            text: '重试会产生新的模型请求，计入今日共享额度。已完成的批次不会重复应用。',
            button: '确认重试',
          }
        : a === 'apply'
          ? {
              title: '应用整理建议',
              text: '程序会重新检查商品归属与目标类目。确认后增量关联商品；冲突时不会覆盖现有人工关联。原始价格和源站类目保留。',
              button: '确认应用',
            }
          : a === 'reject'
            ? {
                title: '拒绝这条建议',
                text: '相关商品内容未变化前，不会再次自动应用同一建议。',
                button: '确认拒绝',
              }
            : {
                title: '撤销本次处理',
                text: '只撤销本条记录产生的关联。若商品已被后续操作修改，系统会返回冲突，避免覆盖新的操作。',
                button: '确认撤销',
              }
})
async function confirmAction() {
  if (!confirm.value || busy.value) return
  const { action: a, item } = confirm.value
  if (a === 'run') return run()
  const path = ['stop', 'retry'].includes(a)
    ? `/ai-analysis/runs/${item.id}/${a}`
    : a === 'undo'
      ? `/ai-analysis/applications/${item.id}/undo`
      : `/ai-analysis/suggestions/${item.id}/${a}`
  await mutate(
    path,
    ['apply', 'reject'].includes(a) ? { revision: item.revision } : {},
    a === 'stop'
      ? '已请求停止后续批次，正在执行的请求仍会完成。'
      : a === 'retry'
        ? '重试任务已提交。'
        : a === 'apply'
          ? '建议已应用，变更已记录。'
          : a === 'reject'
            ? '建议已拒绝。'
            : '本次处理已撤销。',
  )
}
async function preview() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  const n = ++detailRequest
  try {
    const snapshot = await api('/ai-analysis/preview')
    if (!disposed && n === detailRequest) {
      report.value = {
        id: '本地预览',
        preview: true,
        status: 'preview',
        snapshot,
        report: {},
        message: '本机数据库核对结果，未调用外部 AI。',
      }
      selectTab('reports')
    }
  } catch (e: any) {
    if (!disposed && n === detailRequest) {
      error.value = e.message
      report.value = null
    }
  } finally {
    if (!disposed) busy.value = false
  }
}
async function view(item: any) {
  const n = ++detailRequest
  detailLoading.value = true
  error.value = ''
  try {
    const value = await api(`/ai-analysis/runs/${item.id}`)
    if (!disposed && n === detailRequest) {
      report.value = value
      if (tab.value !== 'reports') selectTab('reports')
    }
  } catch (e: any) {
    if (!disposed && n === detailRequest) {
      error.value = e.message
      report.value = null
    }
  } finally {
    if (!disposed && n === detailRequest) detailLoading.value = false
  }
}
function closeReport() {
  detailRequest++
  report.value = null
  detailLoading.value = false
}
async function editSuggestion(item: any) {
  if (busy.value || !reviewable(item)) return
  const n = ++editRequest
  editing.value = item
  editLoading.value = true
  editError.value = ''
  categories.value = []
  groups.value = []
  edit.category_id = String(item.category_id ?? '')
  edit.group_id = String(item.group_id ?? '')
  edit.group_name = item.group_name ?? ''
  try {
    const [cats, result] = await Promise.all([
      api('/comparison-categories'),
      api('/mappings?page=1&page_size=100'),
    ])
    const all = itemsOf(result),
      count = result.total ?? all.length
    for (let p = 2; all.length < count; p++) {
      const chunk = itemsOf(await api(`/mappings?page=${p}&page_size=100`))
      if (!chunk.length) throw new Error('统一商品列表有变化，请重新打开编辑。')
      all.push(...chunk)
    }
    if (!disposed && n === editRequest) {
      categories.value = itemsOf(cats)
      groups.value = all
    }
  } catch (e: any) {
    if (!disposed && n === editRequest) editError.value = e.message
  } finally {
    if (!disposed && n === editRequest) editLoading.value = false
  }
}
function chooseCategory() {
  edit.group_id = ''
}
function chooseGroup() {
  const g = groups.value.find((g) => String(g.id) === String(edit.group_id))
  if (g) {
    edit.category_id = String(g.category_id ?? '')
    edit.group_name = g.name
  }
}
function closeEdit() {
  if (!busy.value) {
    editRequest++
    editing.value = null
    editError.value = ''
  }
}
async function saveSuggestion() {
  if (
    !editing.value ||
    busy.value ||
    editLoading.value ||
    !categories.value.length ||
    auth.user?.role !== 'admin'
  )
    return
  editError.value = ''
  if (!edit.category_id || !edit.group_name.trim()) {
    editError.value = '请选择已有类目并填写统一商品名称。'
    return
  }
  busy.value = true
  try {
    await api(
      `/ai-analysis/suggestions/${editing.value.id}`,
      json('PATCH', {
        revision: editing.value.revision,
        category_id: Number(edit.category_id),
        group_id: edit.group_id ? Number(edit.group_id) : null,
        group_name: edit.group_name.trim(),
      }),
    )
    if (!disposed) {
      editing.value = null
      notice.value = '建议已更新，尚未应用。'
      noticeKind.value = 'success'
      await load()
    }
  } catch (e: any) {
    if (!disposed) editError.value = e.message
  } finally {
    if (!disposed) busy.value = false
  }
}
onMounted(() => {
  load()
  loadPlatforms()
})
onBeforeUnmount(() => {
  disposed = true
  request++
  detailRequest++
  editRequest++
  clearTimeout(timer)
})
</script>
<template>
  <div class="ai-center">
    <header class="ai-commandbar">
      <div>
        <span class="ai-eyebrow">数据检查与自动整理</span>
        <p>检查采集质量，整理分类与相似商品，所有变更可追溯。</p>
      </div>
      <div class="ai-actions">
        <RouterLink to="/admin/settings/network?section=ai" class="btn btn-white"
          ><Icon name="settings" :size="15" />AI 配置</RouterLink
        ><button type="button" class="btn btn-white" :disabled="busy" @click="preview">
          <Icon name="shield" :size="15" />程序核对</button
        ><button type="button" class="btn btn-primary" :disabled="!canRun" @click="ask('run')">
          <Icon name="play" :size="15" />立即分析
        </button>
      </div>
    </header>
    <div v-if="error" class="notice error" role="alert">
      {{ error
      }}<button type="button" class="text-link" :disabled="busy" @click="load">重新加载</button>
    </div>
    <div v-if="notice" class="notice" :class="noticeKind" role="status">{{ notice }}</div>
    <nav class="ai-tabs" aria-label="AI 数据验证栏目">
      <button
        v-for="item in tabs"
        :key="item.key"
        type="button"
        :aria-current="tab === item.key ? 'page' : undefined"
        :class="{ active: tab === item.key }"
        @click="selectTab(item.key)"
      >
        <Icon :name="item.icon" :size="17" />{{ item.label
        }}<span
          v-if="item.key === 'suggestions' && overview?.pending_suggestions"
          class="tab-count"
          >{{ overview.pending_suggestions }}</span
        >
      </button>
    </nav>
    <div v-if="loading && !overview" class="panel loading-state" role="status">
      <span class="spinner"></span>正在读取 AI 检查中心…
    </div>
    <template v-else-if="overview">
      <section v-if="tab === 'overview'" aria-label="运行概览">
        <div v-if="!cfg?.configured" class="notice info">
          <Icon name="key" :size="18" />
          <div>
            <strong>尚未配置 AI 接口</strong>
            <p>先填写 Base URL、模型与 API Key；“程序核对”可直接查看数据库事实，不调用模型。</p>
          </div>
          <RouterLink to="/admin/settings/network?section=ai" class="text-link">去配置</RouterLink>
        </div>
        <div v-else-if="!workerReady" class="notice warning" role="status">
          <Icon name="alert" :size="18" />
          <div>
            <strong>AI 工作进程未连接</strong>
            <p>等待独立 AI 工作进程恢复后可继续分析；小时采集由独立队列执行。</p>
          </div>
        </div>
        <div v-else-if="!quotaAvailable" class="notice info">
          今日商品或模型请求额度已用完，将在下一个北京时间自然日恢复。
        </div>
        <section v-if="lastIssue" class="panel ai-last-issue" aria-label="最近分析问题">
          <div class="ai-issue-title">
            <Icon name="alert" :size="19" />
            <div>
              <h2>最近一次分析未完成</h2>
              <p>{{ date(lastIssue.created_at) }} · 任务 #{{ lastIssue.id }}</p>
            </div>
          </div>
          <p class="ai-issue-message">{{ lastIssue.message }}</p>
          <p class="ai-muted">{{ lastIssue.action_hint }}</p>
          <div class="ai-inline-actions">
            <RouterLink to="/admin/settings/network?section=ai" class="btn btn-white btn-sm"
              ><Icon name="settings" :size="14" />检查 AI 配置</RouterLink
            ><button type="button" class="text-link" @click="view(lastIssue)">
              查看失败报告<Icon name="arrow" :size="14" />
            </button>
          </div>
        </section>
        <div class="ai-stats">
          <article class="panel ai-stat">
            <span>今日成功检查</span
            ><strong>{{ (today.checked_count ?? 0).toLocaleString() }}<small>件</small></strong>
            <p>
              已使用 {{ today.products_used ?? 0 }} / {{ cfg?.daily_product_limit ?? 1000 }} 件名额
            </p>
          </article>
          <article class="panel ai-stat">
            <span>模型请求</span
            ><strong
              >{{ today.requests_used ?? 0
              }}<small>/ {{ cfg?.daily_request_limit ?? 20 }}</small></strong
            >
            <p>失败请求也计入今日额度</p>
          </article>
          <article class="panel ai-stat">
            <span>异常与核验建议</span><strong>{{ today.findings ?? 0 }}<small>条</small></strong>
            <p>模型推断需要结合证据核对</p>
          </article>
          <article class="panel ai-stat">
            <span>今日自动处理</span
            ><strong>{{ today.automatic_applied ?? 0 }}<small>条</small></strong>
            <p>{{ cfg?.auto_apply ? '自动应用规则已开启' : '自动应用规则未开启' }}</p>
          </article>
        </div>
        <div class="ai-overview-columns">
          <section class="panel ai-panel">
            <div class="ai-panel-title">
              <h2>任务与运行计划</h2>
              <span class="badge" :class="hasActiveRun ? 'warning' : 'neutral'"
                ><i></i>{{ hasActiveRun ? label(currentRun.status) : '当前空闲' }}</span
              >
            </div>
            <template v-if="currentRun"
              ><div class="ai-run-heading">
                <strong>任务 #{{ currentRun.id }} · {{ stage(currentRun) }}</strong
                ><span>{{ currentRun.trigger === 'daily' ? '每日定时' : '手动分析' }}</span>
              </div>
              <p class="ai-muted">{{ currentRun.message }}</p>
              <progress
                :value="currentRun.completed_batches ?? 0"
                :max="Math.max(1, currentRun.batch_count ?? 0)"
                aria-label="分析批次完成进度"
              ></progress>
              <p class="ai-muted">
                {{ currentRun.completed_batches ?? 0 }} / {{ currentRun.batch_count ?? 0 }} 批次 ·
                成功检查 {{ currentRun.checked_count ?? 0 }} /
                {{ currentRun.planned_count ?? 0 }} 件
              </p>
              <p v-if="currentRun.stop_requested" class="ai-muted">
                已请求停止；正在执行的请求仍会完成。
              </p>
              <div class="ai-inline-actions">
                <button type="button" class="text-link" @click="view(currentRun)">查看任务</button
                ><button
                  v-if="hasActiveRun"
                  type="button"
                  class="text-link danger-text"
                  :disabled="busy || currentRun.stop_requested"
                  @click="ask('stop', currentRun)"
                >
                  停止后续批次
                </button>
              </div></template
            >
            <p v-else class="ai-empty-inline">
              暂无排队或执行中的任务。配置完成后，可手动发起或等待每日计划。
            </p>
            <p v-if="overview.schedule_reason" class="ai-small-note">
              {{ overview.schedule_reason }}
            </p>
            <dl class="ai-definition">
              <div>
                <dt>每日计划</dt>
                <dd>{{ cfg?.enabled ? `${cfg.daily_time}（北京时间）` : '未启用' }}</dd>
              </div>
              <div>
                <dt>下次执行</dt>
                <dd>{{ cfg?.enabled ? date(overview.next_run_at) : '—' }}</dd>
              </div>
              <div>
                <dt>独立 AI 进程</dt>
                <dd>
                  <span class="status-dot" :class="{ ready: workerReady }"></span
                  >{{ workerReady ? '已连接' : '未连接' }}
                </dd>
              </div>
              <div>
                <dt>使用模型</dt>
                <dd>{{ cfg?.model || '未配置' }}</dd>
                <dt>接口 / 思考等级</dt>
                <dd>
                  {{ protocolLabel(cfg?.api_format) }} · {{ reasoningLabel(cfg?.reasoning_effort) }}
                </dd>
              </div>
            </dl>
          </section>
          <section class="panel ai-panel">
            <div class="ai-panel-title">
              <h2>轮巡覆盖</h2>
              <span class="subtle-tag">成功检查才计入</span>
            </div>
            <div class="ai-coverage-number">
              <strong>{{ (coverage.checked ?? 0).toLocaleString() }}</strong
              ><span>/ {{ (coverage.total ?? 0).toLocaleString() }} 件</span>
            </div>
            <progress
              :value="coverage.checked ?? 0"
              :max="Math.max(1, coverage.total ?? 0)"
              aria-label="商品轮巡覆盖进度"
            ></progress>
            <p class="ai-muted">
              还有
              {{ (coverage.remaining ?? 0).toLocaleString() }}
              件未成功检查。此处展示历史成功覆盖，变动商品会优先重检。
            </p>
            <div class="ai-policy">
              <Icon name="refresh" :size="20" />
              <p>
                <strong>变动优先 + 跨平台轮巡</strong
                ><span>优先名额占 70%，轮巡占 30%；每批最多 50 件，不足时互相补足。</span>
              </p>
            </div>
            <p class="ai-small-note">
              仅在已有类目内整理未关联商品，保留人工关联。覆盖不代表源站价格或服务质量已得到验证。
            </p>
          </section>
        </div>
        <section class="panel">
          <div class="ai-panel-title padded">
            <h2>最近分析任务</h2>
            <button type="button" class="text-link" @click="selectTab('reports')">
              全部报告<Icon name="arrow" :size="14" />
            </button>
          </div>
          <div v-if="runs.length" class="ai-recent">
            <button v-for="item in runs" :key="item.id" type="button" @click="view(item)">
              <span
                ><strong
                  >{{ item.trigger === 'daily' ? '每日分析' : '手动分析' }} #{{ item.id }}</strong
                ><small>{{ date(item.created_at) }}</small></span
              ><span class="badge" :class="badge(item.status)">{{ label(item.status) }}</span
              ><Icon name="arrow" :size="15" />
            </button>
          </div>
          <EmptyState
            v-else
            icon="file"
            title="尚无分析任务"
            description="程序核对可先检查本地事实；配置接口后再进行 AI 分析。"
          />
        </section>
      </section>
      <section v-if="tab === 'suggestions'" class="panel" aria-label="建议审核">
        <div class="ai-panel-title padded">
          <div>
            <h2>建议审核</h2>
            <p class="ai-muted">规则不能确定的建议进入此处，修改或确认后再应用。</p>
          </div>
          <label class="ai-filter"
            >处理状态<select v-model="suggestionStatus" :disabled="busy" @change="filterChanged">
              <option value="pending">待审核</option>
              <option value="stale">数据已变化</option>
              <option value="applied">已应用</option>
              <option value="rejected">已拒绝</option>
              <option value="">全部状态</option>
            </select></label
          >
        </div>
        <div v-if="loading" class="loading-state">正在读取建议…</div>
        <div v-else-if="suggestions.length">
          <article v-for="item in suggestions" :key="item.id" class="ai-suggestion">
            <header>
              <div>
                <span class="ai-eyebrow"
                  >{{ item.kind === 'category' ? '分类分配建议' : '相似商品归总建议' }} #{{
                    item.id
                  }}</span
                >
                <h3>{{ item.group_name || '统一商品待确认' }}</h3>
                <p class="ai-muted">目标类目：{{ item.category_path || '类目待确认' }}</p>
              </div>
              <span class="badge" :class="badge(item.status)">{{ label(item.status) }}</span>
            </header>
            <div v-if="item.blocked_reason" class="ai-blocked">
              <Icon name="alert" :size="15" /><span>未自动执行：{{ item.blocked_reason }}</span>
            </div>
            <p class="ai-reason">{{ item.reason }}</p>
            <div class="ai-products">
              <article v-for="product in item.products" :key="product.id">
                <div>
                  <RouterLink :to="`/admin/products/${product.id}`">{{
                    product.name || `商品 #${product.id}`
                  }}</RouterLink
                  ><small>{{ product.platform_name }} · #{{ product.id }}</small
                  ><small>源类目：{{ product.source_category || '未分类' }}</small>
                </div>
                <div>
                  <strong>{{ price(product.price, product.currency) }}</strong
                  ><small>{{ product.unit || '单位待核验' }}</small
                  ><small>{{ quantityBasis(product) }}</small
                  ><small>{{
                    product.current_group_name
                      ? `已归属：${product.current_group_name}`
                      : '尚未关联统一商品'
                  }}</small>
                </div>
              </article>
            </div>
            <footer>
              <span>{{ date(item.created_at) }}</span>
              <div v-if="reviewable(item)" class="ai-inline-actions">
                <button
                  type="button"
                  class="btn btn-white btn-sm"
                  :disabled="busy"
                  @click="editSuggestion(item)"
                >
                  <Icon name="edit" :size="14" />修改建议</button
                ><button
                  type="button"
                  class="btn btn-white btn-sm"
                  :disabled="busy"
                  @click="ask('reject', item)"
                >
                  拒绝</button
                ><button
                  type="button"
                  class="btn btn-primary btn-sm"
                  :disabled="busy || item.status === 'stale'"
                  @click="ask('apply', item)"
                >
                  采纳并应用
                </button>
              </div>
              <button
                v-else-if="item.status === 'stale'"
                type="button"
                class="btn btn-white btn-sm"
                :disabled="busy"
                @click="ask('reject', item)"
              >
                拒绝过期建议
              </button>
            </footer>
            <p v-if="item.status === 'stale'" class="ai-small-note">
              商品内容已变化，需要重新分析；旧建议不可直接修改后应用。
            </p>
          </article>
        </div>
        <EmptyState
          v-else
          icon="shield"
          title="当前筛选下没有建议"
          description="AI 分析完成后，未满足自动规则的分类或关联建议会显示在这里。"
        /><Pagination v-if="total" :page="page" :total="total" :page-size="20" @change="change" />
      </section>
      <section v-if="tab === 'reports'" aria-label="分析报告">
        <div v-if="detailLoading" class="panel loading-state" role="status">
          <span class="spinner"></span>正在读取报告…
        </div>
        <section v-if="report" class="panel ai-report">
          <header class="ai-panel-title">
            <div>
              <span class="ai-eyebrow">{{
                report.preview ? '本地数据库事实' : `分析报告 #${report.id}`
              }}</span>
              <h2>{{ label(report.status) }}</h2>
            </div>
            <button type="button" class="text-link" @click="closeReport">
              关闭报告<Icon name="close" :size="14" />
            </button>
          </header>
          <div v-if="reportIssue" class="notice warning ai-report-issue" role="status">
            <Icon name="alert" :size="18" />
            <div>
              <strong>{{ reportIssue.message }}</strong>
              <p>{{ reportIssue.action_hint }}</p>
              <RouterLink to="/admin/settings/network?section=ai" class="text-link"
                >检查 AI 配置<Icon name="arrow" :size="14"
              /></RouterLink>
            </div>
          </div>
          <p v-else class="ai-muted">{{ report.message }}</p>
          <p class="ai-period">
            统计期：{{ date(report.snapshot?.period_start) }} ～
            {{ date(report.snapshot?.period_end)
            }}<span v-if="report.snapshot?.generated_at">
              · 当前状态快照：{{ date(report.snapshot.generated_at) }}</span
            >
          </p>
          <div class="ai-report-metrics">
            <span v-if="!report.preview"
              >{{ protocolLabel(report.api_format) }} · 思考
              {{ reasoningLabel(report.reasoning_effort)
              }}<template v-if="report.max_output_tokens">
                · 输出上限 {{ report.max_output_tokens }} Token</template
              ></span
            >
            <span
              >统计商品 <strong>{{ report.snapshot?.total_products ?? '—' }}</strong></span
            ><span
              >{{ report.preview ? '预览样本' : 'AI 成功检查' }}
              <strong>{{
                report.preview
                  ? (report.snapshot?.sample_count ?? 0)
                  : (report.checked_count ?? report.sampled_products ?? 0)
              }}</strong></span
            ><span
              >耗时 <strong>{{ elapsed(report) }}</strong></span
            ><span
              >{{ report.preview ? '未调用模型' : usage(report.usage)
              }}{{ !report.preview && report.usage_partial ? '（部分批次用量未知）' : '' }}</span
            >
          </div>
          <p class="ai-small-note">
            {{
              report.snapshot?.coverage || '统计为全量数据库事实，模型结论只覆盖成功送检的商品。'
            }}
          </p>
          <p class="ai-small-note">历史统计与当前商品状态分别标注时间。</p>
          <div v-if="report.snapshot?.platforms?.length" class="ai-evidence">
            <h3>程序核对事实</h3>
            <details
              v-for="platform in report.snapshot.platforms"
              :key="platform.platform_id"
              class="ai-platform-evidence"
            >
              <summary>
                <strong>{{ platform.name }}</strong
                ><span
                  >批次 {{ platform.batch_count }} / 周期参考
                  {{ platform.expected_period_batches ?? '—' }} · 非成功
                  {{ platform.unsuccessful_batches }}</span
                >
              </summary>
              <p class="ai-muted">
                会话：{{ platform.auth_status === 'connected' ? '已连接' : platform.auth_status }} ·
                最近成功：{{ date(platform.last_success_at)
                }}{{ platform.batches_truncated ? ' · 批次明细有截断' : '' }}
              </p>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>批次 / 时间</th>
                      <th>状态</th>
                      <th>期望数量</th>
                      <th>已见 / 实际入库</th>
                      <th>数量核对</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="batch in platform.batches ?? []" :key="batch.id">
                      <td>
                        #{{ batch.id
                        }}<small class="table-secondary">{{ date(batch.scheduled_at) }}</small>
                      </td>
                      <td>{{ label(batch.status) }}</td>
                      <td>{{ batch.expected ?? '未知' }}</td>
                      <td>{{ batch.seen }} / {{ batch.stored_observations }}</td>
                      <td>
                        <span class="badge" :class="batch.count_matches ? 'success' : 'warning'">{{
                          batch.count_matches ? '一致' : '待检查'
                        }}</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </details>
            <div class="ai-quality-counts">
              <span
                >无效价格或数量：{{ report.snapshot.invalid_price_or_quantity_count ?? 0 }} 件</span
              ><span v-for="(value, key) in report.snapshot.event_counts ?? {}" :key="key"
                >{{ eventLabel(key) }}：{{ value }}</span
              >
            </div>
          </div>
          <template v-if="report.report?.summary"
            ><h3>AI 分析归总</h3>
            <p class="ai-summary">{{ report.report.summary }}</p></template
          >
          <article
            v-for="(finding, index) in report.report?.findings ?? []"
            :key="index"
            class="ai-finding"
          >
            <header>
              <strong>{{ finding.title }}</strong
              ><span
                class="badge"
                :class="
                  finding.severity === 'error'
                    ? 'danger'
                    : finding.severity === 'warning'
                      ? 'warning'
                      : 'info'
                "
                >{{
                  finding.severity === 'error'
                    ? '需检查'
                    : finding.severity === 'warning'
                      ? '待核验'
                      : '提示'
                }}</span
              >
            </header>
            <p>{{ finding.evidence }}</p>
            <div class="ai-product-links">
              <RouterLink v-for="id in finding.product_ids" :key="id" :to="`/admin/products/${id}`"
                >{{ productName(id)
                }}<small>{{ productPlatform(id) }} · #{{ id }}</small></RouterLink
              >
            </div>
          </article>
          <section v-if="legacySuggestions.length" class="ai-legacy-suggestions">
            <h3>历史分类与归总建议 · 只读</h3>
            <p class="ai-small-note">
              以下保留旧版报告的原始建议，不会自动创建类目或商品关联。需要处理时请重新分析。
            </p>
            <article v-for="(item, index) in legacySuggestions" :key="index" class="ai-finding">
              <strong
                >{{ item.kind === 'category' ? '分类分配' : '相似商品归总' }}：{{
                  item.name || '统一商品待确认'
                }}</strong
              >
              <p>建议类目：{{ item.category_path || '类目待确认' }}</p>
              <p>{{ item.reason }}</p>
              <div class="ai-product-links">
                <RouterLink v-for="id in item.product_ids" :key="id" :to="`/admin/products/${id}`">
                  {{ productName(id) }}<small>{{ productPlatform(id) }} · #{{ id }}</small>
                  <small>{{ quantityBasis(reportProducts.get(id)) }}</small>
                </RouterLink>
              </div>
            </article>
          </section>
          <details v-for="batch in report.batches ?? []" :key="batch.id" class="ai-batch">
            <summary>
              <strong>AI 批次 {{ batch.ordinal }}</strong
              ><span class="badge" :class="badge(batch.status)">{{ label(batch.status) }}</span
              ><span
                >{{ batch.checked_count ?? 0 }} / {{ batch.product_count }} 件 ·
                {{ usage(batch.usage) }}</span
              >
            </summary>
            <p class="ai-muted">{{ batch.message }}</p>
            <p v-if="batch.report?.summary" class="ai-summary">{{ batch.report.summary }}</p>
            <article
              v-for="(finding, index) in batch.report?.findings ?? []"
              :key="index"
              class="ai-finding"
            >
              <strong>{{ finding.title }}</strong>
              <p>{{ finding.evidence }}</p>
              <div class="ai-product-links">
                <RouterLink
                  v-for="id in finding.product_ids"
                  :key="id"
                  :to="`/admin/products/${id}`"
                  >{{ productName(id)
                  }}<small>{{ productPlatform(id) }} · #{{ id }}</small></RouterLink
                >
              </div>
            </article>
            <details v-if="batch.snapshot?.products?.length">
              <summary>本批商品与观测依据</summary>
              <div class="ai-products">
                <article v-for="product in batch.snapshot.products" :key="product.id">
                  <div>
                    <RouterLink :to="`/admin/products/${product.id}`">{{ product.name }}</RouterLink
                    ><small
                      >{{ product.platform_name || reportPlatforms.get(product.platform_id) }} · #{{
                        product.id
                      }}
                      · {{ product.source_category || '未分类' }}</small
                    ><small>当前观测：{{ date(product.last_observed_at) }}</small
                    ><small
                      v-for="observation in product.recent_period_observations ?? []"
                      :key="observation.at"
                      >期内：{{ date(observation.at) }} ·
                      {{ price(observation.price, observation.currency) }} /
                      {{ observation.unit || '单位待核验' }} ·
                      {{ quantityBasis(observation) }}</small
                    >
                  </div>
                  <div>
                    <strong>{{ price(product.price, product.currency) }}</strong
                    ><small>{{ product.unit || '单位待核验' }}</small>
                    <small>{{ quantityBasis(product) }}</small>
                  </div>
                </article>
              </div>
            </details>
          </details>
          <footer v-if="!report.preview" class="ai-report-footer">
            <button
              v-if="['failed', 'partial', 'stopped'].includes(report.status)"
              type="button"
              class="btn btn-white"
              :disabled="busy || !canRun"
              @click="ask('retry', report)"
            >
              重试未完成分析</button
            ><button
              v-if="['queued', 'running'].includes(report.status)"
              type="button"
              class="btn btn-white"
              :disabled="busy || report.stop_requested"
              @click="ask('stop', report)"
            >
              停止后续批次</button
            ><button
              v-if="report.batches?.length"
              type="button"
              class="text-link"
              @click="selectTab('suggestions')"
            >
              查看结构化建议与处理状态<Icon name="arrow" :size="14" />
            </button>
          </footer>
        </section>
        <section class="panel">
          <div class="ai-panel-title padded">
            <h2>分析记录</h2>
            <button type="button" class="text-link" :disabled="loading" @click="load">
              <Icon name="refresh" :size="14" />刷新
            </button>
          </div>
          <form class="ai-filters" @submit.prevent="filterChanged">
            <label class="ai-filter"
              >状态<select v-model="filters.status">
                <option value="">全部状态</option>
                <option value="queued">排队中</option>
                <option value="running">分析中</option>
                <option value="succeeded">已完成</option>
                <option value="partial">部分完成</option>
                <option value="failed">失败</option>
                <option value="stopped">已停止</option>
              </select></label
            ><label class="ai-filter"
              >平台<select v-model="filters.platform_id">
                <option value="">全部平台</option>
                <option v-for="platform in platforms" :key="platform.id" :value="platform.id">
                  {{ platform.name }}
                </option>
              </select></label
            ><label class="ai-filter"
              >开始日期（北京时间）<input v-model="filters.date_from" type="date" /></label
            ><label class="ai-filter"
              >结束日期（北京时间）<input
                v-model="filters.date_to"
                type="date"
                :min="filters.date_from || undefined" /></label
            ><button type="submit" class="btn btn-white" :disabled="loading">筛选</button>
          </form>
          <div v-if="loading" class="loading-state">正在读取分析记录…</div>
          <div v-else-if="runs.length" class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>任务 / 时间</th>
                  <th>方式 / 模型</th>
                  <th>状态 / 阶段</th>
                  <th>成功检查 / 计划</th>
                  <th>摘要</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in runs" :key="item.id">
                  <td>
                    #{{ item.id }}
                    <div class="table-secondary">{{ date(item.created_at) }}</div>
                  </td>
                  <td>
                    {{ item.trigger === 'daily' ? '每日定时' : '手动' }}
                    <div class="table-secondary">{{ item.model }}</div>
                    <div class="table-secondary">
                      {{ protocolLabel(item.api_format) }} ·
                      {{ reasoningLabel(item.reasoning_effort) }}
                    </div>
                  </td>
                  <td>
                    <span class="badge" :class="badge(item.status)">{{ label(item.status) }}</span>
                    <div class="table-secondary">{{ stage(item) }}</div>
                  </td>
                  <td>
                    {{ item.checked_count ?? item.sampled_products ?? 0 }} /
                    {{ item.planned_count ?? '—' }}
                  </td>
                  <td class="ai-excerpt">{{ item.summary || item.message }}</td>
                  <td>
                    <button type="button" class="text-link" @click="view(item)">查看报告</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <EmptyState
            v-else
            icon="file"
            title="当前筛选下没有报告"
            description="每日报告分析前一个北京时间自然日；手动分析最近 24 小时。"
          /><Pagination v-if="total" :page="page" :total="total" :page-size="20" @change="change" />
        </section>
      </section>
      <section v-if="tab === 'applications'" class="panel" aria-label="处理记录">
        <div class="ai-panel-title padded">
          <div>
            <h2>处理记录</h2>
            <p class="ai-muted">自动处理和管理员操作均保留记录，撤销前会检查后续变更。</p>
          </div>
          <label class="ai-filter"
            >结果<select v-model="applicationStatus" @change="filterChanged">
              <option value="">全部</option>
              <option value="applied">已应用</option>
              <option value="undone">已撤销</option>
            </select></label
          >
        </div>
        <div v-if="loading" class="loading-state">正在读取处理记录…</div>
        <div v-else-if="applications.length">
          <article v-for="item in applications" :key="item.id" class="ai-application">
            <header>
              <div>
                <span class="ai-eyebrow"
                  >{{ item.mode === 'auto' ? '规则自动处理' : '管理员确认' }} · #{{ item.id }}</span
                >
                <h3>{{ item.group_name }}</h3>
                <p class="ai-muted">
                  {{ item.category_path }} · {{ date(item.created_at) }} ·
                  {{ item.actor_name || (item.mode === 'auto' ? '系统规则' : '管理员') }}
                </p>
              </div>
              <span class="badge" :class="badge(item.status)">{{ label(item.status) }}</span>
            </header>
            <div class="ai-products">
              <article v-for="product in item.products ?? []" :key="product.id">
                <div>
                  <RouterLink :to="`/admin/products/${product.id}`">{{
                    product.name || `商品 #${product.id}`
                  }}</RouterLink
                  ><small>{{ product.platform_name }} · #{{ product.id }}</small>
                  <small v-if="product.display_quantity != null">{{
                    quantityBasis(product)
                  }}</small>
                </div>
                <div class="ai-change-arrow">
                  <span>{{ product.before_group_name || '未关联' }}</span
                  ><Icon name="arrow" :size="15" /><strong>{{
                    product.after_group_name || item.group_name
                  }}</strong>
                </div>
              </article>
            </div>
            <details class="ai-change-evidence">
              <summary>变更前后记录</summary>
              <div>
                <section>
                  <h4>变更前</h4>
                  <pre>{{ JSON.stringify(item.before, null, 2) }}</pre>
                </section>
                <section>
                  <h4>变更后</h4>
                  <pre>{{ JSON.stringify(item.after, null, 2) }}</pre>
                </section>
              </div>
            </details>
            <footer>
              <span v-if="item.undone_at">撤销时间：{{ date(item.undone_at) }}</span
              ><button
                v-if="item.status === 'applied'"
                type="button"
                class="btn btn-white btn-sm"
                :disabled="busy"
                @click="ask('undo', item)"
              >
                <Icon name="back" :size="14" />撤销本次处理
              </button>
            </footer>
          </article>
        </div>
        <EmptyState
          v-else
          icon="clock"
          title="当前筛选下没有处理记录"
          description="应用建议后，将展示具体商品、关联结果与处理方式。"
        /><Pagination v-if="total" :page="page" :total="total" :page-size="20" @change="change" />
      </section>
    </template>
    <Modal v-if="confirm" :title="confirmation.title" @close="!busy && (confirm = null)"
      ><p class="ai-dialog-copy">{{ confirmation.text }}</p>
      <div v-if="confirm.item?.group_name" class="ai-confirm-target">
        <strong>{{ confirm.item.group_name }}</strong
        ><small>{{ confirm.item.category_path }}</small>
      </div>
      <div v-if="error" class="notice error" role="alert">{{ error }}</div>
      <div class="modal-actions">
        <button type="button" class="btn btn-white" :disabled="busy" @click="confirm = null">
          取消</button
        ><button
          type="button"
          class="btn"
          :class="
            ['reject', 'undo', 'stop'].includes(confirm.action) ? 'btn-danger' : 'btn-primary'
          "
          :disabled="busy || (confirm.action === 'run' && !canRun)"
          @click="confirmAction"
        >
          {{ busy ? '处理中…' : confirmation.button }}
        </button>
      </div></Modal
    >
    <Modal v-if="editing" title="修改整理建议" @close="closeEdit"
      ><div v-if="editLoading" class="loading-state">正在读取已有类目与统一商品…</div>
      <form v-else @submit.prevent="saveSuggestion">
        <p class="ai-dialog-copy">
          修改目标后先保存建议，再单独采纳应用。原始商品与价格保持为采集事实。
        </p>
        <label
          >比价类目<select
            v-model="edit.category_id"
            required
            :disabled="busy"
            @change="chooseCategory"
          >
            <option value="" disabled>选择已有类目</option>
            <option v-for="category in categories" :key="category.id" :value="category.id">
              {{ category.path_name || category.name }}
            </option>
          </select></label
        ><label
          >目标统一商品<select v-model="edit.group_id" :disabled="busy" @change="chooseGroup">
            <option value="">在此类目下创建统一商品</option>
            <option v-for="group in selectableGroups" :key="group.id" :value="group.id">
              {{ group.name }}{{ group.match_mode === 'exact' ? '（同规格）' : '（同类／相似）' }}
            </option>
          </select></label
        ><label
          >统一商品名称<input
            v-model="edit.group_name"
            required
            maxlength="200"
            :disabled="busy || Boolean(edit.group_id)"
        /></label>
        <div v-if="editError" class="notice error" role="alert">{{ editError }}</div>
        <div class="modal-actions">
          <button type="button" class="btn btn-white" :disabled="busy" @click="closeEdit">
            取消</button
          ><button type="submit" class="btn btn-primary" :disabled="busy || !categories.length">
            {{ busy ? '保存中…' : '保存建议' }}
          </button>
        </div>
      </form></Modal
    >
  </div>
</template>
<style scoped>
.ai-last-issue {
  padding: 20px 23px;
  border-color: #f0dfbf;
  background: #fffcf6;
}
.ai-issue-title {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  color: #b18137;
  margin-bottom: 14px;
}
.ai-issue-title > svg {
  flex-shrink: 0;
  margin-top: 2px;
}
.ai-issue-title h2 {
  font-size: 14px;
  color: #785d32;
}
.ai-issue-title p {
  font-size: 11px;
  color: #9a8a70;
  margin-top: 5px;
}
.ai-issue-message {
  font-size: 13px;
  color: #725b3a;
  margin-bottom: 7px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
.ai-last-issue .ai-inline-actions {
  margin-top: 15px;
}
.ai-report-issue {
  align-items: flex-start;
  margin-top: 16px;
}
.ai-report-issue > div {
  min-width: 0;
}
.ai-report-issue p {
  margin: 6px 0 10px;
}
.ai-report-issue .text-link {
  margin-left: 0;
}
.ai-center {
  min-width: 0;
}
.ai-center .notice > div {
  flex: 1;
  min-width: 0;
}
.ai-center .notice > .text-link {
  flex-shrink: 0;
  white-space: nowrap;
  align-self: center;
}
.ai-commandbar,
.ai-actions,
.ai-panel-title,
.ai-inline-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.ai-commandbar {
  align-items: flex-end;
  margin-bottom: 24px;
}
.ai-commandbar p {
  color: #73839c;
  margin-top: 7px;
  font-size: 12px;
}
.ai-eyebrow {
  color: #8b9bb1;
  font-size: 10px;
  letter-spacing: 0.7px;
}
.ai-actions,
.ai-inline-actions {
  justify-content: flex-start;
}
.ai-tabs {
  display: flex;
  gap: 6px;
  border-bottom: 1px solid #e1e7f1;
  margin-bottom: 24px;
  overflow-x: auto;
}
.ai-tabs button {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
  padding: 14px 18px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: none;
  color: #7b8aa0;
  font-size: 13px;
}
.ai-tabs button.active {
  color: #3d6bea;
  border-bottom-color: #3d6bea;
  font-weight: 600;
}
.tab-count {
  padding: 1px 6px;
  border-radius: 10px;
  background: #eaf0ff;
  font-size: 10px;
}
.ai-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 18px;
  margin-bottom: 22px;
}
.ai-stat {
  margin: 0;
  padding: 20px;
}
.ai-stat > span {
  color: #7b8ba3;
  font-size: 12px;
}
.ai-stat > strong {
  display: block;
  font-size: 29px;
  color: #243d64;
  margin: 9px 0;
  letter-spacing: -0.8px;
}
.ai-stat strong small {
  font-size: 12px;
  font-weight: 400;
  color: #8d9bb1;
  margin-left: 7px;
  letter-spacing: 0;
}
.ai-stat p {
  font-size: 11px;
  color: #94a0b3;
}
.ai-overview-columns {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 22px;
}
.ai-panel {
  padding: 23px;
  min-width: 0;
}
.ai-panel-title {
  margin-bottom: 18px;
}
.ai-panel-title.padded {
  padding: 22px;
  margin: 0;
  border-bottom: 1px solid #edf1f7;
}
.ai-panel-title h2 {
  font-size: 15px;
}
.ai-panel-title .ai-muted {
  margin-top: 6px;
}
.ai-muted {
  color: #7b8aa0;
  font-size: 12px;
  line-height: 1.85;
}
.ai-run-heading {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: space-between;
  font-size: 12px;
  margin: 24px 0 8px;
}
.ai-run-heading > span {
  color: #94a1b3;
}
progress {
  display: block;
  appearance: none;
  border: none;
  border-radius: 8px;
  width: 100%;
  height: 7px;
  margin: 17px 0 10px;
  background: #edf1f8;
  overflow: hidden;
}
progress::-webkit-progress-bar {
  background: #edf1f8;
  border-radius: 8px;
}
progress::-webkit-progress-value {
  background: #4e7bec;
  border-radius: 8px;
}
progress::-moz-progress-bar {
  background: #4e7bec;
  border-radius: 8px;
}
.ai-inline-actions {
  margin-top: 14px;
}
.ai-definition {
  margin: 23px 0 0;
  border-top: 1px solid #edf1f7;
  padding-top: 14px;
}
.ai-definition > div {
  display: flex;
  align-items: baseline;
  gap: 14px;
  justify-content: space-between;
  padding: 7px 0;
  font-size: 12px;
}
.ai-definition dt {
  color: #91a0b5;
}
.ai-definition dd {
  margin: 0;
  color: #556a86;
  text-align: right;
  overflow-wrap: anywhere;
}
.status-dot {
  display: inline-block;
  height: 6px;
  width: 6px;
  border-radius: 50%;
  background: #b2bdcd;
  margin-right: 7px;
}
.status-dot.ready {
  background: #4ab18f;
}
.ai-empty-inline {
  font-size: 12px;
  color: #91a0b3;
  line-height: 1.9;
  padding: 12px 0;
}
.ai-coverage-number {
  display: flex;
  align-items: baseline;
  gap: 9px;
  margin-top: 24px;
}
.ai-coverage-number strong {
  font-size: 30px;
  color: #2c456b;
}
.ai-coverage-number span {
  font-size: 12px;
  color: #91a0b5;
}
.ai-policy {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  background: #f6f9ff;
  padding: 16px;
  border-radius: 9px;
  margin-top: 22px;
  color: #7290cb;
}
.ai-policy svg {
  flex-shrink: 0;
  margin-top: 2px;
}
.ai-policy strong {
  display: block;
  color: #5b739b;
  font-size: 12px;
}
.ai-policy span {
  display: block;
  margin-top: 5px;
  font-size: 11px;
  color: #8d9db5;
  line-height: 1.8;
}
.ai-small-note {
  color: #8a9bb2;
  font-size: 11px;
  line-height: 1.85;
  margin-top: 15px;
}
.ai-recent {
  padding: 0 22px;
}
.ai-recent button {
  border: 0;
  border-bottom: 1px solid #edf1f7;
  width: 100%;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 18px 0;
  background: white;
  text-align: left;
}
.ai-recent button:last-child {
  border: 0;
}
.ai-recent button > span:first-child {
  flex: 1;
}
.ai-recent strong {
  font-size: 12px;
}
.ai-recent small {
  display: block;
  color: #94a3b7;
  margin-top: 5px;
  font-size: 11px;
}
.ai-filter {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: #8091ab;
  font-size: 11px;
}
.ai-filter select,
.ai-filter input {
  font-size: 12px;
  height: 38px;
  min-width: 125px;
  max-width: 220px;
}
.ai-filters {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 14px;
  padding: 18px 22px;
  background: #fafcff;
  border-bottom: 1px solid #edf1f7;
}
.ai-suggestion,
.ai-application {
  padding: 24px;
  border-bottom: 1px solid #edf1f7;
}
.ai-suggestion:last-child,
.ai-application:last-child {
  border: 0;
}
.ai-suggestion > header,
.ai-application > header {
  display: flex;
  gap: 14px;
  justify-content: space-between;
  align-items: flex-start;
}
.ai-suggestion h3,
.ai-application h3 {
  font-size: 16px;
  margin: 7px 0;
}
.ai-suggestion > footer,
.ai-application > footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 20px;
}
.ai-suggestion > footer > span,
.ai-application > footer > span {
  color: #94a1b5;
  font-size: 11px;
}
.ai-suggestion footer .ai-inline-actions {
  margin: 0;
}
.ai-blocked {
  display: flex;
  gap: 8px;
  color: #a17e4a;
  background: #fff8ed;
  border: 1px solid #f7ead4;
  border-radius: 7px;
  padding: 10px 12px;
  margin-top: 18px;
  font-size: 12px;
}
.ai-blocked svg {
  flex-shrink: 0;
  margin-top: 2px;
}
.ai-reason {
  font-size: 13px;
  line-height: 1.9;
  color: #647995;
  margin: 16px 0;
  white-space: pre-wrap;
}
.ai-products {
  border: 1px solid #e9eef6;
  border-radius: 8px;
  overflow: hidden;
  margin-top: 16px;
}
.ai-products > article {
  display: flex;
  gap: 18px;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid #edf1f7;
}
.ai-products > article:last-child {
  border: 0;
}
.ai-products > article > div {
  min-width: 0;
}
.ai-products > article > div:first-child {
  flex: 1;
}
.ai-products a {
  color: #476baf;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.ai-products strong {
  font-size: 12px;
  color: #4e6483;
}
.ai-products small {
  display: block;
  color: #8e9cb1;
  font-size: 11px;
  line-height: 1.8;
  margin-top: 3px;
}
.ai-products > article > div:last-child {
  text-align: right;
}
.ai-report {
  padding: 24px;
}
.ai-report h2 {
  margin-top: 7px;
  font-size: 19px;
}
.ai-report h3 {
  font-size: 14px;
  margin: 24px 0 12px;
}
.ai-period {
  font-size: 11px;
  color: #8a9bb1;
  margin-top: 13px;
  line-height: 1.8;
}
.ai-report-metrics {
  display: flex;
  align-items: center;
  gap: 14px 30px;
  flex-wrap: wrap;
  padding: 16px;
  background: #f7f9fd;
  border-radius: 8px;
  margin-top: 20px;
  color: #8393ab;
  font-size: 12px;
}
.ai-report-metrics strong {
  color: #516986;
  margin-left: 5px;
}
.ai-summary {
  font-size: 13px;
  line-height: 1.95;
  white-space: pre-wrap;
  color: #536c8d;
}
.ai-platform-evidence,
.ai-batch {
  border: 1px solid #e6edf7;
  border-radius: 8px;
  padding: 15px;
  margin-top: 12px;
  min-width: 0;
}
.ai-platform-evidence summary,
.ai-batch > summary {
  cursor: pointer;
  font-size: 12px;
  color: #637d9f;
  line-height: 1.9;
}
.ai-platform-evidence summary span,
.ai-batch summary > span {
  margin-left: 14px;
  font-size: 11px;
  color: #879bb6;
}
.ai-platform-evidence > p,
.ai-batch > p {
  margin-top: 12px;
}
.ai-platform-evidence .table-wrap {
  margin-top: 12px;
}
.ai-quality-counts {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 18px 0;
  font-size: 11px;
  color: #8699b4;
}
.ai-finding {
  padding: 16px 0;
  border-bottom: 1px solid #edf1f7;
}
.ai-finding header {
  display: flex;
  gap: 12px;
  align-items: center;
}
.ai-finding strong {
  font-size: 13px;
}
.ai-finding p {
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.9;
  color: #7489a4;
  margin-top: 8px;
}
.ai-product-links {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
.ai-product-links a {
  font-size: 11px;
  background: #f1f6ff;
  border: 1px solid #e5edfb;
  color: #6182ba;
  padding: 6px 9px;
  border-radius: 5px;
  max-width: 100%;
  overflow-wrap: anywhere;
}
.ai-product-links small {
  display: block;
  color: #96a6bf;
  margin-top: 3px;
}
.ai-report-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 16px;
  border-top: 1px solid #edf1f7;
  margin-top: 22px;
  padding-top: 18px;
}
.ai-excerpt {
  max-width: 280px;
  white-space: normal;
  font-size: 12px;
  line-height: 1.8;
}
.ai-change-arrow {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 11px;
  color: #91a0b5;
}
.ai-change-evidence {
  margin-top: 18px;
  font-size: 11px;
  color: #8396b1;
}
.ai-change-evidence summary {
  cursor: pointer;
}
.ai-change-evidence > div {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 10px;
}
.ai-change-evidence pre {
  font-size: 10px;
  padding: 12px;
  background: #f7f9fc;
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow: auto;
}
.ai-dialog-copy {
  color: #6a7e9a;
  font-size: 13px;
  line-height: 1.9;
  margin-bottom: 18px;
}
.ai-confirm-target {
  padding: 15px;
  background: #f6f9ff;
  border-radius: 8px;
  margin: 16px 0;
}
.ai-confirm-target strong,
.ai-confirm-target small {
  display: block;
}
.ai-confirm-target small {
  font-size: 12px;
  color: #8d9cb1;
  margin-top: 6px;
}
@media (max-width: 1200px) {
  .ai-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .ai-overview-columns {
    grid-template-columns: 1fr;
    gap: 0;
  }
}
@media (max-width: 600px) {
  .ai-tabs {
    gap: 0;
  }
  .ai-tabs button {
    padding: 13px 11px;
    font-size: 12px;
  }
  .ai-tabs svg {
    display: none;
  }
  .ai-commandbar {
    align-items: flex-start;
  }
  .ai-actions {
    width: 100%;
    gap: 8px;
  }
  .ai-actions .btn {
    padding: 9px 10px;
    font-size: 11px;
  }
  .ai-stats {
    gap: 10px;
  }
  .ai-stat {
    padding: 15px;
  }
  .ai-stat > strong {
    font-size: 25px;
  }
  .ai-stat p {
    font-size: 10px;
  }
  .ai-panel,
  .ai-report,
  .ai-suggestion,
  .ai-application {
    padding: 18px;
  }
  .ai-panel-title.padded {
    padding: 18px;
  }
  .ai-filter {
    flex: 1;
    min-width: 120px;
  }
  .ai-filter input,
  .ai-filter select {
    max-width: 100%;
    width: 100%;
  }
  .ai-products > article {
    flex-direction: column;
    gap: 8px;
    padding: 13px;
  }
  .ai-products > article > div:last-child {
    text-align: left;
  }
  .ai-products > article > .ai-change-arrow {
    flex-direction: row;
  }
  .ai-report-metrics {
    gap: 10px 16px;
    padding: 12px;
  }
  .ai-change-evidence > div {
    grid-template-columns: 1fr;
  }
  .ai-platform-evidence summary span {
    display: block;
    margin: 4px 0;
  }
  .ai-filters {
    padding: 16px;
    gap: 12px;
  }
  .ai-recent {
    padding: 0 18px;
  }
}
</style>
