<script setup>
// Meetings table with status badges. Every row opens the meeting page (click,
// or the focusable date link for keyboard users); viewing, downloading and
// deleting live on that page.
import { formatCost, formatDate, formatDuration, formatNumber } from "../format.js";

defineProps({
  items: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
});

defineEmits(["open"]);

/** Tooltip explaining the cost cell. */
function costTitle(m) {
  if (!m.usage_calls) {
    return "No AI provider call recorded for this meeting";
  }
  const calls = `${m.usage_calls} AI call${m.usage_calls === 1 ? "" : "s"}`;
  if (m.cost_usd === null || m.cost_usd === undefined) {
    return `${calls} — model not in the price table`;
  }
  return m.usage_unpriced > 0
    ? `${calls}, estimated from list prices (${m.usage_unpriced} unpriced)`
    : `${calls}, estimated from list prices`;
}
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
          <th scope="col">Est. cost</th>
          <th scope="col">Status</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading && !items.length">
          <td colspan="8" class="muted">Loading meetings…</td>
        </tr>
        <tr v-else-if="!items.length">
          <td colspan="8" class="muted">No meetings recorded yet.</td>
        </tr>
        <tr v-for="m in items" :key="m.id" class="clickable" @click="$emit('open', m)">
          <td>
            <router-link
              class="row-link"
              :to="`/meetings/${encodeURIComponent(m.id)}`"
              :aria-label="`Open meeting of ${formatDate(m.started_at)}`"
              @click.stop
            >
              {{ formatDate(m.started_at) }}
            </router-link>
          </td>
          <td>{{ m.guild_name || "—" }}</td>
          <td>{{ m.voice_channel_name || "—" }}</td>
          <td>{{ m.started_by_name || "—" }}</td>
          <td>{{ formatDuration(m.duration_seconds) }}</td>
          <td>{{ formatNumber(m.word_count) }}</td>
          <td :title="costTitle(m)">
            {{ formatCost(m.cost_usd, { calls: m.usage_calls, unpriced: m.usage_unpriced }) }}
          </td>
          <td>
            <span class="badge" :class="m.status">{{ m.status }}</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
