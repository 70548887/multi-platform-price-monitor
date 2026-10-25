<script setup lang="ts">
import { onMounted, ref, reactive } from 'vue'
import { api, itemsOf, json, auth, date } from '../api'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/EmptyState.vue'
import Modal from '../components/Modal.vue'
const users = ref<any[]>([]),
  loading = ref(true),
  error = ref(''),
  notice = ref(''),
  showForm = ref(false),
  editing = ref<any>(null),
  saving = ref(false),
  formError = ref(''),
  form = reactive({ username: '', password: '', role: 'viewer', enabled: true })
async function load() {
  loading.value = true
  error.value = ''
  try {
    users.value = itemsOf(await api('/users'))
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
onMounted(load)
function open(user?: any) {
  editing.value = user ?? null
  Object.assign(form, {
    username: user?.username ?? '',
    password: '',
    role: user?.role ?? 'viewer',
    enabled: user?.enabled ?? user?.is_active ?? true,
  })
  formError.value = ''
  showForm.value = true
}
async function save() {
  saving.value = true
  formError.value = ''
  try {
    const payload = editing.value
      ? {
          role: form.role,
          enabled: form.enabled,
          ...(form.password ? { password: form.password } : {}),
        }
      : { username: form.username, password: form.password, role: form.role }
    await api(
      editing.value ? `/users/${editing.value.id}` : '/users',
      json(editing.value ? 'PATCH' : 'POST', payload),
    )
    showForm.value = false
    form.password = ''
    notice.value = editing.value ? '成员信息已更新。' : '团队成员已添加。'
    await load()
  } catch (e: any) {
    formError.value = e.message
  } finally {
    saving.value = false
  }
}
</script>
<template>
  <div class="page-tools">
    <div class="section-description">管理员维护采集配置，查看者浏览商品、走势与事件。</div>
    <button class="btn btn-primary" @click="open()">
      <Icon name="plus" :size="17" />添加团队成员
    </button>
  </div>
  <div v-if="error" class="notice error">{{ error }}</div>
  <div v-if="notice" class="notice success">{{ notice }}</div>
  <section class="panel">
    <div v-if="loading" class="loading-state"><span class="spinner"></span>正在加载成员…</div>
    <div v-else class="table-wrap">
      <table v-if="users.length">
        <thead>
          <tr>
            <th>团队成员</th>
            <th>访问角色</th>
            <th>账号状态</th>
            <th>创建时间</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="user in users" :key="user.id">
            <td>
              <div class="user-cell">
                <span class="avatar light">{{ user.username.slice(0, 1).toUpperCase() }}</span>
                <div>
                  <strong>{{ user.username }}</strong
                  ><span v-if="user.id === auth.user?.id" class="self-tag">当前账号</span>
                </div>
              </div>
            </td>
            <td>
              <span class="role-tag" :class="user.role"
                ><Icon :name="user.role === 'admin' ? 'shield' : 'eye'" :size="14" />{{
                  user.role === 'admin' ? '管理员' : '查看者'
                }}</span
              >
            </td>
            <td>
              <span
                class="badge"
                :class="(user.enabled ?? user.is_active ?? true) ? 'success' : 'neutral'"
                ><i></i>{{ (user.enabled ?? user.is_active ?? true) ? '已启用' : '已停用' }}</span
              >
            </td>
            <td>{{ date(user.created_at) }}</td>
            <td>
              <button class="text-link" @click="open(user)">
                <Icon name="edit" :size="15" />编辑
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <EmptyState
        v-else
        icon="users"
        title="暂无团队成员"
        description="添加成员并分配查看或管理权限。"
      />
    </div>
  </section>
  <Modal
    v-if="showForm"
    :title="editing ? '编辑团队成员' : '添加团队成员'"
    @close="!saving && (showForm = false)"
    ><form @submit.prevent="save">
      <div v-if="formError" class="notice error">{{ formError }}</div>
      <label
        >用户名<input
          v-model="form.username"
          required
          minlength="2"
          maxlength="80"
          pattern="[a-zA-Z0-9_.@-]+"
          autocomplete="off"
          :disabled="Boolean(editing)"
          placeholder="设置成员登录用户名" /></label
      ><label
        >{{ editing ? '重设密码（留空不修改）' : '初始密码'
        }}<input
          v-model="form.password"
          type="password"
          autocomplete="new-password"
          :required="!editing"
          minlength="12"
          maxlength="256"
          placeholder="至少 12 位字符" /></label
      ><label
        >访问角色<select v-model="form.role">
          <option value="viewer">查看者 · 浏览价格与趋势</option>
          <option value="admin">管理员 · 配置平台和管理成员</option>
        </select></label
      ><label v-if="editing" class="checkbox-label"
        ><input v-model="form.enabled" type="checkbox" />启用此账号</label
      >
      <p v-if="editing?.id === auth.user?.id" class="form-help">
        当前管理员账号不能自行停用或降级。
      </p>
      <div class="modal-actions">
        <button type="button" class="btn btn-white" :disabled="saving" @click="showForm = false">
          取消</button
        ><button class="btn btn-primary" :disabled="saving">
          {{ saving ? '保存中…' : '保存成员' }}
        </button>
      </div>
    </form></Modal
  >
</template>
