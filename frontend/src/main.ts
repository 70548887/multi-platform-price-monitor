import { createApp } from 'vue'
import App from './App.vue'
import { router } from './router'
import './style.css'
import { installAssetRecovery } from './assetRecovery'
installAssetRecovery(window, router)
createApp(App).use(router).mount('#app')
