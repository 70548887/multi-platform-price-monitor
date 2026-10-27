<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, auth, json } from '../api'
import Icon from './Icon.vue'
import Modal from './Modal.vue'
const config = ref<any>(null),
  loading = ref(true),
  saving = ref(false),
  error = ref(''),
  notice = ref('')
const form = reactive({
  enabled: false,
  base_url: '',
  model: '',
  key: '',
  daily_time: '09:00',
  proxy_route: 'direct',
  sample_limit: 100,
  json_mode: true,
  daily_product_limit: 1000,
  daily_request_limit: 20,
  auto_apply: false,
})
const clearConfirm = ref(false)
const testing = ref(false),
  diagnostic = ref<any>(null),
  diagnosticError = ref('')
let diagnosticRequest = 0
function invalidateDiagnostic() {
  diagnosticRequest++
  diagnostic.value = null
  diagnosticError.value = ''
}
const canCheckConnection = computed(
  () =>
    Boolean(config.value?.configured) &&
    !loading.value &&
    !saving.value &&
    !testing.value &&
    !dirty.value &&
    auth.user?.role === 'admin',
)
const diagnosticClass = computed(() =>
  diagnostic.value?.status === 'connected'
    ? 'success'
    : diagnostic.value?.status === 'failed'
      ? 'error'
      : 'warning',
)
function configSignature() {
  return JSON.stringify({ config: config.value, form })
}
async function checkConnection() {
  if (!canCheckConnection.value) return
  testing.value = true
  diagnostic.value = null
  diagnosticError.value = ''
  const sequence = ++diagnosticRequest,
    signature = configSignature()
  try {
    const result = await api<any>('/ai-analysis/connection-check', json('POST', {}))
    if (
      disposed ||
      sequence !== diagnosticRequest ||
      dirty.value ||
      signature !== configSignature()
    )
      return
    if (
      typeof config.value?.revision === 'number' &&
      result.config_revision !== config.value.revision
    )
      return
    const models = Array.isArray(result.models)
      ? [
          ...new Set<string>(
            result.models.filter(
              (value: unknown): value is string =>
                typeof value === 'string' &&
                /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$/.test(value) &&
                !/^(?:sk-|github_pat_|ghp_)/i.test(value),
            ),
          ),
        ].slice(0, 200)
      : []
    diagnostic.value = { ...result, models }
  } catch (e: any) {
    if (!disposed && sequence === diagnosticRequest && signature === configSignature())
      diagnosticError.value = e.message
  } finally {
    if (!disposed) testing.value = false
  }
}
const dirty = computed(
  () =>
    Boolean(config.value) &&
    (Boolean(form.key) ||
      Object.keys(form).some(
        (key) => key !== 'key' && form[key as keyof typeof form] !== config.value[key],
      )),
)
function resetForm() {
  if (!config.value || saving.value) return
  invalidateDiagnostic()
  for (const key of Object.keys(form)) {
    if (key !== 'key') (form as Record<string, unknown>)[key] = config.value[key]
  }
  form.key = ''
  error.value = ''
  notice.value = ''
}
let disposed = false
async function load() {
  invalidateDiagnostic()
  loading.value = true
  error.value = ''
  try {
    const value = await api('/ai-analysis/config')
    if (!disposed) {
      config.value = value
      resetFormAfterSave(value)
    }
  } catch (e: any) {
    if (!disposed) {
      config.value = null
      error.value = e.message
    }
  } finally {
    if (!disposed) loading.value = false
  }
}
async function save() {
  if (
    saving.value ||
    testing.value ||
    loading.value ||
    !config.value ||
    auth.user?.role !== 'admin'
  )
    return
  invalidateDiagnostic()
  error.value = ''
  notice.value = ''
  saving.value = true
  try {
    const body = {
      enabled: form.enabled,
      base_url: form.base_url.trim(),
      ...(form.model.trim() ? { model: form.model.trim() } : {}),
      ...(form.key ? { key: form.key } : {}),
      daily_time: form.daily_time,
      proxy_route: form.proxy_route,
      sample_limit: form.sample_limit,
      json_mode: form.json_mode,
      daily_product_limit: form.daily_product_limit,
      daily_request_limit: form.daily_request_limit,
      auto_apply: form.auto_apply,
    }
    const value = await api('/ai-analysis/config', json('PUT', body))
    if (disposed) return
    config.value = value
    resetFormAfterSave(value)
    notice.value = 'AI 配置已保存，密钥不会回显。'
  } catch (e: any) {
    if (!disposed) error.value = e.message
  } finally {
    if (!disposed) saving.value = false
  }
}
async function clearKey() {
  if (saving.value || testing.value || !config.value?.key_configured) return
  invalidateDiagnostic()
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const value = await api('/ai-analysis/config', json('PUT', { clear_key: true }))
    if (disposed) return
    config.value = value
    form.key = ''
    form.enabled = false
    clearConfirm.value = false
    notice.value = 'Key 已清除，每日自动分析已停用。'
  } catch (e: any) {
    if (!disposed) error.value = e.message
  } finally {
    if (!disposed) saving.value = false
  }
}
function resetFormAfterSave(value: any) {
  for (const key of Object.keys(form)) {
    if (key !== 'key' && key in value) (form as Record<string, unknown>)[key] = value[key]
  }
  form.key = ''
}
onMounted(load)
watch(form, invalidateDiagnostic, { deep: true, flush: 'sync' })
onBeforeUnmount(() => {
  disposed = true
  invalidateDiagnostic()
  form.key = ''
})
</script>
<template>
  <section class="ai-settings-section" aria-labelledby="ai-settings-heading" :aria-busy="loading">
    <header class="settings-section-heading">
      <div>
        <span class="settings-kicker">智能分析</span>
        <h2 id="ai-settings-heading">AI 数据验证</h2>
        <p>连接 AI 服务，按计划分析采集数据，生成分类与归总建议。</p>
      </div>
      <RouterLink to="/admin/ai-analysis" class="btn btn-white"
        ><Icon name="chart" :size="16" />查看分析报告</RouterLink
      >
    </header>
    <div v-if="loading" class="panel loading-state">
      <span class="spinner"></span>正在读取 AI 配置…
    </div>
    <div v-else-if="!config" class="panel ai-load-error" role="alert">
      <Icon name="alert" :size="25" />
      <h3>AI 配置暂时未能加载</h3>
      <p>{{ error }}</p>
      <button type="button" class="btn btn-white" @click="load">
        <Icon name="refresh" :size="15" />重新加载
      </button>
    </div>
    <form v-else class="panel ai-settings-panel" @submit.prevent="save">
      <header class="ai-card-heading">
        <span class="settings-icon"><Icon name="shield" :size="22" /></span>
        <div>
          <h3>分析服务配置</h3>
          <p>兼容 OpenAI Chat Completions 接口</p>
        </div>
        <span
          class="badge"
          :class="config.enabled ? 'success' : config.configured ? 'info' : 'neutral'"
          ><i></i
          >{{
            config.enabled ? '每日分析已启用' : config.configured ? '已配置 · 手动分析' : '待配置'
          }}</span
        >
      </header>
      <div class="ai-section">
        <div class="ai-section-title">
          <span>01</span>
          <div>
            <h3>接口连接</h3>
            <p>填写服务商提供的地址、模型和密钥。</p>
          </div>
        </div>
        <div class="ai-config-grid">
          <div>
            <label class="settings-field" for="ai-base-url"
              >接口 Base URL<input
                id="ai-base-url"
                v-model="form.base_url"
                type="url"
                maxlength="500"
                placeholder="https://AI服务域名/v1"
                :disabled="saving"
                aria-describedby="ai-base-help"
            /></label>
            <p id="ai-base-help" class="settings-help">
              填写公开 HTTPS 基础地址，例如 /v1；系统自动拼接 /chat/completions。
            </p>
          </div>
          <div>
            <label class="settings-field" for="ai-model"
              >模型名称<input
                id="ai-model"
                v-model="form.model"
                maxlength="120"
                placeholder="填写服务商支持的模型名称"
                :disabled="saving"
                aria-describedby="ai-model-help"
                list="ai-available-models"
            /></label>
            <datalist id="ai-available-models">
              <option v-for="model in diagnostic?.models ?? []" :key="model" :value="model" />
            </datalist>
            <p id="ai-model-help" class="settings-help">与服务商提供的模型标识保持一致。</p>
          </div>
        </div>
        <div class="ai-key-field">
          <label class="settings-field" for="ai-api-key"
            ><span
              >{{ config.key_configured ? '替换 API Key' : 'API Key'
              }}<small v-if="config.key_configured" class="field-optional">留空保留</small></span
            ><input
              id="ai-api-key"
              v-model="form.key"
              type="password"
              autocomplete="new-password"
              spellcheck="false"
              maxlength="4096"
              placeholder="输入服务商提供的 API Key"
              :disabled="saving"
              aria-describedby="ai-key-help"
          /></label>
          <p id="ai-key-help" class="settings-help">
            <Icon name="key" :size="13" />{{
              config.key_configured
                ? '密钥已加密保存，不会回显。'
                : '尚未配置密钥，填写并保存后即可使用。'
            }}
          </p>
        </div>
        <div class="ai-connection-tools">
          <button
            type="button"
            class="btn btn-white"
            :disabled="!canCheckConnection"
            @click="checkConnection"
          >
            <span v-if="testing" class="spinner"></span><Icon v-else name="link" :size="15" />{{
              testing ? '正在检查连接…' : '检查已保存连接并读取模型'
            }}
          </button>
          <p class="settings-help">
            {{
              dirty
                ? '有未保存的修改，请先保存配置再检查连接。'
                : !config.configured
                  ? '填写并保存接口、模型和 Key 后可检查。'
                  : '使用已保存的配置；模型列表不会自动替换当前模型。'
            }}
          </p>
          <p class="settings-help">
            仅验证连接、鉴权与模型列表，不代表分析可用；不会发送商品数据或产生模型推理请求。
          </p>
        </div>
        <div v-if="diagnosticError" class="notice error ai-diagnostic" role="alert">
          {{ diagnosticError }}
        </div>
        <div v-if="diagnostic" class="notice ai-diagnostic" :class="diagnosticClass" role="status">
          <Icon :name="diagnostic.status === 'connected' ? 'check' : 'alert'" :size="17" />
          <div>
            <strong>{{ diagnostic.message }}</strong>
            <p>
              鉴权：{{
                diagnostic.authenticated === true
                  ? '通过'
                  : diagnostic.authenticated === false
                    ? '未通过'
                    : '未确认'
              }}
              · 当前模型：{{
                diagnostic.model_available === true
                  ? '列表中存在'
                  : diagnostic.model_available === false
                    ? '列表中未找到'
                    : '未确认'
              }}
              · {{ diagnostic.latency_ms ?? '—' }} ms
            </p>
            <p v-if="diagnostic.models.length">
              已读取 {{ diagnostic.models.length }} 个模型{{
                diagnostic.models_truncated ? '（列表有截断）' : ''
              }}，可在模型名称输入框中选择。
            </p>
          </div>
        </div>
      </div>
      <div class="ai-section">
        <div class="ai-section-title">
          <span>02</span>
          <div>
            <h3>分析计划</h3>
            <p>按北京时间执行，每日分析前一天的采集数据。</p>
          </div>
        </div>
        <label class="settings-toggle-row"
          ><span
            ><strong>每日自动分析</strong><small>保存后生效，关闭时仍可手动发起分析</small></span
          ><span class="settings-switch"
            ><input
              v-model="form.enabled"
              type="checkbox"
              role="switch"
              aria-label="启用每日自动分析"
              :disabled="saving" /><span aria-hidden="true"></span></span
        ></label>
        <div class="ai-config-grid ai-schedule-grid">
          <div>
            <label class="settings-field" for="ai-daily-time"
              ><span>每日分析时间</span
              ><input
                id="ai-daily-time"
                v-model="form.daily_time"
                type="time"
                required
                :disabled="saving"
                aria-describedby="ai-time-help"
            /></label>
            <p id="ai-time-help" class="settings-help">北京时间 UTC+8，分析前一个自然日。</p>
          </div>
          <div>
            <label class="settings-field" for="ai-proxy-route"
              >AI 请求线路<select
                id="ai-proxy-route"
                v-model="form.proxy_route"
                :disabled="saving"
                aria-describedby="ai-route-help"
              >
                <option value="direct">直连</option>
                <option value="overseas">境外代理</option>
              </select></label
            >
            <p id="ai-route-help" class="settings-help">境外线路使用本系统已启用的境外代理。</p>
          </div>
        </div>
      </div>
      <div class="ai-section">
        <div class="ai-section-title">
          <span>03</span>
          <div>
            <h3>分析范围与输出</h3>
            <p>全量统计独立核对，AI 按变动优先与轮巡规则分批检查。</p>
          </div>
        </div>
        <div class="ai-config-grid">
          <div>
            <label class="settings-field" for="ai-daily-product-limit"
              >每日商品名额<input
                id="ai-daily-product-limit"
                v-model.number="form.daily_product_limit"
                type="number"
                min="1"
                max="1000"
                required
                :disabled="saving"
            /></label>
            <p class="settings-help">最多 1,000 件；手动、定时和重试共享，失败请求占用名额。</p>
          </div>
          <div>
            <label class="settings-field" for="ai-daily-request-limit"
              >每日模型请求上限<input
                id="ai-daily-request-limit"
                v-model.number="form.daily_request_limit"
                type="number"
                min="1"
                max="20"
                required
                :disabled="saving"
            /></label>
            <p class="settings-help">最多 20 次，每批最多 50 件；失败请求也计入。</p>
          </div>
        </div>
        <label class="settings-toggle-row ai-automation-toggle"
          ><span
            ><strong>符合规则自动应用</strong
            ><small
              >在已有类目内创建统一商品或增量关联未归属商品；保留人工关联。不满足规则的建议进入待审核。</small
            ></span
          ><span class="settings-switch"
            ><input
              v-model="form.auto_apply"
              type="checkbox"
              role="switch"
              aria-label="符合规则自动应用"
              :disabled="saving" /><span aria-hidden="true"></span></span
        ></label>
        <div class="ai-config-grid ai-preview-grid">
          <div>
            <label class="settings-field" for="ai-sample-limit"
              >程序核对预览样本<input
                id="ai-sample-limit"
                v-model.number="form.sample_limit"
                type="number"
                min="10"
                max="200"
                step="1"
                required
                :disabled="saving"
                aria-describedby="ai-sample-help"
            /></label>
            <p id="ai-sample-help" class="settings-help">
              仅控制“程序核对”的本地预览，10–200 件；不改变 AI 每批 50 件的上限。
            </p>
          </div>
          <div class="ai-output-field">
            <label class="settings-field ai-output-caption" for="ai-json-mode">输出格式</label>
            <label class="settings-toggle-row ai-json-toggle"
              ><strong>JSON 结构化输出</strong
              ><span class="settings-switch"
                ><input
                  id="ai-json-mode"
                  v-model="form.json_mode"
                  type="checkbox"
                  role="switch"
                  aria-label="请求 JSON 模式"
                  aria-describedby="ai-json-help"
                  :disabled="saving" /><span aria-hidden="true"></span></span
            ></label>
            <p id="ai-json-help" class="settings-help">接口不支持 JSON 模式时可关闭。</p>
          </div>
        </div>
        <details class="ai-data-note">
          <summary><Icon name="shield" :size="15" />数据使用与建议处理</summary>
          <p>
            启用后，将商品名称、源类目、价格、批次计数和统计摘要发送给所配置的 AI
            服务；不会发送平台账号、Cookie、会话或代理密码。开启自动应用后，仅满足规则的建议会整理未关联商品；其他建议待管理员审核。原始价格、源类目和人工关联保留，变更可在处理记录中核对与撤销。
          </p>
        </details>
      </div>
      <div v-if="error" class="notice error ai-feedback" role="alert">{{ error }}</div>
      <div v-if="notice" class="notice success ai-feedback" role="status">
        <Icon name="check" :size="16" />{{ notice }}
      </div>
      <footer class="settings-form-footer ai-panel-actions">
        <button type="submit" class="btn btn-primary" :disabled="saving || testing || !dirty">
          <span v-if="saving" class="spinner"></span><Icon v-else name="check" :size="15" />{{
            saving ? '保存中…' : '保存 AI 配置'
          }}</button
        ><button v-if="dirty" type="button" class="text-link" :disabled="saving" @click="resetForm">
          撤销修改</button
        ><span v-if="dirty" class="settings-dirty">有未保存的修改</span
        ><button
          type="button"
          class="text-link danger-text clear-key-button"
          :disabled="saving || testing || !config.key_configured"
          @click="clearConfirm = true"
        >
          清除密钥
        </button>
      </footer>
    </form>
    <Modal v-if="clearConfirm" title="清除 AI 密钥" @close="!saving && (clearConfirm = false)"
      ><p>清除后每日自动分析将停用。再次使用 AI 分析时，需要重新填写 API Key。</p>
      <div v-if="error" class="notice error" role="alert">{{ error }}</div>
      <div class="modal-actions">
        <button
          type="button"
          class="btn btn-white"
          :disabled="saving"
          @click="clearConfirm = false"
        >
          取消</button
        ><button type="button" class="btn btn-danger" :disabled="saving" @click="clearKey">
          {{ saving ? '处理中…' : '清除并停用' }}
        </button>
      </div></Modal
    >
  </section>
</template>
<style scoped>
.ai-settings-panel {
  border-radius: 12px;
  margin-bottom: 0;
}
.ai-card-heading {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr) auto;
  align-items: center;
  gap: 13px;
  padding: 24px 28px;
  border-bottom: 1px solid #edf1f7;
}
.ai-card-heading h3 {
  font-size: 15px;
}
.ai-card-heading p {
  font-size: 12px;
  color: #70819a;
  margin-top: 5px;
}
.ai-card-heading .badge {
  justify-self: end;
  font-size: 11px;
}
.ai-section {
  padding: 25px 28px;
  border-bottom: 1px solid #f0f3f8;
}
.ai-section-title {
  display: flex;
  gap: 11px;
  margin-bottom: 22px;
  align-items: baseline;
}
.ai-section-title > span {
  font-size: 11px;
  font-weight: 600;
  color: #8c9eba;
  font-variant-numeric: tabular-nums;
}
.ai-section-title h3 {
  font-size: 14px;
  color: #475a77;
}
.ai-section-title p {
  color: #71829a;
  font-size: 12px;
  margin-top: 5px;
}
.ai-config-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 20px;
}
.ai-config-grid > * {
  min-width: 0;
}
.ai-key-field {
  margin-top: 22px;
}
.ai-connection-tools {
  margin-top: 20px;
}
.ai-connection-tools .btn {
  white-space: normal;
  text-align: left;
}
.ai-connection-tools .spinner {
  width: 14px;
  height: 14px;
}
.ai-diagnostic {
  margin: 16px 0 0;
  align-items: flex-start;
}
.ai-diagnostic > svg {
  margin-top: 2px;
}
.ai-diagnostic > div {
  min-width: 0;
}
.ai-diagnostic strong {
  font-size: 12px;
}
.ai-diagnostic p {
  font-size: 11px;
  line-height: 1.8;
  margin-top: 5px;
}
.ai-key-field .settings-help {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ai-schedule-grid {
  margin-top: 22px;
}
.ai-automation-toggle {
  margin-top: 22px;
}
.ai-preview-grid {
  margin-top: 22px;
}
.ai-output-caption {
  margin-bottom: 9px;
}
.ai-output-field .ai-json-toggle {
  height: 44px;
  padding: 10px 12px;
  border-color: #dfe5ef;
  background: #fff;
}
.ai-data-note {
  margin-top: 24px;
  color: #7f90aa;
  font-size: 12px;
  line-height: 1.9;
}
.ai-data-note summary {
  cursor: pointer;
  color: #7487a4;
}
.ai-data-note summary svg {
  vertical-align: middle;
  margin-right: 6px;
}
.ai-data-note p {
  padding: 12px 16px;
  background: #f7f9fc;
  border-radius: 8px;
  margin-top: 10px;
}
.ai-feedback {
  margin: 18px 28px;
}
.clear-key-button {
  margin-left: auto;
}
.ai-load-error {
  padding: 38px 24px;
  text-align: center;
  color: #8a9bb5;
}
.ai-load-error h3 {
  color: #5b6e8a;
  font-size: 15px;
  margin: 12px 0 8px;
}
.ai-load-error p {
  font-size: 12px;
  margin-bottom: 20px;
}
@media (max-width: 680px) {
  .ai-config-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 18px;
  }
  .ai-card-heading {
    grid-template-columns: 44px minmax(0, 1fr);
    gap: 10px 13px;
    padding: 20px;
  }
  .ai-card-heading .badge {
    grid-column: 2;
    justify-self: start;
  }
  .ai-section {
    padding: 22px 20px;
  }
  .ai-feedback {
    margin: 16px 20px;
  }
  .ai-panel-actions {
    gap: 14px;
  }
  .settings-dirty {
    display: none;
  }
}
</style>
