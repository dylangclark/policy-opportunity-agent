"use strict";

const DATA_ROOT = "data";
const state = { manifest: null, opportunities: [], changes: [], sources: [], view: "opportunities", calendarWeekStart: null };

const el = (id) => document.getElementById(id);
const asArray = (value) => Array.isArray(value) ? value : [];
const text = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
const escapeHtml = (value) => text(value, "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));

function filePath(key, fallback) {
  const entry = state.manifest?.files?.[key];
  return `${DATA_ROOT}/${entry?.path || fallback}`;
}

async function fetchJson(url) {
  const response = await fetch(`${url}?v=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}: ${url}`);
  return response.json();
}

function formatDate(value, includeTime = true) {
  if (!value) return "Date unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return text(value);
  const options = { timeZone: "America/Vancouver", weekday: "short", month: "short", day: "numeric", year: "numeric" };
  if (includeTime) Object.assign(options, { hour: "numeric", minute: "2-digit" });
  return new Intl.DateTimeFormat("en-CA", options).format(date);
}

function eventDate(item) {
  return item.relevant_at || item.deadline_at || item.scheduled_at || item.published_at || null;
}

function dateKeyInVancouver(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.valueOf())) return null;
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Vancouver",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function dateFromKey(key) {
  const [year, month, day] = key.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day, 12));
}

function addDays(date, days) {
  const result = new Date(date);
  result.setUTCDate(result.getUTCDate() + days);
  return result;
}

function startOfWeek(value = new Date()) {
  const localDate = dateFromKey(dateKeyInVancouver(value));
  const weekday = localDate.getUTCDay();
  return addDays(localDate, weekday === 0 ? -6 : 1 - weekday);
}

function formatCalendarDay(date, options) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", ...options }).format(date);
}

function normalizeStatus(status) {
  const value = String(status || "unknown").toLowerCase();
  return ["ok", "partial", "failed"].includes(value) ? value : "unknown";
}

async function loadData() {
  setLoading(true);
  try {
    state.manifest = await fetchJson(`${DATA_ROOT}/manifest.json`);
    const [oppData, changesData, sourceData] = await Promise.all([
      fetchJson(filePath("opportunities", "opportunities.json")),
      fetchJson(filePath("changes", "changes.json")),
      fetchJson(filePath("source_status", "source-status.json")),
    ]);
    state.opportunities = asArray(oppData.opportunities);
    state.changes = asArray(changesData.changes);
    state.sources = asArray(sourceData.sources);
    if (!state.calendarWeekStart) state.calendarWeekStart = startOfWeek();
    populateFilters();
    renderAll();
    showStaleness();
    el("error-panel").classList.add("hidden");
  } catch (error) {
    el("error-panel").textContent = `Unable to load policy data. ${error.message}`;
    el("error-panel").classList.remove("hidden");
    el("run-status").textContent = "Load failed";
    el("run-status").className = "status-pill failed";
  } finally {
    setLoading(false);
  }
}

function setLoading(loading) {
  el("refresh-button").disabled = loading;
  if (loading) el("refresh-button").textContent = "Loading…";
  else el("refresh-button").textContent = "Refresh";
}

function populateFilters() {
  const jurisdictions = [...new Set(state.opportunities.map(x => x.jurisdiction).filter(Boolean))].sort();
  const topics = [...new Set(state.opportunities.flatMap(x => asArray(x.topics)).filter(Boolean))].sort();
  replaceOptions("jurisdiction-filter", jurisdictions, "All jurisdictions");
  replaceOptions("topic-filter", topics, "All topics", value => value.replaceAll("_", " "));
}

function replaceOptions(id, values, allLabel, formatter = value => value) {
  const select = el(id);
  const prior = select.value;
  select.innerHTML = `<option value="all">${escapeHtml(allLabel)}</option>` + values.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(formatter(value))}</option>`).join("");
  if ([...select.options].some(option => option.value === prior)) select.value = prior;
}

function renderAll() {
  renderSummary();
  renderOpportunities();
  renderCalendar();
  renderChanges();
  renderSources();
  const generated = state.manifest?.generated_at;
  el("generated-at").textContent = generated ? `Last collected ${formatDate(generated)}` : "Collection time unavailable";
}

function renderSummary() {
  const total = state.opportunities.length;
  const execution = state.opportunities.filter(x => x.horizon === "execution").length;
  const changed = state.changes.filter(x => !["unchanged", "none"].includes(String(x.change_type || x.type || "").toLowerCase())).length;
  const ok = state.sources.filter(x => normalizeStatus(x.status) === "ok").length;
  const partial = state.sources.filter(x => normalizeStatus(x.status) === "partial").length;
  const failed = state.sources.filter(x => normalizeStatus(x.status) === "failed").length;
  el("summary-high").textContent = total;
  el("summary-execution").textContent = execution;
  el("summary-changes").textContent = changed;
  el("summary-health").textContent = `${ok}/${state.sources.length}`;
  el("summary-health-detail").textContent = `${partial} partial · ${failed} failed`;
  const status = normalizeStatus(state.manifest?.status);
  el("run-status").textContent = status === "ok" ? "All sources healthy" : status === "partial" ? "Partial collection" : status === "failed" ? "Collection failed" : text(state.manifest?.status, "Unknown status");
  el("run-status").className = `status-pill ${status}`;
}

function filteredOpportunities() {
  const query = el("search-input").value.trim().toLowerCase();
  const horizon = el("horizon-filter").value;
  const jurisdiction = el("jurisdiction-filter").value;
  const topic = el("topic-filter").value;
  const sort = el("sort-filter").value;

  const items = state.opportunities.filter(item => {
    const searchable = [item.event_title, item.institution, item.source_name, item.event_type, item.hook_type, item.why_now, ...asArray(item.topics)].join(" ").toLowerCase();
    return (!query || searchable.includes(query))
      && (horizon === "all" || item.horizon === horizon)
      && (jurisdiction === "all" || item.jurisdiction === jurisdiction)
      && (topic === "all" || asArray(item.topics).includes(topic));
  });

  items.sort((a, b) => {
    if (sort === "newest") {
      return String(a.change_type === "new" ? 0 : 1)
        .localeCompare(String(b.change_type === "new" ? 0 : 1))
        || new Date(eventDate(a) || 0) - new Date(eventDate(b) || 0);
    }
    return new Date(eventDate(a) || 0) - new Date(eventDate(b) || 0);
  });
  return items;
}

function renderOpportunities() {
  const items = filteredOpportunities();
  const list = el("opportunity-list");
  list.innerHTML = "";
  el("results-count").textContent = `${items.length} of ${state.opportunities.length} opportunities`;
  el("empty-state").classList.toggle("hidden", items.length !== 0);

  const template = el("opportunity-template");
  for (const item of items) {
    const node = template.content.cloneNode(true);
    node.querySelector(".card-meta").textContent = `${formatDate(eventDate(item))} · ${text(item.institution)} · ${text(item.jurisdiction)}`;
    const link = node.querySelector(".title-link");
    link.textContent = text(item.event_title, "Untitled opportunity");
    link.href = item.source_url || "#";
    node.querySelector(".why-now").textContent = text(item.why_now, "No timing rationale supplied.");

    const tags = [item.horizon, item.hook_type, item.event_type, ...asArray(item.topics)].filter(Boolean);
    node.querySelector(".tag-row").innerHTML = tags.map(tag => `<span class="tag">${escapeHtml(String(tag).replaceAll("_", " "))}</span>`).join("");

    const angles = asArray(item.angle_prompts);
    node.querySelector(".angle-list").innerHTML = angles.length ? angles.map(angle => `<li>${escapeHtml(angle)}</li>`).join("") : "<li>No angle prompts supplied.</li>";
    list.appendChild(node);
  }
}

function renderCalendar() {
  if (!state.calendarWeekStart) state.calendarWeekStart = startOfWeek();
  const items = filteredOpportunities().filter(item => eventDate(item));
  const start = state.calendarWeekStart;
  const end = addDays(start, 6);
  const todayKey = dateKeyInVancouver(new Date());
  const days = Array.from({ length: 7 }, (_, index) => addDays(start, index));
  const byDay = new Map(days.map(day => [dateKeyInVancouver(day), []]));

  for (const item of items) {
    const key = dateKeyInVancouver(eventDate(item));
    if (byDay.has(key)) byDay.get(key).push(item);
  }
  for (const dayItems of byDay.values()) {
    dayItems.sort(
      (a, b) =>
        new Date(eventDate(a) || 0) - new Date(eventDate(b) || 0)
        || String(a.event_title || "").localeCompare(String(b.event_title || ""))
    );
  }

  el("calendar-range").textContent = `${formatCalendarDay(start, { month: "long", day: "numeric" })}–${formatCalendarDay(end, { month: "long", day: "numeric", year: "numeric" })}`;
  const grid = el("calendar-grid");
  grid.innerHTML = "";
  let total = 0;

  for (const day of days) {
    const key = dateKeyInVancouver(day);
    const dayItems = byDay.get(key) || [];
    total += dayItems.length;
    const column = document.createElement("section");
    column.className = `calendar-day${key === todayKey ? " today" : ""}`;
    column.innerHTML = `
      <header class="calendar-day-header">
        <span>${escapeHtml(formatCalendarDay(day, { weekday: "short" }))}</span>
        <strong>${escapeHtml(formatCalendarDay(day, { month: "short", day: "numeric" }))}</strong>
        <small>${dayItems.length} ${dayItems.length === 1 ? "event" : "events"}</small>
      </header>
      <div class="calendar-events"></div>`;
    const eventList = column.querySelector(".calendar-events");
    for (const item of dayItems) {
      const event = document.createElement("article");
      event.className = "calendar-event";
      event.innerHTML = `
        <div class="calendar-event-top">
          <span class="calendar-time">${escapeHtml(formatDate(eventDate(item), true).split(", ").pop())}</span>
        </div>
        <a href="${escapeHtml(item.source_url || "#")}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.event_title || "Untitled opportunity")}</a>
        <small>${escapeHtml(item.institution || item.source_name || "")}</small>`;
      eventList.appendChild(event);
    }
    grid.appendChild(column);
  }

  el("calendar-empty").classList.toggle("hidden", total !== 0);
}

function moveCalendarWeek(days) {
  state.calendarWeekStart = addDays(state.calendarWeekStart || startOfWeek(), days);
  renderCalendar();
}

function renderChanges() {
  const list = el("changes-list");
  list.innerHTML = "";
  const items = [...state.changes].sort((a, b) => new Date(b.detected_at || b.generated_at || 0) - new Date(a.detected_at || a.generated_at || 0));
  if (!items.length) {
    list.innerHTML = '<div class="empty-state">No changes were recorded in the latest run.</div>';
    return;
  }
  for (const item of items.slice(0, 250)) {
    const kind = item.change_type || item.type || "changed";
    const title = item.event_title || item.title || item.current?.title || item.event_id || "Policy record";
    const description = item.summary || item.description || item.reason || `Record marked ${kind}.`;
    const card = document.createElement("article");
    card.className = "change-card";
    card.innerHTML = `<span class="tag">${escapeHtml(kind)}</span><h3>${escapeHtml(title)}</h3><p>${escapeHtml(description)}</p><small>${escapeHtml(formatDate(item.detected_at || item.generated_at || item.changed_at, true))}</small>`;
    list.appendChild(card);
  }
}

function renderSources() {
  const body = el("sources-table");
  body.innerHTML = "";
  const priority = { failed: 0, partial: 1, ok: 2, unknown: 3 };
  const items = [...state.sources].sort((a, b) => priority[normalizeStatus(a.status)] - priority[normalizeStatus(b.status)] || text(a.source_name).localeCompare(text(b.source_name)));
  for (const item of items) {
    const status = normalizeStatus(item.status);
    const details = item.error || asArray(item.warnings).join("; ") || (item.stale ? "Using retained previous data." : "No reported issue.");
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><a href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.source_name || item.source_id)}</a><br><small>${escapeHtml(item.collector || "")}</small></td>
      <td><span class="tag ${status}">${escapeHtml(status)}</span>${item.stale ? ' <span class="tag partial">stale</span>' : ""}</td>
      <td>${escapeHtml(item.event_count ?? 0)}</td>
      <td>${escapeHtml(formatDate(item.checked_at, true))}</td>
      <td class="source-detail">${escapeHtml(details)}</td>`;
    body.appendChild(row);
  }
}

function showStaleness() {
  const generated = state.manifest?.generated_at ? new Date(state.manifest.generated_at) : null;
  const hours = generated ? (Date.now() - generated.valueOf()) / 36e5 : Infinity;
  const panel = el("stale-panel");
  if (hours > 30) {
    panel.textContent = `The latest published collection is ${Math.floor(hours)} hours old. The Raspberry Pi collector may not have uploaded recently.`;
    panel.classList.remove("hidden");
  } else panel.classList.add("hidden");
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll(".tab").forEach(tab => tab.classList.toggle("active", tab.dataset.view === view));
  document.querySelectorAll(".view").forEach(section => section.classList.toggle("active", section.id === `${view}-view`));
}

function clearFilters() {
  el("search-input").value = "";
  el("horizon-filter").value = "all";
  el("jurisdiction-filter").value = "all";
  el("topic-filter").value = "all";
  el("sort-filter").value = "date";
  renderOpportunities();
  renderCalendar();
}

["search-input", "horizon-filter", "jurisdiction-filter", "topic-filter", "sort-filter"].forEach(id => {
  el(id).addEventListener(id === "search-input" ? "input" : "change", () => {
    renderOpportunities();
    renderCalendar();
  });
});
el("refresh-button").addEventListener("click", loadData);
el("clear-filters").addEventListener("click", clearFilters);
el("calendar-previous").addEventListener("click", () => moveCalendarWeek(-7));
el("calendar-next").addEventListener("click", () => moveCalendarWeek(7));
el("calendar-today").addEventListener("click", () => { state.calendarWeekStart = startOfWeek(); renderCalendar(); });
document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => switchView(tab.dataset.view)));

loadData();
