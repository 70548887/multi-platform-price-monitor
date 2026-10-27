<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, auth, date, json } from '../api'
import Icon from '../components/Icon.vue'
import Modal from '../components/Modal.vue'
import AIConfigPanel from '../components/AIConfigPanel.vue'
import '../settings.css'

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
const route = useRoute()
const activeSection = ref<'network' | 'ai'>(route.query.section === 'ai' ? 'ai' : 'network')
const feedbackTarget = ref<RouteKey | ''>('')
function isDirty(key: RouteKey) {
  return Boolean(forms[key].url.trim()) || forms[key].enabled !== state.value?.[key].enabled
}
function resetForm(key: RouteKey) {
  if (!state.value || busy.value) return
  forms[key].enabled = state.value[key].enabled
  forms[key].url = ''
  if (feedbackTarget.value === key) {
    error.value = ''
    notice.value = ''
  }
}
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
  if (busy.value || loading.value || !state.value || auth.user?.role !== 'admin') return
  error.value = ''
  notice.value = ''
  feedbackTarget.value = key
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
      notice.value = `${options.find((item) => item.key === key)?.title}已保存。`
    }
  } catch (e: any) {
    if (!disposed) error.value = e.message
  } finally {
    if (!disposed) busy.value = ''
  }
}
async function clearProxy() {
  const key = clearTarget.value
  if (!key || busy.value) return
  busy.value = key
  error.value = ''
  notice.value = ''
  feedbackTarget.value = key
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
  <div v-if="auth.user?.role === 'admin'" class="settings-layout">
    <aside class="settings-sidebar">
      <p class="settings-nav-label">配置中心</p>
      <nav aria-label="系统设置分区" class="settings-nav">
        <button
          type="button"
          :class="{ selected: activeSection === 'network' }"
          :aria-pressed="activeSection === 'network'"
          @click="activeSection = 'network'"
        >
          <Icon name="globe" :size="19" /><span
            ><strong>网络代理</strong><small>采集出口与连接</small></span
          ><Icon name="arrow" :size="15" />
        </button>
        <button
          type="button"
          :class="{ selected: activeSection === 'ai' }"
          :aria-pressed="activeSection === 'ai'"
          @click="activeSection = 'ai'"
        >
          <Icon name="shield" :size="19" /><span
            ><strong>AI 数据验证</strong><small>接口与每日分析</small></span
          ><Icon name="arrow" :size="15" />
        </button>
      </nav>
      <div class="settings-sidebar-note">
        <Icon name="key" :size="17" />
        <p>凭据加密保存<small>已保存的密钥和代理密码不会在页面中显示。</small></p>
      </div>
      <RouterLink to="/admin/settings/update" class="settings-related"
        ><Icon name="download" :size="17" />系统更新<Icon name="external" :size="14"
      /></RouterLink>
    </aside>
    <div class="settings-workspace">
      <section
        v-show="activeSection === 'network'"
        aria-labelledby="network-heading"
        :aria-busy="loading"
        class="settings-section"
      >
        <header class="settings-section-heading">
          <div>
            <span class="settings-kicker">连接设置</span>
            <h2 id="network-heading">网络代理</h2>
            <p>为不同平台配置合适的网络出口，让采集线路更易管理。</p>
          </div>
          <RouterLink to="/admin/platforms" class="btn btn-white"
            ><Icon name="platforms" :size="16" />平台线路管理</RouterLink
          >
        </header>
        <div v-if="error && !feedbackTarget" class="notice error" role="alert">
          {{ error
          }}<button v-if="!state && !loading" type="button" class="text-link" @click="load">
            重新加载
          </button>
        </div>
        <div v-if="loading" class="panel loading-state">
          <span class="spinner"></span>正在读取网络设置…
        </div>
        <template v-else-if="state">
          <div class="settings-context">
            <span class="settings-context-icon"><Icon name="link" :size="18" /></span>
            <p>
              <strong>先配置代理，再为平台选择线路</strong
              ><span>支持直连、隧道代理和境外代理。保存本页后，在平台管理中按需选择。</span>
            </p>
          </div>
          <div class="network-grid">
            <section
              v-for="option in options"
              :key="option.key"
              class="panel network-card"
              :aria-labelledby="`${option.key}-heading`"
            >
              <header class="network-card-heading">
                <span class="settings-icon" :class="{ teal: option.key === 'overseas' }"
                  ><Icon :name="option.icon" :size="21"
                /></span>
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
                <h3 :id="`${option.key}-heading`">{{ option.title }}</h3>
                <p>{{ option.description }}</p>
              </header>
              <form class="network-form" @submit.prevent="save(option.key)">
                <div class="network-form-body">
                  <label class="settings-field" :for="`${option.key}-address`"
                    ><span
                      >{{ state[option.key].configured ? '替换代理地址' : '代理地址'
                      }}<small v-if="state[option.key].configured" class="field-optional"
                        >留空保留</small
                      ></span
                    >
                    <input
                      :id="`${option.key}-address`"
                      v-model="forms[option.key].url"
                      :name="`${option.key}-proxy-address`"
                      type="password"
                      autocomplete="new-password"
                      autocapitalize="off"
                      spellcheck="false"
                      maxlength="2048"
                      :disabled="Boolean(busy)"
                      placeholder="http://用户名:密码@地址:端口"
                      :aria-describedby="`${option.key}-help`"
                    />
                  </label>
                  <p :id="`${option.key}-help`" class="settings-help">
                    支持 HTTP / HTTPS，保存后仅显示配置状态。
                  </p>
                  <label class="settings-toggle-row"
                    ><span
                      ><strong>启用{{ option.title }}</strong
                      ><small>{{
                        state[option.key].configured
                          ? '供选择此线路的平台使用'
                          : '填写代理地址后即可启用'
                      }}</small></span
                    ><span class="settings-switch"
                      ><input
                        v-model="forms[option.key].enabled"
                        type="checkbox"
                        role="switch"
                        :aria-label="`启用${option.title}`"
                        :disabled="Boolean(busy)" /><span aria-hidden="true"></span></span
                  ></label>
                  <div
                    v-if="feedbackTarget === option.key && error"
                    class="notice error"
                    role="alert"
                  >
                    {{ error }}
                  </div>
                  <div
                    v-if="feedbackTarget === option.key && notice"
                    class="notice success"
                    role="status"
                  >
                    <Icon name="check" :size="16" />{{ notice }}
                  </div>
                </div>
                <footer class="settings-form-footer network-actions">
                  <button
                    type="submit"
                    class="btn btn-primary"
                    :disabled="Boolean(busy) || !isDirty(option.key)"
                  >
                    <span v-if="busy === option.key" class="spinner"></span
                    ><Icon v-else name="check" :size="15" />{{
                      busy === option.key ? '保存中…' : '保存配置'
                    }}
                  </button>
                  <button
                    v-if="isDirty(option.key)"
                    type="button"
                    class="text-link"
                    :disabled="Boolean(busy)"
                    @click="resetForm(option.key)"
                  >
                    撤销修改
                  </button>
                  <button
                    v-else
                    type="button"
                    class="text-link danger-text"
                    :disabled="Boolean(busy) || !state[option.key].configured"
                    @click="clearTarget = option.key"
                  >
                    清除代理
                  </button>
                </footer>
              </form>
            </section>
          </div>
          <div class="settings-footnote">
            <Icon name="clock" :size="15" /><span>{{
              state.updated_at ? `最近保存：${date(state.updated_at)}` : '尚未保存代理配置'
            }}</span
            ><span>独立登录窗口使用本机网络</span>
          </div>
        </template>
      </section>
      <AIConfigPanel v-show="activeSection === 'ai'" />
    </div>
    <Modal
      v-if="clearTarget"
      :title="`清除${options.find((item) => item.key === clearTarget)?.title}`"
      @close="!busy && (clearTarget = null)"
    >
      <p>清除后，此代理会停用。使用该线路的平台需要重新配置代理或切换采集线路。</p>
      <div v-if="error && feedbackTarget === clearTarget" class="notice error" role="alert">
        {{ error }}
      </div>
      <div class="modal-actions">
        <button
          type="button"
          class="btn btn-white"
          :disabled="Boolean(busy)"
          @click="clearTarget = null"
        >
          取消</button
        ><button type="button" class="btn btn-danger" :disabled="Boolean(busy)" @click="clearProxy">
          {{ busy ? '处理中…' : '确认清除' }}
        </button>
      </div>
    </Modal>
  </div>
</template>
<style scoped>
.settings-layout {
  display: grid;
  grid-template-columns: 190px minmax(0, 1fr);
  gap: 30px;
  align-items: start;
}
.settings-sidebar {
  position: sticky;
  top: 24px;
}
.settings-nav-label {
  font-size: 11px;
  color: #8c98aa;
  padding: 0 12px;
  margin-bottom: 14px;
  letter-spacing: 1px;
}
.settings-nav {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.settings-nav button {
  display: flex;
  align-items: center;
  gap: 11px;
  width: 100%;
  padding: 15px 12px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: #78859a;
  text-align: left;
}
.settings-nav button.selected {
  color: #3565d9;
  background: #eaf0ff;
  border-color: #dce5fb;
}
.settings-nav button:hover {
  background: #edf2fc;
}
.settings-nav button > span {
  flex: 1;
  min-width: 0;
}
.settings-nav strong,
.settings-nav small {
  display: block;
}
.settings-nav strong {
  font-size: 13px;
  font-weight: 600;
}
.settings-nav small {
  font-size: 11px;
  margin-top: 4px;
  color: #8a97ad;
}
.settings-nav svg {
  flex-shrink: 0;
}
.settings-sidebar-note {
  border-top: 1px solid #e5eaf3;
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 22px 12px 18px;
  margin-top: 22px;
  color: #8796af;
}
.settings-sidebar-note > svg {
  flex-shrink: 0;
  margin-top: 2px;
}
.settings-sidebar-note p {
  font-size: 12px;
  color: #687b95;
}
.settings-sidebar-note small {
  display: block;
  font-size: 11px;
  line-height: 1.8;
  color: #8d99ad;
  margin-top: 6px;
}
.settings-related {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 12px;
  color: #6c7e9a;
  padding: 10px 12px;
}
.settings-related svg:last-child {
  margin-left: auto;
}
.settings-workspace {
  min-width: 0;
}
.network-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
}
.network-card {
  margin: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  border-radius: 12px;
}
.network-card-heading {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 14px 10px;
  padding: 24px 24px 0;
}
.network-card-heading .badge {
  align-self: start;
  margin-top: 7px;
  font-size: 11px;
}
.network-card-heading h3 {
  grid-column: 1/-1;
  font-size: 16px;
  margin-top: 3px;
}
.network-card-heading p {
  grid-column: 1/-1;
  font-size: 12px;
  color: #8290a5;
  line-height: 1.8;
  margin-top: -7px;
}
.network-form {
  display: flex;
  flex-direction: column;
  flex: 1;
}
.network-form-body {
  padding: 22px 24px;
  flex: 1;
}
.network-form-body .settings-toggle-row {
  margin-top: 22px;
}
.network-form-body .notice {
  margin: 16px 0 0;
}
.network-actions {
  justify-content: space-between;
  margin-top: auto;
}
.settings-footnote {
  margin-top: 19px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 9px;
  color: #8b98ad;
  font-size: 11px;
}
.settings-footnote span:last-child {
  margin-left: auto;
}
@media (max-width: 1200px) {
  .settings-layout {
    grid-template-columns: 1fr;
    gap: 22px;
  }
  .settings-sidebar {
    position: static;
    display: flex;
    align-items: center;
    gap: 18px;
    border-bottom: 1px solid #e4eaf4;
    padding-bottom: 16px;
  }
  .settings-nav-label,
  .settings-sidebar-note {
    display: none;
  }
  .settings-nav {
    flex-direction: row;
    flex: 1;
  }
  .settings-nav button {
    width: auto;
    min-width: 165px;
  }
  .settings-nav button > svg:last-child {
    display: none;
  }
  .settings-related {
    margin-left: auto;
  }
}
@media (max-width: 680px) {
  .network-grid {
    grid-template-columns: 1fr;
    gap: 16px;
  }
  .settings-nav {
    gap: 6px;
  }
  .settings-nav button {
    min-width: 0;
    flex: 1;
    padding: 12px 10px;
    gap: 8px;
  }
  .settings-nav small {
    font-size: 10px;
  }
  .settings-related {
    display: none;
  }
  .network-card-heading {
    padding: 20px 20px 0;
  }
  .network-form-body {
    padding: 20px;
  }
  .settings-footnote span:last-child {
    margin-left: 0;
    flex-basis: 100%;
    padding-left: 24px;
  }
}
</style>
