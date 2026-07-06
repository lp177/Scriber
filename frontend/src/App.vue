<script setup>
// Application shell: header bar with brand, settings link and log-out button.
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";

const route = useRoute();
const router = useRouter();

const isLoginView = computed(() => route.name === "login");

/** Clear the stored session token and return to the login view. */
function logOut() {
  localStorage.removeItem("scriber_token");
  router.push({ name: "login" });
}
</script>

<template>
  <header class="topbar">
    <div class="topbar-left">
      <router-link to="/" class="brand" aria-label="Scriber dashboard home">
        <svg
          class="brand-logo"
          width="26"
          height="26"
          viewBox="0 0 512 512"
          aria-hidden="true"
          focusable="false"
        >
          <defs>
            <linearGradient
              id="brand-teal"
              x1="0"
              y1="0"
              x2="512"
              y2="512"
              gradientUnits="userSpaceOnUse"
            >
              <stop offset="0" stop-color="#3ee0cc" />
              <stop offset="1" stop-color="#12a99a" />
            </linearGradient>
          </defs>
          <rect width="512" height="512" rx="116" fill="url(#brand-teal)" />
          <g fill="#04211e">
            <rect x="166" y="150" width="36" height="92" rx="18" />
            <rect x="238" y="121" width="36" height="150" rx="18" />
            <rect x="310" y="150" width="36" height="92" rx="18" />
            <rect x="166" y="306" width="180" height="32" rx="16" />
            <rect x="166" y="360" width="120" height="32" rx="16" />
          </g>
        </svg>
        <span class="wordmark">Scriber</span>
      </router-link>

      <nav v-if="!isLoginView" class="topbar-nav" aria-label="Primary navigation">
        <router-link to="/" class="nav-link">Dashboard</router-link>
        <router-link to="/participants" class="nav-link">Participants</router-link>
      </nav>
    </div>

    <nav v-if="!isLoginView" class="topbar-actions" aria-label="Account navigation">
      <router-link to="/settings" class="icon-btn" aria-label="Settings" title="Settings">
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
          focusable="false"
        >
          <circle cx="12" cy="12" r="3" />
          <path
            d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"
          />
        </svg>
      </router-link>
      <button type="button" class="btn ghost" @click="logOut">Log out</button>
    </nav>
  </header>

  <main class="page">
    <router-view />
  </main>
</template>
