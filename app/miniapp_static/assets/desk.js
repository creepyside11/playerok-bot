const desk = document.querySelector("#desk");
const drawer = document.querySelector("#drawer");
const toast = document.querySelector("#toast");
const telegram = window.Telegram?.WebApp;
const initData = telegram?.initData || "";
const backButton = telegram?.isVersionAtLeast?.("6.1") ? telegram.BackButton : null;

const illustrations = {
  overview: ["Сводка", "Сводка магазина", "grid"],
  chats: ["Чаты", "Чаты Playerok", "chat"],
  sales: ["Продажи", "Продажи", "bag"],
  plugins: ["Плагины", "Плагины", "plug"],
  accounts: ["Аккаунты", "Аккаунты", "user"],
};
const drawings = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  chat: '<path d="M21 11.5a8.379 8.379 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.379 8.379 0 0 1-3.8-.9L3 21l1.9-5.7a8.379 8.379 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.379 8.379 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z"/><path d="M8.5 11.5h7"/>',
  bag: '<path d="M6 7h12l2 13H4L6 7Z"/><path d="M9 9V6a3 3 0 0 1 6 0v3"/>',
  plug: '<path d="M9 3v5m6-5v5M6 8h12v3a6 6 0 0 1-12 0V8Zm6 9v4"/>',
  user: '<circle cx="12" cy="8" r="3.5"/><path d="M4.5 20v-1.4A4.6 4.6 0 0 1 9.1 14h5.8a4.6 4.6 0 0 1 4.6 4.6V20"/>',
  wallet: '<rect x="3" y="6" width="18" height="14" rx="2"/><path d="M3 8V6a2 2 0 0 1 2-2h13m3 8h-5a2 2 0 0 0 0 4h5"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Zm-8 12a2 2 0 0 0 4 0"/>',
  refresh: '<path d="M20 7v5h-5M4.5 16h.1A8 8 0 1 0 5 7m-1 10v-5h5"/>',
  right: '<path d="m9 6 6 6-6 6"/>',
  back: '<path d="M20 12H4m7-7-7 7 7 7"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  search: '<circle cx="10.5" cy="10.5" r="5.5"/><path d="m15 15 4 4"/>',
  send: '<path d="m3 11 18-8-8 18-2.4-7.6L3 11Zm7.6 2.4L21 3"/>',
  external: '<path d="M13 5h6v6M19 5l-8 8m6 0v6H5V7h6"/>',
  settings: '<path d="M4 7h16M4 12h16M4 17h16"/><circle cx="8" cy="7" r="2" fill="currentColor"/><circle cx="15" cy="12" r="2" fill="currentColor"/><circle cx="10" cy="17" r="2" fill="currentColor"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
};
const statuses = {
  PAID: ["Оплачено", "teal"], PENDING: ["В работе", "amber"], SENT: ["Выполнено", "teal"],
  CONFIRMED: ["Завершено", "green"], CONFIRMED_AUTOMATICALLY: ["Завершено", "green"],
  ROLLED_BACK: ["Возврат", "red"], APPROVED: ["В продаже", "green"],
  DRAFT: ["Черновик", ""], PENDING_APPROVAL: ["На проверке", "amber"],
  PENDING_MODERATION: ["На проверке", "amber"], BLOCKED: ["Заблокирован", "red"],
  DECLINED: ["Отклонён", "red"], EXPIRED: ["Истёк", ""], SOLD: ["Продан", "teal"],
};
const notificationTitles = {
  new_message: "Новые сообщения", new_deal: "Новые сделки", deal_status: "Статусы сделок",
  new_review: "Отзывы", errors: "Ошибки подключения",
};
const chatFilters = [["", "Все"], ["PM", "Покупатели"], ["SUPPORT", "Поддержка"], ["NOTIFICATIONS", "Уведомления"]];
const listingFilters = [["", "Все"], ["APPROVED", "В продаже"], ["DRAFT", "Черновики"], ["SOLD", "Проданы"]];
const number = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const clock = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const day = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" });

let bootstrap = null;
let currentAccount = null;
let tab = "overview";
let panel = null;
let panelError = null;
let panelSequence = 0;
let threadSequence = 0;
let chatType = "";
let salesTab = "deals";
let listingType = "";
let filterText = "";
let pluginFilter = "";
let currentChat = null;
let chatHistory = null;
let chatError = null;
let replyDraft = "";
let toastTimer = 0;

function icon(name, extra = "") {
  return `<svg class="icon ${extra}" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${drawings[name] || drawings.info}</svg>`;
}

function safe(value = "") {
  return String(value ?? "").replace(/[&<>"']/g, (letter) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[letter]);
}

function image(src, alt = "") {
  return `<img loading="lazy" decoding="async" referrerpolicy="no-referrer" src="${safe(src)}" alt="${safe(alt)}">`;
}

function avatar(name, src) {
  const letters = String(name || "?").slice(0, 2).toUpperCase();
  return `<span class="avatar" aria-hidden="true">${src ? image(src) : safe(letters)}</span>`;
}

function brand() {
  return '<div class="brand"><img src="/assets/mark.webp" width="40" height="40" alt=""><div class="brand-copy"><strong>Emerald Desk</strong><small>Playerok</small></div></div>';
}

function rubles(amount) {
  return amount === null || amount === undefined || !Number.isFinite(Number(amount)) ? "—" : `${number.format(amount)} ₽`;
}

function count(value) {
  return value === null || value === undefined ? "—" : number.format(value);
}

function dateTime(value, detailed = false) {
  const date = new Date(value || "");
  if (Number.isNaN(date.getTime())) return "";
  if (detailed) return `${day.format(date)} · ${clock.format(date)}`;
  return date.toDateString() === new Date().toDateString() ? clock.format(date) : day.format(date);
}

function status(value) {
  const [text, color] = statuses[value] || ["Статус неизвестен", ""];
  return `<span class="status ${color}">${text}</span>`;
}

function path(section = "") {
  return `/api/accounts/${encodeURIComponent(currentAccount.id)}${section}`;
}

async function request(url, { method = "GET", body } = {}) {
  const headers = { Authorization: `tma ${initData}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Не удалось получить данные.");
  return data;
}

function notify(text, error = false) {
  toast.textContent = text;
  toast.classList.toggle("err", error);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3600);
  if (!error) telegram?.HapticFeedback?.notificationOccurred("success");
}

function nav() {
  return Object.entries(illustrations).map(([key, [name, , glyph]]) =>
    `<button class="nav-button" data-action="nav" data-value="${key}" aria-current="${key === tab ? "page" : "false"}">
      ${icon(glyph)}<span>${name}</span>
    </button>`
  ).join("");
}

function placeholder(title, description = "", glyph = "info", button = "") {
  return `<div class="placeholder">${icon(glyph)}<strong>${safe(title)}</strong>
    ${description ? `<p>${safe(description)}</p>` : ""}${button}</div>`;
}

function button(label, action, { value = "", glyph = "", variant = "" } = {}) {
  return `<button type="button" class="button ${variant}" data-action="${action}" data-value="${safe(value)}">
    ${glyph ? icon(glyph) : ""}<span>${safe(label)}</span></button>`;
}

function iconButton(label, action, glyph, className = "") {
  return `<button type="button" class="icon-button ${className}" data-action="${action}" aria-label="${safe(label)}" title="${safe(label)}">${icon(glyph)}</button>`;
}

function hero(section, label = "", action = "") {
  return `<header class="hero ${section}"><div class="hero-copy">
    <div class="kicker">${safe(currentAccount ? `@${currentAccount.username}` : "Emerald Desk")}</div>
    <h1>${illustrations[section][1]}</h1>${label ? `<p>${safe(label)}</p>` : ""}${action}
  </div></header>`;
}

function skeleton() {
  return '<div class="skeleton" aria-label="Загрузка" aria-busy="true"><span></span><span></span><span></span><span></span><span></span></div>';
}

function metric(label, value, glyph) {
  return `<div class="metric"><div class="metric-top"><i>${icon(glyph)}</i><span>${label}</span></div><strong>${safe(value)}</strong></div>`;
}

function title(label, action = "", actionLabel = "", value = "") {
  return `<div class="section-title"><h2>${label}</h2>${action
    ? `<button class="section-link" data-action="${action}" data-value="${value}">${actionLabel}${icon("right")}</button>` : ""}</div>`;
}

function quick(label, note, glyph, action, value = "") {
  return `<button class="quick-row" data-action="${action}" data-value="${safe(value)}">
    <span class="quick-icon">${icon(glyph)}</span>
    <span class="row-text"><strong>${label}</strong><small>${safe(note)}</small></span>${icon("right", "caret")}
  </button>`;
}

function dealRows(list) {
  return list.map((entry) => `<button class="row-button" data-action="deal" data-value="${safe(entry.id)}"
    data-search="${safe(`${entry.item?.name || ""} ${entry.buyer || ""}`)}">
      ${avatar(entry.item?.name || "Лот", entry.item?.cover)}
      <span class="row-text"><strong>${safe(entry.item?.name || "Сделка")}</strong>
       <small>${safe([entry.buyer ? `@${entry.buyer}` : "", dateTime(entry.time)].filter(Boolean).join(" · "))}</small></span>
      <span class="row-right"><strong>${rubles(entry.amount)}</strong>${status(entry.status)}</span>
    </button>`).join("");
}

function overview() {
  const openChats = currentAccount ? button("Открыть чаты", "nav", { value: "chats", glyph: "chat", variant: "primary" }) : "";
  const cover = hero("overview", "", openChats);
  if (!panel) return cover + pending();
  return `${cover}
    <div class="metrics">
      ${metric("Доступный баланс", rubles(panel.balance), "wallet")}
      ${metric("Лоты в продаже", count(panel.lots), "layers")}
      ${metric("Непрочитанные чаты", count(panel.unread), "chat")}
    </div>
    ${panel.partial ? `<div class="sync-info">${icon("info")}Часть данных Playerok временно недоступна.</div>` : ""}
    <div class="dashboard-grid">
      <section>${title("Последние сделки", "nav", "Все сделки", "sales")}
        <div class="panel">${panel.deals.length ? dealRows(panel.deals) : placeholder("Сделок пока нет", "", "bag")}</div>
      </section>
      <section>${title("Управление")}<div class="quick-list">
        ${quick("Плагины", panel.plugins === null ? "Настройки плагинов" : `Активных: ${count(panel.plugins)}`, "plug", "nav", "plugins")}
        ${quick("Уведомления", "Настроить события бота", "bell", "nav", "accounts")}
      </div></section>
    </div>`;
}

function tabs(options, selected, action, label) {
  return `<div class="tabs" role="group" aria-label="${label}">${options.map(([key, name]) =>
    `<button data-action="${action}" data-value="${key}" aria-pressed="${key === selected}">${name}</button>`
  ).join("")}</div>`;
}

function search(label) {
  return `<label class="search">${icon("search")}
    <input type="search" id="screen-search" value="${safe(filterText)}" aria-label="${label}" placeholder="${label}" autocomplete="off"></label>`;
}

function more(action) {
  return `<div class="more">${button(action === "older" ? "Ранее" : "Загрузить ещё", action, { variant: "flat" })}</div>`;
}

function chatRows(list) {
  return list.map((chat) => `<button class="row-button" data-action="chat" data-value="${safe(chat.id)}"
    aria-pressed="${chat.id === currentChat}" data-search="${safe(`${chat.title} ${chat.last?.text || ""}`)}">
      ${avatar(chat.title, chat.avatar)}
      <span class="row-text"><strong>${safe(chat.title)}</strong>
       <small>${safe(chat.last?.text || (chat.last ? (chat.last.images?.length ? "Вложение" : "Событие Playerok") : "Диалог"))}</small></span>
      <span class="row-right"><small>${dateTime(chat.last?.time)}</small>${chat.unread ? `<span class="unread">${count(chat.unread)}</span>` : ""}</span>
    </button>`).join("");
}

function bubble(message) {
  const system = Boolean(message.event && !message.author);
  return `<article class="bubble ${system ? "system" : message.mine ? "mine" : ""}">
    ${message.text ? `<p>${safe(message.text)}</p>` : ""}
    ${(message.images || []).map((src) => image(src, "Вложение Playerok")).join("")}
    ${!message.text && !message.images?.length ? "<p>Событие Playerok</p>" : ""}
    <time>${dateTime(message.time, true)}</time></article>`;
}

function thread() {
  if (!currentChat) return placeholder("Выберите диалог", "Здесь появится история сообщений.", "chat");
  if (!chatHistory) {
    return `<div class="thread-head">${iconButton("К списку диалогов", "chatBack", "back")}<strong>Диалог</strong></div>
      ${chatError ? placeholder("Чат недоступен", chatError, "info", button("Повторить", "refreshChat")) : skeleton()}`;
  }
  const chat = chatHistory.chat;
  return `<div class="thread-head">${iconButton("К списку диалогов", "chatBack", "back", "mobile-return")}
    ${avatar(chat.title, chat.avatar)}<div class="row-text"><strong>${safe(chat.title)}</strong><small>Диалог Playerok</small></div>
    ${iconButton("Обновить сообщения", "refreshChat", "refresh")}</div>
    <div id="thread-body" class="thread-body">
      ${chatHistory.cursor ? more("older") : ""}${chatHistory.items.map(bubble).join("")}
      ${!chatHistory.items.length ? placeholder("История пока пуста", "", "chat") : ""}
    </div>
    ${chat.canReply ? `<form class="composer" id="reply-form">
      <textarea name="text" rows="1" maxlength="2000" required aria-label="Ответ покупателю" placeholder="Ответить…">${safe(replyDraft)}</textarea>
      <button type="submit" class="button primary" aria-label="Отправить" title="Отправить">${icon("send")}</button></form>`
    : '<p class="composer-note">В этом чате ответы недоступны.</p>'}`;
}

function pending() {
  return panelError
    ? `<div class="panel">${placeholder("Данные недоступны", panelError, "info", button("Обновить", "reload"))}</div>`
    : skeleton();
}

function chats() {
  return `${hero("chats")}
    <div class="toolbar">${tabs(chatFilters, chatType, "chatType", "Тип чата")}${search("Найти диалог")}</div>
    ${panel || currentChat ? `<div class="panel inbox ${currentChat ? "in-thread" : ""}">
      <div class="inbox-list">
        ${panel ? (panel.items.length ? chatRows(panel.items) : placeholder("Диалогов нет", "", "chat")) : pending()}
        ${panel?.cursor ? more("page") : ""}
      </div>
      <div class="thread">${thread()}</div>
     </div>` : pending()}`;
}

function listingRows(list) {
  return list.map((entry) => {
    const inner = `${avatar(entry.name, entry.cover)}<span class="row-text"><strong>${safe(entry.name)}</strong></span>
      <span class="row-right"><strong>${rubles(entry.price)}</strong>${status(entry.status)}</span>`;
    const data = `class="listing-row" data-search="${safe(entry.name)}"`;
    return entry.slug
      ? `<button ${data} data-action="listing" data-value="${safe(entry.slug)}" title="Открыть на Playerok">${inner}${icon("external", "caret")}</button>`
      : `<div ${data}>${inner}</div>`;
  }).join("");
}

function sales() {
  const bar = `<div class="toolbar">${tabs([["deals", "Сделки"], ["listings", "Лоты"]], salesTab, "salesTab", "Раздел продаж")}${search("Поиск по товарам")}</div>`;
  const filters = salesTab === "listings" ? tabs(listingFilters, listingType, "listingType", "Статус лота") : "";
  return `${hero("sales")}${bar}${filters}${panel ? `<div class="panel">
    ${panel.items.length ? (salesTab === "deals" ? dealRows(panel.items) : listingRows(panel.items)) : placeholder("Здесь пока пусто", "", "bag")}
    ${panel.cursor ? more("page") : ""}
  </div>` : pending()}`;
}

function toggle(label, checked, action, value) {
  return `<button type="button" class="switch" role="switch" aria-label="${safe(label)}" aria-checked="${Boolean(checked)}"
    data-action="${action}" data-value="${safe(value)}"></button>`;
}

function pluginCard(plugin) {
  return `<article class="panel plugin-card" data-search="${safe(plugin.name + " " + plugin.description)}" data-enabled="${plugin.enabled}">
    <div class="plugin-head"><span class="plugin-icon">${icon("plug")}</span>
      <div class="row-text"><strong>${safe(plugin.name)}</strong><small>v${safe(plugin.version)}</small></div>
      ${toggle(plugin.name, plugin.enabled, "togglePlugin", plugin.id)}
    </div>
    <p class="plugin-description">${safe(plugin.description)}</p>
    <div class="plugin-foot">
      <span class="plugin-state ${plugin.enabled ? "on" : ""}"><span class="signal ${plugin.enabled ? "live" : ""}"></span>
      ${plugin.enabled ? "Включён" : "Выключен"}</span>
      ${button("Настройки", "plugin", { value: plugin.id, glyph: "settings" })}
    </div></article>`;
}

function plugins() {
  const active = panel?.items.filter((plugin) => plugin.enabled).length;
  return `${hero("plugins", panel ? `Включено: ${active} из ${panel.items.length}` : "")}
    <div class="toolbar">${tabs([["", "Все"], ["active", "Включённые"]], pluginFilter, "pluginFilter", "Фильтр плагинов")}${search("Найти плагин")}</div>
    ${panel ? `<div class="plugin-grid">${panel.items.length ? panel.items.map(pluginCard).join("") : placeholder("Плагины не установлены", "Добавьте их через каталог в боте.", "plug")}</div>` : pending()}`;
}

function accounts() {
  if (!panel) return hero("accounts") + pending();
  return `${hero("accounts")}
    <div class="account-grid">
      <section>${title("Подключённые аккаунты")}
        <div class="panel">${bootstrap.accounts.map((account) =>
          `<button class="row-button" data-action="selectAccount" data-value="${safe(account.id)}">
          ${avatar(account.username)}<span class="row-text"><strong>@${safe(account.username)}</strong></span>
          ${account.id === currentAccount.id ? '<span class="status green">Активный</span>' : icon("right", "caret")}</button>`
        ).join("")}</div>
      </section>
      <section>${title("Уведомления бота")}
        <div class="panel">${Object.entries(notificationTitles).map(([key, label]) =>
          `<div class="preference"><span>${label}</span>${toggle(label, panel[key], "toggleNotification", key)}</div>`
        ).join("")}</div>
      </section>
    </div>`;
}

const pages = { overview, chats, sales, plugins, accounts };

function updateFilters() {
  const query = filterText.trim().toLocaleLowerCase("ru");
  const elements = [...desk.querySelectorAll("[data-search]")];
  for (const entry of elements) {
    const enabled = tab !== "plugins" || !pluginFilter || entry.dataset.enabled === "true";
    entry.hidden = !enabled || !entry.dataset.search.toLocaleLowerCase("ru").includes(query);
  }
  const noMatches = desk.querySelector("#no-matches");
  if (noMatches) noMatches.hidden = !elements.length || elements.some((entry) => !entry.hidden);
}

function render() {
  const accountName = currentAccount?.username || "Аккаунт";
  const content = currentAccount
    ? pages[tab]()
    : hero("overview") + `<div class="panel">${placeholder("Подключите Playerok", "Откройте «Аккаунты» в Telegram-боте и добавьте аккаунт. Затем обновите панель.", "user", button("Обновить", "boot"))}</div>`;
  desk.innerHTML = `<aside class="rail">${brand()}<nav class="nav" aria-label="Разделы">${nav()}</nav>
    <div class="rail-foot"><span class="signal ${currentAccount ? "live" : ""}"></span>${currentAccount ? "Аккаунт выбран" : "Подключите аккаунт"}</div></aside>
    <div class="shell"><main class="workspace">
      <div class="topbar">${brand()}<span class="topbar-label">Панель продавца</span><div class="top-actions">
      ${iconButton("Обновить экран", "reload", "refresh")}
      <button class="account-pill" data-action="selectDrawer" title="Выбрать аккаунт">${avatar(accountName)}
        <span class="account-name">${safe(currentAccount ? `@${accountName}` : accountName)}</span>${icon("down")}</button>
      </div></div><div class="content ${tab === "chats" ? `chats-content ${currentChat ? "in-thread" : ""}` : ""}">
      ${content}
      <div id="no-matches" class="panel" hidden>${placeholder("Ничего не найдено", "Измените поиск или фильтр.", "search")}</div>
      </div></main></div><nav class="bottom-nav" aria-label="Разделы">${nav()}</nav>`;
  desk.setAttribute("aria-busy", "false");
  updateFilters();
}

function launch(text, canRetry) {
  desk.innerHTML = `<main class="launch-panel">${brand()}${hero("overview")}
    <section class="panel">${placeholder("Откройте панель из Telegram", text, "info", canRetry ? button("Повторить", "boot") : "")}</section>
    </main>`;
  desk.setAttribute("aria-busy", "false");
}

function listPath() {
  if (tab === "chats") return ["/chats", "type", chatType];
  if (tab === "sales" && salesTab === "listings") return ["/listings", "status", listingType];
  if (tab === "sales") return ["/deals"];
  if (tab === "accounts") return ["/notifications"];
  return [`/${tab}`];
}

function panelUrl(next = null) {
  const [section, name, value] = listPath();
  const query = new URLSearchParams();
  if (value) query.set(name, value);
  if (next) query.set("cursor", next);
  return path(section) + (query.size ? `?${query}` : "");
}

async function loadPanel({ fresh = false } = {}) {
  if (!currentAccount) return boot();
  const sequence = ++panelSequence;
  panelError = null;
  if (fresh) panel = null;
  render();
  try {
    const result = await request(panelUrl());
    if (sequence !== panelSequence) return;
    panel = result;
  } catch (error) {
    if (sequence !== panelSequence) return;
    panel = null;
    panelError = error.message;
  }
  render();
}

async function boot() {
  if (!initData) {
    launch("Запустите Emerald Desk кнопкой «Панель продавца» в Telegram-боте.", false);
    return;
  }
  try {
    const result = await request("/api/bootstrap");
    bootstrap = result;
    currentAccount = result.accounts.find((account) => account.id === result.activeAccountId);
    if (currentAccount) await loadPanel({ fresh: true });
    else render();
  } catch (error) {
    launch(error.message, true);
  }
}

function closeThread() {
  currentChat = null;
  chatHistory = null;
  chatError = null;
  replyDraft = "";
  threadSequence += 1;
  backButton?.hide();
}

function go(section) {
  if (!bootstrap || !Object.hasOwn(illustrations, section)) return;
  closeThread();
  tab = currentAccount ? section : "overview";
  filterText = "";
  window.scrollTo(0, 0);
  if (currentAccount) loadPanel({ fresh: true });
  else render();
}

async function readPage(trigger) {
  if (!panel?.cursor) return;
  const sequence = panelSequence;
  trigger.disabled = true;
  try {
    const result = await request(panelUrl(panel.cursor));
    if (sequence !== panelSequence) return;
    const known = new Set(panel.items.map((entry) => entry.id));
    panel = { ...result, items: [...panel.items, ...result.items.filter((entry) => !known.has(entry.id))] };
    render();
  } catch (error) {
    notify(error.message, true);
  } finally {
    trigger.disabled = false;
  }
}

function threadUrl(suffix = "") {
  return path(`/chats/${encodeURIComponent(currentChat)}${suffix}`);
}

function scrollThread(mode = "end", oldHeight = 0) {
  const container = document.querySelector("#thread-body");
  if (container) container.scrollTop = mode === "end" ? container.scrollHeight : container.scrollHeight - oldHeight;
}

async function openChat(id, { mark = true } = {}) {
  if (tab !== "chats") {
    tab = "chats";
    chatType = "";
    panel = null;
    filterText = "";
    loadPanel({ fresh: true });
  }
  if (id !== currentChat) {
    chatHistory = null;
    replyDraft = "";
  }
  currentChat = id;
  const sequence = ++threadSequence;
  chatError = null;
  backButton?.show();
  render();
  try {
    const result = await request(threadUrl());
    if (sequence !== threadSequence) return;
    chatHistory = { ...result, items: [...result.items].reverse() };
    render();
    scrollThread();
    if (mark && (result.chat.unread || panel?.items.find((entry) => entry.id === id)?.unread)) {
      request(threadUrl("/read"), { method: "POST" }).then(() => {
        if (sequence !== threadSequence) return;
        const row = panel?.items.find((entry) => entry.id === id);
        if (row) row.unread = 0;
        const listRow = [...desk.querySelectorAll("[data-action=chat]")].find((element) => element.dataset.value === id);
        listRow?.querySelector(".unread")?.remove();
      }).catch(() => {});
    }
  } catch (error) {
    if (sequence !== threadSequence) return;
    chatError = error.message;
    if (chatHistory) notify(chatError, true);
    render();
  }
}

async function older(trigger) {
  if (!chatHistory?.cursor) return;
  const sequence = threadSequence;
  const oldHeight = document.querySelector("#thread-body")?.scrollHeight || 0;
  trigger.disabled = true;
  try {
    const next = `?${new URLSearchParams({ cursor: chatHistory.cursor })}`;
    const result = await request(threadUrl(next));
    if (sequence !== threadSequence) return;
    const seen = new Set(chatHistory.items.map((entry) => entry.id));
    chatHistory = {
      ...chatHistory, cursor: result.cursor,
      items: [...result.items.filter((entry) => !seen.has(entry.id)).reverse(), ...chatHistory.items],
    };
    render();
    scrollThread("older", oldHeight);
  } catch (error) {
    notify(error.message, true);
  } finally {
    trigger.disabled = false;
  }
}

function showDrawer(heading, body) {
  drawer.innerHTML = `<div class="drawer-head"><h2>${safe(heading)}</h2>${iconButton("Закрыть", "closeDrawer", "close")}</div><div class="drawer-body">${body}</div>`;
  if (!drawer.open) drawer.showModal();
}

function switchDrawer() {
  if (!bootstrap?.accounts.length) return;
  showDrawer("Аккаунты Playerok", `<div class="panel">${bootstrap.accounts.map((account) =>
    `<button class="row-button" data-action="selectAccount" data-value="${safe(account.id)}">
     ${avatar(account.username)}<span class="row-text"><strong>@${safe(account.username)}</strong></span>
     ${account.id === currentAccount.id ? icon("check") : icon("right", "caret")}</button>`
  ).join("")}</div>`);
}

async function chooseAccount(id, trigger) {
  const account = bootstrap.accounts.find((entry) => entry.id === id);
  if (!account) return;
  trigger.disabled = true;
  try {
    await request(`/api/accounts/${encodeURIComponent(id)}/active`, { method: "PATCH" });
    currentAccount = account;
    drawer.close();
    go("overview");
  } catch (error) {
    notify(error.message, true);
  } finally {
    trigger.disabled = false;
  }
}

function dealDrawer(id) {
  const deal = (tab === "overview" ? panel?.deals : panel?.items)?.find((entry) => entry.id === id);
  if (!deal) return;
  showDrawer(deal.item?.name || "Сделка", `<div class="detail-rows">
    <div><span>Сумма</span><strong>${rubles(deal.amount)}</strong></div>
    <div><span>Статус</span>${status(deal.status)}</div>
    <div><span>Покупатель</span><strong>${safe(deal.buyer ? `@${deal.buyer}` : "—")}</strong></div>
    <div><span>Создана</span><strong>${dateTime(deal.time, true) || "—"}</strong></div></div>
    <div class="detail-buttons">${deal.chatId ? button("Перейти в чат", "dealChat", { value: deal.chatId, glyph: "chat", variant: "primary" }) : ""}
    ${deal.item?.slug ? button("Открыть лот", "listing", { value: deal.item.slug, glyph: "external" }) : ""}</div>`);
}

function pluginField(field) {
  const name = safe(field.key);
  const label = safe(field.label);
  if (field.secret) {
    return `<label class="field">${label}<input type="password" name="${name}" placeholder="${field.configured ? "Сохранено, введите новое значение" : "Не задано"}"
       autocomplete="new-password" maxlength="8192"></label>`;
  }
  if (field.type === "bool") {
    return `<label class="field inline"><span>${label}</span><input type="checkbox" name="${name}" ${field.value ? "checked" : ""}></label>`;
  }
  if (field.type === "choice") {
    const options = field.choices.map((choice) =>
      `<option value="${safe(choice)}" ${choice === field.value ? "selected" : ""}>${safe(choice)}</option>`
    ).join("");
    return `<label class="field">${label}<select name="${name}">${field.value == null ? '<option value="">Выберите значение</option>' : ""}${options}</select></label>`;
  }
  if (["int", "float"].includes(field.type)) {
    return `<label class="field">${label}<input type="number" name="${name}" step="${field.type === "int" ? "1" : "any"}" value="${safe(field.value)}"></label>`;
  }
  if (String(field.value || "").length > 60 || /template|prompt|instruction/.test(field.key)) {
    return `<label class="field">${label}<textarea name="${name}" rows="3" maxlength="8192">${safe(field.value)}</textarea></label>`;
  }
  return `<label class="field">${label}<input name="${name}" type="text" value="${safe(field.value)}" maxlength="8192" autocomplete="off"></label>`;
}

function pluginDrawer(id) {
  const plugin = panel?.items.find((entry) => entry.id === id);
  if (!plugin) return;
  const controls = plugin.fields.length ? plugin.fields.map(pluginField).join("") : "<p>У плагина нет дополнительных параметров.</p>";
  showDrawer(plugin.name, `<p>${safe(plugin.description)}</p>
    <form id="plugin-form" data-plugin="${safe(plugin.id)}">
    <label class="field inline"><span>Плагин включён</span><input name="pluginEnabled" type="checkbox" ${plugin.enabled ? "checked" : ""}></label>
    ${controls}<p class="form-error" role="alert" hidden></p><div class="form-actions">
      ${button("Отмена", "closeDrawer", { variant: "flat" })}<button type="submit" class="button primary">Сохранить</button>
    </div></form>`);
}

async function updatePlugin(id, patch) {
  const account = currentAccount.id;
  const result = await request(path(`/plugins/${encodeURIComponent(id)}`), { method: "PATCH", body: patch });
  if (tab === "plugins" && currentAccount.id === account && panel) {
    panel.items = panel.items.map((entry) => entry.id === id ? result : entry);
    render();
  }
  notify("Настройки сохранены");
}

async function flip(target, id, type) {
  const before = target.getAttribute("aria-checked") === "true";
  const account = currentAccount.id;
  target.disabled = true;
  try {
    if (type === "togglePlugin") await updatePlugin(id, { enabled: !before });
    else {
      const result = await request(path("/notifications"), { method: "PATCH", body: { [id]: !before } });
      if (tab === "accounts" && currentAccount.id === account) {
        panel = result;
        render();
      }
      notify("Уведомления сохранены");
    }
  } catch (error) {
    notify(error.message, true);
  } finally {
    target.disabled = false;
  }
}

function listingLink(slug) {
  if (!slug) return;
  const url = `https://playerok.com/products/${encodeURIComponent(slug)}`;
  if (telegram?.openLink) telegram.openLink(url);
  else window.open(url, "_blank", "noopener,noreferrer");
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const { action, value } = target.dataset;
  if (action === "boot") boot();
  if (action === "nav") go(value);
  if (action === "reload") {
    if (tab === "chats" && currentChat) openChat(currentChat, { mark: false });
    else loadPanel();
  }
  if (action === "selectDrawer") switchDrawer();
  if (action === "selectAccount") chooseAccount(value, target);
  if (action === "page") readPage(target);
  if (action === "older") older(target);
  if (action === "deal") dealDrawer(value);
  if (action === "listing") listingLink(value);
  if (action === "dealChat") {
    drawer.close();
    openChat(value);
  }
  if (action === "chat") openChat(value);
  if (action === "refreshChat") openChat(currentChat, { mark: false });
  if (action === "chatBack") {
    closeThread();
    render();
  }
  if (action === "chatType") {
    chatType = value;
    closeThread();
    loadPanel({ fresh: true });
  }
  if (action === "salesTab") {
    salesTab = value;
    loadPanel({ fresh: true });
  }
  if (action === "listingType") {
    listingType = value;
    loadPanel({ fresh: true });
  }
  if (action === "pluginFilter") {
    pluginFilter = value;
    render();
  }
  if (action === "plugin") pluginDrawer(value);
  if (action === "togglePlugin" || action === "toggleNotification") flip(target, value, action);
  if (action === "closeDrawer") drawer.close();
});

document.addEventListener("input", (event) => {
  if (event.target.id === "screen-search") {
    filterText = event.target.value;
    updateFilters();
  }
  if (event.target.closest("#reply-form")) replyDraft = event.target.value;
});

document.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector("[type=submit]");
  if (!submit || submit.disabled) return;
  submit.disabled = true;
  try {
    if (form.id === "reply-form") {
      const text = form.elements.text.value.trim();
      if (!text) return;
      const sequence = threadSequence;
      const result = await request(threadUrl("/reply"), { method: "POST", body: { text } });
      if (sequence !== threadSequence) return;
      replyDraft = "";
      chatHistory.items.push(result);
      const row = panel?.items.find((entry) => entry.id === currentChat);
      if (row) row.last = result;
      render();
      scrollThread();
      notify("Сообщение отправлено");
    }
    if (form.id === "plugin-form") {
      const plugin = panel?.items.find((entry) => entry.id === form.dataset.plugin);
      if (!plugin) return;
      const settings = {};
      for (const field of plugin.fields) {
        const input = form.elements.namedItem(field.key);
        if (field.secret && !input.value) continue;
        let value = field.type === "bool" ? input.checked : input.value;
        if (["int", "float"].includes(field.type)) {
          if (!input.value) continue;
          value = input.valueAsNumber;
        }
        if (value !== field.value) settings[field.key] = value;
      }
      const checked = form.elements.pluginEnabled.checked;
      const patch = { settings };
      if (checked !== plugin.enabled) patch.enabled = checked;
      if (Object.keys(settings).length || "enabled" in patch) await updatePlugin(plugin.id, patch);
      drawer.close();
    }
  } catch (error) {
    const feedback = form.querySelector(".form-error");
    if (feedback) {
      feedback.textContent = error.message;
      feedback.hidden = false;
    } else notify(error.message, true);
  } finally {
    submit.disabled = false;
  }
});

telegram?.ready?.();
telegram?.expand?.();
if (telegram?.isVersionAtLeast?.("6.1")) {
  telegram.setHeaderColor?.("#101511");
  telegram.setBackgroundColor?.("#101511");
}
if (telegram?.isVersionAtLeast?.("7.10")) telegram.setBottomBarColor?.("#151d17");
backButton?.onClick(() => {
  if (drawer.open) drawer.close();
  else if (currentChat) {
    closeThread();
    render();
  }
});

boot();
