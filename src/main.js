import "./styles.css";
import { compactNumber, formatDuration } from "./format.js";
import { activityMaxSeconds, activityTicks, chartBarSizing, chartHeightPercent } from "./chart-scale.js";
import { chartColorMap, modelColorForSlot } from "./chart-colors.js";
import { normalizeProvider, providerOptions } from "./provider-state.js";
import { paginateRows, truncateModelName, USAGE_TABLE_PAGE_SIZE } from "./usage-table.js";
import {
  WITH_CACHE,
  WITHOUT_CACHE,
  metricValue,
  resolveCacheMode
} from "./visualization-accounting.js";

const app = document.querySelector("#app");
const tooltip = document.createElement("div");
tooltip.className = "heat-tooltip";
document.body.appendChild(tooltip);
const modelNameTooltip = document.createElement("div");
modelNameTooltip.className = "heat-tooltip model-name-tooltip";
modelNameTooltip.setAttribute("role", "tooltip");
document.body.appendChild(modelNameTooltip);

const rangeOptions = [
  { value: "all", label: "All" },
  { value: "30d", label: "30d" },
  { value: "7d", label: "7d" },
  { value: "1d", label: "1d" },
  { value: "custom", label: "Custom" }
];
const chartRangeOptions = [
  { value: "30d", label: "30d" },
  { value: "90d", label: "90d" },
  { value: "6m", label: "6m" },
  { value: "1y", label: "1y" },
  { value: "all", label: "All" },
  { value: "custom", label: "Custom" }
];
const activityChartRangeOptions = [
  { value: "3d", label: "3d" },
  { value: "7d", label: "7d" },
  { value: "14d", label: "14d" },
  { value: "21d", label: "21d" },
  { value: "30d", label: "30d" },
  { value: "custom", label: "Custom" }
];
const visualizationOptions = [
  { value: "heatmap", label: "Daily heatmap" },
  { value: "tokens", label: "Tokens over time" },
  { value: "activity", label: "Active time" }
];
const chartRangeDefaults = { heatmap: "all", tokens: "30d", activity: "30d" };
const accountingOptions = [
  { value: WITH_CACHE, label: "With cache" },
  { value: WITHOUT_CACHE, label: "Without cache" }
];
const requestGroupOptions = ["none", "1m", "15m", "30m", "1h", "6h", "12h", "24h"];
const requestPageSizes = [10, 25, 50, 100];
const weekdayOptions = [
  { short: "Mon", full: "Monday" },
  { short: "Tue", full: "Tuesday" },
  { short: "Wed", full: "Wednesday" },
  { short: "Thu", full: "Thursday" },
  { short: "Fri", full: "Friday" },
  { short: "Sat", full: "Saturday" },
  { short: "Sun", full: "Sunday" }
];
const iconPaths = {
  brand: '<path d="m12 3.5 7 4v9l-7 4-7-4v-9l7-4Z"/><polyline points="8.2 12 10.7 14.5 15.8 9.4"/>',
  sessions: '<circle cx="12" cy="8" r="3.2"/><path d="M5 20c.6-3.2 3.2-5 7-5s6.4 1.8 7 5"/>',
  input: '<path d="M12 3v11"/><polyline points="8 10 12 14 16 10"/><path d="M5 17v3h14v-3"/>',
  output: '<path d="M12 21V10"/><polyline points="8 14 12 10 16 14"/><path d="M5 7V4h14v3"/>',
  calculator: '<rect x="5" y="3" width="14" height="18" rx="2"/><line x1="8" y1="7" x2="16" y2="7"/><circle cx="9" cy="11" r=".7"/><circle cx="15" cy="11" r=".7"/><circle cx="9" cy="16" r=".7"/><circle cx="15" cy="16" r=".7"/>',
  cache: '<path d="M5 6c0-1.7 3.1-3 7-3s7 1.3 7 3-3 3-7 3-7-1.3-7-3Z"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
  layers: '<path d="m12 3 8 4-8 4-8-4 8-4Z"/><path d="m4 12 8 4 8-4"/><path d="m4 17 8 4 8-4"/>',
  calendar: '<rect x="4" y="5" width="16" height="15" rx="2"/><line x1="8" y1="3" x2="8" y2="7"/><line x1="16" y1="3" x2="16" y2="7"/><line x1="4" y1="10" x2="20" y2="10"/><line x1="8" y1="14" x2="8" y2="14"/><line x1="12" y1="14" x2="12" y2="14"/><line x1="16" y1="14" x2="16" y2="14"/>',
  coin: '<circle cx="12" cy="12" r="8"/><path d="M12 7v10"/><path d="M15 9.5c-.7-.7-1.6-1-2.8-1-1.6 0-2.7.8-2.7 2s1.1 1.8 2.7 2.2c1.7.4 2.7 1.1 2.7 2.3s-1.1 2-2.8 2c-1.2 0-2.3-.4-3-1.1"/>',
  star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-2.9-5.6 2.9 1.1-6.2L3 9.6l6.2-.9L12 3Z"/>',
  flame: '<path d="M12.5 3.5c.8 3.3-1.7 4.8-3.1 6.8-1.1 1.5-1.4 3.1-.6 4.6.2-1.8 1.2-3 2.5-3.9-.2 2.9 2.8 3.3 2.1 6.1 1.7-.9 3-2.6 3-4.6 0-2.5-1.7-4.8-3.9-9Z"/><path d="M8.8 20.2c-2.4-.8-4-2.7-4-5 0-2.1 1.1-3.8 2.5-5.2"/>',
  trophy: '<path d="M8 4h8v4.5a4 4 0 0 1-8 0V4Z"/><path d="M8 6H4v1a4 4 0 0 0 4 4"/><path d="M16 6h4v1a4 4 0 0 1-4 4"/><path d="M12 12.5V17"/><path d="M8 21h8"/><path d="M9 17h6v4H9z"/>',
  chart: '<line x1="4" y1="20" x2="20" y2="20"/><line x1="6" y1="20" x2="6" y2="12"/><line x1="11" y1="20" x2="11" y2="7"/><line x1="16" y1="20" x2="16" y2="4"/><polyline points="4 8 9 5 13 7 20 3"/>',
  database: '<rect x="4" y="4" width="16" height="16" rx="2"/><line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/><circle cx="8" cy="6.5" r=".8"/><circle cx="8" cy="12" r=".8"/><circle cx="8" cy="18" r=".8"/><line x1="11" y1="6.5" x2="17" y2="6.5"/><line x1="11" y1="12" x2="17" y2="12"/><line x1="11" y1="18" x2="17" y2="18"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21h-4v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H3v-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3h4v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.1v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
  refresh: '<path d="M20 6v5h-5"/><path d="M18.2 9A7 7 0 1 0 19 15"/>',
  info: '<circle cx="12" cy="12" r="9"/><line x1="12" y1="10.5" x2="12" y2="16"/><circle cx="12" cy="7.5" r=".7"/>',
  usage: '<polyline points="3 13 7 13 9.5 6 14 18 16.5 11 21 11"/>',
  models: '<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/><line x1="10" y1="7" x2="14" y2="7"/><line x1="7" y1="10" x2="7" y2="14"/><line x1="17" y1="10" x2="17" y2="14"/>'
};
const providerLogoPaths = {
  all: 'M4 7.5 8 5l4 2.5L16 5l4 2.5v5L16 15l-4-2.5L8 15l-4-2.5v-5Zm4 2.5v5m4-7.5v5m4-7.5v5m-8 5 4 2.5 4-2.5m-4-2.5V18',
  codex: 'M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654 2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z',
  claude: 'm4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z',
  opencode: 'M7 4.5h10l3 7.5-3 7.5H7L4 12l3-7.5Zm2.5 4L8 12l1.5 3.5h5L16 12l-1.5-3.5h-5Z'
};

const initialState = readUrlState();
let activeProvider = initialState.provider;
let activeRange = initialState.range;
let customRangePending = false;
let customRangeOpen = false;
let customStartDate = initialState.start;
let customEndDate = initialState.end;
let activeChartRange = initialState.chartRange;
let chartCustomRangePending = false;
let chartCustomRangeOpen = false;
const chartStateByVisualization = {
  heatmap: { range: chartRangeDefaults.heatmap, start: "", end: "" },
  tokens: { range: chartRangeDefaults.tokens, start: "", end: "" },
  activity: { range: chartRangeDefaults.activity, start: "", end: "" },
  [initialState.visualization]: {
    range: initialState.chartRange,
    start: initialState.chartStart,
    end: initialState.chartEnd
  }
};
let chartStartDate = initialState.chartStart;
let chartEndDate = initialState.chartEnd;
let activeVisualization = initialState.visualization;
let cacheMode = initialState.cacheMode;
let workdaysOnly = initialState.workdaysOnly;
let activeTableView = initialState.view;
let currentData = null;
let diagnosticsController = null;
let diagnosticsRequestKey = null;
let diagnosticsTimer = null;
let usageLoadTimer = null;
let usageController = null;
let sourceSyncPollTimer = null;
const usageCache = new Map();
const diagnosticsCache = new Map();
const diagnosticsErrors = new Map();
const expandedModels = new Set();
let dailyUsagePage = 1;
let modelUsagePage = 1;
let usageTableScope = "";
let modelTooltipTimer = null;
let requestsController = null;
let requestsPayload = null;
let requestsError = null;
let requestGroup = initialState.requestGroup;
let requestPage = initialState.requestPage;
let requestPageSize = initialState.requestPageSize;
let requestSnapshot = null;
const expandedRequestGroups = new Set();
const requestChildrenLoading = new Set();
const requestChildrenErrors = new Map();
const requestChildrenControllers = new Map();
let settingsOpen = false;
let settingsLoading = false;
let settingsData = null;
let settingsDraft = null;
let settingsError = null;
let settingsApplyPending = false;
let settingsApplied = false;
let settingsActiveTab = "general";
let resetConfirmOpen = false;
let resetConfirmation = "";
let operationPollTimer = null;
let manualRefreshPending = false;

const numberFormatter = new Intl.NumberFormat("en-US");
const percentageFormatter = new Intl.NumberFormat("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const moneyFormatter = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const monthFormatter = new Intl.DateTimeFormat("en-US", { month: "short" });

function full(value) {
  return numberFormatter.format(Number(value || 0));
}

function money(value) {
  return moneyFormatter.format(Number(value || 0));
}

function formatTimestamp(value, fallback = "Not synced") {
  if (!value) return fallback;
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return fallback;
  return timestamp.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function requestDayKey(value, timezone) {
  const parts = new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: timezone
  }).formatToParts(value);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function formatRequestDateTime(value, timezone, includeSeconds = true) {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return String(value || "");
  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: includeSeconds ? "2-digit" : undefined,
    hourCycle: "h23",
    timeZone: timezone
  }).format(timestamp);
}

function formatRequestWindow(startValue, endValue, timezone) {
  const start = new Date(startValue);
  const end = new Date(endValue);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return String(startValue || "");
  if (requestDayKey(start, timezone) !== requestDayKey(end, timezone)) {
    return `${formatRequestDateTime(startValue, timezone, false)} – ${formatRequestDateTime(endValue, timezone, false)}`;
  }
  const date = new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: timezone
  }).format(start);
  const time = new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: timezone
  });
  return `${date} · ${time.format(start)}–${time.format(end)}`;
}

function requestTimezoneLabel(payload) {
  const values = payload.items.flatMap((item) => [item.bucket_start, item.bucket_end, item.local_timestamp]).filter(Boolean);
  const offsets = new Set(values.map((value) => {
    const match = String(value).match(/(Z|[+-]\d{2}:\d{2})$/);
    if (!match) return null;
    return match[1] === "Z" ? "+00:00" : match[1];
  }).filter(Boolean));
  const offset = offsets.size === 1 ? ` (UTC${[...offsets][0]})` : "";
  return `${payload.timezone || "UTC"}${offset}`;
}

function formatMegabytes(bytes) {
  return `${(Number(bytes || 0) / (1024 * 1024)).toFixed(2)} MB`;
}

function resetUsageTablePages() {
  dailyUsagePage = 1;
  modelUsagePage = 1;
}

function usageTableScopeKey(data) {
  return [data.provider, data.range, data.range_start || "", data.range_end || "", data.merge_models_across_providers ? "merged" : "split"].join(":");
}

function modelDisplayName(data, row) {
  const model = String(row.model || "");
  if (data.provider !== "all" || data.merge_models_across_providers) return model;
  const providerLabel = providerOptions.find((option) => option.value === row.provider)?.label || row.provider;
  if (model.toLowerCase().startsWith(`${providerLabel.toLowerCase()} ·`)) return model;
  return `${providerLabel} · ${model}`;
}

function localDayKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function todayKey() {
  return localDayKey(new Date());
}

function isIsoDate(value) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value || "") && !Number.isNaN(new Date(`${value}T00:00:00`).getTime());
}

function parseDay(value) {
  if (!isIsoDate(value)) return null;
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day);
}

function normalizeCustomRange() {
  if (!isIsoDate(customStartDate)) customStartDate = todayKey();
  if (!isIsoDate(customEndDate)) customEndDate = customStartDate;
  if (customStartDate > customEndDate) {
    [customStartDate, customEndDate] = [customEndDate, customStartDate];
  }
}

function normalizeChartCustomRange() {
  if (!isIsoDate(chartStartDate)) chartStartDate = todayKey();
  if (!isIsoDate(chartEndDate)) chartEndDate = chartStartDate;
  if (chartStartDate > chartEndDate) {
    [chartStartDate, chartEndDate] = [chartEndDate, chartStartDate];
  }
}

function saveActiveChartState() {
  chartStateByVisualization[activeVisualization] = {
    range: activeChartRange,
    start: chartStartDate,
    end: chartEndDate
  };
}

function restoreChartState(visualization) {
  const state = chartStateByVisualization[visualization];
  activeChartRange = state.range;
  chartStartDate = state.start;
  chartEndDate = state.end;
}

function chartRangeOptionsFor(visualization) {
  return visualization === "activity" ? activityChartRangeOptions : chartRangeOptions;
}

function positionCustomRangeDialog(dialog, anchor) {
  const margin = 8;
  const gap = 8;
  const viewport = window.visualViewport;
  const viewportLeft = viewport?.offsetLeft || 0;
  const viewportTop = viewport?.offsetTop || 0;
  const viewportWidth = viewport?.width || window.innerWidth;
  const viewportHeight = viewport?.height || window.innerHeight;
  const anchorRect = anchor.getBoundingClientRect();
  const dialogRect = dialog.getBoundingClientRect();
  const maxLeft = viewportLeft + viewportWidth - dialogRect.width - margin;
  const maxTop = viewportTop + viewportHeight - dialogRect.height - margin;
  let left = anchorRect.right - dialogRect.width;
  let top = anchorRect.bottom + gap;

  left = Math.max(viewportLeft + margin, Math.min(left, maxLeft));
  if (top > maxTop) top = anchorRect.top - dialogRect.height - gap;
  top = Math.max(viewportTop + margin, Math.min(top, maxTop));

  dialog.style.left = `${left}px`;
  dialog.style.top = `${top}px`;
}

function readUrlState() {
  const params = new URLSearchParams(window.location.search);
  const provider = params.get("provider") || "all";
  const range = params.get("range") || "all";
  const visualization = params.get("visualization") || "heatmap";
  const normalizedVisualization = visualizationOptions.some((option) => option.value === visualization) ? visualization : "heatmap";
  const chartRange = params.get("chart_range") || chartRangeDefaults[normalizedVisualization];
  const selectedPageSize = Number.parseInt(params.get("page_size") || "25", 10);
  return {
    provider: normalizeProvider(provider),
    range: rangeOptions.some((option) => option.value === range) ? range : "all",
    start: params.get("start") || "",
    end: params.get("end") || "",
    chartRange: chartRangeOptionsFor(normalizedVisualization).some((option) => option.value === chartRange)
      ? chartRange
      : chartRangeDefaults[normalizedVisualization],
    chartStart: params.get("chart_start") || "",
    chartEnd: params.get("chart_end") || "",
    visualization: normalizedVisualization,
    cacheMode: resolveCacheMode(params.get("cache")),
    workdaysOnly: params.get("workdays") === "1",
    view: ["usage", "diagnostics", "requests"].includes(params.get("view")) ? params.get("view") : "usage",
    requestGroup: requestGroupOptions.includes(params.get("group")) ? params.get("group") : "none",
    requestPage: Math.max(Number.parseInt(params.get("page") || "1", 10) || 1, 1),
    requestPageSize: requestPageSizes.includes(selectedPageSize) ? selectedPageSize : 25
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function buildQuery(rangeName, includeDiagnostics = false) {
  const params = new URLSearchParams();
  params.set("provider", activeProvider);
  params.set("range", rangeName);
  params.set("chart_range", activeChartRange);
  params.set("visualization", activeVisualization);
  params.set("cache", cacheMode);
  if (workdaysOnly) params.set("workdays", "1");
  params.set("timezone", Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  params.set("view", activeTableView);
  if (activeTableView === "requests") {
    params.set("group", requestGroup);
    params.set("page", String(requestPage));
    params.set("page_size", String(requestPageSize));
  }
  if (rangeName === "custom") {
    normalizeCustomRange();
    params.set("start", customStartDate);
    params.set("end", customEndDate);
  }
  if (activeChartRange === "custom") {
    normalizeChartCustomRange();
    params.set("chart_start", chartStartDate);
    params.set("chart_end", chartEndDate);
  }
  if (includeDiagnostics) params.set("include_diagnostics", "1");
  return params.toString();
}

function syncUrl() {
  history.replaceState(null, "", `/?${buildQuery(activeRange)}`);
}

function describeRange(data) {
  if (data.range === "custom" && data.range_start && data.range_end) {
    return `${data.range_start} - ${data.range_end}`;
  }
  if (data.range === "1d" && data.range_start) {
    return data.range_start;
  }
  if (data.range === "7d") return "Last 7 days";
  if (data.range === "30d") return "Last 30 days";
  return "All time";
}

function describeChartRange(chart) {
  if (chart.range === "custom" && chart.range_start && chart.range_end) {
    return `${chart.range_start} - ${chart.range_end}`;
  }
  if (chart.range === "3d") return "Last 3 days";
  if (chart.range === "30d") return "Last 30 days";
  if (chart.range === "21d") return "Last 21 days";
  if (chart.range === "14d") return "Last 14 days";
  if (chart.range === "7d") return "Last 7 days";
  if (chart.range === "90d") return "Last 90 days";
  if (chart.range === "6m") return "Last 6 months";
  if (chart.range === "1y") return "Last year";
  return "All time";
}

function dayLabel(day, fallbackDay) {
  if (day) return day;
  const date = parseDay(fallbackDay);
  if (!date) return fallbackDay;
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(date);
}

function heatmapCells(daily, rangeName, rangeStart, rangeEnd, accountingMode) {
  const byDay = new Map(daily.map((row) => [row.day, row]));
  const today = new Date();
  const maxTokens = Math.max(0, ...daily.map((row) => metricValue(row, accountingMode)));
  let first = daily.length ? parseDay(daily[0].day) : new Date(today.getFullYear(), today.getMonth(), today.getDate());
  let last = new Date(Math.max(today.getTime(), daily.length ? parseDay(daily.at(-1).day)?.getTime() || today.getTime() : today.getTime()));

  if (rangeName !== "all") {
    first = parseDay(rangeStart) || first;
    last = parseDay(rangeEnd) || first;
  }

  const day = first.getDay() || 7;
  first.setDate(first.getDate() - day + 1);

  const cells = [];
  for (const cursor = new Date(first); cursor <= last; cursor.setDate(cursor.getDate() + 1)) {
    const key = localDayKey(cursor);
    const row = byDay.get(key) || { sessions: 0, total_tokens: 0, total_with_cached_tokens: 0 };
    const tokens = metricValue(row, accountingMode);
    let level = 0;
    if (tokens && maxTokens) {
      if (tokens < maxTokens * 0.2) level = 1;
      else if (tokens < maxTokens * 0.45) level = 2;
      else if (tokens < maxTokens * 0.7) level = 3;
      else level = 4;
    }
    cells.push({ day: key, sessions: row.sessions || 0, tokens, level });
  }
  return cells;
}

function renderVisualizationPanel(data, heat, months, heatColumns) {
  const accountingLabel = cacheMode === WITH_CACHE ? "With cache" : "Without cache";
  const activityMode = activeVisualization === "activity";
  const title = {
    heatmap: "Daily Heatmap",
    tokens: "Tokens over time",
    activity: "Active time"
  }[activeVisualization];
  const note = activityMode
    ? `Showing ${escapeHtml(describeChartRange(data.chart))} · ${full(data.activity?.idle_timeout_minutes || 10)}-minute inactivity timeout · all models combined${workdaysOnly ? " · Workdays only · non-working days are dimmed and excluded from totals" : ""}`
    : `Showing ${escapeHtml(describeChartRange(data.chart))} · ${accountingLabel}`;
  return `
    <div class="daily-visualization">
      <div class="viz-header">
        <div>
          <div class="section-title">
            <span class="section-icon tone-cyan">${icon("usage")}</span>
            <div>
              <h2>${title}</h2>
              <div class="viz-note">${note}</div>
            </div>
          </div>
        </div>
        <div class="viz-controls">
          <div class="viz-primary-controls">
            ${activityMode
              ? `<button class="workdays-toggle" id="workdays-only" type="button" data-workdays-only aria-pressed="${workdaysOnly}"><span class="workdays-toggle-indicator" aria-hidden="true"></span><span>Workdays only</span></button>`
              : `<nav class="segments accounting-tabs" aria-label="Token accounting">
                ${accountingOptions.map((option) => `<button class="seg ${cacheMode === option.value ? "active" : ""}" type="button" data-cache-mode="${option.value}" aria-pressed="${cacheMode === option.value}">${option.label}</button>`).join("")}
              </nav>`}
          </div>
          <div class="chart-filter">
            <div class="chart-filter-scroll">
              <nav class="segments chart-range-tabs" aria-label="Visualization range">
                ${chartRangeOptionsFor(activeVisualization).map((option) => `<button class="seg ${activeChartRange === option.value ? "active" : ""}" type="button" data-chart-range="${option.value}" aria-pressed="${activeChartRange === option.value}" ${option.value === "custom" ? `id="chart-range-trigger" aria-haspopup="dialog" aria-expanded="${chartCustomRangeOpen}"` : ""}>${option.label}</button>`).join("")}
              </nav>
            </div>
          </div>
        </div>
      </div>
      ${chartCustomRangeOpen ? `
        <dialog class="custom-range-dialog" id="chart-range-dialog" aria-labelledby="chart-range-title">
          <form class="custom-range" id="chart-range-form">
            <div class="custom-range-heading">
              <strong id="chart-range-title">Custom visualization range</strong>
              <button class="custom-range-close" type="button" aria-label="Close custom visualization range">×</button>
            </div>
            <label>
              <span>From</span>
              <input id="chart-custom-start" type="date" name="chart_start" value="${escapeHtml(chartStartDate)}" required>
            </label>
            <label>
              <span>To</span>
              <input id="chart-custom-end" type="date" name="chart_end" value="${escapeHtml(chartEndDate)}" required>
            </label>
            <button class="custom-apply" type="submit">Apply</button>
          </form>
        </dialog>
      ` : ""}
      ${activeVisualization === "activity"
        ? renderActiveTime(data.activity)
        : activeVisualization === "tokens"
          ? renderTokensOverTime(data.chart, cacheMode)
          : renderHeatmap(heat, months, heatColumns, cacheMode)}
    </div>
  `;
}

function renderHeatmap(heat, months, heatColumns, accountingMode) {
  const accountingLabel = accountingMode === WITH_CACHE ? "with cache" : "without cache";
  return `
    <div class="heat-wrap">
      <div class="heatmap-shell">
        <div class="heatmap" style="grid-template-columns: repeat(${heatColumns}, 16px)">
          ${heat.map((cell) => `<div class="heat-cell level-${cell.level}" aria-label="${cell.day}: ${full(cell.tokens)} tokens ${accountingLabel}" data-tooltip-date="${cell.day}" data-tooltip-tokens="${full(cell.tokens)} tokens ${accountingLabel}"></div>`).join("")}
        </div>
        <div class="month-labels" style="grid-template-columns: repeat(${heatColumns}, 16px)">
          ${months.map((month) => `<span style="grid-column: ${month.column}">${escapeHtml(month.label)}</span>`).join("")}
        </div>
      </div>
    </div>
  `;
}

function heatmapScrollPosition() {
  const wrap = document.querySelector(".heat-wrap");
  if (!wrap) return null;
  return {
    firstDay: wrap.querySelector(".heat-cell:first-child")?.dataset.tooltipDate,
    lastDay: wrap.querySelector(".heat-cell:last-child")?.dataset.tooltipDate,
    rightOffset: Math.max(0, wrap.scrollWidth - wrap.clientWidth - wrap.scrollLeft),
  };
}

function restoreHeatmapScroll(position) {
  const wrap = document.querySelector(".heat-wrap");
  if (!wrap) return;
  const current = heatmapScrollPosition();
  const sameRange = position && position.firstDay === current.firstDay && position.lastDay === current.lastDay;
  // Start at recent dates; retain the user's position when the same range rerenders.
  wrap.scrollLeft = wrap.scrollWidth - wrap.clientWidth - (sameRange ? position.rightOffset : 0);
}

function renderTokensOverTime(chart, accountingMode) {
  const days = chart.days || [];
  const models = chart.models || [];
  const colors = chartColorMap(models);
  const maxTokens = Math.max(1, ...days.map((day) => metricValue(day, accountingMode)));
  const ticks = [1, 0.75, 0.5, 0.25, 0].map((ratio) => Math.round(maxTokens * ratio));
  const labelEvery = Math.max(1, Math.ceil(days.length / 10));
  const { barGap, barFill, barMax } = chartBarSizing(chart.granularity, days.length);

  if (!days.length) {
    return '<div class="chart-empty">No usage in this chart range.</div>';
  }

  return `
    <div class="chart-shell">
      <div class="chart-y-axis">
        ${ticks.map((tick) => `<span>${compactNumber(tick)}</span>`).join("")}
      </div>
      <div class="chart-scroll">
        <div class="bar-chart" style="--bar-count: ${days.length}; --bar-gap: ${barGap}px; --bar-fill: ${barFill}%; --bar-max: ${barMax}px">
          <div class="chart-grid">
            ${ticks.map(() => '<span></span>').join("")}
          </div>
          <div class="chart-v-grid">
            ${days.map(() => "<span></span>").join("")}
          </div>
          <div class="chart-bars">
            ${days.map((day, index) => renderChartBar(day, models, colors, maxTokens, index, labelEvery, days.length, accountingMode)).join("")}
          </div>
        </div>
      </div>
    </div>
    ${renderChartLegend(models, colors)}
  `;
}

function renderActiveTime(activity) {
  if (!activity) {
    return '<div class="chart-empty">Active-time data is unavailable.</div>';
  }
  const days = activity.days || [];
  const dailyScale = activity.granularity === "day";
  const maxSeconds = activityMaxSeconds(days, dailyScale);
  const ticks = activityTicks(maxSeconds);
  const labelEvery = Math.max(1, Math.ceil(days.length / 10));
  const { barGap, barFill, barMax } = chartBarSizing(activity.granularity, days.length);
  const activitySummary = `${full(activity.focus_blocks)} sessions`;
  return `
    <div class="activity-summary" aria-label="Active time summary">
      <div><span>Total active time</span><strong>${formatDuration(activity.total_seconds)}</strong><small>${full(activity.request_count)} requests</small></div>
      <div><span>Average per day</span><strong>${formatDuration(activity.average_seconds_per_day)}</strong><small>Across the selected period</small></div>
      <div><span>Average active day</span><strong>${formatDuration(activity.average_seconds_per_active_day)}</strong><small>Days with recorded activity</small></div>
      <div><span>Activity coverage</span><strong>${full(activity.active_days)} / ${full(activity.period_days)} days</strong><small>${activitySummary}</small></div>
    </div>
    ${days.length ? `
      <div class="chart-shell activity-chart-shell">
        <div class="chart-y-axis">
          ${ticks.map((tick) => `<span>${dailyScale && tick === 0 ? "" : formatDuration(tick)}</span>`).join("")}
        </div>
        <div class="chart-scroll">
          <div class="bar-chart" style="--bar-count: ${days.length}; --bar-gap: ${barGap}px; --bar-fill: ${barFill}%; --bar-max: ${barMax}px">
            <div class="chart-grid">${ticks.map(() => '<span></span>').join("")}</div>
            <div class="chart-v-grid">${days.map(() => "<span></span>").join("")}</div>
            <div class="chart-bars">
              ${days.map((day, index) => renderActivityBar(day, maxSeconds, index, labelEvery, days.length)).join("")}
            </div>
          </div>
        </div>
      </div>
    ` : '<div class="chart-empty">No requests in this chart range.</div>'}
  `;
}

function renderActivityBar(day, maxSeconds, index, labelEvery, dayCount) {
  const seconds = Number(day.active_seconds || 0);
  const excludedSeconds = Number(day.excluded_active_seconds || 0);
  const rawSeconds = Number(day.raw_active_seconds ?? seconds + excludedSeconds);
  const height = chartHeightPercent(rawSeconds, maxSeconds);
  const includedHeight = rawSeconds ? (seconds / rawSeconds) * 100 : 0;
  const excludedHeight = rawSeconds ? (excludedSeconds / rawSeconds) * 100 : 0;
  const label = index % labelEvery === 0 || index === dayCount - 1 ? dayLabel(day.label, day.day) : "";
  const title = day.bucket_start && day.bucket_end && day.bucket_start !== day.bucket_end
    ? `${day.bucket_start} - ${day.bucket_end}`
    : day.day;
  const requestCount = full(day.raw_request_count ?? day.request_count);
  const barLabel = `${title}: ${formatDuration(rawSeconds)} active, ${requestCount} requests${day.fully_excluded ? ", excluded non-working day" : ""}`;
  const includedLabel = `${title}: ${formatDuration(seconds)} counted, ${full(day.request_count)} requests counted`;
  const excludedLabel = `${title}: ${formatDuration(excludedSeconds)} excluded, ${full(day.excluded_request_count)} requests, non-working days`;
  const includedTooltip = workdaysOnly
    ? ` tabindex="0" aria-label="${escapeHtml(includedLabel)}" data-tooltip-title="${escapeHtml(title)}" data-tooltip-body="${formatDuration(seconds)} counted · ${full(day.request_count)} requests counted"`
    : "";
  return `
    <div class="bar-slot ${day.fully_excluded ? "excluded-day" : ""}" tabindex="0" aria-label="${escapeHtml(barLabel)}" data-tooltip-title="${escapeHtml(title)}" data-tooltip-body="${formatDuration(rawSeconds)} active · ${requestCount} requests${day.fully_excluded ? "<br>Excluded non-working day" : ""}">
      <div class="stacked-bar activity-time-bar ${day.fully_excluded ? "excluded" : ""} ${rawSeconds ? "" : "empty"}" style="height: ${height}%">
        ${seconds ? `<div class="activity-time-segment included" style="height:${includedHeight}%"${includedTooltip}></div>` : ""}
        ${excludedSeconds ? `<div class="activity-time-segment excluded" tabindex="0" aria-label="${escapeHtml(excludedLabel)}" style="height:${excludedHeight}%" data-tooltip-title="${escapeHtml(title)}" data-tooltip-body="${formatDuration(excludedSeconds)} excluded · ${full(day.excluded_request_count)} requests · non-working days"></div>` : ""}
      </div>
      <div class="bar-label">${escapeHtml(label)}</div>
    </div>
  `;
}

function renderChartBar(day, models, colors, maxTokens, index, labelEvery, dayCount, accountingMode) {
  const tokens = metricValue(day, accountingMode);
  const height = chartHeightPercent(tokens, maxTokens);
  const label = index % labelEvery === 0 || index === dayCount - 1 ? dayLabel(day.label, day.day) : "";
  const title = day.bucket_start && day.bucket_end && day.bucket_start !== day.bucket_end ? `${day.bucket_start} - ${day.bucket_end}` : day.day;
  const accountingLabel = accountingMode === WITH_CACHE ? "with cache" : "without cache";
  return `
    <div class="bar-slot" data-tooltip-title="${escapeHtml(title)}" data-tooltip-body="${full(tokens)} tokens ${accountingLabel}">
      <div class="stacked-bar ${tokens ? "" : "empty"}" style="height: ${height}%">
        ${(day.models || []).map((item) => {
          const itemTokens = metricValue(item, accountingMode);
          const segmentHeight = tokens ? (itemTokens / tokens) * 100 : 0;
          return `<div class="bar-segment" data-chart-model="${escapeHtml(item.model)}" style="height: ${segmentHeight}%; background: ${colors.get(item.model) || modelColorForSlot(0)}" data-tooltip-title="${escapeHtml(item.model)}" data-tooltip-body="${escapeHtml(title)}<br>${full(itemTokens)} tokens ${accountingLabel}"></div>`;
        }).join("")}
      </div>
      <div class="bar-label">${escapeHtml(label)}</div>
    </div>
  `;
}

function renderChartLegend(models, colors) {
  if (!models.length) return "";
  return `
    <div class="chart-legend">
      ${models.map((row) => `
        <span class="legend-item" data-chart-model="${escapeHtml(row.model)}" tabindex="0">
          <span class="legend-swatch" style="background: ${colors.get(row.model) || modelColorForSlot(0)}"></span>
          <span>${escapeHtml(row.model)}</span>
        </span>
      `).join("")}
    </div>
  `;
}

function monthLabels(cells) {
  const labels = [];
  let previousMonth = "";
  cells.forEach((cell, index) => {
    const date = new Date(`${cell.day}T00:00:00`);
    const monthKey = `${date.getFullYear()}-${date.getMonth()}`;
    if (monthKey === previousMonth) return;
    previousMonth = monthKey;
    labels.push({
      label: monthFormatter.format(date).replace(".", ""),
      column: Math.floor(index / 7) + 1
    });
  });
  return labels;
}

async function load(rangeName) {
  if (usageController) usageController.abort();
  if (usageLoadTimer) window.clearInterval(usageLoadTimer);
  usageController = new AbortController();
  const controller = usageController;
  const startedAt = Date.now();
  const query = buildQuery(rangeName);
  const renderLoading = () => {
    const elapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);
    const elapsed = elapsedSeconds >= 2 ? `<span>${full(elapsedSeconds)}s elapsed</span>` : "";
    app.innerHTML = `<section class="state usage-loading" aria-live="polite"><span class="diagnostics-spinner" aria-hidden="true"></span><strong>MeterMesh is loading Unibase…</strong>${elapsed}</section>`;
  };
  if (currentData) {
    document.documentElement.classList.add("usage-refreshing");
  } else {
    renderLoading();
    usageLoadTimer = window.setInterval(renderLoading, 1000);
  }
  console.info("[MeterMesh timing] usage fetch started", { range: rangeName, provider: activeProvider });
  try {
    const response = await fetch(`/data.json?${query}`, { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`Usage API returned HTTP ${response.status}`);
    const data = await response.json();
    usageCache.set(query, data);
    console.info("[MeterMesh timing] usage fetch completed", { elapsedMs: Date.now() - startedAt });
    return data;
  } catch (error) {
    if (error.name !== "AbortError") {
      console.error("[MeterMesh timing] usage fetch failed", { elapsedMs: Date.now() - startedAt, error });
    }
    throw error;
  } finally {
    if (usageController === controller) {
      if (usageLoadTimer) window.clearInterval(usageLoadTimer);
      usageLoadTimer = null;
      usageController = null;
    }
    document.documentElement.classList.remove("usage-refreshing");
  }
}

function renderModelDetails(row) {
  if (!row.daily?.length) {
    return '<div class="detail-empty">No daily usage for this model in the selected range.</div>';
  }
  return `
    <div class="detail-card">
      <div class="detail-meta">Used on ${full(row.active_days)} day(s) in this range.</div>
      <div class="table-scroll">
        <table class="detail-table">
          <thead><tr><th>Date</th><th class="num">Input</th><th class="num">Output</th><th class="num">Total w/o cached</th><th class="num">Cached</th><th class="num">Total</th><th class="num">Cost</th><th class="num">Sessions</th></tr></thead>
          <tbody>
            ${row.daily.map((item) => `<tr><td>${escapeHtml(item.day)}</td><td class="num">${full(item.input_tokens)}</td><td class="num">${full(item.output_tokens)}</td><td class="num">${full(item.total_tokens)}</td><td class="num">${full(item.cached_input_tokens)}</td><td class="num">${full(item.total_with_cached_tokens)}</td><td class="num">${money(item.cost_usd)}</td><td class="num">${full(item.sessions)}</td></tr>`).join("")}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

function paginationItems(page, totalPages) {
  const pages = [...new Set([1, totalPages, page - 1, page, page + 1])]
    .filter((item) => item >= 1 && item <= totalPages)
    .sort((left, right) => left - right);
  const items = [];
  pages.forEach((item, index) => {
    if (index > 0 && item - pages[index - 1] > 1) items.push("ellipsis");
    items.push(item);
  });
  return items;
}

function renderUsagePagination(kind, page, totalPages) {
  if (totalPages <= 1) return "";
  const label = kind === "daily" ? "Daily usage" : "Models";
  return `
    <nav class="table-pagination" aria-label="${label} pages">
      <span class="table-page-size">${USAGE_TABLE_PAGE_SIZE} / page</span>
      <button class="table-page-arrow" type="button" data-usage-page="${kind}" data-page="${page - 1}" aria-label="Previous ${label.toLowerCase()} page" ${page <= 1 ? "disabled" : ""}>‹</button>
      ${paginationItems(page, totalPages).map((item) => item === "ellipsis"
        ? '<span class="table-page-ellipsis">…</span>'
        : `<button class="table-page-number ${item === page ? "active" : ""}" type="button" data-usage-page="${kind}" data-page="${item}" ${item === page ? 'aria-current="page"' : ""}>${item}</button>`).join("")}
      <button class="table-page-arrow" type="button" data-usage-page="${kind}" data-page="${page + 1}" aria-label="Next ${label.toLowerCase()} page" ${page >= totalPages ? "disabled" : ""}>›</button>
    </nav>
  `;
}

function diagnosticsKey(data) {
  return [data.provider || "all", data.range, data.range_start || "", data.range_end || "", data.ignore_auto_review ? "1" : "0"].join("|");
}

function invalidateRequests() {
  if (requestsController) requestsController.abort();
  requestChildrenControllers.forEach((controller) => controller.abort());
  requestsController = null;
  requestChildrenControllers.clear();
  requestsPayload = null;
  requestsError = null;
  requestSnapshot = null;
  requestPage = 1;
  expandedRequestGroups.clear();
  requestChildrenLoading.clear();
  requestChildrenErrors.clear();
}

function renderUsageTables(data) {
  const totals = data.totals;
  const daily = [...data.daily].reverse();
  const scope = usageTableScopeKey(data);
  if (scope !== usageTableScope) {
    usageTableScope = scope;
    resetUsageTablePages();
  }
  const dailyPage = paginateRows(daily, dailyUsagePage);
  const modelPage = paginateRows(data.models, modelUsagePage);
  dailyUsagePage = dailyPage.page;
  modelUsagePage = modelPage.page;
  const chartDaily = data.chart?.daily || data.daily;
  const heat = heatmapCells(chartDaily, data.chart.range, data.chart.range_start, data.chart.range_end, cacheMode);
  const months = monthLabels(heat);
  const heatColumns = Math.max(1, Math.ceil(heat.length / 7));
  const supportsDiagnostics = Boolean(data.supports_diagnostics);
  if (!supportsDiagnostics && activeTableView === "diagnostics") activeTableView = "usage";
  const diagnosticsCacheKey = diagnosticsKey(data);
  let rightPanelContent = `
    <div class="usage-table-block models-table-block">
      <div class="table-scroll">
      <table class="usage-data-table models-table">
        <thead><tr><th>Model</th><th class="num">Days</th><th class="num">Sessions</th><th class="num">Input</th><th class="num">Output</th><th class="num">Total w/o cached</th><th class="num">Cached</th><th class="num">Total</th><th class="num">Cost</th><th class="num">Share</th></tr></thead>
        <tbody>
          ${modelPage.items.map((row) => {
            const modelKey = row.model_key || row.model;
            const expanded = expandedModels.has(modelKey);
            const displayName = modelDisplayName(data, row);
            const shortName = truncateModelName(displayName);
            const isTruncated = shortName !== displayName;
            const share = (row.total_tokens / Math.max(totals.total_tokens, 1)) * 100;
            return `
              <tr class="model-row ${expanded ? "expanded" : ""}">
                <td>
                  <button class="model-toggle" type="button" data-model="${escapeHtml(modelKey)}" ${isTruncated ? `data-model-tooltip="${escapeHtml(displayName)}"` : ""} aria-expanded="${expanded}" aria-label="${escapeHtml(displayName)}">
                    <span class="model-chevron">${expanded ? "▾" : "▸"}</span>
                    <span class="model-name">${escapeHtml(shortName)}</span>
                  </button>
                </td>
                <td class="num">${full(row.active_days)}</td>
                <td class="num">${full(row.sessions)}</td>
                <td class="num">${full(row.input_tokens)}</td>
                <td class="num">${full(row.output_tokens)}</td>
                <td class="num">${full(row.total_tokens)}</td>
                <td class="num">${full(row.cached_input_tokens)}</td>
                <td class="num">${full(row.total_with_cached_tokens)}</td>
                <td class="num">${money(row.cost_usd)}</td>
                <td class="num share-cell">
                  <div class="share-meter" aria-label="${share.toFixed(1)}% of total usage">
                    <span class="share-track"><span class="share-fill" style="width:${Math.min(share, 100).toFixed(1)}%"></span></span>
                    <span class="share-value">${share.toFixed(1)}%</span>
                  </div>
                </td>
              </tr>
              ${expanded ? `<tr class="model-detail-row"><td colspan="10">${renderModelDetails(row)}</td></tr>` : ""}
            `;
          }).join("") || '<tr><td colspan="10" class="empty">No models in this range.</td></tr>'}
        </tbody>
      </table>
      </div>
      ${renderUsagePagination("models", modelPage.page, modelPage.totalPages)}
    </div>
  `;
  if (activeTableView === "diagnostics") {
    if (diagnosticsCache.has(diagnosticsCacheKey)) rightPanelContent = renderDataHealth(diagnosticsCache.get(diagnosticsCacheKey));
    else if (diagnosticsErrors.has(diagnosticsCacheKey)) rightPanelContent = renderDiagnosticsError(diagnosticsErrors.get(diagnosticsCacheKey));
    else rightPanelContent = renderDiagnosticsLoading();
  }
  if (activeTableView === "requests") rightPanelContent = renderRequestsState();
  return `
    <div class="tables">
      <section class="usage-table-panel daily-usage-panel" data-table-panel="daily">
        <header class="usage-panel-heading daily-panel-heading">
          <h2 class="section-title"><span class="section-icon tone-lime">${icon("usage")}</span><span>Daily Usage</span></h2>
          <nav class="segments viz-tabs" aria-label="Visualization">
            ${visualizationOptions.map((option) => `<button class="seg ${activeVisualization === option.value ? "active" : ""}" type="button" data-visualization="${option.value}" aria-pressed="${activeVisualization === option.value}">${option.label}</button>`).join("")}
          </nav>
        </header>
        ${renderVisualizationPanel(data, heat, months, heatColumns)}
        <div class="usage-table-block">
          <h3>Daily Breakdown</h3>
          <div class="table-scroll">
          <table class="usage-data-table daily-usage-table">
            <thead><tr><th>Date</th><th class="num">Input</th><th class="num">Output</th><th class="num">Total w/o cached</th><th class="num">Cached</th><th class="num">Total</th><th class="num">Cost</th><th class="num">Sessions</th></tr></thead>
            <tbody>
              ${dailyPage.items.map((row) => `<tr><td>${escapeHtml(row.day)}</td><td class="num">${full(row.input_tokens)}</td><td class="num">${full(row.output_tokens)}</td><td class="num">${full(row.total_tokens)}</td><td class="num">${full(row.cached_input_tokens)}</td><td class="num">${full(row.total_with_cached_tokens)}</td><td class="num">${money(row.cost_usd)}</td><td class="num">${full(row.sessions)}</td></tr>`).join("") || '<tr><td colspan="8" class="empty">No usage in this range.</td></tr>'}
            </tbody>
            <tfoot><tr><td>Total</td><td class="num">${full(totals.input_tokens)}</td><td class="num">${full(totals.output_tokens)}</td><td class="num">${full(totals.total_tokens)}</td><td class="num">${full(totals.cached_input_tokens)}</td><td class="num">${full(totals.total_with_cached_tokens)}</td><td class="num">${money(totals.cost_usd)}</td><td class="num">${full(totals.sessions)}</td></tr></tfoot>
          </table>
          </div>
          ${renderUsagePagination("daily", dailyPage.page, dailyPage.totalPages)}
        </div>
      </section>

      <section class="usage-table-panel models-panel" data-table-panel="models">
        <header class="usage-panel-heading workspace-panel-heading">
          <h2 class="section-title"><span class="section-icon tone-violet">${icon("models")}</span><span>Usage Details</span></h2>
          <nav class="segments workspace-panel-tabs" aria-label="Details view">
            <button class="seg ${activeTableView === "usage" ? "active" : ""}" type="button" data-table-view="usage" aria-pressed="${activeTableView === "usage"}">Models</button>
            <button class="seg ${activeTableView === "requests" ? "active" : ""}" type="button" data-table-view="requests" aria-pressed="${activeTableView === "requests"}">Requests</button>
            ${supportsDiagnostics ? `<button class="seg ${activeTableView === "diagnostics" ? "active" : ""}" type="button" data-table-view="diagnostics" aria-pressed="${activeTableView === "diagnostics"}">Data Health</button>` : ""}
          </nav>
        </header>
        <div class="workspace-panel-content" id="right-panel-content">${rightPanelContent}</div>
      </section>
    </div>
  `;
}

function renderDataHealth(diagnostics) {
  const summary = diagnostics.summary;
  const sources = diagnostics.sources || [];
  const enabledSources = sources.filter((source) => source.enabled && source.status !== "not_installed");
  const readySources = enabledSources.filter((source) => source.status === "ready" && !source.stale && !source.error);
  const staleSources = enabledSources.filter((source) => source.stale);
  const errorSources = enabledSources.filter((source) => source.status === "error" || source.error);
  const conflicts = Number(summary.conflicts || 0);
  const unverifiable = Number(summary.unverifiable_events || 0);
  let healthTone = "good";
  let healthLabel = "Healthy";
  let healthMessage = `All ${full(enabledSources.length)} enabled sources are ready.`;
  if (!enabledSources.length) {
    healthTone = "warn";
    healthLabel = "No active sources";
    healthMessage = "Enable at least one source to keep the Unibase index current.";
  } else if (errorSources.length || conflicts) {
    healthTone = "bad";
    healthLabel = "Needs attention";
    healthMessage = `${full(errorSources.length)} source errors · ${full(conflicts)} index conflicts`;
  } else if (staleSources.length || unverifiable) {
    healthTone = "warn";
    healthLabel = "Review recommended";
    healthMessage = `${full(staleSources.length)} stale sources · ${full(unverifiable)} unverifiable updates`;
  }
  const successfulScans = enabledSources
    .map((source) => new Date(source.last_successful_scan || "").getTime())
    .filter(Number.isFinite);
  const latestScan = successfulScans.length ? new Date(Math.max(...successfulScans)).toISOString() : null;
  const providerOrder = ["codex", "claude", "opencode"];
  const providerCards = providerOrder.map((provider) => {
    const breakdown = diagnostics.provider_breakdown?.[provider] || {};
    const providerSources = enabledSources.filter((source) => source.provider === provider);
    const providerReady = providerSources.filter((source) => source.status === "ready" && !source.stale && !source.error).length;
    const providerIssue = providerSources.some((source) => source.status === "error" || source.error)
      ? "bad"
      : providerSources.some((source) => source.stale) || !providerSources.length ? "warn" : "good";
    const label = providerOptions.find((option) => option.value === provider)?.label || provider;
    return `
      <article class="health-provider-card" data-provider="${provider}">
        <div class="health-provider-head">
          <span class="health-provider-mark">${providerLogo(provider, "health-provider-logo")}</span>
          <strong>${escapeHtml(label)}</strong>
          <span class="health-state-dot health-${providerIssue}" aria-label="${providerIssue === "good" ? "Healthy" : providerIssue === "bad" ? "Error" : "Review"}"></span>
        </div>
        <div class="health-provider-value">${full(breakdown.events)}</div>
        <div class="health-provider-caption">indexed events</div>
        <div class="health-provider-foot"><span>${full(providerReady)}/${full(providerSources.length)} sources ready</span></div>
      </article>
    `;
  }).join("");
  const sourceCards = [...sources]
    .sort((left, right) => Number(right.enabled) - Number(left.enabled) || providerOrder.indexOf(left.provider) - providerOrder.indexOf(right.provider))
    .map((source) => {
      let state = "good";
      let stateLabel = "Ready";
      if (source.status === "not_installed") {
        state = "muted";
        stateLabel = "Not installed";
      } else if (!source.enabled) {
        state = "muted";
        stateLabel = "Disabled";
      } else if (source.status === "error" || source.error) {
        state = "bad";
        stateLabel = "Error";
      } else if (source.stale) {
        state = "warn";
        stateLabel = "Stale";
      } else if (source.status !== "ready") {
        state = "warn";
        stateLabel = String(source.status || "Pending");
      }
      const providerLabel = providerOptions.find((option) => option.value === source.provider)?.label || source.provider;
      return `
        <article class="health-source-row ${source.enabled ? "" : "is-disabled"}">
          <div class="health-source-identity">
            <span class="health-source-logo provider-${escapeHtml(source.provider)}">${providerLogo(source.provider, "health-source-provider-logo")}</span>
            <span><strong>${escapeHtml(source.label)}</strong><small>${escapeHtml(providerLabel)} · ${escapeHtml(String(source.kind || "source").replaceAll("_", " "))}</small></span>
          </div>
          <div class="health-source-stat"><strong>${compactNumber(source.event_count)}</strong><small>source records</small></div>
          <div class="health-source-sync"><strong>${escapeHtml(formatTimestamp(source.last_successful_scan, "Never synced"))}</strong><small>last successful scan</small></div>
          <span class="health-status health-${state}"><i></i>${escapeHtml(stateLabel)}</span>
        </article>
      `;
    }).join("");
  return `
    <div class="data-health-panel">
      <div class="data-health-heading">
        <div>
          <h2 class="section-title"><span class="section-icon tone-cyan">${icon("database")}</span><span>Data Health</span></h2>
          <p>Unibase index integrity, provider coverage, and source freshness for the selected usage range.</p>
        </div>
        <span class="health-status health-${healthTone}"><i></i>${healthLabel}</span>
      </div>
      <div class="health-overview health-overview-${healthTone}">
        <span class="health-overview-icon">${icon("database")}</span>
        <div><span>Unibase status</span><strong>${healthLabel}</strong><p>${escapeHtml(healthMessage)}</p></div>
        <div class="health-last-sync"><span>Latest successful scan</span><strong>${escapeHtml(formatTimestamp(latestScan, "Not synced"))}</strong></div>
      </div>
      <div class="health-metrics">
        <article><span>Indexed updates</span><strong>${full(summary.deduplicated_usage_updates)}</strong><small>active events in this range</small></article>
        <article><span>Ready sources</span><strong>${full(readySources.length)}/${full(enabledSources.length)}</strong><small>${full(sources.length)} registered total</small></article>
        <article><span>Index conflicts</span><strong>${full(conflicts)}</strong><small>canonical event conflicts</small></article>
        <article><span>Integrity signals</span><strong>${full(Number(summary.counter_resets || 0) + unverifiable)}</strong><small>${full(summary.counter_resets)} resets · ${full(unverifiable)} unverifiable</small></article>
      </div>
      <div class="health-section-heading"><div><span>Provider coverage</span><strong>Indexed activity by provider</strong></div></div>
      <div class="health-provider-grid">${providerCards}</div>
      <div class="health-section-heading health-sources-heading"><div><span>Source registry</span><strong>Freshness and ingestion status</strong></div><small>${full(enabledSources.length)} enabled · ${full(sources.length)} registered</small></div>
      <div class="health-source-list">
        ${sourceCards || '<div class="health-empty">No registered sources.</div>'}
      </div>
    </div>
  `;
}

function renderDiagnosticsLoading(elapsedSeconds = 0) {
  const elapsed = elapsedSeconds >= 2 ? `<span>${full(elapsedSeconds)}s elapsed</span>` : "";
  return `<div class="diagnostics-state" aria-live="polite"><span class="diagnostics-spinner" aria-hidden="true"></span><strong>Checking Unibase health…</strong>${elapsed}</div>`;
}

function renderDiagnosticsError(message) {
  return `<div class="diagnostics-state error"><strong>Could not load Data Health.</strong><code>${escapeHtml(message)}</code><button class="diagnostics-retry" type="button">Retry</button></div>`;
}

function renderRequestValues(item) {
  return `<span>In ${full(item.input)}</span><span>Out ${full(item.output)}</span><span>Cache R ${full(item.cache_read)}</span><span>Cache W ${full(item.cache_write)}</span><strong>${full(item.total_with_cache)} total</strong>`;
}

function renderRequestEvent(item, timezone) {
  return `
    <article class="request-event">
      <div class="request-event-main">
        <time datetime="${escapeHtml(item.timestamp)}">${escapeHtml(formatRequestDateTime(item.local_timestamp || item.timestamp, timezone))}</time>
        <strong>${escapeHtml(item.model)}</strong>
        <span class="request-provider provider-${escapeHtml(item.provider)}">${escapeHtml(item.provider)}</span>
      </div>
      <div class="request-values">${renderRequestValues(item)}</div>
      <div class="request-labels"><span>${escapeHtml(item.event_label)}</span><span>${escapeHtml(item.cost_label)}${item.cost == null ? "" : ` · ${money(item.cost)}`}</span></div>
    </article>
  `;
}

function renderRequestChildren(item, timezone) {
  const bucket = escapeHtml(item.bucket_start);
  const loading = requestChildrenLoading.has(item.bucket_start);
  const error = requestChildrenErrors.get(item.bucket_start);
  const children = item.children.map((child) => renderRequestEvent(child, timezone)).join("");
  const status = loading
    ? '<div class="request-child-state"><span class="diagnostics-spinner" aria-hidden="true"></span><span>Loading requests…</span></div>'
    : error
      ? `<div class="request-child-state error"><span>${escapeHtml(error)}</span><button type="button" data-request-child-retry="${bucket}">Retry</button></div>`
      : "";
  const pagination = item.child_page > 0 ? `
    <nav class="request-pagination request-child-pagination" aria-label="Requests in ${bucket}">
      <button type="button" data-request-bucket="${bucket}" data-request-child-page="${item.child_page - 1}" ${item.child_has_previous && !loading ? "" : "disabled"}>Previous</button>
      <span>Page ${full(item.child_page)} of ${full(item.child_total_pages)}</span>
      <button type="button" data-request-bucket="${bucket}" data-request-child-page="${item.child_page + 1}" ${item.child_has_next && !loading ? "" : "disabled"}>Next</button>
    </nav>
  ` : "";
  return `<div class="request-children">${status}${children}${pagination}</div>`;
}

function renderRequests(payload) {
  const grouped = payload.group !== "none";
  const rows = payload.items.map((item) => grouped ? `
    <details class="request-group" data-request-group="${escapeHtml(item.bucket_start)}" ${expandedRequestGroups.has(item.bucket_start) ? "open" : ""}>
      <summary>
        <span><time datetime="${escapeHtml(item.bucket_start)}">${escapeHtml(formatRequestWindow(item.bucket_start, item.bucket_end, payload.timezone))}</time><small>${full(item.count)} requests · summed in this window</small></span>
        <span class="request-values">${renderRequestValues(item)}</span>
      </summary>
      ${renderRequestChildren(item, payload.timezone)}
    </details>
  ` : renderRequestEvent(item, payload.timezone)).join("");
  return `
    <div class="requests-panel">
      <div class="requests-heading">
        <div>
          <h2 class="section-title"><span class="section-icon tone-cyan">${icon("usage")}</span><span>Requests</span></h2>
          <p>Metadata-only usage events from Unibase. No prompts, responses, paths, tools, or raw IDs are shown.</p>
        </div>
        <div class="requests-controls">
          <label>Group<select id="request-group">${requestGroupOptions.map((value) => `<option value="${value}" ${payload.group === value ? "selected" : ""}>${value === "none" ? "None" : value}</option>`).join("")}</select></label>
          <label>Page size<select id="request-page-size">${requestPageSizes.map((value) => `<option value="${value}" ${payload.page_size === value ? "selected" : ""}>${value}</option>`).join("")}</select></label>
        </div>
      </div>
      <div class="request-list">${rows || '<div class="request-empty">No requests in this range.</div>'}</div>
      <nav class="request-pagination" aria-label="Requests pages">
        <button type="button" data-request-page="${payload.page - 1}" ${payload.has_previous ? "" : "disabled"}>Previous</button>
        <span>Page ${full(payload.page)} of ${full(payload.total_pages)}</span>
        <button type="button" data-request-page="${payload.page + 1}" ${payload.has_next ? "" : "disabled"}>Next</button>
      </nav>
      <footer class="requests-footer">Times, date boundaries, and grouped totals are calculated in <strong>${escapeHtml(requestTimezoneLabel(payload))}</strong>.</footer>
    </div>
  `;
}

function renderRequestsState() {
  if (requestsError) return `<div class="diagnostics-state error"><strong>Could not load Requests.</strong><code>${escapeHtml(requestsError)}</code><button class="requests-retry" type="button">Retry</button></div>`;
  if (requestsPayload) return renderRequests(requestsPayload);
  return `<div class="diagnostics-state" aria-live="polite"><span class="diagnostics-spinner" aria-hidden="true"></span><strong>Loading Requests from Unibase…</strong></div>`;
}

function renderTableView(data) {
  return `<div id="table-workspace">${renderUsageTables(data)}</div>`;
}

function clearDiagnosticsTimer() {
  if (diagnosticsTimer) window.clearInterval(diagnosticsTimer);
  diagnosticsTimer = null;
}

function cancelDiagnosticsRequest() {
  clearDiagnosticsTimer();
  if (diagnosticsController) diagnosticsController.abort();
  diagnosticsController = null;
  diagnosticsRequestKey = null;
}

function updateTableView(data) {
  const container = document.querySelector("#table-view-container");
  if (!container) return;
  const heatmapPosition = heatmapScrollPosition();
  hideModelNameTooltip();
  container.innerHTML = renderTableView(data);
  restoreHeatmapScroll(heatmapPosition);
  bindTableView(data);
}

function bindTableView(data) {
  document.querySelectorAll("[data-table-view]").forEach((button) => {
    button.addEventListener("click", () => {
      activeTableView = button.dataset.tableView;
      syncUrl();
      updateTableView(data);
      if (activeTableView === "diagnostics") ensureDiagnostics(data);
      if (activeTableView === "requests") ensureRequests(data);
    });
  });

  document.querySelectorAll(".model-toggle").forEach((button) => {
    button.addEventListener("click", () => {
      hideModelNameTooltip();
      const model = button.dataset.model;
      if (!model) return;
      if (expandedModels.has(model)) expandedModels.delete(model);
      else expandedModels.add(model);
      updateTableView(data);
    });
    if (button.dataset.modelTooltip) {
      button.addEventListener("mouseenter", scheduleModelNameTooltip);
      button.addEventListener("mouseleave", hideModelNameTooltip);
      button.addEventListener("focus", scheduleModelNameTooltip);
      button.addEventListener("blur", hideModelNameTooltip);
    }
  });

  document.querySelectorAll("[data-usage-page]").forEach((button) => {
    button.addEventListener("click", () => {
      const page = Number(button.dataset.page);
      if (button.dataset.usagePage === "daily") dailyUsagePage = page;
      else modelUsagePage = page;
      updateTableView(data);
    });
  });

  document.querySelectorAll("[data-visualization]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextVisualization = button.dataset.visualization;
      if (nextVisualization === activeVisualization) return;
      saveActiveChartState();
      activeVisualization = nextVisualization;
      restoreChartState(activeVisualization);
      const canReuseChart = data.chart?.range === activeChartRange
        && (activeChartRange !== "custom"
          || (data.chart.range_start === chartStartDate && data.chart.range_end === chartEndDate));
      syncUrl();
      if (canReuseChart) render(data);
      else refresh();
    });
  });

  document.querySelector("[data-workdays-only]")?.addEventListener("click", () => {
    workdaysOnly = !workdaysOnly;
    syncUrl();
    refresh();
  });

  document.querySelectorAll("[data-chart-range]").forEach((button) => {
    button.addEventListener("click", () => {
      activeChartRange = button.dataset.chartRange;
      if (activeChartRange === "custom") {
        chartStartDate = isIsoDate(chartStartDate) ? chartStartDate : data.chart.range_start || todayKey();
        chartEndDate = isIsoDate(chartEndDate) ? chartEndDate : data.chart.range_end || chartStartDate;
        normalizeChartCustomRange();
        chartCustomRangePending = data.chart?.range !== "custom";
        chartCustomRangeOpen = true;
        render(data);
        return;
      }
      chartCustomRangePending = false;
      chartCustomRangeOpen = false;
      saveActiveChartState();
      syncUrl();
      refresh();
    });
  });

  const chartRangeDialog = document.querySelector("#chart-range-dialog");
  const chartRangeTrigger = document.querySelector("#chart-range-trigger");
  const chartRangeForm = document.querySelector("#chart-range-form");
  if (chartRangeDialog && chartRangeTrigger && chartRangeForm) {
    let listenersAttached = true;
    const repositionDialog = () => positionCustomRangeDialog(chartRangeDialog, chartRangeTrigger);
    const removePositionListeners = () => {
      if (!listenersAttached) return;
      listenersAttached = false;
      window.removeEventListener("resize", repositionDialog);
      window.visualViewport?.removeEventListener("resize", repositionDialog);
      window.visualViewport?.removeEventListener("scroll", repositionDialog);
    };
    const dismissDialog = () => {
      removePositionListeners();
      chartCustomRangeOpen = false;
      if (chartCustomRangePending) {
        restoreChartState(activeVisualization);
        chartCustomRangePending = false;
      }
      document.documentElement.classList.toggle("custom-range-modal-open", customRangeOpen);
      chartRangeDialog.close();
      render(data);
    };

    chartRangeDialog.showModal();
    repositionDialog();
    chartRangeDialog.classList.add("positioned");
    document.querySelector("#chart-custom-start")?.focus();
    window.addEventListener("resize", repositionDialog);
    window.visualViewport?.addEventListener("resize", repositionDialog);
    window.visualViewport?.addEventListener("scroll", repositionDialog);
    chartRangeDialog.addEventListener("close", removePositionListeners, { once: true });
    chartRangeDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      dismissDialog();
    });
    chartRangeDialog.addEventListener("click", (event) => {
      if (event.target !== chartRangeDialog) return;
      const rect = chartRangeDialog.getBoundingClientRect();
      const inside = event.clientX >= rect.left && event.clientX <= rect.right
        && event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) dismissDialog();
    });
    chartRangeDialog.querySelector(".custom-range-close")?.addEventListener("click", dismissDialog);
    chartRangeForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const form = new FormData(chartRangeForm);
      chartStartDate = String(form.get("chart_start") || "");
      chartEndDate = String(form.get("chart_end") || "");
      normalizeChartCustomRange();
      activeChartRange = "custom";
      chartCustomRangePending = false;
      chartCustomRangeOpen = false;
      saveActiveChartState();
      removePositionListeners();
      document.documentElement.classList.toggle("custom-range-modal-open", customRangeOpen);
      chartRangeDialog.close();
      syncUrl();
      refresh();
    });
  }

  document.querySelectorAll("[data-cache-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      cacheMode = resolveCacheMode(button.dataset.cacheMode);
      syncUrl();
      render(data);
    });
  });

  document.querySelectorAll(".heat-cell").forEach((cell) => {
    cell.addEventListener("mouseenter", showHeatTooltip);
    cell.addEventListener("mousemove", positionHeatTooltip);
    cell.addEventListener("mouseleave", hideHeatTooltip);
  });

  document.querySelectorAll(".bar-slot, .bar-segment, .activity-time-segment[data-tooltip-title]").forEach((item) => {
    item.addEventListener("mouseenter", showChartTooltip);
    item.addEventListener("mousemove", positionHeatTooltip);
    item.addEventListener("mouseleave", handleChartTooltipLeave);
    item.addEventListener("focus", showChartTooltip);
    item.addEventListener("blur", hideHeatTooltip);
  });

  const chartModelItems = document.querySelectorAll("[data-chart-model]");
  let hoveredChartModel = null;
  let focusedChartModel = null;
  const highlightChartModel = () => {
    const model = hoveredChartModel ?? focusedChartModel;
    chartModelItems.forEach((item) => {
      item.classList.toggle("is-model-highlighted", model !== null && item.dataset.chartModel === model);
      item.classList.toggle("is-model-muted", model !== null && item.dataset.chartModel !== model);
    });
  };
  chartModelItems.forEach((item) => {
    item.addEventListener("mouseenter", () => { hoveredChartModel = item.dataset.chartModel; highlightChartModel(); });
    item.addEventListener("mouseleave", () => { hoveredChartModel = null; highlightChartModel(); });
    item.addEventListener("focus", () => { focusedChartModel = item.dataset.chartModel; highlightChartModel(); });
    item.addEventListener("blur", () => { focusedChartModel = null; highlightChartModel(); });
  });

  const retry = document.querySelector(".diagnostics-retry");
  if (retry) {
    retry.addEventListener("click", () => {
      diagnosticsErrors.delete(diagnosticsKey(data));
      updateTableView(data);
      ensureDiagnostics(data, true);
    });
  }

  document.querySelector(".requests-retry")?.addEventListener("click", () => {
    requestsError = null;
    requestSnapshot = null;
    requestPage = 1;
    syncUrl();
    ensureRequests(data, true);
  });
  document.querySelector("#request-group")?.addEventListener("change", (event) => {
    requestChildrenControllers.forEach((controller) => controller.abort());
    requestChildrenControllers.clear();
    requestGroup = event.target.value;
    requestPage = 1;
    requestSnapshot = null;
    requestsPayload = null;
    expandedRequestGroups.clear();
    requestChildrenLoading.clear();
    requestChildrenErrors.clear();
    syncUrl();
    ensureRequests(data, true);
  });
  document.querySelector("#request-page-size")?.addEventListener("change", (event) => {
    requestChildrenControllers.forEach((controller) => controller.abort());
    requestChildrenControllers.clear();
    requestPageSize = Number(event.target.value);
    requestPage = 1;
    requestSnapshot = null;
    requestsPayload = null;
    expandedRequestGroups.clear();
    requestChildrenLoading.clear();
    requestChildrenErrors.clear();
    syncUrl();
    ensureRequests(data, true);
  });
  document.querySelectorAll("[data-request-page]").forEach((button) => {
    button.addEventListener("click", () => {
      requestChildrenControllers.forEach((controller) => controller.abort());
      requestChildrenControllers.clear();
      requestPage = Number(button.dataset.requestPage);
      requestsPayload = null;
      expandedRequestGroups.clear();
      requestChildrenLoading.clear();
      requestChildrenErrors.clear();
      syncUrl();
      ensureRequests(data, true);
    });
  });
  document.querySelectorAll("[data-request-group]").forEach((details) => {
    details.addEventListener("toggle", () => {
      const bucket = details.dataset.requestGroup;
      if (!details.open) {
        expandedRequestGroups.delete(bucket);
        return;
      }
      expandedRequestGroups.add(bucket);
      const item = requestsPayload?.items.find((candidate) => candidate.bucket_start === bucket);
      if (item?.child_page === 0 && !requestChildrenLoading.has(bucket)) loadRequestChildren(data, bucket, 1);
    });
  });
  document.querySelectorAll("[data-request-child-page]").forEach((button) => {
    button.addEventListener("click", () => {
      loadRequestChildren(data, button.dataset.requestBucket, Number(button.dataset.requestChildPage));
    });
  });
  document.querySelectorAll("[data-request-child-retry]").forEach((button) => {
    button.addEventListener("click", () => loadRequestChildren(data, button.dataset.requestChildRetry, 1));
  });
}

function requestsParams(bucketStart = null, childPage = 1) {
  const params = new URLSearchParams();
  params.set("provider", activeProvider);
  params.set("range", activeRange);
  params.set("timezone", Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  params.set("group", requestGroup);
  params.set("page", String(requestPage));
  params.set("page_size", String(requestPageSize));
  if (activeRange === "custom") {
    params.set("start", customStartDate);
    params.set("end", customEndDate);
  }
  if (requestSnapshot) params.set("snapshot", requestSnapshot);
  if (bucketStart) {
    params.set("bucket_start", bucketStart);
    params.set("child_page", String(childPage));
  }
  return params;
}

async function ensureRequests(data, force = false, allowSnapshotRetry = true) {
  if (!force && requestsPayload) {
    updateTableView(data);
    return;
  }
  if (requestsController) requestsController.abort();
  requestsController = new AbortController();
  const controller = requestsController;
  requestsError = null;
  if (activeTableView === "requests") updateTableView(data);
  const params = requestsParams();
  try {
    const response = await fetch(`/api/requests?${params}`, { cache: "no-store", signal: controller.signal });
    if (!response.ok) {
      if (response.status === 409 && requestSnapshot && allowSnapshotRetry) {
        requestSnapshot = null;
        requestPage = 1;
        syncUrl();
        return ensureRequests(data, true, false);
      }
      throw new Error(`Requests API returned HTTP ${response.status}`);
    }
    requestsPayload = await response.json();
    requestSnapshot = requestsPayload.snapshot;
    requestPage = requestsPayload.page;
    if (activeTableView === "requests") updateTableView(data);
  } catch (error) {
    if (error.name === "AbortError") return;
    requestsError = error.message;
    if (activeTableView === "requests") updateTableView(data);
  } finally {
    if (requestsController === controller) requestsController = null;
  }
}

async function loadRequestChildren(data, bucket, childPage) {
  if (!requestsPayload || requestGroup === "none" || childPage < 1) return;
  requestChildrenControllers.get(bucket)?.abort();
  const controller = new AbortController();
  requestChildrenControllers.set(bucket, controller);
  requestChildrenErrors.delete(bucket);
  requestChildrenLoading.add(bucket);
  if (activeTableView === "requests") updateTableView(data);
  try {
    const response = await fetch(`/api/requests?${requestsParams(bucket, childPage)}`, {
      cache: "no-store",
      signal: controller.signal
    });
    if (!response.ok) {
      if (response.status === 409 && requestSnapshot) {
        invalidateRequests();
        syncUrl();
        return ensureRequests(data, true, false);
      }
      throw new Error(`Requests API returned HTTP ${response.status}`);
    }
    const payload = await response.json();
    const nextItem = payload.items[0];
    if (!nextItem || !requestsPayload || payload.snapshot !== requestSnapshot) return;
    requestsPayload = {
      ...requestsPayload,
      items: requestsPayload.items.map((item) => item.bucket_start === bucket ? nextItem : item)
    };
  } catch (error) {
    if (error.name === "AbortError") return;
    requestChildrenErrors.set(bucket, error.message);
  } finally {
    if (requestChildrenControllers.get(bucket) === controller) {
      requestChildrenLoading.delete(bucket);
      requestChildrenControllers.delete(bucket);
      if (activeTableView === "requests") updateTableView(data);
    }
  }
}

function settingsIsDirty() {
  if (!settingsData || !settingsDraft) return false;
  const original = {
    merge_models_across_providers: settingsData.merge_models_across_providers,
    non_working_weekdays: settingsData.non_working_weekdays,
    sources: Object.values(settingsData.sources).flat().map(({ source_id, enabled }) => ({ source_id, enabled })),
    models: Object.values(settingsData.models).flat().map(({ model, enabled }) => ({ model, enabled }))
  };
  return JSON.stringify(original) !== JSON.stringify(settingsDraft);
}

function settingsDraftFromData(payload) {
  return {
    merge_models_across_providers: payload.merge_models_across_providers,
    non_working_weekdays: [...payload.non_working_weekdays],
    sources: Object.values(payload.sources).flat().map(({ source_id, enabled }) => ({ source_id, enabled })),
    models: Object.values(payload.models).flat().map(({ model, enabled }) => ({ model, enabled }))
  };
}

function renderSourceGroup(provider, sources) {
  const label = providerOptions.find((option) => option.value === provider)?.label || provider;
  const settingsLocked = settingsApplyPending || ["queued", "running"].includes(settingsData?.unibase?.current_operation?.state);
  return `
    <div class="settings-source-group">
      <h3>${escapeHtml(label)}</h3>
      <div class="settings-source-list">${sources.length ? sources.map((source) => {
        const draft = settingsDraft?.sources.find((item) => item.source_id === source.source_id);
        return `
          <label class="settings-source ${source.original ? "settings-source-original" : ""}">
            <input type="checkbox" data-settings-source="${escapeHtml(source.source_id)}" ${draft?.enabled ? "checked" : ""} ${source.original || settingsLocked || ["ambiguous", "incomplete"].includes(source.status) ? "disabled" : ""}>
            <span>
              <strong>${escapeHtml(source.label)}${source.original ? '<span class="source-origin-badge">Original</span>' : ""}</strong>
              <small title="${escapeHtml(source.path)}">${escapeHtml(source.path)} · ${formatMegabytes(source.size_bytes)} · ${full(source.event_count)} events</small>
            </span>
            <em class="source-status status-${escapeHtml(source.status)}">${source.status === "not_installed" ? "Not installed" : escapeHtml(source.status)}${source.stale ? " · stale" : ""}</em>
          </label>
        `;
      }).join("") : '<p class="settings-empty">No sources discovered.</p>'}</div>
    </div>
  `;
}

function renderModelGroup(group, models) {
  const settingsLocked = settingsApplyPending || ["queued", "running"].includes(settingsData?.unibase?.current_operation?.state);
  const metadata = {
    gpt: { label: "GPT", description: "OpenAI and Codex", provider: "codex" },
    claude: { label: "Claude", description: "Anthropic models", provider: "claude" },
    others: { label: "Others", description: "OpenCode and custom", provider: "opencode" }
  }[group];
  const enabledCount = models.filter((item) => settingsDraft?.models.find((model) => model.model === item.model)?.enabled).length;
  return `
    <section class="settings-model-group settings-model-group-${escapeHtml(group)}">
      <div class="settings-model-heading">
        <div class="settings-model-identity">
          <span class="settings-model-logo">${providerLogo(metadata.provider)}</span>
          <div><h3>${escapeHtml(metadata.label)}</h3><p>${escapeHtml(metadata.description)}</p></div>
        </div>
        <span class="settings-model-count">${full(enabledCount)}/${full(models.length)} on</span>
      </div>
      <div class="settings-model-list">
        ${models.length ? models.map((item) => {
          const draft = settingsDraft?.models.find((model) => model.model === item.model);
          return `<label class="settings-model"><input type="checkbox" data-settings-model="${escapeHtml(item.model)}" ${draft?.enabled ? "checked" : ""} ${settingsLocked ? "disabled" : ""}><span title="${escapeHtml(item.model)}">${escapeHtml(item.model)}</span></label>`;
        }).join("") : '<p class="settings-empty">No models found.</p>'}
      </div>
    </section>
  `;
}

function renderSettingsDialog() {
  if (!settingsOpen) return "";
  if (settingsLoading) {
    return `<dialog class="settings-dialog" id="settings-dialog" aria-labelledby="settings-title"><div class="settings-loading"><span class="diagnostics-spinner"></span><strong>Loading Settings…</strong></div></dialog>`;
  }
  if (!settingsData || !settingsDraft) {
    return `<dialog class="settings-dialog" id="settings-dialog" aria-labelledby="settings-title"><div class="settings-loading"><strong id="settings-title">Could not load Settings.</strong><code>${escapeHtml(settingsError || "Unknown error")}</code><div class="settings-actions"><button class="settings-close" type="button">Close</button><button class="settings-retry" type="button">Retry</button></div></div></dialog>`;
  }
  const operation = settingsData.unibase.current_operation;
  const operationRunning = operation && ["queued", "running"].includes(operation.state);
  const settingsLocked = operationRunning || settingsApplyPending;
  const dirty = settingsIsDirty();
  return `
    <dialog class="settings-dialog" id="settings-dialog" aria-labelledby="settings-title">
      <form class="settings-shell" id="settings-form">
        <div class="settings-header">
          <div><span class="eyebrow">MeterMesh control plane</span><h2 id="settings-title">Settings</h2></div>
          <button class="settings-close" id="settings-close" type="button" aria-label="Close Settings" ${settingsApplyPending ? "disabled" : ""}>×</button>
        </div>
        ${settingsError ? `<div class="settings-error" role="alert">${escapeHtml(settingsError)}</div>` : ""}
        <div class="settings-tabs" role="tablist" aria-label="Settings sections">
          <button type="button" role="tab" data-settings-tab="general" aria-selected="${settingsActiveTab === "general"}">General</button>
          <button type="button" role="tab" data-settings-tab="models" aria-selected="${settingsActiveTab === "models"}">Models</button>
        </div>
        <div class="settings-tab-panel" role="tabpanel">
          ${settingsActiveTab === "general" ? `
            <section class="settings-section">
              <h3>Preferences</h3>
              <label class="settings-preference"><input id="settings-merge-models" type="checkbox" ${settingsDraft.merge_models_across_providers ? "checked" : ""} ${settingsLocked ? "disabled" : ""}><span><strong>Merge matching models in "All" mode</strong><small>Combines the same model name across Codex, Claude, and OpenCode in charts and model usage tables.</small></span></label>
              <div class="settings-preference weekday-preference"><span><strong>Non-working days</strong><small>Used by the optional Workdays only filter in Active time.</small></span><div class="weekday-chips">${weekdayOptions.map((option, day) => `<label><input type="checkbox" data-non-working-weekday="${day}" aria-label="${option.full} is a non-working day" ${settingsDraft.non_working_weekdays.includes(day) ? "checked" : ""} ${settingsLocked ? "disabled" : ""}><span>${option.short}</span></label>`).join("")}</div></div>
            </section>
            <section class="settings-section">
              <div class="settings-section-heading"><div><h3>Sources</h3><p>Original live sources are always enabled. Optional sources remain registered when unchecked.</p></div></div>
              <div class="settings-source-groups">${["codex", "claude", "opencode"].map((provider) => renderSourceGroup(provider, settingsData.sources[provider] || [])).join("")}</div>
            </section>
            <section class="settings-section unibase-settings">
              <div>
                <h3>Unibase</h3>
                <p><code>${escapeHtml(settingsData.unibase.path)}</code> · generation ${full(settingsData.unibase.generation)} · ${escapeHtml(settingsData.unibase.state)}</p>
                <p>${full(settingsData.unibase.counts.active_events)} active events · ${full(settingsData.unibase.counts.retained_variants)} retained variants</p>
              </div>
              <div class="unibase-actions">
                <span><button id="resync-unibase" type="button" ${dirty || settingsLocked || settingsData.unibase.state === "reset_empty" ? "disabled" : ""}>Resync</button><small>Reload every enabled source and deduplicate without deleting retained Unibase records.</small></span>
                <span><button id="reset-unibase" class="danger" type="button" ${dirty || settingsLocked ? "disabled" : ""}>Reset</button><small>Replace all derived Unibase data with a clean rebuild from enabled sources.</small></span>
              </div>
              ${operation ? `<div class="operation-progress" role="status" aria-live="polite"><div><strong>${escapeHtml(operation.kind)}</strong><span>${escapeHtml(operation.state)}</span></div><progress aria-label="${escapeHtml(operation.kind)} progress" max="${Math.max(operation.progress_total, 1)}" value="${operation.progress_current}"></progress>${operation.error ? `<code>${escapeHtml(operation.error)}</code>` : ""}</div>` : ""}
            </section>
          ` : `
            <section class="settings-section settings-models-section">
              <div class="settings-section-heading"><div><span class="eyebrow">Global visibility</span><h3>Models</h3><p>Disabled models are hidden from every provider, chart, statistic, and Token Usage list. Disable codex-auto-review here when it should be excluded.</p></div></div>
              <div class="settings-model-groups">${["gpt", "claude", "others"].map((group) => renderModelGroup(group, settingsData.models[group] || [])).join("")}</div>
            </section>
          `}
        </div>
        <div class="settings-actions">
          <button class="settings-cancel" id="settings-cancel" type="button" ${settingsApplyPending ? "disabled" : ""}>Cancel</button>
          <button class="settings-apply" id="settings-apply" type="submit" ${dirty && !settingsLocked ? "" : "disabled"}>${settingsApplyPending ? '<span class="settings-apply-spinner" aria-hidden="true"></span><span>Applying…</span>' : settingsApplied && !dirty ? "Applied" : "Apply"}</button>
        </div>
      </form>
    </dialog>
    ${resetConfirmOpen ? `
      <dialog class="confirm-dialog" id="reset-confirm-dialog" aria-labelledby="reset-confirm-title">
        <form id="reset-confirm-form">
          <span class="eyebrow danger-text">Destructive action</span>
          <h2 id="reset-confirm-title">Reset Unibase?</h2>
          <p>Provider source files are untouched. Unibase is fully cleared and automatically rebuilt from every enabled source.</p>
          <label>Type <strong>RESET UNIBASE</strong><input id="reset-confirm-input" autocomplete="off" value="${escapeHtml(resetConfirmation)}"></label>
          <div class="settings-actions"><button class="reset-cancel" type="button">Cancel</button><button class="danger" id="reset-confirm-submit" type="submit" ${resetConfirmation === "RESET UNIBASE" ? "" : "disabled"}>Reset</button></div>
        </form>
      </dialog>
    ` : ""}
  `;
}

async function openSettings() {
  const startedAt = Date.now();
  if (!settingsOpen) settingsActiveTab = "general";
  settingsOpen = true;
  settingsLoading = true;
  settingsError = null;
  settingsApplyPending = false;
  settingsApplied = false;
  render(currentData);
  console.info("[MeterMesh timing] settings fetch started");
  try {
    const response = await fetch("/api/settings", { cache: "no-store" });
    if (!response.ok) throw new Error(`Settings API returned HTTP ${response.status}`);
    settingsData = await response.json();
    settingsDraft = settingsDraftFromData(settingsData);
    console.info("[MeterMesh timing] settings fetch completed", { elapsedMs: Date.now() - startedAt });
  } catch (error) {
    console.error("[MeterMesh timing] settings fetch failed", { elapsedMs: Date.now() - startedAt, error });
    settingsError = error.message;
  } finally {
    settingsLoading = false;
    render(currentData);
    const operation = settingsData?.unibase?.current_operation;
    if (operation && ["queued", "running"].includes(operation.state) && !operationPollTimer) {
      pollOperation(operation.operation_id).catch((error) => {
        settingsError = error.message;
        render(currentData);
      });
    }
  }
}

function closeSettings() {
  if (settingsApplyPending) return;
  settingsOpen = false;
  resetConfirmOpen = false;
  resetConfirmation = "";
  settingsError = null;
  settingsApplied = false;
  if (operationPollTimer) window.clearTimeout(operationPollTimer);
  operationPollTimer = null;
  render(currentData);
}

function invalidateDashboardCaches() {
  usageCache.clear();
  diagnosticsCache.clear();
  diagnosticsErrors.clear();
  invalidateRequests();
}

async function refreshSourceChanges() {
  if (manualRefreshPending) return;
  manualRefreshPending = true;
  render(currentData);
  try {
    const response = await fetch("/api/sources/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
    if (response.status === 409) {
      const conflict = await response.json();
      if (currentData && conflict.source_sync) render({ ...currentData, sync: conflict.source_sync });
      return;
    }
    if (!response.ok) throw new Error(`Source refresh returned HTTP ${response.status}`);
    while (true) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      const statusResponse = await fetch(`/api/unibase/status?provider=${encodeURIComponent(activeProvider)}`, { cache: "no-store" });
      if (!statusResponse.ok) throw new Error(`Sync status returned HTTP ${statusResponse.status}`);
      const status = await statusResponse.json();
      if (status.source_sync?.state === "running") continue;
      if (status.source_sync?.error) throw new Error(status.source_sync.error);
      break;
    }
    invalidateDashboardCaches();
    await refresh();
  } finally {
    manualRefreshPending = false;
    if (currentData) render(currentData);
  }
}

function scheduleSourceSyncPoll(data) {
  if (sourceSyncPollTimer) window.clearTimeout(sourceSyncPollTimer);
  const delay = data.sync?.state === "running" ? 1000 : 30000;
  sourceSyncPollTimer = window.setTimeout(async () => {
    try {
      const response = await fetch(`/api/unibase/status?provider=${encodeURIComponent(activeProvider)}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Sync status returned HTTP ${response.status}`);
      const payload = await response.json();
      if (!currentData) return;
      const generationChanged = Number(payload.generation) !== Number(currentData.generation);
      if (generationChanged && payload.source_sync?.state !== "running") {
        invalidateDashboardCaches();
        await refresh();
        return;
      }
      const nextSync = payload.source_sync || currentData.sync;
      if (nextSync?.state !== currentData.sync?.state || payload.fresh_at !== currentData.fresh_at) {
        const freshAt = Object.hasOwn(payload, "fresh_at") ? payload.fresh_at : currentData.fresh_at;
        render({ ...currentData, sync: nextSync, fresh_at: freshAt });
      } else {
        scheduleSourceSyncPoll(currentData);
      }
    } catch (error) {
      console.warn("[MeterMesh timing] sync status poll failed", error);
      if (currentData) scheduleSourceSyncPoll(currentData);
    }
  }, delay);
}

function renderSettingsUpdate() {
  const dialog = document.querySelector("#settings-dialog");
  const scrollTop = dialog?.scrollTop || 0;
  const focusedId = dialog?.contains(document.activeElement) ? document.activeElement.id : "";
  render(currentData);
  const nextDialog = document.querySelector("#settings-dialog");
  if (nextDialog) nextDialog.scrollTop = scrollTop;
  if (focusedId) document.getElementById(focusedId)?.focus({ preventScroll: true });
}

async function pollOperation(operationId) {
  operationPollTimer = null;
  let payload;
  try {
    const params = new URLSearchParams({ operation_id: operationId, provider: activeProvider });
    const response = await fetch(`/api/unibase/status?${params}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`Operation status returned HTTP ${response.status}`);
    payload = await response.json();
  } catch (error) {
    if (!settingsOpen) return;
    settingsError = `${error.message} Retrying…`;
    renderSettingsUpdate();
    if (settingsOpen) {
      operationPollTimer = window.setTimeout(() => pollOperation(operationId), 1500);
    }
    return;
  }
  if (!settingsOpen) return;
  settingsError = null;
  settingsData.unibase.current_operation = payload.operation;
  settingsData.unibase.generation = payload.generation;
  settingsData.unibase.state = payload.state;
  renderSettingsUpdate();
  if (payload.operation && ["queued", "running"].includes(payload.operation.state)) {
    if (settingsOpen) operationPollTimer = window.setTimeout(() => pollOperation(operationId), 600);
  } else if (payload.operation?.state === "succeeded") {
    invalidateDashboardCaches();
    await refresh();
    if (settingsOpen) await openSettings();
  }
}

async function ensureDiagnostics(data, force = false) {
  if (!data.supports_diagnostics) return;
  const key = diagnosticsKey(data);
  if (!force && diagnosticsCache.has(key)) {
    if (activeTableView === "diagnostics") updateTableView(data);
    return;
  }
  if (!force && diagnosticsController && diagnosticsRequestKey === key) return;

  cancelDiagnosticsRequest();
  diagnosticsErrors.delete(key);
  diagnosticsController = new AbortController();
  diagnosticsRequestKey = key;
  const controller = diagnosticsController;
  const startedAt = Date.now();
  if (activeTableView === "diagnostics") updateTableView(data);
  diagnosticsTimer = window.setInterval(() => {
    if (activeTableView !== "diagnostics" || diagnosticsKey(currentData || data) !== key) return;
      const panel = document.querySelector("#right-panel-content");
      if (panel) panel.innerHTML = renderDiagnosticsLoading(Math.floor((Date.now() - startedAt) / 1000));
  }, 1000);

  try {
    const response = await fetch(`/data.json?${buildQuery(activeRange, true)}`, {
      cache: "no-store",
      signal: controller.signal
    });
    if (!response.ok) throw new Error(`Diagnostics API returned HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload.diagnostics) throw new Error("Diagnostics payload is missing");
    diagnosticsCache.set(key, payload.diagnostics);
    if (activeTableView === "diagnostics" && diagnosticsKey(currentData || data) === key) updateTableView(data);
  } catch (error) {
    if (error.name === "AbortError") return;
    diagnosticsErrors.set(key, error.message);
    if (activeTableView === "diagnostics" && diagnosticsKey(currentData || data) === key) updateTableView(data);
  } finally {
    if (diagnosticsController === controller) {
      clearDiagnosticsTimer();
      diagnosticsController = null;
      diagnosticsRequestKey = null;
    }
  }
}

function dailySeries(data, key) {
  return (data.daily || []).map((row) => Number(row[key] || 0));
}

function tokenSeries(data, key, fallbackValue) {
  const series = dailySeries(data, key);
  return series.length ? series : [Number(fallbackValue || 0)];
}

function renderSparkline(values, label) {
  const finiteValues = values.filter((value) => Number.isFinite(value));
  if (!finiteValues.length) return "";
  const series = finiteValues.length === 1 ? [finiteValues[0], finiteValues[0]] : finiteValues;
  const width = 320;
  const height = 58;
  const inset = 4;
  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = max - min;
  const step = (width - inset * 2) / Math.max(series.length - 1, 1);
  const points = series.map((value, index) => {
    const x = inset + index * step;
    const y = span === 0
      ? height / 2
      : height - inset - ((value - min) / span) * (height - inset * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const area = `${inset},${height - inset} ${points} ${width - inset},${height - inset}`;
  return `
    <div class="metric-sparkline" role="img" aria-label="${escapeHtml(label)}">
      <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
        <polygon points="${area}"></polygon>
        <polyline points="${points}"></polyline>
      </svg>
    </div>
  `;
}

function render(data) {
  const heatmapPosition = heatmapScrollPosition();
  hideModelNameTooltip();
  currentData = data;
  const provider = data.provider || "all";
  const providerLabel = data.provider_label || providerOptions.find((option) => option.value === provider)?.label || "All";
  const totals = data.totals;
  const cacheShare = totals.total_with_cached_tokens > 0
    ? (totals.cached_input_tokens / totals.total_with_cached_tokens) * 100
    : 0;
  const indexingNote = data.indexing
    ? ` · ${full(data.indexing.events)} events from ${full(data.indexing.files)} JSONL files`
    : "";
  const syncRunning = data.sync?.state === "running" || manualRefreshPending;
  const syncNote = syncRunning ? " · syncing sources" : "";
  const freshnessLabel = data.fresh_at
    ? `Unibase current as of ${formatTimestamp(data.fresh_at)}`
    : "Unibase not fully synced";
  const rangeSummary = customRangePending ? "Choose custom range" : describeRange(data);
  document.documentElement.dataset.provider = provider;
  document.documentElement.classList.toggle("custom-range-modal-open", customRangeOpen || chartCustomRangeOpen);
  document.documentElement.classList.toggle("settings-modal-open", settingsOpen);
  document.title = `MeterMesh · ${providerLabel}`;

  app.innerHTML = `
    <header class="app-header">
      <div class="brand-block">
        <div class="brand-identity">
          <div class="brand-pill">
            <span class="brand-mark">${icon("brand", "brand-mesh-logo")}</span>
            <h1>MeterMesh</h1>
            <span class="brand-scope">${escapeHtml(providerLabel)}</span>
          </div>
          <div class="brand-meta">
            <span class="freshness-line">${escapeHtml(freshnessLabel)}${indexingNote}${syncNote}<button class="source-refresh-trigger ${syncRunning ? "is-spinning" : ""}" id="source-refresh-trigger" type="button" aria-label="Check for source changes" title="Check for source changes" ${syncRunning ? "disabled" : ""}>${icon("refresh")}</button></span>
            <strong>Showing ${escapeHtml(rangeSummary)}</strong>
          </div>
        </div>
      </div>
      <div class="header-tools">
        <div class="header-filter-row">
          <nav class="segments provider-switch" aria-label="Usage provider">
            ${providerOptions.map((option) => `<button class="seg provider-option ${provider === option.value ? "active" : ""}" type="button" data-provider="${option.value}" aria-pressed="${provider === option.value}">${providerLogo(option.value, "provider-option-logo")}<span>${option.label}</span></button>`).join("")}
          </nav>
          <nav class="segments range-switch" aria-label="Range">
            ${rangeOptions.map((range) => `<button class="seg ${activeRange === range.value ? "active" : ""}" type="button" data-range="${range.value}" ${range.value === "custom" ? `id="custom-range-trigger" aria-haspopup="dialog" aria-expanded="${customRangeOpen}"` : ""}>${range.label}</button>`).join("")}
          </nav>
          <button class="settings-trigger" id="settings-trigger" type="button" aria-haspopup="dialog" aria-expanded="${settingsOpen}">${icon("settings")}<span>Settings</span></button>
        </div>
      </div>
    </header>

    ${customRangeOpen ? `
      <dialog class="custom-range-dialog" id="custom-range-dialog" aria-labelledby="custom-range-title">
        <form class="custom-range" id="custom-range-form">
          <div class="custom-range-heading">
            <strong id="custom-range-title">Custom range</strong>
            <button class="custom-range-close" type="button" aria-label="Close custom range">×</button>
          </div>
          <label>
            <span>From</span>
            <input id="custom-start" type="date" value="${escapeHtml(customStartDate)}">
          </label>
          <label>
            <span>To</span>
            <input id="custom-end" type="date" value="${escapeHtml(customEndDate)}">
          </label>
          <button class="custom-apply" type="submit">Apply</button>
        </form>
      </dialog>
    ` : ""}

    ${renderSettingsDialog()}

    <div class="metric-hero-grid">
      ${metricCard({
        label: "Total tokens",
        value: compactNumber(totals.total_with_cached_tokens),
        iconName: "layers",
        tone: "violet",
        note: `Cached ${compactNumber(totals.cached_input_tokens)} · Without cache ${compactNumber(totals.total_tokens)}`,
        series: tokenSeries(data, "total_with_cached_tokens", totals.total_with_cached_tokens),
        hero: true
      })}
      ${metricCard({
        label: "API estimate",
        value: money(totals.cost_usd),
        iconName: "coin",
        tone: "amber",
        note: escapeHtml(data.pricing?.source || "pricing unavailable"),
        hero: true
      })}
    </div>

    <section class="metric-group metric-token-group" aria-labelledby="token-usage-title">
      <div class="metric-group-heading">
        <div class="metric-group-title">
          <span class="metric-group-icon tone-violet">${icon("layers")}</span>
          <h2 id="token-usage-title">Token usage</h2>
        </div>
        <div class="metric-group-summary">
          <span>${percentageFormatter.format(cacheShare)}% of total tokens were served from cache</span>
          <span class="metric-group-info" title="Cached input as a share of total tokens" aria-label="Cached input as a share of total tokens">${icon("info")}</span>
        </div>
      </div>
      <div class="group-metric-grid token-usage-grid">
        ${groupMetricItem({ label: "Input tokens", value: compactNumber(totals.input_tokens), iconName: "input", tone: "blue", series: tokenSeries(data, "input_tokens", totals.input_tokens) })}
        ${groupMetricItem({ label: "Output tokens", value: compactNumber(totals.output_tokens), iconName: "output", tone: "green", series: tokenSeries(data, "output_tokens", totals.output_tokens) })}
        ${groupMetricItem({ label: "Cached input", value: compactNumber(totals.cached_input_tokens), iconName: "cache", tone: "cyan", series: tokenSeries(data, "cached_input_tokens", totals.cached_input_tokens) })}
        ${groupMetricItem({ label: "Non-cached", value: compactNumber(totals.total_tokens), iconName: "calculator", tone: "coral", series: tokenSeries(data, "total_tokens", totals.total_tokens) })}
      </div>
    </section>

    <div class="metric-secondary-row">
      <section class="metric-group metric-activity-group" aria-labelledby="activity-title">
        <div class="metric-group-heading">
          <div class="metric-group-title">
            <span class="metric-group-icon tone-violet">${icon("usage")}</span>
            <h2 id="activity-title">Activity</h2>
          </div>
        </div>
        <div class="group-metric-grid activity-grid">
          ${groupMetricItem({ label: "Sessions", value: full(totals.sessions), iconName: "sessions", tone: "blue" })}
          ${groupMetricItem({ label: "Active days", value: full(totals.active_days), iconName: "calendar", tone: "blue" })}
          ${groupMetricItem({ label: "Current streak", value: `${full(data.current_streak)}d`, iconName: "flame", tone: "coral" })}
          ${groupMetricItem({ label: "Longest streak", value: `${full(data.longest_streak)}d`, iconName: "trophy", tone: "amber" })}
          ${groupMetricItem({ label: "Peak day", value: escapeHtml(data.peak_day), iconName: "chart", tone: "green", note: data.peak_day_tokens ? `${compactNumber(data.peak_day_tokens)} tokens` : "" })}
        </div>
      </section>

      <section class="metric-group metric-profile-group" aria-labelledby="usage-profile-title">
        <div class="metric-group-heading">
          <div class="metric-group-title">
            <span class="metric-group-icon tone-violet">${icon("star")}</span>
            <h2 id="usage-profile-title">Usage profile</h2>
          </div>
        </div>
        <div class="group-metric-grid usage-profile-grid">
          ${groupMetricItem({ label: "Favorite model", value: escapeHtml(truncateModelName(data.favorite_model)), iconName: "star", tone: "violet", compact: true })}
          ${groupMetricItem({ label: "Data source", value: escapeHtml(data.data_source || "SQLite + JSONL"), iconName: "database", tone: "violet", compact: true })}
        </div>
      </section>
    </div>

    <div id="table-view-container">${renderTableView(data)}</div>
  `;

  restoreHeatmapScroll(heatmapPosition);
  bindTableView(data);
  if (activeTableView === "diagnostics") ensureDiagnostics(data);
  if (activeTableView === "requests") ensureRequests(data);

  document.querySelectorAll("button[data-provider]").forEach((button) => {
    button.addEventListener("click", () => {
      customRangePending = false;
      customRangeOpen = false;
      activeRange = data.range;
      activeProvider = button.dataset.provider;
      activeTableView = "usage";
      invalidateRequests();
      expandedModels.clear();
      syncUrl();
      refresh();
    });
  });

  document.querySelector("#settings-trigger")?.addEventListener("click", openSettings);
  document.querySelector("#source-refresh-trigger")?.addEventListener("click", () => {
    refreshSourceChanges().catch((error) => {
      console.error("[MeterMesh timing] manual source refresh failed", error);
      window.alert(`Could not refresh sources: ${error.message}`);
    });
  });

  document.querySelectorAll("[data-range]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextRange = button.dataset.range;
      if (nextRange === "custom") {
        activeRange = "custom";
        if (!isIsoDate(customStartDate)) customStartDate = data.range_start || todayKey();
        if (!isIsoDate(customEndDate)) customEndDate = data.range_end || customStartDate;
        normalizeCustomRange();
        customRangePending = data.range !== "custom";
        customRangeOpen = true;
        render(data);
        return;
      }
      customRangePending = false;
      customRangeOpen = false;
      activeRange = nextRange;
      invalidateRequests();
      syncUrl();
      refresh();
    });
  });

  const customRangeDialog = document.querySelector("#custom-range-dialog");
  const customRangeTrigger = document.querySelector("#custom-range-trigger");
  const customRangeForm = document.querySelector("#custom-range-form");
  if (customRangeDialog && customRangeTrigger && customRangeForm) {
    let listenersAttached = true;
    const repositionDialog = () => positionCustomRangeDialog(customRangeDialog, customRangeTrigger);
    const removePositionListeners = () => {
      if (!listenersAttached) return;
      listenersAttached = false;
      window.removeEventListener("resize", repositionDialog);
      window.visualViewport?.removeEventListener("resize", repositionDialog);
      window.visualViewport?.removeEventListener("scroll", repositionDialog);
    };
    const dismissDialog = () => {
      removePositionListeners();
      customRangeOpen = false;
      document.documentElement.classList.remove("custom-range-modal-open");
      if (customRangePending) {
        activeRange = data.range;
        customRangePending = false;
      }
      customRangeDialog.close();
      render(data);
    };

    customRangeDialog.showModal();
    repositionDialog();
    customRangeDialog.classList.add("positioned");
    document.querySelector("#custom-start")?.focus();
    window.addEventListener("resize", repositionDialog);
    window.visualViewport?.addEventListener("resize", repositionDialog);
    window.visualViewport?.addEventListener("scroll", repositionDialog);
    customRangeDialog.addEventListener("close", removePositionListeners, { once: true });
    customRangeDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      dismissDialog();
    });
    customRangeDialog.addEventListener("click", (event) => {
      if (event.target !== customRangeDialog) return;
      const rect = customRangeDialog.getBoundingClientRect();
      const inside = event.clientX >= rect.left && event.clientX <= rect.right
        && event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) dismissDialog();
    });
    customRangeDialog.querySelector(".custom-range-close")?.addEventListener("click", dismissDialog);
    customRangeForm.addEventListener("submit", (event) => {
      event.preventDefault();
      customStartDate = document.querySelector("#custom-start")?.value || customStartDate;
      customEndDate = document.querySelector("#custom-end")?.value || customEndDate;
      normalizeCustomRange();
      activeRange = "custom";
      invalidateRequests();
      customRangePending = false;
      customRangeOpen = false;
      removePositionListeners();
      document.documentElement.classList.remove("custom-range-modal-open");
      customRangeDialog.close();
      syncUrl();
      refresh();
    });
  }

  const settingsDialog = document.querySelector("#settings-dialog");
  if (settingsDialog) {
    settingsDialog.showModal();
    const dismissSettings = () => closeSettings();
    settingsDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      dismissSettings();
    });
    settingsDialog.querySelector(".settings-close")?.addEventListener("click", dismissSettings);
    settingsDialog.querySelector(".settings-retry")?.addEventListener("click", openSettings);
    settingsDialog.querySelector(".settings-cancel")?.addEventListener("click", dismissSettings);
    settingsDialog.addEventListener("click", (event) => {
      if (event.target === settingsDialog) dismissSettings();
    });
    const syncDirtyControls = () => {
      const dirty = settingsIsDirty();
      const applyButton = settingsDialog.querySelector(".settings-apply");
      applyButton?.toggleAttribute("disabled", !dirty);
      if (applyButton) applyButton.innerHTML = settingsApplied && !dirty ? "Applied" : "Apply";
      settingsDialog.querySelector("#resync-unibase")?.toggleAttribute("disabled", dirty || settingsData.unibase.state === "reset_empty");
      settingsDialog.querySelector("#reset-unibase")?.toggleAttribute("disabled", dirty);
    };
    settingsDialog.querySelectorAll("[data-settings-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        settingsActiveTab = button.dataset.settingsTab;
        renderSettingsUpdate();
      });
    });
    settingsDialog.querySelector("#settings-merge-models")?.addEventListener("change", (event) => {
      settingsApplied = false;
      settingsDraft.merge_models_across_providers = event.target.checked;
      syncDirtyControls();
    });
    settingsDialog.querySelectorAll("[data-non-working-weekday]").forEach((input) => {
      input.addEventListener("change", () => {
        const day = Number(input.dataset.nonWorkingWeekday);
        const selected = new Set(settingsDraft.non_working_weekdays);
        if (input.checked) selected.add(day);
        else selected.delete(day);
        if (selected.size === 7) {
          input.checked = false;
          input.setCustomValidity("At least one working day is required.");
          input.reportValidity();
          input.setCustomValidity("");
          return;
        }
        input.setCustomValidity("");
        settingsApplied = false;
        settingsDraft.non_working_weekdays = [...selected].sort((left, right) => left - right);
        syncDirtyControls();
      });
    });
    settingsDialog.querySelectorAll("[data-settings-source]").forEach((input) => {
      input.addEventListener("change", () => {
        settingsApplied = false;
        const source = settingsDraft.sources.find((item) => item.source_id === input.dataset.settingsSource);
        if (source) source.enabled = input.checked;
        syncDirtyControls();
      });
    });
    settingsDialog.querySelectorAll("[data-settings-model]").forEach((input) => {
      input.addEventListener("change", () => {
        settingsApplied = false;
        const model = settingsDraft.models.find((item) => item.model === input.dataset.settingsModel);
        if (model) model.enabled = input.checked;
        syncDirtyControls();
      });
    });
    settingsDialog.querySelector("#settings-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (settingsApplyPending || !settingsIsDirty()) return;
      const body = JSON.stringify({ revision: settingsData.revision, ...settingsDraft });
      settingsApplyPending = true;
      settingsApplied = false;
      settingsError = null;
      renderSettingsUpdate();
      try {
        const response = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body
        });
        if (!response.ok) throw new Error(response.status === 409 ? "Settings changed in another window. Reopen Settings." : `Settings API returned HTTP ${response.status}`);
        settingsData = await response.json();
        settingsDraft = settingsDraftFromData(settingsData);
        settingsApplyPending = false;
        settingsApplied = true;
        invalidateDashboardCaches();
        syncUrl();
        await refresh();
        settingsApplyPending = false;
        closeSettings();
      } catch (error) {
        settingsApplyPending = false;
        settingsApplied = false;
        settingsError = error.message;
        renderSettingsUpdate();
      }
    });
    settingsDialog.querySelector("#reset-unibase")?.addEventListener("click", () => {
      resetConfirmOpen = true;
      resetConfirmation = "";
      render(data);
    });
    settingsDialog.querySelector("#resync-unibase")?.addEventListener("click", async () => {
      settingsError = null;
      try {
        const response = await fetch("/api/unibase/resync", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}"
        });
        if (!response.ok) throw new Error(`Resync returned HTTP ${response.status}`);
        const payload = await response.json();
        settingsData.unibase.current_operation = { kind: "resync", state: "queued", progress_current: 0, progress_total: 0 };
        render(data);
        await pollOperation(payload.operation_id);
      } catch (error) {
        settingsError = error.message;
        render(data);
      }
    });
  }

  const resetDialog = document.querySelector("#reset-confirm-dialog");
  if (resetDialog) {
    resetDialog.showModal();
    const cancelReset = () => {
      resetConfirmOpen = false;
      resetConfirmation = "";
      render(data);
    };
    resetDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      cancelReset();
    });
    resetDialog.querySelector(".reset-cancel")?.addEventListener("click", cancelReset);
    const confirmationInput = resetDialog.querySelector("#reset-confirm-input");
    confirmationInput?.focus();
    confirmationInput?.addEventListener("input", () => {
      resetConfirmation = confirmationInput.value;
      resetDialog.querySelector("#reset-confirm-submit")?.toggleAttribute("disabled", resetConfirmation !== "RESET UNIBASE");
    });
    resetDialog.querySelector("#reset-confirm-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const response = await fetch("/api/unibase/reset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirmation: resetConfirmation })
        });
        if (!response.ok) throw new Error(`Reset returned HTTP ${response.status}`);
        const payload = await response.json();
        settingsData.unibase.current_operation = { kind: "reset", state: "queued", progress_current: 0, progress_total: 0 };
        resetConfirmOpen = false;
        resetConfirmation = "";
        render(data);
        await pollOperation(payload.operation_id);
      } catch (error) {
        settingsError = error.message;
        resetConfirmOpen = false;
        render(data);
      }
    });
  }

  scheduleSourceSyncPoll(data);
}

function icon(name, className = "") {
  const paths = iconPaths[name] || iconPaths.layers;
  return `<svg class="icon ${className}" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
}

function providerLogo(provider, className = "") {
  const path = providerLogoPaths[provider] || providerLogoPaths.all;
  return `<svg class="provider-logo ${className}" aria-hidden="true" viewBox="0 0 24 24" fill="currentColor"><path d="${path}"></path></svg>`;
}

function metricCard({ label, value, iconName, tone, note = "", series = [], hero = false }) {
  const classes = ["metric-card", `metric-${tone}`, hero ? "metric-card-hero" : ""]
    .filter(Boolean)
    .join(" ");
  return `
    <article class="${classes}">
      <span class="metric-accent" aria-hidden="true"></span>
      <div class="metric-card-head">
        <div class="metric-label">${escapeHtml(label)}</div>
        <span class="metric-icon">${icon(iconName)}</span>
      </div>
      <div class="metric-value">${value}</div>
      ${note ? `<div class="metric-note">${note}</div>` : ""}
      ${renderSparkline(series, `${label} trend`)}
    </article>
  `;
}

function groupMetricItem({ label, value, iconName, tone, note = "", series = [], compact = false }) {
  const classes = ["group-metric-item", `metric-${tone}`, compact ? "group-metric-item-compact" : ""]
    .filter(Boolean)
    .join(" ");
  return `
    <div class="${classes}" aria-label="${escapeHtml(label)}">
      <div class="group-metric-main">
        <span class="metric-icon">${icon(iconName)}</span>
        <div class="group-metric-copy">
          <div class="metric-label">${escapeHtml(label)}</div>
          <div class="group-metric-value">${value}</div>
          ${note ? `<div class="metric-note">${note}</div>` : ""}
        </div>
      </div>
      ${renderSparkline(series, `${label} trend`)}
    </div>
  `;
}

function hideModelNameTooltip() {
  if (modelTooltipTimer) window.clearTimeout(modelTooltipTimer);
  modelTooltipTimer = null;
  modelNameTooltip.classList.remove("visible");
}

function scheduleModelNameTooltip(event) {
  hideModelNameTooltip();
  const target = event.currentTarget;
  modelTooltipTimer = window.setTimeout(() => {
    if (!target.isConnected || !target.matches(":hover, :focus")) return;
    modelNameTooltip.textContent = target.dataset.modelTooltip;
    modelNameTooltip.classList.add("visible");
    const gap = 8;
    const margin = 8;
    const targetRect = target.getBoundingClientRect();
    const tooltipRect = modelNameTooltip.getBoundingClientRect();
    let left = targetRect.left;
    let top = targetRect.bottom + gap;
    left = Math.max(margin, Math.min(left, window.innerWidth - tooltipRect.width - margin));
    if (top + tooltipRect.height > window.innerHeight - margin) {
      top = targetRect.top - tooltipRect.height - gap;
    }
    modelNameTooltip.style.left = `${left}px`;
    modelNameTooltip.style.top = `${Math.max(margin, top)}px`;
  }, 1500);
}

function showHeatTooltip(event) {
  const target = event.currentTarget;
  tooltip.innerHTML = `${escapeHtml(target.dataset.tooltipDate)}<br><strong>${escapeHtml(target.dataset.tooltipTokens)}</strong>`;
  tooltip.classList.add("visible");
  positionHeatTooltip(event);
}

function showChartTooltip(event) {
  showChartTooltipFor(event.currentTarget, event);
}

function showChartTooltipFor(target, event) {
  tooltip.innerHTML = `${escapeHtml(target.dataset.tooltipTitle)}<br><strong>${target.dataset.tooltipBody}</strong>`;
  tooltip.classList.add("visible");
  if (event.type === "focus") positionTooltipAtTarget(target);
  else positionHeatTooltip(event);
}

function positionTooltipAtTarget(target) {
  const gap = 12;
  const margin = 8;
  const targetRect = target.getBoundingClientRect();
  const tooltipRect = tooltip.getBoundingClientRect();
  let left = targetRect.left + targetRect.width / 2 - tooltipRect.width / 2;
  let top = targetRect.top - tooltipRect.height - gap;
  left = Math.max(margin, Math.min(left, window.innerWidth - tooltipRect.width - margin));
  if (top < margin) top = targetRect.bottom + gap;
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${Math.max(margin, Math.min(top, window.innerHeight - tooltipRect.height - margin))}px`;
}

function handleChartTooltipLeave(event) {
  const slot = event.currentTarget.closest(".bar-slot");
  const isSegment = event.currentTarget.classList.contains("bar-segment")
    || event.currentTarget.classList.contains("activity-time-segment");
  if (isSegment && slot?.contains(event.relatedTarget)) {
    showChartTooltipFor(slot, event);
    return;
  }
  hideHeatTooltip();
}

function positionHeatTooltip(event) {
  if (!tooltip.classList.contains("visible")) return;
  const gap = 12;
  const margin = 8;
  const rect = tooltip.getBoundingClientRect();
  let left = event.clientX - rect.width / 2;
  let top = event.clientY - rect.height - gap;

  left = Math.max(margin, Math.min(left, window.innerWidth - rect.width - margin));
  if (top < margin) {
    top = event.clientY + gap;
  }
  top = Math.max(margin, Math.min(top, window.innerHeight - rect.height - margin));

  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${top}px`;
}

function hideHeatTooltip() {
  tooltip.classList.remove("visible");
}

async function refresh() {
  cancelDiagnosticsRequest();
  try {
    const cached = usageCache.get(buildQuery(activeRange));
    if (cached) render(cached);
    const data = await load(activeRange);
    diagnosticsCache.delete(diagnosticsKey(data));
    diagnosticsErrors.delete(diagnosticsKey(data));
    activeProvider = data.provider || activeProvider;
    activeRange = data.range || activeRange;
    activeChartRange = data.chart?.range || activeChartRange;
    customRangePending = false;
    customRangeOpen = false;
    chartCustomRangePending = false;
    chartCustomRangeOpen = false;
    if (data.range === "custom") {
      customStartDate = data.range_start || customStartDate;
      customEndDate = data.range_end || customEndDate;
    }
    if (data.chart?.range === "custom") {
      chartStartDate = data.chart.range_start || chartStartDate;
      chartEndDate = data.chart.range_end || chartEndDate;
    }
    saveActiveChartState();
    render(data);
  } catch (error) {
    if (error.name === "AbortError") return;
    customRangeOpen = false;
    chartCustomRangeOpen = false;
    document.documentElement.classList.remove("custom-range-modal-open");
    const providerLabel = providerOptions.find((option) => option.value === activeProvider)?.label || "ALL";
    app.innerHTML = `<section class="state error"><h1>MeterMesh · ${providerLabel}</h1><p>Could not load committed Unibase data.</p><code>${escapeHtml(error.message)}</code></section>`;
  }
}

if (activeRange === "custom") {
  normalizeCustomRange();
}
if (activeChartRange === "custom") {
  normalizeChartCustomRange();
}
saveActiveChartState();

syncUrl();
refresh();
