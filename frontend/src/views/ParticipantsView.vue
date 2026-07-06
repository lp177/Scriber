<script setup>
// Participants view: a paginated table of Discord users who joined meetings.
// Each row shows a compact avatar thumbnail and links to the per-user view.
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { avatarUrl, fetchBlob, getUsers } from "../api.js";
import { formatDate, formatNumber } from "../format.js";

const PAGE_SIZE = 20;

const router = useRouter();
const users = ref([]);
const total = ref(0);
const offset = ref(0);
const loading = ref(false);
const error = ref("");

// user id -> avatar object URL (fetched with auth). Revoked on reload/unmount.
const avatars = ref({});

/** Free every avatar object URL and clear the map. */
function revokeAvatars() {
  for (const url of Object.values(avatars.value)) {
    URL.revokeObjectURL(url);
  }
  avatars.value = {};
}

/** Fetch avatar blobs (auth-required) for the users that have one into object URLs. */
async function loadAvatars(list) {
  await Promise.all(
    list
      .filter((u) => u.has_avatar)
      .map(async (u) => {
        try {
          const blob = await fetchBlob(avatarUrl(u.id));
          avatars.value[u.id] = URL.createObjectURL(blob);
        } catch {
          // Leave it unset — the initials placeholder is shown instead.
        }
      }),
  );
}

/** Load the current page of participants and their avatars. */
async function loadUsers() {
  loading.value = true;
  error.value = "";
  try {
    const data = await getUsers(PAGE_SIZE, offset.value);
    users.value = data.items;
    total.value = data.total;
    revokeAvatars();
    await loadAvatars(data.items);
  } catch (e) {
    error.value = (e && e.message) || "Failed to load participants.";
  } finally {
    loading.value = false;
  }
}

/** Two-letter initials fallback shown when a participant has no avatar. */
function initials(u) {
  const name = (u && u.display_name ? u.display_name : "").trim();
  if (!name) {
    return "?";
  }
  const parts = name.split(/\s+/);
  const chars = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : name.slice(0, 2);
  return chars.toUpperCase();
}

const rangeText = computed(() => {
  if (total.value === 0) {
    return "0 of 0";
  }
  const first = offset.value + 1;
  const last = Math.min(offset.value + PAGE_SIZE, total.value);
  return `${first}–${last} of ${total.value}`;
});

const hasPrev = computed(() => offset.value > 0);
const hasNext = computed(() => offset.value + PAGE_SIZE < total.value);

function prevPage() {
  if (hasPrev.value) {
    offset.value = Math.max(0, offset.value - PAGE_SIZE);
    loadUsers();
  }
}

function nextPage() {
  if (hasNext.value) {
    offset.value += PAGE_SIZE;
    loadUsers();
  }
}

/** Navigate to a participant's detail view. */
function openUser(id) {
  router.push(`/users/${encodeURIComponent(id)}`);
}

onMounted(loadUsers);
onUnmounted(revokeAvatars);
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Participants</h1>
      <button type="button" class="btn ghost" @click="loadUsers">Refresh</button>
    </div>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>

    <section class="panel">
      <div class="table-wrap users-table">
        <table>
          <thead>
            <tr>
              <th scope="col" class="avatar-cell"><span class="visually-hidden">Avatar</span></th>
              <th scope="col">Name</th>
              <th scope="col">Sessions</th>
              <th scope="col">Last session</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loading && !users.length">
              <td colspan="4" class="muted">Loading participants…</td>
            </tr>
            <tr v-else-if="!users.length">
              <td colspan="4" class="muted">No participants yet — they appear once a meeting is recorded.</td>
            </tr>
            <tr
              v-for="u in users"
              :key="u.id"
              class="clickable"
              @click="openUser(u.id)"
            >
              <td class="avatar-cell">
                <img
                  v-if="avatars[u.id]"
                  class="avatar-sm"
                  :src="avatars[u.id]"
                  :alt="`Avatar of ${u.display_name || u.id}`"
                />
                <span v-else class="avatar-sm is-placeholder" aria-hidden="true">{{ initials(u) }}</span>
              </td>
              <td>
                <router-link class="row-link" :to="`/users/${encodeURIComponent(u.id)}`" @click.stop>
                  {{ u.display_name || u.id }}
                </router-link>
              </td>
              <td>{{ formatNumber(u.session_count) }}</td>
              <td>{{ formatDate(u.last_session_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pager">
        <span class="muted">{{ rangeText }}</span>
        <button type="button" class="btn ghost" :disabled="!hasPrev" @click="prevPage">Previous</button>
        <button type="button" class="btn ghost" :disabled="!hasNext" @click="nextPage">Next</button>
      </div>
    </section>
  </section>
</template>
