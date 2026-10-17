import test from "node:test";
import assert from "node:assert/strict";
import {positivePrice, compareSnapshots, filterRows, exportCSV} from "../site_assets/comparison-model.mjs";

const row = (code, price, description = "Service") => ["HCPCS", code, description, price, price, "", "100", "2"];
const snapshot = rows => ({summary_version: 2, rows, recorded_at: "2026-09-01", summary_url: "https://example.org/summary.csv"});

test("missing, zero, negative and invalid prices never become comparable prices", () => {
  for (const value of ["", " ", "0", "-1", "NaN", "Infinity", null, undefined]) assert.equal(positivePrice(value), null);
  assert.equal(positivePrice("24.355"), 24.355);
  const result = compareSnapshots(snapshot([row("a", ""), row("b", "0"), row("c", "20"), row("d", "-1")]),
    snapshot([row("a", "10"), row("b", "0"), row("c", ""), row("d", "10")]), "discounted_cash");
  assert.equal(result.comparable, 0);
  assert.equal(result.counts.newly_priced, 2);
  assert.equal(result.counts.price_missing, 1);
  assert.equal(result.counts.unavailable, 1);
  assert.ok(result.rows.every(r => r.percent === null));
});

test("numeric formatting is ignored, precision and leading-zero codes are retained", () => {
  const result = compareSnapshots(snapshot([row("001", "10.00"), row("1", "24.355")]),
    snapshot([row("001", "10"), row("1", "24.365")]), "discounted_cash");
  assert.equal(result.matched, 2);
  assert.equal(result.counts.unchanged, 1);
  assert.equal(result.counts.increase, 1);
  assert.ok(Math.abs(result.rows.find(r => r.code === "1").delta - .01) < 1e-10);
});

test("renamed descriptions are additions/removals, never automatically paired", () => {
  const result = compareSnapshots(snapshot([row("001", "10", "Old name")]),
    snapshot([row("001", "12", "New name")]), "gross_charge");
  assert.equal(result.matched, 0);
  assert.equal(result.counts.added, 1);
  assert.equal(result.counts.removed, 1);
  assert.ok(result.rows.every(r => r.percent === null));
});

test("different methods and duplicate keys cannot produce a misleading comparison", () => {
  assert.throws(() => compareSnapshots({...snapshot([]), summary_version: 1}, snapshot([]), "gross_charge"), /different summarization/);
  assert.throws(() => compareSnapshots(snapshot([row("a", "10"), row("a", "20")]), snapshot([]), "gross_charge"), /duplicate/);
});

test("extreme movements stay visible and are flagged; counts reconcile", () => {
  const result = compareSnapshots(snapshot([row("a", "10"), row("b", "100"), row("c", "30")]),
    snapshot([row("a", "50"), row("b", "20"), row("c", "30"), row("new", "1")]), "gross_charge");
  assert.equal(result.largeChanges, 2);
  assert.equal(result.comparable, 3);
  assert.equal(result.counts.increase + result.counts.decrease + result.counts.unchanged, result.comparable);
  assert.equal(Object.values(result.counts).reduce((a, b) => a + b), result.rows.length);
});

test("filters and sorting operate over the complete comparison, before pagination", () => {
  const result = compareSnapshots(snapshot([row("001", "10", "MRI"), row("002", "10", "CT"), row("003", "10", "MRI unchanged")]),
    snapshot([row("001", "12", "MRI"), row("002", "20", "CT"), row("003", "10", "MRI unchanged")]), "gross_charge");
  assert.deepEqual(filterRows(result.rows).map(r => r.code), ["002", "001"]);
  assert.deepEqual(filterRows(result.rows, {query: "mri"}).map(r => r.code), ["001"]);
  assert.deepEqual(filterRows(result.rows, {query: "MRI", status: "all", sort: "code"}).map(r => r.code), ["001", "003"]);
  assert.equal(filterRows(result.rows, {type: "NDC"}).length, 0);
});

test("CSV retains source precision, exact revision links, and escapes spreadsheet formulas", () => {
  const before = snapshot([row("001", "24.355", '=HYPERLINK("bad")')]);
  const after = snapshot([row("001", "12.1775", '=HYPERLINK("bad")')]);
  const result = compareSnapshots(before, after, "discounted_cash");
  const csv = exportCSV(result.rows, before, after, "discounted_cash");
  assert.ok(csv.includes('"24.355","12.1775"'));
  assert.ok(csv.includes('"\'=HYPERLINK(""bad"")"'));
  assert.ok(csv.includes('"-12.1775","-50"'));
  assert.ok(csv.includes('"https://example.org/summary.csv"'));
  assert.equal(csv.split("\r\n").length, 3);
});
