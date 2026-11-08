// Runs the real app/static/wizard.js exhibit functions in Node with a fake DOM and fetch.
// Run from the repo root:  node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../../app/static/wizard.js", import.meta.url), "utf8");

function makeSandbox(fetchImpl) {
    const elements = {};
    const el = (id) => (elements[id] ??= { id, innerHTML: "", textContent: "", className: "" });
    const sandbox = {
        console: { log() {}, error() {}, warn() {} },
        document: { addEventListener() {}, getElementById: (id) => el(id) },
        fetch: fetchImpl,
        alert() {},
        confirm: () => true,
        setTimeout: () => 0,
        FormData: class { append() {} },
        loadCompleteness: async () => {},
        loadSubsections: async () => {},
    };
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    return { sandbox, el };
}

const json = (status, body) => ({
    ok: status >= 200 && status < 300, status,
    json: async () => body,
});
const html = (status) => ({
    ok: false, status,
    json: async () => { throw new SyntaxError("Unexpected token <"); },
});

test("403 shows a readable access message and a Retry button (the alpha-tester bug)", async () => {
    const { sandbox, el } = makeSandbox(async () => json(403, { error: "Forbidden", message: "You do not have access to this application" }));
    el("exhibit-container-ex1");
    await sandbox.loadExhibits(7, "ex1");
    const out = el("exhibit-container-ex1").innerHTML;
    assert.match(out, /Couldn't load attachments: Forbidden/);
    assert.match(out, /Retry/);
    assert.match(out, /role="alert"/);
});

test("structured server error message is shown, not a generic one", async () => {
    const { sandbox, el } = makeSandbox(async () => json(500, { error: "Attachments could not be loaded. Please refresh and try again.", code: "database_error" }));
    el("exhibit-container-ex1");
    await sandbox.loadExhibits(7, "ex1");
    assert.match(el("exhibit-container-ex1").innerHTML, /Please refresh and try again/);
});

test("non-JSON error body (proxy HTML page) falls back to a status-based message", async () => {
    const { sandbox, el } = makeSandbox(async () => html(503));
    el("exhibit-container-ex1");
    await sandbox.loadExhibits(7, "ex1");
    assert.match(el("exhibit-container-ex1").innerHTML, /temporarily unavailable/);
});

test("network failure shows a connection message", async () => {
    const { sandbox, el } = makeSandbox(async () => { throw new TypeError("Failed to fetch"); });
    el("exhibit-container-ex1");
    await sandbox.loadExhibits(7, "ex1");
    assert.match(el("exhibit-container-ex1").innerHTML, /Network error/);
});

test("empty list renders the required-exhibit state and the upload control", async () => {
    const { sandbox, el } = makeSandbox(async () => json(200, []));
    el("exhibit-container-ex1");
    await sandbox.loadExhibits(7, "ex1");
    const out = el("exhibit-container-ex1").innerHTML;
    assert.match(out, /EXHIBIT REQUIRED/);
    assert.match(out, /type="file"/);
});

test("files render with escaped names", async () => {
    const { sandbox, el } = makeSandbox(async () => json(200, [{ id: 1, filename: "<img src=x onerror=1>.pdf", file_size_bytes: 2048 }]));
    el("exhibit-container-ex1");
    await sandbox.loadExhibits(7, "ex1");
    const out = el("exhibit-container-ex1").innerHTML;
    assert.match(out, /&lt;img src=x onerror=1&gt;\.pdf/);
    assert.doesNotMatch(out, /<img src=x/);
});

test("error messages are escaped before being inserted as HTML", async () => {
    const { sandbox, el } = makeSandbox(async () => json(500, { error: "<script>alert(1)</script>" }));
    el("exhibit-container-ex1");
    await sandbox.loadExhibits(7, "ex1");
    const out = el("exhibit-container-ex1").innerHTML;
    assert.doesNotMatch(out, /<script>/);
    assert.match(out, /&lt;script&gt;/);
});

test("upload failure with HTML body (e.g. 413 from a proxy) shows a size message, not a JSON parse error", async () => {
    const { sandbox, el } = makeSandbox(async () => html(413));
    el("exhibit-file-ex1").files = [{ name: "big.pdf" }];
    el("status-ex1");
    await sandbox.uploadExhibit(7, "ex1");
    const status = el("status-ex1");
    assert.equal(status.className, "save-indicator save-error");
    assert.match(status.textContent, /too large/);
    assert.doesNotMatch(status.textContent, /Unexpected token/);
});

test("upload storage error shows the server's actionable message", async () => {
    const { sandbox, el } = makeSandbox(async () => json(503, { error: "File storage is temporarily unavailable. Please try again in a minute.", code: "storage_unavailable" }));
    el("exhibit-file-ex1").files = [{ name: "a.pdf" }];
    el("status-ex1");
    await sandbox.uploadExhibit(7, "ex1");
    assert.match(el("status-ex1").textContent, /Upload failed — File storage is temporarily unavailable/);
});
