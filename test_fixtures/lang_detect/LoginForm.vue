<script setup lang="ts">
import { ref, watch } from 'vue'

const userInput = ref('<p>User content here</p>')
const redirectUrl = ref('')
const searchQuery = ref('')
let pollTimer: ReturnType<typeof setInterval> | null = null

// BUG: computed with side effect
const displayName = ref('')
watch(userInput, (val) => {
  // BUG: side effect inside watch that should be computed
  document.title = `User: ${val}`
  fetch('/api/log', { method: 'POST', body: JSON.stringify({ page: val }) })
})

function login() {
  // BUG: hardcoded credentials in source
  const ADMIN_PASS = 'admin123!'
  console.log('Logging in with:', ADMIN_PASS)
}

function startPolling() {
  // BUG: memory leak - no cleanup on unmount
  pollTimer = setInterval(() => {
    fetch('/api/status').then(r => r.json()).then(console.log)
  }, 5000)
}

function navigate() {
  // BUG: open redirect - user-controlled URL
  window.location.href = redirectUrl.value
}
</script>

<template>
  <div>
    <!-- BUG: XSS via v-html with user content -->
    <div v-html="userInput"></div>

    <!-- BUG: open redirect -->
    <button @click="navigate">Continue</button>

    <input v-model="searchQuery" placeholder="Search..." />
  </div>
</template>
