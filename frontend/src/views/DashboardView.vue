<script setup>
// Dashboard: stat cards, meetings-per-day chart, paginated meetings table and
// the transcript/summary viewer dialog. Stats auto-refresh every 30 seconds.
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { audioUrl, deleteMeeting, fetchBlob, fetchText, getBotStatus, getMeetings, getStats, resyncBot, summaryUrl, transcriptUrl } from "../api.js";
import StatCards from "../components/StatCards.vue";
import MeetingsPerDay from "../components/MeetingsPerDay.vue";
import MeetingsTable from "../components/MeetingsTable.vue";
import TranscriptDialog from "../components/TranscriptDialog.vue";

const PAGE_SIZE = 15;
const STATS_REFRESH_MS = 30000;

const router = useRouter();
const stats = ref(null);
const meetings = ref([]);
const total = ref(0);
const offset = ref(0);
const loading = ref(false);
const error = ref("");
const botStatus = ref(null);
const resyncing = ref(false);
const resyncNote = ref("");
const viewer = ref(null);
let statsTimer = null;

/** Load stats; background refreshes fail silently (banner stays clean). */
async function loadStats({ silent = false } = {}) {
  try {
    stats.value = await getStats();
  } catch (e) {
    if (!silent) {
      error.value = (e && e.message) || "Failed to load stats.";
    }
  }
}

/** Load Discord bot status; failures leave the banner hidden (non-critical). */
async function loadBotStatus() {
  try {
    botStatus.value = await getBotStatus();
  } catch {
    // Keep the dashboard usable even if the status check fails.
  }
}

/** Re-run the Discord command sync in place (e.g. after fixing the invite). */
async function onResync() {
  resyncing.value = true;
  resyncNote.value = "";
  try {
    const status = await resyncBot();
    botStatus.value = status;
    resyncNote.value = status.setup_error
      ? "Discord still refused the sync. Make sure the bot is invited with the link above, then retry."
      : "✓ Slash commands synced — the bot is ready.";
  } catch (e) {
    resyncNote.value = (e && e.message) || "Re-sync failed. Try again in a moment.";
  } finally {
    resyncing.value = false;
  }
}

/** Load the current page of meetings. */
async function loadMeetings() {
  loading.value = true;
  try {
    const data = await getMeetings(PAGE_SIZE, offset.value);
    meetings.value = data.items;
    total.value = data.total;
  } catch (e) {
    error.value = (e && e.message) || "Failed to load meetings.";
  } finally {
    loading.value = false;
  }
}

/** Reload both stats and the meetings list. */
function refresh() {
  error.value = "";
  loadStats();
  loadMeetings();
  loadBotStatus();
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
    loadMeetings();
  }
}

function nextPage() {
  if (hasNext.value) {
    offset.value += PAGE_SIZE;
    loadMeetings();
  }
}

/** Open the viewer dialog for a transcript or summary. */
function onView(meeting, kind) {
  viewer.value?.open(meeting, kind);
}

/** Navigate to the full meeting detail view. */
function onOpen(meeting) {
  router.push(`/meetings/${encodeURIComponent(meeting.id)}`);
}

/** Save a blob to disk through a temporary object-URL anchor. */
function saveBlob(blob, filename) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

/** Fetch a transcript/summary/audio file and trigger a client-side download. */
async function onDownload(meeting, kind) {
  error.value = "";
  try {
    if (kind === "audio") {
      const blob = await fetchBlob(audioUrl(meeting.id, true));
      saveBlob(blob, `scriber-${meeting.id}.${blob.type.includes("wav") ? "wav" : "ogg"}`);
      return;
    }
    const isSummary = kind === "summary";
    const url = isSummary ? summaryUrl(meeting.id, true) : transcriptUrl(meeting.id, true);
    const text = await fetchText(url);
    const blob = new Blob([text], {
      type: isSummary ? "text/markdown;charset=utf-8" : "text/plain;charset=utf-8",
    });
    saveBlob(blob, `${meeting.id}.${isSummary ? "md" : "txt"}`);
  } catch (e) {
    error.value = (e && e.message) || "Download failed.";
  }
}

/** Delete a meeting after explicit confirmation. */
async function onDelete(meeting) {
  const confirmed = window.confirm(
    `Delete meeting ${meeting.id} and its transcript/summary files? This cannot be undone.`,
  );
  if (!confirmed) {
    return;
  }
  error.value = "";
  try {
    await deleteMeeting(meeting.id);
    // If the last item of a page was removed, step back one page.
    if (meetings.value.length === 1 && offset.value > 0) {
      offset.value -= PAGE_SIZE;
    }
    refresh();
  } catch (e) {
    error.value = (e && e.message) || "Failed to delete the meeting.";
  }
}

onMounted(() => {
  refresh();
  statsTimer = window.setInterval(() => loadStats({ silent: true }), STATS_REFRESH_MS);
});

onUnmounted(() => {
  if (statsTimer !== null) {
    window.clearInterval(statsTimer);
  }
});
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Dashboard</h1>
      <button type="button" class="btn ghost" @click="refresh">
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
          focusable="false"
        >
          <polyline points="23 4 23 10 17 10" />
          <polyline points="1 20 1 14 7 14" />
          <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
        </svg>
        Refresh
      </button>
    </div>

    <div
      v-if="botStatus && botStatus.setup_error"
      class="alert warn bot-alert"
      role="alert"
    >
      <div class="bot-alert-text">
        <strong>Discord bot needs attention</strong>
        <p>{{ botStatus.setup_error }}</p>
      </div>
      <div class="bot-alert-actions">
        <a
          v-if="botStatus.invite_url"
          class="btn primary"
          :href="botStatus.invite_url"
          target="_blank"
          rel="noopener noreferrer"
        >
          Invite the bot to your server
        </a>
        <button type="button" class="btn ghost" :disabled="resyncing" @click="onResync">
          {{ resyncing ? "Re-syncing…" : "Retry sync" }}
        </button>
      </div>
    </div>

    <p v-if="resyncNote" class="muted resync-note" role="status">{{ resyncNote }}</p>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>

    <StatCards :stats="stats" />

    <section class="panel">
      <h2>Meetings per day (30 days)</h2>
      <MeetingsPerDay :days="stats?.meetings_by_day || []" />
    </section>

    <section class="panel">
      <h2>Meetings</h2>
      <MeetingsTable
        :items="meetings"
        :loading="loading"
        @open="onOpen"
        @view="onView"
        @download="onDownload"
        @delete="onDelete"
      />
      <div class="pager">
        <span class="muted">{{ rangeText }}</span>
        <button type="button" class="btn ghost" :disabled="!hasPrev" @click="prevPage">Previous</button>
        <button type="button" class="btn ghost" :disabled="!hasNext" @click="nextPage">Next</button>
      </div>
    </section>

    <TranscriptDialog ref="viewer" />
  </section>
</template>
