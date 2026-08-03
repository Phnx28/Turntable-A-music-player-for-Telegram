import { adjacentIndex, bufferedPercent, explicitQueue, formatTime, lyricIndex, normalizePlayerState, normalizeTrackPage, queueView, resolveWindowEdge, restoreWindow, shouldCompactHeader, snapshotWindow, toggleShuffleQueue, virtualTrackWindow, windowFromResult } from "./player-core.js";
import { AppError, errorCopy, formatDayRule, formatPostedDate, formatSyncedAt, ordinal, sourceKindLabel } from "./format.js";
import { beginLikeOperation, likedState, representationsFor, resolveLikeResponse, rollbackLikeOperation } from "./like-state.js";
import { knownTotalParam, mergePageInto, pageCacheKey, shouldFetchPage } from "./library-pages.js";

const $ = (id) => document.getElementById(id);
const state = {
  sources: [], tracks: [], discovered: [], source: "", likedMode: false, sort: "posted", current: null, editing: null,
  lyrics: null, flow: "", lyric: -1, queue: [], queueIndex: -1, queueTruncated: false, queueTotal: 0, queueOffset: 0,
  shuffle: localStorage.getItem("tm-shuffle") === "1",
  repeat: localStorage.getItem("tm-repeat") || "off",
  cacheStates: {}, settings: { prefetchCount: 1, coverQuality: "1200", musicbrainzContact: "" },
  trackCache: new Map(), summaryCache: new Map(), libraryCache: new Map(),
  loadedPages: new Set(), pageRequests: new Set(), totalTracks: 0, allMusicTotal: null, dayBreaks: [], windowStart: -1, rowFocusIndex: 0, libraryLoading: false,
  globalTracks: [], globalSources: [], summaryRequests: new Set(),
  temporarySource: null, temporaryJob: null, keepingSource: false, likedCount: 0, historyVisible: 200,
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
let countryList = [], countryMatches = [], countryActive = -1, selectedCountry = null, countryCloseTimer;
const COUNTRY_RESULT_LIMIT = 60;
const GLOBAL_SEARCH_LIMIT = 30;
let lastAudibleVolume = .8;
const pendingCovers = new Set();
const rowLikeOperations = new Map();

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
  notice.classList.remove("counting", "is-leaving");
  notice.hidden = false;
  requestAnimationFrame(() => notice.classList.add("counting"));
  // Fade out rather than blink off the screen at the end of the countdown. The class is
  // cleared on the next toast, so a rapid sequence of messages cannot get stuck mid-exit.
  const settle = matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 180;
  toastTimer = setTimeout(() => {
    notice.classList.add("is-leaving");
    notice.classList.remove("counting");
    toastTimer = setTimeout(() => { notice.hidden = true; notice.classList.remove("is-leaving"); }, settle);
    action?.expire?.();
  }, duration);
}

function showError(error, retry = null, title = "Couldn’t complete that") {
  retryAction = retry;
  $("error-title").textContent = title;
  $("error-message").textContent = errorCopy(error);
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
  const window = snapshotWindow(state.queue, state.queueIndex, {
    total: state.queueTotal, truncated: state.queueTruncated, offset: state.queueOffset,
  });
  return {
    version: 2,
    ...window,
    currentKey: state.current?.key || "", position: audio.currentTime || 0,
    source: state.source, liked: state.likedMode, temporarySource: state.temporarySource,
    panel: document.querySelector(".now-tabs .active")?.id?.replace("-tab", "") || "lyrics",
    panelOpen: !$("now-panel").hidden,
  };
}
// Sleep timer: client-only (pause is a browser action), but the deadline survives reloads via
// localStorage so a closed laptop mid-countdown does not silently lose the "stop" promise.
let sleepEndsAt = 0, sleepTimerHandle = 0;
function setSleepTimer(value, silent = false) {
  localStorage.setItem("tm-sleep", value);
  clearTimeout(sleepTimerHandle);
  const minutes = Number(value) || 0;
  const select = $("sleep-timer");
  if (!minutes) {
    localStorage.removeItem("tm-sleep-ends");
    sleepEndsAt = 0;
    if (select) select.value = "off";
    if (!silent) toast("Sleep timer off");
    return;
  }
  sleepEndsAt = Date.now() + minutes * 60000;
  localStorage.setItem("tm-sleep-ends", String(sleepEndsAt));
  sleepTimerHandle = setTimeout(() => {
    audio.pause(); setBuffering(false);
    toast("Sleep timer ended playback");
    setSleepTimer("off", true);
  }, minutes * 60000);
  if (!silent) toast(`Playback stops in ${minutes} minutes`);
}
function restoreSleepTimer() {
  const saved = localStorage.getItem("tm-sleep") || "off";
  const select = $("sleep-timer");
  if (!select) return;
  const remaining = (Number(localStorage.getItem("tm-sleep-ends")) || 0) - Date.now();
  if (saved !== "off" && remaining > 0) {
    select.value = saved;
    sleepEndsAt = Date.now() + remaining;
    sleepTimerHandle = setTimeout(() => {
      audio.pause(); setBuffering(false);
      toast("Sleep timer ended playback");
      setSleepTimer("off", true);
    }, remaining);
  } else if (saved !== "off") {
    localStorage.removeItem("tm-sleep-ends");
    localStorage.setItem("tm-sleep", "off");
    select.value = "off";
  }
}

function persistPlayerState() {
  // Startup calls selectSource() -> schedulePersist() before restorePlayerState() has finished its
  // awaits, so an empty snapshot was being written over the saved one. It went unnoticed while the
  // whole queue was stored, because the clobber usually rewrote identical data; with only a window
  // saved it destroys the position and the queueTotal that says more tracks exist.
  if (!state.restored) return;
  try { localStorage.setItem("tm-player-state", JSON.stringify(playerSnapshot())); } catch {}
}

function schedulePersist() {
  clearTimeout(positionTimer);
  positionTimer = setTimeout(persistPlayerState, 250);
}

async function restorePlayerState(saved) {
  if (!saved) return;
  Object.assign(state, restoreWindow(saved.queue, saved.queueIndex, saved.queueTotal, saved.queueOffset));
  if (!saved.currentKey) { renderQueue(); return; }
  try {
    const track = await getTrack(saved.currentKey);
    state.current = { ...track, qualified: false, restored: true, _retried: false };
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
  restoreSleepTimer();
  // The accent picker is gone, but a browser that used it still holds tm-accent and a
  // data-accent attribute in the served markup would keep overriding --accent. Drop both once
  // so an existing install lands on the single accent rather than whatever it last chose.
  localStorage.removeItem("tm-accent");
  delete document.documentElement.dataset.accent;
  for (const [name, fallback] of [["theme", "system"], ["font", "sans"]]) {
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
  const field = { phone: "country-search", code: "telegram-code", twofa: "telegram-password" }[stage];
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
  // Trunk prefix: people type their number the way they dial it locally (e.g. 0151... in
  // Germany), but the international form drops that leading zero. Telegram rejects it otherwise.
  const number = $("telegram-phone").value.replace(/\D/g, "").replace(/^0+/, "");
  if (!country) throw new AppError("Choose your country first.");
  if (!number) throw new AppError("Enter your phone number.");
  return `+${country}${number}`;
}

function countryFlag(iso2) {
  return [...iso2.toUpperCase()].map((letter) => String.fromCodePoint(127397 + letter.charCodeAt(0))).join("");
}

async function loadCountries() {
  if (state.countriesLoaded) return;
  const search = $("country-search");
  try {
    countryList = await api("/api/telegram/countries");
    // Fold accents once up front so "Cote" matches "C\u00f4te d'Ivoire" without work per keystroke.
    for (const country of countryList) {
      country.search = `${country.name} ${country.iso2}`.normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
      country.flag = countryFlag(country.iso2);
    }
    search.disabled = false;
    search.placeholder = "Search countries…";
    state.countriesLoaded = true;
    const saved = localStorage.getItem("tm-country");
    let region = saved;
    if (!region) try { region = new Intl.Locale(navigator.language).region; } catch {}
    const preferred = countryList.find((country) => country.iso2 === region);
    if (preferred) selectCountry(preferred, { silent: true });
  } catch (error) {
    search.placeholder = "Countries unavailable";
    showError(error, loadCountries);
  }
}

// Ranked so exact and prefix matches beat mid-word ones: typing "in" should offer India before
// Argentina. Dial-code digits are matched too, with or without a leading "+".
function matchCountries(query) {
  const term = query.normalize("NFD").replace(/\p{Diacritic}/gu, "").trim().toLowerCase();
  if (!term) return countryList.slice(0, COUNTRY_RESULT_LIMIT);
  const digits = term.replace(/[^\d]/g, "");
  const scored = [];
  for (const country of countryList) {
    const name = country.search;
    let score = -1;
    if (name === term) score = 0;
    else if (name.startsWith(term)) score = 1;
    else if (name.includes(` ${term}`)) score = 2;
    else if (name.includes(term)) score = 3;
    if (digits && country.dialCode.startsWith(digits)) score = score === -1 ? 2 : Math.min(score, 2);
    if (score !== -1) scored.push({ country, score });
  }
  scored.sort((a, b) => a.score - b.score || a.country.name.localeCompare(b.country.name));
  return scored.slice(0, COUNTRY_RESULT_LIMIT).map((entry) => entry.country);
}

function renderCountryOptions(query) {
  const list = $("country-listbox");
  countryMatches = matchCountries(query);
  countryActive = countryMatches.length ? 0 : -1;
  if (!countryMatches.length) {
    list.innerHTML = '<li class="combo-empty" role="presentation">No matching country</li>';
  } else {
    list.innerHTML = countryMatches.map((country, index) => `<li class="combo-option${index === 0 ? " is-active" : ""}" role="option" id="country-option-${index}" aria-selected="${index === 0}" data-index="${index}"><span class="combo-flag" aria-hidden="true">${country.flag}</span><span class="combo-name">${escapeHtml(country.name)}</span><span class="combo-dial">+${escapeHtml(country.dialCode)}</span></li>`).join("");
  }
  openCountryList();
  syncCountryActive();
}

function openCountryList() {
  const list = $("country-listbox");
  if (!list.hidden) return;
  clearTimeout(countryCloseTimer);
  list.classList.remove("is-closing");
  list.hidden = false;
  $("country-search").setAttribute("aria-expanded", "true");
}

function closeCountryList() {
  const list = $("country-listbox");
  const search = $("country-search");
  search.setAttribute("aria-expanded", "false");
  search.removeAttribute("aria-activedescendant");
  if (list.hidden) return;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { list.hidden = true; return; }
  list.classList.add("is-closing");
  clearTimeout(countryCloseTimer);
  countryCloseTimer = setTimeout(() => { list.hidden = true; list.classList.remove("is-closing"); }, 140);
}

function syncCountryActive() {
  const list = $("country-listbox");
  for (const option of list.querySelectorAll(".combo-option")) {
    const active = Number(option.dataset.index) === countryActive;
    option.classList.toggle("is-active", active);
    option.setAttribute("aria-selected", String(active));
    if (active) {
      option.scrollIntoView({ block: "nearest" });
      $("country-search").setAttribute("aria-activedescendant", option.id);
    }
  }
}

function moveCountryActive(step) {
  if (!countryMatches.length) return;
  countryActive = (countryActive + step + countryMatches.length) % countryMatches.length;
  syncCountryActive();
}

function selectCountry(country, { silent = false } = {}) {
  selectedCountry = country;
  $("telegram-country").value = country.dialCode;
  $("country-search").value = `${country.flag} ${country.name}`;
  $("dial-prefix").textContent = `+${country.dialCode}`;
  $("country-clear").hidden = false;
  localStorage.setItem("tm-country", country.iso2);
  closeCountryList();
  if (!silent) requestAnimationFrame(() => $("telegram-phone").focus());
}

function clearCountry({ focus = true } = {}) {
  selectedCountry = null;
  $("telegram-country").value = "";
  $("country-search").value = "";
  $("dial-prefix").textContent = "+";
  $("country-clear").hidden = true;
  closeCountryList();
  if (focus) $("country-search").focus();
}

async function startQr(phoneMessage = "Choose your country to continue.") {
  clearTimeout(qrTimer);
  const qrExit = clearQr();
  state.flow = "";
  setLoginStage("phone", phoneMessage);
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

// The phone login markup and the /api/telegram/{phone,code,password} endpoints both existed, but
// nothing ever bound #phone-form, so submitting fell through to the browser's default GET and
// reloaded the page. These three handlers complete that flow.
async function submitPhone(event) {
  event.preventDefault();
  const button = $("phone-form").querySelector('button[type="submit"]');
  try {
    const phone = phoneNumber();
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
    // Stop the QR poller: both flows race to finish the same login otherwise.
    clearTimeout(qrTimer);
    pauseQr("Using phone login");
    const flow = await api("/api/telegram/phone", { method: "POST", body: JSON.stringify({ phone }) });
    state.flow = flow.flowId;
    const via = { App: "Telegram app", Sms: "SMS", Call: "a phone call" }[flow.delivery] || "Telegram";
    setLoginStage("code", `Code sent via ${via}. Enter it to continue.`);
  } catch (error) {
    showError(error);
    $("phone-status").textContent = error.message || "Could not send the code.";
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
}

async function submitPhoneCode(event) {
  event.preventDefault();
  const button = $("code-form").querySelector('button[type="submit"]');
  try {
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
    const status = await api("/api/telegram/code", { method: "POST", body: JSON.stringify({ flowId: state.flow, code: $("telegram-code").value }) });
    await applyPhoneStatus(status);
  } catch (error) {
    showError(error);
    $("phone-status").textContent = error.message || "Could not verify the code.";
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
}

async function submitPhonePassword(event) {
  event.preventDefault();
  const button = $("twofa-form").querySelector('button[type="submit"]');
  try {
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
    const status = await api("/api/telegram/password", { method: "POST", body: JSON.stringify({ flowId: state.flow, password: $("telegram-password").value }) });
    await applyPhoneStatus(status);
  } catch (error) {
    showError(error);
    $("phone-status").textContent = error.message || "Could not verify the password.";
  } finally {
    button.removeAttribute("aria-busy");
    button.disabled = false;
  }
}

// Both the code and password steps return a flow status, so they share one interpreter.
async function applyPhoneStatus(status) {
  if (status.state === "ready") return boot();
  if (status.state === "password_required") {
    $("telegram-password").value = "";
    return setLoginStage("twofa", "Enter your Telegram two-step verification password.");
  }
  if (status.state === "expired" || status.state === "error") {
    const reason = status.error || "That login expired. Send a new code.";
    state.flow = "";
    // Pass the reason into startQr so it is shown from the first frame -- otherwise the user is
    // bounced to step one under a generic "choose your country" with no idea why.
    startQr(reason);
    return;
  }
  // "waiting" means Telegram rejected this attempt but the flow is still open, so stay put.
  $("phone-status").textContent = status.error || "That code was not accepted. Try again.";
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
  $("all-count").textContent = state.allMusicTotal === null ? "—" : state.allMusicTotal.toLocaleString();
  $("liked-count").textContent = state.likedCount.toLocaleString();
  $("liked-source").classList.toggle("active", state.likedMode);
  $("liked-source").toggleAttribute("aria-current", state.likedMode);
  const temporary = state.temporarySource && !state.sources.some((item) => item.chatId === state.temporarySource.chatId)
    ? `<div class="source-link source-entry temporary-source ${state.source === state.temporarySource.chatId ? "active" : ""}" data-temporary-source="${state.temporarySource.chatId}" role="button" tabindex="0" title="${escapeHtml(state.temporarySource.title)}">${avatarMarkup(state.temporarySource)}<span class="source-copy"><strong>${escapeHtml(state.temporarySource.title)}</strong><small>Temporary · current track</small></span><span class="source-count">Live</span></div>` : "";
  $("source-list").innerHTML = temporary + sorted.map((source) => {
    const draggable = $("sidebar-sort").value === "custom" && !state.bulk && !source.pinnedAt;
    return `<div class="source-link source-entry ${!state.likedMode && source.chatId === state.source ? "active" : ""}${source.pinnedAt ? " pinned" : ""}" data-source="${source.chatId}" role="button" tabindex="0" title="${escapeHtml(source.title)}" draggable="${draggable}"${!state.likedMode && source.chatId === state.source ? ' aria-current="page"' : ""}>
    ${state.bulk ? `<input class="source-select" type="checkbox" data-bulk-source="${source.chatId}" ${state.selectedSources.has(source.chatId) ? "checked" : ""} aria-label="Select ${escapeHtml(source.title)}">` : avatarMarkup(source)}
    <span class="source-copy"><strong>${source.pinnedAt ? `<span class="source-pin-mark" aria-hidden="true">${icon("pin")}</span>` : ""}${escapeHtml(source.title)}</strong><small>${escapeHtml(sourceKindLabel(source.kind))}${source.syncError ? `<span class="source-error-dot" role="img" aria-label="Sync problem: ${escapeAttr(source.syncError)}" title="${escapeAttr(source.syncError)}"></span>` : ""}</small></span>
    <span class="source-count">${source.trackCount.toLocaleString()}</span>
    ${state.bulk ? "" : `<button class="icon-button source-menu" type="button" data-source-menu="${source.chatId}" aria-label="Actions for ${escapeHtml(source.title)}">${icon("more")}</button>`}
  </div>`;
  }).join("");
  const allMusic = document.querySelector('[data-source=""]');
  allMusic?.toggleAttribute("hidden", state.bulk);
  $("liked-source").toggleAttribute("hidden", state.bulk);
  allMusic?.classList.toggle("active", !state.source && !state.likedMode);
  allMusic?.toggleAttribute("aria-current", !state.source && !state.likedMode);
  const selected = state.likedMode ? null : state.sources.find((item) => item.chatId === state.source) || (state.temporarySource?.chatId === state.source ? state.temporarySource : null);
  $("source-title").textContent = state.likedMode ? "Liked songs" : selected?.title || "All music";
  $("source-kind").textContent = state.likedMode ? "Saved locally" : selected ? (selected.temporary ? "Temporary source" : sourceKindLabel(selected.kind)) : "Your Telegram";
  $("library").classList.toggle("single-source", Boolean(state.source) && !state.likedMode);
  const synced = formatSyncedAt(selected?.lastSyncedAt, Math.floor(Date.now() / 1000));
  $("library-summary").textContent = `${state.totalTracks.toLocaleString()} ${state.totalTracks === 1 ? "track" : "tracks"}${synced ? ` · ${synced}` : ""}`;
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

function renderTrackRow(track, position = 0) {
  const playing = track.key === state.current?.key;
  const liked = trackLikedState(track.key);
  const now = Math.floor(Date.now() / 1000);
  return `<article class="track-row ${playing ? "current" : ""}" data-track-key="${escapeHtml(track.key)}" data-track-index="${position}" tabindex="${position === state.rowFocusIndex ? "0" : "-1"}">
    <span class="track-ordinal utility">${playing ? `<span class="playing-mark" aria-label="Now playing"></span>` : ordinal(position + 1, state.totalTracks)}</span>
    <button class="track-main" type="button" data-play-key="${escapeHtml(track.key)}">
      <span class="mini-art-wrap"><img class="mini-art row-art" data-src="${mediaUrl(track)}?v=${encodeURIComponent(track.artworkVersion || "telegram")}" alt=""><span class="art-placeholder mini"><span></span></span><span class="track-play-overlay">${icon(playing && !audio.paused ? "pause" : "play-filled")}</span></span>
      <span class="track-copy"><strong>${escapeHtml(track.title)}</strong><small>${escapeHtml(track.artist || "Unknown artist")}</small></span>
    </button>
    <span class="track-source">${escapeHtml(track.source.title)}</span>
    <span class="track-posted utility">${escapeHtml(formatPostedDate(track.sentAt, now))}</span>
    <span class="track-duration utility">${formatTime(track.durationMs / 1000)}</span>
    <span class="track-row-actions">
      <button class="icon-button row-like ${liked ? "active" : ""}" type="button" data-row-like-key="${escapeHtml(track.key)}" aria-pressed="${liked}" aria-label="${liked ? "Unlike" : "Like"} ${escapeHtml(track.title)}">${icon(liked ? "heart-filled" : "heart")}</button>
    </span>
    <button class="icon-button row-menu" type="button" data-track-menu="${escapeHtml(track.key)}" aria-label="Actions for ${escapeHtml(track.title)}">${icon("more")}</button>
  </article>`;
}

function renderTrackPlaceholder() {
  // Seven top-level children, matching the seven grid columns exactly: ordinal, main, source,
  // posted, time, actions, menu. One short and the skeleton shears against real rows.
  return '<article class="track-row track-placeholder" aria-hidden="true"><i class="placeholder-ordinal"></i><span class="placeholder-main"><i></i><span><i></i><i></i></span></span><i class="placeholder-source"></i><i class="placeholder-posted"></i><i class="placeholder-time"></i><i></i><i></i></article>';
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

function trackRowHeight() { return 52; }

function focusTrackRow(index) {
  if (!state.totalTracks) return;
  index = Math.max(0, Math.min(state.totalTracks - 1, index));
  state.rowFocusIndex = index;
  const row = document.querySelector(`.track-row[data-track-index="${index}"]`);
  if (row) { row.focus(); return; }
  // The target is outside the rendered window: scroll it into view; the scroll handler
  // re-renders the window, then focus lands once the row exists.
  const scroller = $("library");
  scroller.scrollTop = Math.max(0, $("track-list").offsetTop + index * trackRowHeight() - scroller.clientHeight / 2);
  let attempts = 0;
  const tryFocus = () => {
    const target = document.querySelector(`.track-row[data-track-index="${index}"]`);
    if (target) target.focus();
    else if (++attempts < 20) requestAnimationFrame(tryFocus);
  };
  requestAnimationFrame(tryFocus);
}
function daySeparatorHeight() {
  return parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--day-separator-height")) || 28;
}

function visibleDayBreaks() {
  return !state.source && !state.likedMode && state.sort === "posted" ? state.dayBreaks : [];
}

function renderDaySeparator(dayBreak) {
  const label = formatDayRule(dayBreak.dayKey);
  return label ? `<div class="day-separator utility" data-day-key="${dayBreak.dayKey}" role="separator">${label}</div>` : "";
}

function renderTracks(force = false) {
  const list = $("track-list");
  for (const cell of document.querySelectorAll(".track-head [data-sort]")) {
    const active = cell.dataset.sort === state.sort;
    // Posted and Duration read newest/longest first; Title and Artist read A-Z.
    if (active) cell.setAttribute("aria-sort", state.sort === "title" || state.sort === "artist" ? "ascending" : "descending");
    else cell.removeAttribute("aria-sort");
  }
  $("track-sort").value = state.sort;
  const empty = state.totalTracks === 0 && !state.libraryLoading;
  $("empty-library").hidden = !empty; list.hidden = empty;
  if (empty) {
    $("library").classList.add("is-empty");
    $("play-playlist").disabled = true;
    $("shuffle-playlist").disabled = true;
    const query = $("track-search").value.trim();
    $("empty-title").textContent = query ? `Nothing matches "${query}"` : "Add a channel, bot, or private chat";
    $("empty-body").textContent = query
      ? "Try fewer words, or search every chat with the search box in the sidebar."
      : "The app will find its audio and keep the playlist in sync.";
    $("empty-add").hidden = Boolean(query);
    $("empty-clear-search").hidden = !query;
    list.replaceChildren(); renderSources(); return;
  }
  $("library").classList.remove("is-empty");
  $("play-playlist").disabled = false;
  $("shuffle-playlist").disabled = false;
  if (!state.totalTracks) {
    // Mid-refresh the old rows are still on screen and dimmed; replacing them with a skeleton
    // is the flash we are avoiding.
    if (!$("library").classList.contains("is-refreshing")) list.innerHTML = librarySkeleton();
    return;
  }
  const scroller = $("library");
  const rowHeight = trackRowHeight();
  const dayBreaks = visibleDayBreaks();
  const { start, end, topHeight, bottomHeight } = virtualTrackWindow({
    scrollTop: scroller.scrollTop,
    listTop: list.offsetTop,
    total: state.totalTracks,
    rowHeight,
    separatorHeight: daySeparatorHeight(),
    dayBreaks,
  });
  if (force || state.windowStart !== start) {
    state.windowStart = start;
    const breaksByIndex = new Map(dayBreaks.map((item) => [item.index, item]));
    const rows = Array.from({ length: end - start }, (_, offset) => {
      const index = start + offset;
      const separator = breaksByIndex.has(index) ? renderDaySeparator(breaksByIndex.get(index)) : "";
      return separator + (state.tracks[index] ? renderTrackRow(state.tracks[index], index) : renderTrackPlaceholder());
    }).join("");
    list.innerHTML = `<div class="track-spacer"></div>${rows}<div class="track-spacer"></div>`;
    const spacers = list.querySelectorAll(".track-spacer");
    spacers[0].style.height = `${topHeight}px`;
    spacers[1].style.height = `${bottomHeight}px`;
    list.querySelectorAll(".row-art[data-src]").forEach((image) => coverObserver.observe(image));
    // Roving tabindex: the focused row keeps the only tab stop. If it is outside this window
    // (Tab entered the list mid-scroll), fall back to the first real row so the list is
    // keyboard-reachable at all.
    if (!list.querySelector(".track-row:not(.track-placeholder)[tabindex='0']")) {
      const fallback = list.querySelector(".track-row:not(.track-placeholder)");
      if (fallback) fallback.tabIndex = 0;
    }
  }
  for (let offset = Math.floor(start / 100) * 100; offset < end; offset += 100) loadPage(offset);
}

function libraryCacheKey(offset) {
  const query = $("track-search").value.trim();
  const temporary = Boolean(state.temporarySource?.chatId === state.source && !state.sources.some((item) => item.chatId === state.source));
  return pageCacheKey(offset, {
    likedMode: state.likedMode, source: state.source, query, temporary, sort: state.sort,
  });
}

async function loadPage(offset, force = false, token = libraryRequest) {
  offset = Math.max(0, Math.floor(offset / 100) * 100);
  if (!shouldFetchPage(offset, state.loadedPages, state.pageRequests)) return;
  state.pageRequests.add(offset);
  const cacheKey = libraryCacheKey(offset);
  try {
    const raw = !force && state.libraryCache.get(cacheKey) || await api(`/api/tracks?${cacheKey}${knownTotalParam(offset, state.totalTracks)}`, { signal: requestController.signal });
    if (token !== libraryRequest) return;
    const page = normalizeTrackPage(raw);
    if (!state.libraryCache.has(cacheKey)) cacheSet(state.libraryCache, cacheKey, page, 8);
    state.totalTracks = page.total;
    state.allMusicTotal = page.allMusicTotal;
    state.dayBreaks = page.dayBreaks;
    mergePageInto(state.tracks, page, (track) => cacheSet(state.summaryCache, track.key, track, 500));
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
  state.totalTracks = 0; state.dayBreaks = []; state.windowStart = -1; state.rowFocusIndex = 0; state.libraryLoading = true;
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

let globalResultsCloseTimer = 0;
function closeGlobalSearch({ clear = false } = {}) {
  const results = $("global-results");
  clearTimeout(globalResultsCloseTimer);
  if (!results.hidden && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    results.classList.add("is-leaving");
    globalResultsCloseTimer = setTimeout(() => { results.classList.remove("is-leaving"); results.hidden = true; }, 140);
  } else {
    results.hidden = true;
  }
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
  // Typing again during the close fade must cancel it, or the results fade out as they arrive.
  clearTimeout(globalResultsCloseTimer); panel.classList.remove("is-leaving");
  panel.hidden = false; $("global-search").setAttribute("aria-expanded", "true");
  $("global-results-title").textContent = $("global-search").value.trim() || "Search everywhere";
  const found = state.globalTracks.length + state.globalSources.length;
  $("global-results-count").textContent = message ? "" : found === GLOBAL_SEARCH_LIMIT
    ? `First ${found} results`
    : `${found} ${found === 1 ? "result" : "results"}`;
  $("global-source-results").innerHTML = state.globalSources.length
    ? `<h3>Telegram sources</h3>${state.globalSources.map((source) => `<button class="global-result" type="button" data-global-source="${source.chatId}"><span class="result-art-wrap">${avatarMarkup(source)}</span><span class="track-copy"><strong>${escapeHtml(source.title)}</strong><small>${escapeHtml(sourceKindLabel(source.kind))}${source.trackCount ? ` · ${source.trackCount.toLocaleString()} known tracks` : ""}</small></span><span class="result-provenance">${source.selected ? "In your library" : "On Telegram"}</span><span class="track-duration utility"></span></button>`).join("")}`
    : "";
  $("global-track-results").innerHTML = state.globalTracks.length
    ? `<h3>Tracks</h3>${state.globalTracks.map((track) => `<button class="global-result" type="button" data-global-track="${escapeHtml(track.key)}"><span class="result-art-wrap"><img class="row-art" src="${mediaUrl(track)}?v=${encodeURIComponent(track.artworkVersion || "telegram")}" alt="" loading="lazy"></span><span class="track-copy"><strong>${escapeHtml(track.title)}</strong><small>${escapeHtml(track.artist || "Unknown artist")} · ${escapeHtml(track.source.title)}</small></span><span class="result-provenance">${track.source.selected ? "In your library" : "On Telegram"}</span><span class="track-duration utility">${formatTime(track.durationMs / 1000)}</span></button>`).join("")}`
    : "";
  const empty = $("global-search-empty");
  empty.hidden = Boolean(state.globalTracks.length || state.globalSources.length) && !message;
  empty.textContent = message || "Nothing matches that search";
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
    const remote = await api("/api/search/telegram", { method: "POST", signal: globalController.signal, body: JSON.stringify({ query, limit: GLOBAL_SEARCH_LIMIT }) });
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
  if (queue) {
    Object.assign(state, explicitQueue(queue, key, explicitIndex));
  }
  else if (Number.isInteger(explicitIndex)) state.queueIndex = explicitIndex;
  // Playing a track that is not in the queue replaces the queue outright, so any restored window
  // is gone and there is nothing left to rebuild.
  else if (!state.queue.includes(key)) { state.queue = [key]; state.queueIndex = 0; state.queueTruncated = false; }
  else state.queueIndex = state.queue.indexOf(key);
  const track = await getTrack(key);
  if (state.current && state.current.key !== key && !state.current.qualified) api("/api/playback/events", { method: "POST", body: JSON.stringify({ key: state.current.key, event: "skipped" }) }).catch(() => {});
  state.current = { ...track, qualified: false, _retried: false };
  if (track.source.selected === false) {
    state.temporarySource = { ...track.source, temporary: true, trackCount: 1 };
    renderSources();
  }
  state.lyricsFollow = true; $("sync-lyrics").hidden = true;
  state.lyric = -1; audio.src = mediaUrl(track, "audio"); setBuffering(true);
  // Cache the playing track in the background so seeks, scrubs and the error retry below are
  // served from disk instead of reopening ranges against Telegram.
  api("/api/playback/cache-current", { method: "POST", body: JSON.stringify({ key }) }).catch(() => {});
  setTrackUi(); renderQueue(); schedulePrefetch();
  api("/api/playback/events", { method: "POST", body: JSON.stringify({ key, event: "started" }) }).catch(() => {});
  loadLyrics();
  schedulePersist();
  try { await startAudioPlayback(); } catch (error) { showError(error, () => startAudioPlayback().catch(() => {}), "Couldn’t play this track"); }
}

function detailRowsFor(track) {
  const metadata = track.metadata || {};
  const now = Math.floor(Date.now() / 1000);
  const disc = Number(metadata.discNumber) || 0;
  const number = Number(metadata.trackNumber) || 0;
  return [
    ["Source", track.source?.title],
    ["Album", metadata.album],
    ["Album artist", metadata.albumArtist],
    ["Genre", metadata.genre],
    ["Year", metadata.year || ""],
    ["Duration", track.durationMs ? formatTime(track.durationMs / 1000) : ""],
    ["Posted", track.sentAt ? formatPostedDate(track.sentAt, now) : ""],
    ["Track", number ? String(number) : ""],
    ["Disc", disc ? String(disc) : ""],
    ["Format", (track.file?.mimeType || "").replace(/^audio\//, "").toUpperCase()],
    ["File", track.file?.name],
    ["Size", track.file?.size ? `${(track.file.size / 1048576).toFixed(1)} MB` : ""],
  ].filter(([, value]) => value).map(([key, value]) => `<div><dt>${key}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
}

window.__renderDetailsForTest = (track) => { $("track-details").innerHTML = detailRowsFor(track); };

function setTrackUi() {
  const track = state.current;
  if (!track) { $("progress").disabled = true; return; }
  $("progress").disabled = false;
  const liked = trackLikedState(track.key);
  const metadata = track.metadata || {};
  const changed = lastUiTrackKey !== track.key;
  lastUiTrackKey = track.key;
  const title = metadata?.title || track.file?.name || "Untitled";
  const artist = metadata.artist || "Unknown artist";
  for (const id of ["player-title", "now-title"]) $(id).textContent = title;
  for (const id of ["player-artist", "now-artist"]) $(id).textContent = artist;
  $("label-stamp").textContent = title;
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
  $("like-current").classList.toggle("active", liked);
  $("like-current").querySelector("use").setAttribute("href", liked ? "#i-heart-filled" : "#i-heart");
  $("like-current").setAttribute("aria-pressed", String(liked));
  $("like-current").setAttribute("aria-label", liked ? "Unlike current track" : "Like current track");
  const detailRows = detailRowsFor(track);
  // Removing the speed control took the only write of detailRows with it, so the Details tab
  // rendered its action buttons over an empty <dl>. The rows are the tab's actual content.
  $("track-details").innerHTML = detailRows;
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
  document.querySelector(".label-disc")?.classList.toggle("is-playing", playing);
  $("play").classList.toggle("playing", playing);
  $("play").setAttribute("aria-busy", String(state.buffering));
  $("play").setAttribute("aria-pressed", String(playing));
  $("play").setAttribute("aria-label", state.buffering ? "Pause while loading" : playing ? "Pause" : "Play");
  const row = state.current && document.querySelector(`.track-row[data-track-key="${CSS.escape(state.current.key)}"]`);
  if (row) {
    row.classList.toggle("buffering", state.buffering);
    row.querySelector(".track-play-overlay").innerHTML = icon(playing ? "pause" : "play-filled");
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
  $("progress").setAttribute("aria-valuetext",
    `${formatTime(audio.currentTime)} of ${formatTime(duration)}${Number.isFinite(duration) && duration > 0 ? ` (${Math.round(played)}%)` : ""}`);
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

async function libraryQueue(shuffle, currentKey = "", windowed = true) {
  const body = {
    source: state.likedMode ? null : state.source || null,
    query: $("track-search").value.trim(), shuffle, currentKey,
    liked: state.likedMode,
    temporary: Boolean(state.temporarySource?.chatId === state.source),
  };
  if (windowed) {
    // The server returns a slice around currentKey (or the top of the library/shuffle when
    // there is no current track) instead of all 54,660 keys. move() rebuilds at the edges.
    body.windowBefore = 50;
    body.windowAfter = 300;
  }
  const result = await api("/api/playback/queue", {
    method: "POST",
    body: JSON.stringify(body),
  });
  const keys = Array.isArray(result?.keys) ? result.keys.filter((key) => typeof key === "string") : [];
  // The window is complete by definition, but the *library* it came from may be larger. The
  // truncated marker and total travel with the window so move() and the queue summary know.
  Object.assign(state, windowFromResult(keys, result?.total, result?.offset));
  return keys;
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
  state.queue = toggleShuffleQueue(keys, state.current?.key || "", enabled);
  state.queueIndex = state.current ? state.queue.indexOf(state.current.key) : -1;
  state.queueTotal = Math.max(state.queueTotal, state.queue.length);
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
  // A windowed queue holds only part of the playlist, so running past either edge is not the
  // end of it -- rebuild from the server before deciding to stop or wrap. A restored session
  // holds a window too, which is why the truncated marker travels with every snapshot.
  if ((next < 0 || next >= state.queue.length) && state.queueTruncated) {
    const keys = await libraryQueue(state.shuffle, state.current?.key || "");
    // The block condition already established the edge and truncation; libraryQueue has since
    // refreshed the window fields, so the gate is passed as the constant it was.
    const resolved = resolveWindowEdge(direction, state.queue, state.queueIndex, true, state.current?.key || "", { keys });
    if (resolved) {
      state.queue = resolved.queue; state.queueIndex = resolved.queueIndex;
      renderQueue();
      next = resolved.next;
    }
  }
  if (next >= state.queue.length) {
    if (state.repeat !== "all") return;
    if (state.shuffle) {
      state.queue = await libraryQueue(true, state.current?.key || "", false); next = 0;
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

const QUEUE_EMPTY = '<div class="queue-empty"><strong>Your queue is empty</strong><span>Choose a track or add one from its more menu.</span><button class="button" type="button" data-queue-browse>Browse library</button></div>';

function renderQueue() {
  const historyStart = Math.max(0, state.queueIndex - state.historyVisible);
  const visibleStart = historyStart;
  const visibleEnd = Math.min(state.queue.length, state.queueIndex + 101);
  const view = queueView(
    state.queue, state.queueIndex,
    state.queueTruncated ? state.queueTotal : null,
    state.queueTruncated ? state.queueOffset : 0,
  );
  $("queue-summary").textContent = view.summary;
  if ($("queue-pane").hidden) return;
  const visible = state.queue.slice(visibleStart, visibleEnd);
  const rows = visible.map((key, offset) => {
    const summary = state.summaryCache.get(key); const detail = state.trackCache.get(key); const index = visibleStart + offset;
    const title = summary?.title || detail?.metadata?.title || "Loading track…";
    const artist = summary?.artist || detail?.metadata?.artist || "";
    const section = index < state.queueIndex ? "Played" : index === state.queueIndex ? "Playing" : "Up next";
    return `<div class="queue-row ${index < state.queueIndex ? "played" : ""} ${index === state.queueIndex ? "current" : ""}" draggable="${index > state.queueIndex}" data-queue-index="${index}" data-queue-key="${escapeHtml(key)}"><button class="queue-copy" type="button" data-queue-play="${index}"><span class="queue-state">${section}</span><strong>${escapeHtml(title)}</strong><small>${escapeHtml(artist)}</small></button><span>${index > state.queueIndex ? `<span class="cache-state ${state.cacheStates[key] || ""}">${escapeHtml(state.cacheStates[key] || "queued")}</span><button class="icon-button" type="button" data-remove-queue="${index}" aria-label="Remove from queue">${icon("close")}</button>` : ""}</span></div>`;
  }).join("");
  // Rows or the empty state, never both: the old form concatenated them, so a one-track queue
  // showed a PLAYING row with "your queue is clear" underneath it.
  $("queue-list").innerHTML = view.isEmpty ? QUEUE_EMPTY : rows;
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
    return `<section class="discover-group"><h3>${labels[group]}</h3>${items.map((item) => {
      const counted = item.musicFileCount ?? (item.trackCount > 0 ? item.trackCount : null);
      // ponytail: counted-and-empty is indistinguishable from uncounted here; needs a real "counted" flag in the discover payload to separate them.
      return `<label class="discover-row ${item.pending ? "pending" : ""}"><img class="source-avatar" src="${item.avatarUrl}" data-avatar-fallback="${escapeHtml(initials(item.title))}" alt="" loading="lazy"><span class="discover-copy"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(sourceKindLabel(item.kind))} · ${counted === null ? "—" : `${counted.toLocaleString()} music files`}</small></span><input type="checkbox" data-chat="${item.chatId}" ${item.selected ? "checked" : ""} aria-label="Select ${escapeHtml(item.title)}"></label>`;
    }).join("")}</section>`;
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
  if (!ids.length || !await confirmAction("Remove from library?", "They’ll disappear from this player and stop syncing. Nothing will be deleted or left in Telegram.", "Remove from library")) return;
  try {
    await api("/api/sources/bulk-select", { method: "POST", body: JSON.stringify({ chatIds: ids, selected: false }) });
    state.selectedSources.clear(); state.bulk = false; $("bulk-bar").hidden = true; state.libraryCache.clear();
    if (ids.includes(state.source)) state.source = "";
    await loadLibrary(true); toast(ids.length === 1 ? "Source removed from library" : `${ids.length} sources removed from library`);
  } catch (error) { showError(error, () => unselectSources(ids)); }
}

function openMetadata(track = state.current) {
  if (!track) return; state.editing = track;
  for (const element of $("metadata-form").elements) if (element.name) element.value = track.metadata[element.name] || "";
  $("cover-quality").value = state.settings.coverQuality || "1200"; $("candidate-section").hidden = true; $("metadata-status").textContent = "";
  if (!$("metadata-dialog").open) $("metadata-dialog").showModal();
}

// All four metadata handlers used to funnel error.message straight into the status line, and
// fetchMetadata additionally left its skeleton and section up, so a rate-limited lookup showed
// a loading animation and an error at the same time, forever.
function metadataFailed(error) {
  $("candidate-list").innerHTML = "";
  $("candidate-section").hidden = true;
  $("metadata-status").textContent = errorCopy(error);
}

async function saveMetadata(event) {
  event.preventDefault(); const values = Object.fromEntries(new FormData(event.currentTarget));
  for (const field of ["year", "trackNumber", "discNumber"]) values[field] = Number(values[field]) || 0;
  try {
    const updated = await api(mediaUrl(state.editing, "metadata"), { method: "PATCH", body: JSON.stringify({ set: values, clear: [] }) });
    state.editing = updated; cacheSet(state.trackCache, updated.key, updated, 100); if (state.current?.key === updated.key) { state.current = { ...state.current, ...updated }; setTrackUi(); }
    state.libraryCache.clear(); await loadLibrary(true); $("metadata-status").textContent = "Saved locally. Downloads will use these tags.";
  } catch (error) { metadataFailed(error); }
}

async function resetMetadata() {
  if (!state.editing || !await confirmAction("Reset local metadata?", "Telegram’s original metadata will become visible again.", "Reset")) return;
  try { const updated = await api(mediaUrl(state.editing, "metadata"), { method: "PATCH", body: JSON.stringify({ set: {}, clear: Object.keys(state.editing.overrides) }) }); state.editing = updated; openMetadata(updated); state.libraryCache.clear(); await loadLibrary(true); }
  catch (error) { metadataFailed(error); }
}

async function fetchMetadata() {
  const button = $("fetch-metadata"); button.disabled = true; button.setAttribute("aria-busy", "true");
  $("metadata-status").textContent = "Searching MusicBrainz…";
  $("candidate-section").hidden = false; $("candidate-list").innerHTML = '<div class="list-skeleton"><span></span><span></span></div>';
  try {
    const candidates = await api(`${mediaUrl(state.editing, "metadata")}/search`, { method: "POST", body: "{}" });
    $("candidate-list").innerHTML = candidates.map((item) => `<article class="candidate-row">${item.coverUrl ? `<img class="candidate-cover" src="${mediaUrl(state.editing, `metadata/candidates/${encodeURIComponent(item.id)}/cover`)}" alt="" loading="lazy">` : '<div class="candidate-cover art-placeholder"><span></span></div>'}<div class="candidate-copy"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.artist)} · ${escapeHtml(item.album || "Single")} ${item.year || ""}</span></div><span class="utility">${item.score}%</span><button class="button" type="button" data-candidate="${escapeHtml(item.id)}">Use match</button></article>`).join("") || '<p class="small-copy">No close matches found.</p>';
    $("metadata-status").textContent = candidates.length ? `${candidates.length} matches found.` : "No close matches found.";
  } catch (error) { metadataFailed(error); }
  finally { button.disabled = false; button.removeAttribute("aria-busy"); }
}

async function applyCandidate(id) {
  try {
    const updated = await api(`${mediaUrl(state.editing, "metadata")}/apply`, { method: "POST", body: JSON.stringify({ candidateId: id, coverQuality: $("cover-quality").value }) });
    state.editing = updated; cacheSet(state.trackCache, updated.key, updated, 100); if (state.current?.key === updated.key) { state.current = { ...state.current, ...updated }; setTrackUi(); }
    openMetadata(updated); state.libraryCache.clear(); await loadLibrary(true); $("metadata-status").textContent = "Internet metadata applied locally.";
  } catch (error) { metadataFailed(error); }
}

function openLyricsEditor() { if (!state.current) return; $("lyrics-text").value = state.lyrics?.syncedText || state.lyrics?.plainText || ""; $("lyrics-status").textContent = ""; if (!$("lyrics-dialog").open) $("lyrics-dialog").showModal(); }
async function saveLyrics(event) { event.preventDefault(); try { state.lyrics = await api(mediaUrl(state.current, "lyrics"), { method: "PUT", body: JSON.stringify({ text: $("lyrics-text").value }) }); renderLyrics(); $("lyrics-status").textContent = "Lyrics saved."; } catch (error) { $("lyrics-status").textContent = error.message; } }

function showPanel(tab = "lyrics", toggle = false) {
  if (toggle && !$("now-panel").hidden && document.querySelector(`#${tab}-tab.active`)) return closePanel();
  const switching = !document.querySelector(`#${tab}-tab.active`);
  // Re-opening mid-close would otherwise keep the exit animation and fade straight back out.
  clearTimeout(panelCloseTimer);
  const panel = $("now-panel");
  // "Was it visually absent" -- hidden, or still playing its exit. Not just .hidden, because
  // closePanel only hides after the animation ends, so an interrupted close is still visible.
  const wasAway = panel.hidden || panel.classList.contains("is-closing");
  panel.classList.remove("is-closing");
  panel.hidden = false; $("app-shell").classList.add("panel-open");
  // Re-showing within the same frame does not restart the entry animation: the browser sees the
  // same animation-name still applied and keeps the old clock, so interrupting a close reopened
  // the panel already ~60% faded in. Removing the animation, flushing style, then letting it
  // reapply is what actually rewinds it. Only when the panel was away -- a plain tab switch on
  // an open panel must not replay the entrance.
  if (wasAway) {
    panel.classList.add("is-restarting");
    void panel.offsetWidth;
    panel.classList.remove("is-restarting");
  }
  for (const name of ["lyrics", "queue", "details"]) { const active = name === tab; $(`${name}-pane`).hidden = !active; $(`${name}-tab`).classList.toggle("active", active); $(`${name}-tab`).setAttribute("aria-selected", String(active)); }
  const pane = $(`${tab}-pane`); pane.classList.remove("pane-entering"); requestAnimationFrame(() => pane.classList.add("pane-entering"));
  if (tab === "queue") renderQueue();
  // All three panes share one scroll container, so a deep offset from a long lyric sheet
  // carried over to a short Details pane and left it opened mid-scroll. Only on a real tab
  // change, so re-opening the panel on the current tab keeps the lyric you were reading.
  if (switching) $("now-content").scrollTop = 0;
  // Switching tabs swaps the scroll container's content, so re-derive the header state from
  // the new scrollTop instead of leaving it collapsed over a short pane.
  updateNowHeader();
  schedulePersist();
}
// Play the exit animation before hiding, so the panel leaves the way it arrived instead of
// disappearing between frames. The grid column collapses at the same time, so the surface and
// the layout move together. animationend can be missed (reduced motion, a backgrounded tab),
// so a timeout guarantees the panel still ends up hidden.
let panelCloseTimer = 0;
function closePanel() {
  const panel = $("now-panel");
  if (panel.hidden) return;
  $("app-shell").classList.remove("panel-open");
  schedulePersist();
  const finish = () => { clearTimeout(panelCloseTimer); panel.classList.remove("is-closing"); panel.hidden = true; };
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return finish();
  panel.classList.add("is-closing");
  panel.addEventListener("animationend", finish, { once: true });
  clearTimeout(panelCloseTimer);
  panelCloseTimer = setTimeout(finish, 400);
}

// Collapse the now-playing header once you scroll into the content. Two thresholds rather
// than one: a single boundary sits exactly where collapsing changes scrollHeight, so the
// header would oscillate. Collapse at 48px, expand only back under 12px.
//
// The thresholds alone are not enough. The header is a flex sibling of the scroller, so
// collapsing it hands its freed height to .now-content: clientHeight grows and the maximum
// scrollTop shrinks by the same amount. On a short pane (Details on a phone) that maximum
// drops under 12px, the browser clamps scrollTop to it, and the clamp reads as "scrolled to
// the top" -- so we expand, the pane becomes scrollable again, momentum pushes past 48 and
// it collapses once more. That feedback loop is the juddering in the Details tab.
//
// So require the pane to still be scrollable after collapsing -- but subtract only the height
// the collapse actually frees, not the whole header. Subtracting the whole header was a bound
// so conservative it was never satisfiable: measured at 1440x900 with lyrics loaded,
// 636 - 206 - 546 = -116, so the collapse could not fire at all. The collapsed header is a
// measured, CSS-pinned 132px (120px at <=860px), read from --compact-header so the two cannot
// drift; freed = 546 - 132 = 414 leaves +16px of real overflow, which clears the 12px floor.
function updateNowHeader() {
  const header = document.querySelector(".now-header");
  const content = $("now-content");
  if (!header || !content) return;
  const compact = header.classList.contains("is-compact");
  const compactHeight = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--compact-header")) || 132;
  const next = shouldCompactHeader({
    scrollTop: content.scrollTop,
    scrollHeight: content.scrollHeight,
    clientHeight: content.clientHeight,
    headerHeight: header.offsetHeight,
    compactHeight,
    compact,
  });
  if (next !== compact) setNowHeaderCompact(header, next);
}

// The collapse switches the header between two grid layouts, so the art and the title land in
// new places at new sizes. FLIP that: measure both elements, toggle the class, measure again,
// then animate the delta with transform/scale so the art appears to glide beside the title
// rather than cutting to it. Transform-only, so nothing reflows mid-animation.
const NOW_HEADER_MORPH = [".large-art-wrap", ".now-title"];
const NOW_HEADER_FLIP_ANIMATIONS = new WeakMap();
function setNowHeaderCompact(header, compact) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    header.classList.toggle("is-compact", compact);
    return;
  }
  const targets = NOW_HEADER_MORPH.map((selector) => header.querySelector(selector)).filter(Boolean);
  const before = targets.map((element) => element.getBoundingClientRect());
  header.classList.toggle("is-compact", compact);
  targets.forEach((element, index) => {
    const first = before[index];
    const last = element.getBoundingClientRect();
    if (!first.width || !last.width || !first.height || !last.height) return;
    const dx = first.left - last.left;
    const dy = first.top - last.top;
    const sx = first.width / last.width;
    const sy = first.height / last.height;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1 && Math.abs(sx - 1) < 0.01 && Math.abs(sy - 1) < 0.01) return;
    NOW_HEADER_FLIP_ANIMATIONS.get(element)?.cancel();
    const flip = element.animate(
      [{ transformOrigin: "top left", transform: `translate(${dx}px, ${dy}px) scale(${sx}, ${sy})` },
       { transformOrigin: "top left", transform: "none" }],
      { duration: 280, easing: "cubic-bezier(.2,.8,.2,1)" },
    );
    NOW_HEADER_FLIP_ANIMATIONS.set(element, flip);
  });
}

function openMenu(actions, x, y) {
  const menu = $("context-menu"); menu.innerHTML = actions.map((item, index) => `<button class="${item.danger ? "danger" : ""}" type="button" role="menuitem" data-menu-index="${index}">${escapeHtml(item.label)}</button>`).join("");
  // Re-opening while the previous menu is still fading would inherit the exit state.
  clearTimeout(menuCloseTimer); menu.classList.remove("is-leaving");
  menu.hidden = false; menu._actions = actions;
  menu._returnFocus = document.activeElement;
  menu.style.left = `${Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8))}px`; menu.style.top = `${Math.max(8, Math.min(y, innerHeight - menu.offsetHeight - 8))}px`; menu.querySelector("button")?.focus();
}
// Popovers get a quick fade on the way out too, so dismissing does not blink. Kept short
// (--dur-1) because a context menu should feel instant, not animated at.
let menuCloseTimer = 0;
function closeMenu() {
  const menu = $("context-menu");
  if (menu.hidden) return;
  const returnFocus = menu._returnFocus;
  menu._returnFocus = null;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { menu.hidden = true; }
  else {
    menu.classList.add("is-leaving");
    menuCloseTimer = setTimeout(() => { menu.classList.remove("is-leaving"); menu.hidden = true; }, 120);
  }
  // Keyboard users opened this menu from somewhere; send focus back to it when it closes,
  // whether they picked an item or dismissed with Escape/click-outside.
  if (returnFocus?.isConnected) returnFocus.focus();
}

async function playFromLibrary(key) {
  // Fetch a window around the picked track rather than the whole library: the window is what
  // the queue pane draws and what prefetch reads, and move() rebuilds at the edges.
  const keys = await libraryQueue(false, key);
  if (!keys.includes(key)) keys.unshift(key);
  state.shuffle = false; updateModes();
  state.queue = keys;
  return playKey(key);
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
  const narrow = matchMedia("(max-width: 860px)").matches;
  const durationMs = summary?.durationMs ?? detail?.durationMs;
  const liked = trackLikedState(key);
  openMenu([
    { label: "Play", action: () => source.selected === false ? playKey(key, state.globalTracks.map((item) => item.key)) : playFromLibrary(key) },
    ...(narrow ? [
      { label: liked ? "Unlike" : "Like", action: () => toggleRowLike(key) },
      { label: `Duration ${formatTime(durationMs / 1000)}`, action: null },
    ] : []),
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
    { label: "Remove from library", danger: true, action: () => unselectSources([chatId]) },
  ], x, y);
}

function updateRowLikeUi(key, liked) {
  document.querySelectorAll(`[data-row-like-key="${CSS.escape(key)}"]`).forEach((button) => {
    button.classList.toggle("active", liked);
    button.setAttribute("aria-pressed", String(liked));
    button.setAttribute("aria-label", `${liked ? "Unlike" : "Like"} ${button.getAttribute("aria-label")?.replace(/^(Unlike|Like) /, "") || "track"}`);
    button.querySelector("use")?.setAttribute("href", liked ? "#i-heart-filled" : "#i-heart");
  });
}

function trackRepresentations(key) {
  return representationsFor(key, {
    tracks: state.tracks, globalTracks: state.globalTracks,
    summaryCache: state.summaryCache, trackCache: state.trackCache, current: state.current,
  });
}

function trackLikedState(key, representations = trackRepresentations(key)) {
  return likedState(key, {
    pending: rowLikeOperations.get(key), current: state.current,
    trackCache: state.trackCache, summaryCache: state.summaryCache, representations,
  });
}

async function toggleTrackLike(key, { notify = false } = {}) {
  const representations = trackRepresentations(key);
  if (!representations.length) return;
  const previous = trackLikedState(key, representations);
  const operation = beginLikeOperation(rowLikeOperations, key, previous);
  const requested = operation.desired;
  const applyLiked = (liked) => {
    trackRepresentations(key).forEach((track) => { track.liked = liked; });
    updateRowLikeUi(key, liked);
    if (state.current?.key === key) setTrackUi();
    renderSources();
  };
  state.likedCount += requested ? 1 : -1;
  applyLiked(requested);
  try {
    const updated = await api(`/api/tracks/${encodeURIComponent(key)}/like`, { method: "PATCH", body: JSON.stringify({ liked: requested }) });
    const canonical = typeof updated?.liked === "boolean" ? updated.liked : requested;
    const resolved = resolveLikeResponse(rowLikeOperations, key, operation, canonical);
    if (!resolved.applied) return;
    if (canonical !== requested) state.likedCount += canonical ? 1 : -1;
    applyLiked(canonical);
    if (notify) { toast(canonical ? "Added to Liked Songs" : "Removed from Liked Songs"); schedulePersist(); }
    if (state.likedMode) loadLibrary(true);
  } catch (error) {
    const baseline = rollbackLikeOperation(rowLikeOperations, key, operation);
    if (baseline === null) return;
    if (operation.baseline !== operation.desired) state.likedCount += operation.baseline ? 1 : -1;
    applyLiked(baseline);
    showError(error);
  }
}

async function toggleRowLike(key) {
  return toggleTrackLike(key);
}

async function toggleLike() {
  if (!state.current) return;
  return toggleTrackLike(state.current.key, { notify: true });
}

async function saveCurrentToTelegram() {
  if (!state.current) return;
  const button = $("save-current-telegram"); button.disabled = true; button.setAttribute("aria-busy", "true");
  try { await api(`${mediaUrl(state.current, "saved-messages")}`, { method: "POST" }); toast("Sent to Saved Messages"); }
  catch (error) { showError(error, saveCurrentToTelegram); }
  finally { button.disabled = false; button.removeAttribute("aria-busy"); }
}

// Telegram's frequent-forward peers, best first. Capped at 5: this is a single scannable row of
// shortcuts, and a longer one would just be the full list again in a different order.
const FREQUENT_LIMIT = 5;

function frequentContacts() {
  return state.contacts
    .filter((contact) => typeof contact.forwardRank === "number")
    .sort((a, b) => b.forwardRank - a.forwardRank)
    .slice(0, FREQUENT_LIMIT);
}

function renderFrequentContacts() {
  const searching = $("contact-search").value.trim() !== "";
  const frequent = frequentContacts();
  // Hide the shortcut row while searching: the filtered list below is the answer to the query,
  // and keeping unrelated faces pinned above it competes with the actual results.
  const show = frequent.length > 0 && !searching;
  $("frequent-contacts").hidden = !show;
  if (!show) { $("frequent-list").innerHTML = ""; return; }
  $("frequent-list").innerHTML = frequent.map((contact) => `<button class="frequent-contact" type="button" data-contact="${contact.id}" title="${escapeHtml(contact.name)}"><img class="source-avatar" src="${contact.avatarUrl}" data-avatar-fallback="${escapeHtml(initials(contact.name))}" alt="" loading="lazy"><span class="frequent-name">${escapeHtml(contact.name)}</span></button>`).join("");
}

function renderContacts() {
  const query = $("contact-search").value.trim().toLocaleLowerCase();
  const contacts = state.contacts.filter((contact) => `${contact.name} ${contact.username || ""}`.toLocaleLowerCase().includes(query));
  renderFrequentContacts();
  const typed = $("contact-search").value.trim();
  $("share-status").textContent = typed ? `${contacts.length} ${contacts.length === 1 ? "match" : "matches"}` : "";
  $("contact-list").innerHTML = contacts.map((contact) => `<button class="contact-row" type="button" data-contact="${contact.id}"><img class="source-avatar" src="${contact.avatarUrl}" data-avatar-fallback="${escapeHtml(initials(contact.name))}" alt="" loading="lazy"><span><strong>${escapeHtml(contact.name)}</strong><small>${contact.username ? `@${escapeHtml(contact.username)}` : "Your cloud storage"}</small></span></button>`).join("") || '<p class="empty-copy">No contacts found.</p>';
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
  if (pendingShare) { clearTimeout(pendingShare.timer); clearInterval(pendingShare.tick); }
  const pending = { cancelled: false };
  pending.timer = setTimeout(async () => {
    clearInterval(pending.tick);
    if (pending.cancelled) return;
    pendingShare = null;
    try { await api(`/api/tracks/${encodeURIComponent(key)}/share`, { method: "POST", body: JSON.stringify({ recipientId }) }); toast("Shared on Telegram"); }
    catch (error) { showError(error); }
  }, 5000);
  pendingShare = pending;
  // The label was a fixed string, so the undo window read "5 seconds" for its whole life while
  // only the timer bar moved. Retitle it each second so the number matches the bar.
  const deadline = Date.now() + 5000;
  const undo = { label: "Undo", run: () => { pending.cancelled = true; clearTimeout(pending.timer); clearInterval(pending.tick); pendingShare = null; toast("Share cancelled"); } };
  toast("Sharing in 5 seconds…", undo, 5000);
  pending.tick = setInterval(() => {
    const left = Math.round((deadline - Date.now()) / 1000);
    // Stop if the toast was replaced by another message, so we never retitle someone else's.
    if (pending.cancelled || left <= 0 || $("toast").hidden) return clearInterval(pending.tick);
    $("toast-message").textContent = `Sharing in ${left} second${left === 1 ? "" : "s"}…`;
  }, 250);
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
    const result = await api(`/api/tracks/${encodeURIComponent(state.current.key)}/position?source=${encodeURIComponent(state.source)}&temporary=${temporary}&sort=${encodeURIComponent(state.sort)}`);
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

// The Account pane held nothing but a disconnect link, so it gave no reason to visit and no
// confirmation of which account the library belongs to.
function renderAccount(account) {
  const linked = account && account.linked;
  $("settings-account-name").textContent = linked ? (account.displayName || "Telegram account") : "Not connected";
  const parts = [];
  if (linked && account.userId) parts.push(`ID ${account.userId}`);
  if (Array.isArray(state.sources)) {
    const count = state.sources.length;
    parts.push(`${count} ${count === 1 ? "source" : "sources"} indexed`);
  }
  $("settings-account-meta").textContent = parts.join(" · ");
}

async function openSettings() {
  $("settings-dialog").showModal();
  try {
    state.settings = await api("/api/settings"); $("prefetch-count").value = state.settings.prefetchCount; $("musicbrainz-contact").value = state.settings.musicbrainzContact; $("default-cover-quality").value = state.settings.coverQuality;
    const cache = await api("/api/cache/status"); $("cache-usage").textContent = `${cache.files} songs cached · ${(cache.bytes / 1048576).toFixed(1)} MB`;
    const [network, auth, status] = await Promise.all([api("/api/network"), api("/api/auth/status"), api("/api/status")]);
    state.network = network;
    state.passwordEnabled = auth.passwordEnabled;
    renderAccount(status.telegram);
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
  $("bind-host-help").textContent = "This machine only keeps the player private. Anyone on my network lets other devices reach it — including anyone else on the same Wi-Fi.";

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
    notice.textContent = "This setting needs a restart to take effect.";
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

// Settings apply as you change them, the way Theme and Type always have. The three "Save"
// buttons meant some settings took effect instantly and others waited for a click, with nothing
// on screen saying which -- so a typed value could be silently discarded by closing the dialog.
let settingsSaveTimer = null;
async function commitSettings(values) {
  try {
    state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify(values) });
  } catch (error) {
    showError(error, () => commitSettings(values));
  }
}

// Text inputs save on a debounce so a PATCH is not issued per keystroke; selects and numbers
// commit immediately on change.
function saveSettingsSoon(values, delay = 600) {
  clearTimeout(settingsSaveTimer);
  settingsSaveTimer = setTimeout(() => { settingsSaveTimer = null; commitSettings(values); }, delay);
}

// Only flush when a debounce is actually pending. Firing on every blur meant tabbing through the
// contact field wrote to the database without the user changing anything.
function flushSettings() {
  if (settingsSaveTimer === null) return;
  clearTimeout(settingsSaveTimer);
  settingsSaveTimer = null;
  commitSettings(currentSettingsValues());
}

function currentSettingsValues() {
  return {
    prefetchCount: Number($("prefetch-count").value),
    musicbrainzContact: $("musicbrainz-contact").value.trim(),
    coverQuality: $("default-cover-quality").value,
  };
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

$("country-search").addEventListener("input", () => {
  // Typing always reopens the list and re-filters, so the field never looks stuck after a pick.
  if (selectedCountry) { selectedCountry = null; $("telegram-country").value = ""; $("dial-prefix").textContent = "+"; }
  $("country-clear").hidden = !$("country-search").value;
  renderCountryOptions($("country-search").value);
});
$("country-search").addEventListener("focus", () => { if (!state.countriesLoaded) return; renderCountryOptions(selectedCountry ? "" : $("country-search").value); });
$("country-search").addEventListener("keydown", (event) => {
  const open = !$("country-listbox").hidden;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (!open) return renderCountryOptions(selectedCountry ? "" : $("country-search").value);
    return moveCountryActive(event.key === "ArrowDown" ? 1 : -1);
  }
  if (event.key === "Enter" && open && countryActive >= 0) {
    // Only swallow Enter when a suggestion is highlighted, so the form can still submit.
    event.preventDefault();
    return selectCountry(countryMatches[countryActive]);
  }
  if (event.key === "Escape" && open) { event.preventDefault(); event.stopPropagation(); return closeCountryList(); }
  if (event.key === "Tab" && open && countryActive >= 0) selectCountry(countryMatches[countryActive], { silent: true });
});
$("country-search").addEventListener("blur", () => {
  // Leaving a half-typed query in the box would imply a choice that was never made, so either
  // restore the selected country's label or empty the field entirely.
  setTimeout(() => {
    if (document.activeElement?.closest("#country-combo")) return;
    const typed = $("country-search").value.trim();
    // A query that narrows to exactly one country is unambiguous, so commit it rather than
    // discard what the user typed.
    if (!selectedCountry && typed && countryMatches.length === 1) return selectCountry(countryMatches[0], { silent: true });
    closeCountryList();
    if (selectedCountry) $("country-search").value = `${selectedCountry.flag} ${selectedCountry.name}`;
    else if (typed) clearCountry({ focus: false });
  }, 0);
});
$("country-listbox").addEventListener("mousedown", (event) => {
  const option = event.target.closest(".combo-option");
  if (!option) return;
  event.preventDefault(); // keep focus in the field so blur does not fight the selection
  selectCountry(countryMatches[Number(option.dataset.index)]);
});
$("country-listbox").addEventListener("mousemove", (event) => {
  const option = event.target.closest(".combo-option");
  if (option && Number(option.dataset.index) !== countryActive) { countryActive = Number(option.dataset.index); syncCountryActive(); }
});
$("country-clear").addEventListener("click", () => clearCountry());

$("phone-form").addEventListener("submit", submitPhone);
$("code-form").addEventListener("submit", submitPhoneCode);
$("twofa-form").addEventListener("submit", submitPhonePassword);

// Re-arms the QR flow too, since abandoning a phone login leaves no active flow to poll.
function restartPhoneLogin() { state.flow = ""; $("telegram-code").value = ""; $("telegram-password").value = ""; startQr("Choose your country and send a new code."); }
$("change-number").addEventListener("click", restartPhoneLogin);
$("change-number-2fa").addEventListener("click", restartPhoneLogin);

document.querySelector('[data-source=""]').addEventListener("click", () => selectSource(""));
$("source-list").addEventListener("click", (event) => {
  const menu = event.target.closest("[data-source-menu]");
  if (menu) {
    const source = state.sources.find((item) => item.chatId === menu.dataset.sourceMenu);
    if (!source) return;
    const box = menu.getBoundingClientRect();
    // Labels, not glyphs: the old rescan button drew a repeat loop, which reads as "repeat".
    // pinSource(chatId) takes one argument and toggles from source.pinnedAt itself.
    return openMenu([
      { label: "Sync new tracks", action: () => syncSource(source.chatId, false) },
      { label: "Full rescan", action: () => syncSource(source.chatId, true) },
      { label: source.pinnedAt ? "Unpin from top" : "Pin to top", action: () => pinSource(source.chatId) },
    ], box.left, box.bottom + 4);
  }
  const checkbox = event.target.closest("[data-bulk-source]");
  if (checkbox) { checkbox.checked ? state.selectedSources.add(checkbox.dataset.bulkSource) : state.selectedSources.delete(checkbox.dataset.bulkSource); return renderSources(); }
  const temporary = event.target.closest("[data-temporary-source]");
  if (temporary && !state.bulk) return selectTemporary();
  const row = event.target.closest("[data-source]");
  if (row && !state.bulk) selectSource(row.dataset.source);
});
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

$("track-list").addEventListener("click", (event) => { const play = event.target.closest("[data-play-key]"); const menu = event.target.closest("[data-track-menu]"); const like = event.target.closest("[data-row-like-key]"); if (play) playFromLibrary(play.dataset.playKey).catch(showError); if (menu) { const rect = menu.getBoundingClientRect(); trackMenu(menu.dataset.trackMenu, rect.right, rect.bottom); } if (like) { event.stopPropagation(); toggleRowLike(like.dataset.rowLikeKey).catch(showError); } });
$("track-list").addEventListener("focusin", (event) => { const row = event.target.closest(".track-row"); if (row?.dataset.trackIndex != null) state.rowFocusIndex = parseInt(row.dataset.trackIndex, 10); });
$("track-list").addEventListener("keydown", (event) => {
  const row = event.target.closest(".track-row:not(.track-placeholder)");
  if (!row || row.dataset.trackIndex == null) return;
  const index = parseInt(row.dataset.trackIndex, 10);
  if (event.key === "Enter") {
    const play = row.querySelector("[data-play-key]");
    if (play) { event.preventDefault(); play.click(); }
    return;
  }
  if (event.key === "ArrowDown") { event.preventDefault(); focusTrackRow(index + 1); return; }
  if (event.key === "ArrowUp") { event.preventDefault(); focusTrackRow(index - 1); return; }
  if (event.key === "Home") { event.preventDefault(); focusTrackRow(0); return; }
  if (event.key === "End") { event.preventDefault(); focusTrackRow(state.totalTracks - 1); return; }
  // PageUp/PageDown: one viewport of rows.
  if (event.key === "PageDown") { event.preventDefault(); focusTrackRow(index + Math.max(1, Math.floor($("library").clientHeight / trackRowHeight()))); return; }
  if (event.key === "PageUp") { event.preventDefault(); focusTrackRow(index - Math.max(1, Math.floor($("library").clientHeight / trackRowHeight()))); return; }
});
$("track-sort").addEventListener("change", (event) => {
  state.sort = event.target.value;
  state.libraryCache.clear();
  loadLibrary(true).catch(showError);
});
document.querySelector(".track-head").addEventListener("click", (event) => {
  const cell = event.target.closest("[data-sort]");
  if (!cell) return;
  $("track-sort").value = cell.dataset.sort;
  $("track-sort").dispatchEvent(new Event("change"));
});
$("track-list").addEventListener("contextmenu", (event) => { const row = event.target.closest("[data-track-key]"); if (row) { event.preventDefault(); trackMenu(row.dataset.trackKey, event.clientX, event.clientY); } });
$("track-list").addEventListener("error", (event) => { if (event.target.matches(".row-art")) { event.target.classList.remove("is-ready"); event.target.nextElementSibling?.classList.remove("is-covered"); } }, true);
// The row may be replaced by an innerHTML re-render between load and the rAF
// callback, which detaches the image and leaves nextElementSibling null.
// Hold the image in a local: the browser clears event.target once dispatch finishes, so
// reading it inside the rAF threw on every single thumbnail and the fade-in never ran.
$("track-list").addEventListener("load", (event) => { const image = event.target; if (image.matches(".row-art")) requestAnimationFrame(() => { image.classList.add("is-ready"); image.nextElementSibling?.classList.add("is-covered"); }); }, true);
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
  $("volume").setAttribute("aria-valuetext", `${Math.round(audio.volume * 100)} percent`);
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
audio.addEventListener("error", () => {
  setBuffering(false);
  const track = state.current;
  if (!track) return;
  if (track._retried) {
    showError(new AppError("This track couldn’t be streamed. Try syncing its source."), () => syncSource(track.chatId, false));
    return;
  }
  // One automatic resume-from-position retry before the dialog: transient Telegram hiccups
  // usually clear, and the cache-current job may have just written the bytes that failed.
  track._retried = true;
  const at = Number.isFinite(audio.currentTime) && audio.currentTime > 0 ? audio.currentTime : 0;
  audio.src = `${mediaUrl(track, "audio")}?retry=${Date.now()}`;
  if (at) audio.addEventListener("loadedmetadata", () => { try { audio.currentTime = at; } catch {} }, { once: true });
  audio.play().catch(() => {
    if (state.current?.key === track.key) {
      showError(new AppError("This track couldn’t be streamed. Try syncing its source."), () => syncSource(track.chatId, false));
    }
  });
});
audio.addEventListener("seeked", schedulePersist);
if ("mediaSession" in navigator) { navigator.mediaSession.setActionHandler("play", () => startAudioPlayback().catch(showError)); navigator.mediaSession.setActionHandler("pause", () => audio.pause()); navigator.mediaSession.setActionHandler("previoustrack", () => move(-1).catch(showError)); navigator.mediaSession.setActionHandler("nexttrack", () => move(1).catch(showError)); }

$("queue-list").addEventListener("click", (event) => { const play = event.target.closest("[data-queue-play]"); const button = event.target.closest("[data-remove-queue]"); if (event.target.closest("[data-queue-browse]")) { closePanel(); return $("track-list").querySelector(".track-row")?.focus(); } if (play) playKey(state.queue[Number(play.dataset.queuePlay)], null, Number(play.dataset.queuePlay)).catch(showError); if (button) { state.queue.splice(Number(button.dataset.removeQueue), 1); renderQueue(); schedulePrefetch(); schedulePersist(); } }); $("queue-list").addEventListener("dragstart", (event) => { draggedQueue = Number(event.target.closest("[data-queue-index]")?.dataset.queueIndex); event.target.closest("[data-queue-index]")?.classList.add("queue-dragging"); }); $("queue-list").addEventListener("dragover", (event) => event.preventDefault()); $("queue-list").addEventListener("drop", (event) => { event.preventDefault(); document.querySelector(".queue-dragging")?.classList.remove("queue-dragging"); const target = Number(event.target.closest("[data-queue-index]")?.dataset.queueIndex); if (Number.isInteger(draggedQueue) && Number.isInteger(target) && draggedQueue !== target && draggedQueue > state.queueIndex && target > state.queueIndex) { const [key] = state.queue.splice(draggedQueue, 1); state.queue.splice(target, 0, key); renderQueue(); schedulePrefetch(); schedulePersist(); } }); $("queue-list").addEventListener("dragend", (event) => { event.target.closest("[data-queue-index]")?.classList.remove("queue-dragging"); }); $("clear-queue").addEventListener("click", () => { state.queue = state.current ? [state.current.key] : []; state.queueIndex = state.current ? 0 : -1; renderQueue(); schedulePrefetch(); schedulePersist(); });

$("player-open").addEventListener("click", () => showPanel("lyrics")); $("player-locate").addEventListener("click", () => locateCurrent()); $("show-lyrics").addEventListener("click", () => showPanel("lyrics", true)); $("close-now").addEventListener("click", closePanel); for (const tab of ["lyrics", "queue", "details"]) $(`${tab}-tab`).addEventListener("click", () => showPanel(tab));
$("now-panel").querySelector('[role="tablist"]').addEventListener("keydown", (e) => {
  const tabs = [...e.currentTarget.querySelectorAll('[role="tab"]')]; const i = tabs.indexOf(document.activeElement); if (i === -1) return;
  if (e.key === "ArrowRight") { e.preventDefault(); tabs[(i + 1) % tabs.length].click(); tabs[(i + 1) % tabs.length].focus(); }
  if (e.key === "ArrowLeft") { e.preventDefault(); tabs[(i - 1 + tabs.length) % tabs.length].click(); tabs[(i - 1 + tabs.length) % tabs.length].focus(); }
});
$("lyrics-lines").addEventListener("click", (event) => { const line = event.target.closest("[data-lyric]"); if (line) { audio.currentTime = state.lyrics.lines[Number(line.dataset.lyric)].startMs / 1000; state.lyricsFollow = true; $("sync-lyrics").hidden = true; line.classList.remove("seek-pulse"); void line.offsetWidth; line.classList.add("seek-pulse"); } }); $("now-content").addEventListener("scroll", updateNowHeader, { passive: true });
$("now-panel").addEventListener("wheel", stopFollowingLyrics, { passive: true }); $("now-panel").addEventListener("touchmove", stopFollowingLyrics, { passive: true }); $("sync-lyrics").addEventListener("click", () => { state.lyricsFollow = true; $("sync-lyrics").hidden = true; state.lyric = -2; updateLyric(); }); $("add-lyrics-empty").addEventListener("click", openLyricsEditor); $("edit-current").addEventListener("click", () => openMetadata()); $("edit-lyrics").addEventListener("click", openLyricsEditor);
$("like-current").addEventListener("click", () => toggleLike().catch((error) => showError(error, toggleLike))); $("save-current-telegram").addEventListener("click", saveCurrentToTelegram); $("share-current").addEventListener("click", openShare); $("player-more").addEventListener("click", (event) => { if (!state.current) return; const rect = event.currentTarget.getBoundingClientRect(); trackMenu(state.current.key, rect.right, rect.top); });
$("contact-search").addEventListener("input", renderContacts);
// Both lists share one handler: the shortcut row and the full list carry the same data-contact
// contract, so a click means the same thing wherever it lands.
for (const id of ["contact-list", "frequent-list"]) {
  $(id).addEventListener("click", (event) => { const contact = event.target.closest("[data-contact]"); if (contact) queueShare(contact.dataset.contact); });
}
$("metadata-form").addEventListener("submit", saveMetadata); $("reset-metadata").addEventListener("click", resetMetadata); $("fetch-metadata").addEventListener("click", fetchMetadata); $("candidate-list").addEventListener("click", (event) => { const button = event.target.closest("[data-candidate]"); if (button) applyCandidate(button.dataset.candidate); }); $("lyrics-form").addEventListener("submit", saveLyrics); $("reset-lyrics").addEventListener("click", async () => { if (await confirmAction("Fetch lyrics again?", "Saved lyrics will be replaced by a new internet lookup.", "Fetch again")) { try { $("lyrics-status").textContent = "Looking for lyrics…"; state.lyrics = await api(mediaUrl(state.current, "lyrics"), { method: "DELETE" }); renderLyrics(); $("lyrics-status").textContent = "Lyrics lookup finished."; } catch (error) { $("lyrics-status").textContent = error.message; } } });

$("open-settings").addEventListener("click", openSettings); document.querySelectorAll("[data-settings-tab]").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll("[data-settings-tab]").forEach((item) => item.classList.toggle("active", item === button)); document.querySelectorAll("[data-settings-pane]").forEach((pane) => { pane.hidden = pane.dataset.settingsPane !== button.dataset.settingsTab; }); })); $("prefetch-count").addEventListener("change", () => commitSettings(currentSettingsValues()));
$("sleep-timer").addEventListener("change", () => setSleepTimer($("sleep-timer").value));
$("default-cover-quality").addEventListener("change", () => commitSettings(currentSettingsValues()));
$("musicbrainz-contact").addEventListener("input", () => saveSettingsSoon(currentSettingsValues()));
// Leaving the field flushes the pending debounce, so closing the dialog straight after typing
// cannot lose the value.
$("musicbrainz-contact").addEventListener("blur", flushSettings);
$("test-musicbrainz").addEventListener("click", async () => { try { state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify({ musicbrainzContact: $("musicbrainz-contact").value.trim(), coverQuality: $("default-cover-quality").value }) }); await api("/api/settings/musicbrainz/test", { method: "POST" }); toast("MusicBrainz connection works"); } catch (error) { showError(error, () => $("test-musicbrainz").click()); } });
$("clear-cache").addEventListener("click", async () => { if (await confirmAction("Clear prefetched songs?", "Playback metadata and Telegram files will not be changed.", "Clear cache")) { try { await api("/api/cache", { method: "DELETE" }); $("cache-usage").textContent = "0 songs cached · 0 MB"; toast("Prefetched songs cleared"); } catch (error) { showError(error); } } });
document.querySelectorAll("[data-setting] [data-value]").forEach((button) => button.addEventListener("click", () => { localStorage.setItem(`tm-${button.parentElement.dataset.setting}`, button.dataset.value); applyPreferences(); }));
$("disconnect-telegram").addEventListener("click", async () => { if (await confirmAction("Disconnect Telegram?", "This signs out the stored Telegram session and clears the local library. It does not leave or delete any Telegram chats.", "Disconnect")) { try { await api("/api/telegram/session", { method: "DELETE" }); location.reload(); } catch (error) { showError(error); } } });

$("error-retry").addEventListener("click", () => { $("error-dialog").close(); const action = retryAction; retryAction = null; action?.(); }); $("confirm-accept").addEventListener("click", () => { $("confirm-dialog").close(); confirmResolve?.(true); confirmResolve = null; });
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => { const dialog = $(button.dataset.close); dialog.close(); if (dialog.id === "confirm-dialog") { confirmResolve?.(false); confirmResolve = null; } }));
document.querySelectorAll("dialog").forEach((dialog) => { dialog.addEventListener("click", (event) => { if (event.target !== dialog) return; const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) { dialog.close(); if (dialog.id === "confirm-dialog") { confirmResolve?.(false); confirmResolve = null; } } }); dialog.addEventListener("cancel", () => { if (dialog.id === "confirm-dialog") { confirmResolve?.(false); confirmResolve = null; } }); });
document.addEventListener("error", (event) => { const image = event.target; if (image.matches?.("img.source-avatar")) { const replacement = document.createElement("span"); replacement.className = "source-avatar"; replacement.textContent = image.dataset.avatarFallback || "♪"; image.replaceWith(replacement); } }, true);
$("context-menu").addEventListener("keydown", (event) => {
  const items = [...$("context-menu").querySelectorAll("[data-menu-index]")];
  if (!items.length) return;
  const index = items.indexOf(document.activeElement);
  if (event.key === "Tab") {
    // Focus trap: the menu is a plain div, not a dialog, so Tab would escape it.
    event.preventDefault();
    const next = items[(index + (event.shiftKey ? -1 : 1) + items.length) % items.length];
    next.focus();
    return;
  }
  if (event.key === "ArrowDown") { event.preventDefault(); items[(index + 1) % items.length].focus(); return; }
  if (event.key === "ArrowUp") { event.preventDefault(); items[(index - 1 + items.length) % items.length].focus(); return; }
  if (event.key === "Home") { event.preventDefault(); items[0].focus(); return; }
  if (event.key === "End") { event.preventDefault(); items[items.length - 1].focus(); return; }
});
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
