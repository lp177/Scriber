<script setup>
// Accessible CSS bar list of meetings per day: every bar carries its date and
// count as visible text, single accent color for all marks.
import { computed } from "vue";

const props = defineProps({
  days: { type: Array, default: () => [] },
});

const maxCount = computed(() =>
  props.days.reduce((max, day) => Math.max(max, Number(day.count) || 0), 0),
);

/** Bar width as a percentage of the busiest day (minimum sliver kept visible). */
function barWidth(count) {
  if (maxCount.value <= 0) {
    return "0%";
  }
  return `${Math.max(2, ((Number(count) || 0) / maxCount.value) * 100)}%`;
}

function rowTitle(day) {
  const n = Number(day.count) || 0;
  return `${day.date}: ${n} ${n === 1 ? "meeting" : "meetings"}`;
}
</script>

<template>
  <p v-if="!days.length" class="muted">No meetings in the last 30 days.</p>
  <ul v-else class="day-chart">
    <li v-for="day in days" :key="day.date" class="day-row" :title="rowTitle(day)">
      <span class="day-date">{{ day.date }}</span>
      <span class="day-track">
        <span class="day-bar" :style="{ width: barWidth(day.count) }"></span>
      </span>
      <span class="day-count">{{ day.count }}</span>
    </li>
  </ul>
</template>
