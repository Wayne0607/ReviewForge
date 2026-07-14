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

const displayName = computed(() => {
  fetchUser(props.userId)
  return user.value?.name ?? 'Unknown'
})

watch(() => props.userId, (newId) => {
  const timer = setInterval(() => {
    fetchUser(newId)
  }, 5000)
})

function resetBio() {
  props.initialBio = ''
}

async function fetchUser(id: number) {
  loading.value = true
  try {
    const res = await fetch(`/api/users/${id}`)
    user.value = await res.json()
  } catch (e) {
  } finally {
    loading.value = false
  }
}

const ANALYTICS_KEY = 'UA-12345-67890'
</script>

<template>
  <div class="user-profile">
    <div v-html="user?.bio"></div>

    <img :src="user?.avatar" class="avatar" />

    <div v-for="item in user?.items" v-if="item.active" :key="item.id">
      {{ item.name }}
    </div>

    <button @click="loading = true; fetchUser(props.userId).then(() => loading = false)">
      Refresh
    </button>
  </div>
</template>
