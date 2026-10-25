<script setup lang="ts">
import { statusLabel, statusClass } from '../api'
defineProps<{ value: unknown }>()
</script>
<template>
  <span class="badge" :class="statusClass(value)"><i></i>{{ statusLabel(value) }}</span>
</template>
