<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { api, itemsOf, json, params, auth, price, date } from '../api'
import Icon from '../components/Icon.vue'
import Badge from '../components/Badge.vue'
import EmptyState from '../components/EmptyState.vue'
import Modal from '../components/Modal.vue'
const groups = ref<any[]>([]),
  loading = ref(true),
  error = ref(''),
  notice = ref(''),
  showForm = ref(false),
  editing = ref<any>(null),
  saving = ref(false),
  formError = ref(''),
  search = ref(''),
  candidates = ref<any[]>([]),
  picked = ref<any[]>([]),
  searching = ref(false),
  comparison = ref<any>(null),
  compareGroup = ref<any>(null),
  deleteGroup = ref<any>(null),
  admin = computed(() => auth.user?.role === 'admin')
const form = reactive({ name: '', unit: '', currency: 'CNY' })
let timer: ReturnType<typeof setTimeout>,
  searchId = 0
async function load() {
  loading.value = true
  error.value = ''
  try {
    groups.value = itemsOf(await api('/mappings'))
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
onMounted(load)
async function find() {
  const id = ++searchId
  searching.value = true
  try {
    const result = await api(`/products${params({ q: search.value, page_size: 30 })}`)
    if (id === searchId) candidates.value = itemsOf(result)
  } catch (e: any) {
    formError.value = e.message
  } finally {
    if (id === searchId) searching.value = false
  }
}
watch(search, () => {
  clearTimeout(timer)
  timer = setTimeout(find, 250)
})
function open(group?: any) {
  editing.value = group ?? null
  Object.assign(form, {
    name: group?.name ?? '',
    unit: group?.unit ?? '',
    currency: group?.currency ?? 'CNY',
  })
  picked.value = [...(group?.products ?? [])]
  formError.value = ''
  search.value = ''
  candidates.value = []
  showForm.value = true
  find()
}
function toggle(product: any) {
  const index = picked.value.findIndex((p) => p.id === product.id)
  if (index >= 0) picked.value.splice(index, 1)
  else picked.value.push(product)
}
async function save() {
  saving.value = true
  formError.value = ''
  try {
    await api(
      editing.value ? `/mappings/${editing.value.id}` : '/mappings',
      json(editing.value ? 'PUT' : 'POST', { ...form, product_ids: picked.value.map((p) => p.id) }),
    )
    showForm.value = false
    notice.value = '统一商品映射已保存。'
    await load()
  } catch (e: any) {
    formError.value = e.message
  } finally {
    saving.value = false
  }
}
async function compare(group: any) {
  compareGroup.value = group
  comparison.value = null
  try {
    comparison.value = await api(`/mappings/${group.id}/compare`)
  } catch (e: any) {
    comparison.value = { error: e.message, items: [] }
  }
}
async function remove() {
  saving.value = true
  formError.value = ''
  try {
    await api(`/mappings/${deleteGroup.value.id}`, json('DELETE'))
    deleteGroup.value = null
    notice.value = '映射组已删除，原商品与价格历史保留。'
    await load()
  } catch (e: any) {
    formError.value = e.message
  } finally {
    saving.value = false
  }
}
function confirmRemove(group: any) {
  deleteGroup.value = group
  formError.value = ''
}
</script>
<template>
  <div class="page-tools">
    <div class="section-description">人工确认相同商品及规格，统一单位、币种后再比较价格。</div>
    <button v-if="admin" class="btn btn-primary" @click="open()">
      <Icon name="plus" :size="17" />创建统一商品
    </button>
  </div>
  <div v-if="error" class="notice error">{{ error }}</div>
  <div v-if="notice" class="notice success">{{ notice }}</div>
  <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载映射…</div>
  <div v-else-if="groups.length" class="mapping-grid">
    <section v-for="group in groups" :key="group.id" class="panel mapping-card">
      <div class="mapping-card-heading">
        <div class="mapping-symbol"><Icon name="link" :size="21" /></div>
        <div>
          <h2>{{ group.name }}</h2>
          <p>
            {{ group.currency }} · {{ group.unit
            }}<span class="dot-separator"
              >{{ group.products?.length ?? group.product_ids?.length ?? 0 }} 个平台商品</span
            >
          </p>
        </div>
      </div>
      <div class="mapping-products">
        <div v-for="p in group.products" :key="p.id">
          <div>
            <RouterLink :to="`/products/${p.id}`">{{ p.name }}</RouterLink
            ><small>{{ p.platform_name }} · #{{ p.source_id }}</small>
          </div>
          <strong class="mono">{{ price(p.price, p.currency) }}</strong>
        </div>
        <p v-if="!group.products?.length" class="muted">尚未关联平台商品</p>
      </div>
      <div class="mapping-actions">
        <button class="btn btn-primary btn-sm" @click="compare(group)">
          <Icon name="chart" :size="14" />查看比价</button
        ><button v-if="admin" class="btn btn-white btn-sm" @click="open(group)">编辑映射</button
        ><button v-if="admin" class="text-link danger-text" @click="confirmRemove(group)">
          删除
        </button>
      </div>
    </section>
  </div>
  <section v-else class="panel">
    <EmptyState
      icon="link"
      title="建立第一个跨平台商品映射"
      description="选择不同平台中相同规格的商品，建立可持续追踪的比价关系。"
      ><button v-if="admin" class="btn btn-primary btn-sm" @click="open()">
        创建统一商品
      </button></EmptyState
    >
  </section>
  <Modal
    v-if="showForm"
    :title="editing ? '编辑统一商品' : '创建统一商品'"
    wide
    @close="!saving && (showForm = false)"
    ><form @submit.prevent="save">
      <div v-if="formError" class="notice error">{{ formError }}</div>
      <label
        >统一商品名称<input
          v-model="form.name"
          required
          maxlength="200"
          placeholder="明确商品规格、品质、区服等条件"
      /></label>
      <div class="form-grid">
        <label
          >计价单位<input
            v-model="form.unit"
            required
            maxlength="50"
            placeholder="例如：人、月、件" /></label
        ><label
          >币种<input
            v-model="form.currency"
            required
            minlength="3"
            maxlength="3"
            placeholder="CNY"
            @input="form.currency = form.currency.toUpperCase()"
        /></label>
      </div>
      <label
        >关联平台商品<span class="muted">已选择 {{ picked.length }} 个</span></label
      >
      <div v-if="picked.length" class="picked-products">
        <button v-for="p in picked" :key="p.id" type="button" @click="toggle(p)">
          {{ p.platform_name }} · {{ p.name }}<Icon name="close" :size="13" />
        </button>
      </div>
      <div class="search-input full">
        <Icon name="search" :size="16" /><input
          v-model="search"
          placeholder="搜索并选择同规格商品"
          aria-label="搜索关联商品"
        />
      </div>
      <div class="product-picker">
        <p v-if="searching" class="muted">正在搜索…</p>
        <button
          v-for="p in candidates"
          v-else
          :key="p.id"
          type="button"
          class="picker-item"
          :class="{ selected: picked.some((x) => x.id === p.id) }"
          @click="toggle(p)"
        >
          <span class="picker-check"
            ><Icon v-if="picked.some((x) => x.id === p.id)" name="check" :size="13"
          /></span>
          <div>
            <strong>{{ p.name }}</strong
            ><small
              >{{ p.platform_name }} · #{{ p.source_id }} · {{ p.currency }} / {{ p.unit }}</small
            >
          </div>
          <span class="mono">{{ price(p.price, p.currency) }}</span>
        </button>
        <p v-if="!searching && !candidates.length" class="muted">
          暂无可选择商品，请先采集平台商品。
        </p>
      </div>
      <p class="form-help">只有单位、币种、规格和价格口径一致的在售有效报价参与比较。</p>
      <div class="modal-actions">
        <button type="button" class="btn btn-white" :disabled="saving" @click="showForm = false">
          取消</button
        ><button class="btn btn-primary" :disabled="saving || picked.length < 1">
          {{ saving ? '保存中…' : '保存映射' }}
        </button>
      </div>
    </form></Modal
  ><Modal
    v-if="compareGroup"
    :title="`跨平台比价 · ${compareGroup.name}`"
    wide
    @close="compareGroup = null"
    ><div v-if="!comparison" class="loading-state">
      <span class="spinner"></span>正在检查可比性…
    </div>
    <template v-else
      ><div v-if="comparison.error" class="notice error">{{ comparison.error }}</div>
      <div v-else class="notice" :class="comparison.comparable ? 'success' : 'warning'">
        {{
          comparison.comparable
            ? '已核对比较条件，以下为当前可比报价。'
            : comparison.reason || '当前报价存在单位、币种、状态或数据质量差异，暂不具备可比条件。'
        }}
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>交易平台 / 商品</th>
              <th>基础价格</th>
              <th>单位</th>
              <th>状态</th>
              <th>最近观测</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in comparison.items" :key="p.id">
              <td>
                <strong>{{ p.platform_name }}</strong>
                <div class="table-secondary">{{ p.name }}</div>
              </td>
              <td class="price-text">{{ price(p.price, p.currency) }}</td>
              <td>{{ p.unit }}</td>
              <td><Badge :value="p.status" /></td>
              <td>{{ date(p.last_observed_at, true) }}</td>
            </tr>
          </tbody>
        </table>
      </div></template
    ></Modal
  ><Modal v-if="deleteGroup" title="删除统一商品映射" @close="!saving && (deleteGroup = null)"
    ><div v-if="formError" class="notice error">{{ formError }}</div>
    <p>确认删除“{{ deleteGroup.name }}”的映射关系？原平台商品与价格历史将保留。</p>
    <div class="modal-actions">
      <button class="btn btn-white" :disabled="saving" @click="deleteGroup = null">取消</button
      ><button class="btn btn-danger" :disabled="saving" @click="remove">
        {{ saving ? '删除中…' : '删除映射' }}
      </button>
    </div></Modal
  >
</template>
