import { adjacentIndex, bufferedPercent, formatTime, lyricIndex, normalizePlayerState, normalizeTrackPage } from "./player-core.js";

const $ = (id) => document.getElementById(id);
const state = {
  sources: [], tracks: [], discovered: [], source: "", likedMode: false, current: null, editing: null,
  lyrics: null, flow: "", lyric: -1, queue: [], queueIndex: -1,
  shuffle: localStorage.getItem("tm-shuffle") === "1",
  repeat: localStorage.getItem("tm-repeat") || "off",
  cacheStates: {}, settings: { prefetchCount: 1, coverQuality: "1200", musicbrainzContact: "" },
  trackCache: new Map(), summaryCache: new Map(), libraryCache: new Map(),
  loadedPages: new Set(), pageRequests: new Set(), totalTracks: 0, windowStart: -1, libraryLoading: false,
  globalTracks: [], globalSources: [], summaryRequests: new Set(),
  temporarySource: null, temporaryJob: null, keepingSource: false, likedCount: 0, historyVisible: 50,
  lyricsFollow: true, lyricScrollTimer: 0, restored: false, contacts: [],
  bulk: false, selectedSources: new Set(),
  countriesLoaded: false,
  buffering: false,
};
const audio = $("audio");
let searchTimer, globalSearchTimer, toastTimer, qrTimer, qrStatusTimer, requestController, globalController, positionTimer;
let libraryRequest = 0, globalRequest = 0, libraryFrame = 0, scrollIdleTimer = 0, retryAction = null, lyricsController;
let confirmResolve = null, draggedSource = "", draggedQueue = -1, pendingShare = null;
let lastUiTrackKey = "";
let lastAudibleVolume = .8;
const pendingCovers = new Set();

class AppError extends Error {
  constructor(message, retryable = false, code = "request_failed") {
    super(message); this.retryable = retryable; this.code = code;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const failure = body?.error;
    // An expired or revoked session must return to the gate rather than surface
    // a stream of unrelated failures from whatever request happened to notice.
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      showView("lock-view");
      $("lock-status").textContent = "Your session expired. Sign in again.";
      $("lock-password").focus();
    }
    throw new AppError(failure?.message || body?.detail || `Request failed (${response.status})`, failure?.retryable, failure?.code);
  }
  return body;
}

function showView(id) {
  for (const view of ["lock-view", "telegram-view", "app-shell"]) $(view).hidden = view !== id;
}

function toast(message, action = null, duration = 3200) {
  clearTimeout(toastTimer);
  $("toast-message").textContent = message;
  $("toast-action").hidden = !action;
  $("toast-action").textContent = action?.label || "";
  $("toast-action").onclick = action?.run || null;
  const notice = $("toast");
  notice.style.setProperty("--toast-duration", `${duration}ms`);
  notice.classList.remove("counting");
  notice.hidden = false;
  requestAnimationFrame(() => notice.classList.add("counting"));
  toastTimer = setTimeout(() => { notice.hidden = true; notice.classList.remove("counting"); action?.expire?.(); }, duration);
}

function showError(error, retry = null, title = "Couldn’t complete that") {
  retryAction = retry;
  $("error-title").textContent = title;
  $("error-message").textContent = error?.message || String(error);
  $("error-retry").hidden = !(retry && error?.retryable !== false);
  if (!$("error-dialog").open) $("error-dialog").showModal();
}

function confirmAction(title, message, accept = "Continue") {
  $("confirm-title").textContent = title;
  $("confirm-message").textContent = message;
  $("confirm-accept").textContent = accept;
  if (!$("confirm-dialog").open) $("confirm-dialog").showModal();
  return new Promise((resolve) => { confirmResolve = resolve; });
}

function icon(name) { return `<svg aria-hidden="true"><use href="#i-${name}"></use></svg>`; }
function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value ?? ""; return node.innerHTML; }
function escapeAttr(value) { return escapeHtml(value).replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/`/g, "&#96;"); }
function mediaUrl(track, action = "cover") { return `/api/tracks/${encodeURIComponent(track.key)}/${action}`; }
function initials(value) { return String(value || "?").split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase(); }
function cacheSet(cache, key, value, maximum) {
  cache.delete(key); cache.set(key, value);
  while (cache.size > maximum) cache.delete(cache.keys().next().value);
}

function playerSnapshot() {
  return {
    version: 1, queue: state.queue, queueIndex: state.queueIndex,
    currentKey: state.current?.key || "", position: audio.currentTime || 0,
    source: state.source, liked: state.likedMode, temporarySource: state.temporarySource,
    panel: document.querySelector(".now-tabs .active")?.id?.replace("-tab", "") || "lyrics",
    panelOpen: !$("now-panel").hidden,
  };
}

function persistPlayerState() {
  try { localStorage.setItem("tm-player-state", JSON.stringify(playerSnapshot())); } catch {}
}

function schedulePersist() {
  clearTimeout(positionTimer);
  positionTimer = setTimeout(persistPlayerState, 250);
}

async function restorePlayerState(saved) {
  if (!saved) return;
  state.queue = saved.queue; state.queueIndex = saved.queueIndex;
  if (!saved.currentKey) { renderQueue(); return; }
  try {
    const track = await getTrack(saved.currentKey);
    state.current = { ...track, qualified: false, restored: true };
    if (state.queueIndex < 0 || state.queue[state.queueIndex] !== saved.currentKey) {
      state.queueIndex = state.queue.indexOf(saved.currentKey);
    }
    audio.src = mediaUrl(track, "audio");
    audio.addEventListener("loadedmetadata", () => {
      audio.currentTime = Math.min(saved.position, Number.isFinite(audio.duration) ? audio.duration : saved.position);
    }, { once: true });
    setTrackUi(); renderQueue(); loadLyrics(); schedulePrefetch();
  } catch {
    state.current = null;
    toast("The previously playing track is no longer available.");
  }
}

function applyPreferences() {
  for (const [name, fallback] of [["theme", "system"], ["font", "sans"], ["accent", "blue"]]) {
    const value = localStorage.getItem(`tm-${name}`) || fallback;
    document.documentElement.dataset[name] = value;
    document.querySelectorAll(`[data-setting="${name}"] [data-value]`).forEach((button) => {
      const active = button.dataset.value === value;
      button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active));
    });
  }
  $("app-shell").classList.toggle("sidebar-collapsed", localStorage.getItem("tm-sidebar") === "collapsed");
  document.documentElement.style.setProperty("--rail-width", `${Math.max(220, Math.min(420, Number(localStorage.getItem("tm-rail-width")) || 260))}px`);
  document.documentElement.style.setProperty("--panel-width", `${Math.max(300, Math.min(640, Number(localStorage.getItem("tm-panel-width")) || 368))}px`);
  updateModes();
}

function setLoginStage(stage, message = "") {
  for (const name of ["phone", "code", "twofa"]) $(`${name}-form`).hidden = name !== stage;
  if (message) $("phone-status").textContent = message;
  const field = { phone: "telegram-country", code: "telegram-code", twofa: "telegram-password" }[stage];
  requestAnimationFrame(() => $(field)?.focus());
}

function pauseQr(message) {
  clearTimeout(qrTimer);
  $("qr-stage").classList.add("paused");
  $("qr-stage").dataset.pauseLabel = message;
}

function setQrStatus(message) {
  const status = $("qr-status");
  clearTimeout(qrStatusTimer);
  status.classList.add("is-changing");
  qrStatusTimer = setTimeout(() => {
    status.textContent = message;
    status.classList.remove("is-changing");
  }, matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 150);
}

async function clearQr() {
  const code = $("qr-code");
  if (!code.childElementCount) return;
  code.classList.add("is-exiting");
  await new Promise((resolve) => setTimeout(resolve, matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 150));
  code.replaceChildren();
  code.classList.remove("is-exiting");
}

function phoneNumber() {
  const country = $("telegram-country").value;
  const number = $("telegram-phone").value.replace(/\D/g, "");
  if (!country) throw new AppError("Choose your country first.");
  if (!number) throw new AppError("Enter your phone number.");
  return `+${country}${number}`;
}

function countryFlag(iso2) {
  return [...iso2.toUpperCase()].map((letter) => String.fromCodePoint(127397 + letter.charCodeAt(0))).join("");
}

async function loadCountries() {
  if (state.countriesLoaded) return;
  const select = $("telegram-country");
  try {
    const countries = await api("/api/telegram/countries");
    select.innerHTML = '<option value="">Choose a country</option>' + countries.map((country) => `<option value="${escapeAttr(country.dialCode)}" data-iso="${escapeAttr(country.iso2)}">${countryFlag(country.iso2)} ${escapeHtml(country.name)} (+${escapeHtml(country.dialCode)})</option>`).join("");
    const saved = localStorage.getItem("tm-country");
    let region = saved;
    if (!region) try { region = new Intl.Locale(navigator.language).region; } catch {}
    const preferred = [...select.options].find((option) => option.dataset.iso === region);
    if (preferred) preferred.selected = true;
    select.disabled = false;
    state.countriesLoaded = true;
    select.dispatchEvent(new Event("change"));
  } catch (error) {
    select.innerHTML = '<option value="">Countries unavailable</option>';
    showError(error, loadCountries);
  }
}

async function startQr() {
  clearTimeout(qrTimer);
  const qrExit = clearQr();
  state.flow = "";
  setLoginStage("phone", "Choose your country to continue.");
  $("telegram-code").value = "";
  $("telegram-password").value = "";
  $("qr-stage").classList.remove("paused");
  $("qr-stage").setAttribute("aria-busy", "true");
  setQrStatus("Generating a new code…");
  $("qr-start").setAttribute("aria-busy", "true");
  try {
    const flow = await api("/api/telegram/qr", { method: "POST" });
    await qrExit;
    state.flow = flow.flowId;
    $("qr-code").innerHTML = flow.svg;
    $("qr-stage").setAttribute("aria-busy", "false");
    setQrStatus("Ready to scan · refreshes automatically");
    pollQr(flow.flowId);
  } catch (error) {
    await qrExit;
    setQrStatus("Couldn’t generate a QR code.");
    showError(error, startQr);
  } finally {
    $("qr-start").removeAttribute("aria-busy");
  }
}

function pollQr(flowId) {
  clearTimeout(qrTimer);
  qrTimer = setTimeout(async () => {
    if (state.flow !== flowId || $("telegram-view").hidden) return;
    try {
      const status = await api(`/api/telegram/flow/${encodeURIComponent(flowId)}`);
      if (status.state === "ready") return boot();
      if (status.state === "password_required") {
        pauseQr("QR accepted");
        setQrStatus("QR accepted · enter your 2FA password");
        return setLoginStage("twofa", "Enter your Telegram two-step verification password.");
      }
      if (status.state === "expired") return startQr();
      if (status.state === "error") {
        setQrStatus("QR login stopped.");
        return showError(new AppError(status.error || "QR login stopped."), startQr);
      }
      pollQr(flowId);
    } catch (error) { showError(error, startQr); }
  }, 1500);
}

function enterTelegramLogin() {
  loadCountries();
  if (!state.flow) startQr();
}

async function boot() {
  applyPreferences();
  const auth = await api("/api/auth/status");
  if (auth.passwordEnabled && !auth.authenticated) {
    showView("lock-view");
    $("lock-password").focus();
    return;
  }
  const status = await api("/api/status");
  if (!status.telegram.linked) {
    showView("telegram-view");
    if (status.startupError) showError(new AppError("The saved Telegram authorization expired. Reconnect with QR or phone; your local library and edits are safe."), null, "Reconnect Telegram");
    enterTelegramLogin();
    return;
  }
  $("account-name").textContent = status.telegram.displayName || "Connected";
  showView("app-shell");
  let saved = null;
  try { saved = normalizePlayerState(JSON.parse(localStorage.getItem("tm-player-state") || "null")); } catch {}
  const stats = await api("/api/library/stats");
  [state.settings, state.sources] = await Promise.all([api("/api/settings"), api("/api/sources")]);
  state.likedCount = stats.likedCount || 0;
  state.temporarySource = saved?.temporarySource || null;
  state.likedMode = Boolean(saved?.liked);
  if (!state.likedMode && saved?.source && (
    state.sources.some((source) => source.chatId === saved.source)
    || state.temporarySource?.chatId === saved.source
  )) state.source = saved.source;
  renderSources();
  await loadLibrary();
  await restorePlayerState(saved);
  if (saved?.panelOpen) showPanel(saved.panel);
  state.restored = true;
  if (status.startupError) showError(new AppError(status.startupError));
}

function sourceSort(items) {
  const mode = $("sidebar-sort").value || "custom";
  const compare = (a, b) => mode === "name" ? a.title.localeCompare(b.title)
    : mode === "recent" ? (b.lastPostAt || 0) - (a.lastPostAt || 0)
    : mode === "count" ? b.trackCount - a.trackCount
    : a.sortOrder - b.sortOrder || a.title.localeCompare(b.title);
  const pinned = items.filter((item) => item.pinnedAt).sort((a, b) => a.pinnedAt - b.pinnedAt);
  const rest = items.filter((item) => !item.pinnedAt).sort(compare);
  return [...pinned, ...rest];
}

function avatarMarkup(source) {
  return `<img class="source-avatar" src="/api/sources/${encodeURIComponent(source.chatId)}/avatar" data-avatar-fallback="${escapeAttr(initials(source.title))}" alt="" loading="lazy">`;
}

function renderSources() {
  const sorted = sourceSort(state.sources);
  $("all-count").textContent = state.sources.reduce((total, item) => total + item.trackCount, 0);
  $("liked-count").textContent = state.likedCount.toLocaleString();
  $("liked-source").classList.toggle("active", state.likedMode);
  $("liked-source").toggleAttribute("aria-current", state.likedMode);
  const temporary = state.temporarySource && !state.sources.some((item) => item.chatId === state.temporarySource.chatId)
    ? `<div class="source-link source-entry temporary-source ${state.source === state.temporarySource.chatId ? "active" : ""}" data-temporary-source="${state.temporarySource.chatId}" role="button" tabindex="0" title="${escapeHtml(state.temporarySource.title)}">${avatarMarkup(state.temporarySource)}<span class="source-copy"><strong>${escapeHtml(state.temporarySource.title)}</strong><small>Temporary · current track</small></span><span class="source-count">Live</span></div>` : "";
  $("source-list").innerHTML = temporary + sorted.map((source) => {
    const draggable = $("sidebar-sort").value === "custom" && !state.bulk && !source.pinnedAt;
    return `<div class="source-link source-entry ${!state.likedMode && source.chatId === state.source ? "active" : ""}${source.pinnedAt ? " pinned" : ""}" data-source="${source.chatId}" role="button" tabindex="0" title="${escapeHtml(source.title)}" draggable="${draggable}"${!state.likedMode && source.chatId === state.source ? ' aria-current="page"' : ""}>
    ${state.bulk ? `<input class="source-select" type="checkbox" data-bulk-source="${source.chatId}" ${state.selectedSources.has(source.chatId) ? "checked" : ""} aria-label="Select ${escapeHtml(source.title)}">` : avatarMarkup(source)}
    <span class="source-copy"><strong>${source.pinnedAt ? `<span class="source-pin-mark" aria-hidden="true">${icon("pin")}</span>` : ""}${escapeHtml(source.title)}</strong><small>${escapeHtml(source.kind)}${source.syncError ? " · needs attention" : ""}</small></span>
    <span class="source-actions"><span class="source-count">${source.trackCount}</span><button class="icon-button" type="button" data-sync-source="${source.chatId}" data-full="false" title="Sync new tracks" aria-label="Sync new tracks from ${escapeHtml(source.title)}">${icon("sync")}</button><button class="icon-button" type="button" data-sync-source="${source.chatId}" data-full="true" title="Full rescan" aria-label="Full rescan ${escapeHtml(source.title)}">${icon("repeat")}</button><button class="icon-button ${source.pinnedAt ? "active" : ""}" type="button" data-pin-source="${source.chatId}" title="${source.pinnedAt ? "Unpin" : "Pin"} from top" aria-pressed="${Boolean(source.pinnedAt)}" aria-label="${source.pinnedAt ? "Unpin" : "Pin"} ${escapeHtml(source.title)}">${icon("pin")}</button></span>
  </div>`;
  }).join("");
  const allMusic = document.querySelector('[data-source=""]');
  allMusic?.classList.toggle("active", !state.source && !state.likedMode);
  allMusic?.toggleAttribute("aria-current", !state.source && !state.likedMode);
  const selected = state.likedMode ? null : state.sources.find((item) => item.chatId === state.source) || (state.temporarySource?.chatId === state.source ? state.temporarySource : null);
  $("source-title").textContent = state.likedMode ? "Liked songs" : selected?.title || "All music";
  $("source-kind").textContent = state.likedMode ? "Saved locally" : selected ? (selected.temporary ? "Temporary source" : selected.kind) : "Your Telegram";
  $("library-summary").textContent = `${state.totalTracks.toLocaleString()} ${state.totalTracks === 1 ? "track" : "tracks"}${selected?.lastSyncedAt ? ` · synced ${new Date(selected.lastSyncedAt * 1000).toLocaleString()}` : ""}`;
  $("bulk-count").textContent = `${state.selectedSources.size} selected`;
  $("bulk-unselect").disabled = !state.selectedSources.size;
  $("sync-source").disabled = state.likedMode || Boolean(selected?.temporary);
  // A temporary source is a dead end otherwise: it vanishes when the track stops and you
  // would have to hunt it down again in Add source. Offer to keep it while you are in it.
  const keep = $("keep-source");
  keep.hidden = !selected?.temporary;
  keep.disabled = Boolean(state.keepingSource);
  keep.textContent = "";
  keep.insertAdjacentHTML("beforeend", '<svg><use href="#i-plus"/></svg>');
  keep.append(state.keepingSource ? "Adding\u2026" : "Add to library");
  if (state._sourceChangeScroll) {
    document.querySelector('.source-entry.active')?.scrollIntoView({ block: 'nearest' });
    state._sourceChangeScroll = false;
  }
}

function renderTrackRow(track) {
  const playing = track.key === state.current?.key;
  const liked = Boolean(track.liked);
  return `<article class="track-row ${playing ? "current" : ""}" data-track-key="${escapeHtml(track.key)}" tabindex="-1">
    <button class="track-main" type="button" data-play-key="${escapeHtml(track.key)}">
      <span class="mini-art-wrap"><img class="mini-art row-art" data-src="${mediaUrl(track)}?v=${encodeURIComponent(track.artworkVersion || "telegram")}" alt=""><span class="art-placeholder mini"><span></span></span><span class="track-play-overlay">${icon(playing && !audio.paused ? "pause" : "play")}</span></span>
      <span class="track-copy"><strong>${escapeHtml(track.title)}</strong><small>${escapeHtml(track.artist || "Unknown artist")}</small></span>
    </button>
    <span class="track-source">${escapeHtml(track.source.title)}</span>
    <span class="track-duration utility">${formatTime(track.durationMs / 1000)}</span>
    <span class="track-row-actions">
      <button class="icon-button row-like ${liked ? "active" : ""}" type="button" data-row-like-key="${escapeHtml(track.key)}" aria-pressed="${liked}" aria-label="${liked ? "Unlike" : "Like"} ${escapeHtml(track.title)}">${icon(liked ? "heart-filled" : "heart")}</button>
    </span>
    <button class="icon-button row-menu" type="button" data-track-menu="${escapeHtml(track.key)}" aria-label="Actions for ${escapeHtml(track.title)}">${icon("more")}</button>
  </article>`;
}

function renderTrackPlaceholder() {
  return '<article class="track-row track-placeholder" aria-hidden="true"><span class="placeholder-main"><i></i><span><i></i><i></i></span></span><i class="placeholder-source"></i><i class="placeholder-time"></i><i></i></article>';
}

function librarySkeleton() {
  return `<div class="track-skeleton">${Array.from({ length: 4 }, renderTrackPlaceholder).join("")}</div>`;
}

function loadCover(image) {
  if (!image.isConnected || !image.dataset.src) return;
  image.src = image.dataset.src;
  image.removeAttribute("data-src");
  pendingCovers.delete(image);
  coverObserver.unobserve(image);
}

const coverObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    const image = entry.target;
    if (!entry.isIntersecting) { pendingCovers.delete(image); continue; }
    if ($("library").classList.contains("is-scrolling")) pendingCovers.add(image);
    else loadCover(image);
  }
}, { root: $("library") || null, rootMargin: "320px" });

function finishLibraryScroll() {
  $("library").classList.remove("is-scrolling");
  for (const image of [...pendingCovers]) loadCover(image);
}

function revealLibrary() {
  if (!$("track-list").hidden) $("track-list").classList.add("library-reveal");
}

function trackRowHeight() { return 68; }

function renderTracks(force = false) {
  const list = $("track-list");
  const empty = state.totalTracks === 0 && !state.libraryLoading;
  $("empty-library").hidden = !empty; list.hidden = empty;
  if (empty) {
    const query = $("track-search").value.trim();
    $("empty-eyebrow").textContent = query ? "No matches" : "No tracks here";
    $("empty-title").textContent = query ? `Nothing matches "${query}".` : "Add a channel, bot, or private chat.";
    $("empty-body").textContent = query
      ? "Try fewer words, or search every chat with the search box in the sidebar."
      : "The app will find its audio and keep the playlist in sync.";
    $("empty-add").hidden = Boolean(query);
    $("empty-clear-search").hidden = !query;
    list.replaceChildren(); renderSources(); return;
  }
  if (!state.totalTracks) {
    // Mid-refresh the old rows are still on screen and dimmed; replacing them with a skeleton
    // is the flash we are avoiding.
    if (!$("library").classList.contains("is-refreshing")) list.innerHTML = librarySkeleton();
    return;
  }
  const scroller = $("library");
  const rowHeight = trackRowHeight();
  const firstVisible = Math.max(0, Math.floor((scroller.scrollTop - list.offsetTop) / rowHeight));
  const start = Math.max(0, Math.floor(firstVisible / 40) * 40 - 40);
  const end = Math.min(state.totalTracks, start + 80);
  if (force || state.windowStart !== start) {
    state.windowStart = start;
    const rows = Array.from({ length: end - start }, (_, offset) => state.tracks[start + offset] ? renderTrackRow(state.tracks[start + offset]) : renderTrackPlaceholder()).join("");
    list.innerHTML = `<div class="track-spacer"></div>${rows}<div class="track-spacer"></div>`;
    const spacers = list.querySelectorAll(".track-spacer");
    spacers[0].style.height = `${start * rowHeight}px`;
    spacers[1].style.height = `${Math.max(0, state.totalTracks - end) * rowHeight}px`;
    list.querySelectorAll(".row-art[data-src]").forEach((image) => coverObserver.observe(image));
  }
  for (let offset = Math.floor(start / 100) * 100; offset < end; offset += 100) loadPage(offset);
}

function libraryParameters(offset) {
  const query = $("track-search").value.trim();
  const temporary = Boolean(state.temporarySource?.chatId === state.source && !state.sources.some((item) => item.chatId === state.source));
  return `source=${encodeURIComponent(state.likedMode ? "" : state.source)}&q=${encodeURIComponent(query)}&offset=${offset}&limit=100&liked=${state.likedMode}&temporary=${temporary}`;
}

async function loadPage(offset, force = false, token = libraryRequest) {
  offset = Math.max(0, Math.floor(offset / 100) * 100);
  if (state.loadedPages.has(offset) || state.pageRequests.has(offset)) return;
  state.pageRequests.add(offset);
  const cacheKey = libraryParameters(offset);
  try {
    // The total only changes when the filter changes, and the cache key encodes the filter,
    // so replaying it lets the server skip a COUNT(*) over the whole library on every page.
    const known = offset > 0 && state.totalTracks > 0 ? `&total=${state.totalTracks}` : "";
    const raw = !force && state.libraryCache.get(cacheKey) || await api(`/api/tracks?${cacheKey}${known}`, { signal: requestController.signal });
    if (token !== libraryRequest) return;
    const page = normalizeTrackPage(raw);
    if (!state.libraryCache.has(cacheKey)) cacheSet(state.libraryCache, cacheKey, page, 8);
    state.totalTracks = page.total;
    state.tracks.length = page.total;
    page.items.forEach((track, index) => { state.tracks[page.offset + index] = track; cacheSet(state.summaryCache, track.key, track, 500); });
    state.loadedPages.add(page.offset);
    renderTracks(true);
    renderSources();
  } catch (error) {
    if (error.name !== "AbortError") showError(error, () => loadLibrary(true));
  } finally { state.pageRequests.delete(offset); }
}

async function loadLibrary(force = false, keepVisible = false) {
  requestController?.abort(); requestController = new AbortController();
  const token = ++libraryRequest;
  state.tracks = []; state.loadedPages.clear(); state.pageRequests.clear();
  state.totalTracks = 0; state.windowStart = -1; state.libraryLoading = true;
  // Refining a search should not blank the list. Keep the previous rows on screen, dimmed,
  // until the new page lands; a skeleton flash on every keystroke reads as the app breaking.
  if (keepVisible) $("library").classList.add("is-refreshing");
  else $("track-list").innerHTML = librarySkeleton();
  try {
    if (force) {
      const [sources, stats] = await Promise.all([api("/api/sources", { signal: requestController.signal }), api("/api/library/stats", { signal: requestController.signal })]);
      state.sources = sources; state.likedCount = stats.likedCount || 0;
    }
    await loadPage(0, force, token);
  } finally {
    if (token === libraryRequest) {
      state.libraryLoading = false;
      $("library").classList.remove("is-refreshing");
      renderTracks(true); revealLibrary();
    }
  }
}

async function selectSource(chatId) {
  if (chatId === state.source && !state.likedMode) return;
  state._sourceChangeScroll = true;
  // The temporary entry belongs to the playing track, not to the browsing position, so
  // browsing elsewhere must not drop it. Losing it here meant the pin vanished mid-song and
  // coming back through the title re-previewed the whole channel from scratch.
  if (state.temporarySource && chatId !== state.temporarySource.chatId
      && state.current?.source?.chatId !== state.temporarySource.chatId) {
    if (state.temporaryJob?.jobId) api(`/api/jobs/${encodeURIComponent(state.temporaryJob.jobId)}`, { method: "DELETE" }).catch(() => {});
    state.temporarySource = null; state.temporaryJob = null;
  }
  // A preview that is still running is deliberately left alone. Cancelling it here meant it
  // never finished, so its lastMessageId was never recorded and the next visit rescanned the
  // whole channel. Letting it finish in the background makes the return trip incremental.
  state.source = chatId; state.likedMode = false;
  $("track-search").value = "";
  $("library").scrollTop = 0;
  closeGlobalSearch();
  renderSources();
  $("source-rail").classList.remove("open");
  schedulePersist(); await loadLibrary();
}

async function selectLiked() {
  state.source = ""; state.likedMode = true; $("track-search").value = "";
  $("library").scrollTop = 0; closeGlobalSearch(); renderSources(); schedulePersist();
  await loadLibrary();
}

async function selectTemporary() {
  if (!state.temporarySource) return;
  state._sourceChangeScroll = true;
  state.source = state.temporarySource.chatId; state.likedMode = false;
  $("track-search").value = ""; $("library").scrollTop = 0; renderSources(); schedulePersist();
  await loadLibrary();
  // The preview is incremental once the source has a cursor, so re-entering only scans new
  // messages. Fire it either way, but do not block the list on it.
  state.temporaryJob = await api(`/api/sources/${encodeURIComponent(state.source)}/preview`, { method: "POST" });
  let visible = state.totalTracks;
  watchJob(state.temporaryJob, (job) => { if (job.found > visible && state.source === state.temporarySource?.chatId) { visible = job.found; loadLibrary(); } }, () => state.source === state.temporarySource?.chatId);
}

function closeGlobalSearch({ clear = false } = {}) {
  $("global-results").hidden = true;
  $("global-search").setAttribute("aria-expanded", "false");
  // A debounced search queued before the close would re-open the panel on its own, so drop
  // the pending run as well as the in-flight request.
  clearTimeout(globalSearchTimer);
  globalController?.abort();
  if (clear) {
    $("global-search").value = "";
    state.globalTracks = []; state.globalSources = [];
  }
}

function renderGlobalSearch(message = "") {
  const panel = $("global-results");
  panel.hidden = false; $("global-search").setAttribute("aria-expanded", "true");
  $("global-results-title").textContent = $("global-search").value.trim() || "Search everywhere";
  $("global-source-results").innerHTML = state.globalSources.length
    ? `<h3>Telegram sources</h3>${state.globalSources.map((source) => `<button class="global-result" type="button" data-global-source="${source.chatId}"><span><strong>${escapeHtml(source.title)}</strong><small>${escapeHtml(source.kind)}${source.trackCount ? ` · ${source.trackCount.toLocaleString()} known tracks` : ""}</small></span><span class="global-result-mark">${source.selected ? "Open" : "Preview"}</span></button>`).join("")}`
    : "";
  $("global-track-results").innerHTML = state.globalTracks.length
    ? `<h3>Tracks</h3>${state.globalTracks.map((track) => `<button class="global-result" type="button" data-global-track="${escapeHtml(track.key)}"><span><strong>${escapeHtml(track.title)}</strong><small>${escapeHtml(track.artist)} · ${escapeHtml(track.source.title)}</small></span><span class="global-result-mark">${track.source.selected ? formatTime(track.durationMs / 1000) : "Telegram"}</span></button>`).join("")}`
    : "";
  const empty = $("global-search-empty");
  empty.hidden = Boolean(state.globalTracks.length || state.globalSources.length) && !message;
  empty.textContent = message || "No matches yet.";
}

async function searchEverywhere() {
  const query = $("global-search").value.trim();
  const token = ++globalRequest;
  globalController?.abort(); globalController = new AbortController();
  state.globalTracks = []; state.globalSources = [];
  if (!query) return renderGlobalSearch("Type a title, artist, or source name.");
  if (query.length < 3) return renderGlobalSearch("Type at least three characters to search Telegram.");
  renderGlobalSearch("Searching Telegram…");
  try {
    $("global-search-signal").hidden = false;
    const remote = await api("/api/search/telegram", { method: "POST", signal: globalController.signal, body: JSON.stringify({ query, limit: 30 }) });
    if (token !== globalRequest) return;
    state.globalTracks = Array.isArray(remote?.tracks) ? remote.tracks : [];
    state.globalSources = Array.isArray(remote?.sources) ? remote.sources : [];
    for (const track of state.globalTracks) cacheSet(state.summaryCache, track.key, track, 500);
    renderGlobalSearch();
  } catch (error) {
    if (error.name !== "AbortError") renderGlobalSearch(error.message);
  } finally { if (token === globalRequest) $("global-search-signal").hidden = true; }
}

async function getTrack(key) {
  if (state.trackCache.has(key)) return state.trackCache.get(key);
  const track = await api(`/api/tracks/${encodeURIComponent(key)}`);
  cacheSet(state.trackCache, key, track, 100); return track;
}

function setBuffering(buffering) {
  state.buffering = Boolean(buffering && state.current);
  updateTransport();
}

async function startAudioPlayback() {
  setBuffering(true);
  try { await audio.play(); }
  catch (error) {
    setBuffering(false);
    if (error?.name !== "AbortError") throw error;
  }
}

async function togglePlayback() {
  if (!state.current) return startPlaylist(false);
  if (state.buffering || !audio.paused) { audio.pause(); setBuffering(false); }
  else await startAudioPlayback();
}

async function playKey(key, queue = null, explicitIndex = null) {
  if (state.current?.key === key && (explicitIndex == null || explicitIndex === state.queueIndex)) {
    await togglePlayback();
    return;
  }
  if (queue) { state.queue = queue; state.queueIndex = explicitIndex ?? Math.max(0, queue.indexOf(key)); }
  else if (Number.isInteger(explicitIndex)) state.queueIndex = explicitIndex;
  else if (!state.queue.includes(key)) { state.queue = [key]; state.queueIndex = 0; }
  else state.queueIndex = state.queue.indexOf(key);
  const track = await getTrack(key);
  if (state.current && state.current.key !== key && !state.current.qualified) api("/api/playback/events", { method: "POST", body: JSON.stringify({ key: state.current.key, event: "skipped" }) }).catch(() => {});
  state.current = { ...track, qualified: false };
  if (track.source.selected === false) {
    state.temporarySource = { ...track.source, temporary: true, trackCount: 1 };
    renderSources();
  }
  state.lyricsFollow = true; $("sync-lyrics").hidden = true;
  state.lyric = -1; audio.src = mediaUrl(track, "audio"); setBuffering(true);
  setTrackUi(); renderQueue(); schedulePrefetch();
  api("/api/playback/events", { method: "POST", body: JSON.stringify({ key, event: "started" }) }).catch(() => {});
  loadLyrics();
  schedulePersist();
  try { await startAudioPlayback(); } catch (error) { showError(error); }
}

function setTrackUi() {
  const track = state.current;
  if (!track) { $("progress").disabled = true; return; }
  $("progress").disabled = false;
  const metadata = track.metadata;
  const changed = lastUiTrackKey !== track.key;
  lastUiTrackKey = track.key;
  const title = metadata.title || track.file.name;
  const artist = metadata.artist || "Unknown artist";
  for (const id of ["player-title", "now-title"]) $(id).textContent = title;
  for (const id of ["player-artist", "now-artist"]) $(id).textContent = artist;
  if (changed) $("playback-status").textContent = `Now playing ${title} by ${artist}.`;
  for (const id of ["mini-art", "large-art"]) {
    const image = $(id); const placeholder = $(`${id}-placeholder`);
    image.classList.remove("is-ready"); image.hidden = false;
    placeholder.hidden = false; placeholder.classList.remove("is-covered");
    image.onerror = () => { image.classList.remove("is-ready"); placeholder.classList.remove("is-covered"); };
    image.onload = () => requestAnimationFrame(() => { image.classList.add("is-ready"); placeholder.classList.add("is-covered"); });
    image.src = `${mediaUrl(track)}?v=${encodeURIComponent(metadata.artworkPath || "telegram")}`;
    if (id === "large-art") {
      image.addEventListener("load", () => {
        const highSrc = `${mediaUrl(track, "cover")}?quality=high&v=${encodeURIComponent(metadata.artworkPath || "telegram")}`;
        const highImg = new Image();
        highImg.onload = () => { image.src = highSrc; };
        highImg.src = highSrc;
      }, { once: true });
    }
  }
  $("download-current").href = mediaUrl(track, "download");
  $("like-current").classList.toggle("active", Boolean(track.liked));
  $("like-current").querySelector("use").setAttribute("href", track.liked ? "#i-heart-filled" : "#i-heart");
  $("like-current").setAttribute("aria-pressed", String(Boolean(track.liked)));
  $("like-current").setAttribute("aria-label", track.liked ? "Unlike current track" : "Like current track");
  const detailRows = [["Source", track.source.title], ["Album", metadata.album], ["Album artist", metadata.albumArtist], ["Genre", metadata.genre], ["Year", metadata.year || ""], ["File", track.file.name], ["Size", track.file.size ? `${(track.file.size / 1048576).toFixed(1)} MB` : ""]].filter(([, value]) => value).map(([key, value]) => `<div><dt>${key}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
  document.querySelector(".track-row.current")?.classList.remove("current");
  document.querySelector(`.track-row[data-track-key="${CSS.escape(track.key)}"]`)?.classList.add("current");
  if (changed && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    for (const element of [$("player-locate"), document.querySelector(".now-title")]) {
      element.getAnimations().forEach((animation) => animation.cancel());
      element.animate([{ opacity: 0, transform: "translateY(5px)" }, { opacity: 1, transform: "none" }], { duration: 180, easing: "cubic-bezier(.2,.8,.2,1)" });
    }
  }
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: metadata.title, artist: metadata.artist, album: metadata.album,
      artwork: [{ src: mediaUrl(track, "cover"), sizes: "512x512", type: "image/jpeg" }],
    });
    try { navigator.mediaSession.setPositionState?.({ duration: audio.duration || 0, playbackRate: 1, position: 0 }); } catch {}
  }
}

function updateTransport() {
  const playing = state.buffering || !audio.paused;
  $("play").classList.toggle("playing", playing);
  $("play").setAttribute("aria-busy", String(state.buffering));
  $("play").setAttribute("aria-pressed", String(playing));
  $("play").setAttribute("aria-label", state.buffering ? "Pause while loading" : playing ? "Pause" : "Play");
  const row = state.current && document.querySelector(`.track-row[data-track-key="${CSS.escape(state.current.key)}"]`);
  if (row) {
    row.classList.toggle("buffering", state.buffering);
    row.querySelector(".track-play-overlay").innerHTML = icon(playing ? "pause" : "play");
  }
}

function updateProgress() {
  const duration = audio.duration;
  const played = Number.isFinite(duration) && duration > 0 ? audio.currentTime / duration * 100 : 0;
  $("elapsed").textContent = formatTime(audio.currentTime);
  $("duration").textContent = formatTime(duration);
  $("progress").value = String(played * 10);
  $("progress").style.setProperty("--progress", `${played}%`);
  $("progress").style.setProperty("--buffered", `${Math.max(played, bufferedPercent(audio.buffered, duration))}%`);
}

async function loadLyrics(refresh = false) {
  if (!state.current) return;
  lyricsController?.abort();
  lyricsController = new AbortController();
  const key = state.current.key;
  $("lyrics-lines").innerHTML = '<div class="lyrics-skeleton"><span></span><span></span><span></span><span></span></div>'; $("lyrics-empty").hidden = true;
  try {
    const lyrics = await api(`${mediaUrl(state.current, "lyrics")}${refresh ? "?refresh=true" : ""}`, { signal: lyricsController.signal });
    if (state.current?.key !== key) return;
    state.lyrics = lyrics; renderLyrics();
  } catch (error) {
    if (error.name !== "AbortError") { $("lyrics-empty").hidden = true; showError(error, () => loadLyrics(refresh)); }
  }
}

function renderLyrics() {
  const lines = state.lyrics?.lines || [];
  $("lyrics-lines").innerHTML = lines.length ? lines.map((line, index) => `<button class="lyric-line" type="button" data-lyric="${index}">${escapeHtml(line.text)}</button>`).join("") : (state.lyrics?.plainText || "").split("\n").map((line) => `<p>${escapeHtml(line) || "&nbsp;"}</p>`).join("");
  const empty = !lines.length && !state.lyrics?.plainText;
  $("lyrics-empty").hidden = !empty; $("add-lyrics-empty").hidden = !empty;
  if (empty) $("lyrics-empty").textContent = "No lyrics found for this track.";
}

function updateLyric() {
  const lines = state.lyrics?.lines || [];
  const index = lyricIndex(lines, audio.currentTime * 1000); if (index === state.lyric) return;
  state.lyric = index;
  $("lyrics-lines").querySelectorAll("button").forEach((button, position) => button.classList.toggle("active", position === index));
  if (!state.lyricsFollow) return;
  state.lyricAutoScrolling = true;
  $("lyrics-lines").querySelector(`[data-lyric="${index}"]`)?.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  clearTimeout(state.lyricScrollTimer);
  state.lyricScrollTimer = setTimeout(() => { state.lyricAutoScrolling = false; }, 500);
}

function stopFollowingLyrics() {
  if ($("lyrics-pane").hidden || state.lyricAutoScrolling || !state.lyrics?.lines?.length) return;
  state.lyricsFollow = false; $("sync-lyrics").hidden = false;
}

async function libraryQueue(shuffle, currentKey = "") {
  const result = await api("/api/playback/queue", {
    method: "POST",
    body: JSON.stringify({
      source: state.likedMode ? null : state.source || null,
      query: $("track-search").value.trim(), shuffle, currentKey,
      liked: state.likedMode,
      temporary: Boolean(state.temporarySource?.chatId === state.source),
    }),
  });
  return Array.isArray(result?.keys) ? result.keys.filter((key) => typeof key === "string") : [];
}

async function startPlaylist(shuffle = false) {
  if (!state.totalTracks) return toast("This playlist has no tracks yet");
  const button = shuffle ? $("shuffle-playlist") : $("play-playlist");
  button.disabled = true; button.setAttribute("aria-busy", "true");
  try {
    const keys = await libraryQueue(shuffle);
    if (!keys.length) return toast("This playlist has no tracks yet");
    state.shuffle = shuffle; updateModes(); state.queue = keys; state.queueIndex = 0;
    await playKey(keys[0]);
  } finally { button.disabled = false; button.removeAttribute("aria-busy"); }
}

async function toggleShuffle() {
  const enabled = !state.shuffle;
  const keys = await libraryQueue(enabled, enabled ? state.current?.key || "" : "");
  state.shuffle = enabled; updateModes();
  state.queue = enabled && state.current ? [state.current.key, ...keys] : keys;
  if (state.current && !state.queue.includes(state.current.key)) state.queue.unshift(state.current.key);
  state.queueIndex = state.current ? state.queue.indexOf(state.current.key) : -1;
  renderQueue(); schedulePrefetch();
}

function updateModes() {
  localStorage.setItem("tm-shuffle", state.shuffle ? "1" : "0"); localStorage.setItem("tm-repeat", state.repeat);
  $("shuffle")?.classList.toggle("active", state.shuffle); $("shuffle")?.setAttribute("aria-pressed", String(state.shuffle));
  if ($("shuffle")) $("shuffle").setAttribute("aria-label", `Shuffle ${state.shuffle ? "on" : "off"}`);
  $("repeat")?.classList.toggle("active", state.repeat !== "off");
  if ($("repeat-badge")) $("repeat-badge").textContent = state.repeat === "one" ? "1" : "";
  if ($("repeat")) $("repeat").setAttribute("aria-label", `Repeat ${state.repeat}`);
}

async function move(direction, ended = false) {
  if (!state.queue.length) return;
  if (ended && state.repeat === "one") { audio.currentTime = 0; return startAudioPlayback(); }
  let next = state.queueIndex + direction;
  if (next >= state.queue.length) {
    if (state.repeat !== "all") return;
    if (state.shuffle) {
      state.queue = await libraryQueue(true, state.current?.key || ""); next = 0;
    } else next = 0;
  }
  if (next < 0) next = state.repeat === "all" ? state.queue.length - 1 : 0;
  state.queueIndex = next; await playKey(state.queue[next]);
}

async function ensureSummaries(keys) {
  const missing = [...new Set(keys)].filter((key) => !state.summaryCache.has(key) && !state.summaryRequests.has(key)).slice(0, 100);
  if (!missing.length) return;
  missing.forEach((key) => state.summaryRequests.add(key));
  try {
    const result = await api("/api/tracks/summaries", { method: "POST", body: JSON.stringify({ keys: missing }) });
    for (const track of result?.items || []) cacheSet(state.summaryCache, track.key, track, 500);
    if (!$("queue-pane").hidden) renderQueue();
  } catch (error) { showError(error); }
  finally { missing.forEach((key) => state.summaryRequests.delete(key)); }
}

function renderQueue() {
  const historyStart = Math.max(0, state.queueIndex - state.historyVisible);
  const visibleStart = historyStart;
  const visibleEnd = Math.min(state.queue.length, state.queueIndex + 101);
  const upcoming = state.queue.slice(state.queueIndex + 1);
  $("queue-summary").textContent = upcoming.length ? `${upcoming.length.toLocaleString()} upcoming` : "Your queue is empty";
  if ($("queue-pane").hidden) return;
  const visible = state.queue.slice(visibleStart, visibleEnd);
  const rows = visible.map((key, offset) => {
    const summary = state.summaryCache.get(key); const detail = state.trackCache.get(key); const index = visibleStart + offset;
    const title = summary?.title || detail?.metadata?.title || "Loading track…";
    const artist = summary?.artist || detail?.metadata?.artist || "";
    const section = index < state.queueIndex ? "Played" : index === state.queueIndex ? "Playing" : "Up next";
    return `<div class="queue-row ${index < state.queueIndex ? "played" : ""} ${index === state.queueIndex ? "current" : ""}" draggable="${index > state.queueIndex}" data-queue-index="${index}" data-queue-key="${escapeHtml(key)}"><button class="queue-copy" type="button" data-queue-play="${index}"><span class="queue-state">${section}</span><strong>${escapeHtml(title)}</strong><small>${escapeHtml(artist)}</small></button><span>${index > state.queueIndex ? `<span class="cache-state ${state.cacheStates[key] || ""}">${escapeHtml(state.cacheStates[key] || "queued")}</span><button class="icon-button" type="button" data-remove-queue="${index}" aria-label="Remove from queue">${icon("close")}</button>` : ""}</span></div>`;
  }).join("");
  $("queue-list").innerHTML = rows + (upcoming.length ? "" : '<div class="queue-empty"><strong>Your queue is clear.</strong><span>Choose a track or add one from its more menu.</span><button class="button" type="button" data-queue-browse>Browse library</button></div>');
  ensureSummaries(visible);
}

async function schedulePrefetch() {
  if (!state.queue.length) return;
  const count = Math.max(0, Math.min(Number(state.settings.prefetchCount) || 0, 20));
  const keys = state.queue.slice(state.queueIndex + 1, state.queueIndex + 1 + count);
  if (!keys.length) return;
  keys.forEach((key) => { state.cacheStates[key] = "queued"; }); renderQueue();
  try { const job = await api("/api/playback/prefetch", { method: "POST", body: JSON.stringify({ keys }) }); watchJob(job, (current) => {
    state.cacheStates = { ...state.cacheStates, ...(current.result || {}) };
    if (!$("queue-pane").hidden) for (const [key, value] of Object.entries(current.result || {})) {
      const badge = document.querySelector(`.queue-row[data-queue-key="${CSS.escape(key)}"] .cache-state`);
      if (badge) { badge.className = `cache-state ${value}`; badge.textContent = value; }
    }
  }); }
  catch (error) { showError(error, schedulePrefetch); }
}

function watchJob(job, onUpdate = () => {}, relevant = () => true) {
  if (!job?.jobId) return;
  let syncAnnouncement = "";
  const poll = async () => {
    try {
      if (!relevant()) return;
      if (document.hidden) return setTimeout(poll, 2000);
      const current = await api(`/api/jobs/${encodeURIComponent(job.jobId)}`); onUpdate(current);
      if (["sync", "preview"].includes(current.kind)) {
        const strip = $("sync-strip");
        const active = ["queued", "running"].includes(current.state);
        strip.hidden = false;
        strip.classList.toggle("is-complete", current.state === "complete");
        $("sync-copy").textContent = current.state === "queued" ? "Waiting to sync…" : current.state === "complete" ? "Sync complete" : `${current.found.toLocaleString()} tracks indexed · ${current.processed.toLocaleString()} files checked`;
        const announcement = active ? "Sync started." : current.state === "complete" ? "Sync complete." : current.error ? `Sync failed. ${current.error}` : "";
        if (announcement && announcement !== syncAnnouncement) {
          syncAnnouncement = announcement;
          $("sync-status").textContent = announcement;
        }
        if (!active) setTimeout(() => { strip.hidden = true; strip.classList.remove("is-complete"); }, current.state === "complete" && !matchMedia("(prefers-reduced-motion: reduce)").matches ? 280 : 0);
      }
      if (["queued", "running"].includes(current.state)) setTimeout(poll, current.processed ? 1800 : 1000);
      else if (["sync", "preview"].includes(current.kind)) { state.libraryCache.clear(); await loadLibrary(true); if (current.state === "complete") toast(current.kind === "preview" ? "Temporary source indexed" : "Source is up to date"); else if (current.error) showError(new AppError(current.error, true), () => current.kind === "preview" ? selectTemporary() : syncSource(current.chatId, current.mode === "full")); }
    } catch (error) { showError(error); }
  };
  poll();
}

async function syncSource(chatId, full = false) {
  try {
    const job = await api(`/api/sources/${encodeURIComponent(chatId)}/sync`, { method: "POST", body: JSON.stringify({ full }) });
    watchJob(job);
  } catch (error) { showError(error, () => syncSource(chatId, full)); }
}

async function syncAllSources() {
  try {
    await api("/api/sources/sync-all", { method: "POST" });
    toast("Syncing all sources");
  } catch (error) { showError(error, syncAllSources); }
}

async function openSources() {
  $("source-dialog").showModal(); $("discover-list").innerHTML = '<div class="list-skeleton"><span></span><span></span><span></span></div>';
  try {
    state.discovered = await api("/api/sources/discover"); renderDiscovered();
    const job = await api("/api/sources/discover/counts", { method: "POST" });
    watchJob(job, (current) => {
      $("discover-progress").textContent = current.state === "complete" ? "Counts ready" : `Counting ${current.processed}/${state.discovered.length}`;
      for (const item of state.discovered) if (current.result?.[item.chatId] != null) item.musicFileCount = current.result[item.chatId];
      renderDiscovered();
    }, () => $("source-dialog").open);
  } catch (error) { showError(error, openSources); }
}

function renderDiscovered() {
  const order = ["selected", "channel", "bot", "private", "saved"];
  const labels = { selected: "Selected", channel: "Channels", bot: "Bots", private: "Private chats", saved: "Saved messages" };
  const groups = new Map(order.map((key) => [key, []]));
  const mode = $("discover-sort").value;
  const compare = (a, b) => mode === "count" ? (b.musicFileCount ?? b.trackCount ?? -1) - (a.musicFileCount ?? a.trackCount ?? -1) : mode === "name" ? a.title.localeCompare(b.title) : (b.lastPostAt || 0) - (a.lastPostAt || 0);
  for (const item of state.discovered) groups.get(item.selected ? "selected" : item.kind)?.push(item);
  $("discover-list").innerHTML = order.map((group) => {
    const items = groups.get(group).sort(compare); if (!items.length) return "";
    return `<section class="discover-group"><h3>${labels[group]}</h3>${items.map((item) => `<label class="discover-row ${item.pending ? "pending" : ""}"><img class="source-avatar" src="${item.avatarUrl}" data-avatar-fallback="${escapeHtml(initials(item.title))}" alt="" loading="lazy"><span class="discover-copy"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.kind)} · ${item.musicFileCount ?? item.trackCount ?? "…"} music files</small></span><input type="checkbox" data-chat="${item.chatId}" ${item.selected ? "checked" : ""} aria-label="Select ${escapeHtml(item.title)}"></label>`).join("")}</section>`;
  }).join("") || '<p class="small-copy">No supported chats found.</p>';
}

async function toggleSource(input) {
  const item = state.discovered.find((value) => value.chatId === input.dataset.chat); if (!item) return;
  const selected = input.checked; item.selected = selected; item.pending = true; renderDiscovered();
  try {
    const result = selected ? await api("/api/sources", { method: "POST", body: JSON.stringify({ chatId: item.chatId }) }) : await api(`/api/sources/${encodeURIComponent(item.chatId)}`, { method: "PATCH", body: JSON.stringify({ selected: false }) });
    item.pending = false; if (result.job) watchJob(result.job); state.libraryCache.clear(); await loadLibrary(true); renderDiscovered();
  } catch (error) { item.selected = !selected; item.pending = false; renderDiscovered(); showError(error, () => toggleSource(input)); }
}

async function pinSource(chatId) {
  const source = state.sources.find((item) => item.chatId === chatId);
  if (!source) return;
  const pinned = !source.pinnedAt;
  // Reorder straight away; the list order is derived from pinnedAt and the request is small.
  const previous = source.pinnedAt;
  source.pinnedAt = pinned ? Math.floor(Date.now() / 1000) : null;
  renderSources();
  try {
    await api(`/api/sources/${encodeURIComponent(chatId)}/pin`, {
      method: "PATCH", body: JSON.stringify({ pinned }),
    });
  } catch (error) {
    source.pinnedAt = previous; renderSources();
    showError(error, () => pinSource(chatId));
  }
}

async function keepTemporarySource() {
  const chatId = state.temporarySource?.chatId;
  if (!chatId || state.keepingSource) return;
  state.keepingSource = true; renderSources();
  try {
    // The row already exists in the database, so selecting it keeps every track the preview
    // already stored instead of starting over.
    const result = await api(`/api/sources/${encodeURIComponent(chatId)}`, {
      method: "PATCH", body: JSON.stringify({ selected: true }),
    });
    if (state.temporaryJob?.jobId && result.job?.jobId !== state.temporaryJob.jobId) {
      api(`/api/jobs/${encodeURIComponent(state.temporaryJob.jobId)}`, { method: "DELETE" }).catch(() => {});
    }
    state.temporarySource = null; state.temporaryJob = null;
    state.libraryCache.clear();
    state.source = chatId; state.likedMode = false;
    // force refetches the source list too, so the pin is replaced by the real entry.
    await loadLibrary(true);
    if (result.job) watchJob(result.job, () => loadLibrary(true), () => state.source === chatId);
    schedulePersist();
    toast("Added to your library");
  } catch (error) {
    showError(error, keepTemporarySource);
  } finally {
    state.keepingSource = false; renderSources();
  }
}

async function unselectSources(ids) {
  if (!ids.length || !await confirmAction("Unselect sources?", "They’ll disappear from this player and stop syncing. Nothing will be deleted or left in Telegram.", "Unselect")) return;
  try {
    await api("/api/sources/bulk-select", { method: "POST", body: JSON.stringify({ chatIds: ids, selected: false }) });
    state.selectedSources.clear(); state.bulk = false; $("bulk-bar").hidden = true; state.libraryCache.clear();
    if (ids.includes(state.source)) state.source = "";
    await loadLibrary(true); toast(ids.length === 1 ? "Source unselected" : `${ids.length} sources unselected`);
  } catch (error) { showError(error, () => unselectSources(ids)); }
}

function openMetadata(track = state.current) {
  if (!track) return; state.editing = track;
  for (const element of $("metadata-form").elements) if (element.name) element.value = track.metadata[element.name] || "";
  $("cover-quality").value = state.settings.coverQuality || "1200"; $("candidate-section").hidden = true; $("metadata-status").textContent = "";
  if (!$("metadata-dialog").open) $("metadata-dialog").showModal();
}

async function saveMetadata(event) {
  event.preventDefault(); const values = Object.fromEntries(new FormData(event.currentTarget));
  for (const field of ["year", "trackNumber", "discNumber"]) values[field] = Number(values[field]) || 0;
  try {
    const updated = await api(mediaUrl(state.editing, "metadata"), { method: "PATCH", body: JSON.stringify({ set: values, clear: [] }) });
    state.editing = updated; cacheSet(state.trackCache, updated.key, updated, 100); if (state.current?.key === updated.key) { state.current = { ...state.current, ...updated }; setTrackUi(); }
    state.libraryCache.clear(); await loadLibrary(true); $("metadata-status").textContent = "Saved locally. Downloads will use these tags.";
  } catch (error) { $("metadata-status").textContent = error.message; }
}

async function resetMetadata() {
  if (!state.editing || !await confirmAction("Reset local metadata?", "Telegram’s original metadata will become visible again.", "Reset")) return;
  try { const updated = await api(mediaUrl(state.editing, "metadata"), { method: "PATCH", body: JSON.stringify({ set: {}, clear: Object.keys(state.editing.overrides) }) }); state.editing = updated; openMetadata(updated); state.libraryCache.clear(); await loadLibrary(true); }
  catch (error) { $("metadata-status").textContent = error.message; }
}

async function fetchMetadata() {
  const button = $("fetch-metadata"); button.disabled = true; button.setAttribute("aria-busy", "true");
  $("metadata-status").textContent = "Searching MusicBrainz…";
  $("candidate-section").hidden = false; $("candidate-list").innerHTML = '<div class="list-skeleton"><span></span><span></span></div>';
  try {
    const candidates = await api(`${mediaUrl(state.editing, "metadata")}/search`, { method: "POST", body: "{}" });
    $("candidate-list").innerHTML = candidates.map((item) => `<article class="candidate-row">${item.coverUrl ? `<img class="candidate-cover" src="${mediaUrl(state.editing, `metadata/candidates/${encodeURIComponent(item.id)}/cover`)}" alt="" loading="lazy">` : '<div class="candidate-cover art-placeholder"><span></span></div>'}<div class="candidate-copy"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.artist)} · ${escapeHtml(item.album || "Single")} ${item.year || ""}</span></div><span class="utility">${item.score}%</span><button class="button" type="button" data-candidate="${escapeHtml(item.id)}">Use match</button></article>`).join("") || '<p class="small-copy">No close matches found.</p>';
    $("metadata-status").textContent = candidates.length ? `${candidates.length} matches found.` : "No close matches found.";
  } catch (error) { $("metadata-status").textContent = error.message; }
  finally { button.disabled = false; button.removeAttribute("aria-busy"); }
}

async function applyCandidate(id) {
  try {
    const updated = await api(`${mediaUrl(state.editing, "metadata")}/apply`, { method: "POST", body: JSON.stringify({ candidateId: id, coverQuality: $("cover-quality").value }) });
    state.editing = updated; cacheSet(state.trackCache, updated.key, updated, 100); if (state.current?.key === updated.key) { state.current = { ...state.current, ...updated }; setTrackUi(); }
    openMetadata(updated); state.libraryCache.clear(); await loadLibrary(true); $("metadata-status").textContent = "Internet metadata applied locally.";
  } catch (error) { $("metadata-status").textContent = error.message; }
}

function openLyricsEditor() { if (!state.current) return; $("lyrics-text").value = state.lyrics?.syncedText || state.lyrics?.plainText || ""; $("lyrics-status").textContent = ""; if (!$("lyrics-dialog").open) $("lyrics-dialog").showModal(); }
async function saveLyrics(event) { event.preventDefault(); try { state.lyrics = await api(mediaUrl(state.current, "lyrics"), { method: "PUT", body: JSON.stringify({ text: $("lyrics-text").value }) }); renderLyrics(); $("lyrics-status").textContent = "Lyrics saved."; } catch (error) { $("lyrics-status").textContent = error.message; } }

function showPanel(tab = "lyrics", toggle = false) {
  if (toggle && !$("now-panel").hidden && document.querySelector(`#${tab}-tab.active`)) return closePanel();
  $("now-panel").hidden = false; $("app-shell").classList.add("panel-open");
  for (const name of ["lyrics", "queue", "details"]) { const active = name === tab; $(`${name}-pane`).hidden = !active; $(`${name}-tab`).classList.toggle("active", active); $(`${name}-tab`).setAttribute("aria-selected", String(active)); }
  const pane = $(`${tab}-pane`); pane.classList.remove("pane-entering"); requestAnimationFrame(() => pane.classList.add("pane-entering"));
  if (tab === "queue") renderQueue();
  schedulePersist();
}
function closePanel() { $("now-panel").hidden = true; $("app-shell").classList.remove("panel-open"); schedulePersist(); }

function openMenu(actions, x, y) {
  const menu = $("context-menu"); menu.innerHTML = actions.map((item, index) => `<button class="${item.danger ? "danger" : ""}" type="button" role="menuitem" data-menu-index="${index}">${escapeHtml(item.label)}</button>`).join("");
  menu.hidden = false; menu._actions = actions;
  menu.style.left = `${Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8))}px`; menu.style.top = `${Math.max(8, Math.min(y, innerHeight - menu.offsetHeight - 8))}px`; menu.querySelector("button")?.focus();
}
function closeMenu() { $("context-menu").hidden = true; }

async function playFromLibrary(key) {
  const keys = await libraryQueue(false);
  if (!keys.includes(key)) keys.unshift(key);
  state.shuffle = false; updateModes();
  return playKey(key, keys);
}

async function selectCachedSource(chatId) {
  try {
    const result = await api(`/api/sources/${encodeURIComponent(chatId)}`, { method: "PATCH", body: JSON.stringify({ selected: true }) });
    if (result.job) watchJob(result.job);
    state.libraryCache.clear(); await loadLibrary(true); toast("Source added");
  } catch (error) { showError(error, () => selectCachedSource(chatId)); }
}

function trackMenu(key, x, y) {
  const summary = state.summaryCache.get(key)
    || state.globalTracks.find((track) => track.key === key);
  const detail = state.trackCache.get(key);
  const source = summary?.source || detail?.source;
  if (!source) return;
  openMenu([
    { label: "Play", action: () => source.selected === false ? playKey(key, state.globalTracks.map((item) => item.key)) : playFromLibrary(key) },
    { label: "Play next", action: () => { const at = Math.max(state.queueIndex + 1, 0); state.queue.splice(at, 0, key); renderQueue(); schedulePrefetch(); toast("Playing next"); } },
    { label: "Add to queue", action: () => { state.queue.push(key); renderQueue(); schedulePrefetch(); toast("Added to queue"); } },
    { label: "Download", action: () => { location.href = mediaUrl({ key }, "download"); } },
    { label: "Edit metadata", action: async () => openMetadata(await getTrack(key)) },
    { label: "Edit lyrics", action: async () => { if (state.current?.key !== key) await playKey(key); openLyricsEditor(); } },
    source.selected === false
      ? { label: "Add source", action: () => selectCachedSource(source.chatId) }
      : { label: "Show source", action: () => selectSource(source.chatId) },
  ], x, y);
}

function sourceMenu(chatId, x, y) {
  const source = state.sources.find((item) => item.chatId === chatId); if (!source) return;
  openMenu([
    { label: "Open", action: () => selectSource(chatId) },
    { label: "Sync new tracks", action: () => syncSource(chatId, false) },
    { label: "Full rescan", action: () => syncSource(chatId, true) },
    { label: "Unselect source", danger: true, action: () => unselectSources([chatId]) },
  ], x, y);
}

async function toggleRowLike(key, button) {
  const track = state.tracks.find((entry) => entry?.key === key);
  if (!track) return;
  const previous = Boolean(track.liked);
  const next = !previous;
  track.liked = next;
  state.likedCount += next ? 1 : -1;
  const summary = state.summaryCache.get(key);
  if (summary) summary.liked = next;
  button.classList.toggle("active", next);
  button.setAttribute("aria-pressed", String(next));
  button.querySelector("use").setAttribute("href", next ? "#i-heart-filled" : "#i-heart");
  renderSources();
  try {
    await api(`/api/tracks/${encodeURIComponent(key)}/like`, { method: "PATCH", body: JSON.stringify({ liked: next }) });
    if (state.likedMode) loadLibrary(true);
  } catch (error) {
    track.liked = previous;
    state.likedCount += previous ? 1 : -1;
    if (summary) summary.liked = previous;
    button.classList.toggle("active", previous);
    button.setAttribute("aria-pressed", String(previous));
    button.querySelector("use").setAttribute("href", previous ? "#i-heart-filled" : "#i-heart");
    renderSources();
    showError(error);
  }
}

async function toggleLike() {
  if (!state.current) return;
  const previous = state.current.liked;
  state.current.liked = !previous;
  state.likedCount += state.current.liked ? 1 : -1;
  const summary = state.summaryCache.get(state.current.key);
  if (summary) summary.liked = state.current.liked;
  setTrackUi(); renderSources();
  try {
    const updated = await api(`${mediaUrl(state.current, "like")}`, { method: "PATCH", body: JSON.stringify({ liked: state.current.liked }) });
    state.current.liked = updated.liked;
    toast(updated.liked ? "Added to Liked Songs" : "Removed from Liked Songs");
    schedulePersist();
    if (state.likedMode) loadLibrary(true);
  } catch (error) {
    state.current.liked = previous;
    state.likedCount += previous ? 1 : -1;
    if (summary) summary.liked = previous;
    setTrackUi(); renderSources();
    showError(error);
  }
}

async function saveCurrentToTelegram() {
  if (!state.current) return;
  const button = $("save-current-telegram"); button.disabled = true; button.setAttribute("aria-busy", "true");
  try { await api(`${mediaUrl(state.current, "saved-messages")}`, { method: "POST" }); toast("Sent to Saved Messages"); }
  catch (error) { showError(error, saveCurrentToTelegram); }
  finally { button.disabled = false; button.removeAttribute("aria-busy"); }
}

function renderContacts() {
  const query = $("contact-search").value.trim().toLocaleLowerCase();
  const contacts = state.contacts.filter((contact) => `${contact.name} ${contact.username || ""}`.toLocaleLowerCase().includes(query));
  $("share-status").textContent = `${contacts.length} matching ${contacts.length === 1 ? "contact" : "contacts"}`;
  $("contact-list").innerHTML = contacts.map((contact) => `<button class="contact-row" type="button" data-contact="${contact.id}"><img class="source-avatar" src="${contact.avatarUrl}" data-avatar-fallback="${escapeHtml(initials(contact.name))}" alt="" loading="lazy"><span><strong>${escapeHtml(contact.name)}</strong><small>${contact.username ? `@${escapeHtml(contact.username)}` : "Telegram contact"}</small></span></button>`).join("") || '<p class="empty-copy">No matching contacts.</p>';
}

async function openShare() {
  if (!state.current) return;
  $("share-dialog").showModal(); $("contact-search").value = "";
  $("share-status").textContent = "Loading contacts…";
  $("contact-list").innerHTML = '<div class="list-skeleton"><span></span><span></span></div>';
  try { state.contacts = await api("/api/telegram/contacts"); renderContacts(); }
  catch (error) { $("share-dialog").close(); showError(error, openShare); }
}

function queueShare(recipientId) {
  const key = state.current?.key; if (!key) return;
  $("share-dialog").close();
  if (pendingShare) clearTimeout(pendingShare.timer);
  const pending = { cancelled: false };
  pending.timer = setTimeout(async () => {
    if (pending.cancelled) return;
    pendingShare = null;
    try { await api(`/api/tracks/${encodeURIComponent(key)}/share`, { method: "POST", body: JSON.stringify({ recipientId }) }); toast("Shared on Telegram"); }
    catch (error) { showError(error); }
  }, 5000);
  pendingShare = pending;
  toast("Sharing in 5 seconds…", { label: "Undo", run: () => { pending.cancelled = true; clearTimeout(pending.timer); pendingShare = null; toast("Share cancelled"); } }, 5000);
}

async function locateCurrent() {
  if (!state.current) return;
  const button = $("player-locate"); button.disabled = true; button.setAttribute("aria-busy", "true");
  try {
    const source = state.current.source;
    if (source.selected === false) {
      state.temporarySource = { ...source, temporary: true, trackCount: 1 };
      await selectTemporary();
    } else if (state.source !== source.chatId || state.likedMode) await selectSource(source.chatId);
    else {
      // A previous locate may have left a temporary source pointing elsewhere; keep it only
      // while it is still the chat being viewed, or the position lookup disagrees with the list.
      if (state.temporarySource && state.temporarySource.chatId !== source.chatId) {
        if (state.temporaryJob?.jobId) api(`/api/jobs/${encodeURIComponent(state.temporaryJob.jobId)}`, { method: "DELETE" }).catch(() => {});
        state.temporarySource = null; state.temporaryJob = null;
      }
      $("track-search").value = ""; $("library").scrollTop = 0; await loadLibrary();
    }
    const temporary = Boolean(state.temporarySource?.chatId === state.source && !state.sources.some((item) => item.chatId === state.source));
    const result = await api(`/api/tracks/${encodeURIComponent(state.current.key)}/position?source=${encodeURIComponent(state.source)}&temporary=${temporary}`);
    await loadPage(Math.floor(result.index / 100) * 100);
    state.windowStart = -1; renderTracks(true);
    requestAnimationFrame(() => {
      const row = document.querySelector(`.track-row[data-track-key="${CSS.escape(state.current.key)}"]`);
      if (row) row.scrollIntoView({ block: 'center' });
      row?.focus();
    });
  } catch (error) { showError(error, locateCurrent); }
  finally { button.disabled = false; button.removeAttribute("aria-busy"); }
}

function installResizer(id, side) {
  const handle = $(id);
  const resize = (clientX) => {
    const value = side === "left" ? clientX : innerWidth - clientX;
    const bounded = Math.max(side === "left" ? 220 : 300, Math.min(side === "left" ? 420 : 640, value));
    document.documentElement.style.setProperty(side === "left" ? "--rail-width" : "--panel-width", `${bounded}px`);
    localStorage.setItem(side === "left" ? "tm-rail-width" : "tm-panel-width", bounded);
  };
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault(); handle.setPointerCapture(event.pointerId); document.body.classList.add("resizing");
    const move = (current) => resize(current.clientX);
    const stop = () => { handle.removeEventListener("pointermove", move); document.body.classList.remove("resizing"); };
    handle.addEventListener("pointermove", move); handle.addEventListener("pointerup", stop, { once: true }); handle.addEventListener("pointercancel", stop, { once: true });
  });
  handle.addEventListener("keydown", (event) => { if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return; const current = parseInt(getComputedStyle(document.documentElement).getPropertyValue(side === "left" ? "--rail-width" : "--panel-width")); resize(side === "left" ? current + (event.key === "ArrowRight" ? 10 : -10) : innerWidth - current + (event.key === "ArrowLeft" ? 10 : -10)); });
}

async function openSettings() {
  $("settings-dialog").showModal();
  try {
    state.settings = await api("/api/settings"); $("prefetch-count").value = state.settings.prefetchCount; $("musicbrainz-contact").value = state.settings.musicbrainzContact; $("default-cover-quality").value = state.settings.coverQuality;
    const cache = await api("/api/cache/status"); $("cache-usage").textContent = `${cache.files} cached · ${(cache.bytes / 1048576).toFixed(1)} MB`;
    const [network, auth] = await Promise.all([api("/api/network"), api("/api/auth/status")]);
    state.network = network;
    state.passwordEnabled = auth.passwordEnabled;
    renderNetwork();
    renderPasswordState();
    showPasswordForm(null);
  } catch (error) { showError(error, openSettings); }
}

function renderNetwork() {
  const { bindHost, activeHost, managed, inDocker } = state.network || {};
  for (const button of document.querySelectorAll("[data-bind-host] [data-value]")) {
    const active = button.dataset.value === bindHost;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  $("bind-host-help").textContent = bindHost === "0.0.0.0"
    ? "Anyone on your network can open this player. Set a password below."
    : "Loopback keeps the player reachable only from this computer.";

  const notice = $("bind-restart-notice");
  if (inDocker) {
    notice.hidden = false;
    notice.textContent = bindHost === "0.0.0.0"
      ? "Docker: publish the port as \"8000:8000\" in compose.yaml, then run docker compose up -d."
      : "Docker: publish the port as \"127.0.0.1:8000:8000\" in compose.yaml, then run docker compose up -d.";
  } else if (activeHost && activeHost !== bindHost) {
    notice.hidden = false;
    notice.textContent = `Saved. Currently still serving on ${activeHost} \u2014 restart to apply.`;
  } else if (!managed) {
    notice.hidden = false;
    notice.textContent = "Started without run.py, so this setting is not applied. Restart with: uv run python run.py";
  } else {
    notice.hidden = true;
  }
}

function renderPasswordState() {
  const enabled = Boolean(state.passwordEnabled);
  $("password-state-label").textContent = enabled
    ? "Password is on. This player asks for it before loading."
    : "No password. Anyone who can reach this player can use it.";
  const toggle = $("password-toggle");
  toggle.hidden = false;
  toggle.textContent = enabled ? "Change password" : "Set a password";
  $("password-remove").hidden = !enabled;
  $("sign-out").hidden = !enabled;
}

function showPasswordForm(which) {
  $("password-status").textContent = "";
  $("password-form").hidden = which !== "set";
  $("password-disable-form").hidden = which !== "disable";
  $("password-current-block").hidden = !state.passwordEnabled;
  $("password-toggle").hidden = which !== null;
  $("password-remove").hidden = which !== null || !state.passwordEnabled;
  if (which === "set") ($("password-current-block").hidden ? $("password-new") : $("password-current")).focus();
  if (which === "disable") $("password-disable-current").focus();
}

async function setBindHost(value) {
  if (!state.network || state.network.bindHost === value) return;
  const previous = state.network.bindHost;
  try {
    state.network = { ...state.network, ...await api("/api/network", { method: "PATCH", body: JSON.stringify({ bindHost: value }) }) };
    renderNetwork();
    toast(value === "0.0.0.0" ? "Now reachable on your network after restart" : "Now this machine only after restart");
  } catch (error) {
    state.network = { ...state.network, bindHost: previous };
    renderNetwork();
    showError(error);
  }
}

async function saveSettings(button) {
  const pane = button.closest("[data-settings-pane]").dataset.settingsPane;
  const values = pane === "playback" ? { prefetchCount: Number($("prefetch-count").value) } : { musicbrainzContact: $("musicbrainz-contact").value.trim(), coverQuality: $("default-cover-quality").value };
  const label = button.dataset.label ||= button.textContent;
  try {
    button.textContent = label; button.setAttribute("aria-busy", "true");
    state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify(values) });
    button.textContent = "Saved"; button.classList.add("saved"); clearTimeout(button._savedTimer); button._savedTimer = setTimeout(() => { button.textContent = label; button.classList.remove("saved"); }, 2000);
    toast("Settings saved");
  }
  catch (error) { showError(error, () => saveSettings(button)); } finally { button.removeAttribute("aria-busy"); }
}

$("bind-host-options").addEventListener("click", (event) => {
  const button = event.target.closest("[data-value]");
  if (button) setBindHost(button.dataset.value);
});

$("password-toggle").addEventListener("click", () => showPasswordForm("set"));
$("password-remove").addEventListener("click", () => showPasswordForm("disable"));
$("password-cancel").addEventListener("click", () => showPasswordForm(null));
$("password-disable-cancel").addEventListener("click", () => showPasswordForm(null));

$("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = $("password-status");
  const next = $("password-new").value;
  if (next.length < 8) return void (status.textContent = "Use at least 8 characters.");
  if (next !== $("password-confirm").value) return void (status.textContent = "Those passwords do not match.");
  status.textContent = "Saving\u2026";
  try {
    const result = await api("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({ current: $("password-current").value, password: next }),
    });
    state.passwordEnabled = result.passwordEnabled;
    for (const id of ["password-current", "password-new", "password-confirm"]) $(id).value = "";
    renderPasswordState();
    showPasswordForm(null);
    toast("Password saved");
  } catch (error) { status.textContent = error.message; }
});

$("password-disable-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = $("password-status");
  status.textContent = "Removing\u2026";
  try {
    await api("/api/auth/password/disable", {
      method: "POST",
      body: JSON.stringify({ current: $("password-disable-current").value }),
    });
    state.passwordEnabled = false;
    $("password-disable-current").value = "";
    renderPasswordState();
    showPasswordForm(null);
    toast("Password removed");
  } catch (error) { status.textContent = error.message; }
});

$("sign-out").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
    $("settings-dialog").close();
    showView("lock-view");
    $("lock-status").textContent = "";
    $("lock-password").focus();
  } catch (error) { showError(error); }
});

$("lock-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const field = $("lock-password");
  const status = $("lock-status");
  status.textContent = "Checking\u2026";
  try {
    await api("/api/auth/login", { method: "POST", body: JSON.stringify({ password: field.value }) });
    field.value = "";
    status.textContent = "";
    await boot();
  } catch (error) {
    status.textContent = error.message;
    field.select();
  }
});

function restartPhoneLogin() { state.flow = ""; $("telegram-code").value = ""; $("telegram-password").value = ""; setLoginStage("phone", "Choose your country and send a new code."); }
$("change-number").addEventListener("click", restartPhoneLogin);
$("change-number-2fa").addEventListener("click", restartPhoneLogin);

document.querySelector('[data-source=""]').addEventListener("click", () => selectSource(""));
$("source-list").addEventListener("click", (event) => { const sync = event.target.closest("[data-sync-source]"); if (sync) return syncSource(sync.dataset.syncSource, sync.dataset.full === "true"); const pin = event.target.closest("[data-pin-source]"); if (pin) return pinSource(pin.dataset.pinSource); const checkbox = event.target.closest("[data-bulk-source]"); if (checkbox) { checkbox.checked ? state.selectedSources.add(checkbox.dataset.bulkSource) : state.selectedSources.delete(checkbox.dataset.bulkSource); return renderSources(); } const temporary = event.target.closest("[data-temporary-source]"); if (temporary && !state.bulk) return selectTemporary(); const row = event.target.closest("[data-source]"); if (row && !state.bulk) selectSource(row.dataset.source); });
// The row is a keyboard target, but the buttons inside it are too. Let a focused button
// handle its own Enter/Space instead of navigating to the source behind it.
$("source-list").addEventListener("keydown", (event) => { if (event.target.closest("button")) return; const row = event.target.closest("[data-source]"); if (row && ["Enter", " "].includes(event.key)) { event.preventDefault(); selectSource(row.dataset.source); } });
$("source-list").addEventListener("contextmenu", (event) => { const row = event.target.closest("[data-source]"); if (row) { event.preventDefault(); sourceMenu(row.dataset.source, event.clientX, event.clientY); } });
function cleanupSourceDrag() {
  $("source-list").querySelectorAll(".source-entry.is-dragging, .source-entry.drag-over").forEach((el) => el.classList.remove("is-dragging", "drag-over"));
}
function closestSourceEntry(clientY) {
  const entries = [...$("source-list").querySelectorAll(".source-entry[draggable=true]")];
  return entries.reduce((best, el) => {
    const mid = el.getBoundingClientRect().top + el.getBoundingClientRect().height / 2;
    const dist = Math.abs(clientY - mid);
    return dist < best.dist ? { entry: el, dist } : best;
  }, { entry: null, dist: Infinity });
}
$("source-list").addEventListener("dragstart", (event) => {
  const entry = event.target.closest(".source-entry[draggable=true]");
  if (!entry) { event.preventDefault(); return; }
  draggedSource = entry.dataset.source || "";
  entry.classList.add("is-dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", "");
});
$("source-list").addEventListener("dragover", (event) => {
  event.preventDefault();
  if (!draggedSource) return;
  $("source-list").querySelectorAll(".source-entry.drag-over").forEach((el) => el.classList.remove("drag-over"));
  const best = closestSourceEntry(event.clientY);
  if (best.entry && best.entry.dataset.source !== draggedSource) best.entry.classList.add("drag-over");
});
$("source-list").addEventListener("drop", async (event) => {
  event.preventDefault();
  cleanupSourceDrag();
  if (!draggedSource) return;
  const entries = [...$("source-list").querySelectorAll(".source-entry[draggable=true]:not(.is-dragging)")];
  const closest = entries.reduce((best, el) => {
    const mid = el.getBoundingClientRect().top + el.getBoundingClientRect().height / 2;
    const dist = Math.abs(event.clientY - mid);
    return dist < best.dist ? { entry: el, dist } : best;
  }, { entry: null, dist: Infinity });
  const target = closest.entry?.dataset.source;
  if (!target || target === draggedSource) { draggedSource = ""; return; }
  const ordered = sourceSort(state.sources).map((item) => item.chatId);
  ordered.splice(ordered.indexOf(draggedSource), 1);
  ordered.splice(ordered.indexOf(target), 0, draggedSource);
  try {
    await api("/api/sources/order", { method: "PATCH", body: JSON.stringify({ chatIds: ordered }) });
    $("sidebar-sort").value = "custom";
    await loadLibrary(true);
  } catch (error) { showError(error); }
  draggedSource = "";
});
$("source-list").addEventListener("dragend", () => { cleanupSourceDrag(); draggedSource = ""; });

$("track-list").addEventListener("click", (event) => { const play = event.target.closest("[data-play-key]"); const menu = event.target.closest("[data-track-menu]"); const like = event.target.closest("[data-row-like-key]"); if (play) playFromLibrary(play.dataset.playKey).catch(showError); if (menu) { const rect = menu.getBoundingClientRect(); trackMenu(menu.dataset.trackMenu, rect.right, rect.bottom); } if (like) { event.stopPropagation(); toggleRowLike(like.dataset.rowLikeKey, like).catch(showError); } });
$("track-list").addEventListener("contextmenu", (event) => { const row = event.target.closest("[data-track-key]"); if (row) { event.preventDefault(); trackMenu(row.dataset.trackKey, event.clientX, event.clientY); } });
$("track-list").addEventListener("error", (event) => { if (event.target.matches(".row-art")) { event.target.classList.remove("is-ready"); event.target.nextElementSibling?.classList.remove("is-covered"); } }, true);
// The row may be replaced by an innerHTML re-render between load and the rAF
// callback, which detaches the image and leaves nextElementSibling null.
$("track-list").addEventListener("load", (event) => { if (event.target.matches(".row-art")) requestAnimationFrame(() => { event.target.classList.add("is-ready"); event.target.nextElementSibling?.classList.add("is-covered"); }); }, true);
$("track-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { $("library").scrollTop = 0; loadLibrary(false, true); }, 220); });
$("empty-clear-search").addEventListener("click", () => { $("track-search").value = ""; $("library").scrollTop = 0; loadLibrary(); $("track-search").focus(); });
$("library").addEventListener("scroll", () => {
  document.querySelector(".library-header").classList.toggle("is-scrolled", $("library").scrollTop > 8);
  $("library").classList.add("is-scrolling");
  clearTimeout(scrollIdleTimer); scrollIdleTimer = setTimeout(finishLibraryScroll, 120);
  if (libraryFrame) return;
  libraryFrame = requestAnimationFrame(() => { libraryFrame = 0; renderTracks(); });
}, { passive: true });
addEventListener("resize", () => { state.windowStart = -1; renderTracks(true); });
$("global-search").addEventListener("focus", () => renderGlobalSearch("Type a title, artist, or source name."));
$("global-search").addEventListener("input", () => { clearTimeout(globalSearchTimer); globalSearchTimer = setTimeout(searchEverywhere, 280); });
// Escape on a type=search input fires a native clear whose input event would re-open the
// panel after the document handler closed it. Close on keyup instead, once that has settled.
$("global-search").addEventListener("keyup", (event) => { if (event.key === "Escape") closeGlobalSearch({ clear: true }); });
$("global-search-trigger").addEventListener("click", () => { if ($("app-shell").classList.contains("sidebar-collapsed")) { $("app-shell").classList.remove("sidebar-collapsed"); localStorage.setItem("tm-sidebar", "expanded"); } requestAnimationFrame(() => $("global-search").focus()); });
$("close-global-search").addEventListener("click", () => closeGlobalSearch({ clear: true }));
$("global-results").addEventListener("click", (event) => {
  const source = event.target.closest("[data-global-source]");
  const track = event.target.closest("[data-global-track]");
  if (source) {
    const item = state.globalSources.find((value) => value.chatId === source.dataset.globalSource);
    if (item?.selected) selectSource(item.chatId).catch(showError);
    else { state.temporarySource = { ...item, temporary: true, trackCount: item?.trackCount || 0 }; selectTemporary().catch(showError); }
  }
  if (track) { const queue = state.globalTracks.map((item) => item.key); closeGlobalSearch(); playKey(track.dataset.globalTrack, queue).catch(showError); }
});
$("global-results").addEventListener("contextmenu", (event) => { const track = event.target.closest("[data-global-track]"); if (track) { event.preventDefault(); trackMenu(track.dataset.globalTrack, event.clientX, event.clientY); } });
$("play-playlist").addEventListener("click", () => startPlaylist(false).catch(showError)); $("shuffle-playlist").addEventListener("click", () => startPlaylist(true).catch(showError));
$("sync-source").addEventListener("click", () => state.source ? syncSource(state.source, false) : toast("Choose a source to sync"));
$("keep-source").addEventListener("click", () => keepTemporarySource().catch(showError));
$("add-source").addEventListener("click", openSources); document.querySelector('[data-action="add-source"]').addEventListener("click", openSources);
$("sync-all-sources").addEventListener("click", () => syncAllSources().catch(showError));
$("discover-list").addEventListener("change", (event) => event.target.matches("[data-chat]") && toggleSource(event.target)); $("discover-sort").addEventListener("change", renderDiscovered);
$("sidebar-sort").value = localStorage.getItem("tm-source-sort") || "custom"; $("sidebar-sort").addEventListener("change", () => { localStorage.setItem("tm-source-sort", $("sidebar-sort").value); renderSources(); });
$("bulk-sources").addEventListener("click", () => { state.bulk = true; $("bulk-bar").hidden = false; renderSources(); }); $("bulk-cancel").addEventListener("click", () => { state.bulk = false; state.selectedSources.clear(); $("bulk-bar").hidden = true; renderSources(); }); $("bulk-unselect").addEventListener("click", () => unselectSources([...state.selectedSources]));
$("collapse-sidebar").addEventListener("click", () => { const collapsed = !$("app-shell").classList.contains("sidebar-collapsed"); $("app-shell").classList.toggle("sidebar-collapsed", collapsed); localStorage.setItem("tm-sidebar", collapsed ? "collapsed" : "expanded"); $("collapse-sidebar").setAttribute("aria-label", collapsed ? "Expand sources" : "Collapse sources"); });
$("liked-source").addEventListener("click", selectLiked);

$("play").addEventListener("click", () => togglePlayback().catch(showError)); $("previous").addEventListener("click", () => audio.currentTime > 3 ? audio.currentTime = 0 : move(-1).catch(showError)); $("next").addEventListener("click", () => move(1).catch(showError)); $("shuffle").addEventListener("click", () => toggleShuffle().catch(showError)); $("repeat").addEventListener("click", () => { state.repeat = state.repeat === "off" ? "all" : state.repeat === "all" ? "one" : "off"; updateModes(); toast(`Repeat ${state.repeat}`); });
function setVolume(value) {
  audio.volume = Math.min(1, Math.max(0, Number(value) || 0));
  if (audio.volume) lastAudibleVolume = audio.volume;
  $("volume").value = audio.volume;
  $("volume-toggle").classList.toggle("muted", !audio.volume);
  $("volume-toggle").setAttribute("aria-label", audio.volume ? "Mute" : "Restore volume");
  $("volume-pct").textContent = Math.round(audio.volume * 100);
  localStorage.setItem("tm-volume", audio.volume);
}
$("volume").addEventListener("input", () => setVolume($("volume").value));
$("volume-toggle").addEventListener("click", () => setVolume(audio.volume ? 0 : lastAudibleVolume));
setVolume(localStorage.getItem("tm-volume") ?? .8);
$("progress").addEventListener("input", () => {
  const progress = $("progress"), tooltip = $("progress-tooltip");
  const max = Number(progress.max) || 1;
  const ratio = Math.min(1, Math.max(0, Number(progress.value) / max));
  const inputRect = progress.getBoundingClientRect();
  const rowRect = progress.parentElement.getBoundingClientRect();
  const seconds = Number.isFinite(audio.duration) ? ratio * audio.duration : 0;
  tooltip.textContent = formatTime(seconds);
  tooltip.style.left = `${inputRect.left - rowRect.left + ratio * inputRect.width}px`;
  tooltip.classList.add("is-visible");
  tooltip.setAttribute("aria-hidden", "false");
});
const hideProgressTooltip = () => {
  const tooltip = $("progress-tooltip");
  tooltip.classList.remove("is-visible");
  tooltip.setAttribute("aria-hidden", "true");
};
$("progress").addEventListener("change", () => {
  if (audio.duration) audio.currentTime = Number($("progress").value) / 1000 * audio.duration;
  hideProgressTooltip();
});
$("progress").addEventListener("pointerup", hideProgressTooltip);
$("progress").addEventListener("pointercancel", hideProgressTooltip);
$("progress").addEventListener("blur", hideProgressTooltip);
audio.addEventListener("timeupdate", () => { updateProgress(); updateLyric(); const threshold = audio.duration ? Math.min(30, audio.duration / 2) : 30; if (state.current && !state.current.qualified && audio.currentTime >= threshold) { state.current.qualified = true; api("/api/playback/events", { method: "POST", body: JSON.stringify({ key: state.current.key, event: "qualified" }) }).catch(() => {}); } });
for (const event of ["progress", "durationchange", "loadedmetadata"]) audio.addEventListener(event, updateProgress);
for (const event of ["waiting", "stalled"]) audio.addEventListener(event, () => { if (!audio.paused) setBuffering(true); });
for (const event of ["canplay", "playing"]) audio.addEventListener(event, () => setBuffering(false));
audio.addEventListener("play", () => { updateTransport(); schedulePersist(); });
audio.addEventListener("pause", () => { setBuffering(false); schedulePersist(); renderQueue(); });
audio.addEventListener("ended", () => { setBuffering(false); move(1, true).catch(showError); });
audio.addEventListener("error", () => { setBuffering(false); if (state.current) showError(new AppError("This track couldn’t be streamed. Try syncing its source."), () => syncSource(state.current.chatId, false)); });
audio.addEventListener("seeked", schedulePersist);
if ("mediaSession" in navigator) { navigator.mediaSession.setActionHandler("play", () => startAudioPlayback().catch(showError)); navigator.mediaSession.setActionHandler("pause", () => audio.pause()); navigator.mediaSession.setActionHandler("previoustrack", () => move(-1).catch(showError)); navigator.mediaSession.setActionHandler("nexttrack", () => move(1).catch(showError)); }

$("queue-list").addEventListener("click", (event) => { const play = event.target.closest("[data-queue-play]"); const button = event.target.closest("[data-remove-queue]"); if (event.target.closest("[data-queue-browse]")) { closePanel(); return $("track-list").querySelector(".track-row")?.focus(); } if (play) playKey(state.queue[Number(play.dataset.queuePlay)], null, Number(play.dataset.queuePlay)).catch(showError); if (button) { state.queue.splice(Number(button.dataset.removeQueue), 1); renderQueue(); schedulePrefetch(); schedulePersist(); } }); $("queue-list").addEventListener("dragstart", (event) => { draggedQueue = Number(event.target.closest("[data-queue-index]")?.dataset.queueIndex); event.target.closest("[data-queue-index]")?.classList.add("queue-dragging"); }); $("queue-list").addEventListener("dragover", (event) => event.preventDefault()); $("queue-list").addEventListener("drop", (event) => { event.preventDefault(); document.querySelector(".queue-dragging")?.classList.remove("queue-dragging"); const target = Number(event.target.closest("[data-queue-index]")?.dataset.queueIndex); if (Number.isInteger(draggedQueue) && Number.isInteger(target) && draggedQueue !== target && draggedQueue > state.queueIndex && target > state.queueIndex) { const [key] = state.queue.splice(draggedQueue, 1); state.queue.splice(target, 0, key); renderQueue(); schedulePrefetch(); schedulePersist(); } }); $("queue-list").addEventListener("dragend", (event) => { event.target.closest("[data-queue-index]")?.classList.remove("queue-dragging"); }); $("clear-queue").addEventListener("click", () => { state.queue = state.current ? [state.current.key] : []; state.queueIndex = state.current ? 0 : -1; renderQueue(); schedulePrefetch(); schedulePersist(); });

$("player-open").addEventListener("click", () => showPanel("lyrics")); $("player-locate").addEventListener("click", () => locateCurrent()); $("show-lyrics").addEventListener("click", () => showPanel("lyrics", true)); $("close-now").addEventListener("click", closePanel); for (const tab of ["lyrics", "queue", "details"]) $(`${tab}-tab`).addEventListener("click", () => showPanel(tab));
$("now-panel").querySelector('[role="tablist"]').addEventListener("keydown", (e) => {
  const tabs = [...e.currentTarget.querySelectorAll('[role="tab"]')]; const i = tabs.indexOf(document.activeElement); if (i === -1) return;
  if (e.key === "ArrowRight") { e.preventDefault(); tabs[(i + 1) % tabs.length].click(); tabs[(i + 1) % tabs.length].focus(); }
  if (e.key === "ArrowLeft") { e.preventDefault(); tabs[(i - 1 + tabs.length) % tabs.length].click(); tabs[(i - 1 + tabs.length) % tabs.length].focus(); }
});
$("lyrics-lines").addEventListener("click", (event) => { const line = event.target.closest("[data-lyric]"); if (line) { audio.currentTime = state.lyrics.lines[Number(line.dataset.lyric)].startMs / 1000; state.lyricsFollow = true; $("sync-lyrics").hidden = true; line.classList.remove("seek-pulse"); void line.offsetWidth; line.classList.add("seek-pulse"); } }); $("now-panel").addEventListener("wheel", stopFollowingLyrics, { passive: true }); $("now-panel").addEventListener("touchmove", stopFollowingLyrics, { passive: true }); $("sync-lyrics").addEventListener("click", () => { state.lyricsFollow = true; $("sync-lyrics").hidden = true; state.lyric = -2; updateLyric(); }); $("add-lyrics-empty").addEventListener("click", openLyricsEditor); $("edit-current").addEventListener("click", () => openMetadata()); $("edit-lyrics").addEventListener("click", openLyricsEditor);
$("like-current").addEventListener("click", () => toggleLike().catch((error) => showError(error, toggleLike))); $("save-current-telegram").addEventListener("click", saveCurrentToTelegram); $("share-current").addEventListener("click", openShare); $("player-more").addEventListener("click", (event) => { if (!state.current) return; const rect = event.currentTarget.getBoundingClientRect(); trackMenu(state.current.key, rect.right, rect.top); });
$("contact-search").addEventListener("input", renderContacts); $("contact-list").addEventListener("click", (event) => { const contact = event.target.closest("[data-contact]"); if (contact) queueShare(contact.dataset.contact); });
$("metadata-form").addEventListener("submit", saveMetadata); $("reset-metadata").addEventListener("click", resetMetadata); $("fetch-metadata").addEventListener("click", fetchMetadata); $("candidate-list").addEventListener("click", (event) => { const button = event.target.closest("[data-candidate]"); if (button) applyCandidate(button.dataset.candidate); }); $("lyrics-form").addEventListener("submit", saveLyrics); $("reset-lyrics").addEventListener("click", async () => { if (await confirmAction("Fetch lyrics again?", "Saved lyrics will be replaced by a new internet lookup.", "Fetch again")) { try { $("lyrics-status").textContent = "Looking for lyrics…"; state.lyrics = await api(mediaUrl(state.current, "lyrics"), { method: "DELETE" }); renderLyrics(); $("lyrics-status").textContent = "Lyrics lookup finished."; } catch (error) { $("lyrics-status").textContent = error.message; } } });

$("open-settings").addEventListener("click", openSettings); document.querySelectorAll("[data-settings-tab]").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll("[data-settings-tab]").forEach((item) => item.classList.toggle("active", item === button)); document.querySelectorAll("[data-settings-pane]").forEach((pane) => { pane.hidden = pane.dataset.settingsPane !== button.dataset.settingsTab; }); })); document.querySelectorAll(".save-settings").forEach((button) => button.addEventListener("click", () => saveSettings(button))); $("test-musicbrainz").addEventListener("click", async () => { try { state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify({ musicbrainzContact: $("musicbrainz-contact").value.trim(), coverQuality: $("default-cover-quality").value }) }); await api("/api/settings/musicbrainz/test", { method: "POST" }); toast("MusicBrainz connection works"); } catch (error) { showError(error, () => $("test-musicbrainz").click()); } });
$("clear-cache").addEventListener("click", async () => { if (await confirmAction("Clear prefetched songs?", "Playback metadata and Telegram files will not be changed.", "Clear cache")) { try { await api("/api/cache", { method: "DELETE" }); $("cache-usage").textContent = "0 cached · 0 MB"; toast("Prefetched songs cleared"); } catch (error) { showError(error); } } });
document.querySelectorAll("[data-setting] [data-value]").forEach((button) => button.addEventListener("click", () => { localStorage.setItem(`tm-${button.parentElement.dataset.setting}`, button.dataset.value); applyPreferences(); }));
$("disconnect-telegram").addEventListener("click", async () => { if (await confirmAction("Disconnect Telegram?", "This signs out the stored Telegram session and clears the local library. It does not leave or delete any Telegram chats.", "Disconnect")) { try { await api("/api/telegram/session", { method: "DELETE" }); location.reload(); } catch (error) { showError(error); } } });

$("error-retry").addEventListener("click", () => { $("error-dialog").close(); const action = retryAction; retryAction = null; action?.(); }); $("confirm-accept").addEventListener("click", () => { $("confirm-dialog").close(); confirmResolve?.(true); confirmResolve = null; });
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => { const dialog = $(button.dataset.close); dialog.close(); if (dialog.id === "confirm-dialog") { confirmResolve?.(false); confirmResolve = null; } }));
document.querySelectorAll("dialog").forEach((dialog) => { dialog.addEventListener("click", (event) => { if (event.target !== dialog) return; const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) { dialog.close(); if (dialog.id === "confirm-dialog") { confirmResolve?.(false); confirmResolve = null; } } }); dialog.addEventListener("cancel", () => { if (dialog.id === "confirm-dialog") { confirmResolve?.(false); confirmResolve = null; } }); });
document.addEventListener("error", (event) => { const image = event.target; if (image.matches?.("img.source-avatar")) { const replacement = document.createElement("span"); replacement.className = "source-avatar"; replacement.textContent = image.dataset.avatarFallback || "♪"; image.replaceWith(replacement); } }, true);
$("context-menu").addEventListener("click", (event) => { const button = event.target.closest("[data-menu-index]"); if (button) { const action = $("context-menu")._actions[Number(button.dataset.menuIndex)]?.action; closeMenu(); action?.(); } }); document.addEventListener("pointerdown", (event) => { if (!event.target.closest("#context-menu") && !$("context-menu").hidden) closeMenu(); if (!event.target.closest(".global-search-wrap") && !$("global-results").hidden) closeGlobalSearch(); }); document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeMenu(); closeGlobalSearch(); } });
document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea, [contenteditable]")) return;
  if (event.target.closest("button, a, [role='button'], select")) return;
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.key === " " || event.code === "Space") {
    event.preventDefault();
    if (state.current) togglePlayback().catch(showError);
    return;
  }
  if (event.key === "ArrowLeft") { event.preventDefault(); audio.currentTime = Math.max(0, audio.currentTime - 5); return; }
  if (event.key === "ArrowRight") { event.preventDefault(); audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 5); return; }
  if (event.key === "l" || event.key === "L") { event.preventDefault(); $("like-current")?.click(); return; }
  if (event.key === "/") { event.preventDefault(); $("global-search")?.focus(); return; }
  if (event.key === "m" || event.key === "M") { event.preventDefault(); $("volume-toggle")?.click(); return; }
});
$("open-nav").addEventListener("click", () => { $("source-rail").classList.add("open"); $("rail-scrim").hidden = false; }); $("close-nav").addEventListener("click", () => { $("source-rail").classList.remove("open"); $("rail-scrim").hidden = true; }); $("rail-scrim").addEventListener("click", () => { $("source-rail").classList.remove("open"); $("rail-scrim").hidden = true; });

installResizer("left-resizer", "left"); installResizer("right-resizer", "right");
addEventListener("pagehide", persistPlayerState); addEventListener("beforeunload", persistPlayerState);

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
boot().catch((error) => { showError(error, boot); });
