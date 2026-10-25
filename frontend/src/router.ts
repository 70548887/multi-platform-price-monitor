import { createRouter, createWebHistory } from 'vue-router'
import { auth, loadUser } from './api'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: () => import('./pages/Login.vue'), meta: { title: '登录' } },
    {
      path: '/',
      component: () => import('./pages/Dashboard.vue'),
      meta: { title: '监控总览', subtitle: '掌握全平台价格与采集运行情况' },
    },
    {
      path: '/products',
      component: () => import('./pages/Products.vue'),
      meta: { title: '商品中心', subtitle: '统一查看商品价格、销售状态与历史走势' },
    },
    {
      path: '/products/:id',
      component: () => import('./pages/ProductDetail.vue'),
      meta: { title: '商品详情', subtitle: '追踪每一次价格和销售状态的变化' },
    },
    {
      path: '/platforms',
      component: () => import('./pages/Platforms.vue'),
      meta: { title: '平台管理', subtitle: '连接交易平台，管理账号与采集会话' },
    },
    {
      path: '/platforms/:id/rules',
      component: () => import('./pages/Rules.vue'),
      meta: { title: '采集规则', subtitle: '测试、发布并管理平台采集规则版本', admin: true },
    },
    {
      path: '/runs',
      component: () => import('./pages/Runs.vue'),
      meta: { title: '采集任务', subtitle: '检查采集批次完整性、执行进度与失败原因' },
    },
    {
      path: '/events',
      component: () => import('./pages/Events.vue'),
      meta: { title: '事件与提醒', subtitle: '关注价格异动、商品停售与采集异常' },
    },
    {
      path: '/mappings',
      component: () => import('./pages/Mappings.vue'),
      meta: { title: '跨平台映射', subtitle: '建立统一商品关系，比较相同口径的报价' },
    },
    {
      path: '/users',
      component: () => import('./pages/Users.vue'),
      meta: { title: '成员管理', subtitle: '管理团队账号与访问权限', admin: true },
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})
router.beforeEach(async (to) => {
  if (!auth.initialized) await loadUser()
  if (!auth.user && to.path !== '/login')
    return { path: '/login', query: { redirect: to.fullPath } }
  if (auth.user && to.path === '/login') return '/'
  if (to.meta.admin && auth.user?.role !== 'admin') return '/'
  document.title = `${to.meta.title ?? '价格观测'} · PriceScope`
})
