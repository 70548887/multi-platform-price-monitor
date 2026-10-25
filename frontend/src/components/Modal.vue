<script setup lang="ts">
import { onMounted, onBeforeUnmount } from 'vue'
import Icon from './Icon.vue'
defineProps<{ title: string; wide?: boolean }>()
const emit = defineEmits<{ close: [] }>()
function key(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}
onMounted(() => document.addEventListener('keydown', key))
onBeforeUnmount(() => document.removeEventListener('keydown', key))
</script>
<template>
  <Teleport to="body"
    ><div class="modal-backdrop" @click.self="emit('close')">
      <section role="dialog" aria-modal="true" :aria-label="title" class="modal" :class="{ wide }">
        <header>
          <h2>{{ title }}</h2>
          <button class="icon-btn" aria-label="关闭" @click="emit('close')">
            <Icon name="close" />
          </button>
        </header>
        <div class="modal-body"><slot /></div>
      </section></div
  ></Teleport>
</template>
