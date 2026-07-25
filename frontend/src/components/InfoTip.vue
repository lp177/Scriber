<script setup>
// Small "ⓘ" help button with a tooltip bubble. Works on every input method:
// desktop hover shows it, click/tap toggles it (so it is usable on phones,
// where hover does not exist), keyboard focus shows it and Escape dismisses.
import { onMounted, onUnmounted, ref, useId } from "vue";

defineProps({
  /** Tooltip text. */
  text: { type: String, required: true },
  /** Accessible name of the button, e.g. "About audio expiration". */
  label: { type: String, default: "More information" },
});

const tipId = useId();
const root = ref(null);
const pinned = ref(false); // toggled by click/tap — stays open until dismissed
const hovered = ref(false); // transient hover/focus visibility

function onDocumentPointerDown(event) {
  if (pinned.value && root.value && !root.value.contains(event.target)) {
    pinned.value = false;
  }
}

function onKeydown(event) {
  if (event.key === "Escape") {
    pinned.value = false;
    hovered.value = false;
  }
}

function onToggle() {
  pinned.value = !pinned.value;
  if (!pinned.value) {
    // On touch, emulated hover/focus stick to the button after a tap, so a
    // second tap must also clear them or the bubble would never close.
    hovered.value = false;
  }
}

onMounted(() => {
  document.addEventListener("pointerdown", onDocumentPointerDown);
});
onUnmounted(() => {
  document.removeEventListener("pointerdown", onDocumentPointerDown);
});
</script>

<template>
  <span ref="root" class="info-tip" @keydown="onKeydown">
    <button
      type="button"
      class="info-tip-btn"
      :aria-label="label"
      :aria-expanded="pinned || hovered"
      :aria-describedby="tipId"
      @click="onToggle"
      @mouseenter="hovered = true"
      @mouseleave="hovered = false"
      @focus="hovered = true"
      @blur="hovered = false"
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
        focusable="false"
      >
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="16" x2="12" y2="12" />
        <line x1="12" y1="8" x2="12.01" y2="8" />
      </svg>
    </button>
    <span :id="tipId" class="info-tip-pop" role="tooltip" :class="{ open: pinned || hovered }">
      {{ text }}
    </span>
  </span>
</template>
