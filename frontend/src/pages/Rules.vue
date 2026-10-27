<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { api, itemsOf, json, date, price, displayBasisLabel, qualityText } from '../api'
import { platformSupported } from '../platformTypes'
import Icon from '../components/Icon.vue'
import Badge from '../components/Badge.vue'
import EmptyState from '../components/EmptyState.vue'
import Modal from '../components/Modal.vue'
const route = useRoute(),
  platformId = route.params.id,
  rules = ref<any[]>([]),
  platform = ref<any>(null),
  selected = ref<any>(null),
  loading = ref(true),
  error = ref(''),
  notice = ref(''),
  busy = ref(false),
  showNew = ref(false),
  configText = ref(''),
  notes = ref(''),
  formError = ref(''),
  testSource = ref(''),
  testResult = ref<any>(null),
  testing = ref(false)
const selectedConfig = computed(() =>
  selected.value ? JSON.stringify(selected.value.config, null, 2) : '',
)
const supported = computed(() => platformSupported(platform.value))
async function load() {
  loading.value = true
  error.value = ''
  try {
    const [list, p] = await Promise.all([api(`/platforms/${platformId}/rules`), api('/platforms')])
    rules.value = itemsOf(list)
    platform.value = itemsOf(p).find((i: any) => String(i.id) === String(platformId))
    selected.value =
      rules.value.find((i) => i.id === selected.value?.id) ??
      rules.value.find((i) => i.id === platform.value?.active_rule_id) ??
      rules.value[0] ??
      null
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
onMounted(load)
async function newRule() {
  showNew.value = true
  formError.value = ''
  notes.value = ''
  busy.value = true
  try {
    const result = selected.value?.config ?? (await api(`/platforms/${platformId}/default-rule`))
    configText.value = JSON.stringify(result.config ?? result, null, 2)
  } catch (e: any) {
    formError.value = e.message
    configText.value = '{}'
  } finally {
    busy.value = false
  }
}
async function save() {
  formError.value = ''
  let config
  try {
    config = JSON.parse(configText.value)
    if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error()
  } catch {
    formError.value = '规则必须是有效的 JSON 对象，请检查引号、逗号和括号。'
    return
  }
  busy.value = true
  try {
    const result = await api(
      `/platforms/${platformId}/rules`,
      json('POST', { config, notes: notes.value }),
    )
    selected.value = result
    showNew.value = false
    testResult.value = null
    notice.value = '草稿版本已保存，请测试确认后发布。'
    await load()
  } catch (e: any) {
    formError.value = e.message
  } finally {
    busy.value = false
  }
}
async function test() {
  if (!selected.value || !supported.value) return
  const rule = selected.value
  testing.value = true
  error.value = ''
  testResult.value = null
  try {
    const result = await api(
      `/rules/${rule.id}/test`,
      json('POST', testSource.value.trim() ? { source_product_id: testSource.value.trim() } : {}),
    )
    rule.test_ok = result.ok
    if (selected.value?.id === rule.id) testResult.value = result
  } catch (e: any) {
    error.value = e.message
  } finally {
    testing.value = false
  }
}
async function publish() {
  if (!selected.value || !supported.value) return
  busy.value = true
  error.value = ''
  try {
    await api(`/rules/${selected.value.id}/publish`, json('POST'))
    notice.value = `规则 v${selected.value.version} 已发布，后续采集使用此版本。`
    await load()
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
function choose(rule: any) {
  selected.value = rule
  testResult.value = null
}
</script>
<template>
  <RouterLink to="/admin/platforms" class="back-link"
    ><Icon name="back" :size="16" />返回平台管理<span v-if="platform">
      / {{ platform.name }}</span
    ></RouterLink
  >
  <div class="page-tools">
    <div class="section-description">已发布规则参与正式采集。每次修改创建新版本，可随时回滚。</div>
    <button class="btn btn-primary" :disabled="testing || busy" @click="newRule">
      <Icon name="plus" :size="16" />新建规则版本
    </button>
  </div>
  <div v-if="notice" class="notice success">
    {{ notice
    }}<RouterLink v-if="platform?.active_rule_id" to="/admin/platforms" class="text-link"
      >返回平台采集 →</RouterLink
    >
  </div>
  <div v-if="error" class="notice error">{{ error }}</div>
  <div v-if="platform && !supported" class="notice warning">
    当前站点版本待验证，可管理规则草稿；采集测试与发布将在版本验证完成后开放。
  </div>
  <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载规则版本…</div>
  <template v-else-if="rules.length"
    ><div v-if="platform && supported && !platform.active_rule_id" class="setup-guide">
      <Icon name="file" :size="21" />
      <div>
        <strong>首次接入：连接账号 → 测试默认草稿 → 发布规则 → 开始采集</strong>
        <p>
          {{
            platform.auth_status === 'connected'
              ? '登录会话已连接。平台已生成默认草稿，可直接点击“运行测试”，通过后点击“发布此版本”。'
              : '平台已自动生成默认草稿，请先在平台管理中连接采集账号，再测试并发布此规则。'
          }}
        </p>
      </div>
      <RouterLink
        v-if="platform.auth_status !== 'connected'"
        to="/admin/platforms"
        class="btn btn-white btn-sm"
        >连接账号</RouterLink
      >
    </div>
    <div class="rules-layout">
      <section class="panel rule-list">
        <div class="panel-heading">
          <h2>版本历史</h2>
          <span class="subtle-tag">{{ rules.length }}</span>
        </div>
        <button
          v-for="rule in rules"
          :key="rule.id"
          class="rule-version"
          :disabled="testing || busy"
          :class="{ selected: selected?.id === rule.id }"
          @click="choose(rule)"
        >
          <div>
            <strong>v{{ rule.version }}</strong
            ><Badge :value="platform?.active_rule_id === rule.id ? 'published' : rule.status" />
          </div>
          <p>{{ rule.notes || '无版本备注' }}</p>
          <time>{{ date(rule.created_at, true) }}</time>
        </button>
      </section>
      <div v-if="selected" class="rule-main">
        <section class="panel">
          <div class="panel-heading">
            <div>
              <h2>
                规则配置 <span class="muted">v{{ selected.version }}</span>
              </h2>
              <p>
                {{ selected.notes || '采集字段与请求配置' }} ·
                {{ selected.test_ok ? '测试已通过' : '发布前需测试通过' }}
              </p>
            </div>
            <button
              class="btn btn-primary btn-sm"
              :disabled="
                busy ||
                testing ||
                !supported ||
                !selected.test_ok ||
                selected.id === platform?.active_rule_id
              "
              :title="
                !supported
                  ? '站点版本待验证'
                  : !selected.test_ok
                    ? '请先运行测试，成功后即可发布'
                    : ''
              "
              @click="publish"
            >
              {{
                busy
                  ? '发布中…'
                  : selected.id === platform?.active_rule_id
                    ? '当前生效'
                    : selected.status === 'draft'
                      ? '发布此版本'
                      : '回滚至此版本'
              }}
            </button>
          </div>
          <pre class="json-viewer">{{ selectedConfig }}</pre>
        </section>
        <section class="panel">
          <div class="panel-heading">
            <div>
              <h2>采集测试</h2>
              <p>使用当前会话验证规则，测试结果不计入正式价格历史</p>
            </div>
          </div>
          <div class="panel-body">
            <div class="test-controls">
              <input
                v-model="testSource"
                placeholder="商品编号（可选，例如 9585）"
                aria-label="测试商品编号"
                :disabled="testing || busy"
              /><button
                class="btn btn-white"
                :disabled="testing || busy || !supported || platform?.auth_status !== 'connected'"
                :title="supported ? '' : '站点版本待验证'"
                @click="test"
              >
                <Icon :name="testing ? 'refresh' : 'play'" :size="15" />{{
                  testing ? '测试中…' : '运行测试'
                }}
              </button>
            </div>
            <div v-if="testResult" class="test-result">
              <div class="notice" :class="testResult.ok ? 'success' : 'error'">
                <Icon :name="testResult.ok ? 'check' : 'alert'" :size="17" />{{
                  testResult.ok ? '规则测试通过' : '规则测试未通过'
                }}
                · {{ testResult.products?.length ?? 0 }} 个预览商品
              </div>
              <div v-if="testResult.errors?.length" class="notice error">
                <p v-for="(e, i) in testResult.errors" :key="i">
                  {{ typeof e === 'string' ? e : JSON.stringify(e) }}
                </p>
              </div>
              <div v-if="testResult.products?.length" class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>商品编号 / 名称</th>
                      <th>基础单价</th>
                      <th>展示数量</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(p, i) in testResult.products" :key="i">
                      <td>
                        <strong>{{ p.name }}</strong>
                        <div class="table-secondary">#{{ p.source_id }}</div>
                        <div v-if="p.quality?.length" class="quality-warning">
                          {{ qualityText(p.quality) }}
                        </div>
                      </td>
                      <td class="mono">{{ price(p.price, p.currency) }}</td>
                      <td>{{ displayBasisLabel(p.display_quantity, p.unit) }}</td>
                      <td><Badge :value="p.status" /></td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </section>
      </div></div
  ></template>
  <section v-else class="panel">
    <EmptyState
      icon="file"
      title="还没有采集规则"
      description="从平台默认规则创建第一个版本，运行测试后发布。"
      ><button class="btn btn-primary" :disabled="testing || busy" @click="newRule">
        创建首个版本
      </button></EmptyState
    >
  </section>
  <Modal v-if="showNew" title="新建采集规则版本" wide @close="!busy && (showNew = false)"
    ><form @submit.prevent="save">
      <div v-if="formError" class="notice error">{{ formError }}</div>
      <label
        >版本备注<input
          v-model="notes"
          maxlength="500"
          placeholder="说明本次字段、分页或分类调整" /></label
      ><label
        >规则 JSON<textarea
          v-model="configText"
          rows="19"
          class="code-input"
          spellcheck="false"
          :disabled="busy"
        ></textarea>
      </label>
      <p class="form-help">基于当前版本或适配器默认规则创建。保存后先测试，再发布到正式采集。</p>
      <div class="modal-actions">
        <button type="button" class="btn btn-white" :disabled="busy" @click="showNew = false">
          取消</button
        ><button class="btn btn-primary" :disabled="busy">
          {{ busy ? '处理中…' : '保存草稿' }}
        </button>
      </div>
    </form></Modal
  >
</template>
