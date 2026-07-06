<script setup>
// Sign-in view: a real form with proper labels and autocomplete attributes so
// browser password managers work. Pasting into the password field is allowed.
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { getHealth, login } from "../api.js";

const router = useRouter();

const username = ref("");
const password = ref("");
const error = ref("");
const loading = ref(false);
const notice = ref("");

// Surface a bot problem (not invited / missing rights / not configured) on the
// very first page a visitor sees, so the site never looks broken or blank.
// A failing health check must not block sign-in.
onMounted(async () => {
  try {
    const health = await getHealth();
    if (health && health.notice) {
      notice.value = health.notice;
    }
  } catch {
    // Ignore: the login form stays fully usable regardless.
  }
});

/** Submit credentials; show an inline error on 401, redirect on success. */
async function onSubmit() {
  error.value = "";
  loading.value = true;
  try {
    const { token } = await login(username.value, password.value);
    localStorage.setItem("scriber_token", token);
    router.push({ name: "dashboard" });
  } catch (e) {
    error.value =
      e && e.status === 401
        ? "Invalid username or password."
        : (e && e.message) || "Login failed. Please try again.";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <section class="login-wrap">
    <div class="login-card">
      <h1>Sign in to Scriber</h1>
      <p class="muted">Use the admin credentials configured on the server.</p>

      <p v-if="notice" class="alert warn" role="alert">{{ notice }}</p>

      <form @submit.prevent="onSubmit">
        <div class="field">
          <label for="username">Username</label>
          <input
            id="username"
            v-model="username"
            type="text"
            name="username"
            autocomplete="username"
            required
          />
        </div>

        <div class="field">
          <label for="current-password">Password</label>
          <input
            id="current-password"
            v-model="password"
            type="password"
            name="password"
            autocomplete="current-password"
            required
          />
        </div>

        <p v-if="error" class="form-error" role="alert">{{ error }}</p>

        <button type="submit" class="btn primary btn-block" :disabled="loading">Sign in</button>
      </form>
    </div>
  </section>
</template>
