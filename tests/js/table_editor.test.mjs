// Runs the real app/static/wizard.js table editor in jsdom with a mocked fetch and manual timers.
// Requires jsdom (dev only):  npm install --no-save jsdom@24.1.3
// Run from the repo root:      node --test tests/js/table_editor.test.mjs
// If jsdom lives elsewhere, set JSDOM_PATH to its package directory.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");
const source = readFileSync(new URL("../../app/static/wizard.js", import.meta.url), "utf8");

const COLS = ["Approval Date (Month/Year)", "Substantive Change"];
const STAFF = ["Position", "Staff Member's Name", "# of Years Employed"];

async function setup({ serverColumns = COLS, rows = [], putStatus = 200, putBody } = {}) {
    const dom = new JSDOM(`<div id="table-container-c1"></div><div id="status-c1"></div>`, { runScripts: "dangerously" });
    const w = dom.window;
    // Let DOMContentLoaded pass first so wizard.js's page start-up (initializeApplication) never runs here.
    if (w.document.readyState === "loading") {
        await new Promise(r => w.document.addEventListener("DOMContentLoaded", r));
    }
    const calls = [];
    const timers = [];
    let resolvePut = null;
    let holdPut = false;
    w.fetch = async (url, opts = {}) => {
        calls.push({ url, method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null });
        if ((opts.method || "GET") === "PUT") {
            if (holdPut) await new Promise(r => { resolvePut = r; });
            const body = putBody ?? (putStatus === 200 ? { saved_at: "2026-10-21T12:00:00", rows: [], complete: true }
                                                        : { error: "The table could not be saved. Please try again.", code: "database_error" });
            return { ok: putStatus === 200, status: putStatus, json: async () => body };
        }
        return { ok: true, status: 200, json: async () => ({ table_columns: serverColumns, rows }) };
    };
    // Load wizard.js the way the browser does (a classic <script>), so its top-level let/const are shared globals.
    const runScript = (code) => {
        const el = w.document.createElement("script");
        el.textContent = code;
        w.document.body.appendChild(el);
    };
    runScript(source);
    w.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
    w.clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].fn = null; };
    runScript("applicationId = 7;");
    let completenessRefreshes = 0;
    w.loadCompleteness = async () => { completenessRefreshes++; };
    w.loadSubsections = async () => {};
    const $ = (sel) => w.document.querySelector(sel);
    const $$ = (sel) => [...w.document.querySelectorAll(sel)];
    const type = (input, text) => {
        input.value = text;
        input.dispatchEvent(new w.Event("input", { bubbles: true }));
    };
    const blur = (input) => input.dispatchEvent(new w.FocusEvent("focusout", { bubbles: true }));
    const runTimers = async () => {
        const pending = timers.splice(0).filter(t => t.fn);
        for (const t of pending) await t.fn();
    };
    return {
        w, $, $$, calls, type, blur, runTimers,
        read: (expr) => { runScript(`window.__out = (${expr});`); return w.__out; },
        puts: () => calls.filter(c => c.method === "PUT"),
        status: () => w.document.getElementById("status-c1"),
        refreshes: () => completenessRefreshes,
        holdPuts: () => { holdPut = true; },
        releasePut: () => { holdPut = false; if (resolvePut) resolvePut(); },
        flush: () => new Promise(r => setTimeout(r, 0)),
    };
}

test("columns from the server render editable inputs even when the page passes null columns (the lock bug)", async () => {
    const t = await setup();
    await t.w.loadTableRows(7, "c1", null);
    const inputs = t.$$("#table-elem-c1 tbody input[data-col-index]");
    assert.equal(inputs.length, 2);
    assert.ok(inputs.every(i => !i.disabled && !i.readOnly));
    assert.deepEqual(t.$$("#table-elem-c1 th").map(th => th.textContent).slice(0, 2), COLS);
});

test("a table with no columns shows a clear message instead of locked rows", async () => {
    const t = await setup({ serverColumns: [] });
    await t.w.loadTableRows(7, "c1", null);
    assert.equal(t.$$("#table-container-c1 input").length, 0);
    assert.match(t.$("#table-container-c1").textContent, /no columns configured/);
});

test("typing marks unsaved and autosaves after the delay with column-named rows", async () => {
    const t = await setup();
    await t.w.loadTableRows(7, "c1", null);
    const [date, change] = t.$$("#table-elem-c1 input");
    t.type(date, "03/2025");
    t.type(change, "Change of ownership");
    assert.match(t.status().textContent, /Unsaved changes/);
    assert.equal(t.puts().length, 0, "no save until the debounce fires");
    await t.runTimers();
    assert.equal(t.puts().length, 1);
    assert.deepEqual(t.puts()[0].body, { rows: [{ "Approval Date (Month/Year)": "03/2025", "Substantive Change": "Change of ownership" }] });
    assert.match(t.status().textContent, /Saved at/);
    assert.equal(t.refreshes(), 1, "completion status is refreshed after save");
});

test("leaving a cell saves immediately when there are unsaved edits, and not otherwise", async () => {
    const t = await setup();
    await t.w.loadTableRows(7, "c1", null);
    const [date] = t.$$("#table-elem-c1 input");
    t.blur(date);
    assert.equal(t.puts().length, 0, "blur without edits does not save");
    t.type(date, "01/2026");
    t.blur(date);
    await t.flush();
    assert.equal(t.puts().length, 1);
});

test("column names with apostrophes and # work, and existing values round-trip", async () => {
    const t = await setup({ serverColumns: STAFF, rows: [{ row_data: { "Position": "Director", "Staff Member's Name": "O'Brien \"Pat\"", "# of Years Employed": "3" } }] });
    await t.w.loadTableRows(7, "c1", null);
    const inputs = t.$$("#table-elem-c1 input");
    assert.deepEqual(inputs.map(i => i.value), ["Director", "O'Brien \"Pat\"", "3"]);
    await t.w.saveTableRows(7, "c1");
    assert.deepEqual(t.puts()[0].body.rows[0], { "Position": "Director", "Staff Member's Name": "O'Brien \"Pat\"", "# of Years Employed": "3" });
});

test("add row appends editable inputs and focuses the first one", async () => {
    const t = await setup();
    await t.w.loadTableRows(7, "c1", null);
    t.w.addTableRow("c1");
    const rows = t.$$("#table-elem-c1 tbody tr");
    assert.equal(rows.length, 2);
    assert.equal(t.w.document.activeElement, rows[1].querySelector("input"));
});

test("deleting a row saves the remaining rows", async () => {
    const t = await setup({ rows: [{ row_data: { [COLS[0]]: "a", [COLS[1]]: "x" } }, { row_data: { [COLS[0]]: "b", [COLS[1]]: "y" } }] });
    await t.w.loadTableRows(7, "c1", null);
    t.$$("#table-elem-c1 tbody tr")[0].querySelector("button").click();
    await t.flush();
    assert.deepEqual(t.puts().at(-1).body.rows, [{ [COLS[0]]: "b", [COLS[1]]: "y" }]);
});

test("a failed save shows the server message and keeps edits marked unsaved", async () => {
    const t = await setup({ putStatus: 500 });
    await t.w.loadTableRows(7, "c1", null);
    const [date] = t.$$("#table-elem-c1 input");
    t.type(date, "x");
    await t.runTimers();
    assert.match(t.status().textContent, /Save failed — The table could not be saved/);
    assert.equal(t.read("tableSaveState['c1'].dirty"), true);
    assert.equal(t.refreshes(), 0);
});

test("saves are serialized: a save requested mid-flight runs once afterwards with the latest data", async () => {
    const t = await setup();
    await t.w.loadTableRows(7, "c1", null);
    const [date] = t.$$("#table-elem-c1 input");
    t.holdPuts();
    t.type(date, "first");
    const p1 = t.w.saveTableRows(7, "c1");
    await t.flush();
    t.type(date, "second");
    t.w.saveTableRows(7, "c1");
    t.w.saveTableRows(7, "c1");
    assert.equal(t.puts().length, 1, "only one request in flight");
    t.releasePut();
    await p1;
    await t.flush();
    assert.equal(t.puts().length, 2, "exactly one follow-up save");
    assert.equal(t.puts()[1].body.rows[0][COLS[0]], "second");
});

test("load error shows an escaped message with Retry", async () => {
    const t = await setup();
    t.w.fetch = async () => ({ ok: false, status: 500, json: async () => ({ error: "<b>boom</b>", code: "internal_error" }) });
    await t.w.loadTableRows(7, "c1", null);
    const html = t.$("#table-container-c1").innerHTML;
    assert.match(html, /&lt;b&gt;boom&lt;\/b&gt;/);
    assert.match(html, /Retry/);
});
