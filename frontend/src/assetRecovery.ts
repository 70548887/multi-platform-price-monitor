import { ref } from 'vue'

// Only a boolean is retained. Import URLs, network details and exception payloads
// never become page content, storage, telemetry or a repeated navigation.
export const assetLoadFailed = ref(false)
export const assetRecoveryMessage = '页面资源已更新或加载失败，请确认未保存的内容后刷新。'

interface RecoveryRouter {
  onError(handler: (error: unknown) => void): () => void
}

export function isAssetLoadError(error: unknown): boolean {
  const message =
    typeof error === 'object' && error !== null && 'message' in error
      ? (error as { message?: unknown }).message
      : error
  return (
    typeof message === 'string' &&
    /failed to fetch dynamically imported module|error loading dynamically imported module|importing a module script failed|unable to preload css|loading (?:css )?chunk [\w-]+ failed|failed to load module script/i.test(
      message,
    )
  )
}

export function installAssetRecovery(target: Window, router: RecoveryRouter): () => void {
  const onPreload = (event: Event) => {
    event.preventDefault()
    assetLoadFailed.value = true
  }
  const removeRouterHandler = router.onError((error) => {
    if (isAssetLoadError(error)) assetLoadFailed.value = true
  })
  target.addEventListener('vite:preloadError', onPreload)
  return () => {
    target.removeEventListener('vite:preloadError', onPreload)
    removeRouterHandler()
  }
}

export function refreshAfterAssetFailure(): void {
  if (assetLoadFailed.value) window.location.reload()
}
