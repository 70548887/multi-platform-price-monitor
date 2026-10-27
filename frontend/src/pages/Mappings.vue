<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { areaPath } from '../navigation'
import { platformPreviews } from '../comparisonPreview'
import {
  categoryPath,
  categoryPathName,
  categoryTreeRows,
  descendantCategoryIds,
} from '../categoryTree'
import { api, itemsOf, json, params, auth, price, displayBasisLabel } from '../api'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/EmptyState.vue'
import Modal from '../components/Modal.vue'
import Pagination from '../components/Pagination.vue'

const route = useRoute()
const router = useRouter()
const adminArea = computed(() => Boolean(route.meta?.admin))
const admin = computed(() => auth.user?.role === 'admin' && adminArea.value)
const groups = ref<any[]>([]),
  categories = ref<any[]>([]),
  selectedCategory = ref(
    typeof route.query?.category_id === 'string' && /^[1-9][0-9]*$/.test(route.query.category_id)
      ? route.query.category_id
      : 'all',
  ),
  collapsedCategories = ref(new Set<number>()),
  categoryPanelOpen = ref(false),
  query = ref(''),
  groupPage = ref(1),
  groupTotal = ref(0),
  loading = ref(true),
  error = ref(''),
  notice = ref(''),
  categoryError = ref(''),
  showForm = ref(false),
  editing = ref<any>(null),
  saving = ref(false),
  formError = ref(''),
  search = ref(''),
  candidates = ref<any[]>([]),
  candidatePlatforms = ref<any[]>([]),
  candidatePlatform = ref(''),
  candidateCategories = ref<any[]>([]),
  candidateCategory = ref(''),
  candidatePage = ref(1),
  candidateTotal = ref(0),
  candidateError = ref(''),
  candidateResultToken = ref(''),
  searchMode = ref('plain'),
  matchMode = ref('all'),
  excludeKeywords = ref(''),
  activeOnly = ref(true),
  matchedKeywords = ref<{ label: string; variants: string[] }[]>([]),
  matchingPlatforms = ref<{ platform_id: number; name: string; count: number }[]>([]),
  selectionNotice = ref(''),
  bulkSelecting = ref(false),
  showPicked = ref(false),
  picked = ref<any[]>([]),
  searching = ref(false),
  deleteGroup = ref<any>(null),
  showCategoryForm = ref(false),
  editingCategory = ref<any>(null),
  deletingCategory = ref<any>(null),
  categorySaving = ref(false),
  categoryFormError = ref('')
const groupPageSize = 12
const maxOffers = 5000
const form = reactive({
  name: '',
  category_id: '',
  description: '',
  match_mode: 'similar',
  unit: '',
  currency: 'CNY',
})
const categoryForm = reactive({ name: '', description: '', sort_order: 0, parent_id: '' })
let timer: ReturnType<typeof setTimeout>,
  queryTimer: ReturnType<typeof setTimeout>,
  searchId = 0,
  categoryRequest = 0,
  listRequest = 0,
  directoryRequest = 0,
  bulkSequence = 0,
  disposed = false
const selectedCategoryName = computed(() =>
  selectedCategory.value === 'all'
    ? '全部商品'
    : selectedCategory.value === 'uncategorized'
      ? '未分类商品'
      : categoryLabel(categories.value.find((item) => String(item.id) === selectedCategory.value)),
)
const categoryRows = computed(() => categoryTreeRows(categories.value, collapsedCategories.value))
const previewByGroup = computed(
  () => new Map(groups.value.map((group) => [group.id, platformPreviews(group.products ?? [])])),
)
function previews(group: any) {
  return previewByGroup.value.get(group.id) ?? []
}
const categoryOptions = computed(() => categoryTreeRows(categories.value))
const categoryParentOptions = computed(() => {
  const excluded = editingCategory.value
    ? descendantCategoryIds(categories.value, editingCategory.value.id)
    : new Set<number>()
  return categoryOptions.value.filter((item) => !excluded.has(item.id))
})
function categoryLabel(category: any) {
  return category ? categoryPathName(category, categories.value) : '比价商品'
}
function toggleCategory(id: number) {
  const next = new Set(collapsedCategories.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  collapsedCategories.value = next
}
function revealCategory(id: number) {
  const category = categories.value.find((item) => item.id === id)
  if (!category) return
  const next = new Set(collapsedCategories.value)
  for (const item of categoryPath(category, categories.value)) next.delete(item.id)
  collapsedCategories.value = next
}
function cannotDeleteCategory(category: any) {
  return (
    category.product_count > 0 ||
    category.children_count > 0 ||
    categories.value.some((item) => item.parent_id === category.id)
  )
}
const candidateCategoryOptions = computed(() => {
  const grouped = new Map<string, number>()
  for (const item of candidateCategories.value)
    if (item.category_name)
      grouped.set(item.category_name, (grouped.get(item.category_name) ?? 0) + item.product_count)
  return [...grouped].map(([name, count]) => ({ name, count }))
})
function detailPath(group: any) {
  return areaPath(`/compare/${group.id}`, adminArea.value)
}
function platformCount(group: any) {
  return group.platform_count ?? new Set((group.products ?? []).map((p: any) => p.platform_id)).size
}
function offerCount(group: any) {
  return group.product_count ?? group.products?.length ?? 0
}
async function load() {
  const id = ++listRequest
  loading.value = true
  error.value = ''
  try {
    const result = await api(
      `/mappings${params({
        category_id:
          selectedCategory.value !== 'all' && selectedCategory.value !== 'uncategorized'
            ? selectedCategory.value
            : undefined,
        uncategorized: selectedCategory.value === 'uncategorized' ? true : undefined,
        q: query.value,
        page: groupPage.value,
        page_size: groupPageSize,
      })}`,
    )
    if (id === listRequest && !disposed) {
      groups.value = itemsOf(result)
      groupTotal.value = result.total ?? groups.value.length
      if (groupPage.value > 1 && !groups.value.length && groupTotal.value) {
        groupPage.value = Math.ceil(groupTotal.value / groupPageSize)
        await load()
      }
    }
  } catch (e: any) {
    if (id === listRequest && !disposed) error.value = e.message
  } finally {
    if (id === listRequest && !disposed) loading.value = false
  }
}
async function loadCategories() {
  const id = ++directoryRequest
  categoryError.value = ''
  try {
    const result = await api('/comparison-categories')
    if (id === directoryRequest && !disposed) {
      categories.value = itemsOf(result)
      if (/^[1-9][0-9]*$/.test(selectedCategory.value))
        revealCategory(Number(selectedCategory.value))
    }
  } catch (e: any) {
    if (id === directoryRequest && !disposed) categoryError.value = e.message
  }
}
function selectCategory(value: string) {
  selectedCategory.value = value
  categoryPanelOpen.value = false
  groupPage.value = 1
  load()
}
function changeGroupPage(value: number) {
  groupPage.value = value
  load()
}
watch(query, () => {
  clearTimeout(queryTimer)
  listRequest++
  queryTimer = setTimeout(() => {
    groupPage.value = 1
    load()
  }, 250)
})
onMounted(async () => {
  await Promise.all([
    load(),
    loadCategories(),
    admin.value
      ? api('/platforms')
          .then((result) => {
            if (!disposed) candidatePlatforms.value = itemsOf(result)
          })
          .catch((e) => {
            if (!disposed) error.value = e.message
          })
      : Promise.resolve(),
  ])
  const editId = route.query?.edit
  if (admin.value && typeof editId === 'string' && /^[1-9][0-9]*$/.test(editId) && !disposed) {
    try {
      const group = await api(`/mappings/${editId}`)
      if (!disposed) open(group)
    } catch (e: any) {
      if (!disposed) error.value = e.message
    }
  }
})
async function loadCandidateCategories() {
  if (!showForm.value || disposed) return
  const id = ++categoryRequest
  try {
    const result = await api(
      `/product-categories${params({ platform_id: candidatePlatform.value })}`,
    )
    if (id === categoryRequest && showForm.value && !disposed)
      candidateCategories.value = itemsOf(result)
  } catch (e: any) {
    if (id === categoryRequest && showForm.value && !disposed) candidateError.value = e.message
  }
}
async function find() {
  if (!showForm.value || !admin.value || disposed) return
  const id = ++searchId
  searching.value = true
  candidateError.value = ''
  try {
    const result = await api(candidateQuery(candidatePage.value, 30))
    if (id === searchId && showForm.value && !disposed) {
      candidates.value = itemsOf(result)
      candidateTotal.value = result.total ?? candidates.value.length
      matchedKeywords.value = result.keywords ?? []
      matchingPlatforms.value = result.platform_counts ?? []
      candidateResultToken.value = result.result_token ?? ''
    }
  } catch (e: any) {
    if (id === searchId && showForm.value && !disposed) {
      candidates.value = []
      candidateTotal.value = 0
      candidateResultToken.value = ''
      matchedKeywords.value = []
      matchingPlatforms.value = []
      candidateError.value = e.message
    }
  } finally {
    if (id === searchId && showForm.value && !disposed) searching.value = false
  }
}
function candidateQuery(page: number, pageSize: number) {
  const smart = searchMode.value === 'smart' && Boolean(search.value.trim())
  return `${smart ? '/product-suggestions' : '/products'}${params({
    q: search.value,
    platform_id: candidatePlatform.value,
    category: candidateCategory.value,
    page,
    page_size: pageSize,
    ...(smart
      ? {
          exclude: excludeKeywords.value,
          mode: matchMode.value,
          active_only: activeOnly.value,
          mapping_id: editing.value?.id,
        }
      : {}),
  })}`
}
function invalidateBulk() {
  bulkSequence++
  bulkSelecting.value = false
  selectionNotice.value = ''
}
function applyMatchOptions() {
  invalidateBulk()
  clearTimeout(timer)
  searchId++
  candidates.value = []
  matchedKeywords.value = []
  matchingPlatforms.value = []
  candidateTotal.value = 0
  candidateResultToken.value = ''
  candidatePage.value = 1
  if (showForm.value) find()
}
function suggestFromName() {
  searchMode.value = 'smart'
  search.value = form.name.trim()
  applyMatchOptions()
}
function selectCurrentPage() {
  if (searching.value || saving.value || bulkSelecting.value || !admin.value) return
  const selected = new Set(picked.value.map((p) => p.id))
  let added = 0
  for (const product of candidates.value) {
    if (picked.value.length >= maxOffers) break
    if (!candidateConflict(product) && !selected.has(product.id)) {
      picked.value.push(product)
      selected.add(product.id)
      added++
    }
  }
  selectionNotice.value = `已加入本页 ${added} 条商品，保存后生效。`
}
function removeCurrentPage() {
  if (searching.value || saving.value || bulkSelecting.value || !admin.value) return
  const ids = new Set(candidates.value.map((p) => p.id))
  picked.value = picked.value.filter((p) => !ids.has(p.id))
  selectionNotice.value = '已取消本页选择，其他页的选择保留。'
}
async function selectAllMatching() {
  if (
    !admin.value ||
    saving.value ||
    searching.value ||
    bulkSelecting.value ||
    !showForm.value ||
    !candidateTotal.value
  )
    return
  const sequence = ++bulkSequence
  bulkSelecting.value = true
  selectionNotice.value = ''
  const initial = new Set(picked.value.map((p) => p.id))
  const collected = new Map<number, any>()
  const visited = new Set<number>()
  const queryAtStart = candidateQuery(1, 100)
  let resultToken = candidateResultToken.value
  let skipped = 0
  try {
    let total = candidateTotal.value
    for (let page = 1; (page - 1) * 100 < Math.min(total, maxOffers); page++) {
      const result = await api(queryAtStart.replace(/([?&])page=1(?=&|$)/, `$1page=${page}`))
      if (disposed || !showForm.value || sequence !== bulkSequence) return
      if (searchMode.value === 'smart') {
        if (typeof result.result_token !== 'string' || !result.result_token)
          throw new Error('推荐数据缺少一致性标识，请刷新页面后重试。')
        if (resultToken && resultToken !== result.result_token)
          throw new Error('候选商品或归属已变化，请重新搜索并核对后再选择。')
        resultToken = result.result_token
      }
      if (page === 1) total = result.total ?? total
      else if (result.total !== undefined && result.total !== total)
        throw new Error('候选目录在读取期间发生变化，请重新筛选后再批量选择。')
      const rows = itemsOf(result)
      const expected = Math.min(100, Math.min(total, maxOffers) - (page - 1) * 100)
      if (rows.length !== expected || rows.some((product) => visited.has(product.id)))
        throw new Error('候选分页不完整或有重复，请重试；本次没有加入商品。')
      for (const product of rows) {
        visited.add(product.id)
        if (candidateConflict(product)) {
          skipped++
          continue
        }
        if (!initial.has(product.id)) collected.set(product.id, product)
      }
      selectionNotice.value = `正在准备 ${Math.min(page * 100, total)} / ${Math.min(total, maxOffers)} 条候选…`
    }
    if (sequence !== bulkSequence || disposed || !showForm.value) return
    // Commit to the local draft only after the complete operation succeeds.
    const selected = new Set(picked.value.map((p) => p.id))
    let added = 0
    for (const product of collected.values()) {
      if (picked.value.length >= maxOffers) break
      if (!selected.has(product.id)) {
        picked.value.push(product)
        selected.add(product.id)
        added++
      }
    }
    selectionNotice.value = `已加入 ${added} 条候选，跳过 ${skipped} 条已有其他归属的商品。${total > maxOffers ? `本次只处理前 ${maxOffers} 条，请缩小关键词范围。` : ''}请核对后保存，推荐不代表同规格。`
  } catch (e: any) {
    if (sequence === bulkSequence && !disposed) {
      candidateError.value = e.message
      selectionNotice.value = '批量读取失败，本次未添加任何商品；原有选择已保留。'
    }
  } finally {
    if (sequence === bulkSequence && !disposed) bulkSelecting.value = false
  }
}
function scheduleMatchOptions() {
  invalidateBulk()
  clearTimeout(timer)
  searchId++
  candidates.value = []
  matchedKeywords.value = []
  matchingPlatforms.value = []
  candidateTotal.value = 0
  candidatePage.value = 1
  if (showForm.value) timer = setTimeout(find, 250)
}
watch(search, scheduleMatchOptions)
watch(candidatePlatform, () => {
  invalidateBulk()
  candidateCategory.value = ''
  candidateCategories.value = []
  candidatePage.value = 1
  if (showForm.value) {
    loadCandidateCategories()
    find()
  }
})
watch(candidateCategory, () => {
  invalidateBulk()
  candidatePage.value = 1
  if (showForm.value) find()
})
function changeCandidatePage(value: number) {
  candidatePage.value = value
  find()
}
function clearFormRequests() {
  invalidateBulk()
  showForm.value = false
  searching.value = false
  searchId++
  categoryRequest++
  clearTimeout(timer)
}
async function clearEditQuery() {
  if (!Object.prototype.hasOwnProperty.call(route.query ?? {}, 'edit')) return
  const nextQuery = { ...route.query }
  delete nextQuery.edit
  await router.replace({ path: route.path, query: nextQuery, hash: route.hash })
}
async function closeForm() {
  if (saving.value) return
  clearFormRequests()
  try {
    await clearEditQuery()
  } catch {
    if (!disposed) error.value = '商品编辑已关闭，清理链接状态失败，请刷新页面。'
  }
}
onBeforeUnmount(() => {
  disposed = true
  searchId++
  categoryRequest++
  listRequest++
  directoryRequest++
  clearTimeout(timer)
  clearTimeout(queryTimer)
})
function open(group?: any) {
  if (!admin.value) return
  editing.value = group ?? null
  Object.assign(form, {
    name: group?.name ?? '',
    description: group?.description ?? '',
    category_id:
      group?.category_id != null
        ? String(group.category_id)
        : !group && !['all', 'uncategorized'].includes(selectedCategory.value)
          ? selectedCategory.value
          : '',
    match_mode: group?.match_mode ?? (group ? 'exact' : 'similar'),
    unit: group?.unit ?? '',
    currency: group?.currency ?? 'CNY',
  })
  picked.value = [...(group?.products ?? [])]
  formError.value = ''
  invalidateBulk()
  searchMode.value = 'plain'
  matchMode.value = 'all'
  excludeKeywords.value = ''
  activeOnly.value = true
  matchedKeywords.value = []
  matchingPlatforms.value = []
  showPicked.value = false
  search.value = ''
  candidates.value = []
  candidatePlatform.value = ''
  candidateCategory.value = ''
  candidatePage.value = 1
  candidateTotal.value = 0
  candidateError.value = ''
  showForm.value = true
  loadCandidateCategories()
  find()
}
function candidateConflict(product: any) {
  return Boolean(product.canonical_id && product.canonical_id !== editing.value?.id)
}
function toggle(product: any) {
  const index = picked.value.findIndex((p) => p.id === product.id)
  if (index >= 0) picked.value.splice(index, 1)
  else if (!candidateConflict(product)) {
    if (picked.value.length >= maxOffers) {
      formError.value = `每个统一商品最多关联 ${maxOffers} 条平台商品。`
      return
    }
    picked.value.push(product)
  }
}
async function save() {
  if (!admin.value || saving.value || bulkSelecting.value) return
  formError.value = ''
  if (!form.name.trim()) {
    formError.value = '请填写统一商品名称。'
    return
  }
  if (form.match_mode === 'exact' && !form.unit.trim()) {
    formError.value = '同规格比价需要明确计价单位；也可先选择同类 / 相似商品。'
    return
  }
  saving.value = true
  try {
    await api(
      editing.value ? `/mappings/${editing.value.id}` : '/mappings',
      json(editing.value ? 'PUT' : 'POST', {
        ...form,
        name: form.name.trim(),
        category_id: form.category_id ? Number(form.category_id) : null,
        product_ids: picked.value.map((p) => p.id),
      }),
    )
    if (disposed) return
    clearFormRequests()
    notice.value = '统一商品已保存，可在详情中查看关联平台的报价。'
    groupPage.value = 1
    await Promise.all([load(), loadCategories()])
    await clearEditQuery()
  } catch (e: any) {
    if (!disposed) formError.value = e.message
  } finally {
    if (!disposed) saving.value = false
  }
}
function confirmRemove(group: any) {
  if (admin.value) {
    deleteGroup.value = group
    formError.value = ''
  }
}
async function remove() {
  if (!admin.value || saving.value) return
  saving.value = true
  formError.value = ''
  try {
    await api(`/mappings/${deleteGroup.value.id}`, json('DELETE'))
    if (disposed) return
    deleteGroup.value = null
    notice.value = '统一商品已删除，原平台商品与价格历史保留。'
    await Promise.all([load(), loadCategories()])
  } catch (e: any) {
    if (!disposed) formError.value = e.message
  } finally {
    if (!disposed) saving.value = false
  }
}
function openCategory(category?: any) {
  if (!admin.value) return
  editingCategory.value = category ?? null
  Object.assign(categoryForm, {
    name: category?.name ?? '',
    description: category?.description ?? '',
    sort_order: category?.sort_order ?? 0,
    parent_id: category?.parent_id ? String(category.parent_id) : '',
  })
  categoryFormError.value = ''
  showCategoryForm.value = true
}
function openSubcategory(parent: any) {
  if (!admin.value) return
  openCategory()
  categoryForm.parent_id = String(parent.id)
}
async function saveCategory() {
  if (!admin.value || categorySaving.value) return
  categoryFormError.value = ''
  if (!categoryForm.name.trim()) {
    categoryFormError.value = '请填写类目名称。'
    return
  }
  if (
    categoryForm.parent_id &&
    !categoryParentOptions.value.some((item) => String(item.id) === categoryForm.parent_id)
  ) {
    categoryFormError.value = '请选择有效的上级类目，不能选择自身或下级类目。'
    return
  }
  categorySaving.value = true
  try {
    const result = await api(
      editingCategory.value
        ? `/comparison-categories/${editingCategory.value.id}`
        : '/comparison-categories',
      json(editingCategory.value ? 'PUT' : 'POST', {
        ...categoryForm,
        name: categoryForm.name.trim(),
        parent_id: categoryForm.parent_id ? Number(categoryForm.parent_id) : null,
      }),
    )
    if (disposed) return
    showCategoryForm.value = false
    selectedCategory.value = String(result.id)
    groupPage.value = 1
    notice.value = '比价类目已保存。可在该类目下建立统一商品。'
    await Promise.all([loadCategories(), load()])
  } catch (e: any) {
    if (!disposed) categoryFormError.value = e.message
  } finally {
    if (!disposed) categorySaving.value = false
  }
}
function confirmRemoveCategory(category: any) {
  if (!admin.value || cannotDeleteCategory(category)) return
  deletingCategory.value = category
  categoryFormError.value = ''
}
async function removeCategory() {
  if (!admin.value || categorySaving.value) return
  categorySaving.value = true
  categoryFormError.value = ''
  try {
    const id = deletingCategory.value.id
    await api(`/comparison-categories/${id}`, json('DELETE'))
    if (disposed) return
    deletingCategory.value = null
    if (selectedCategory.value === String(id)) selectedCategory.value = 'all'
    groupPage.value = 1
    notice.value = '空类目已删除。'
    await Promise.all([loadCategories(), load()])
  } catch (e: any) {
    if (!disposed) categoryFormError.value = e.message
  } finally {
    if (!disposed) categorySaving.value = false
  }
}
</script>
<template>
  <div class="page-tools">
    <div class="section-description">按类目整理统一商品，查看同类或相似商品在不同平台的报价。</div>
    <button v-if="admin" class="btn btn-primary" @click="open()">
      <Icon name="plus" :size="17" />建立商品
    </button>
  </div>
  <div v-if="error" class="notice error">{{ error }}</div>
  <div v-if="notice" class="notice success">{{ notice }}</div>
  <div class="comparison-catalogue">
    <aside class="panel comparison-categories" aria-label="比价类目">
      <div class="category-heading">
        <h2>比价类目</h2>
        <button v-if="admin" class="icon-btn" aria-label="新增一级类目" @click="openCategory()">
          <Icon name="plus" :size="17" />
        </button>
      </div>
      <button
        type="button"
        class="category-mobile-summary"
        :aria-expanded="categoryPanelOpen"
        aria-controls="comparison-category-tree"
        @click="categoryPanelOpen = !categoryPanelOpen"
      >
        <span>{{ selectedCategoryName }}</span
        ><span
          >{{ categoryPanelOpen ? '收起分类' : '选择分类'
          }}<Icon :name="categoryPanelOpen ? 'up' : 'down'" :size="14"
        /></span>
      </button>
      <div
        id="comparison-category-tree"
        class="category-body"
        :class="{ 'is-open': categoryPanelOpen }"
      >
        <p class="category-caption">按业务与场景分层，例如“抖音 / 视频”</p>
        <div v-if="categoryError" class="notice error">
          {{ categoryError
          }}<button type="button" class="text-link" @click="loadCategories">重试</button>
        </div>
        <button
          type="button"
          class="category-link"
          :class="{ selected: selectedCategory === 'all' }"
          @click="selectCategory('all')"
        >
          全部商品
        </button>
        <button
          type="button"
          class="category-link"
          :class="{ selected: selectedCategory === 'uncategorized' }"
          @click="selectCategory('uncategorized')"
        >
          未分类商品
        </button>
        <div
          v-for="item in categoryRows"
          :key="item.id"
          class="category-entry"
          :style="{ '--category-depth': item.depth }"
        >
          <button
            v-if="item.hasChildren"
            type="button"
            class="category-toggle icon-btn"
            :aria-label="`${collapsedCategories.has(item.id) ? '展开' : '收起'}类目 ${categoryLabel(item)}`"
            :aria-expanded="!collapsedCategories.has(item.id)"
            @click="toggleCategory(item.id)"
          >
            <Icon :name="collapsedCategories.has(item.id) ? 'arrow' : 'down'" :size="13" />
          </button>
          <span v-else class="category-toggle-spacer" aria-hidden="true"></span>
          <button
            type="button"
            class="category-link"
            :class="{ selected: selectedCategory === String(item.id) }"
            @click="selectCategory(String(item.id))"
            :title="categoryLabel(item)"
          >
            <span>{{ item.name }}</span
            ><small>{{ item.product_count }}</small>
          </button>
          <button
            v-if="admin"
            type="button"
            class="icon-btn"
            :aria-label="`新增子类目 ${categoryLabel(item)}`"
            @click="openSubcategory(item)"
          >
            <Icon name="plus" :size="13" />
          </button>
          <button
            v-if="admin"
            type="button"
            class="icon-btn"
            :aria-label="`编辑类目 ${categoryLabel(item)}`"
            @click="openCategory(item)"
          >
            <Icon name="edit" :size="13" />
          </button>
          <button
            v-if="admin"
            type="button"
            class="icon-btn"
            :disabled="cannotDeleteCategory(item)"
            :aria-label="`删除类目 ${categoryLabel(item)}`"
            :title="cannotDeleteCategory(item) ? '请先移走下级类目和统一商品' : '删除空类目'"
            @click="confirmRemoveCategory(item)"
          >
            <Icon name="close" :size="13" />
          </button>
        </div>
        <p v-if="!categories.length && !categoryError" class="category-empty">
          {{
            admin
              ? '先新增一级类目，例如“抖音”，再建立“视频”“直播”等子类目。'
              : '管理员建立类目并分配平台后，可在这里查看。'
          }}
        </p>
        <button
          v-if="admin"
          type="button"
          class="btn btn-white btn-sm category-create"
          @click="openCategory()"
        >
          新增一级类目
        </button>
      </div>
    </aside>
    <section class="comparison-groups">
      <div class="catalogue-toolbar">
        <div>
          <h2>{{ selectedCategoryName }}</h2>
          <p>{{ groupTotal }} 个统一商品 · 包含下级类目</p>
        </div>
        <div class="search-input">
          <Icon name="search" :size="16" /><input
            v-model="query"
            aria-label="搜索统一商品"
            placeholder="搜索统一商品名称或说明"
          />
        </div>
      </div>
      <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载全网比价…</div>
      <div v-else-if="groups.length" class="mapping-grid comparison-grid">
        <article
          v-for="group in groups"
          :key="group.id"
          class="panel mapping-card"
          :class="{ 'is-unlinked': !offerCount(group) }"
        >
          <header class="comparison-card-head">
            <div class="comparison-card-top">
              <span class="comparison-category">{{
                group.category_path_name || group.category_name || '未分类'
              }}</span
              ><span class="comparison-mode" :class="{ pending: !offerCount(group) }">{{
                !offerCount(group)
                  ? '待关联'
                  : group.match_mode === 'similar'
                    ? '同类 / 相似'
                    : '同规格'
              }}</span>
            </div>
            <h3 class="comparison-name">
              <RouterLink :to="detailPath(group)">{{ group.name }}</RouterLink>
            </h3>
            <p v-if="group.description" class="comparison-description" :title="group.description">
              {{ group.description }}
            </p>
            <div v-if="offerCount(group)" class="comparison-counts">
              <span
                ><Icon name="platforms" :size="13" /><strong>{{ platformCount(group) }}</strong>
                个平台</span
              >
              <span
                ><strong>{{ offerCount(group) }}</strong> 条报价</span
              >
            </div>
          </header>
          <div v-if="offerCount(group)" class="comparison-previews" aria-label="各平台报价概览">
            <div
              v-for="platform in previews(group).slice(0, 3)"
              :key="platform.id"
              class="platform-preview"
            >
              <div class="preview-platform">
                <strong>{{ platform.name }}</strong
                ><small>{{ platform.count }} 条报价 · {{ platform.validCount }} 条有效在售</small>
              </div>
              <div class="preview-range">
                <template v-if="platform.minimum !== null"
                  ><strong class="mono"
                    >{{ price(platform.minimum, platform.currency)
                    }}<template v-if="platform.minimum !== platform.maximum">
                      ～ {{ price(platform.maximum, platform.currency) }}</template
                    ></strong
                  ><small
                    >基础报价范围 ·
                    {{ platform.unit ? `每 ${platform.unit}` : '单位待核验' }}</small
                  ></template
                >
                <template v-else
                  ><span>{{ platform.mixedBasis ? '多种计价口径' : '暂无有效在售报价' }}</span
                  ><small>打开详情逐项查看</small></template
                >
              </div>
            </div>
            <p v-if="previews(group).length > 3" class="preview-more">
              另有 {{ previews(group).length - 3 }} 个平台，进入详情查看全部报价
            </p>
          </div>
          <p v-else class="unlinked-description">
            选择各交易平台的同类商品后，即可在这里汇总报价。
          </p>
          <div class="mapping-actions">
            <RouterLink
              v-if="offerCount(group)"
              :to="detailPath(group)"
              class="btn btn-primary btn-sm"
              ><Icon name="chart" :size="14" />查看各平台价格</RouterLink
            ><button
              v-if="admin"
              class="btn btn-sm"
              :class="offerCount(group) ? 'btn-white' : 'btn-primary'"
              @click="open(group)"
            >
              <Icon v-if="!offerCount(group)" name="plus" :size="14" />{{
                offerCount(group) ? '关联 / 编辑' : '关联平台商品'
              }}</button
            ><button v-if="admin" class="text-link danger-text" @click="confirmRemove(group)">
              删除
            </button>
          </div>
        </article>
      </div>
      <section v-else class="panel">
        <EmptyState
          icon="globe"
          :title="query ? '没有找到符合条件的统一商品' : '该类目下暂无商品'"
          :description="
            admin
              ? '先建立统一商品，再选择其他平台的同类或相似商品。例如将多个平台的点赞商品关联到同一个商品中。'
              : '管理员建立商品并关联已授权平台后，可在这里查看各平台报价。'
          "
          ><button v-if="admin" type="button" class="btn btn-primary btn-sm" @click="open()">
            建立商品
          </button></EmptyState
        >
      </section>
      <Pagination
        v-if="groupTotal"
        :page="groupPage"
        :total="groupTotal"
        :page-size="groupPageSize"
        @change="changeGroupPage"
      />
    </section>
  </div>
  <Modal v-if="showForm" :title="editing ? '编辑比价商品' : '建立比价商品'" wide @close="closeForm"
    ><form @submit.prevent="save">
      <div v-if="formError" class="notice error">{{ formError }}</div>
      <div class="form-grid">
        <label
          >统一商品名称<input
            v-model="form.name"
            required
            maxlength="200"
            placeholder="例如：抖音点赞、视频播放" /></label
        ><label
          >比价类目<select v-model="form.category_id" aria-label="统一商品比价类目">
            <option value="">未分类</option>
            <option v-for="item in categoryOptions" :key="item.id" :value="String(item.id)">
              {{ categoryLabel(item) }}
            </option>
          </select></label
        >
      </div>
      <label
        >商品说明<textarea
          v-model="form.description"
          rows="2"
          maxlength="2000"
          placeholder="说明目标平台、品质、速度、售后等关联范围"
        />
      </label>
      <label
        >关联方式<select v-model="form.match_mode" aria-label="商品关联方式">
          <option value="similar">同类 / 相似商品 · 分别展示各平台报价</option>
          <option value="exact">同规格商品 · 核对口径后比较最低价</option>
        </select></label
      >
      <p class="form-help">
        {{
          form.match_mode === 'similar'
            ? '可关联不同规格或计价单位的同类商品，各自展示源价格，不混算最低价。'
            : '仅关联明确相同单位、币种和规格的商品；系统还会核对销售状态、质量与采样时间。'
        }}
      </p>
      <div class="form-grid">
        <label
          >{{ form.match_mode === 'exact' ? '统一计价单位' : '参考单位（可留空）'
          }}<input
            v-model="form.unit"
            :required="form.match_mode === 'exact'"
            maxlength="80"
            placeholder="例如：个、次、人" /></label
        ><label
          >{{ form.match_mode === 'exact' ? '统一币种' : '参考币种'
          }}<input
            v-model="form.currency"
            required
            maxlength="8"
            placeholder="CNY"
            @input="form.currency = form.currency.toUpperCase()"
        /></label>
      </div>
      <div class="association-heading">
        <h3>关联各平台商品</h3>
        <span>已选择 {{ picked.length }} / {{ maxOffers }} 条</span>
      </div>
      <p class="form-help">
        可以先保存统一商品，再逐步关联平台商品。切换平台、类目或页码时，已选择的商品会保留。
      </p>
      <div v-if="picked.length" class="picked-summary">
        <button
          type="button"
          class="text-link"
          :aria-expanded="showPicked"
          @click="showPicked = !showPicked"
        >
          {{ showPicked ? '收起已选商品' : `查看已选 ${picked.length} 条商品` }}
        </button>
        <span>选择仅保存在当前编辑草稿，点击保存才生效。</span>
      </div>
      <div v-if="picked.length && showPicked" class="picked-products selected-product-scroll">
        <button
          v-for="product in picked"
          :key="product.id"
          type="button"
          :disabled="bulkSelecting"
          @click="toggle(product)"
        >
          {{ product.platform_name }} · {{ product.name }}<Icon name="close" :size="13" />
        </button>
      </div>
      <section class="smart-match-controls">
        <div class="smart-match-heading">
          <strong>关键词匹配助手</strong
          ><button
            type="button"
            class="text-link"
            :disabled="!form.name.trim() || saving || bulkSelecting"
            @click="suggestFromName"
          >
            用商品名称推荐
          </button>
        </div>
        <div class="form-grid">
          <label
            >查找方式<select
              v-model="searchMode"
              aria-label="商品查找方式"
              :disabled="saving"
              @change="applyMatchOptions"
            >
              <option value="plain">普通名称 / 编号搜索</option>
              <option value="smart">智能关键词推荐（包含常见别名）</option>
            </select></label
          >
          <label v-if="searchMode === 'smart'"
            >关键词条件<select
              v-model="matchMode"
              aria-label="关键词匹配条件"
              :disabled="saving"
              @change="applyMatchOptions"
            >
              <option value="all">同时匹配全部关键词</option>
              <option value="any">宽松匹配任一关键词</option>
            </select></label
          >
        </div>
        <template v-if="searchMode === 'smart'">
          <label
            >排除关键词<input
              v-model="excludeKeywords"
              maxlength="160"
              aria-label="排除匹配关键词"
              placeholder="例如：评论 分享 三连（空格分开）"
              :disabled="saving"
              @input="scheduleMatchOptions"
          /></label>
          <label class="checkbox-label"
            ><input
              v-model="activeOnly"
              type="checkbox"
              :disabled="saving"
              @change="applyMatchOptions"
            />只推荐当前在售商品</label
          >
          <p class="form-help">
            例如“B站 点赞”可识别“哔哩哔哩 / bili / BL”和“点赞 /
            赞”等写法。名称与源类目共同匹配，结果仍需核对用途、规格和单位。
          </p>
        </template>
      </section>
      <div class="form-grid">
        <label
          >候选平台<select v-model="candidatePlatform" aria-label="筛选候选平台">
            <option value="">全部已授权平台</option>
            <option v-for="platform in candidatePlatforms" :key="platform.id" :value="platform.id">
              {{ platform.name }}
            </option>
          </select></label
        ><label
          >源平台类目<select v-model="candidateCategory" aria-label="筛选候选类目">
            <option value="">全部源平台类目</option>
            <option v-for="item in candidateCategoryOptions" :key="item.name" :value="item.name">
              {{ item.name }}（{{ item.count }}）
            </option>
          </select></label
        >
      </div>
      <div class="search-input full">
        <Icon name="search" :size="16" /><input
          v-model="search"
          :placeholder="
            searchMode === 'smart'
              ? '输入关键词，例如：B站 点赞、抖音 真人 赞'
              : '搜索各平台的同类或相似商品'
          "
          maxlength="160"
          aria-label="搜索关联商品"
        />
      </div>
      <div v-if="searchMode === 'smart' && matchedKeywords.length" class="match-explanation">
        <span>已识别：</span
        ><span
          v-for="keyword in matchedKeywords"
          :key="keyword.label"
          class="match-keyword"
          :title="keyword.variants.join('、')"
          >{{ keyword.label }}</span
        >
        <p>
          匹配 {{ candidateTotal }} 条商品<span v-if="matchingPlatforms.length"
            >，来自 {{ matchingPlatforms.length }} 个交易平台</span
          >；按匹配程度排列。
        </p>
      </div>
      <div class="candidate-bulk-tools">
        <button
          type="button"
          class="btn btn-white btn-sm"
          :disabled="searching || bulkSelecting || saving || !candidates.length"
          @click="selectCurrentPage"
        >
          选中本页可关联项
        </button>
        <button
          type="button"
          class="text-link"
          :disabled="searching || bulkSelecting || saving || !candidates.length"
          @click="removeCurrentPage"
        >
          取消本页选择
        </button>
        <button
          v-if="searchMode === 'smart' && search.trim()"
          type="button"
          class="btn btn-white btn-sm"
          :disabled="
            searching || bulkSelecting || saving || !candidateTotal || picked.length >= maxOffers
          "
          @click="selectAllMatching"
        >
          {{ bulkSelecting ? '正在准备全部结果…' : '选择全部匹配结果' }}
        </button>
        <button v-if="bulkSelecting" type="button" class="text-link" @click="invalidateBulk">
          取消批量准备
        </button>
      </div>
      <p v-if="selectionNotice" class="selection-notice" role="status">{{ selectionNotice }}</p>
      <div v-if="candidateError" class="notice error">
        {{ candidateError }}<button type="button" class="text-link" @click="find">重新搜索</button>
      </div>
      <div class="product-picker">
        <p v-if="searching" class="muted">正在搜索…</p>
        <button
          v-for="product in candidates"
          v-else
          :key="product.id"
          type="button"
          class="picker-item"
          :class="{ selected: picked.some((item) => item.id === product.id) }"
          :disabled="candidateConflict(product) || saving || bulkSelecting"
          @click="toggle(product)"
        >
          <span class="picker-check"
            ><Icon v-if="picked.some((item) => item.id === product.id)" name="check" :size="13"
          /></span>
          <div>
            <strong>{{ product.name }}</strong
            ><small
              >{{ product.platform_name }} · #{{ product.source_id }} ·
              {{ product.category_name || '未分类' }}</small
            ><small>{{
              candidateConflict(product)
                ? '已关联其他统一商品，需先解除原关联'
                : displayBasisLabel(product.display_quantity, product.unit)
            }}</small>
            <div v-if="product.match_reasons?.length" class="candidate-match-reasons">
              {{ product.match_reasons.join(' · ') }}
            </div>
            <div v-if="product.match_warnings?.length" class="candidate-match-warning">
              {{ product.match_warnings.join('；') }}
            </div>
          </div>
          <span class="mono">{{ price(product.price, product.currency) }}</span>
        </button>
        <p v-if="!searching && !candidates.length" class="muted">
          暂无符合条件的平台商品，请调整平台、类目或关键词。
        </p>
      </div>
      <Pagination
        v-if="candidateTotal"
        :page="candidatePage"
        :total="candidateTotal"
        :page-size="30"
        @change="changeCandidatePage"
      />
      <div class="modal-actions">
        <button type="button" class="btn btn-white" :disabled="saving" @click="closeForm">
          取消</button
        ><button class="btn btn-primary" :disabled="saving || bulkSelecting">
          {{ saving ? '保存中…' : '保存商品' }}
        </button>
      </div>
    </form></Modal
  >
  <Modal
    v-if="showCategoryForm"
    :title="
      editingCategory ? '编辑比价类目' : categoryForm.parent_id ? '新增子类目' : '新增一级类目'
    "
    @close="!categorySaving && (showCategoryForm = false)"
    ><form @submit.prevent="saveCategory">
      <div v-if="categoryFormError" class="notice error">{{ categoryFormError }}</div>
      <label
        >上级类目<select v-model="categoryForm.parent_id" aria-label="上级比价类目">
          <option value="">无上级 · 一级类目</option>
          <option v-for="item in categoryParentOptions" :key="item.id" :value="String(item.id)">
            {{ categoryLabel(item) }}
          </option>
        </select></label
      >
      <label
        >类目名称<input
          v-model="categoryForm.name"
          required
          maxlength="120"
          :placeholder="
            categoryForm.parent_id ? '例如：视频、直播' : '例如：抖音、快手、小红书、哔哩哔哩'
          " /></label
      ><label
        >类目说明<textarea
          v-model="categoryForm.description"
          rows="2"
          maxlength="2000"
          placeholder="说明此类目收录的商品范围"
        /></label
      ><label
        >排序<input
          v-model.number="categoryForm.sort_order"
          type="number"
          min="-100000"
          max="100000"
          step="1"
      /></label>
      <div class="modal-actions">
        <button
          type="button"
          class="btn btn-white"
          :disabled="categorySaving"
          @click="showCategoryForm = false"
        >
          取消</button
        ><button class="btn btn-primary" :disabled="categorySaving">
          {{ categorySaving ? '保存中…' : '保存类目' }}
        </button>
      </div>
    </form></Modal
  >
  <Modal
    v-if="deletingCategory"
    title="删除空类目"
    @close="!categorySaving && (deletingCategory = null)"
    ><div v-if="categoryFormError" class="notice error">{{ categoryFormError }}</div>
    <p>确认删除“{{ categoryLabel(deletingCategory) }}”？包含下级类目或统一商品时需要先移动它们。</p>
    <div class="modal-actions">
      <button
        type="button"
        class="btn btn-white"
        :disabled="categorySaving"
        @click="deletingCategory = null"
      >
        取消</button
      ><button
        type="button"
        class="btn btn-danger"
        :disabled="categorySaving"
        @click="removeCategory"
      >
        {{ categorySaving ? '删除中…' : '删除类目' }}
      </button>
    </div></Modal
  >
  <Modal v-if="deleteGroup" title="删除统一商品" @close="!saving && (deleteGroup = null)"
    ><div v-if="formError" class="notice error">{{ formError }}</div>
    <p>确认删除“{{ deleteGroup.name }}”及其关联关系？原平台商品和价格历史会保留。</p>
    <div class="modal-actions">
      <button type="button" class="btn btn-white" :disabled="saving" @click="deleteGroup = null">
        取消</button
      ><button type="button" class="btn btn-danger" :disabled="saving" @click="remove">
        {{ saving ? '删除中…' : '删除商品' }}
      </button>
    </div></Modal
  >
</template>
<style scoped>
.form-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.form-grid > label {
  min-width: 0;
}
.form-grid select,
.form-grid input {
  min-width: 0;
  width: 100%;
  max-width: 100%;
}
@media (max-width: 580px) {
  .form-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
.smart-match-controls {
  background: #f6f8fc;
  border: 1px solid #e5ebf5;
  border-radius: 8px;
  padding: 14px;
  margin: 16px 0;
}
.smart-match-heading,
.picked-summary,
.candidate-bulk-tools {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.smart-match-heading {
  justify-content: space-between;
  margin-bottom: 12px;
  font-size: 13px;
}
.picked-summary {
  margin: 12px 0;
  font-size: 12px;
  color: #76869e;
}
.selected-product-scroll {
  max-height: 180px;
  overflow-y: auto;
  padding: 8px;
  border: 1px solid #edf1f7;
  border-radius: 6px;
}
.candidate-bulk-tools {
  margin: 14px 0;
  font-size: 12px;
}
.match-explanation {
  margin: 12px 0;
  font-size: 12px;
  color: #637896;
  line-height: 1.8;
}
.match-keyword {
  display: inline-block;
  background: #edf3ff;
  color: #4368ae;
  border-radius: 4px;
  padding: 2px 8px;
  margin: 0 5px 4px 0;
}
.candidate-match-reasons {
  color: #42699f;
  font-size: 11px;
  line-height: 1.6;
  margin-top: 5px;
}
.candidate-match-warning {
  color: #9b7134;
  font-size: 11px;
  line-height: 1.6;
  margin-top: 4px;
}
.selection-notice {
  font-size: 12px;
  color: #42699f;
  line-height: 1.8;
  margin: 10px 0;
}
.comparison-catalogue {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 22px;
  align-items: start;
}
.comparison-categories {
  padding: 18px 12px;
  min-width: 0;
}
.comparison-groups,
.comparison-grid,
.mapping-card {
  min-width: 0;
}
.category-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 8px;
}
.category-heading h2,
.catalogue-toolbar h2 {
  font-size: 14px;
  color: #536680;
  margin: 0;
}
.category-caption,
.category-empty {
  font-size: 11px;
  line-height: 1.8;
  color: #8a99af;
  padding: 8px;
  margin: 0 0 8px;
}
.category-link {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  border: 0;
  border-radius: 6px;
  background: transparent;
  text-align: left;
  color: #657a97;
  font-size: 12px;
  padding: 11px 10px;
  cursor: pointer;
  white-space: nowrap;
}
.category-link.selected {
  background: #edf3ff;
  color: #476aaf;
  font-weight: 600;
}
.category-link small {
  font-size: 10px;
  color: #8699b7;
  flex-shrink: 0;
}
.category-entry {
  display: flex;
  align-items: center;
  padding-left: calc(var(--category-depth, 0) * 14px);
}
.category-toggle-spacer {
  width: 24px;
  flex-shrink: 0;
}
.category-toggle {
  color: #778ca9;
}
.category-entry .category-link {
  flex: 1;
  min-width: 0;
}
.category-entry .category-link span {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.category-entry .icon-btn {
  width: 24px;
  height: 28px;
  flex-shrink: 0;
}
.category-create {
  margin: 14px 8px 0;
}
.catalogue-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 15px;
  margin: 0 0 18px;
}
.catalogue-toolbar p {
  font-size: 11px;
  color: #91a0b6;
  margin: 7px 0 0;
}
.catalogue-toolbar .search-input {
  width: 230px;
  max-width: 100%;
}
.comparison-grid {
  grid-template-columns: repeat(auto-fit, minmax(min(380px, 100%), 1fr));
  gap: 18px;
}
.comparison-grid .mapping-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  align-self: start;
}
.comparison-card-head {
  padding: 20px 22px 16px;
}
.comparison-card-top {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  margin-bottom: 10px;
  font-size: 11px;
}
.comparison-category {
  color: #657b9b;
  min-width: 0;
  overflow-wrap: anywhere;
}
.comparison-mode {
  color: #7285a0;
  flex-shrink: 0;
  background: #f3f6fa;
  border-radius: 4px;
  padding: 3px 7px;
}
.comparison-mode.pending {
  color: #876b37;
  background: #fbf6ea;
}
.comparison-name {
  font-size: 17px;
  line-height: 1.5;
  margin: 0;
  color: #4f6283;
  overflow-wrap: anywhere;
}
.comparison-description {
  color: #7c8da5;
  font-size: 12px;
  line-height: 1.8;
  margin: 10px 0;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  overflow-wrap: anywhere;
}
.comparison-counts {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  color: #7587a1;
  font-size: 12px;
}
.comparison-counts strong {
  color: #536b8c;
  font-size: 13px;
  font-weight: 600;
}
.comparison-counts > span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 8px;
  background: #f5f7fb;
  border-radius: 4px;
}
.comparison-previews {
  padding: 0 22px;
  border-top: 1px solid #edf1f6;
}
.platform-preview {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 13px 0;
}
.platform-preview + .platform-preview {
  border-top: 1px solid #f0f3f8;
}
.preview-platform {
  min-width: 0;
}
.preview-platform strong {
  display: block;
  font-size: 12px;
  color: #516783;
  line-height: 1.7;
}
.preview-range {
  text-align: right;
  color: #526d95;
  font-size: 12px;
  min-width: 0;
}
.preview-range .mono {
  display: block;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.comparison-previews small {
  display: block;
  margin-top: 4px;
  font-size: 10px;
  line-height: 1.6;
  font-weight: 400;
  color: #91a0b5;
}
.preview-more {
  margin: 0;
  padding: 4px 0 14px;
  font-size: 11px;
  color: #8797af;
}
.comparison-grid .mapping-actions {
  padding: 14px 22px;
  flex-wrap: wrap;
}
.comparison-grid .mapping-actions .btn {
  min-height: 34px;
}
.unlinked-description {
  margin: 0;
  padding: 0 22px 18px;
  font-size: 12px;
  line-height: 1.8;
  color: #8a9bb2;
}
.is-unlinked .comparison-card-head {
  padding-bottom: 10px;
}
.is-unlinked .comparison-description {
  display: none;
}
.category-mobile-summary {
  display: none;
}
.association-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 20px;
}
.association-heading h3 {
  font-size: 13px;
  margin: 0;
}
.association-heading span {
  color: #8b9bb1;
  font-size: 11px;
}
.picker-item:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
@media (max-width: 1100px) {
  .comparison-catalogue {
    grid-template-columns: minmax(0, 1fr);
    gap: 15px;
  }
  .comparison-categories {
    padding: 16px;
  }
  .category-heading {
    padding: 0;
  }
  .category-mobile-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 15px;
    width: 100%;
    border: 0;
    padding: 12px 0 0;
    background: transparent;
    color: #647b9e;
    font-size: 12px;
    text-align: left;
    cursor: pointer;
  }
  .category-mobile-summary > span:first-child {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .category-mobile-summary > span:last-child {
    display: flex;
    align-items: center;
    gap: 7px;
    white-space: nowrap;
  }
  .category-body {
    display: none;
  }
  .category-body.is-open {
    display: block;
    padding-top: 10px;
    max-height: 420px;
    overflow-y: auto;
  }
  .category-entry .category-link {
    padding-top: 9px;
    padding-bottom: 9px;
  }
  .comparison-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 680px) {
  .comparison-catalogue {
    grid-template-columns: minmax(0, 1fr);
  }
  .catalogue-toolbar {
    align-items: start;
    flex-direction: column;
  }
  .catalogue-toolbar .search-input {
    width: 100%;
  }
  .comparison-card-head {
    padding: 17px 16px 14px;
  }
  .comparison-previews {
    padding: 0 16px;
  }
  .comparison-grid .mapping-actions {
    padding: 12px 16px;
    gap: 8px;
  }
  .unlinked-description {
    padding: 0 16px 15px;
  }
  .platform-preview {
    gap: 10px;
    align-items: flex-start;
  }
  .preview-platform {
    flex: 1;
  }
  .preview-range {
    max-width: 55%;
  }
}
</style>
