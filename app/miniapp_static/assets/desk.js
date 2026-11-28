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
  plus: '<path d="M12 5v14M5 12h14"/>',
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
let latestBalance = null;
let payoutOptions = null;
let editedListing = null;
let listingWizard = null;
let publicationQuote = null;
let telegramPanel = null;
let smmPanel = null;
let smmTab = "rules";
let smmCatalog = null;
let smmOrders = null;
let smmQuery = "";
let smmSelectedService = "";
let telegramTab = "stock";
let telegramLots = [];
let telegramLotsCursor = null;
let drawerSequence = 0;

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
  const form = body instanceof FormData;
  if (!form && body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url, { method, headers, body: form || body === undefined ? body : JSON.stringify(body) });
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
        ${quick("Мои лоты", "Просмотр и редактирование", "layers", "myListings")}
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

function chatPreview(message) {
  if (!message) return "Диалог";
  if (message.systemEvent) return [
    message.systemEvent.verified === false ? "Шаблон статуса" : message.systemEvent.source === "deal" ? "Статус сделки" : "",
    message.systemEvent.title, message.item?.name,
  ].filter(Boolean).join(" · ");
  return message.text || (message.images?.length ? "Вложение" : "Событие Playerok");
}

function chatRows(list) {
  return list.map((chat) => `<button class="row-button" data-action="chat" data-value="${safe(chat.id)}"
    aria-pressed="${chat.id === currentChat}" data-search="${safe(`${chat.title} ${chatPreview(chat.last)}`)}">
      ${avatar(chat.title, chat.avatar)}
      <span class="row-text"><strong>${safe(chat.title)}</strong>
       <small class="${chat.last?.systemEvent ? `event-preview${chat.last.systemEvent.verified === false ? " unverified" : ""}` : ""}">${chat.last?.systemEvent ? icon(chat.last.systemEvent.icon) : ""}<span>${safe(chatPreview(chat.last))}</span></small></span>
      <span class="row-right"><small>${dateTime(chat.last?.time)}</small>${chat.unread ? `<span class="unread">${count(chat.unread)}</span>` : ""}</span>
    </button>`).join("");
}

function eventCard(message) {
  const event = message.systemEvent;
  const item = message.item;
  const tone = ["paid", "sent", "success", "danger", "warning", "neutral"].includes(event.tone) ? event.tone : "neutral";
  const price = rubles(item?.price);
  return `<article class="event-card event-${tone}" aria-label="${safe(event.title)}">
    <header class="event-head">
      <span class="event-icon">${icon(event.icon)}</span>
      <div class="event-heading"><small>${event.verified === false ? "Статус из сообщения" : event.source === "deal" ? "Статус сделки" : "Событие Playerok"}</small><strong>${safe(event.title)}</strong></div>
      <time>${dateTime(message.time, true)}</time>
    </header>
    ${event.description ? `<p class="event-description">${safe(event.description)}</p>` : ""}
    ${item ? `<div class="event-product">
      <div class="event-cover" aria-hidden="true">${icon("bag")}${item.cover ? image(item.cover) : ""}</div>
      <div class="event-product-info"><strong>${safe(item.name || "Товар")}</strong>
        <div class="event-price"><span>Цена товара</span><b>${price === "—" ? "Не указана" : safe(price)}</b></div>
      </div>
    </div>` : event.trade ? '<p class="event-unavailable">Данные товара недоступны</p>' : ""}
    ${event.detail ? `<p class="event-detail">${safe(event.detail)}</p>` : ""}
    ${(message.images || []).map((src) => `<div class="event-attachment">${image(src, "Вложение Playerok")}</div>`).join("")}
  </article>`;
}

function bubble(message) {
  if (message.systemEvent) return eventCard(message);
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
  return list.map((entry) => `<button class="listing-row" data-search="${safe(entry.name)}"
      data-action="editListing" data-value="${safe(entry.id)}" title="Открыть лот">
    ${avatar(entry.name, entry.cover)}<span class="row-text"><strong>${safe(entry.name)}</strong></span>
    <span class="row-right"><strong>${rubles(entry.price)}</strong>${status(entry.status)}</span>
    ${icon("settings", "caret")}</button>`).join("");
}

function sales() {
  const bar = `<div class="toolbar">${tabs([["deals", "Сделки"], ["listings", "Мои лоты"]], salesTab, "salesTab", "Раздел продаж")}${search("Поиск по товарам")}</div>`;
  const filters = salesTab === "listings" ? tabs(listingFilters, listingType, "listingType", "Статус лота") : "";
  return `${hero("sales", "", salesTab === "listings" ? button("Выставить лот", "newListing", { glyph: "plus", variant: "primary" }) : "")}${bar}${filters}${panel ? `<div class="panel">
    ${panel.items.length ? (salesTab === "deals" ? dealRows(panel.items) : listingRows(panel.items)) : placeholder("Здесь пока пусто", "", "bag", salesTab === "listings" ? button("Создать первый лот", "newListing", { glyph: "plus", variant: "primary" }) : "")}
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
      ${plugin.telegramAccounts
        ? button("Управление", "telegramPlugin", { glyph: "settings" })
        : plugin.autoSmm ? button("Управление", "autosmmPlugin", { glyph: "settings" })
        : button("Настройки", "plugin", { value: plugin.id, glyph: "settings" })}
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
      ${currentAccount ? `<button class="account-pill balance-pill" data-action="balance" aria-label="Баланс" title="Баланс">${icon("wallet")}<span>${latestBalance === null ? "Баланс" : rubles(latestBalance.available)}</span></button>` : ""}
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
    if (tab === "overview") latestBalance = result.balanceDetails || null;
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
    latestBalance = null;
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
    latestBalance = null;
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

async function balanceDrawer() {
  const sequence = ++drawerSequence;
  const account = currentAccount.id;
  showDrawer("Баланс Playerok", skeleton());
  try {
    const result = await request(path("/balance"));
    if (sequence !== drawerSequence || currentAccount.id !== account) return;
    latestBalance = result;
    render();
    showDrawer("Баланс Playerok", `<div class="detail-rows">
      ${Object.entries({ available: "Доступно", total: "Всего", frozen: "Заморожено", pending: "Ожидает зачисления", withdrawable: "Доступно к выводу" })
      .map(([key, label]) => `<div><span>${label}</span><strong>${rubles(result[key])}</strong></div>`).join("")}
    </div>
    ${button("Вывод", "withdrawals", { glyph: "wallet", variant: "primary" })}`);
  } catch (error) {
    if (sequence === drawerSequence && currentAccount?.id === account) showDrawer("Баланс Playerok",
      `${placeholder("Баланс недоступен", error.message, "wallet", button("Обновить", "balance"))}
        ${button("Вывод", "withdrawals", { glyph: "wallet", variant: "primary" })}`);
  }
}

function payoutStatus(entry) {
  const colors = { PENDING: "amber", PROCESSING: "amber", CONFIRMED: "green", ROLLED_BACK: "red", FAILED: "red" };
  return `<span class="status ${colors[entry.status] || ""}">${safe(entry.statusLabel)}</span>`;
}

function payoutActions() {
  return `<div class="payout-actions">
    ${button("Создать выплату", "newPayout", { glyph: "wallet", variant: "primary" })}
    ${button("Обновить статусы", "withdrawals", { glyph: "refresh", variant: "flat" })}
    ${button("Помощь с выплатами", "payoutContacts", { glyph: "info", variant: "flat" })}
    ${button("К балансу", "balance", { glyph: "back", variant: "flat" })}
  </div>`;
}

async function withdrawalsDrawer(pageCursor = "") {
  if (!currentAccount) return;
  const sequence = ++drawerSequence;
  const account = currentAccount.id;
  showDrawer("Вывод средств", skeleton());
  try {
    const query = pageCursor ? `?cursor=${encodeURIComponent(pageCursor)}` : "";
    const result = await request(path("/withdrawals") + query);
    if (sequence !== drawerSequence || currentAccount?.id !== account) return;
    const rows = result.items.map((entry) => `<article class="payout-row">
      <div class="detail-rows">
        <div><span>Сумма</span><strong>${safe(rubles(entry.amount))}</strong></div>
        <div><span>Статус</span>${payoutStatus(entry)}</div>
        <div><span>Реквизиты</span><strong>${safe(entry.details || "—")}</strong></div>
        <div><span>Комиссия</span><strong>${safe(rubles(entry.fee))}</strong></div>
        <div><span>Дата</span><strong>${safe(dateTime(entry.time, true) || "—")}</strong></div>
      </div>
    </article>`).join("");
    showDrawer("Вывод средств", `<p class="payout-note">Выплату можно создать здесь: способ вывода, реквизиты и сумма. Реквизиты передаются в Playerok один раз и не сохраняются.</p>
      ${payoutActions()}
      <h3>История вывода</h3>
      ${rows || placeholder("Нет выплат на этой странице", "", "wallet")}
      ${result.cursor ? button("Следующая страница", "withdrawals", { value: result.cursor, glyph: "right", variant: "flat" }) : ""}`);
  } catch (error) {
    if (sequence !== drawerSequence || currentAccount?.id !== account) return;
    showDrawer("Вывод средств", `${placeholder("История недоступна", error.message, "wallet")}${payoutActions()}`);
  }
}

function payoutProviderData(id) {
  return payoutOptions?.providers.find((provider) => provider.id === id) || null;
}

function payoutHints() {
  return {
    card_number: "0000 0000 0000 0000", phone: "+7 999 123-45-67",
    wallet: "T… адрес USDT TRC20", account: "Номер счёта",
  };
}

function payoutEstimate(provider, amount) {
  if (!provider || !Number.isFinite(amount) || amount <= 0) return null;
  const percent = Number(provider.fee) || 0;
  let fee = Math.round(amount * percent) / 100;
  const floor = provider.minFee === null || provider.minFee === undefined ? null : Number(provider.minFee);
  if (floor !== null && fee < floor) fee = floor;
  return { fee, payout: Math.round((amount - fee) * 100) / 100, percent, floor };
}

function payoutBody(provider, amount = "") {
  if (!provider) return "";
  const hints = payoutHints();
  const fields = [];
  if (provider.requisites === "card_id") {
    fields.push(`<label class="field">Привязанная карта<select id="payout-card">
      ${payoutOptions.cards.map((card) => `<option value="${safe(card.id)}">•••• ${safe(card.lastFour)} · ${safe(card.type || "карта")}</option>`).join("")}
    </select></label>`);
  } else {
    if (provider.needsBank) {
      fields.push(`<label class="field">Банк СБП<select id="payout-bank">
        ${payoutOptions.banks.map((bank) => `<option value="${safe(bank.id)}">${safe(bank.name)}</option>`).join("")}
      </select></label>`);
    }
    fields.push(`<label class="field">${safe(provider.requisitesTitle)}<input id="payout-account" autocomplete="off"
      inputmode="${provider.requisites === "phone" ? "tel" : "text"}"
      placeholder="${safe(hints[provider.requisites] || hints.account)}"></label>`);
  }
  if (provider.paymentMethods.length > 1) {
    fields.push(`<label class="field">Платёжная система<select id="payout-method">
      ${provider.paymentMethods.map((method) => `<option value="${safe(method.id)}">${safe(method.name || method.id)}</option>`).join("")}
    </select></label>`);
  }
  const limit = provider.minValue !== null || provider.maxValue !== null
    ? `<p class="payout-note">Лимит способа: ${rubles(provider.minValue)} – ${rubles(provider.maxValue)}</p>` : "";
  return `${fields.join("")}
    <label class="field">Сумма, ₽<input id="payout-amount" type="number" min="1" step="1" inputmode="numeric"
      placeholder="500" value="${safe(amount)}"></label>
    ${limit}
    <div class="detail-rows" id="payout-estimate"></div>
    <label class="field inline">Реквизиты мои, подтверждаю выплату<input type="checkbox" id="payout-confirm"></label>
    <p class="form-error" role="alert" hidden></p>
    <div class="form-actions"><button class="button primary" type="submit">Создать выплату</button></div>`;
}

function refreshPayoutEstimate() {
  const provider = payoutProviderData(document.querySelector("#payout-provider")?.value);
  const amount = Number(document.querySelector("#payout-amount")?.value);
  const box = document.querySelector("#payout-estimate");
  if (!box) return;
  const estimate = payoutEstimate(provider, amount);
  box.innerHTML = estimate ? `
    <div><span>Комиссия</span><strong>${rubles(estimate.fee)} · ${estimate.percent}%</strong></div>
    <div><span>К получению</span><strong>${rubles(estimate.payout)}</strong></div>
    <div><span>Доступно к выводу</span><strong>${rubles(payoutOptions?.withdrawable)}</strong></div>` : "";
}

function renderPayoutDrawer(providerId = "", amount = "") {
  if (!payoutOptions) return;
  const providers = payoutOptions.providers;
  if (!providers.length) {
    showDrawer("Вывод средств", placeholder("Нет доступных способов вывода", "Проверьте статус аккаунта на Playerok.", "wallet",
      button("К истории", "withdrawals", { glyph: "back", variant: "flat" })));
    return;
  }
  const provider = payoutProviderData(providerId) || providers[0];
  const warning = payoutOptions.fundsProtection
    ? `<p class="payout-note">⚠️ Включена «Защита средств»: на новые реквизиты Playerok запросит код из почты. Код вводится только на сайте.</p>` : "";
  const cardHint = provider.requisites === "card_id" && !payoutOptions.cards.length
    ? `<p class="payout-note">${safe(payoutOptions.cardHint)}</p>` : "";
  showDrawer("Вывод средств", `${warning}${cardHint}
    <form id="payout-form">
      <label class="field">Способ вывода<select id="payout-provider">
        ${providers.map((entry) => `<option value="${safe(entry.id)}"${entry.id === provider.id ? " selected" : ""}>${safe(entry.name)}${entry.fee !== null ? ` · ${entry.fee}%` : ""}</option>`).join("")}
      </select></label>
      <div id="payout-body">${payoutBody(provider, amount)}</div>
    </form>
    ${button("Кошелёк и привязка карты на Playerok", "payoutSite", { glyph: "external", variant: "flat" })}
    ${button("К истории вывода", "withdrawals", { glyph: "back", variant: "flat" })}`);
  refreshPayoutEstimate();
}

async function payoutDrawer() {
  if (!currentAccount) return;
  const sequence = ++drawerSequence;
  const account = currentAccount.id;
  showDrawer("Вывод средств", skeleton());
  try {
    const result = await request(path("/withdrawal-methods"));
    if (sequence !== drawerSequence || currentAccount?.id !== account) return;
    payoutOptions = result;
    renderPayoutDrawer();
  } catch (error) {
    if (sequence !== drawerSequence || currentAccount?.id !== account) return;
    showDrawer("Вывод средств", `${placeholder("Вывод недоступен", error.message, "wallet")}
      ${button("К истории", "withdrawals", { glyph: "back", variant: "flat" })}`);
  }
}

async function submitPayout(form) {
  const provider = payoutProviderData(form.querySelector("#payout-provider").value);
  if (!provider) throw new Error("Выберите способ вывода.");
  const card = form.querySelector("#payout-card");
  const account = provider.requisites === "card_id"
    ? (card ? card.value : "")
    : (form.querySelector("#payout-account")?.value || "").trim();
  if (!account) throw new Error(provider.requisites === "card_id"
    ? "Привяжите карту на Playerok." : "Укажите реквизиты выплаты.");
  const value = Number(form.querySelector("#payout-amount").value);
  if (!Number.isInteger(value) || value <= 0) throw new Error("Введите сумму целыми рублями.");
  if (!form.querySelector("#payout-confirm").checked) throw new Error("Подтвердите выплату.");
  const method = form.querySelector("#payout-method")?.value || null;
  const bank = form.querySelector("#payout-bank")?.value || null;
  const result = await request(path("/withdrawals"), {
    method: "POST",
    body: { provider: provider.id, account, value, paymentMethodId: method, sbpBankMemberId: bank, confirm: true },
  });
  notify(`Выплата ${rubles(result.amount)} создана`);
  await withdrawalsDrawer();
}

function openPayoutSite(contacts = false) {
  const url = contacts ? "https://playerok.com/contacts" : "https://playerok.com/wallet/withdraw";
  if (telegram?.openLink) telegram.openLink(url);
  else window.open(url, "_blank", "noopener,noreferrer");
}

function wizardProgress(step) {
  return `<div class="wizard-progress" aria-label="Шаг ${step} из 4">
    ${["Раздел", "Категория", "Данные", "Публикация"].map((label, i) =>
      `<span class="${i + 1 === step ? "current" : i + 1 < step ? "done" : ""}"><b>${i + 1 < step ? icon("check") : i + 1}</b>${label}</span>`).join("")}</div>`;
}

function wizardValid(sequence, wizard) {
  return sequence === drawerSequence && listingWizard === wizard && currentAccount?.id === wizard.accountId;
}

function wizardError(title, error, retry) {
  showDrawer("Выставить лот", placeholder(title, error.message, "info") + (retry || ""));
}

function showCatalogGames() {
  const wizard = listingWizard;
  const rows = wizard.games.map((game) => `<button class="row-button" data-action="listingGame" data-value="${safe(game.id)}">
    ${avatar(game.name, game.cover)}<span class="row-text"><strong>${safe(game.name)}</strong></span>${icon("right", "caret")}</button>`).join("");
  showDrawer("Выставить лот", `${wizardProgress(1)}<p>Выберите игру, приложение или раздел каталога Playerok.</p>
    <form id="listing-catalog-form" class="wizard-search">
      <label class="field">Поиск<input name="q" type="search" value="${safe(wizard.query)}" maxlength="120" placeholder="Название игры или приложения"></label>
      <button class="button primary" type="submit">${icon("search")}Найти</button>
    </form>
    <p class="form-error" id="listing-catalog-error" role="alert" hidden></p>
    <div class="panel wizard-catalog">${rows || placeholder("Ничего не найдено", "Попробуйте другое название.", "search")}</div>
    ${wizard.cursor ? button("Показать ещё", "listingMoreGames", { variant: "flat", glyph: "down" }) : ""}`);
}

async function loadCatalogGames(query = "", append = false) {
  const wizard = listingWizard;
  if (!wizard || wizard.accountId !== currentAccount?.id || append && !wizard.cursor) return;
  const sequence = ++drawerSequence;
  const search = new URLSearchParams({ q: query });
  if (append) search.set("cursor", wizard.cursor);
  if (!append) {
    wizard.games = [];
    wizard.cursor = null;
    showDrawer("Выставить лот", wizardProgress(1) + skeleton());
  }
  try {
    const result = await request(path("/catalog/games?") + search.toString());
    if (!wizardValid(sequence, wizard)) return;
    const known = new Set(append ? wizard.games.map((entry) => entry.id) : []);
    wizard.games = (append ? wizard.games : []).concat(result.items.filter((entry) => !known.has(entry.id)));
    wizard.cursor = result.cursor;
    wizard.query = query;
    showCatalogGames();
  } catch (error) {
    if (!wizardValid(sequence, wizard)) return;
    wizard.query = query;
    showCatalogGames();
    const feedback = drawer.querySelector("#listing-catalog-error");
    feedback.textContent = error.message;
    feedback.hidden = false;
  }
}

function newListing() {
  if (!currentAccount) return;
  listingWizard = { accountId: currentAccount.id, query: "", games: [], cursor: null, game: null, schema: null };
  loadCatalogGames();
}

function showListingCategories() {
  const game = listingWizard.game;
  const rows = game.categories.map((entry) => `<button class="row-button" data-action="listingCategory" data-value="${safe(entry.id)}">
    <span class="quick-icon">${icon("layers")}</span><span class="row-text"><strong>${safe(entry.name)}</strong></span>${icon("right", "caret")}</button>`).join("");
  showDrawer("Выставить лот", `${wizardProgress(2)}<div class="wizard-context">${icon("bag")}<strong>${safe(game.name)}</strong></div>
    <div class="panel wizard-catalog">${rows || placeholder("Категорий нет", "Выберите другой раздел.", "info")}</div>
    ${button("К разделам", "listingGamesBack", { glyph: "back", variant: "flat" })}`);
}

async function chooseListingGame(gameId) {
  const wizard = listingWizard;
  const sequence = ++drawerSequence;
  showDrawer("Выставить лот", wizardProgress(2) + skeleton());
  try {
    const game = await request(path(`/catalog/games/${encodeURIComponent(gameId)}`));
    if (!wizardValid(sequence, wizard)) return;
    wizard.game = game;
    wizard.schema = null;
    showListingCategories();
  } catch (error) {
    if (wizardValid(sequence, wizard)) wizardError("Каталог недоступен", error,
      button("К разделам", "listingGamesBack", { glyph: "back", variant: "flat" }));
  }
}

function showListingMethods() {
  const schema = listingWizard.schema;
  showDrawer("Выставить лот", `${wizardProgress(2)}<div class="wizard-context">${icon("layers")}<strong>${safe(schema.name)}</strong></div>
    <h3>Способ получения</h3><div class="panel wizard-catalog">${schema.methods.map((method) =>
      `<button class="row-button" data-action="listingMethod" data-value="${safe(method.id)}"><span class="row-text"><strong>${safe(method.name)}</strong></span>${icon("right", "caret")}</button>`).join("")}</div>
    ${button("К категориям", "listingCategoriesBack", { glyph: "back", variant: "flat" })}`);
}

async function chooseListingCategory(categoryId, methodId = null) {
  const wizard = listingWizard;
  const sequence = ++drawerSequence;
  const params = methodId ? `?method=${encodeURIComponent(methodId)}` : "";
  showDrawer("Выставить лот", wizardProgress(2) + skeleton());
  try {
    const schema = await request(path(`/catalog/categories/${encodeURIComponent(categoryId)}${params}`));
    if (!wizardValid(sequence, wizard)) return;
    wizard.schema = schema;
    if (schema.ready) showListingDraftForm();
    else showListingMethods();
  } catch (error) {
    if (wizardValid(sequence, wizard)) wizardError("Не удалось загрузить параметры", error,
      button("К категориям", "listingCategoriesBack", { glyph: "back", variant: "flat" }));
  }
}

function listingOptionField(group, index) {
  const name = `option-${index}`;
  if (group.kind === "range") return `<label class="field">${safe(group.label)}<input name="${name}" type="number" step="1"
    ${group.minimum !== null ? `min="${safe(group.minimum)}"` : ""} ${group.maximum !== null ? `max="${safe(group.maximum)}"` : ""} placeholder="Не выбрано"></label>`;
  const choices = group.choices.map((choice) => `<option value="${safe(choice.value)}">${safe(choice.label)}</option>`).join("");
  return `<label class="field">${safe(group.label)}<select name="${name}" ${group.multiple ? 'multiple size="4"' : ""}>
    ${group.multiple ? "" : '<option value="">Не выбрано</option>'}${choices}</select></label>`;
}

function showListingDraftForm() {
  const wizard = listingWizard;
  const schema = wizard.schema;
  const context = [wizard.game.name, schema.name, schema.methods.find((entry) => entry.id === schema.methodId)?.name].filter(Boolean).join(" · ");
  const back = button("Назад", "listingSchemaBack", { glyph: "back", variant: "flat" });
  if (schema.blocked) {
    showDrawer("Выставить лот", `${wizardProgress(3)}<div class="wizard-context">${icon("layers")}<strong>${safe(context)}</strong></div>
      ${placeholder("Категория пока недоступна", "Для неё необходимы закрытые данные, которые этот мастер не запрашивает.", "info")}
      ${back}`);
    return;
  }
  showDrawer("Выставить лот", `${wizardProgress(3)}<div class="wizard-context">${icon("layers")}<strong>${safe(context)}</strong></div>
    <form id="listing-create-form">
      <label class="field">Название товара<input name="name" maxlength="120" required placeholder="Короткое название"></label>
      <label class="field">Цена, ₽<input name="price" type="number" min="1" max="10000000" step="1" inputmode="numeric" required placeholder="0"></label>
      <label class="field">Описание<textarea name="description" rows="4" maxlength="4096" required placeholder="Что получает покупатель"></textarea></label>
      ${schema.options.length ? `<section class="wizard-fields"><h3>Параметры товара</h3>${schema.options.map(listingOptionField).join("")}</section>` : ""}
      ${schema.fields.length ? `<section class="wizard-fields"><h3>Дополнительные поля</h3>${schema.fields.map((field, index) =>
        `<label class="field">${safe(field.label)}<input name="data-${index}" maxlength="2048" ${field.required ? "required" : "placeholder=Необязательно"}></label>`).join("")}</section>` : ""}
      <label class="field">Комментарий покупателю<textarea name="comment" rows="2" minlength="2" maxlength="2048" required placeholder="Инструкция или сообщение после покупки"></textarea></label>
      <section class="wizard-fields"><h3>Фотографии товара</h3>
        <label class="wizard-upload">${icon("layers")}<span><strong>Выбрать фотографии</strong><small>JPEG, PNG, WebP · до 10 фото, 30 МБ суммарно</small></span><input name="images" type="file" multiple accept="image/jpeg,image/png,image/webp"></label>
        <p id="listing-photo-summary" class="wizard-photo-summary" aria-live="polite">Файлы не выбраны</p>
      </section>
      <p class="wizard-note">${icon("info")}Сначала создадим черновик. Плата за размещение не списывается без подтверждения.</p>
      <p class="form-error" role="alert" hidden></p>
      <div class="form-actions">${back}<button class="button primary" type="submit">${icon("check")}Создать черновик</button></div>
    </form>`);
}

async function submitListingDraft(form) {
  const wizard = listingWizard;
  const schema = wizard.schema;
  const options = Object.create(null);
  const fields = Object.create(null);
  schema.options.forEach((group, index) => {
    const input = form.elements.namedItem(`option-${index}`);
    if (group.multiple && group.kind !== "range") {
      const values = [...input.selectedOptions].map((entry) => entry.value);
      if (values.length) options[group.field] = values;
    } else if (input.value !== "") options[group.field] = group.kind === "range" ? input.valueAsNumber : input.value;
  });
  schema.fields.forEach((field, index) => {
    const value = form.elements.namedItem(`data-${index}`).value.trim();
    if (value) fields[field.id] = value;
  });
  const photos = [...form.elements.images.files];
  if (photos.length > 10 || photos.some((file) => file.size > 10 * 1024 * 1024)
      || photos.reduce((sum, file) => sum + file.size, 0) > 30 * 1024 * 1024) {
    throw new Error("До 10 фотографий, максимум 10 МБ на фото и 30 МБ суммарно.");
  }
  const body = new FormData();
  body.append("data", JSON.stringify({ categoryId: schema.id, methodId: schema.methodId,
    name: form.elements.name.value.trim(), price: form.elements.price.valueAsNumber,
    description: form.elements.description.value.trim(), comment: form.elements.comment.value.trim(), options, fields }));
  photos.forEach((file) => body.append("image", file));
  const sequence = drawerSequence;
  const created = await request(path("/listings"), { method: "POST", body });
  if (!wizardValid(sequence, wizard)) return;
  editedListing = created;
  listingWizard = null;
  listingType = created.status === "DRAFT" ? "DRAFT" : "";
  salesTab = "listings";
  loadPanel({ fresh: true });
  notify(created.status === "DRAFT" ? "Черновик создан" : "Лот создан");
  if (created.canPublish) await listingPublication(created.id);
  else showListingEditor();
}

async function listingPublication(id) {
  const sequence = ++drawerSequence;
  const account = currentAccount?.id;
  showDrawer("Публикация лота", wizardProgress(4) + skeleton());
  try {
    const quote = await request(path(`/listings/${encodeURIComponent(id)}/publication`));
    if (sequence !== drawerSequence || account !== currentAccount?.id) return;
    publicationQuote = quote;
    showPublicationQuote();
  } catch (error) {
    if (sequence !== drawerSequence || account !== currentAccount?.id) return;
    showDrawer("Публикация лота", `${placeholder("Условия публикации недоступны", error.message, "info")}
      <div class="detail-buttons">${button("Обновить", "publishListing", { value: id, glyph: "refresh" })}
      ${button("К лоту", "editListing", { value: id, glyph: "back", variant: "flat" })}</div>`);
  }
}

function showPublicationQuote() {
  const quote = publicationQuote;
  const tariffs = quote.tariffs;
  const first = tariffs.find((entry) => entry.available);
  showDrawer("Публикация лота", `${wizardProgress(4)}
    <div class="wizard-publication-summary">${icon("bag")}<div><small>Цена продажи</small><strong>${rubles(quote.price)}</strong><p>${safe(quote.name)}</p></div></div>
    <div class="detail-rows"><div><span>Доступный баланс</span><strong>${rubles(quote.balance)}</strong></div></div>
    <form id="listing-publish-form">
      <h3>Тариф размещения</h3>
      <div class="publication-tariffs">${tariffs.length ? tariffs.map((entry) => `<label class="publication-tariff ${entry.available ? "" : "unavailable"}">
        <input type="radio" name="tariff" value="${safe(entry.id)}" ${entry.id === first?.id ? "checked" : ""} ${entry.available ? "" : "disabled"} required>
        <span><strong>${safe(entry.name)}</strong><small>${entry.available ? Number(entry.fee) === 0 ? "Бесплатное размещение" : `Остаток: ${rubles(entry.remaining)}` : "Тариф недоступен"}</small></span>
        <b>${rubles(entry.fee)}</b></label>`).join("") : '<p>Playerok пока не предложил тарифы.</p>'}</div>
      <div id="publication-estimate" class="detail-rows"></div>
      <label class="field inline">Подтверждаю размещение и выбранную стоимость<input type="checkbox" name="confirm" required></label>
      <p class="form-error" role="alert" hidden></p>
      <div class="form-actions"><button class="button primary" type="submit" ${first ? "" : "disabled"}>Опубликовать</button></div>
    </form>
    <div class="detail-buttons">${button("Оставить лот", "editListing", { value: quote.itemId, glyph: "back", variant: "flat" })}${button("Обновить тарифы", "publishListing", { value: quote.itemId, glyph: "refresh", variant: "flat" })}</div>`);
  updatePublicationEstimate();
}

function updatePublicationEstimate() {
  const form = drawer.querySelector("#listing-publish-form");
  if (!form || !publicationQuote) return;
  const tariff = publicationQuote.tariffs.find((entry) => entry.id === form.elements.tariff?.value);
  const estimate = form.querySelector("#publication-estimate");
  estimate.innerHTML = tariff ? `<div><span>Стоимость размещения</span><strong>${rubles(tariff.fee)}</strong></div>
    <div><span>Остаток после размещения</span><strong>${rubles(tariff.remaining)}</strong></div>` : "";
  const submit = form.querySelector("[type=submit]");
  submit.textContent = tariff && Number(tariff.fee) > 0 ? `Опубликовать · списать ${rubles(tariff.fee)}` : "Опубликовать";
}

async function submitPublication(form) {
  const quote = publicationQuote;
  const tariff = quote?.tariffs.find((entry) => entry.id === form.elements.tariff?.value);
  if (!tariff?.available || !form.elements.confirm.checked) throw new Error("Выберите доступный тариф и подтвердите публикацию.");
  const sequence = drawerSequence;
  const account = currentAccount?.id;
  const result = await request(path(`/listings/${encodeURIComponent(quote.itemId)}/publication`), {
    method: "POST", body: { token: quote.token, tariffId: tariff.id, confirm: true },
  });
  if (sequence !== drawerSequence || account !== currentAccount?.id) return;
  publicationQuote = null;
  editedListing = result;
  listingType = "";
  loadPanel({ fresh: true });
  showDrawer("Лот размещён", `<div class="wizard-publication-result">${icon("check")}<h3>${safe(result.name)}</h3>${status(result.status)}
    <strong>${rubles(result.price)}</strong><p>${result.status === "APPROVED" ? "Товар опубликован." : "Playerok принял лот. Ожидается проверка."}</p></div>
    <div class="detail-buttons">${button("К лоту", "editListing", { value: result.id, glyph: "bag", variant: "primary" })}
    ${result.slug ? button("На Playerok", "listing", { value: result.slug, glyph: "external" }) : ""}</div>`);
  notify("Playerok подтвердил размещение");
}

function editedListingPath(suffix = "") {
  return path(`/listings/${encodeURIComponent(editedListing.id)}${suffix}`);
}

function renderListingPhotos() {
  const pictures = editedListing.photos;
  return `<section id="listing-photos" class="seller-subsection">
    <h3>Фотографии</h3>
    ${pictures.length ? `<div class="photo-grid">${pictures.map((photo, index) => image(photo.url, `Фото ${index + 1}`)).join("")}</div>` : "<p>У лота пока нет фотографий.</p>"}
    ${editedListing.editable ? `<form id="listing-photo-form">
      <label class="field">Загрузить JPEG, PNG или WebP<input name="image" type="file" required accept="image/jpeg,image/png,image/webp"></label>
      ${pictures.length ? `<label class="field">Действие<select name="replace"><option value="">Добавить фото</option>
        ${pictures.map((photo, index) => `<option value="${safe(photo.id)}">Заменить фото ${index + 1}</option>`).join("")}</select></label>` : ""}
      <p class="form-error" role="alert" hidden></p><button class="button" type="submit">Загрузить фото</button>
    </form>` : ""}
  </section>`;
}

function showListingEditor() {
  const entry = editedListing;
  showDrawer("Лот Playerok", `
    <div class="detail-rows"><div><span>Статус</span>${status(entry.status)}</div>
    ${entry.basePrice && Number(entry.price) < Number(entry.basePrice) ? `<div><span>Базовая цена</span><strong>${rubles(entry.basePrice)}</strong></div>` : ""}</div>
    ${entry.editable ? `<form id="listing-form">
      <label class="field">Название<input name="name" value="${safe(entry.name)}" maxlength="120" required></label>
      <label class="field">Цена, ₽<input type="number" name="price" min="1" max="10000000" step="1" value="${safe(entry.price)}" required></label>
      <label class="field">Описание<textarea name="description" rows="4" maxlength="4096" required>${safe(entry.description)}</textarea></label>
      <label class="field">Комментарий покупателю после покупки<textarea name="comment" rows="2" maxlength="2048" placeholder="Введите новый комментарий, если хотите его изменить"></textarea></label>
      <p class="form-error" role="alert" hidden></p><div class="detail-buttons"><button class="button primary" type="submit">Сохранить лот</button>
      ${entry.slug ? button("На Playerok", "listing", { value: entry.slug, glyph: "external" }) : ""}</div></form>`
      : `<p>${safe(entry.name)} · ${rubles(entry.price)}</p><p>${safe(entry.description)}</p><p>Playerok пока не разрешает редактировать этот лот.</p>
        ${entry.slug ? button("На Playerok", "listing", { value: entry.slug, glyph: "external" }) : ""}`}
    ${entry.canPublish ? `<div class="seller-subsection">${button("Опубликовать лот", "publishListing", { value: entry.id, glyph: "plus", variant: "primary" })}</div>` : ""}
    ${renderListingPhotos()}`);
}

async function editListing(id) {
  const sequence = ++drawerSequence;
  showDrawer("Лот Playerok", skeleton());
  try {
    const item = await request(path(`/listings/${encodeURIComponent(id)}`));
    if (sequence !== drawerSequence) return;
    editedListing = item;
    showListingEditor();
  } catch (error) {
    if (sequence === drawerSequence) showDrawer("Лот Playerok", placeholder("Не удалось открыть лот", error.message, "info", button("Повторить", "editListing", { value: id })));
  }
}

function saveListingToList(item) {
  if (tab === "sales" && salesTab === "listings" && panel) {
    panel.items = panel.items.map((entry) => entry.id === item.id ? item : entry);
    render();
  }
}

async function saveListingForm(form) {
  const entry = editedListing;
  const name = form.elements.name.value.trim();
  const price = form.elements.price.valueAsNumber;
  const description = form.elements.description.value.trim();
  const comment = form.elements.comment.value.trim();
  const patch = {};
  if (name !== entry.name) patch.name = name;
  if (price !== Number(entry.price)) patch.price = price;
  if (description !== entry.description) patch.description = description;
  if (comment) patch.comment = comment;
  if (!Object.keys(patch).length) return;
  const sequence = drawerSequence;
  const updated = await request(editedListingPath(), { method: "PATCH", body: patch });
  if (sequence !== drawerSequence) return;
  editedListing = updated;
  saveListingToList(updated);
  showListingEditor();
  notify("Лот сохранён");
}

async function sendPhotoForm(form) {
  const file = form.elements.image.files[0];
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) throw new Error("Фотография должна быть не больше 10 МБ.");
  const replace = form.elements.replace?.value;
  const body = new FormData();
  body.append("image", file);
  const sequence = drawerSequence;
  const updated = await request(editedListingPath("/photo" + (replace ? `?replace=${encodeURIComponent(replace)}` : "")), { method: "POST", body });
  if (sequence !== drawerSequence) return;
  editedListing = updated;
  saveListingToList(updated);
  drawer.querySelector("#listing-photos").outerHTML = renderListingPhotos();
  notify("Фотография сохранена");
}

function renderTelegramPlugin() {
  const options = tabs([["stock", "Склад"], ["rules", "Привязки лотов"], ["api", "Telegram API"], ["ai", "ИИ-помощник"]], telegramTab, "telegramTab", "Настройки ТГ-аккаунтов");
  const countryOptions = `<datalist id="tg-countries">${telegramPanel.countries.map((country) => `<option value="${safe(country)}"></option>`).join("")}</datalist>`;
  let content = "";
  if (telegramTab === "stock") {
    const stock = telegramPanel.stock;
    content = stock.length ? `<div class="panel">${stock.map((entry) =>
      `<div class="preference"><span>${safe(entry.country)}</span><span>${count(entry.available)} доступно · ${count(entry.issued)} выдано${entry.other ? ` · ${count(entry.other)} прочее` : ""}</span></div>`).join("")}</div>`
      : placeholder("На складе пока пусто", "Добавление профилей доступно в меню бота.", "layers");
    if (bootstrap?.botUsername) content += `<div class="detail-buttons">${button("Открыть склад в боте", "telegramStockBot", { glyph: "external" })}</div>`;
  } else if (telegramTab === "api") {
    const config = telegramPanel.config;
    content = `<form id="telegram-api-form">
      <label class="field">API ID<input name="apiId" type="number" min="1" max="2147483647" value="${safe(config.apiId)}" required></label>
      <label class="field">API Hash<input name="apiHash" type="password" maxlength="32" autocomplete="new-password" placeholder="${config.apiHashSet ? "Сохранён. Введите новый при необходимости" : "Не задан"}"></label>
      <p class="form-error" role="alert" hidden></p><div class="detail-buttons"><button class="button primary" type="submit">Сохранить API</button>
      ${!config.defaults ? button("Сбросить API", "telegramReset") : ""}</div></form>`;
  } else if (telegramTab === "ai") {
    content = renderTelegramAi(telegramPanel.ai);
  } else {
    const rules = telegramPanel.rules;
    content = `${countryOptions}<form id="telegram-bind-form">
      <label class="field">Лот<select name="lotId" required><option value="">Выберите свой лот</option>
      ${telegramLots.map((item) => `<option value="${safe(item.id)}">${safe(item.name)}</option>`).join("")}</select></label>
      ${telegramLotsCursor ? button("Загрузить ещё лоты", "telegramMoreLots") : ""}
      <label class="field">Страна<input name="country" list="tg-countries" maxlength="128" required></label>
      <p class="form-error" role="alert" hidden></p><button class="button primary" type="submit">Привязать лот</button></form>
      <div class="seller-subsection"><h3>По названию</h3>
        <form id="telegram-keyword-form">
          <label class="field">Слово или фраза<input name="keyword" maxlength="128" required></label>
          <label class="field">Страна<input name="country" list="tg-countries" maxlength="128" required></label>
          <p class="form-error" role="alert" hidden></p><button class="button primary" type="submit">Привязать по названию</button>
        </form>
      </div>
      <div class="seller-subsection"><h3>Сохранённые привязки</h3>
      ${rules.length ? rules.map((rule) => `<form class="panel telegram-rule" data-rule="${rule.id}">
        <div class="preference"><span>${rule.keyword ? "Фраза «" + safe(rule.keyword) + "»" : safe(rule.title)}</span>${toggle("Активность привязки", rule.enabled, "telegramRuleSwitch", rule.id)}</div>
        <label class="field">Страна<input name="country" list="tg-countries" maxlength="128" value="${safe(rule.country)}" required></label>
        <div class="detail-buttons"><button class="button" type="submit">Сохранить страну</button>${button("Удалить", "telegramDeleteRule", { value: rule.id, variant: "flat" })}</div>
        <p class="form-error" role="alert" hidden></p></form>`).join("")
        : '<p>Привязок пока нет.</p>'}</div>`;
  }
  showDrawer("ТГ-аккаунты", `<div class="telegram-panel">${options}${content}</div>`);
}

function renderTelegramAi(ai) {
  if (!ai?.available) {
    return placeholder("ИИ-помощник не установлен", "Включите плагин «ИИ Ассистент (#аск)» в разделе «Плагины».", "plug",
      button("К плагинам", "nav", { value: "plugins", glyph: "plug", variant: "flat" }));
  }
  return `<form id="telegram-ai-form">
    <div class="preference"><span>ИИ отвечает покупателям</span>${toggle("Включить ИИ-помощника", ai.enabled, "telegramAiSwitch")}</div>
    <p class="payout-note">Покупатель зовёт помощника командой <b>#аск</b>, останавливает — <b>#аскстоп</b>. Команды #код и #продавец работают всегда.</p>
    <label class="field">Base URL<input name="baseUrl" value="${safe(ai.baseUrl)}" maxlength="300"
      placeholder="https://api.anthropic.com" autocomplete="off" spellcheck="false" required></label>
    <label class="field">API Token<input name="apiToken" type="password" maxlength="512" autocomplete="new-password"
      placeholder="${ai.tokenSet ? "Сохранён. Введите новый, чтобы заменить" : "Не задан"}"></label>
    <label class="field">ID модели<input name="model" value="${safe(ai.model)}" maxlength="200"
      placeholder="claude-sonnet-4-20250514" autocomplete="off" spellcheck="false"></label>
    <label class="field">Базовый промпт<textarea name="basePrompt" rows="6" maxlength="8000"
      placeholder="Роль и правила помощника">${safe(ai.basePrompt)}</textarea></label>
    <label class="field">Режим работы<select name="mode">
      ${ai.modes.map((mode) => `<option value="${safe(mode)}"${mode === ai.mode ? " selected" : ""}>${safe((ai.modeLabels || {})[mode] || aiModeName(mode))}</option>`).join("")}
    </select></label>
    <label class="field inline">Уведомлять в TG об ответах ИИ<input type="checkbox" name="notifyTg"${ai.notifyTg ? " checked" : ""}></label>
    <p class="form-error" role="alert" hidden></p>
    <div class="detail-buttons"><button class="button primary" type="submit">Сохранить ИИ</button>
    ${ai.tokenSet ? button("Сбросить токен", "telegramAiResetToken", { variant: "flat" }) : ""}</div>
  </form>`;
}

function aiModeName(mode) {
  return {
    help_command_only: "Только по команде #аск",
    always_after_help: "Диалог 30 минут после #аск",
    all_messages: "Отвечать на все сообщения",
  }[mode] || mode;
}

async function telegramAi(patch) {
  const sequence = drawerSequence;
  const result = await request(path("/telegram-accounts/ai"), { method: "PATCH", body: patch });
  if (sequence !== drawerSequence) return;
  telegramPanel.ai = result;
  renderTelegramPlugin();
  notify("Настройки ИИ сохранены");
}

async function loadTelegramLots(append = false) {
  const seq = drawerSequence;
  const next = append && telegramLotsCursor ? `?${new URLSearchParams({ cursor: telegramLotsCursor })}` : "";
  const data = await request(path("/listings" + next));
  if (seq !== drawerSequence || telegramTab !== "rules") return;
  const seen = new Set(append ? telegramLots.map((item) => item.id) : []);
  telegramLots = [...(append ? telegramLots : []), ...data.items.filter((item) => !seen.has(item.id))];
  telegramLotsCursor = data.cursor;
  renderTelegramPlugin();
}

async function telegramPluginDrawer() {
  const sequence = ++drawerSequence;
  telegramTab = "stock";
  telegramLots = [];
  telegramLotsCursor = null;
  showDrawer("ТГ-аккаунты", skeleton());
  try {
    const data = await request(path("/telegram-accounts"));
    if (sequence !== drawerSequence) return;
    telegramPanel = data;
    renderTelegramPlugin();
  } catch (error) {
    if (sequence === drawerSequence) showDrawer("ТГ-аккаунты", placeholder("Меню недоступно", error.message, "info", button("Повторить", "telegramPlugin")));
  }
}

async function telegramChangeTab(value) {
  telegramTab = value;
  renderTelegramPlugin();
  if (value === "rules" && !telegramLots.length) await loadTelegramLots();
}

async function telegramConfig(patch) {
  const sequence = drawerSequence;
  const result = await request(path("/telegram-accounts"), { method: "PATCH", body: patch });
  if (sequence !== drawerSequence) return;
  telegramPanel.config = result;
  renderTelegramPlugin();
  notify("Настройки API сохранены");
}

async function telegramRule(id, method, body) {
  const sequence = drawerSequence;
  const url = path(id ? `/telegram-accounts/rules/${encodeURIComponent(id)}` : "/telegram-accounts/rules");
  const result = await request(url, { method, body });
  if (sequence !== drawerSequence) return;
  telegramPanel.rules = telegramPanel.rules.filter((row) => String(row.id) !== String(id) && row.id !== result.id);
  if (method !== "DELETE") telegramPanel.rules.unshift(result);
  renderTelegramPlugin();
  notify(method === "DELETE" ? "Привязка удалена" : "Привязка сохранена");
}

function smmStateBadge(value) {
  const raw = String(value || "—");
  const colors = { completed: "green", pending: "amber", processing: "teal", "in progress": "teal",
    partial: "amber", requesting: "amber", unknown: "amber", rejected: "red", canceled: "red", cancelled: "red", failed: "red" };
  return `<span class="status ${colors[raw.toLowerCase()] || ""}">${safe(raw)}</span>`;
}

function smmRuleCard(rule) {
  return `<article class="smm-rule">
    <div class="smm-rule-head"><span class="quick-icon">${icon(rule.keyword ? "search" : "bag")}</span>
      <div class="row-text"><small>${rule.keyword ? "Ключевая фраза" : "Конкретный лот"}</small>
      <strong>${safe(rule.keyword || rule.title)}</strong><small>Услуга #${safe(rule.serviceId)} · ${safe(rule.serviceName)}</small></div>
      ${toggle("Привязка", rule.enabled, "smmToggleRule", rule.id)}</div>
    <form class="smm-rule-form" data-rule="${safe(rule.id)}">
      <label class="field">Количество<input type="number" name="quantity" min="1" max="10000000" step="1" value="${safe(rule.quantity)}" required></label>
      <label class="field inline">Отметить сделку SENT после запуска<input type="checkbox" name="autoSent" ${rule.autoSent ? "checked" : ""}></label>
      <p class="form-error" role="alert" hidden></p>
      <div class="detail-buttons"><button class="button" type="submit">Сохранить</button>
      ${button("Удалить", "smmDeleteRule", { value: rule.id, glyph: "close", variant: "flat" })}</div>
    </form></article>`;
}

function renderSmmPanel() {
  const configured = smmPanel.configured;
  let body = "";
  if (smmTab === "rules") {
    body = `<div class="smm-section"><h3>Новая привязка</h3>
      ${configured ? `<form id="smm-bind-form">
      <label class="field">Ключевая фраза<input name="keyword" maxlength="128" placeholder="Слово или фраза из названия лота"></label>
      <label class="field">Или ID конкретного лота<input name="lotId" maxlength="128" placeholder="Оставьте пустым при привязке по фразе"></label>
      <label class="field">ID услуги SMM<input name="serviceId" maxlength="48" value="${safe(smmSelectedService)}" required></label>
      <label class="field">Количество<input name="quantity" type="number" min="1" max="10000000" step="1" required placeholder="100"></label>
      <p class="payout-note">Точный ID имеет приоритет. Для нескольких совпадений выбирается наиболее длинная фраза. Количество проверяется по каталогу.</p>
      <p class="form-error" role="alert" hidden></p>
      <button type="submit" class="button primary">${icon("plus")}Добавить привязку</button></form>` : '<p>Сначала настройте API панели в меню AutoSMM Telegram-бота.</p>'}
      <h3>Привязки · ${count(smmPanel.rules.length)}</h3>
      ${smmPanel.rules.length ? `<div class="smm-rules">${smmPanel.rules.map(smmRuleCard).join("")}</div>` : placeholder("Пока нет привязок", "", "bag")}</div>`;
  } else if (smmTab === "catalog") {
    const list = smmCatalog?.items || [];
    body = `<form id="smm-search-form" class="wizard-search">
      <label class="field">Поиск услуг<input name="q" type="search" maxlength="128" value="${safe(smmQuery)}" placeholder="ID, название или категория"></label>
      <button class="button primary" type="submit">Найти</button><p class="form-error" role="alert" hidden></p></form>
      ${smmCatalog ? `<small class="payout-note">Найдено: ${count(smmCatalog.total)}</small><div class="panel wizard-catalog">${list.map((service) =>
        `<button class="row-button smm-service-row" data-action="smmBindService" data-value="${safe(service.id)}"><span class="row-text">
        <strong>#${safe(service.id)} · ${safe(service.name)}</strong><small>${safe(service.category)} · ${safe(service.min || "—")}–${safe(service.max || "—")}</small>
        <small>Тариф: ${safe(service.rate || "—")} / 1000 · Refill: ${service.refill ? "доступен" : "нет"}</small></span>${icon("right", "caret")}</button>`).join("") || placeholder("Нет услуг", "Попробуйте другой запрос.", "search")}</div>
        ${smmCatalog.next !== null ? button("Показать ещё", "smmCatalogMore", { value: smmCatalog.next, glyph: "down" }) : ""}` : skeleton()}`;
  } else {
    body = smmOrders ? `<small class="payout-note">Всего заказов: ${count(smmOrders.total)}</small>
      <div class="panel wizard-catalog">${smmOrders.items.map((entry) =>
      `<button class="row-button smm-service-row" data-action="smmOrder" data-value="${safe(entry.id)}"><span class="row-text">
        <strong>Заказ #${safe(entry.orderId)}</strong><small>${safe(entry.serviceName || entry.serviceId)} · ${count(entry.quantity)} шт.</small>
        <small>${entry.refillStatus ? `Refill: ${safe(entry.refillStatus)}` : "Refill не запрашивался"}</small></span>${smmStateBadge(entry.status)}${icon("right", "caret")}</button>`).join("") || placeholder("Заказов пока нет", "", "bag")}</div>
        ${smmOrders.next !== null ? button("Показать ещё", "smmOrdersMore", { value: smmOrders.next, glyph: "down" }) : ""}` : skeleton();
  }
  showDrawer("AutoSMM", `<div class="smm-overview"><span class="plugin-icon">${icon("plug")}</span>
    <div class="row-text"><strong>${configured ? "SMM-панель подключена" : "Панель не подключена"}</strong><small>${safe(smmPanel.apiUrl || "Настройки API в Telegram-боте")}</small></div>
    <span class="signal ${configured ? "live" : ""}"></span></div>
    <div class="detail-buttons smm-toolbar">
      ${configured ? button(smmPanel.balance ? `${smmPanel.balance.balance} ${smmPanel.balance.currency}` : "Баланс панели", "smmBalance", { glyph: "wallet" }) : ""}
      ${button("Настройки плагина", "plugin", { value: "autosmm", glyph: "settings", variant: "flat" })}
    </div>
    ${tabs([["rules", "Привязки"], ["catalog", "Услуги"], ["orders", "Заказы"]], smmTab, "smmTab", "Раздел AutoSMM")}
    <div class="smm-body">${body}</div>`);
}

async function smmPluginDrawer() {
  const sequence = ++drawerSequence;
  smmTab = "rules"; smmCatalog = null; smmOrders = null; smmQuery = ""; smmSelectedService = "";
  showDrawer("AutoSMM", skeleton());
  try {
    const data = await request(path("/autosmm"));
    if (sequence !== drawerSequence) return;
    smmPanel = data;
    renderSmmPanel();
  } catch (error) {
    if (sequence === drawerSequence) showDrawer("AutoSMM", placeholder("Не удалось открыть", error.message, "info",
      button("Повторить", "autosmmPlugin")));
  }
}

async function smmBalance(target) {
  const sequence = drawerSequence;
  target.disabled = true;
  try {
    const balance = await request(path("/autosmm/balance"));
    if (sequence !== drawerSequence) return;
    smmPanel.balance = balance;
    renderSmmPanel();
  } catch (error) {
    if (sequence === drawerSequence) notify(error.message, true);
  } finally {
    target.disabled = false;
  }
}

async function loadSmmCatalog(query = smmQuery, offset = 0) {
  const sequence = ++drawerSequence;
  smmQuery = query;
  if (offset === 0) smmCatalog = null;
  renderSmmPanel();
  try {
    const data = await request(path(`/autosmm/catalog?${new URLSearchParams({ q: query, offset })}`));
    if (sequence !== drawerSequence || smmTab !== "catalog") return;
    smmCatalog = { ...data, items: [...(offset ? smmCatalog?.items || [] : []), ...data.items] };
    renderSmmPanel();
  } catch (error) {
    if (sequence === drawerSequence) {
      smmCatalog = { items: [], total: 0, next: null };
      renderSmmPanel();
      const feedback = drawer.querySelector("#smm-search-form .form-error");
      feedback.textContent = error.message; feedback.hidden = false;
    }
  }
}

async function loadSmmOrders(offset = 0) {
  const sequence = ++drawerSequence;
  if (!offset) smmOrders = null;
  renderSmmPanel();
  try {
    const data = await request(path(`/autosmm/orders?offset=${offset}`));
    if (sequence !== drawerSequence || smmTab !== "orders") return;
    smmOrders = { ...data, items: [...(offset ? smmOrders?.items || [] : []), ...data.items] };
    renderSmmPanel();
  } catch (error) {
    if (sequence === drawerSequence) showDrawer("AutoSMM", placeholder("История недоступна", error.message, "info", button("Назад", "autosmmPlugin")));
  }
}

function smmChangeTab(value) {
  drawerSequence += 1;
  smmTab = value;
  renderSmmPanel();
  if (value === "catalog") loadSmmCatalog();
  if (value === "orders") loadSmmOrders();
}

async function smmBind(form) {
  const keyword = form.elements.keyword.value.trim(), lotId = form.elements.lotId.value.trim();
  if (Boolean(keyword) === Boolean(lotId)) throw new Error("Выберите ID лота или ключевую фразу (одно из двух).");
  const sequence = drawerSequence;
  const row = await request(path("/autosmm/rules"), { method: "POST", body: {
    ...(keyword ? { keyword } : { lotId }), serviceId: form.elements.serviceId.value.trim(),
    quantity: form.elements.quantity.valueAsNumber,
  } });
  if (sequence !== drawerSequence) return;
  smmPanel.rules = [row, ...smmPanel.rules.filter((entry) => entry.id !== row.id)];
  smmSelectedService = "";
  renderSmmPanel();
  notify("Привязка сохранена");
}

async function smmUpdateRule(id, patch = null) {
  const sequence = drawerSequence;
  const row = await request(path(`/autosmm/rules/${encodeURIComponent(id)}`), { method: patch === null ? "DELETE" : "PATCH", body: patch === null ? undefined : patch });
  if (sequence !== drawerSequence) return;
  smmPanel.rules = patch === null ? smmPanel.rules.filter((entry) => entry.id !== row.id)
    : smmPanel.rules.map((entry) => entry.id === row.id ? row : entry);
  renderSmmPanel();
  notify(patch === null ? "Привязка удалена" : "Привязка сохранена");
}

function showSmmOrder(order) {
  showDrawer("Заказ AutoSMM", `<div class="wizard-publication-summary">${icon("bag")}<div><small>Заказ панели</small><strong>#${safe(order.orderId)}</strong><p>${safe(order.serviceName)}</p></div></div>
    <div class="detail-rows"><div><span>Статус заказа</span>${smmStateBadge(order.status)}</div>
      <div><span>Услуга</span><strong>#${safe(order.serviceId)}</strong></div><div><span>Количество</span><strong>${count(order.quantity)}</strong></div>
      <div><span>Сделка Playerok</span><strong>${safe(order.dealId)}</strong></div>
      <div><span>Ссылка</span><strong>${safe(order.link)}</strong></div>
      <div><span>Refill</span>${smmStateBadge(order.refillStatus || "Не запрашивался")}</div>
      ${order.refillId ? `<div><span>ID refill</span><strong>#${safe(order.refillId)}</strong></div>` : ""}</div>
    ${order.canRefill ? `<form id="smm-refill-form" data-order="${safe(order.id)}">
      <label class="field inline">Подтверждаю ручной запрос refill<input type="checkbox" name="confirm" required></label>
      ${order.legacy ? '<label class="field inline">Старый заказ принадлежит текущей панели<input type="checkbox" name="confirmLegacy" required></label>' : ""}
      <p class="form-error" role="alert" hidden></p><button class="button primary" type="submit">${icon("refresh")}Запросить refill</button></form>`
      : '<p class="payout-note">Refill сейчас недоступен, уже запрошен или не поддерживается услугой. Обновите статус.</p>'}
    <div class="detail-buttons">
      ${button("Обновить", "smmOrder", { value: order.id, glyph: "refresh" })}
      ${button("История", "smmTab", { value: "orders", glyph: "back", variant: "flat" })}</div>`);
}

async function smmOrderDrawer(id) {
  const sequence = ++drawerSequence;
  showDrawer("Заказ AutoSMM", skeleton());
  try {
    const result = await request(path(`/autosmm/orders/${encodeURIComponent(id)}`));
    if (sequence !== drawerSequence) return;
    showSmmOrder(result);
  } catch (error) {
    if (sequence === drawerSequence) showDrawer("Заказ AutoSMM", placeholder("Статус недоступен", error.message, "info",
      button("Обновить", "smmOrder", { value: id, glyph: "refresh" })) + button("К истории", "smmTab", { value: "orders" }));
  }
}

async function submitSmmRefill(form) {
  const sequence = drawerSequence;
  const id = form.dataset.order;
  if (!form.elements.confirm.checked) throw new Error("Подтвердите запрос.");
  const result = await request(path(`/autosmm/orders/${encodeURIComponent(id)}/refill`), {
    method: "POST", body: { confirm: true, confirmLegacy: Boolean(form.elements.confirmLegacy?.checked) },
  });
  if (sequence !== drawerSequence) return;
  showSmmOrder(result);
  notify("Запрос refill принят");
}

function askConfirmation(message, action) {
  if (telegram?.isVersionAtLeast?.("6.2") && telegram.showConfirm) {
    telegram.showConfirm(message, (confirmed) => { if (confirmed) action().catch((error) => notify(error.message, true)); });
  } else if (window.confirm(message)) {
    action().catch((error) => notify(error.message, true));
  }
}

function openTelegramStock() {
  const username = bootstrap?.botUsername;
  if (!/^[A-Za-z0-9_]{5,32}$/.test(username || "")) return;
  const url = `https://t.me/${username}?start=tg_accounts`;
  if (telegram?.openTelegramLink) telegram.openTelegramLink(url);
  else window.open(url, "_blank", "noopener,noreferrer");
}

function listingLink(slug) {
  if (!slug) return;
  const url = `https://playerok.com/products/${encodeURIComponent(slug)}`;
  if (telegram?.openLink) telegram.openLink(url);
  else window.open(url, "_blank", "noopener,noreferrer");
}

document.addEventListener("change", (event) => {
  const target = event.target;
  if (target.matches?.('#listing-publish-form [name="tariff"]')) updatePublicationEstimate();
  if (target.matches?.('#listing-create-form [name="images"]')) {
    const photos = [...target.files];
    const summary = document.querySelector("#listing-photo-summary");
    if (summary) summary.textContent = photos.length ? `${photos.length} фото · ${(photos.reduce((sum, file) => sum + file.size, 0) / 1024 / 1024).toFixed(1)} МБ` : "Файлы не выбраны";
  }
  if (target.id !== "payout-provider") return;
  const provider = payoutProviderData(target.value);
  const body = document.querySelector("#payout-body");
  const amount = document.querySelector("#payout-amount")?.value || "";
  if (body && provider) body.innerHTML = payoutBody(provider, amount);
  refreshPayoutEstimate();
});

document.addEventListener("input", (event) => {
  if (event.target.id === "payout-amount") refreshPayoutEstimate();
});

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
  if (action === "balance") balanceDrawer();
  if (action === "withdrawals") withdrawalsDrawer(value);
  if (action === "newPayout") payoutDrawer();
  if (action === "payoutSite") openPayoutSite();
  if (action === "payoutContacts") openPayoutSite(true);
  if (action === "myListings") {
    salesTab = "listings";
    go("sales");
  }
  if (action === "editListing") editListing(value);
  if (action === "newListing") newListing();
  if (action === "listingGame") chooseListingGame(value);
  if (action === "listingMoreGames") {
    target.disabled = true;
    loadCatalogGames(listingWizard.query, true).finally(() => { target.disabled = false; });
  }
  if (action === "listingGamesBack") { drawerSequence += 1; showCatalogGames(); }
  if (action === "listingCategory") chooseListingCategory(value);
  if (action === "listingMethod") chooseListingCategory(listingWizard.schema.id, value);
  if (action === "listingCategoriesBack") { drawerSequence += 1; showListingCategories(); }
  if (action === "listingSchemaBack") {
    drawerSequence += 1;
    if (listingWizard.schema.methods.length) showListingMethods();
    else showListingCategories();
  }
  if (action === "publishListing") listingPublication(value);
  if (action === "telegramPlugin") telegramPluginDrawer();
  if (action === "autosmmPlugin") smmPluginDrawer();
  if (action === "smmTab") smmChangeTab(value);
  if (action === "smmBalance") smmBalance(target);
  if (action === "smmBindService") { smmSelectedService = value; smmChangeTab("rules"); }
  if (action === "smmCatalogMore") loadSmmCatalog(smmQuery, Number(value));
  if (action === "smmOrdersMore") loadSmmOrders(Number(value));
  if (action === "smmOrder") smmOrderDrawer(value);
  if (action === "smmToggleRule") {
    target.disabled = true;
    smmUpdateRule(value, { enabled: target.getAttribute("aria-checked") !== "true" })
      .catch(error => notify(error.message, true)).finally(() => { target.disabled = false; });
  }
  if (action === "smmDeleteRule") askConfirmation("Удалить привязку?", () => smmUpdateRule(value));
  if (action === "telegramTab") telegramChangeTab(value).catch((error) => notify(error.message, true));
  if (action === "telegramMoreLots") loadTelegramLots(true).catch((error) => notify(error.message, true));
  if (action === "telegramStockBot") openTelegramStock();
  if (action === "telegramReset") askConfirmation("Сбросить Telegram API к стандартным значениям?", () => telegramConfig({ reset: true }));
  if (action === "telegramAiSwitch") {
    target.disabled = true;
    telegramAi({ enabled: target.getAttribute("aria-checked") !== "true" })
      .catch((error) => notify(error.message, true)).finally(() => { target.disabled = false; });
  }
  if (action === "telegramAiResetToken") askConfirmation("Удалить сохранённый токен ИИ?", () => telegramAi({ resetToken: true }));
  if (action === "telegramRuleSwitch") {
    target.disabled = true;
    telegramRule(value, "PATCH", { enabled: target.getAttribute("aria-checked") !== "true" })
      .catch((error) => notify(error.message, true)).finally(() => { target.disabled = false; });
  }
  if (action === "telegramDeleteRule") askConfirmation("Удалить эту привязку?", () => telegramRule(value, "DELETE"));
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

drawer.addEventListener("close", () => { drawerSequence += 1; });

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
    if (form.id === "payout-form") await submitPayout(form);
    if (form.id === "smm-bind-form") await smmBind(form);
    if (form.id === "smm-search-form") await loadSmmCatalog(form.elements.q.value.trim());
    if (form.id === "smm-refill-form") await submitSmmRefill(form);
    if (form.classList.contains("smm-rule-form")) await smmUpdateRule(form.dataset.rule, {
      quantity: form.elements.quantity.valueAsNumber, autoSent: form.elements.autoSent.checked,
    });
    if (form.id === "listing-catalog-form") await loadCatalogGames(form.elements.q.value.trim());
    if (form.id === "listing-create-form") await submitListingDraft(form);
    if (form.id === "listing-publish-form") await submitPublication(form);
    if (form.id === "listing-form") await saveListingForm(form);
    if (form.id === "listing-photo-form") await sendPhotoForm(form);
    if (form.id === "telegram-ai-form") {
      const data = new FormData(form);
      const patch = {
        baseUrl: String(data.get("baseUrl") || "").trim(),
        model: String(data.get("model") || "").trim(),
        basePrompt: String(data.get("basePrompt") || "").trim(),
        mode: String(data.get("mode") || ""),
        notifyTg: form.elements.notifyTg.checked,
      };
      const token = String(data.get("apiToken") || "").trim();
      if (token) patch.apiToken = token;
      await telegramAi(patch);
    }
    if (form.id === "telegram-api-form") {
      const patch = {};
      const apiId = form.elements.apiId.valueAsNumber;
      const apiHash = form.elements.apiHash.value.trim();
      if (apiId !== telegramPanel.config.apiId) patch.apiId = apiId;
      if (apiHash) patch.apiHash = apiHash;
      if (Object.keys(patch).length) await telegramConfig(patch);
    }
    if (form.id === "telegram-bind-form") {
      await telegramRule(null, "POST", { lotId: form.elements.lotId.value, country: form.elements.country.value.trim() });
    }
    if (form.id === "telegram-keyword-form") {
      await telegramRule(null, "POST", { keyword: form.elements.keyword.value.trim(), country: form.elements.country.value.trim() });
    }
    if (form.matches(".telegram-rule")) {
      await telegramRule(form.dataset.rule, "PATCH", { country: form.elements.country.value.trim() });
    }
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

desk.addEventListener("error", (event) => {
  if (event.target.matches?.(".event-cover img")) event.target.hidden = true;
}, true);

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
