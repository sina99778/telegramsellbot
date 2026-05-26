<script setup lang="ts">
import { ref } from "vue";
import { useRouter, useRoute } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { ApiError } from "@/api/client";

const username = ref("");
const password = ref("");
const submitting = ref(false);
const errorMsg = ref<string | null>(null);

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

async function onSubmit() {
  errorMsg.value = null;
  submitting.value = true;
  try {
    await auth.login(username.value.trim(), password.value);
    const next = typeof route.query.next === "string" ? route.query.next : "/";
    await router.replace(next);
  } catch (exc) {
    if (exc instanceof ApiError) {
      errorMsg.value = exc.detail || "خطایی پیش آمد.";
    } else {
      errorMsg.value = "خطای شبکه — اتصال خود را بررسی کنید.";
    }
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center px-4 relative overflow-hidden">
    <!-- subtle ambient bg blobs -->
    <div class="pointer-events-none absolute inset-0 -z-10">
      <div class="absolute -top-32 -end-32 w-[480px] h-[480px] rounded-full bg-accent/10 blur-3xl"></div>
      <div class="absolute -bottom-32 -start-32 w-[420px] h-[420px] rounded-full bg-indigo-500/10 blur-3xl"></div>
    </div>

    <form
      @submit.prevent="onSubmit"
      class="w-full max-w-sm card space-y-5"
      autocomplete="on"
    >
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-lg bg-accent flex items-center justify-center text-bg-base font-extrabold">
          TS
        </div>
        <div>
          <h1 class="text-lg font-bold text-white">پنل مدیریت</h1>
          <p class="text-[11px] text-slate-400">TelegramSellBot — Dashboard</p>
        </div>
      </div>

      <div>
        <label for="username" class="label">نام کاربری</label>
        <input
          id="username"
          v-model="username"
          class="input"
          type="text"
          autocomplete="username"
          required
          autofocus
          :disabled="submitting"
        />
      </div>

      <div>
        <label for="password" class="label">رمز عبور</label>
        <input
          id="password"
          v-model="password"
          class="input"
          type="password"
          autocomplete="current-password"
          required
          :disabled="submitting"
        />
      </div>

      <div
        v-if="errorMsg"
        class="text-xs text-danger bg-danger/10 border border-danger/30 rounded-lg px-3 py-2"
      >
        {{ errorMsg }}
      </div>

      <button class="btn btn-primary w-full" :disabled="submitting">
        <span v-if="!submitting">ورود</span>
        <span v-else class="inline-flex items-center gap-2">
          <svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" stroke-dasharray="40 60" stroke-linecap="round" />
          </svg>
          در حال ورود…
        </span>
      </button>

      <p class="text-[11px] text-slate-500 text-center leading-relaxed">
        ادمین داشبورد رو هنوز نساختی؟<br />
        روی سرور بزن: <code class="text-accent">./install.sh</code> → گزینه‌ی
        <span class="text-accent">«ساخت ادمین داشبورد»</span>
      </p>
    </form>
  </div>
</template>
