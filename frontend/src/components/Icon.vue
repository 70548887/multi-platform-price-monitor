<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 20 })
const paths: Record<string, string> = {
  chart: 'M3 3v18h18M7 14l4-4 4 3 6-8',
  dashboard: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  box: 'M12 3l9 5-9 5-9-5 9-5zM3 8v9l9 5 9-5V8M12 13v9M7.5 5.5l9 5',
  platforms: 'M3 4h18v6H3zM3 14h18v6H3zM7 7h.01M7 17h.01M11 7h6M11 17h6',
  clock: 'M12 8v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0z',
  bell: 'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4',
  link: 'M10 13a5 5 0 0 0 7 .5l4-4a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7-.5l-4 4a5 5 0 0 0 7 7l2-2',
  users:
    'M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M16 3a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.87M12 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0z',
  search: 'M21 21l-4.4-4.4M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0z',
  plus: 'M12 5v14M5 12h14',
  close: 'M6 6l12 12M6 18L18 6',
  refresh: 'M20 7v5h-5M4 17v-5h5M6.1 6.1a8 8 0 0 1 13.2 3M4.7 14.9a8 8 0 0 0 13.2 3',
  arrow: 'M5 12h14M13 6l6 6-6 6',
  back: 'M19 12H5M11 6l-6 6 6 6',
  down: 'M6 9l6 6 6-6',
  up: 'M7 14l5-5 5 5M12 9v12',
  logout: 'M9 21H4V3h5M9 12h12M17 8l4 4-4 4',
  edit: 'M16 3l5 5L9 20H4v-5L16 3zM13 6l5 5',
  play: 'M8 5l12 7-12 7V5z',
  settings:
    'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9L7 7M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1',
  check: 'M5 12l4 4L19 6',
  alert:
    'M12 9v4M12 17h.01M10.3 3.8L1.8 18.5A2 2 0 0 0 3.5 21h17a2 2 0 0 0 1.7-2.5L13.7 3.8a2 2 0 0 0-3.4 0z',
  shield: 'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3zM8 12l3 3 5-6',
  globe: 'M2 12h20M12 2c6 6 6 14 0 20-6-6-6-14 0-20zM22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0z',
  key: 'M15 7a4 4 0 1 1-3.5 6L3 21H0v-3l8.5-8.5A4 4 0 0 1 15 7z',
  file: 'M14 2H4v20h16V8l-6-6zM14 2v6h6M8 13h8M8 17h5',
  menu: 'M3 6h18M3 12h18M3 18h18',
  external: 'M14 3h7v7M10 14L21 3M21 14v7H3V3h7',
  download: 'M12 3v12M7 10l5 5 5-5M3 16v5h18v-5',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
}
</script>
<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] ?? paths.box" />
  </svg>
</template>
