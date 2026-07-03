<!-- Large module 6/8: Admin Panel with planted bugs (Vue) -->
<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'

const userContent = ref('<h1>Admin Dashboard</h1>')
const systemLog = ref('')
const exportUrl = ref('')
let refreshInterval: ReturnType<typeof setInterval> | null = null

const ADMIN_TOKEN = 'lg-vue-admin-token-2024' // BUG: hardcoded token

function loadSystemLog() {
  fetch('/api/admin/log?token=' + ADMIN_TOKEN)
    .then(r => r.text())
    .then(data => {
      systemLog.value = data
    })
}

function exportData(format: string) {
  // BUG: open redirect
  window.location.href = exportUrl.value + '?format=' + format
}

// BUG: memory leak - no cleanup
onMounted(() => {
  refreshInterval = setInterval(loadSystemLog, 30000)
})

// BUG: side effect in watch
watch(userContent, (newVal) => {
  document.title = 'Admin: ' + newVal.replace(/<[^>]*>/g, '')
  fetch('/api/admin/log-action', {
    method: 'POST',
    body: JSON.stringify({ action: 'view', content: newVal })
  })
})
</script>

<template>
  <div>
    <!-- BUG: XSS via v-html -->
    <div v-html="userContent"></div>

    <!-- BUG: user content rendered unsafely -->
    <pre>{{ systemLog }}</pre>

    <button @click="exportData('csv')">Export</button>
  </div>
</template>
