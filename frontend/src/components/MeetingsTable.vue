<script setup>
// Meetings table with status badges and inline-SVG icon action buttons.
import { formatDate, formatDuration, formatNumber } from "../format.js";

defineProps({
  items: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
});

defineEmits(["open", "view", "download", "delete"]);
</script>

<template>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th scope="col">Date</th>
          <th scope="col">Server</th>
          <th scope="col">Channel</th>
          <th scope="col">Started by</th>
          <th scope="col">Duration</th>
          <th scope="col">Words</th>
          <th scope="col">Status</th>
          <th scope="col">Actions</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading && !items.length">
          <td colspan="8" class="muted">Loading meetings…</td>
        </tr>
        <tr v-else-if="!items.length">
          <td colspan="8" class="muted">No meetings recorded yet.</td>
        </tr>
        <tr v-for="m in items" :key="m.id">
          <td>{{ formatDate(m.started_at) }}</td>
          <td>{{ m.guild_name || "—" }}</td>
          <td>{{ m.voice_channel_name || "—" }}</td>
          <td>{{ m.started_by_name || "—" }}</td>
          <td>{{ formatDuration(m.duration_seconds) }}</td>
          <td>{{ formatNumber(m.word_count) }}</td>
          <td>
            <span class="badge" :class="m.status">{{ m.status }}</span>
          </td>
          <td class="actions">
            <div class="actions-wrap">
            <button
              type="button"
              class="icon-btn"
              aria-label="Open meeting"
              title="Open meeting"
              @click="$emit('open', m)"
            >
              <svg
                width="17"
                height="17"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
                focusable="false"
              >
                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                <polyline points="15 3 21 3 21 9" />
                <line x1="10" y1="14" x2="21" y2="3" />
              </svg>
            </button>
            <button
              type="button"
              class="icon-btn"
              :disabled="!m.has_transcript"
              aria-label="View transcript"
              title="View transcript"
              @click="$emit('view', m, 'transcript')"
            >
              <svg
                width="17"
                height="17"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
                focusable="false"
              >
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
            </button>
            <button
              type="button"
              class="icon-btn"
              :disabled="!m.has_summary"
              aria-label="View summary"
              title="View summary"
              @click="$emit('view', m, 'summary')"
            >
              <svg
                width="17"
                height="17"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
                focusable="false"
              >
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
                <line x1="16" y1="13" x2="8" y2="13" />
                <line x1="16" y1="17" x2="8" y2="17" />
              </svg>
            </button>
            <button
              type="button"
              class="icon-btn"
              :disabled="!m.has_transcript"
              aria-label="Download transcript"
              title="Download transcript"
              @click="$emit('download', m, 'transcript')"
            >
              <svg
                width="17"
                height="17"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
                focusable="false"
              >
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
            </button>
            <button
              type="button"
              class="icon-btn"
              :disabled="!m.has_summary"
              aria-label="Download summary"
              title="Download summary"
              @click="$emit('download', m, 'summary')"
            >
              <svg
                width="17"
                height="17"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
                focusable="false"
              >
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
                <polyline points="9 15 12 18 15 15" />
                <line x1="12" y1="18" x2="12" y2="11" />
              </svg>
            </button>
            <button
              type="button"
              class="icon-btn danger"
              :disabled="m.status === 'recording'"
              aria-label="Delete meeting"
              :title="m.status === 'recording' ? 'Cannot delete while recording' : 'Delete meeting'"
              @click="$emit('delete', m)"
            >
              <svg
                width="17"
                height="17"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
                focusable="false"
              >
                <polyline points="3 6 5 6 21 6" />
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                <line x1="10" y1="11" x2="10" y2="17" />
                <line x1="14" y1="11" x2="14" y2="17" />
              </svg>
            </button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
