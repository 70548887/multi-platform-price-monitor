<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { areaPath } from '../navigation'
import { api, auth, itemsOf, params, price, displayBasisLabel, date, qualityText } from '../api'
import Icon from '../components/Icon.vue'
const route = useRoute()
const adminArea = computed(() => Boolean(route.meta?.admin))
import Badge from '../components/Badge.vue'
import EmptyState from '../components/EmptyState.vue'
import Pagination from '../components/Pagination.vue'
const products = ref<any[]>([]),
  platforms = ref<any[]>([]),
  platformsLoaded = ref(false),
  categories = ref<any[]>([]),
  categoryError = ref(''),
  loading = ref(true),
  error = ref(''),
  q = ref(''),
  platform = ref(''),
  status = ref(''),
  category = ref(''),
  page = ref(1),
  total = ref(0),
  pageSize = 20
let request = 0,
  categoryRequest = 0,
  disposed = false,
  timer: ReturnType<typeof setTimeout>
const noAssignedPlatforms = computed(
  () => auth.user?.role === 'viewer' && platformsLoaded.value && !platforms.value.length,
)
async function load() {
  const current = ++request
  loading.value = true
  error.value = ''
  try {
    const result = await api(
      `/products${params({ platform_id: platform.value, q: q.value, status: status.value, category: category.value, page: page.value, page_size: pageSize })}`,
    )
    if (current === request) {
      products.value = itemsOf(result)
      total.value = result.total ?? products.value.length
    }
  } catch (e: any) {
    if (current === request) error.value = e.message
  } finally {
    if (current === request) loading.value = false
  }
}
const categoryOptions = computed(() => {
  const grouped = new Map<string, number>()
  for (const item of categories.value) {
    if (item.category_name)
      grouped.set(item.category_name, (grouped.get(item.category_name) ?? 0) + item.product_count)
  }
  return [...grouped].map(([name, count]) => ({ name, count }))
})
async function loadCategories() {
  const id = ++categoryRequest
  categoryError.value = ''
  try {
    const result = await api(`/product-categories${params({ platform_id: platform.value })}`)
    if (id === categoryRequest && !disposed) categories.value = itemsOf(result)
  } catch (e: any) {
    if (id === categoryRequest && !disposed) {
      categories.value = []
      categoryError.value = e.message
    }
  }
}
watch(platform, () => {
  category.value = ''
  page.value = 1
  categories.value = []
  loadCategories()
  load()
})
watch([status, category], () => {
  page.value = 1
  load()
})
watch(q, () => {
  clearTimeout(timer)
  timer = setTimeout(() => {
    page.value = 1
    load()
  }, 300)
})
onMounted(async () => {
  await Promise.all([
    load(),
    loadCategories(),
    api('/platforms')
      .then((result) => {
        if (!disposed) {
          platforms.value = itemsOf(result)
          platformsLoaded.value = true
        }
      })
      .catch(() => {}),
  ])
})
onBeforeUnmount(() => {
  disposed = true
  request++
  categoryRequest++
  clearTimeout(timer)
})
function changePage(p: number) {
  page.value = p
  load()
}
</script>
<template>
  <section class="panel">
    <div v-if="noAssignedPlatforms" class="notice warning inset">
      尚未分配平台，请联系管理员为当前成员分配可查看的平台。
    </div>
    <div class="panel-toolbar">
      <div class="search-input">
        <Icon name="search" :size="17" /><input
          v-model="q"
          placeholder="搜索商品名称或平台商品编号"
          aria-label="搜索商品"
        />
      </div>
      <div class="filters">
        <select v-model="platform" aria-label="筛选平台">
          <option value="">全部交易平台</option>
          <option v-for="p in platforms" :key="p.id" :value="p.id">{{ p.name }}</option></select
        ><select v-model="status" aria-label="销售状态">
          <option value="">全部销售状态</option>
          <option value="active">在售</option>
          <option value="closed">停售</option></select
        ><select v-model="category" class="category-filter" aria-label="筛选源平台类目">
          <option value="">全部源平台类目</option>
          <option v-for="item in categoryOptions" :key="item.name" :value="item.name">
            {{ item.name }}（{{ item.count }}）
          </option></select
        ><span class="result-count">{{ total.toLocaleString() }} 个商品</span>
      </div>
    </div>
    <div v-if="categoryError" class="notice warning inset">
      源平台类目读取失败：{{ categoryError
      }}<button class="text-link" @click="loadCategories">重试</button>
    </div>
    <div v-if="error" class="notice error inset">
      {{ error }}<button class="text-link" @click="load">重新加载</button>
    </div>
    <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载商品…</div>
    <div v-else class="table-wrap">
      <table v-if="products.length" class="products-table">
        <thead>
          <tr>
            <th>商品名称 / 编号</th>
            <th>交易平台</th>
            <th>基础单价</th>
            <th>展示价格 / 数量</th>
            <th>销售状态</th>
            <th>最近观测</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in products" :key="p.id">
            <td>
              <RouterLink class="product-name" :to="areaPath(`/products/${p.id}`, adminArea)">{{
                p.name
              }}</RouterLink>
              <div class="table-secondary">
                <span class="mono">#{{ p.source_id }}</span
                ><span v-if="p.category_name" class="dot-separator">{{ p.category_name }}</span>
              </div>
            </td>
            <td>
              <span>{{ p.platform_name }}</span>
              <div v-if="adminArea" class="table-secondary">
                {{ p.account_label ?? '默认账号' }}
              </div>
            </td>
            <td>
              <strong class="price-text">{{ price(p.price, p.currency) }}</strong
              ><span v-if="p.unit" class="unit-text"> / {{ p.unit }}</span>
            </td>
            <td>
              <span class="mono">{{ price(p.display_price, p.currency) }}</span>
              <div class="table-secondary">
                {{ displayBasisLabel(p.display_quantity, p.unit) }}
              </div>
            </td>
            <td>
              <Badge :value="p.status" />
              <div v-if="p.quality?.length" class="quality-warning">
                {{ qualityText(p.quality) }}
              </div>
            </td>
            <td>
              <span>{{ date(p.last_observed_at, true) }}</span>
              <div v-if="p.freshness === 'stale'" class="quality-warning">数据已过期</div>
            </td>
            <td>
              <RouterLink
                :to="areaPath(`/products/${p.id}`, adminArea)"
                class="table-action"
                aria-label="查看商品"
                ><Icon name="arrow" :size="17"
              /></RouterLink>
            </td>
          </tr>
        </tbody>
      </table>
      <EmptyState
        v-else
        :title="q || platform || status || category ? '没有找到符合条件的商品' : '尚未采集到商品'"
        :description="
          q || platform || status || category
            ? '试试其他关键词或调整筛选条件。'
            : adminArea
              ? '请先添加平台并完成首次采集，商品与价格将自动汇总。'
              : '暂无可查看商品，请联系管理员分配平台或等待采集完成。'
        "
        ><RouterLink
          v-if="adminArea && !q && !platform && !status && !category"
          to="/admin/platforms"
          class="btn btn-primary btn-sm"
          >管理平台</RouterLink
        ></EmptyState
      >
    </div>
    <Pagination
      v-if="total"
      :page="page"
      :total="total"
      :page-size="pageSize"
      @change="changePage"
    />
  </section>
  <div class="page-note">
    <Icon
      name="shield"
      :size="15"
    />价格按原始精度保存。展示价格已应用平台展示数量；不同单位的商品请核对口径后比较。
  </div>
</template>
