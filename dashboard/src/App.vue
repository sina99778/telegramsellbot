<script setup lang="ts">
import { onMounted } from "vue";
import { useAuthStore } from "@/stores/auth";

// Hydrate the auth state once at boot — refreshing the page should
// keep the operator logged in if their cookie is still valid.
const auth = useAuthStore();
onMounted(() => auth.hydrate());
</script>

<template>
  <router-view />
</template>
