<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ page: number; total: number; pageSize?: number }>()
const emit = defineEmits<{ change: [page: number] }>()
const pages = computed(() => Math.max(1, Math.ceil(props.total / (props.pageSize ?? 20))))
</script>
<template>
  <div class="pagination">
    <span>共 {{ total.toLocaleString() }} 条记录</span>
    <div>
      <button
        type="button"
        class="btn btn-sm btn-ghost"
        :disabled="page <= 1"
        @click="emit('change', page - 1)"
      >
        上一页</button
      ><span class="page-number">{{ page }} / {{ pages }}</span
      ><button
        type="button"
        class="btn btn-sm btn-ghost"
        :disabled="page >= pages"
        @click="emit('change', page + 1)"
      >
        下一页
      </button>
    </div>
  </div>
</template>
