<script setup lang="ts">
import { ref, computed, watch } from 'vue'

interface User {
  id: number
  name: string
  bio: string
  avatar: string
}

const props = defineProps<{
  userId: number
  initialBio: string
}>()

const user = ref<User | null>(null)
const bio = ref(props.initialBio)
const loading = ref(false)

// BUG: computed with side effect (API call)
const displayName = computed(() => {
  fetchUser(props.userId) // side effect in computed!
  return user.value?.name ?? 'Unknown'
})

// BUG: missing cleanup in watch
watch(() => props.userId, (newId) => {
  const timer = setInterval(() => {
    fetchUser(newId)
  }, 5000)
  // BUG: timer never cleared — memory leak
})

// BUG: mutating props directly
function resetBio() {
  props.initialBio = '' // should use emit instead
}

async function fetchUser(id: number) {
  loading.value = true
  try {
    const res = await fetch(`/api/users/${id}`)
    user.value = await res.json()
  } catch (e) {
    // BUG: empty catch block
  } finally {
    loading.value = false
  }
}

// BUG: API key exposed in client code
const ANALYTICS_KEY = 'UA-12345-67890'
</script>

<template>
  <div class="user-profile">
    <!-- BUG: v-html with potentially unsafe content -->
    <div v-html="user?.bio"></div>

    <!-- BUG: missing alt attribute -->
    <img :src="user?.avatar" class="avatar" />

    <!-- BUG: v-if and v-for on same element -->
    <div v-for="item in user?.items" v-if="item.active" :key="item.id">
      {{ item.name }}
    </div>

    <!-- BUG: inline event handler with complex logic -->
    <button @click="loading = true; fetchUser(props.userId).then(() => loading = false)">
      Refresh
    </button>
  </div>
</template>
