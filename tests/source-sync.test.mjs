import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../src/main.js", import.meta.url), "utf8");

function productionFunction(name) {
  const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  assert.notEqual(start, -1, `Missing function ${name}`);
  const rest = source.slice(start);
  const next = rest.slice(1).search(/^(?:async )?function /m);
  return next === -1 ? rest : rest.slice(0, next + 1);
}

async function poll(generation, syncState) {
  let callback;
  const context = vm.createContext({
    AbortController, URLSearchParams, encodeURIComponent,
    window: { clearTimeout() {}, setTimeout(fn) { callback = fn; return 1; } },
    console,
    statusResponse: { generation, source_sync: { state: syncState } },
  });
  vm.runInContext(`
    let sourceSyncPollTimer = null;
    let currentData = { generation: 1, sync: { state: "idle" }, fresh_at: null };
    let activeProvider = "all", activeTableView = "requests";
    let requestSnapshot = "old", requestPage = 2;
    let requestsPayload = { snapshot: "old", items: [{ timestamp: "2026-10-01" }] };
    let requestsController = null, requestsError = null;
    const usageCache = new Map(), diagnosticsCache = new Map(), diagnosticsErrors = new Map();
    const requestChildrenControllers = new Map(), requestChildrenErrors = new Map();
    const requestChildrenLoading = new Set(), expandedRequestGroups = new Set();
    let renderedItems = requestsPayload.items, requestsFetched = 0;
    function updateTableView() { renderedItems = requestsPayload?.items || []; }
    function requestsParams() {
      const params = new URLSearchParams({page: String(requestPage)});
      if (requestSnapshot) params.set("snapshot", requestSnapshot);
      return params;
    }
    function syncUrl() {}
    function render(data) { currentData = data; }
    async function refresh() {
      currentData = { generation: statusResponse.generation, sync: statusResponse.source_sync };
      await ensureRequests(currentData);
    }
    async function fetch(url) {
      if (url.startsWith("/api/unibase/status")) {
        return { ok: true, json: async () => statusResponse };
      }
      requestsFetched++;
      if (new URLSearchParams(url.split("?")[1]).has("snapshot")) throw new Error("Old snapshot reused");
      return { ok: true, json: async () => ({
        snapshot: "new", page: 1, items: [{ timestamp: "2026-10-23" }]
      }) };
    }
  ` + ["invalidateRequests", "invalidateDashboardCaches", "ensureRequests", "scheduleSourceSyncPoll"]
    .map(productionFunction).join("\n"), context);
  vm.runInContext("scheduleSourceSyncPoll(currentData)", context);
  await callback();
  return JSON.parse(vm.runInContext("JSON.stringify({renderedItems, requestPage, requestsFetched, requestSnapshot})", context));
}

test("completed source sync replaces old Requests snapshot with current events", async () => {
  const result = await poll(2, "idle");
  assert.deepEqual(result.renderedItems, [{ timestamp: "2026-10-23" }]);
  assert.equal(result.requestSnapshot, "new");
  assert.equal(result.requestPage, 1);
  assert.equal(result.requestsFetched, 1);
});

test("unchanged or running source sync preserves Requests pagination", async () => {
  for (const [generation, state] of [[1, "idle"], [2, "running"]]) {
    const result = await poll(generation, state);
    assert.equal(result.requestPage, 2);
    assert.equal(result.requestSnapshot, "old");
    assert.equal(result.requestsFetched, 0);
  }
});
