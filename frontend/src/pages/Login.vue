<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, auth, json, loadUser } from '../api'
import Icon from '../components/Icon.vue'
const username = ref(''),
  password = ref(''),
  busy = ref(false),
  error = ref(''),
  route = useRoute(),
  router = useRouter()
async function submit() {
  busy.value = true
  error.value = ''
  try {
    const result = await api(
      '/auth/login',
      json('POST', { username: username.value, password: password.value }),
    )
    auth.csrf = result.csrf_token ?? ''
    await loadUser()
    if (!auth.user) throw new Error('登录会话未能建立，请检查浏览器 Cookie 设置。')
    password.value = ''
    const dest = String(route.query.redirect ?? '/')
    await router.replace(dest.startsWith('/') && !dest.startsWith('//') ? dest : '/')
  } catch (e: any) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div class="login-page">
    <div class="login-brand brand">
      <span class="brand-mark"><Icon name="chart" :size="25" /></span>
      <div>PriceScope<small>多平台价格观测</small></div>
    </div>
    <section class="login-story">
      <span class="login-kicker">ONE WORKSPACE. EVERY PRICE.</span>
      <h1>洞察价格变化，<br />让每一次决策<br /><span>更有依据。</span></h1>
      <p>连接多个交易平台，沉淀每小时价格快照。<br />从实时比价到历史走势，在一处掌握全局。</p>
      <div class="login-graphic">
        <div class="graphic-grid"></div>
        <svg viewBox="0 0 500 160">
          <defs>
            <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#6691ff" stop-opacity=".35" />
              <stop offset="100%" stop-color="#6691ff" stop-opacity="0" />
            </linearGradient>
          </defs>
          <path
            d="M0 135L50 118L90 128L135 89L185 96L220 63L265 77L310 45L355 56L400 23L445 34L500 5V160H0Z"
            fill="url(#g)"
          />
          <path
            d="M0 135L50 118L90 128L135 89L185 96L220 63L265 77L310 45L355 56L400 23L445 34L500 5"
            fill="none"
            stroke="#739bff"
            stroke-width="3"
          />
        </svg>
        <div class="graphic-label"><span class="live-dot"></span>持续观测 · 完整追溯</div>
      </div>
    </section>
    <section class="login-form-section">
      <form class="login-form" @submit.prevent="submit">
        <div class="login-icon"><Icon name="shield" :size="26" /></div>
        <h2>登录工作空间</h2>
        <p class="muted">使用团队账号访问价格监控平台</p>
        <div v-if="error" role="alert" class="notice error">{{ error }}</div>
        <label
          >用户名<input
            v-model="username"
            required
            autocomplete="username"
            placeholder="请输入用户名"
            :disabled="busy" /></label
        ><label
          >密码<input
            v-model="password"
            required
            type="password"
            autocomplete="current-password"
            placeholder="请输入密码"
            :disabled="busy" /></label
        ><button class="btn btn-primary login-submit" :disabled="busy">
          {{ busy ? '正在登录…' : '登录工作空间' }}<Icon name="arrow" :size="18" />
        </button>
        <p class="login-help">
          <Icon name="shield" :size="14" />仅限团队成员访问，账号由管理员分配
        </p>
      </form>
    </section>
    <footer class="login-footer">
      © {{ new Date().getFullYear() }} PriceScope · 多平台价格观测
    </footer>
  </div>
</template>
