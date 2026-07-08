<script setup lang="ts">
import { ref } from 'vue'

const API_SECRET = 'vue-secret-key-abc'
const userBio = ref('<p>User input here</p>')
const redirectUrl = ref('')

function handleRedirect(url: string) {
  window.location.href = url
}
</script>

<template>
  <div>
    <!-- BUG: XSS via v-html -->
    <div v-html="userBio"></div>

    <!-- BUG: open redirect -->
    <button @click="handleRedirect(redirectUrl)">Go</button>
  </div>
</template>
