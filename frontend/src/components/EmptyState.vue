<script setup lang="ts">
import Icon from './Icon.vue'
withDefaults(defineProps<{ title?: string; description?: string; icon?: string }>(), {
  title: '暂无数据',
  description: '完成平台接入和首次采集后，数据将显示在这里。',
  icon: 'box',
})
</script>
<template>
  <div class="empty">
    <div class="empty-icon"><Icon :name="icon" :size="26" /></div>
    <h3>{{ title }}</h3>
    <p>{{ description }}</p>
    <slot />
  </div>
</template>
