import { createRouter, createWebHistory } from 'vue-router'
import { auth, loadUser } from './api'
import { isAdminPath, isLoginPath, loginDestination } from './navigation'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      component: () => import('./pages/Login.vue'),
      meta: { title: '成员登录', login: true },
    },
    {
      path: '/admin/login',
      component: () => import('./pages/Login.vue'),
      meta: { title: '管理后台登录', login: true, adminArea: true },
    },
    {
      path: '/',
      component: () => import('./pages/Dashboard.vue'),
      meta: { title: '价格总览', subtitle: '查看已授权平台的价格和数据变化' },
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
      path: '/admin/platforms',
      component: () => import('./pages/Platforms.vue'),
      meta: { title: '平台管理', subtitle: '连接交易平台，管理账号与采集会话', admin: true },
    },
    {
      path: '/admin/platforms/:id/rules',
      component: () => import('./pages/Rules.vue'),
      meta: { title: '采集规则', subtitle: '测试、发布并管理平台采集规则版本', admin: true },
    },
    {
      path: '/admin/runs',
      component: () => import('./pages/Runs.vue'),
      meta: { title: '采集任务', subtitle: '检查采集批次完整性、执行进度与失败原因', admin: true },
    },
    {
      path: '/events',
      component: () => import('./pages/Events.vue'),
      meta: { title: '事件与提醒', subtitle: '关注价格异动、商品停售与采集异常' },
    },
    {
      path: '/compare',
      component: () => import('./pages/Mappings.vue'),
      meta: { title: '全网比价', subtitle: '按类目查看同类或相似商品在不同平台的报价' },
    },
    {
      path: '/compare/:id',
      component: () => import('./pages/ComparisonDetail.vue'),
      meta: { title: '全网比价详情', subtitle: '查看关联平台的商品价格与计价条件' },
    },
    {
      path: '/admin/users',
      component: () => import('./pages/Users.vue'),
      meta: { title: '成员管理', subtitle: '管理团队账号与访问权限', admin: true },
    },
    {
      path: '/platform-submissions',
      component: () => import('./pages/PlatformSubmissions.vue'),
      meta: { title: '提交平台收录', subtitle: '推荐交易平台并查看审核结果' },
    },
    {
      path: '/admin/platform-submissions',
      component: () => import('./pages/PlatformSubmissions.vue'),
      meta: { title: '平台收录审核', subtitle: '审核成员推荐的平台与收录说明', admin: true },
    },
    {
      path: '/admin/settings/network',
      component: () => import('./pages/NetworkSettings.vue'),
      meta: { title: '系统设置', subtitle: '管理网络代理、AI 接口与每日分析计划', admin: true },
    },
    {
      path: '/admin/settings/update',
      component: () => import('./pages/SystemUpdate.vue'),
      meta: { title: '系统更新', subtitle: '检查、安装与回滚正式发布版本', admin: true },
    },
    {
      path: '/admin',
      component: () => import('./pages/Dashboard.vue'),
      meta: { title: '后台总览', subtitle: '管理采集平台与系统运行', admin: true },
    },
    {
      path: '/admin/products',
      component: () => import('./pages/Products.vue'),
      meta: { title: '商品管理', subtitle: '核对商品价格与历史走势', admin: true },
    },
    {
      path: '/admin/products/:id',
      component: () => import('./pages/ProductDetail.vue'),
      meta: { title: '商品详情', subtitle: '核对商品价格与采样记录', admin: true },
    },
    {
      path: '/admin/events',
      component: () => import('./pages/Events.vue'),
      meta: { title: '事件管理', subtitle: '检查并处理价格、状态与采集异常', admin: true },
    },
    {
      path: '/admin/compare',
      component: () => import('./pages/Mappings.vue'),
      meta: { title: '全网比价', subtitle: '维护比价类目、统一商品与平台报价关联', admin: true },
    },
    {
      path: '/admin/comparison-categories',
      component: () => import('./pages/ComparisonCategories.vue'),
      meta: {
        title: '比价类目管理',
        subtitle: '独立维护比价类目的层级、名称、排序和归属',
        admin: true,
      },
    },
    {
      path: '/admin/ai-analysis',
      component: () => import('./pages/AIAnalysis.vue'),
      meta: {
        title: 'AI 数据验证',
        subtitle: '每日分析采集质量、价格异常和分类归总建议',
        admin: true,
      },
    },
    {
      path: '/admin/compare/:id',
      component: () => import('./pages/ComparisonDetail.vue'),
      meta: { title: '全网比价详情', subtitle: '核对关联商品的各平台报价', admin: true },
    },
    {
      path: '/mappings',
      redirect: (to) => ({ path: '/compare', query: to.query, hash: to.hash }),
    },
    {
      path: '/mappings/:id',
      redirect: (to) => ({ path: `/compare/${to.params.id}`, query: to.query, hash: to.hash }),
    },
    {
      path: '/admin/mappings',
      redirect: (to) => ({ path: '/admin/compare', query: to.query, hash: to.hash }),
    },
    {
      path: '/admin/mappings/:id',
      redirect: (to) => ({
        path: `/admin/compare/${to.params.id}`,
        query: to.query,
        hash: to.hash,
      }),
    },
    {
      path: '/users',
      redirect: (to) => ({ path: '/admin/users', query: to.query, hash: to.hash }),
    },
    {
      path: '/platforms',
      redirect: (to) => ({ path: '/admin/platforms', query: to.query, hash: to.hash }),
    },
    {
      path: '/platforms/:id/rules',
      redirect: (to) => ({
        path: `/admin/platforms/${to.params.id}/rules`,
        query: to.query,
        hash: to.hash,
      }),
    },
    { path: '/runs', redirect: (to) => ({ path: '/admin/runs', query: to.query, hash: to.hash }) },
    { path: '/settings/network', redirect: '/admin/settings/network' },
    { path: '/settings/update', redirect: '/admin/settings/update' },
    { path: '/admin/:pathMatch(.*)*', redirect: '/admin' },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})
router.beforeEach(async (to) => {
  if (!auth.initialized) await loadUser()
  const loginPage = isLoginPath(to.path)
  if (!auth.user && !loginPage)
    return {
      path: isAdminPath(to.path) ? '/admin/login' : '/login',
      query: { redirect: to.fullPath },
    }
  if (auth.user && loginPage)
    return loginDestination(to.query.redirect, auth.user.role, to.path === '/admin/login')
  if (to.meta.admin && auth.user?.role !== 'admin') return '/'
  document.title = `${to.meta.title ?? '价格观测'} · PriceScope`
})
