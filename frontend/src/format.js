// Shared display formatting helpers.

/** Format a duration in seconds as "3 h 24 min", "24 min" or "42 s"; "—" when unknown. */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) {
    return "—";
  }
  const total = Math.round(Number(seconds));
  if (total < 60) {
    return `${total} s`;
  }
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return hours > 0 ? `${hours} h ${minutes} min` : `${minutes} min`;
}

/** Format an ISO8601 timestamp as a local "YYYY-MM-DD HH:MM" string; "—" when missing. */
export function formatDate(iso) {
  if (!iso) {
    return "—";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** Format an integer with thousands separators; "0" when missing. */
export function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "0";
  }
  return new Intl.NumberFormat("en-US").format(Number(value));
}
