<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api, auth, date, json } from '../api'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'

type RouteKey = 'tunnel' | 'overseas'
interface ProxyState {
  enabled: boolean
  configured: boolean
}
interface NetworkConfig {
  tunnel: ProxyState
  overseas: ProxyState
  updated_at?: string | null
}
const options: { key: RouteKey; title: string; description: string; icon: string }[] = [
  {
    key: 'tunnel',
    title: '隧道代理',
    description: '为需要隧道出口的平台预留，配置后可按需启用。',
    icon: 'link',
  },
  {
    key: 'overseas',
    title: '境外代理',
    description: '为需要境外出口的平台配置独立代理。',
    icon: 'globe',
  },
]
const state = ref<NetworkConfig | null>(null)
const forms = reactive({
  tunnel: { enabled: false, url: '' },
  overseas: { enabled: false, url: '' },
})
const loading = ref(true)
const busy = ref<RouteKey | ''>('')
const error = ref('')
const notice = ref('')
const clearTarget = ref<RouteKey | null>(null)
let disposed = false
let controller: AbortController | undefined
function apply(value: NetworkConfig) {
  state.value = value
  for (const option of options) {
    forms[option.key].enabled = value[option.key].enabled
    forms[option.key].url = ''
  }
}
async function load() {
  if (auth.user?.role !== 'admin') {
    loading.value = false
    return
  }
  loading.value = true
  error.value = ''
  controller = new AbortController()
  try {
    const result = await api<NetworkConfig>('/settings/network', { signal: controller.signal })
    if (!disposed) apply(result)
  } catch (e: any) {
    if (!disposed) error.value = e.message
  } finally {
    if (!disposed) loading.value = false
  }
}
function validProxy(value: string) {
  try {
    const proxy = new URL(value)
    return (
      ['http:', 'https:'].includes(proxy.protocol) &&
      Boolean(proxy.hostname) &&
      !proxy.search &&
      !proxy.hash &&
      ['', '/'].includes(proxy.pathname)
    )
  } catch {
    return false
  }
}
async function save(key: RouteKey) {
  if (busy.value || !state.value) return
  error.value = ''
  notice.value = ''
  const form = forms[key]
  const address = form.url.trim()
  if (address && !validProxy(address)) {
    error.value = '请填写有效的 HTTP 或 HTTPS 代理地址，不包含额外路径、查询参数或片段。'
    return
  }
  if (form.enabled && !state.value[key].configured && !address) {
    error.value = '启用前需要填写代理地址。'
    return
  }
  busy.value = key
  controller = new AbortController()
  try {
    const payload = { [key]: { enabled: form.enabled, ...(address ? { url: address } : {}) } }
    const result = await api<NetworkConfig>('/settings/network', {
      ...json('PUT', payload),
      signal: controller.signal,
    })
    if (!disposed) {
      // Only clear the saved form, preserving unsaved changes in the other card.
      state.value = result
      forms[key].url = ''
      forms[key].enabled = result[key].enabled
      notice.value = `${options.find((item) => item.key === key)?.title}已保存。请在平台管理中选择相应采集线路。`
    }
  } catch (e: any) {
    if (!disposed) error.value = e.message
  } finally {
    forms[key].url = ''
    if (!disposed) busy.value = ''
  }
}
async function clearProxy() {
  const key = clearTarget.value
  if (!key || busy.value) return
  busy.value = key
  error.value = ''
  notice.value = ''
  controller = new AbortController()
  try {
    const result = await api<NetworkConfig>('/settings/network', {
      ...json('PUT', { [key]: { clear: true } }),
      signal: controller.signal,
    })
    if (!disposed) {
      state.value = result
      forms[key].enabled = false
      forms[key].url = ''
      clearTarget.value = null
      notice.value = '代理已清除并停用，请核对使用该线路的平台配置。'
    }
  } catch (e: any) {
    if (!disposed) error.value = e.message
  } finally {
    if (!disposed) busy.value = ''
  }
}
onMounted(load)
onBeforeUnmount(() => {
  disposed = true
  controller?.abort()
  forms.tunnel.url = ''
  forms.overseas.url = ''
})
</script>

<template>
  <template v-if="auth.user?.role === 'admin'">
    <div class="page-tools">
      <div class="section-description">
        分别维护两类代理，并为每个平台选择直连、隧道代理或境外代理。独立登录窗口使用本机网络，服务器按所选线路校验会话并采集。
      </div>
      <RouterLink to="/platforms" class="btn btn-white"
        ><Icon name="platforms" :size="16" />平台管理</RouterLink
      >
    </div>
    <div v-if="error" class="notice error" role="alert">
      {{ error }}<button v-if="!state && !loading" class="text-link" @click="load">重新加载</button>
    </div>
    <div v-if="notice" class="notice success" role="status">{{ notice }}</div>
    <div v-if="loading" class="loading-state"><span class="spinner"></span>正在读取网络设置…</div>
    <template v-else-if="state">
      <div class="network-grid">
        <section v-for="option in options" :key="option.key" class="panel network-card">
          <div class="panel-heading">
            <div class="network-title">
              <span class="network-icon"><Icon :name="option.icon" /></span>
              <div>
                <h2>{{ option.title }}</h2>
                <p>{{ option.description }}</p>
              </div>
            </div>
            <span
              class="badge"
              :class="
                state[option.key].enabled
                  ? 'success'
                  : state[option.key].configured
                    ? 'info'
                    : 'neutral'
              "
              ><i></i
              >{{
                state[option.key].enabled
                  ? '已启用'
                  : state[option.key].configured
                    ? '已配置 · 停用'
                    : '未配置'
              }}</span
            >
          </div>
          <form class="network-form" @submit.prevent="save(option.key)">
            <label>
              {{ state[option.key].configured ? '替换代理地址（留空保留）' : '代理地址' }}
              <input
                v-model="forms[option.key].url"
                :name="`${option.key}-proxy-address`"
                type="password"
                autocomplete="new-password"
                autocapitalize="off"
                spellcheck="false"
                maxlength="2048"
                :disabled="Boolean(busy)"
                placeholder="http://用户名:密码@代理地址:端口"
              />
              <small>支持 HTTP / HTTPS；保存后只显示配置状态，代理地址和凭据不会回显。</small>
            </label>
            <label class="checkbox-label"
              ><input
                v-model="forms[option.key].enabled"
                type="checkbox"
                :disabled="Boolean(busy)"
              />启用{{ option.title }}</label
            >
            <p class="form-help">
              平台选择此线路后使用该代理。停用或清除前，请先核对使用此线路的平台。
            </p>
            <div class="network-actions">
              <button class="btn btn-primary" :disabled="Boolean(busy)">
                <Icon name="check" :size="15" />{{ busy === option.key ? '处理中…' : '保存配置' }}
              </button>
              <button
                type="button"
                class="text-link danger-text"
                :disabled="Boolean(busy) || !state[option.key].configured"
                @click="clearTarget = option.key"
              >
                清除代理
              </button>
            </div>
          </form>
        </section>
      </div>
      <div class="page-note">
        <Icon
          name="shield"
          :size="15"
        />代理凭据加密保存。保存网络设置后，可在平台管理中单独选择采集线路。<span
          v-if="state.updated_at"
          >最近保存：{{ date(state.updated_at) }}</span
        >
      </div>
    </template>
    <Modal
      v-if="clearTarget"
      :title="`清除${options.find((item) => item.key === clearTarget)?.title}`"
      @close="!busy && (clearTarget = null)"
    >
      <p>清除后，此代理会停用。使用该线路的平台需要重新配置代理或切换采集线路。</p>
      <div class="modal-actions">
        <button class="btn btn-white" :disabled="Boolean(busy)" @click="clearTarget = null">
          取消</button
        ><button class="btn btn-primary" :disabled="Boolean(busy)" @click="clearProxy">
          {{ busy ? '处理中…' : '确认清除' }}
        </button>
      </div>
    </Modal>
  </template>
</template>

<style scoped>
.network-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
}
.network-card {
  min-width: 0;
}
.network-card .panel-heading {
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 14px;
}
.network-title {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  flex: 1;
  min-width: 180px;
}
.network-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background: #eef2ff;
  color: #4f46e5;
}
.network-form {
  padding: 24px;
}
.network-form small {
  overflow-wrap: anywhere;
}
.network-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 24px;
}
.network-actions button {
  white-space: nowrap;
}
.page-note {
  flex-wrap: wrap;
}
@media (max-width: 1000px) {
  .network-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 600px) {
  .network-form {
    padding: 18px;
  }
  .network-actions {
    flex-wrap: wrap;
  }
}
</style>
