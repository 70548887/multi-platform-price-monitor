<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api, itemsOf, json, auth, date } from '../api'
import {
  fallbackPlatformTypes,
  platformAdapterName,
  platformIdentityLocked,
  platformSupported,
  platformTypeId,
  platformTypeName,
} from '../platformTypes'
import type { PlatformAdapter, PlatformType } from '../platformTypes'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/EmptyState.vue'
import Modal from '../components/Modal.vue'
const platforms = ref<any[]>([]),
  platformTypes = ref<PlatformType[]>(fallbackPlatformTypes),
  typeFilter = ref(''),
  directoryError = ref(''),
  loading = ref(true),
  error = ref(''),
  notice = ref(''),
  editing = ref<any>(null),
  showForm = ref(false),
  sessionPlatform = ref<any>(null),
  token = ref(''),
  saving = ref(false),
  formError = ref(''),
  checkingIdentity = ref(false),
  identityCheckFailed = ref(false),
  hasPublishedRules = ref(false),
  previousDefaultUrl = ref(''),
  collecting = ref<number | null>(null)
const form = reactive({
  name: '',
  platform_type: 'yile',
  base_url: 'https://www.xiaohong66.com',
  adapter: 'xiaohong66',
  account_label: '监控账号',
  enabled: true,
  interval_seconds: 3600,
  request_interval: 1,
  proxy_route: 'direct',
})
const admin = computed(() => auth.user?.role === 'admin')
const filterTypes = computed(() => {
  const types = platformTypes.value.map((type) => ({ id: type.id, name: type.name }))
  platforms.value.forEach((platform) => {
    const id = platformTypeId(platform, platformTypes.value)
    if (!types.some((type) => type.id === id))
      types.push({ id, name: platformTypeName(platform, platformTypes.value) })
  })
  return types
})
const filteredPlatforms = computed(() =>
  typeFilter.value
    ? platforms.value.filter(
        (platform) => platformTypeId(platform, platformTypes.value) === typeFilter.value,
      )
    : platforms.value,
)
const formTypes = computed(() => {
  if (!editing.value || platformTypes.value.some((type) => type.id === form.platform_type))
    return platformTypes.value
  return [
    ...platformTypes.value,
    {
      id: form.platform_type,
      name: platformTypeName(editing.value, platformTypes.value),
      adapters: [],
    },
  ]
})
const formAdapters = computed<PlatformAdapter[]>(() => {
  const adapters =
    platformTypes.value.find((type) => type.id === form.platform_type)?.adapters ?? []
  if (
    editing.value &&
    platformTypeId(editing.value, platformTypes.value) === form.platform_type &&
    !adapters.some((adapter) => adapter.id === editing.value.adapter)
  )
    return [
      ...adapters,
      {
        id: editing.value.adapter,
        name: platformAdapterName(editing.value, platformTypes.value),
        implemented: platformSupported(editing.value, platformTypes.value),
      },
    ]
  return adapters
})
const selectedAdapter = computed(() =>
  formAdapters.value.find((adapter) => adapter.id === form.adapter),
)
const selectedSupported = computed(() => Boolean(selectedAdapter.value?.implemented))
const identityLocked = computed(() =>
  Boolean(
    editing.value &&
      (checkingIdentity.value ||
        identityCheckFailed.value ||
        hasPublishedRules.value ||
        platformIdentityLocked(editing.value)),
  ),
)
const monitorOrigin = window.location.origin
const helperOriginAllowed =
  window.location.protocol === 'https:' ||
  ['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname) ||
  /^127\./.test(window.location.hostname)
async function load() {
  loading.value = true
  error.value = ''
  try {
    const [connections, directory] = await Promise.allSettled([
      api('/platforms'),
      api('/platform-types'),
    ])
    if (connections.status === 'rejected') throw connections.reason
    platforms.value = itemsOf(connections.value)
    if (directory.status === 'fulfilled' && itemsOf<PlatformType>(directory.value).length) {
      platformTypes.value = itemsOf<PlatformType>(directory.value)
      directoryError.value = ''
    } else {
      directoryError.value = '平台类型目录暂未加载，当前显示已知版本。刷新后可获取最新站点版本。'
    }
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
onMounted(load)
async function open(platform?: any) {
  editing.value = platform ?? null
  const typeId = platform
    ? platformTypeId(platform, platformTypes.value)
    : (platformTypes.value[0]?.id ?? '')
  const adapter = platformTypes.value.find((type) => type.id === typeId)?.adapters[0]
  Object.assign(form, {
    name: platform?.name ?? '',
    platform_type: typeId,
    base_url: platform?.base_url ?? adapter?.default_base_url ?? '',
    adapter: platform?.adapter ?? adapter?.id ?? '',
    account_label: platform?.account_label ?? '监控账号',
    enabled: platform?.enabled ?? true,
    interval_seconds: platform?.interval_seconds ?? 3600,
    request_interval: platform?.request_interval ?? 1,
    proxy_route: platform?.proxy_route ?? 'direct',
  })
  previousDefaultUrl.value = selectedAdapter.value?.default_base_url ?? ''
  hasPublishedRules.value = false
  checkingIdentity.value = false
  identityCheckFailed.value = false
  formError.value = ''
  showForm.value = true
  if (platform && !platformIdentityLocked(platform) && platform.type_locked === undefined) {
    checkingIdentity.value = true
    try {
      const rules = await api(`/admin/platforms/${platform.id}/rules`)
      if (editing.value?.id === platform.id)
        hasPublishedRules.value = itemsOf(rules).some((rule) => rule.status === 'published')
    } catch {
      if (editing.value?.id === platform.id) identityCheckFailed.value = true
    } finally {
      if (editing.value?.id === platform.id) checkingIdentity.value = false
    }
  }
}
function updateAdapterDefault() {
  const nextDefault = selectedAdapter.value?.default_base_url ?? ''
  const current = form.base_url.trim().replace(/\/$/, '')
  const previous = previousDefaultUrl.value.trim().replace(/\/$/, '')
  if (!current || (previous && current === previous)) form.base_url = nextDefault
  previousDefaultUrl.value = nextDefault
  if (!selectedSupported.value) form.enabled = false
}
function changePlatformType() {
  form.adapter = formAdapters.value[0]?.id ?? ''
  updateAdapterDefault()
}
async function save() {
  if (!form.adapter || !selectedAdapter.value) {
    formError.value = '请选择该平台类型下可配置的站点版本。'
    return
  }
  saving.value = true
  formError.value = ''
  try {
    await api(
      editing.value ? `/platforms/${editing.value.id}` : '/platforms',
      json(editing.value ? 'PUT' : 'POST', {
        ...form,
        enabled: selectedSupported.value && form.enabled,
      }),
    )
    showForm.value = false
    notice.value = !selectedSupported.value
      ? '平台配置已保存。当前站点版本待验证，验证完成后可连接账号并采集。'
      : editing.value
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
  if (!platformSupported(platform, platformTypes.value)) return
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
  if (!platformSupported(platform, platformTypes.value)) return
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
function proxyLabel(value: string | undefined) {
  return (
    ({ direct: '直连', tunnel: '隧道代理', overseas: '境外代理' } as Record<string, string>)[
      value ?? 'direct'
    ] ?? '待核对线路'
  )
}
</script>
<template>
  <div class="page-tools">
    <div class="section-description">
      每个平台独立配置账号、采集频率与规则。<strong>{{ platforms.length }}</strong> 个平台连接
    </div>
    <div class="filters platform-tools">
      <select v-model="typeFilter" aria-label="按平台类型筛选">
        <option value="">全部平台类型</option>
        <option v-for="type in filterTypes" :key="type.id" :value="type.id">
          {{ type.name }}
        </option>
      </select>
      <button v-if="admin" class="btn btn-primary" @click="open()">
        <Icon name="plus" :size="17" />添加交易平台
      </button>
    </div>
  </div>
  <div v-if="admin" class="setup-guide">
    <Icon name="link" :size="21" />
    <div>
      <strong>首次接入：添加平台 → 连接账号 → 测试规则 → 发布版本 → 首次采集</strong>
      <p>
        先选择亿乐或易客，再选择站点版本。已支持的版本会生成默认草稿，账号与规则就绪后启动采集。
      </p>
    </div>
  </div>
  <div v-if="notice" class="notice success" role="status">
    <Icon name="check" :size="18" />{{ notice
    }}<button class="notice-close" aria-label="关闭提示" @click="notice = ''">×</button>
  </div>
  <div v-if="error" class="notice error" role="alert">
    {{ error }}<button class="text-link" @click="load">重试</button>
  </div>
  <div v-if="directoryError && admin" class="notice warning" role="status">
    {{ directoryError }}<button class="text-link" @click="load">刷新目录</button>
  </div>
  <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载平台…</div>
  <div v-else-if="filteredPlatforms.length" class="platform-grid">
    <article v-for="platform in filteredPlatforms" :key="platform.id" class="panel platform-card">
      <div class="platform-card-header">
        <div class="platform-logo"><Icon name="globe" :size="25" /></div>
        <span class="badge" :class="platform.enabled ? 'success' : 'neutral'"
          ><i></i>{{ platform.enabled ? '已启用' : '已停用' }}</span
        >
      </div>
      <h2>{{ platform.name }}</h2>
      <div class="platform-type-tags">
        <span class="badge info">{{ platformTypeName(platform, platformTypes) }}</span>
        <span v-if="!platformSupported(platform, platformTypes)" class="badge warning">
          站点版本待验证
        </span>
      </div>
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
        <span>站点版本</span><strong>{{ platformAdapterName(platform, platformTypes) }}</strong>
      </div>
      <div class="platform-detail">
        <span>采集账号</span><strong>{{ platform.account_label }}</strong>
      </div>
      <div class="platform-detail">
        <span>采集线路</span><strong>{{ proxyLabel(platform.proxy_route) }}</strong>
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
      <div v-if="!platformSupported(platform, platformTypes)" class="platform-hint setup-hint">
        <Icon name="alert" :size="13" />当前版本待完成采集验证，暂未开放账号连接和采集。
      </div>
      <div v-else-if="platform.last_error" class="platform-error">
        <Icon name="alert" :size="14" />{{ platform.last_error }}
      </div>
      <div v-else-if="!platform.active_rule_id" class="platform-hint setup-hint">
        <Icon name="alert" :size="13" />{{
          admin
            ? '连接账号后，请进入规则页完成测试并发布。'
            : '采集规则尚未发布，等待管理员完成配置。'
        }}
      </div>
      <div v-else class="platform-hint">每 {{ platform.request_interval }} 秒最多 1 次请求</div>
      <div v-if="admin" class="platform-actions">
        <button
          class="btn btn-primary btn-sm"
          :disabled="
            collecting === platform.id ||
            !platformSupported(platform, platformTypes) ||
            !platform.enabled ||
            !platform.active_rule_id ||
            platform.auth_status !== 'connected'
          "
          @click="collect(platform)"
        >
          <Icon :name="collecting === platform.id ? 'refresh' : 'play'" :size="14" />{{
            collecting === platform.id ? '提交中…' : '立即采集'
          }}</button
        ><button
          class="btn btn-white btn-sm"
          :disabled="!platformSupported(platform, platformTypes)"
          :title="platformSupported(platform, platformTypes) ? '' : '站点版本待验证'"
          @click="connect(platform)"
        >
          <Icon name="key" :size="14" />{{
            platform.auth_status === 'missing' ? '连接账号' : '更新会话'
          }}</button
        ><RouterLink :to="`/admin/platforms/${platform.id}/rules`" class="btn btn-white btn-sm"
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
      :title="platforms.length ? '该类型还没有平台连接' : '连接第一个交易平台'"
      :description="
        platforms.length
          ? '选择其他平台类型查看，或为当前类型添加平台连接。'
          : '添加平台、配置采集账号，即可开始自动记录全站商品价格。'
      "
      ><button v-if="platforms.length" class="btn btn-white" @click="typeFilter = ''">
        查看全部平台</button
      ><button v-else-if="admin" class="btn btn-primary" @click="open()">
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
      <div class="form-grid">
        <label>
          平台类型
          <select
            v-model="form.platform_type"
            required
            :disabled="identityLocked || saving"
            @change="changePlatformType"
          >
            <option v-for="type in formTypes" :key="type.id" :value="type.id">
              {{ type.name }}
            </option>
          </select>
          <small>按平台系统类型选择，站点名称可单独设置。</small>
        </label>
        <label>
          站点版本
          <select
            v-model="form.adapter"
            required
            :disabled="identityLocked || saving || !formAdapters.length"
            @change="updateAdapterDefault"
          >
            <option v-if="!formAdapters.length" value="">版本目录暂未就绪</option>
            <option v-for="adapter in formAdapters" :key="adapter.id" :value="adapter.id">
              {{ adapter.name
              }}{{ adapter.implemented || adapter.name.includes('待验证') ? '' : ' · 待验证' }}
            </option>
          </select>
          <small>选择与目标网站对应的版本。</small>
        </label>
      </div>
      <p v-if="identityLocked" class="form-help platform-identity-note">
        {{
          checkingIdentity
            ? '正在核对已保存记录，类型与站点版本暂时锁定。'
            : identityCheckFailed
              ? '记录状态未能核对，类型与站点版本暂时锁定；其他配置可继续保存。'
              : '该连接已有商品、会话或已发布规则，平台类型与站点版本已固定。更换类型或版本请新建连接。'
        }}
      </p>
      <div v-if="!selectedSupported" class="notice warning">
        当前站点版本待验证。可先保存平台配置，账号连接和采集将在验证完成后开放。
        <span v-if="selectedAdapter?.note || selectedAdapter?.description">
          {{ selectedAdapter.note || selectedAdapter.description }}
        </span>
      </div>
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
          :placeholder="selectedAdapter?.default_base_url || 'https://请输入目标平台域名'"
      /></label>
      <label
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
      <label>
        采集线路
        <select v-model="form.proxy_route" :disabled="saving">
          <option value="direct">直连 · 使用服务器出口</option>
          <option value="tunnel">隧道代理</option>
          <option value="overseas">境外代理</option>
        </select>
        <small
          >按平台选择出口，代理地址在
          <RouterLink to="/admin/settings/network">系统设置</RouterLink>
          中维护。所选代理未配置或停用时，需要先启用代理。</small
        >
      </label>
      <label class="checkbox-label"
        ><input
          v-model="form.enabled"
          type="checkbox"
          :disabled="!selectedSupported"
        />启用平台自动采集</label
      >
      <div class="modal-actions">
        <button type="button" class="btn btn-white" :disabled="saving" @click="showForm = false">
          取消</button
        ><button class="btn btn-primary" :disabled="saving || !form.adapter">
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
<style scoped>
.platform-tools {
  justify-content: flex-end;
}
.platform-type-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 9px 0;
}
.platform-type-tags .badge {
  font-size: 9px;
}
.platform-detail strong {
  text-align: right;
  overflow-wrap: anywhere;
}
.platform-identity-note {
  margin-top: -4px;
  margin-bottom: 18px;
}
</style>
