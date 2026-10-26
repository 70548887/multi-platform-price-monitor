<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, auth, json, appVersion, loadVersion } from './api'
import Icon from './components/Icon.vue'
const route = useRoute(),
  router = useRouter(),
  mobile = ref(false),
  version = ref(0),
  logoutError = ref('')
const navigation = [
  { to: '/', name: '监控总览', icon: 'dashboard' },
  { to: '/products', name: '商品中心', icon: 'box' },
  { to: '/platforms', name: '平台管理', icon: 'platforms' },
  { to: '/runs', name: '采集任务', icon: 'clock' },
  { to: '/events', name: '事件与提醒', icon: 'bell' },
  { to: '/mappings', name: '跨平台映射', icon: 'link' },
]
const today = computed(() =>
  new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'short',
  }).format(new Date()),
)
watch(
  () => route.path,
  () => {
    mobile.value = false
  },
)
watch(
  () => auth.user?.id,
  (id) => {
    if (id) loadVersion().catch(() => {})
    else appVersion.value = ''
  },
  { immediate: true },
)
watch(
  () => auth.user,
  (user) => {
    if (!user && auth.initialized && route.path !== '/login') router.replace('/login')
  },
)
async function logout() {
  try {
    await api('/auth/logout', json('POST'))
    auth.user = null
    auth.csrf = ''
    router.replace('/login')
  } catch (e: any) {
    logoutError.value = e.message
  }
}
</script>
<template>
  <RouterView v-if="route.path === '/login'" />
  <div v-else class="app-shell">
    <div v-if="mobile" class="sidebar-shade" @click="mobile = false"></div>
    <aside class="sidebar" :class="{ open: mobile }">
      <RouterLink to="/" class="brand"
        ><span class="brand-mark"><Icon name="chart" :size="25" /></span>
        <div>PriceScope<small>多平台价格观测</small></div></RouterLink
      >
      <div class="nav-caption">工作空间</div>
      <nav>
        <RouterLink
          v-for="item in navigation"
          :key="item.to"
          :to="item.to"
          :class="{ active: item.to === '/' ? route.path === '/' : route.path.startsWith(item.to) }"
          ><Icon :name="item.icon" /><span>{{ item.name }}</span
          ><span v-if="item.to === '/events'" class="nav-dot"></span
        ></RouterLink>
      </nav>
      <template v-if="auth.user?.role === 'admin'"
        ><div class="nav-caption second">团队设置</div>
        <nav>
          <RouterLink to="/settings/network" :class="{ active: route.path === '/settings/network' }"
            ><Icon name="settings" /><span>系统设置</span></RouterLink
          >
          <RouterLink to="/users" :class="{ active: route.path === '/users' }"
            ><Icon name="users" /><span>成员管理</span></RouterLink
          >
          <RouterLink to="/settings/update" :class="{ active: route.path === '/settings/update' }"
            ><Icon name="refresh" /><span>系统更新</span></RouterLink
          >
        </nav></template
      >
      <div class="sidebar-bottom">
        <div class="system-info">
          <span class="live-dot"></span>小时级价格观测<span class="version-label">{{
            appVersion.value ? `v${appVersion.value}` : '版本待读取'
          }}</span>
        </div>
        <div class="profile">
          <span class="avatar">{{ auth.user?.username?.slice(0, 1).toUpperCase() ?? 'U' }}</span>
          <div>
            <strong>{{ auth.user?.username }}</strong
            ><small>{{ auth.user?.role === 'admin' ? '工作空间管理员' : '工作空间查看者' }}</small>
          </div>
          <button class="sidebar-logout" aria-label="退出登录" title="退出登录" @click="logout">
            <Icon name="logout" :size="17" />
          </button>
        </div>
      </div>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <div class="breadcrumbs">
          <button class="icon-btn mobile-menu" aria-label="打开菜单" @click="mobile = true">
            <Icon name="menu" /></button
          ><span>工作空间</span><span class="breadcrumb-slash">/</span
          ><strong>{{ route.meta.title }}</strong>
        </div>
        <div class="topbar-right">
          <span class="topbar-date">{{ today }}</span
          ><span class="timezone">UTC+8</span
          ><RouterLink to="/events" class="icon-btn" aria-label="查看事件"
            ><Icon name="bell" :size="19"
          /></RouterLink>
        </div>
      </header>
      <main class="main-content">
        <div class="page-heading">
          <div>
            <div class="eyebrow">PRICE INTELLIGENCE</div>
            <h1>{{ route.meta.title }}</h1>
            <p>{{ route.meta.subtitle }}</p>
          </div>
          <button class="btn btn-white refresh-btn" @click="version++">
            <Icon name="refresh" :size="16" />刷新数据
          </button>
        </div>
        <div v-if="logoutError" class="notice error">{{ logoutError }}</div>
        <RouterView :key="`${route.fullPath}-${version}`" />
      </main>
      <footer class="app-footer">
        <span>PriceScope 价格观测平台</span><span>所有时间均为北京时间 · 价格以采样时刻为准</span>
      </footer>
    </div>
  </div>
</template>
