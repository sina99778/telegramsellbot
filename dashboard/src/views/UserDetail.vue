<script setup lang="ts">
defineProps<{ id: string }>();
</script>

<template>
  <div class="p-6 lg:p-8 max-w-7xl mx-auto">
    <header class="mb-6">
      <h1 class="text-2xl font-bold text-white">پروفایل کاربر</h1>
      <p class="text-sm text-slate-400 mt-1">شناسه: <code class="text-accent">{{ id }}</code></p>
    </header>
    <div class="card text-center text-slate-400 py-12">جزئیات کاربر در commit بعدی فعال می‌شود.</div>
  </div>
</template>
