export const FIELDS = {
  discounted_cash: {label: "Cash price", index: 4},
  gross_charge: {label: "Gross charge", index: 3},
  min_negotiated: {label: "Minimum negotiated rate", index: 5},
  max_negotiated: {label: "Maximum negotiated rate", index: 6},
};

export const STATUS = {
  increase: "Price increased", decrease: "Price decreased",
  added: "Entry added", removed: "Entry removed",
  newly_priced: "Price now reported", price_missing: "Price no longer reported",
  unchanged: "Price unchanged", unavailable: "No positive price",
};

export function positivePrice(value) {
  if (value === null || value === undefined || String(value).trim() === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function indexRows(snapshot) {
  const map = new Map();
  for (const row of snapshot.rows) {
    const key = JSON.stringify(row.slice(0, 3));
    if (map.has(key)) throw new Error("A snapshot contains duplicate entries.");
    map.set(key, row);
  }
  return map;
}

export function compareSnapshots(before, after, field) {
  if (!FIELDS[field]) throw new Error("Choose a supported price field.");
  if (before.summary_version !== after.summary_version) {
    throw new Error("These snapshots use different summarization methods. Choose two snapshots with the same method to compare prices.");
  }
  const index = FIELDS[field].index;
  const oldRows = indexRows(before), newRows = indexRows(after);
  const keys = new Set([...oldRows.keys(), ...newRows.keys()]);
  const counts = Object.fromEntries(Object.keys(STATUS).map(key => [key, 0]));
  const rows = [];
  let comparable = 0, matched = 0, largeChanges = 0;
  for (const key of keys) {
    const oldRow = oldRows.get(key), newRow = newRows.get(key);
    const previous = positivePrice(oldRow?.[index]);
    const current = positivePrice(newRow?.[index]);
    let status;
    if (!oldRow) status = "added";
    else if (!newRow) status = "removed";
    else {
      matched++;
      if (previous !== null && current !== null) {
        comparable++;
        status = current > previous ? "increase" : current < previous ? "decrease" : "unchanged";
      } else if (current !== null) status = "newly_priced";
      else if (previous !== null) status = "price_missing";
      else status = "unavailable";
    }
    const delta = previous !== null && current !== null ? current - previous : null;
    const percent = delta !== null ? delta / previous * 100 : null;
    const large = previous !== null && current !== null && (current / previous > 4 || current / previous < .25);
    if (large) largeChanges++;
    counts[status]++;
    const identity = (newRow || oldRow).slice(0, 3);
    rows.push({key, type: identity[0], code: identity[1], description: identity[2],
      oldRow, newRow, previous, current, delta, percent, status, large});
  }
  return {rows, counts, comparable, matched, largeChanges};
}

export function filterRows(rows, {query = "", type = "all", status = "changes", sort = "percent"} = {}) {
  const search = query.trim().toLocaleLowerCase();
  const filtered = rows.filter(row => {
    if (type !== "all" && row.type !== type) return false;
    if (status === "changes" && ["unchanged", "unavailable"].includes(row.status)) return false;
    if (status !== "all" && status !== "changes" && row.status !== status) return false;
    return !search || [row.type, row.code, row.description].join(" ").toLocaleLowerCase().includes(search);
  });
  const byCode = (a, b) => a.type.localeCompare(b.type) ||
    a.code.localeCompare(b.code, undefined, {numeric: true}) || a.description.localeCompare(b.description);
  return filtered.sort((a, b) => {
    if (sort === "code") return byCode(a, b);
    const field = sort === "dollars" ? "delta" : "percent";
    const x = a[field] === null ? -Infinity : Math.abs(a[field]);
    const y = b[field] === null ? -Infinity : Math.abs(b[field]);
    return (x === y ? 0 : y - x) || byCode(a, b);
  });
}

function csvCell(value, numeric = false) {
  let text = String(value ?? "");
  // Source descriptions and codes must not become spreadsheet formulas.
  const number = numeric && /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(text.trim());
  if (!number && /^[\s]*[=+@-]/.test(text)) text = "'" + text;
  return '"' + text.replaceAll('"', '""') + '"';
}

export function exportCSV(rows, before, after, field) {
  const headers = ["code_type", "code", "description", "price_field", "change",
    "before_value", "after_value", "dollar_change", "percent_change",
    "before_recorded_at", "after_recorded_at", "before_summary", "after_summary",
    "before_method", "after_method"];
  const index = FIELDS[field].index;
  const content = rows.map(row => [
    row.type, row.code, row.description, field, row.status,
    row.oldRow?.[index], row.newRow?.[index],
    row.delta === null ? "" : Number(row.delta.toPrecision(12)),
    row.percent === null ? "" : Number(row.percent.toPrecision(12)),
    before.recorded_at, after.recorded_at, before.summary_url, after.summary_url,
    before.summary_version, after.summary_version,
  ]);
  return [headers, ...content].map(row => row.map((value, i) => csvCell(value, i >= 5 && i <= 8)).join(",")).join("\r\n") + "\r\n";
}
