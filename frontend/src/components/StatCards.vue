<script setup>
// Row of stat cards summarizing the /api/stats payload.
import { computed } from "vue";
import { formatDuration, formatNumber } from "../format.js";

const props = defineProps({
  stats: { type: Object, default: null },
});

const cards = computed(() => {
  const s = props.stats;
  return [
    { label: "Total meetings", value: s ? formatNumber(s.total_meetings) : "—" },
    { label: "Completed", value: s ? formatNumber(s.completed) : "—" },
    { label: "Total duration", value: s ? formatDuration(s.total_duration_seconds ?? 0) : "—" },
    { label: "Words transcribed", value: s ? formatNumber(s.total_words) : "—" },
    { label: "Participants", value: s ? formatNumber(s.total_users) : "—" },
    { label: "Active sessions", value: s ? formatNumber(s.active_sessions) : "—" },
    { label: "Errors", value: s ? formatNumber(s.errors) : "—" },
  ];
});
</script>

<template>
  <div class="stat-grid">
    <div v-for="card in cards" :key="card.label" class="stat-card">
      <div class="stat-value">{{ card.value }}</div>
      <div class="stat-label">{{ card.label }}</div>
    </div>
  </div>
</template>
