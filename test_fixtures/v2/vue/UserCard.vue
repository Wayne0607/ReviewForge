<script setup lang="ts">
import { ref, computed } from 'vue'

interface Props {
  userBio: string
  analyticsId: string
}

const props = defineProps<Props>()

// BUG: client-side secret exposed
const MIXPANEL_TOKEN = 'mp_live_abc123def456'

// BUG: v-html will be used with userBio in template — XSS risk
const bioHtml = computed(() => props.userBio)

// BUG: open redirect
function goToDestination(url: string) {
  window.location.href = url
}
</script>

<template>
  <div class="user-profile">
    <!-- BUG: XSS — user content rendered as raw HTML -->
    <div v-html="bioHtml"></div>

    <!-- Should be OK: static image from assets -->
    <img src="/assets/avatar-placeholder.png" alt="Default avatar" />

    <!-- BUG: dynamic component name from prop — potential attack surface -->
    <component :is="props.analyticsId" />

    <!-- BUG: inline complex logic -->
    <button @click="goToDestination(props.userBio)">
      Visit Profile
    </button>
  </div>
</template>
