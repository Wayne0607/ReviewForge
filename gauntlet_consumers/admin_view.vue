<template>
  <main>
    <img :src="profile.avatar">
    <div v-html="profile.bio"></div>
    <button @click="go(profile.next)">Continue</button>
  </main>
</template>

<script lang="ts">
import { AccountLabel, RawProfileCard } from "gauntlet_fullstack/seed_frontend";

export default {
  props: {
    profile: { type: Object, required: true },
  },
  methods: {
    go(next: string) {
      window.location.href = next;
    },
    renderBridge(html: string) {
      return RawProfileCard({ html });
    },
    labelText(text: string) {
      return AccountLabel({ text });
    },
  },
};
</script>
