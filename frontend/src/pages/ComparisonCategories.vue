<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api, auth, itemsOf, json } from '../api'
import {
  categoryPath,
  categoryPathName,
  categoryTreeRows,
  descendantCategoryIds,
} from '../categoryTree'
import type { ComparisonCategoryNode } from '../categoryTree'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/EmptyState.vue'
import Modal from '../components/Modal.vue'

interface Category extends ComparisonCategoryNode {
  description?: string
  direct_product_count?: number
  offer_count?: number
  platform_count?: number
}
type FormMode = 'create' | 'edit' | 'move' | 'sort'

const categories = ref<Category[]>([])
const loading = ref(true)
const error = ref('')
const notice = ref('')
const query = ref('')
const collapsedCategories = ref(new Set<number>())
const saving = ref(false)
const showForm = ref(false)
const formMode = ref<FormMode>('create')
const editing = ref<Category | null>(null)
const formError = ref('')
const deletingCategory = ref<Category | null>(null)
const deleteError = ref('')
const lastSavedId = ref<number | null>(null)
const form = reactive({ name: '', description: '', sort_order: 0, parent_id: '' })
const admin = computed(() => auth.user?.role === 'admin')
const maxLevels = 8
let disposed = false
let requestNumber = 0
let controller: AbortController | undefined

function label(category: Category) {
  return categoryPathName(category, categories.value)
}
const fullTree = computed(() => categoryTreeRows(categories.value))
const rootsCount = computed(() => categories.value.filter((item) => !item.parent_id).length)
const visibleRows = computed(() => {
  const keyword = query.value.trim().toLocaleLowerCase()
  if (!keyword) return categoryTreeRows(categories.value, collapsedCategories.value)
  const visible = new Set<number>()
  for (const category of categories.value)
    if (label(category).toLocaleLowerCase().includes(keyword))
      for (const ancestor of categoryPath(category, categories.value)) visible.add(ancestor.id)
  return fullTree.value.filter((item) => visible.has(item.id))
})
const parentOptions = computed(() => {
  const excluded = editing.value
    ? descendantCategoryIds(categories.value, editing.value.id)
    : new Set<number>()
  const subtreeHeight = editing.value
    ? Math.max(
        1,
        ...categories.value
          .filter((item) => excluded.has(item.id))
          .map(
            (item) =>
              categoryPath(item, categories.value).length -
              categoryPath(editing.value!, categories.value).length +
              1,
          ),
      )
    : 1
  return fullTree.value.filter(
    (item) =>
      !excluded.has(item.id) &&
      categoryPath(item, categories.value).length + subtreeHeight <= maxLevels,
  )
})
const dialogTitle = computed(
  () =>
    ({
      create: form.parent_id ? '新增子类目' : '新增一级类目',
      edit: '编辑比价类目',
      move: '移动比价类目',
      sort: '调整类目排序',
    })[formMode.value],
)
const currentPath = computed(() => (editing.value ? label(editing.value) : ''))
const parentPath = computed(() => {
  const parent = categories.value.find((item) => String(item.id) === form.parent_id)
  return parent ? label(parent) : '一级类目'
})
const canManage = computed(() => admin.value && !loading.value && !error.value && !saving.value)

async function load() {
  const number = ++requestNumber
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  error.value = ''
  if (!admin.value) {
    loading.value = false
    categories.value = []
    error.value = '需要管理员权限。'
    return
  }
  try {
    const result = await api('/comparison-categories', { signal: controller.signal })
    if (number === requestNumber && !disposed) {
      categories.value = itemsOf<Category>(result)
      collapsedCategories.value = new Set(
        [...collapsedCategories.value].filter((id) =>
          categories.value.some((item) => item.id === id),
        ),
      )
    }
  } catch (e: any) {
    if (number === requestNumber && !disposed) {
      categories.value = []
      error.value = e.message
    }
  } finally {
    if (number === requestNumber && !disposed) loading.value = false
  }
}
onMounted(load)
onBeforeUnmount(() => {
  disposed = true
  requestNumber++
  controller?.abort()
})

function toggleCategory(id: number) {
  const next = new Set(collapsedCategories.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  collapsedCategories.value = next
}
function expandAll() {
  collapsedCategories.value = new Set()
}
function collapseAll() {
  collapsedCategories.value = new Set(
    categories.value.filter((item) => hasChildren(item)).map((item) => item.id),
  )
}
function hasChildren(category: Category) {
  return Boolean(
    category.children_count || categories.value.some((item) => item.parent_id === category.id),
  )
}
function cannotDelete(category: Category) {
  return Boolean(hasChildren(category) || category.product_count || category.direct_product_count)
}
function deleteReason(category: Category) {
  return hasChildren(category)
    ? '请先移走或删除子类目'
    : category.product_count || category.direct_product_count
      ? '请先移走该类目中的商品'
      : '删除空类目'
}
function canAddChild(category: Category) {
  return categoryPath(category, categories.value).length < maxLevels
}
function fillForm(category: Category | null) {
  editing.value = category
  Object.assign(form, {
    name: category?.name ?? '',
    description: category?.description ?? '',
    sort_order: category?.sort_order ?? 0,
    parent_id: category?.parent_id ? String(category.parent_id) : '',
  })
  formError.value = ''
  showForm.value = true
}
function openCreate(parent?: Category) {
  if (!canManage.value || (parent && !canAddChild(parent))) return
  formMode.value = 'create'
  fillForm(null)
  form.parent_id = parent ? String(parent.id) : ''
}
function openEdit(category: Category) {
  if (!canManage.value) return
  formMode.value = 'edit'
  fillForm(category)
}
function openMove(category: Category) {
  if (!canManage.value) return
  formMode.value = 'move'
  fillForm(category)
}
function openSort(category: Category) {
  if (!canManage.value) return
  formMode.value = 'sort'
  fillForm(category)
}
function closeForm() {
  if (saving.value) return
  showForm.value = false
  formError.value = ''
}
async function save() {
  if (!admin.value || saving.value || !showForm.value || disposed) return
  formError.value = ''
  if (!form.name.trim()) {
    formError.value = '请填写类目名称。'
    return
  }
  if (
    !Number.isInteger(form.sort_order) ||
    form.sort_order < -1000000 ||
    form.sort_order > 1000000
  ) {
    formError.value = '排序须为 -1000000 到 1000000 之间的整数。'
    return
  }
  if (form.parent_id && !parentOptions.value.some((item) => String(item.id) === form.parent_id)) {
    formError.value = '请选择有效的上级类目，不能移入自身、后代或超过八层的分支。'
    return
  }
  saving.value = true
  try {
    const item = await api(
      editing.value ? `/comparison-categories/${editing.value.id}` : '/comparison-categories',
      json(editing.value ? 'PUT' : 'POST', {
        name: form.name.trim(),
        description: form.description,
        sort_order: form.sort_order,
        parent_id: form.parent_id ? Number(form.parent_id) : null,
      }),
    )
    if (disposed) return
    showForm.value = false
    lastSavedId.value = item.id
    notice.value =
      formMode.value === 'move'
        ? '类目已移动，子类目与商品关联已保留。'
        : formMode.value === 'sort'
          ? '排序已保存，同级类目按数值从小到大显示。'
          : '类目已保存。'
    await load()
    if (!disposed) {
      const saved = categories.value.find((category) => category.id === item.id)
      if (saved) {
        const next = new Set(collapsedCategories.value)
        for (const ancestor of categoryPath(saved, categories.value)) next.delete(ancestor.id)
        collapsedCategories.value = next
      }
    }
  } catch (e: any) {
    if (!disposed) formError.value = e.message
  } finally {
    if (!disposed) saving.value = false
  }
}
function confirmRemove(category: Category) {
  if (!canManage.value || cannotDelete(category)) return
  deletingCategory.value = category
  deleteError.value = ''
}
async function remove() {
  if (!admin.value || saving.value || !deletingCategory.value || disposed) return
  const current = categories.value.find((item) => item.id === deletingCategory.value!.id)
  if (!current || cannotDelete(current)) {
    deleteError.value = '该类目包含子类目或商品，请先刷新并移走内容。'
    return
  }
  saving.value = true
  deleteError.value = ''
  try {
    await api(`/comparison-categories/${current.id}`, json('DELETE'))
    if (disposed) return
    deletingCategory.value = null
    notice.value = '空类目已删除。'
    if (lastSavedId.value === current.id) lastSavedId.value = null
    await load()
  } catch (e: any) {
    if (!disposed) deleteError.value = e.message
  } finally {
    if (!disposed) saving.value = false
  }
}
</script>

<template>
  <div class="category-admin-tools">
    <p>维护比价类目层级、名称和显示顺序，最多支持八层。</p>
    <div>
      <button type="button" class="btn btn-white" :disabled="loading || saving" @click="load">
        <Icon name="refresh" :size="15" />刷新类目</button
      ><button type="button" class="btn btn-primary" :disabled="!canManage" @click="openCreate()">
        <Icon name="plus" :size="16" />新增一级类目
      </button>
    </div>
  </div>
  <div v-if="error" role="alert" class="notice error">{{ error }}</div>
  <div v-if="notice" role="status" class="notice success">{{ notice }}</div>
  <section class="panel category-admin-panel">
    <div class="category-admin-filters">
      <div class="search-input">
        <Icon name="search" :size="16" /><input
          v-model="query"
          aria-label="搜索比价类目"
          placeholder="搜索类目名称或完整路径"
        />
      </div>
      <div class="tree-actions">
        <button
          type="button"
          class="text-link"
          :disabled="loading || !categories.length || Boolean(query.trim())"
          @click="expandAll"
        >
          展开全部</button
        ><button
          type="button"
          class="text-link"
          :disabled="loading || !categories.length || Boolean(query.trim())"
          @click="collapseAll"
        >
          收起全部
        </button>
      </div>
      <span class="category-admin-summary"
        >{{ categories.length }} 个类目 · {{ rootsCount }} 个一级类目</span
      >
    </div>
    <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载比价类目…</div>
    <div v-else-if="visibleRows.length" class="category-admin-table-wrap">
      <table class="category-admin-table">
        <thead>
          <tr>
            <th>类目 / 完整路径</th>
            <th>商品数量</th>
            <th>同级排序</th>
            <th>管理操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in visibleRows"
            :key="item.id"
            :class="{ 'recently-saved': item.id === lastSavedId }"
          >
            <td class="category-identity" :style="{ '--depth': Math.min(item.depth, 5) }">
              <div class="category-tree-name">
                <button
                  v-if="item.hasChildren"
                  type="button"
                  class="icon-btn category-toggle"
                  :aria-label="`${collapsedCategories.has(item.id) ? '展开' : '收起'}类目 ${label(item)}`"
                  :aria-expanded="Boolean(query.trim()) || !collapsedCategories.has(item.id)"
                  :disabled="Boolean(query.trim())"
                  @click="toggleCategory(item.id)"
                >
                  <Icon
                    :name="collapsedCategories.has(item.id) && !query.trim() ? 'arrow' : 'down'"
                    :size="13"
                  /></button
                ><span v-else class="category-toggle-placeholder"></span
                ><strong>{{ item.name }}</strong
                ><span class="category-level">{{ item.depth + 1 }} 级</span>
              </div>
              <div class="category-full-path">{{ label(item) }}</div>
              <p v-if="item.description" class="category-row-description" :title="item.description">
                {{ item.description }}
              </p>
            </td>
            <td class="category-counts" data-label="商品数量">
              <strong>{{ item.direct_product_count ?? 0 }}</strong> 直属<span
                >{{ item.product_count ?? 0 }} 个分支商品</span
              >
            </td>
            <td data-label="同级排序" class="category-order">
              <span class="mono">{{ item.sort_order ?? 0 }}</span
              ><button
                type="button"
                class="text-link"
                :disabled="!canManage"
                @click="openSort(item)"
              >
                调整
              </button>
            </td>
            <td class="category-row-actions">
              <button
                type="button"
                class="text-link"
                :disabled="!canManage || !canAddChild(item)"
                :title="canAddChild(item) ? '新增下一级类目' : '已达到八层上限'"
                @click="openCreate(item)"
              >
                新增子类目</button
              ><button
                type="button"
                class="text-link"
                :disabled="!canManage"
                @click="openEdit(item)"
              >
                编辑</button
              ><button
                type="button"
                class="text-link"
                :disabled="!canManage"
                @click="openMove(item)"
              >
                移动</button
              ><RouterLink
                :to="{ path: '/admin/compare', query: { category_id: item.id } }"
                class="text-link"
                >查看商品</RouterLink
              ><button
                type="button"
                class="text-link danger-text"
                :disabled="!canManage || cannotDelete(item)"
                :title="deleteReason(item)"
                @click="confirmRemove(item)"
              >
                删除
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <EmptyState
      v-else
      icon="box"
      :title="query.trim() ? '没有找到匹配的类目' : '尚未建立比价类目'"
      :description="
        query.trim()
          ? '搜索名称或路径，例如“抖音 / 视频”。'
          : '先建立一级类目，再添加视频、直播等子类目。'
      "
      ><button
        v-if="!query.trim() && admin"
        type="button"
        class="btn btn-primary btn-sm"
        :disabled="!canManage"
        @click="openCreate()"
      >
        新增一级类目
      </button></EmptyState
    >
    <p class="category-admin-help">
      同级类目按排序数值从小到大显示；相同数值按创建先后显示。包含子类目或商品的类目需要先移走内容才能删除。
    </p>
  </section>
  <Modal v-if="showForm" :title="dialogTitle" @close="closeForm">
    <form @submit.prevent="save">
      <div v-if="formError" role="alert" class="notice error">{{ formError }}</div>
      <p v-if="editing" class="category-current-path">当前路径：{{ currentPath }}</p>
      <label v-if="formMode === 'create' || formMode === 'edit'"
        >类目名称<input
          v-model="form.name"
          required
          maxlength="120"
          autocomplete="off"
          :disabled="saving"
          placeholder="例如：抖音、视频、直播"
      /></label>
      <label v-if="formMode !== 'sort'"
        >上级类目<select v-model="form.parent_id" aria-label="上级比价类目" :disabled="saving">
          <option value="">无上级，作为一级类目</option>
          <option v-for="item in parentOptions" :key="item.id" :value="String(item.id)">
            {{ label(item) }}
          </option>
        </select></label
      >
      <p v-if="formMode === 'move'" class="form-help">
        移动到：{{ parentPath }}。此类目的子类目会一起移动，商品关联保持不变。
      </p>
      <label v-if="formMode === 'create' || formMode === 'edit'"
        >类目说明<textarea
          v-model="form.description"
          rows="3"
          maxlength="4000"
          :disabled="saving"
          placeholder="说明这个分支收录的商品范围"
        />
      </label>
      <label v-if="formMode !== 'move'"
        >同级排序<input
          v-model.number="form.sort_order"
          type="number"
          required
          min="-1000000"
          max="1000000"
          step="1"
          :disabled="saving"
      /></label>
      <p v-if="formMode === 'sort'" class="form-help">
        数值越小越靠前，仅影响同一上级下的类目顺序。
      </p>
      <div class="modal-actions">
        <button type="button" class="btn btn-white" :disabled="saving" @click="closeForm">
          取消</button
        ><button class="btn btn-primary" :disabled="saving || !admin">
          {{ saving ? '保存中…' : formMode === 'move' ? '确认移动' : '保存类目' }}
        </button>
      </div>
    </form>
  </Modal>
  <Modal v-if="deletingCategory" title="删除空类目" @close="!saving && (deletingCategory = null)">
    <div v-if="deleteError" role="alert" class="notice error">{{ deleteError }}</div>
    <p class="delete-category-message">确认删除“{{ label(deletingCategory) }}”？</p>
    <div class="modal-actions">
      <button
        type="button"
        class="btn btn-white"
        :disabled="saving"
        @click="deletingCategory = null"
      >
        取消</button
      ><button type="button" class="btn btn-danger" :disabled="saving || !admin" @click="remove">
        {{ saving ? '删除中…' : '删除类目' }}
      </button>
    </div>
  </Modal>
</template>

<style scoped>
.category-admin-tools,
.category-admin-tools > div {
  display: flex;
  align-items: center;
  gap: 12px;
}
.category-admin-tools {
  justify-content: space-between;
  flex-wrap: wrap;
  margin-bottom: 20px;
}
.category-admin-tools > p {
  color: #72829a;
  font-size: 12px;
  line-height: 1.8;
}
.category-admin-filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 18px;
  padding: 18px 20px;
  border-bottom: 1px solid #edf1f7;
}
.category-admin-filters .search-input {
  width: 310px;
  max-width: 100%;
}
.tree-actions {
  display: flex;
  gap: 16px;
}
.category-admin-summary {
  font-size: 12px;
  color: #78899e;
  margin-left: auto;
}
.category-admin-table-wrap {
  overflow-x: auto;
}
.category-admin-table {
  width: 100%;
  table-layout: fixed;
}
.category-admin-table th:first-child {
  width: 39%;
}
.category-admin-table th:nth-child(2) {
  width: 16%;
}
.category-admin-table th:nth-child(3) {
  width: 12%;
}
.category-admin-table th:nth-child(4) {
  width: 33%;
}
.category-admin-table td,
.category-admin-table th {
  padding: 16px 20px;
}
.category-admin-table td {
  white-space: normal;
}
.category-admin-table tr.recently-saved {
  background: #f4f8ff;
}
.category-identity {
  padding-left: calc(16px + var(--depth) * 16px) !important;
}
.category-tree-name {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.category-tree-name strong {
  font-size: 13px;
  color: #354d6f;
  overflow-wrap: anywhere;
}
.category-toggle {
  width: 22px;
  height: 22px;
  flex: 0 0 22px;
}
.category-toggle-placeholder {
  display: block;
  width: 22px;
  flex: 0 0 22px;
}
.category-level {
  font-size: 10px;
  color: #8a9ab0;
  white-space: nowrap;
}
.category-full-path {
  font-size: 11px;
  color: #8797ae;
  line-height: 1.7;
  margin: 5px 0 0 30px;
  overflow-wrap: anywhere;
}
.category-row-description {
  font-size: 11px;
  line-height: 1.7;
  margin: 5px 0 0 30px;
  color: #72829a;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.category-counts {
  font-size: 12px;
  line-height: 1.7;
}
.category-counts strong {
  font-weight: 600;
}
.category-counts span {
  display: block;
  font-size: 11px;
  color: #8797ae;
  margin-top: 4px;
}
.category-order .mono {
  display: block;
  margin-bottom: 5px;
  font-size: 12px;
}
.category-row-actions {
  line-height: 2;
}
.category-row-actions > * {
  display: inline-flex;
  font-size: 11px;
  margin-right: 12px;
  white-space: nowrap;
}
.category-row-actions button:disabled {
  color: #b5bfce;
  cursor: not-allowed;
}
.category-admin-help {
  font-size: 11px;
  color: #8a9ab0;
  line-height: 1.8;
  padding: 14px 20px;
  border-top: 1px solid #edf1f7;
}
.category-current-path {
  font-size: 12px;
  line-height: 1.8;
  color: #72829a;
  margin-bottom: 16px;
  overflow-wrap: anywhere;
}
.delete-category-message {
  font-size: 13px;
  line-height: 1.8;
  overflow-wrap: anywhere;
}
@media (max-width: 1000px) {
  .category-admin-table,
  .category-admin-table tbody,
  .category-admin-table tr,
  .category-admin-table td {
    display: block;
    width: auto;
  }
  .category-admin-table thead {
    display: none;
  }
  .category-admin-table tbody {
    padding: 0 16px;
  }
  .category-admin-table tr {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(90px, 0.45fr);
    gap: 0 12px;
    padding: 15px 0;
    border-bottom: 1px solid #edf1f7;
  }
  .category-admin-table tr:last-child {
    border-bottom: 0;
  }
  .category-admin-table td {
    padding: 8px 0;
    border: 0;
    min-width: 0;
  }
  .category-admin-table .category-identity {
    grid-column: 1 / -1;
    padding-left: calc(var(--depth) * 12px) !important;
  }
  .category-admin-table .category-row-actions {
    grid-column: 1 / -1;
    padding-top: 10px;
  }
  .category-admin-table td[data-label]::before {
    content: attr(data-label);
    display: block;
    margin-bottom: 6px;
    font-size: 10px;
    color: #8a9ab0;
  }
  .category-order .mono {
    display: inline-block;
    margin-right: 12px;
  }
  .category-admin-summary {
    margin-left: 0;
  }
}
@media (max-width: 540px) {
  .category-admin-tools > div {
    width: 100%;
    justify-content: flex-end;
    flex-wrap: wrap;
  }
  .category-admin-filters {
    gap: 12px;
    padding: 15px;
  }
  .category-admin-filters .search-input {
    width: 100%;
  }
  .category-admin-summary {
    margin-left: auto;
    font-size: 11px;
  }
  .category-admin-table tbody {
    padding: 0 12px;
  }
  .category-admin-table .category-identity {
    padding-left: calc(var(--depth) * 7px) !important;
  }
  .category-row-actions > * {
    margin-right: 14px;
    font-size: 12px;
  }
}
</style>
