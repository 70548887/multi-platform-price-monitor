<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, ApiError, appVersion, auth, date, itemsOf, json, loadVersion } from '../api'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/EmptyState.vue'
import Modal from '../components/Modal.vue'
import Pagination from '../components/Pagination.vue'

interface UpdateRun {
  id: number
  current_version: string
  target_version?: string
  tag?: string
  status: string
  steps: {
    key: string
    name: string
    status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped'
  }[]
  progress: { percentage: number; downloaded_bytes?: number; total_bytes?: number }
  error?: string | null
  message?: string
  error_code?: string | null
  created_at?: string
  completed_at?: string | null
  backup_id?: string | null
  backup_version?: string
  release?: {
    version: string
    tag: string
    name: string
    notes: string
    published_at: string
    size: number
    asset_name: string
  }
  actor?: string
}
interface Backup {
  id: string
  version: string
  created_at: string
  run_id: number
}
interface UpdateConfig {
  enabled: boolean
  github_owner: string
  github_repo: string
  check_interval: number
  auto_download: boolean
  auto_restart: boolean
  token_configured?: boolean
  proxy_configured?: boolean
  proxy_route?: 'direct' | 'overseas' | 'custom'
  proxy_enabled?: boolean
  proxy_ready?: boolean
  proxy_source?: string
}
interface UpdateStatus {
  current_version: string
  latest_version?: string | null
  status: string
  enabled: boolean
  host?: { connected: boolean; mode?: string; last_seen?: string }
  run?: UpdateRun | null
  backups: Backup[]
}

const config = ref<UpdateConfig | null>(null)
const status = ref<UpdateStatus | null>(null)
const history = ref<UpdateRun[]>([])
const historyPage = ref(1)
const historyTotal = ref(0)
const historyPageSize = 20
const loading = ref(true)
const refreshing = ref(false)
const actionBusy = ref('')
const error = ref('')
const notice = ref('')
const reconnecting = ref(false)
const copied = ref('')
const hostCommands = {
  native: 'powershell -ExecutionPolicy Bypass -File scripts/update_host.ps1 -Mode native',
  compose: 'bash scripts/update_host.sh --mode compose',
}
const confirmAction = ref<
  { action: 'apply'; run: UpdateRun } | { action: 'rollback'; backup: Backup } | null
>(null)
const details = ref<UpdateRun | null>(null)
const selectedBackupId = ref('')
const isAdmin = computed(() => auth.user?.role === 'admin')
const run = computed(() => status.value?.run ?? null)
const activeStates = [
  'checking',
  'downloading',
  'awaiting_host',
  'applying',
  'restarting',
  'rolling_back',
  'applied',
]
const isActive = computed(() => activeStates.includes(run.value?.status ?? ''))
const isBusy = computed(() => Boolean(actionBusy.value) || isActive.value)
const hostConnected = computed(() => status.value?.host?.connected ?? false)
const enabled = computed(() => config.value?.enabled === true)
const progress = computed(() =>
  Math.min(100, Math.max(0, Number(run.value?.progress?.percentage) || 0)),
)
const latestVersion = computed(
  () => status.value?.latest_version ?? run.value?.target_version ?? '—',
)
const releaseNotes = computed(() => run.value?.release?.notes ?? '')
const publishedAt = computed(() => run.value?.release?.published_at)
const runError = computed(() => run.value?.error ?? '')
const steps = computed(() => run.value?.steps ?? [])
const backups = computed(() => status.value?.backups ?? [])
const selectedBackup = computed(() =>
  backups.value.find((item) => item.id === selectedBackupId.value),
)
const statusLabels: Record<string, string> = {
  idle: '尚未检查',
  checking: '检查中',
  available: '发现新版本',
  up_to_date: '已是最新版本',
  downloading: '下载中',
  ready: '已校验，待安装',
  applied: '已应用，等待手动重启',
  awaiting_host: '等待宿主机更新器',
  applying: '安装中',
  restarting: '服务重启中',
  succeeded: '更新成功',
  failed: '更新失败',
  rolling_back: '回滚中',
  rolled_back: '已回滚',
}
function label(value: string | undefined) {
  return statusLabels[value ?? 'idle'] ?? value ?? '尚未检查'
}
function badge(value: string) {
  if (['succeeded', 'rolled_back', 'up_to_date', 'ready'].includes(value)) return 'success'
  if (value === 'failed') return 'danger'
  if (['available', 'awaiting_host'].includes(value)) return 'warning'
  return activeStates.includes(value) ? 'info' : 'neutral'
}
function backupFor(item: UpdateRun) {
  return backups.value.find((backup) => backup.run_id === item.id)
}

let disposed = false
let timer: ReturnType<typeof setTimeout> | undefined
let requestController: AbortController | undefined
let mutationController: AbortController | undefined
let historyKey = ''
let copyTimer: ReturnType<typeof setTimeout> | undefined
async function copyCommand(mode: 'native' | 'compose') {
  try {
    await navigator.clipboard.writeText(hostCommands[mode])
    if (disposed) return
    copied.value = mode
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => (copied.value = ''), 2000)
  } catch {
    if (!disposed) notice.value = '请选中并复制下方命令，在宿主机项目目录运行。'
  }
}
function schedule() {
  if (timer) clearTimeout(timer)
  if (!disposed && isAdmin.value)
    timer = setTimeout(
      () => refresh(),
      document.hidden ? 30000 : isActive.value || reconnecting.value ? 3000 : 15000,
    )
}
async function refresh(initial = false, forceHistory = false) {
  if (disposed || refreshing.value || !isAdmin.value) return
  refreshing.value = true
  requestController = new AbortController()
  try {
    const next = await api<UpdateStatus>('/update/status', { signal: requestController.signal })
    if (disposed) return
    status.value = next
    if (next.current_version) appVersion.value = next.current_version
    reconnecting.value = false
    error.value = ''
    const key = `${run.value?.id ?? ''}:${run.value?.status ?? ''}`
    const requests: Promise<unknown>[] = []
    if (initial || forceHistory || !config.value)
      requests.push(
        api<UpdateConfig>('/update/config', { signal: requestController.signal }).then((value) => {
          if (!disposed) config.value = value
        }),
      )
    if (initial || forceHistory || key !== historyKey)
      requests.push(
        api(`/update/history?page=${historyPage.value}&page_size=${historyPageSize}`, {
          signal: requestController.signal,
        }).then((value) => {
          if (!disposed) {
            history.value = itemsOf<UpdateRun>(value)
            historyTotal.value = value.total ?? history.value.length
            historyKey = key
          }
        }),
      )
    await Promise.all(requests)
  } catch (e: unknown) {
    if (disposed) return
    if (e instanceof ApiError && [0, 502, 503, 504].includes(e.status)) {
      reconnecting.value = true
      error.value = ''
    } else error.value = e instanceof Error ? e.message : '读取更新状态失败。'
  } finally {
    if (!disposed) {
      loading.value = false
      refreshing.value = false
      schedule()
    }
  }
}
async function changeHistoryPage(page: number) {
  if (disposed || refreshing.value || page === historyPage.value) return
  historyPage.value = page
  await refresh(false, true)
}
async function perform(
  action: 'check' | 'download' | 'apply' | 'rollback',
  item?: UpdateRun | Backup,
) {
  if (disposed || !isAdmin.value || isBusy.value || !enabled.value) return
  actionBusy.value = action
  error.value = ''
  notice.value = ''
  mutationController = new AbortController()
  try {
    const body =
      action === 'rollback' && item ? { backup_id: item.id } : item ? { run_id: item.id } : {}
    await api(`/update/${action}`, { ...json('POST', body), signal: mutationController.signal })
    if (disposed) return
    historyPage.value = 1
    confirmAction.value = null
    notice.value = {
      check: '更新检查已提交。',
      download: '下载任务已提交，校验完成后可安装。',
      apply: '安装任务已提交，服务重启期间会自动重新连接。',
      rollback: '回滚任务已提交，服务重启期间会自动重新连接。',
    }[action]
    await loadVersion({ signal: mutationController.signal }).catch(() => {})
  } catch (e: unknown) {
    if (disposed) return
    if (
      e instanceof ApiError &&
      [0, 502, 503, 504].includes(e.status) &&
      ['apply', 'rollback'].includes(action)
    ) {
      reconnecting.value = true
      confirmAction.value = null
      notice.value = '连接暂时中断，正在自动核对任务状态。请等待状态恢复。'
    } else error.value = e instanceof Error ? e.message : '操作失败，请重新检查状态。'
  } finally {
    if (!disposed) {
      actionBusy.value = ''
      await refresh(false, true)
    }
  }
}
function onVisible() {
  if (!document.hidden) refresh()
}
onMounted(() => {
  refresh(true)
  document.addEventListener('visibilitychange', onVisible)
})
onBeforeUnmount(() => {
  disposed = true
  if (timer) clearTimeout(timer)
  if (copyTimer) clearTimeout(copyTimer)
  requestController?.abort()
  mutationController?.abort()
  document.removeEventListener('visibilitychange', onVisible)
})
</script>

<template>
  <div v-if="!isAdmin" class="notice error">只有管理员可以访问系统更新。</div>
  <div v-else class="update-page">
    <div v-if="error" class="notice error" role="alert">{{ error }}</div>
    <div v-if="notice" class="notice success" role="status">{{ notice }}</div>
    <div v-if="reconnecting" class="setup-guide" role="status">
      <span class="spinner"></span>
      <div>
        <strong>正在重新连接服务</strong>
        <p>重启期间连接可能短暂中断，页面将自动恢复并显示最终结果。</p>
      </div>
    </div>
    <div v-if="loading" class="loading-state"><span class="spinner"></span>正在读取更新信息…</div>
    <template v-else>
      <section class="panel update-overview">
        <div class="update-overview-top">
          <div class="update-symbol"><Icon name="refresh" :size="26" /></div>
          <div>
            <h2>应用版本</h2>
            <p class="muted">从 GitHub 正式发布版本更新</p>
          </div>
          <span class="badge" :class="badge(status?.status ?? 'idle')"
            ><i></i>{{ label(status?.status) }}</span
          >
        </div>
        <div class="update-version-grid">
          <div>
            <span>当前版本</span
            ><strong class="mono">v{{ status?.current_version || appVersion.value || '—' }}</strong>
          </div>
          <Icon name="arrow" :size="22" />
          <div>
            <span>最新版本</span
            ><strong class="mono">{{
              latestVersion === '—' ? '—' : `v${latestVersion.replace(/^v/, '')}`
            }}</strong>
          </div>
          <div class="update-source">
            <span>更新仓库</span
            ><strong class="mono">{{
              config ? `${config.github_owner}/${config.github_repo}` : '待读取'
            }}</strong>
          </div>
        </div>
        <div class="update-actions">
          <button
            class="btn btn-white"
            :disabled="!enabled || isBusy || reconnecting"
            @click="perform('check')"
          >
            <Icon name="refresh" :size="16" />{{
              actionBusy === 'check' ? '提交中…' : '检查更新'
            }}</button
          ><button
            class="btn btn-white"
            :disabled="!enabled || isBusy || reconnecting || run?.status !== 'available'"
            @click="perform('download', run!)"
          >
            <Icon name="download" :size="16" />下载更新</button
          ><button
            class="btn btn-primary"
            :disabled="!enabled || isBusy || reconnecting || run?.status !== 'ready'"
            @click="run && (confirmAction = { action: 'apply', run })"
          >
            <Icon name="play" :size="16" />应用更新</button
          ><span v-if="config && !config.enabled" class="muted">自更新已在配置中停用</span
          ><span v-else-if="config" class="muted"
            >{{
              config.check_interval === 0 ? '仅手动检测' : `每 ${config.check_interval} 秒检测`
            }}
            · {{ config.auto_restart ? '安装后自动重启' : '安装后等待手动重启' }}</span
          >
        </div>
      </section>

      <div v-if="config" class="notice update-network" :class="config.proxy_ready === false ? 'warning' : ''">
        <strong
          >更新网络：{{
            config.proxy_source || (config.proxy_configured ? '独立更新代理' : '直连')
          }}</strong
        >
        <span v-if="config.proxy_route === 'overseas'">
          · {{ config.proxy_ready ? '已配置并启用' : '未就绪，请配置并启用境外代理' }}
        </span>
        <p>
          GitHub 版本检测、SHA256 校验文件和发布包下载使用此线路；连接失败时不会自动切换为直连。
        </p>
        <RouterLink
          v-if="config.proxy_route === 'overseas'"
          to="/admin/settings/network"
          class="text-link"
          >管理境外代理</RouterLink
        >
      </div>
      <section v-if="status && !hostConnected" class="setup-guide update-host">
        <Icon name="platforms" :size="22" />
        <div>
          <strong>宿主机更新器未连接</strong>
          <p>安装任务会等待宿主机更新器处理。请由服务器管理员在项目目录启动对应更新器。</p>
          <details>
            <summary>查看启动命令</summary>
            <div class="connect-command">
              <span>Windows</span><code>{{ hostCommands.native }}</code>
              <button class="btn btn-white btn-sm" @click="copyCommand('native')">
                {{ copied === 'native' ? '已复制' : '复制命令' }}
              </button>
              <span>Linux / Docker</span><code>{{ hostCommands.compose }}</code>
              <button class="btn btn-white btn-sm" @click="copyCommand('compose')">
                {{ copied === 'compose' ? '已复制' : '复制命令' }}
              </button>
            </div>
          </details>
        </div>
      </section>
      <div v-else-if="status" class="update-host-ready">
        <span class="live-dot"></span>宿主机更新器已连接<span
          v-if="status.host?.last_seen"
          class="muted"
          >最近心跳 {{ date(status.host.last_seen) }}</span
        >
      </div>

      <section v-if="run" class="panel update-progress-panel" aria-live="polite">
        <div class="update-section-heading">
          <div>
            <h2>更新进度</h2>
            <p class="muted">任务 #{{ run.id }} · {{ label(run.status) }}</p>
          </div>
          <span class="mono">{{ progress }}%</span>
        </div>
        <div
          class="update-progress-track"
          role="progressbar"
          :aria-valuenow="progress"
          :aria-valuemin="0"
          :aria-valuemax="100"
          :aria-label="label(run.status)"
        >
          <span
            :style="{ width: `${progress}%` }"
            :class="{ failed: run.status === 'failed' }"
          ></span>
        </div>
        <ol class="update-steps">
          <li
            v-for="(step, index) in steps"
            :key="step.key"
            :class="{
              complete: step.status === 'completed',
              current: step.status === 'running',
              failed: step.status === 'failed',
              skipped: step.status === 'skipped',
            }"
          >
            <span>{{
              step.status === 'completed'
                ? '✓'
                : step.status === 'failed'
                  ? '!'
                  : step.status === 'skipped'
                    ? '—'
                    : index + 1
            }}</span
            >{{ step.name }}
          </li>
        </ol>
        <div v-if="runError" class="notice error" role="alert">{{ runError }}</div>
        <p v-if="run.message" class="form-help">{{ run.message }}</p>
        <p v-if="run.status === 'awaiting_host'" class="form-help">
          安装申请已保存，等待宿主机更新器接收。
        </p>
        <p v-if="run.status === 'rolled_back'" class="form-help">
          旧版本已恢复；请查看任务详情确认回滚结果。
        </p>
      </section>

      <section v-if="backups.length" class="panel update-rollback">
        <div class="update-section-heading">
          <div>
            <h2>回滚备份</h2>
            <p class="muted">恢复更新前的应用版本</p>
          </div>
          <Icon name="back" :size="20" />
        </div>
        <div class="update-rollback-controls">
          <select v-model="selectedBackupId" aria-label="选择回滚备份">
            <option value="">选择可用备份</option>
            <option v-for="backup in backups" :key="backup.id" :value="backup.id">
              v{{ backup.version }} · {{ date(backup.created_at) }} · 任务 #{{ backup.run_id }}
            </option></select
          ><button
            class="btn btn-white"
            :disabled="!selectedBackup || !enabled || isBusy || reconnecting"
            @click="
              selectedBackup && (confirmAction = { action: 'rollback', backup: selectedBackup })
            "
          >
            <Icon name="back" :size="16" />回滚此备份
          </button>
        </div>
      </section>
      <section class="panel update-release">
        <div class="update-section-heading">
          <div>
            <h2>版本说明</h2>
            <p class="muted">
              {{ publishedAt ? `发布于 ${date(publishedAt)}` : '检查更新后显示正式版本说明' }}
            </p>
          </div>
          <Icon name="file" :size="20" />
        </div>
        <div v-if="releaseNotes" class="update-release-notes">{{ releaseNotes }}</div>
        <p v-else class="muted update-empty-notes">暂无版本说明。</p>
      </section>

      <section class="panel">
        <div class="panel-toolbar">
          <h2>升级历史</h2>
          <button class="btn btn-white btn-sm" :disabled="refreshing" @click="refresh(false, true)">
            <Icon name="refresh" :size="15" />更新状态
          </button>
        </div>
        <div v-if="history.length" class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>任务 / 版本</th>
                <th>开始时间</th>
                <th>结束时间</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in history" :key="item.id">
                <td>
                  <strong class="mono"
                    >{{ item.current_version }} → {{ item.target_version ?? '待检测' }}</strong
                  >
                  <div class="table-secondary">任务 #{{ item.id }}</div>
                </td>
                <td>{{ date(item.created_at) }}</td>
                <td>{{ date(item.completed_at) }}</td>
                <td>
                  <span class="badge" :class="badge(item.status)"
                    ><i></i>{{ label(item.status) }}</span
                  >
                </td>
                <td class="update-history-actions">
                  <button class="text-link" @click="details = item">详情</button
                  ><button
                    v-if="backupFor(item)"
                    class="text-link"
                    :disabled="!enabled || isBusy || reconnecting"
                    @click="confirmAction = { action: 'rollback', backup: backupFor(item)! }"
                  >
                    回滚
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <EmptyState
          v-else
          icon="clock"
          title="暂无升级记录"
          description="检查和安装更新后，可在这里查看执行结果。"
        />
        <Pagination
          v-if="historyTotal"
          :page="historyPage"
          :total="historyTotal"
          :page-size="historyPageSize"
          @change="changeHistoryPage"
        />
      </section>
    </template>
    <Modal
      v-if="confirmAction"
      :title="confirmAction.action === 'apply' ? '应用新版本' : '回滚到备份版本'"
      @close="!actionBusy && (confirmAction = null)"
      ><p>
        {{
          confirmAction.action === 'apply'
            ? `将安装版本 ${confirmAction.run.target_version}，更新器会先备份当前版本。`
            : `将恢复备份版本 ${confirmAction.backup.version}。`
        }}
      </p>
      <p class="form-help update-confirm-help">
        {{
          config?.auto_restart
            ? '处理期间应用服务会短暂重启，此页面会自动重新连接。'
            : '处理完成后需要服务器管理员手动重启应用服务。'
        }}
      </p>
      <div class="modal-actions">
        <button class="btn btn-white" :disabled="Boolean(actionBusy)" @click="confirmAction = null">
          取消</button
        ><button
          class="btn btn-primary"
          :disabled="Boolean(actionBusy) || isActive"
          @click="
            confirmAction.action === 'apply'
              ? perform('apply', confirmAction.run)
              : perform('rollback', confirmAction.backup)
          "
        >
          {{ actionBusy ? '提交中…' : confirmAction.action === 'apply' ? '确认安装' : '确认回滚' }}
        </button>
      </div></Modal
    >
    <Modal v-if="details" :title="`更新任务 #${details.id}`" @close="details = null"
      ><dl class="detail-list">
        <div>
          <dt>原版本</dt>
          <dd class="mono">{{ details.current_version }}</dd>
        </div>
        <div>
          <dt>目标版本</dt>
          <dd class="mono">{{ details.target_version ?? '—' }}</dd>
        </div>
        <div>
          <dt>发布标签</dt>
          <dd class="mono">{{ details.tag ?? '—' }}</dd>
        </div>
        <div>
          <dt>任务状态</dt>
          <dd>{{ label(details.status) }}</dd>
        </div>
        <div>
          <dt>开始时间</dt>
          <dd>{{ date(details.created_at) }}</dd>
        </div>
        <div>
          <dt>结束时间</dt>
          <dd>{{ date(details.completed_at) }}</dd>
        </div>
        <div>
          <dt>操作者</dt>
          <dd>{{ details.actor ?? '系统' }}</dd>
        </div>
        <div>
          <dt>备份</dt>
          <dd>{{ details.backup_id ? '已创建' : '未创建' }}</dd>
        </div>
      </dl>
      <div v-if="details.error" class="notice error">{{ details.error }}</div></Modal
    >
  </div>
</template>

<style scoped>
.update-network {
  display: block;
  line-height: 1.8;
}
.update-network p {
  margin: 8px 0;
}
.update-page {
  display: grid;
  gap: 20px;
}
.update-page > .notice,
.update-page > .setup-guide {
  margin: 0;
}
.update-overview {
  padding: 26px;
}
.update-overview-top {
  display: flex;
  align-items: center;
  gap: 13px;
}
.update-overview-top > .badge {
  margin-left: auto;
}
.update-overview-top p,
.update-section-heading p {
  font-size: 11px;
  margin-top: 4px;
}
.update-symbol {
  display: grid;
  place-items: center;
  height: 48px;
  width: 48px;
  border-radius: 12px;
  color: #5276dc;
  background: #edf2ff;
}
.update-version-grid {
  display: flex;
  align-items: center;
  gap: 32px;
  padding: 29px 0;
}
.update-version-grid > div {
  display: grid;
  gap: 6px;
}
.update-version-grid span {
  color: var(--muted);
  font-size: 11px;
}
.update-version-grid strong {
  font-size: 26px;
  letter-spacing: -0.6px;
}
.update-version-grid > svg {
  color: #aebcd4;
}
.update-version-grid .update-source {
  margin-left: auto;
  max-width: 45%;
}
.update-source strong {
  font-size: 13px;
  overflow-wrap: anywhere;
  letter-spacing: 0;
}
.update-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  border-top: 1px solid var(--border);
  padding-top: 20px;
}
.update-actions > .muted {
  margin-left: auto;
  font-size: 11px;
}
.update-host details {
  margin-top: 12px;
}
.update-host summary {
  cursor: pointer;
  font-size: 11px;
}
.update-host .connect-command {
  margin-top: 12px;
  margin-bottom: 0;
}
.update-host .connect-command code + span {
  margin-top: 12px;
}
.update-host-ready {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #309980;
  font-size: 12px;
}
.update-host-ready .muted {
  margin-left: auto;
}
.update-progress-panel,
.update-release,
.update-rollback {
  padding: 24px 26px;
}
.update-section-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 15px;
}
.update-section-heading > .mono {
  color: #5478dc;
}
.update-progress-track {
  background: #edf1f8;
  border-radius: 8px;
  height: 7px;
  overflow: hidden;
  margin: 23px 0;
}
.update-progress-track > span {
  display: block;
  background: #6689eb;
  height: 100%;
  transition: width 0.3s;
}
.update-progress-track > .failed {
  background: #dc6274;
}
.update-steps {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  padding: 0;
  margin: 0;
  list-style: none;
  gap: 9px;
}
.update-steps li {
  display: flex;
  align-items: center;
  gap: 7px;
  color: #9ba8bd;
  font-size: 11px;
}
.update-steps li > span {
  display: grid;
  place-items: center;
  border-radius: 50%;
  height: 23px;
  width: 23px;
  flex-shrink: 0;
  background: #f1f4f9;
  font-size: 10px;
}
.update-steps .complete {
  color: #309980;
}
.update-steps .complete > span {
  background: #eaf8f2;
}
.update-steps .current {
  color: #5478dc;
  font-weight: 600;
}
.update-steps .current > span {
  background: #eaf0ff;
}
.update-steps .failed {
  color: #d45a70;
}
.update-steps .failed > span {
  background: #fff0f2;
}
.update-steps .skipped {
  opacity: 0.65;
}
.update-rollback-controls {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 20px;
}
.update-rollback-controls select {
  flex: 1;
  min-width: 220px;
}
.update-progress-panel > .notice,
.update-progress-panel > .form-help {
  margin-top: 22px;
}
.update-release-notes {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.9;
  color: #667890;
  padding-top: 20px;
  font-size: 12px;
  max-height: 360px;
  overflow-y: auto;
}
.update-empty-notes {
  padding-top: 20px;
  font-size: 12px;
}
.update-history-actions {
  display: flex;
  gap: 16px;
}
.update-confirm-help {
  margin-top: 12px;
}
@media (max-width: 820px) {
  .update-version-grid {
    flex-wrap: wrap;
    gap: 22px;
  }
  .update-version-grid .update-source {
    margin-left: 0;
    max-width: 100%;
    width: 100%;
  }
  .update-actions > .muted {
    margin-left: 0;
    width: 100%;
    margin-top: 3px;
  }
  .update-steps {
    grid-template-columns: repeat(4, 1fr);
    gap: 13px 8px;
  }
}
@media (max-width: 480px) {
  .update-overview,
  .update-progress-panel,
  .update-release,
  .update-rollback {
    padding: 20px;
  }
  .update-overview-top {
    flex-wrap: wrap;
  }
  .update-overview-top > .badge {
    margin-left: 0;
  }
  .update-version-grid strong {
    font-size: 23px;
  }
  .update-steps {
    grid-template-columns: repeat(3, 1fr);
  }
  .update-host-ready {
    flex-wrap: wrap;
  }
  .update-host-ready .muted {
    width: 100%;
    margin-left: 16px;
  }
}
</style>
