<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, auth, json, appVersion, loadVersion } from './api'
import Icon from './components/Icon.vue'
import { isAdminPath, isLoginPath } from './navigation'
import { assetLoadFailed, assetRecoveryMessage, refreshAfterAssetFailure } from './assetRecovery'
const route = useRoute(),
  router = useRouter(),
  mobile = ref(false),
  version = ref(0),
  logoutError = ref('')
const adminArea = computed(() => Boolean(route.meta.admin))
const loginPage = computed(() => isLoginPath(route.path))
const navigation = computed(() =>
  adminArea.value
    ? [
        { to: '/admin', name: '后台总览', icon: 'dashboard' },
        { to: '/admin/products', name: '商品管理', icon: 'box' },
        { to: '/admin/platforms', name: '平台管理', icon: 'platforms' },
        { to: '/admin/runs', name: '采集任务', icon: 'clock' },
        { to: '/admin/events', name: '事件管理', icon: 'bell' },
        { to: '/admin/compare', name: '全网比价', icon: 'globe' },
        { to: '/admin/comparison-categories', name: '比价类目管理', icon: 'menu' },
        { to: '/admin/platform-submissions', name: '平台收录审核', icon: 'file' },
        { to: '/admin/ai-analysis', name: 'AI 数据验证', icon: 'shield' },
      ]
    : [
        { to: '/', name: '价格总览', icon: 'dashboard' },
        { to: '/products', name: '商品中心', icon: 'box' },
        { to: '/events', name: '价格事件', icon: 'bell' },
        { to: '/compare', name: '全网比价', icon: 'globe' },
        { to: '/platform-submissions', name: '提交平台收录', icon: 'plus' },
      ],
)
function activeNavigation(path: string) {
  return path === '/' || path === '/admin' ? route.path === path : route.path.startsWith(path)
}
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
    if (!user && auth.initialized && !isLoginPath(route.path))
      router.replace({
        path: isAdminPath(route.path) ? '/admin/login' : '/login',
        query: { redirect: route.fullPath },
      })
  },
)
async function logout() {
  try {
    const destination = adminArea.value ? '/admin/login' : '/login'
    await api('/auth/logout', json('POST'))
    auth.user = null
    auth.csrf = ''
    router.replace(destination)
  } catch (e: any) {
    logoutError.value = e.message
  }
}
</script>
<template>
  <template v-if="loginPage">
    <div
      v-if="assetLoadFailed"
      class="asset-recovery asset-recovery-login notice warning"
      role="alert"
    >
      <Icon name="alert" :size="19" />
      <p>{{ assetRecoveryMessage }}</p>
      <button type="button" class="btn btn-white" @click="refreshAfterAssetFailure">
        确认后刷新页面
      </button>
    </div>
    <RouterView />
  </template>
  <div v-else class="app-shell">
    <div v-if="mobile" class="sidebar-shade" @click="mobile = false"></div>
    <aside class="sidebar" :class="{ open: mobile }">
      <RouterLink :to="adminArea ? '/admin' : '/'" class="brand"
        ><span class="brand-mark"><Icon name="chart" :size="25" /></span>
        <div>
          PriceScope<small>{{ adminArea ? '管理后台' : '价格查看中心' }}</small>
        </div></RouterLink
      >
      <div class="nav-caption">{{ adminArea ? '管理工作空间' : '价格查看' }}</div>
      <nav>
        <RouterLink
          v-for="item in navigation"
          :key="item.to"
          :to="item.to"
          :class="{ active: activeNavigation(item.to) }"
          ><Icon :name="item.icon" /><span>{{ item.name }}</span
          ><span v-if="item.to === '/events'" class="nav-dot"></span
        ></RouterLink>
      </nav>
      <template v-if="adminArea"
        ><div class="nav-caption second">团队设置</div>
        <nav>
          <RouterLink
            to="/admin/settings/network"
            :class="{ active: route.path === '/admin/settings/network' }"
            ><Icon name="settings" /><span>系统设置</span></RouterLink
          >
          <RouterLink to="/admin/users" :class="{ active: route.path === '/admin/users' }"
            ><Icon name="users" /><span>成员管理</span></RouterLink
          >
          <RouterLink
            to="/admin/settings/update"
            :class="{ active: route.path === '/admin/settings/update' }"
            ><Icon name="refresh" /><span>系统更新</span></RouterLink
          >
        </nav></template
      >
      <nav class="nav-caption second">
        <RouterLink v-if="adminArea" to="/" class="text-link"
          ><Icon name="back" :size="16" />返回用户前台</RouterLink
        >
        <RouterLink v-else-if="auth.user?.role === 'admin'" to="/admin" class="text-link"
          ><Icon name="shield" :size="16" />进入管理后台</RouterLink
        >
      </nav>
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
          ><span>{{ adminArea ? '管理后台' : '用户前台' }}</span
          ><span class="breadcrumb-slash">/</span><strong>{{ route.meta.title }}</strong>
        </div>
        <div class="topbar-right">
          <span class="topbar-date">{{ today }}</span
          ><span class="timezone">UTC+8</span
          ><RouterLink
            :to="adminArea ? '/admin/events' : '/events'"
            class="icon-btn"
            aria-label="查看事件"
            ><Icon name="bell" :size="19"
          /></RouterLink>
        </div>
      </header>
      <main class="main-content">
        <div v-if="assetLoadFailed" class="asset-recovery notice warning" role="alert">
          <Icon name="alert" :size="19" />
          <p>{{ assetRecoveryMessage }}</p>
          <button type="button" class="btn btn-white" @click="refreshAfterAssetFailure">
            确认后刷新页面
          </button>
        </div>
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
        <span>PriceScope 价格观测平台</span><span>时间默认北京时间 · 日聚合按 UTC 自然日统计</span>
      </footer>
    </div>
  </div>
</template>
<style scoped>
.asset-recovery {
  align-items: center;
  gap: 12px;
}
.asset-recovery > p {
  flex: 1;
  min-width: 0;
  margin: 0;
}
.asset-recovery > svg {
  flex-shrink: 0;
}
.asset-recovery > button {
  flex-shrink: 0;
}
.asset-recovery-login {
  position: relative;
  z-index: 100;
  margin: 16px;
}
@media (max-width: 600px) {
  .asset-recovery {
    flex-wrap: wrap;
  }
  .asset-recovery > button {
    margin-left: 31px;
    white-space: normal;
  }
}
</style>
