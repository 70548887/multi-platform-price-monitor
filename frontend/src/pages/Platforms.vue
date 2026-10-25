<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api, itemsOf, json, auth, date } from '../api'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/EmptyState.vue'
import Modal from '../components/Modal.vue'
const platforms = ref<any[]>([]),
  loading = ref(true),
  error = ref(''),
  notice = ref(''),
  editing = ref<any>(null),
  showForm = ref(false),
  sessionPlatform = ref<any>(null),
  token = ref(''),
  saving = ref(false),
  formError = ref(''),
  collecting = ref<number | null>(null)
const form = reactive({
  name: '',
  base_url: 'https://www.xiaohong66.com',
  adapter: 'xiaohong66',
  account_label: '监控账号',
  enabled: true,
  interval_seconds: 3600,
  request_interval: 1,
})
const admin = computed(() => auth.user?.role === 'admin')
const monitorOrigin = window.location.origin
const helperOriginAllowed =
  window.location.protocol === 'https:' ||
  ['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname) ||
  /^127\./.test(window.location.hostname)
async function load() {
  loading.value = true
  error.value = ''
  try {
    platforms.value = itemsOf(await api('/platforms'))
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
onMounted(load)
function open(platform?: any) {
  editing.value = platform ?? null
  Object.assign(form, {
    name: platform?.name ?? '',
    base_url: platform?.base_url ?? 'https://www.xiaohong66.com',
    adapter: platform?.adapter ?? 'xiaohong66',
    account_label: platform?.account_label ?? '监控账号',
    enabled: platform?.enabled ?? true,
    interval_seconds: platform?.interval_seconds ?? 3600,
    request_interval: platform?.request_interval ?? 1,
  })
  formError.value = ''
  showForm.value = true
}
async function save() {
  saving.value = true
  formError.value = ''
  try {
    await api(
      editing.value ? `/platforms/${editing.value.id}` : '/platforms',
      json(editing.value ? 'PUT' : 'POST', form),
    )
    showForm.value = false
    notice.value = editing.value
      ? '平台配置已更新。'
      : '平台已添加并生成默认规则草稿。请连接账号，再测试并发布规则。'
    await load()
  } catch (e: any) {
    formError.value = e.message
  } finally {
    saving.value = false
  }
}
function connect(platform: any) {
  sessionPlatform.value = platform
  token.value = ''
  formError.value = ''
}
function finishSessionConnection() {
  sessionPlatform.value = null
  token.value = ''
  void load()
}
async function saveSession() {
  saving.value = true
  formError.value = ''
  try {
    const connected = await api(
      `/platforms/${sessionPlatform.value.id}/session`,
      json('PUT', { token: token.value.trim() }),
    )
    token.value = ''
    sessionPlatform.value = null
    notice.value = connected.active_rule_id
      ? '会话已加密保存，可以发起采集。'
      : '会话已连接。请进入规则页测试默认草稿，发布后再开始采集。'
    await load()
  } catch (e: any) {
    formError.value = e.message
  } finally {
    saving.value = false
  }
}
async function collect(platform: any) {
  collecting.value = platform.id
  error.value = ''
  try {
    const result = await api(`/platforms/${platform.id}/collect`, json('POST'))
    notice.value = `${platform.name} 的采集任务已提交（批次 #${result.id}）。可在采集任务中查看进度。`
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    collecting.value = null
  }
}
function authLabel(value: string) {
  return (
    ({ missing: '未连接', connected: '已连接', expired: '登录过期' } as Record<string, string>)[
      value
    ] ?? value
  )
}
</script>
<template>
  <div class="page-tools">
    <div class="section-description">
      每个平台独立配置账号、采集频率与规则。<strong>{{ platforms.length }}</strong> 个已接入平台
    </div>
    <button v-if="admin" class="btn btn-primary" @click="open()">
      <Icon name="plus" :size="17" />添加交易平台
    </button>
  </div>
  <div v-if="admin" class="setup-guide">
    <Icon name="link" :size="21" />
    <div>
      <strong>首次接入：添加平台 → 连接账号 → 测试规则 → 发布版本 → 首次采集</strong>
      <p>新增平台会自动生成默认草稿；登录会话有效且规则已发布后，才会启动自动采集。</p>
    </div>
  </div>
  <div v-if="notice" class="notice success" role="status">
    <Icon name="check" :size="18" />{{ notice
    }}<button class="notice-close" aria-label="关闭提示" @click="notice = ''">×</button>
  </div>
  <div v-if="error" class="notice error" role="alert">
    {{ error }}<button class="text-link" @click="load">重试</button>
  </div>
  <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载平台…</div>
  <div v-else-if="platforms.length" class="platform-grid">
    <article v-for="platform in platforms" :key="platform.id" class="panel platform-card">
      <div class="platform-card-header">
        <div class="platform-logo"><Icon name="globe" :size="25" /></div>
        <span class="badge" :class="platform.enabled ? 'success' : 'neutral'"
          ><i></i>{{ platform.enabled ? '已启用' : '已停用' }}</span
        >
      </div>
      <h2>{{ platform.name }}</h2>
      <a :href="platform.base_url" target="_blank" rel="noopener noreferrer" class="platform-url"
        >{{ platform.base_url }}<Icon name="external" :size="12"
      /></a>
      <div class="platform-stats">
        <div>
          <span>监控商品</span
          ><strong>{{ (platform.product_count ?? 0).toLocaleString() }}<small>个</small></strong>
        </div>
        <div>
          <span>采集周期</span
          ><strong
            >{{
              platform.interval_seconds % 3600 === 0
                ? platform.interval_seconds / 3600
                : platform.interval_seconds / 60
            }}<small>{{ platform.interval_seconds % 3600 === 0 ? '小时' : '分钟' }}</small></strong
          >
        </div>
      </div>
      <div class="platform-detail">
        <span>采集账号</span><strong>{{ platform.account_label }}</strong>
      </div>
      <div class="platform-detail">
        <span>登录会话</span
        ><strong
          :class="
            platform.auth_status === 'connected'
              ? 'green-text'
              : platform.auth_status === 'expired'
                ? 'danger-text'
                : 'muted'
          "
          ><span class="inline-dot" :class="platform.auth_status"></span
          >{{ authLabel(platform.auth_status) }}</strong
        >
      </div>
      <div class="platform-detail">
        <span>最近成功</span><strong>{{ date(platform.last_success_at, true) }}</strong>
      </div>
      <div v-if="platform.last_error" class="platform-error">
        <Icon name="alert" :size="14" />{{ platform.last_error }}
      </div>
      <div v-else-if="!platform.active_rule_id" class="platform-hint setup-hint">
        <Icon name="alert" :size="13" />{{
          admin
            ? '连接账号后，请进入规则页完成测试并发布。'
            : '采集规则尚未发布，等待管理员完成配置。'
        }}
      </div>
      <div v-else class="platform-hint">
        适配器 {{ platform.adapter }} · 每 {{ platform.request_interval }} 秒最多 1 次请求
      </div>
      <div v-if="admin" class="platform-actions">
        <button
          class="btn btn-primary btn-sm"
          :disabled="
            collecting === platform.id ||
            !platform.enabled ||
            !platform.active_rule_id ||
            platform.auth_status !== 'connected'
          "
          @click="collect(platform)"
        >
          <Icon :name="collecting === platform.id ? 'refresh' : 'play'" :size="14" />{{
            collecting === platform.id ? '提交中…' : '立即采集'
          }}</button
        ><button class="btn btn-white btn-sm" @click="connect(platform)">
          <Icon name="key" :size="14" />{{
            platform.auth_status === 'missing' ? '连接账号' : '更新会话'
          }}</button
        ><RouterLink :to="`/platforms/${platform.id}/rules`" class="btn btn-white btn-sm"
          >规则</RouterLink
        ><button class="icon-btn" title="编辑平台" aria-label="编辑平台" @click="open(platform)">
          <Icon name="edit" :size="17" />
        </button>
      </div>
    </article>
  </div>
  <section v-else class="panel">
    <EmptyState
      icon="platforms"
      title="连接第一个交易平台"
      description="添加平台、配置采集账号，即可开始自动记录全站商品价格。"
      ><button v-if="admin" class="btn btn-primary" @click="open()">
        <Icon name="plus" :size="17" />添加交易平台
      </button></EmptyState
    >
  </section>
  <div class="page-note">
    <Icon name="shield" :size="15" />登录会话在服务端加密保存。会话过期会暂停采集，并在事件中提醒。
  </div>
  <Modal
    v-if="showForm"
    :title="editing ? '编辑交易平台' : '添加交易平台'"
    @close="!saving && (showForm = false)"
    ><form @submit.prevent="save">
      <div v-if="formError" class="notice error">{{ formError }}</div>
      <label
        >平台名称<input
          v-model="form.name"
          required
          maxlength="120"
          placeholder="例如：亿乐海外运营学习站" /></label
      ><label
        >平台网址<input
          v-model="form.base_url"
          type="url"
          required
          placeholder="https://www.xiaohong66.com"
      /></label>
      <div class="form-grid">
        <label
          >采集适配器<select v-model="form.adapter">
            <option value="xiaohong66">Xiaohong66</option>
          </select></label
        ><label
          >采集账号标识（建立商品后固定）<input
            v-model="form.account_label"
            required
            maxlength="120"
            :disabled="Boolean(editing && editing.product_count > 0)"
            placeholder="例如：采购监控账号"
          /><small>{{
            editing?.product_count > 0
              ? '已有商品，账号标识已固定；更换采集账号请创建新的平台连接。'
              : '使用稳定且唯一的账号标识，建立商品后将固定保存。'
          }}</small></label
        >
      </div>
      <div class="form-grid">
        <label
          >采集间隔（秒）<input
            v-model.number="form.interval_seconds"
            type="number"
            min="3600"
            max="604800"
            step="1"
            required
          /><small>3600 秒 = 每小时一次</small></label
        ><label
          >请求间隔（秒）<input
            v-model.number="form.request_interval"
            type="number"
            min="1"
            max="60"
            step="0.1"
            required
          /><small>同平台所有任务共享限速</small></label
        >
      </div>
      <label class="checkbox-label"
        ><input v-model="form.enabled" type="checkbox" />启用平台自动采集</label
      >
      <div class="modal-actions">
        <button type="button" class="btn btn-white" :disabled="saving" @click="showForm = false">
          取消</button
        ><button class="btn btn-primary" :disabled="saving">
          {{ saving ? '保存中…' : '保存平台' }}
        </button>
      </div>
    </form></Modal
  ><Modal
    v-if="sessionPlatform"
    :title="`连接账号 · ${sessionPlatform.name}`"
    @close="!saving && (sessionPlatform = null)"
    ><form @submit.prevent="saveSession">
      <div class="connection-intro">
        <span class="connection-symbol"><Icon name="globe" :size="24" /></span>
        <h3>在浏览器中登录采集账号</h3>
        <p>
          由管理员在服务器或开发电脑运行连接助手，助手将打开目标平台。你只需在浏览器内完成登录，会话会自动加密提交。
        </p>
      </div>
      <div v-if="helperOriginAllowed" class="connect-command">
        <span>项目根目录运行 · 连接当前监控后台</span
        ><code
          >python scripts/connect_platform.py --platform-id {{ sessionPlatform.id }} --monitor-url
          "{{ monitorOrigin }}"</code
        >
      </div>
      <div v-else class="notice warning">
        远程连接助手需要 HTTPS。请从本监控后台的 HTTPS 地址打开此页面，再复制连接命令；HTTP
        仅支持本机 localhost 地址。
      </div>
      <p class="form-help">
        采集账号标识：{{
          sessionPlatform.account_label
        }}。连接完成后关闭此窗口并刷新平台状态，再进入规则页测试并发布。
      </p>
      <details class="advanced-session">
        <summary>高级连接：手动填写会话 Token</summary>
        <div v-if="formError" class="notice error">{{ formError }}</div>
        <label
          >登录会话 Token<input
            v-model="token"
            type="password"
            minlength="10"
            maxlength="16384"
            autocomplete="off"
            placeholder="粘贴目标平台登录会话 Token"
        /></label>
        <p class="form-help">会话在服务端加密保存，已存内容不会回显。</p>
        <button class="btn btn-primary" :disabled="saving || token.trim().length < 10">
          {{ saving ? '保存中…' : '加密保存会话' }}
        </button>
      </details>
      <div class="modal-actions">
        <button
          type="button"
          class="btn btn-white"
          :disabled="saving"
          @click="finishSessionConnection"
        >
          已完成，刷新状态
        </button>
      </div>
    </form></Modal
  >
</template>
