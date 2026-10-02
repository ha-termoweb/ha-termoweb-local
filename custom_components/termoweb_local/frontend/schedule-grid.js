/*
 * Grid model for the termoweb-local schedule card.
 *
 * A "prog" array is 168 (hourly) or 336 (half-hourly) values, each 0 (cold),
 * 1 (night), 2 (day) or null (unset/undecodable), Monday 00:00 first,
 * day-major (day 0 = Monday's slots, in order). This matches
 * termoweb_local's own climate.py attribute and set_schedule service, so no
 * reordering happens anywhere in this module.
 */

export const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
export const DAYS_PER_WEEK = 7;
export const HOURLY_LENGTH = 168;
export const HALF_HOURLY_LENGTH = 336;

export function slotsPerDayFor(length) {
  return length === HALF_HOURLY_LENGTH ? 48 : 24;
}

export function hoursPerSlotFor(slotsPerDay) {
  return 24 / slotsPerDay;
}

export function arraysEqual(a, b) {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

export function formatTemp(value) {
  if (value === null || value === undefined) return null;
  const num = Number(value);
  if (Number.isNaN(num)) return null;
  return `${num.toFixed(1)}°C`;
}

// Cell click/keyboard cycle order: cold (0) -> night (1) -> day (2) -> cold.
// Unset (null/undefined) enters the cycle at cold, same as a fresh click.
export function nextPresetValue(value) {
  if (value === 0) return 1;
  if (value === 1) return 2;
  if (value === 2) return 0;
  return 0;
}

// "HH:MM" for a fractional hour (e.g. 1.5 -> "01:30"), used for cell
// aria-labels; the visible hour-row header keeps its bare-number labels.
export function formatHourLabel(hour) {
  const h = Math.floor(hour);
  const m = Math.round((hour - h) * 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

// Returns a new grid array with sourceDay's slots copied onto each day in
// targetDays (source day, if included, is skipped); the source day and slot
// order are never rotated, only replicated.
export function copyDayToDays(grid, sourceDay, targetDays, slotsPerDay) {
  const next = grid.slice();
  const start = sourceDay * slotsPerDay;
  const rowValues = grid.slice(start, start + slotsPerDay);
  for (const day of targetDays) {
    if (day === sourceDay) continue;
    const dest = day * slotsPerDay;
    for (let col = 0; col < slotsPerDay; col++) {
      next[dest + col] = rowValues[col];
    }
  }
  return next;
}
