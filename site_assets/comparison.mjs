import {FIELDS, STATUS, positivePrice, compareSnapshots, filterRows, exportCSV} from "./comparison-model.mjs";

const manifest = JSON.parse(document.querySelector("#comparison-data").textContent);
const snapshots = manifest.snapshots;
const $ = id => document.getElementById(id);
const count = value => new Intl.NumberFormat("en-US").format(value);
const money = value => value === null ? "—" : new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 6,
}).format(value);
const date = value => new Intl.DateTimeFormat("en-US", {
  month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
}).format(new Date(value));
const shortDate = value => new Intl.DateTimeFormat("en-US", {
  month: "short", day: "numeric", timeZone: "UTC",
}).format(new Date(value));
const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
function percent(value) {
  if (value === null) return "—";
  if (value === 0) return "0.00%";
  return (value > 0 ? "+" : "−") + (Math.abs(value) < .01 ? "<0.01" : Math.abs(value).toLocaleString("en-US", {maximumFractionDigits: 2, minimumFractionDigits: 2})) + "%";
}
function signedMoney(value) {
  return value === null ? "" : (value < 0 ? "−" : value > 0 ? "+" : "") + money(Math.abs(value));
}
const cache = new Map();
let active = null, filtered = [], currentPage = 1, generation = 0;
let typeInitialized = false;
const PAGE_SIZE = 25;
let initialNotice = "";
const params = new URLSearchParams(location.search);

for (const snapshot of snapshots) {
  for (const name of ["before", "after"]) {
    const option = document.createElement("option");
    option.value = snapshot.id;
    option.textContent = date(snapshot.recorded_at) + (snapshot.summary_version < 2 ? " · legacy" : "");
    $(name).append(option);
  }
}
const latest = snapshots.at(-1);
const previous = snapshots.slice(0, -1).findLast(s => s.summary_version === latest.summary_version) || snapshots.at(-2);
$("before").value = snapshots.some(s => s.id === params.get("before")) ? params.get("before") : previous.id;
$("after").value = snapshots.some(s => s.id === params.get("after")) ? params.get("after") : latest.id;
if ((params.has("before") && !snapshots.some(s => s.id === params.get("before"))) ||
    (params.has("after") && !snapshots.some(s => s.id === params.get("after")))) {
  initialNotice = "A snapshot in this link is unavailable. The selectors show the available replacement; check the dates before using these results.";
}
if (FIELDS[params.get("field")]) $("field").value = params.get("field");
$("search").value = params.get("q") || "";
for (const [id, key] of [["status", "status"], ["sort", "sort"]]) {
  if (Array.from($(id).options).some(option => option.value === params.get(key))) $(id).value = params.get(key);
}
$("snapshot-count").textContent = snapshots.length + " recorded summary snapshots";
$("full-methodology").href = manifest.methodology_url;

function notify(messages) {
  const box = $("message");
  box.replaceChildren();
  for (const message of messages.filter(Boolean)) {
    const p = document.createElement("p");
    p.textContent = message;
    box.append(p);
  }
  box.hidden = !box.childElementCount;
}

function sourceCard(snapshot, label, target) {
  const source = snapshot.source;
  $(target).innerHTML =
    '<div class="source-label">' + label + '</div><h3>' + escape(date(snapshot.recorded_at)) + '</h3>' +
    '<dl><dt>Summary entries</dt><dd>' + count(snapshot.row_count) + '</dd>' +
    '<dt>Summarization method</dt><dd>Version ' + escape(snapshot.summary_version) + '</dd>' +
    '<dt>Source version observed</dt><dd>' + (snapshot.source_observed_at ? escape(date(snapshot.source_observed_at)) : "Not recorded") + '</dd>' +
    '<dt>Recorded revision</dt><dd><a href="' + escape(snapshot.metadata_url) + '">' + snapshot.id.slice(0, 8) + '</a></dd></dl>' +
    '<div class="source-links"><a href="' + escape(snapshot.summary_url) + '">Download summary CSV ↗</a>' +
    (source ? '<a href="' + escape(source.url) + '">' + escape(source.label) + ' ↗</a>' : '<span>Original not linked in this snapshot</span>') + '</div>' +
    (snapshot.sha256 ? '<details class="source-fingerprint"><summary>Snapshot fingerprint</summary><code>' + escape(snapshot.sha256) + '</code></details>' : "");
}

async function loadSnapshot(snapshot) {
  if (!cache.has(snapshot.id)) {
    const pending = (async () => {
      const response = await fetch(snapshot.data_url);
      if (!response.ok) throw new Error("A snapshot could not be loaded (HTTP " + response.status + "). Please try Compare snapshots again.");
      let data;
      try { data = await response.json(); }
      catch { throw new Error("A snapshot could not be read. Please try Compare snapshots again."); }
      if (data.id !== snapshot.id || !Array.isArray(data.rows) || data.rows.length !== snapshot.row_count ||
          JSON.stringify(data.columns) !== JSON.stringify(["code_type", "code", "description", "gross_charge", "discounted_cash", "min_negotiated", "max_negotiated", "payer_entries"]) ||
          data.rows.some(row => !Array.isArray(row) || row.length !== 8)) {
        throw new Error("A snapshot failed its integrity check. Its comparison has not been displayed.");
      }
      return {...snapshot, rows: data.rows};
    })();
    cache.set(snapshot.id, pending);
    pending.catch(() => cache.delete(snapshot.id));
  }
  return cache.get(snapshot.id);
}

async function compare() {
  const request = ++generation;
  const before = snapshots.find(s => s.id === $("before").value);
  const after = snapshots.find(s => s.id === $("after").value);
  const field = $("field").value;
  active = null;
  filtered = [];
  $("results").hidden = true;
  $("results").setAttribute("aria-busy", "true");
  $("loading").hidden = false;
  $("loading").textContent = "Loading recorded snapshots…";
  $("compare-button").disabled = true;
  notify([]);
  sourceCard(before, "EARLIER SNAPSHOT", "source-before");
  sourceCard(after, "LATER SNAPSHOT", "source-after");
  $("sources").hidden = false;
  try {
    if (snapshots.indexOf(before) >= snapshots.indexOf(after)) {
      throw new Error("Choose an earlier snapshot that comes before the later snapshot.");
    }
    if (before.summary_version !== after.summary_version) {
      throw new Error("These snapshots use different summarization methods. Choose two snapshots with the same method to compare prices. Both source files are available below.");
    }
    const [a, b] = await Promise.all([loadSnapshot(before), loadSnapshot(after)]);
    if (request !== generation) return;
    const result = compareSnapshots(a, b, field);
    active = {before, after, field, ...result};
    currentPage = 1;
    const retainedType = typeInitialized ? $("code-type").value : (params.get("type") || "all");
    typeInitialized = true;
    $("code-type").replaceChildren(new Option("All code types", "all"));
    for (const type of [...new Set(result.rows.map(row => row.type))].sort()) {
      $("code-type").add(new Option(type || "(No code type)", type));
    }
    if (Array.from($("code-type").options).some(option => option.value === retainedType)) $("code-type").value = retainedType;
    const {counts, comparable, largeChanges} = result;
    $("comparison-period").textContent = date(before.recorded_at) + " → " + date(after.recorded_at);
    $("results-heading").textContent = FIELDS[field].label + " changes";
    $("summary-sentence").textContent = comparable
      ? count(counts.increase + counts.decrease) + " of " + count(comparable) + " comparable entries have a different published summary value."
      : "There are no entries with a positive price in both selected snapshots for this field.";
    for (const [id, value] of [["comparable-count", comparable], ["increase-count", counts.increase],
      ["decrease-count", counts.decrease], ["added-count", counts.added], ["removed-count", counts.removed]]) $(id).textContent = count(value);
    for (const status of ["increase", "decrease"]) {
      $(status + "-note").textContent = comparable ? (counts[status] / comparable * 100).toFixed(1) + "% of comparable entries" : "No comparable positive prices";
    }
    const distribution = $("distribution");
    distribution.replaceChildren();
    for (const status of ["decrease", "unchanged", "increase"]) {
      const segment = document.createElement("span");
      segment.className = status;
      segment.style.width = (comparable ? counts[status] / comparable * 100 : 0) + "%";
      segment.title = STATUS[status] + ": " + count(counts[status]);
      distribution.append(segment);
    }
    distribution.setAttribute("aria-label", count(counts.decrease) + " decreases, " + count(counts.unchanged) + " unchanged, " + count(counts.increase) + " increases.");
    $("distribution-note").textContent = count(counts.unchanged) + " prices unchanged · " +
      count(counts.newly_priced) + " now reported · " + count(counts.price_missing) +
      " no longer reported · " + count(counts.unavailable) + " without a positive price in either snapshot";
    $("field-note").textContent = field.includes("negotiated")
      ? "Negotiated extremes span different payers and plans. A change in the minimum or maximum can reflect a change in which rates are reported."
      : "Values summarize the hospital’s published entries. Descriptions, billing units, or the mix of service settings may change; inspect the source before drawing conclusions.";
    $("before-date").textContent = shortDate(before.recorded_at);
    $("after-date").textContent = shortDate(after.recorded_at);
    notify([initialNotice,
      before.summary_version < 2 && "These snapshots use legacy method 1, whose values could change when source rows were reordered. Treat the results as historical summary differences requiring source review.",
      before.warning && "Earlier snapshot warning: " + before.warning,
      after.warning && "Later snapshot warning: " + after.warning,
      largeChanges && count(largeChanges) + " entries have changes greater than fourfold in either direction. They are marked for source review and remain in the comparison."]);
    initialNotice = "";
    $("results").hidden = false;
    renderRows();
  } catch (error) {
    if (request === generation) notify([error.message || "Unable to load the comparison. Please try again."]);
  } finally {
    if (request === generation) {
      $("loading").hidden = true;
      $("compare-button").disabled = false;
      $("results").setAttribute("aria-busy", "false");
    }
  }
}

function updateURL() {
  if (!active) return;
  const query = new URLSearchParams({
    before: active.before.id, after: active.after.id, field: active.field,
  });
  for (const [key, id, fallback] of [["q", "search", ""], ["status", "status", "changes"],
    ["type", "code-type", "all"], ["sort", "sort", "percent"]]) {
    if ($(id).value !== fallback) query.set(key, $(id).value);
  }
  history.replaceState(null, "", location.pathname + "?" + query + location.hash);
}

function renderRows() {
  if (!active) return;
  filtered = filterRows(active.rows, {
    query: $("search").value, status: $("status").value, type: $("code-type").value, sort: $("sort").value,
  });
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  currentPage = Math.min(currentPage, totalPages);
  const offset = (currentPage - 1) * PAGE_SIZE;
  const visible = filtered.slice(offset, offset + PAGE_SIZE);
  $("result-count").textContent = count(filtered.length) + " matching " + (filtered.length === 1 ? "entry" : "entries");
  $("page-label").textContent = filtered.length
    ? count(offset + 1) + "–" + count(offset + visible.length) + " of " + count(filtered.length)
    : "0 entries";
  $("previous-page").disabled = currentPage === 1;
  $("next-page").disabled = currentPage === totalPages;
  $("download-button").disabled = filtered.length === 0;
  $("empty").hidden = filtered.length !== 0;
  $("price-rows").replaceChildren();
  visible.forEach((row, i) => {
    const tr = document.createElement("tr");
    tr.className = "entry-row";
    const detailId = "entry-detail-" + i;
    tr.innerHTML = '<td><button class="entry-button" type="button" aria-expanded="false" aria-controls="' + detailId + '">' +
      '<span class="entry-code"><span class="code-type">' + escape(row.type || "No type") + '</span><span class="code-value">' + escape(row.code || "No code") +
      '</span><span class="entry-chevron" aria-hidden="true">＋</span></span><span class="entry-description">' + escape(row.description || "No description") + '</span></button></td>' +
      '<td class="numeric" title="' + escape(row.oldRow?.[FIELDS[active.field].index] ?? "Not present") + '">' + money(row.previous) + '</td>' +
      '<td class="numeric" title="' + escape(row.newRow?.[FIELDS[active.field].index] ?? "Not present") + '">' + money(row.current) + '</td>' +
      '<td class="numeric ' + (row.status === "increase" ? "change-up" : row.status === "decrease" ? "change-down" : "") + '">' +
      percent(row.percent) + '<span class="delta-amount">' + signedMoney(row.delta) + '</span></td>' +
      '<td><span class="status-badge ' + row.status + '">' + STATUS[row.status] + '</span>' +
      (row.large ? '<span class="review-mark">Large change · review source</span>' : "") + '</td>';
    const details = document.createElement("tr");
    details.className = "detail-row";
    details.id = detailId;
    details.hidden = true;
    const button = tr.querySelector("button");
    button.addEventListener("click", () => {
      const open = button.getAttribute("aria-expanded") !== "true";
      button.setAttribute("aria-expanded", String(open));
      button.querySelector(".entry-chevron").textContent = open ? "−" : "＋";
      details.hidden = !open;
      if (open && !details.childElementCount) renderDetail(details, row);
    });
    $("price-rows").append(tr, details);
  });
  updateURL();
}

function renderDetail(target, row) {
  const scale = Math.max(row.previous || 0, row.current || 0, 1);
  const values = Object.entries(FIELDS).map(([, field]) =>
    '<span>' + field.label + '</span><span>' + money(positivePrice(row.oldRow?.[field.index])) +
    '</span><span>' + money(positivePrice(row.newRow?.[field.index])) + '</span>').join("");
  target.innerHTML = '<td colspan="5"><div class="detail-inner"><h3 class="detail-title">' + escape(row.description || row.code) + '</h3>' +
    '<div class="detail-grid"><div class="detail-chart" role="img" aria-label="' + escape(FIELDS[active.field].label + ": earlier " + money(row.previous) + ", later " + money(row.current)) + '">' +
    '<div class="detail-bar-row"><span>Earlier</span><div class="detail-bar" style="width:' + ((row.previous || 0) / scale * 100) + '%"></div></div>' +
    '<div class="detail-bar-row"><span>Later</span><div class="detail-bar after" style="width:' + ((row.current || 0) / scale * 100) + '%"></div></div></div>' +
    '<div class="detail-values"><strong>Published field</strong><strong>Earlier</strong><strong>Later</strong>' + values + '</div></div>' +
    '<p class="detail-links"><a href="' + escape(active.before.summary_url) + '">Earlier summary CSV ↗</a><a href="' + escape(active.after.summary_url) +
    '">Later summary CSV ↗</a><a href="#sources">Archived source files ↓</a></p>' +
    (row.large ? '<p class="detail-caution">This unusually large difference needs source review. Check units, settings, and how the hospital describes this entry before interpreting it as a change in the price of equivalent care.</p>' : "") +
    '</div></td>';
}

$("comparison-form").addEventListener("submit", event => {event.preventDefault(); compare();});
for (const id of ["status", "code-type", "sort"]) {
  $(id).addEventListener("change", () => {currentPage = 1; renderRows();});
}
let searchTimer;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {currentPage = 1; renderRows();}, 120);
});
$("reset-filters").addEventListener("click", () => {
  $("search").value = ""; $("status").value = "changes"; $("code-type").value = "all";
  $("sort").value = "percent"; currentPage = 1; renderRows(); $("search").focus();
});
for (const [id, increment] of [["previous-page", -1], ["next-page", 1]]) {
  $(id).addEventListener("click", () => {
    currentPage += increment; renderRows();
    $("result-count").scrollIntoView({block: "nearest"});
    $("announcement").textContent = "Showing entries " + $("page-label").textContent;
  });
}
$("download-button").addEventListener("click", () => {
  if (!active) return;
  clearTimeout(searchTimer);
  renderRows();
  const blob = new Blob(["\uFEFF", exportCSV(filtered, active.before, active.after, active.field)], {type: "text/csv;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = manifest.slug + "-" + active.before.recorded_at.slice(0, 10) + "-to-" + active.after.recorded_at.slice(0, 10) + "-" + active.field + ".csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  $("announcement").textContent = "Downloaded " + count(filtered.length) + " filtered entries.";
});
$("share-button").addEventListener("click", async () => {
  updateURL();
  try {
    await navigator.clipboard.writeText(location.href);
    $("share-button").textContent = "Link copied ✓";
    $("announcement").textContent = "Comparison link copied to clipboard.";
    setTimeout(() => {$("share-button").textContent = "Copy link ↗";}, 2500);
  } catch {
    notify(["Copy the address from your browser’s address bar to share these snapshots and filters."]);
  }
});
compare();
