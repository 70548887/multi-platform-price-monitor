<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api, itemsOf, params, price, displayBasisLabel, date, qualityText } from '../api'
import Icon from '../components/Icon.vue'
import Badge from '../components/Badge.vue'
import EmptyState from '../components/EmptyState.vue'
import Pagination from '../components/Pagination.vue'
const products = ref<any[]>([]),
  platforms = ref<any[]>([]),
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
  timer: ReturnType<typeof setTimeout>
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
watch([platform, status], () => {
  page.value = 1
  load()
})
watch([q, category], () => {
  clearTimeout(timer)
  timer = setTimeout(() => {
    page.value = 1
    load()
  }, 300)
})
onMounted(async () => {
  await Promise.all([
    load(),
    api('/platforms')
      .then((result) => (platforms.value = itemsOf(result)))
      .catch(() => {}),
  ])
})
function changePage(p: number) {
  page.value = p
  load()
}
</script>
<template>
  <section class="panel">
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
        ><input
          v-model="category"
          class="category-filter"
          placeholder="分类名称"
          aria-label="筛选分类（精确名称）"
        /><span class="result-count">{{ total.toLocaleString() }} 个商品</span>
      </div>
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
              <RouterLink class="product-name" :to="`/products/${p.id}`">{{ p.name }}</RouterLink>
              <div class="table-secondary">
                <span class="mono">#{{ p.source_id }}</span
                ><span v-if="p.category_name" class="dot-separator">{{ p.category_name }}</span>
              </div>
            </td>
            <td>
              <span>{{ p.platform_name }}</span>
              <div class="table-secondary">{{ p.account_label ?? '默认账号' }}</div>
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
              <RouterLink :to="`/products/${p.id}`" class="table-action" aria-label="查看商品"
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
            : '请先添加平台并完成首次采集，商品与价格将自动汇总。'
        "
        ><RouterLink
          v-if="!q && !platform && !status && !category"
          to="/platforms"
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
