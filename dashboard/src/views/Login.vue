<script setup lang="ts">
import { ref } from "vue";
import { useRouter, useRoute } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { ApiError } from "@/api/client";

const username = ref("");
const password = ref("");
const showPassword = ref(false);
import Icon from "@/components/Icon.vue";
import { usePreferences } from "@/stores/preferences";
const preferences = usePreferences();
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
    const next =
      typeof route.query.next === "string" && /^\/(?!\/)/.test(route.query.next)
        ? route.query.next
        : "/";
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
  <div class="login-page">
    <header class="login-topbar">
      <div class="login-brand" dir="ltr">
        <span class="brand-symbol"><Icon name="server" :size="19" /></span
        >SELLBOT<span class="text-slate-500 text-[10px]">CONSOLE</span>
      </div>
      <button
        class="icon-button"
        :aria-label="preferences.theme === 'dark' ? 'حالت روشن' : 'حالت تاریک'"
        @click="preferences.toggleTheme()"
      >
        <Icon :name="preferences.theme === 'dark' ? 'sun' : 'moon'" />
      </button>
    </header>
    <main class="login-main">
      <section class="login-intro">
        <div class="eyebrow">WORKSPACE / MANAGEMENT</div>
        <h1>یک نمای دقیق.<br />مدیریت همهٔ سرویس‌ها.</h1>
        <p>
          کاربران، فروش و سلامت سرورها را در یک فضای منظم دنبال کنید و رسیدهای
          منتظر بررسی را سریع‌تر پیدا کنید.
        </p>
        <div class="login-features">
          <span>مرکز عملیات</span><span>گزارش‌های مالی</span
          ><span>مدیریت کاربران</span>
        </div>
        <div class="login-art" aria-hidden="true">
          <div><Icon name="server" :size="32" /></div>
          <div><Icon name="chart" :size="32" /></div>
          <div><Icon name="receipt" :size="32" /></div>
        </div>
      </section>
      <form class="login-form" @submit.prevent="onSubmit" autocomplete="on">
        <div class="eyebrow mb-2">ورود به فضای مدیریت</div>
        <h2>خوش آمدید</h2>
        <p>با حساب مدیر داشبورد وارد شوید.</p>
        <div class="mt-7">
          <label for="username" class="label">نام کاربری</label
          ><input
            id="username"
            v-model="username"
            class="input"
            dir="ltr"
            type="text"
            autocomplete="username"
            autocapitalize="none"
            :spellcheck="false"
            maxlength="64"
            required
            autofocus
            :disabled="submitting"
            placeholder="admin"
          />
        </div>
        <div class="mt-5">
          <label for="password" class="label">رمز عبور</label>
          <div class="login-password">
            <input
              id="password"
              v-model="password"
              class="input"
              dir="ltr"
              :type="showPassword ? 'text' : 'password'"
              autocomplete="current-password"
              maxlength="256"
              required
              :disabled="submitting"
            /><button
              type="button"
              :aria-label="showPassword ? 'پنهان‌کردن رمز' : 'نمایش رمز'"
              :aria-pressed="showPassword"
              @click="showPassword = !showPassword"
            >
              {{ showPassword ? "پنهان" : "نمایش" }}
            </button>
          </div>
        </div>
        <div v-if="errorMsg" role="alert" class="console-alert danger mt-4">
          {{ errorMsg }}
        </div>
        <button class="btn btn-primary w-full mt-6" :disabled="submitting">
          <span>{{ submitting ? "در حال ورود…" : "ورود به پنل مدیریت" }}</span
          ><Icon
            :name="submitting ? 'refresh' : 'chevron'"
            :size="16"
            :class="{ 'animate-spin': submitting }"
          />
        </button>
        <p class="!mt-5 text-center">
          برای دریافت یا بازیابی حساب مدیر، با مالک سامانه تماس بگیرید.
        </p>
      </form>
    </main>
    <footer class="login-footer">
      <span>TelegramSellBot · فضای مدیریت</span
      ><span>دسترسی ویژهٔ مدیران سامانه</span>
    </footer>
  </div>
</template>
