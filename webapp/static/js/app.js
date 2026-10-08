/**
 * iOS 27 Liquid Glass - Playerok BOT Mini App Frontend Engine
 * Pure monochrome, zero emojis, fluid reactive glass interactions.
 */

const App = {
  state: {
    user: null,
    activeAccount: null,
    accounts: [],
    stats: {},
    settings: {},
    deals: [],
    chats: [],
    rules: [],
    autoReplies: [],
    plugins: [],
    currentChatId: null,
    pollingInterval: null
  },

  init() {
    this.setupTelegramWebApp();
    this.bindEvents();
    this.refreshAll();
    this.state.pollingInterval = setInterval(() => this.backgroundSync(), 15000);
  },

  setupTelegramWebApp() {
    if (window.Telegram && window.Telegram.WebApp) {
      const tg = window.Telegram.WebApp;
      tg.ready();
      tg.expand();
      try {
        if (tg.setHeaderColor) tg.setHeaderColor('#0a0a0c');
        if (tg.setBackgroundColor) tg.setBackgroundColor('#0a0a0c');
      } catch (e) {}

      // If opening in Telegram WebApp, transmit initData to server for auto-auth
      if (tg.initData) {
        sessionStorage.setItem('tg_init_data', tg.initData);
      }
    }
  },

  async request(endpoint, options = {}) {
    const headers = {
      'Content-Type': 'application/json',
      ...(options.headers || {})
    };

    const tgInitData = sessionStorage.getItem('tg_init_data');
    if (tgInitData) {
      headers['X-Telegram-Init-Data'] = tgInitData;
    }

    try {
      const res = await fetch(endpoint, { credentials: 'same-origin', credentials: 'include', ...options, headers });
      if (res.status === 401) {
        window.location.href = '/login';
        return null;
      }
      const data = await res.json();
      return data;
    } catch (err) {
      console.error('API request failed:', endpoint, err);
      return { ok: false, error: err.message };
    }
  },

  bindEvents() {
    // Tab switching
    document.querySelectorAll('.nav-item').forEach(tab => {
      tab.addEventListener('click', (e) => {
        const targetView = e.currentTarget.getAttribute('data-tab');
        this.switchView(targetView);
      });
    });

    // Account selector
    const accountSelect = document.getElementById('accountSelect');
    if (accountSelect) {
      accountSelect.addEventListener('change', async (e) => {
        const accId = e.target.value;
        if (accId) {
          const res = await this.request('/api/accounts/switch', {
            method: 'POST',
            body: JSON.stringify({ account_id: accId })
          });
          if (res && res.ok) {
            this.toast('Аккаунт переключен');
            await this.refreshAll();
          } else {
            this.toast(res?.error || 'Ошибка переключения');
          }
        }
      });
    }

    // Modal Close buttons
    document.querySelectorAll('[data-close-modal]').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.liquid-modal-backdrop').forEach(m => m.classList.remove('show'));
      });
    });
  },

  switchView(tabId) {
    document.querySelectorAll('.nav-item').forEach(t => {
      t.classList.toggle('active', t.getAttribute('data-tab') === tabId);
    });
    document.querySelectorAll('.view-panel').forEach(p => {
      p.classList.toggle('active', p.id === `view-${tabId}`);
    });

    // Lazy view loads
    if (tabId === 'deals') this.loadDeals();
    if (tabId === 'chats') this.loadChats();
    if (tabId === 'delivery') this.loadDeliveryRules();
    if (tabId === 'autoreply') this.loadAutoReplies();
    if (tabId === 'plugins') this.loadPlugins();
  },

  toast(msg) {
    let t = document.getElementById('liquidToast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'liquidToast';
      t.className = 'liquid-toast';
      document.body.appendChild(t);
    }
    t.innerText = msg;
    t.classList.add('show');
    clearTimeout(this._toastTimeout);
    this._toastTimeout = setTimeout(() => {
      t.classList.remove('show');
    }, 2800);
  },

  async refreshAll() {
    await this.loadMe();
    await this.loadStats();
  },

  async backgroundSync() {
    const currentTab = document.querySelector('.nav-item.active')?.getAttribute('data-tab');
    if (currentTab === 'overview') await this.loadStats();
    if (currentTab === 'deals') await this.loadDeals(true);
    if (currentTab === 'chats' && this.state.currentChatId) await this.loadChats(true);
  },

  async loadMe() {
    const res = await this.request('/api/me');
    if (!res || !res.ok) return;

    this.state.user = res.user;
    this.state.activeAccount = res.active_account;
    this.state.accounts = res.accounts || [];

    const brandStatus = document.getElementById('brandStatus');
    if (brandStatus) {
      if (this.state.activeAccount) {
        brandStatus.innerText = `ONLINE | ${this.state.activeAccount.username}`;
      } else {
        brandStatus.innerText = 'НЕТ ПОДКЛЮЧЕННЫХ АККАУНТОВ';
      }
    }

    const select = document.getElementById('accountSelect');
    if (select) {
      select.innerHTML = '';
      if (this.state.accounts.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.innerText = 'Аккаунты не найдены';
        select.appendChild(opt);
      } else {
        this.state.accounts.forEach(acc => {
          const opt = document.createElement('option');
          opt.value = acc.id;
          opt.innerText = acc.username;
          if (acc.is_active) opt.selected = true;
          select.appendChild(opt);
        });
      }
    }
  },

  async loadStats() {
    const res = await this.request('/api/dashboard');
    if (!res || !res.ok) return;

    const s = res.stats || {};
    this.state.stats = s;
    this.state.settings = res.settings || {};

    const elTotalDeals = document.getElementById('statTotalDeals');
    const elConfirmedDeals = document.getElementById('statConfirmedDeals');
    const elReviews = document.getElementById('statReviews');
    const elMessages = document.getElementById('statMessages');
    const elDeliveries = document.getElementById('statDeliveries');
    const elAutoconfirms = document.getElementById('statAutoconfirms');

    if (elTotalDeals) elTotalDeals.innerText = s.total_deals ?? '0';
    if (elConfirmedDeals) elConfirmedDeals.innerText = s.confirmed_deals ?? '0';
    if (elReviews) elReviews.innerText = s.reviews ?? '0';
    if (elMessages) elMessages.innerText = s.messages ?? '0';
    if (elDeliveries) elDeliveries.innerText = s.delivery_actions ?? '0';
    if (elAutoconfirms) elAutoconfirms.innerText = s.autoconfirm_actions ?? '0';

    // Update settings switches if present
    const switchAutoConfirm = document.getElementById('settingAutoConfirm');
    if (switchAutoConfirm && res.settings) {
      switchAutoConfirm.checked = Boolean(res.settings.auto_confirm);
    }

    const autoConfirmMode = document.getElementById('settingAutoConfirmMode');
    if (autoConfirmMode && res.settings) {
      autoConfirmMode.value = res.settings.auto_confirm_mode || 'all';
    }

    const notifDeals = document.getElementById('notifNewDeal');
    if (notifDeals && res.settings?.notifications) {
      notifDeals.checked = Boolean(res.settings.notifications.new_deal);
    }
    const notifMsg = document.getElementById('notifNewMessage');
    if (notifMsg && res.settings?.notifications) {
      notifMsg.checked = Boolean(res.settings.notifications.new_message);
    }
    const notifReviews = document.getElementById('notifNewReview');
    if (notifReviews && res.settings?.notifications) {
      notifReviews.checked = Boolean(res.settings.notifications.new_review);
    }

    // Load live profile balance
    this.loadProfile();
  },

  async loadProfile() {
    const res = await this.request('/api/playerok/profile');
    if (!res || !res.ok) return;
    const p = res.profile || {};
    const elBalance = document.getElementById('statBalance');
    if (elBalance) {
      elBalance.innerText = `${p.balance ?? 0} RUB`;
    }
  },

  async toggleSetting(key, val) {
    const payload = {};
    payload[key] = val;
    const res = await this.request('/api/settings', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    if (res && res.ok) {
      this.toast('Настройка сохранена');
    } else {
      this.toast(res?.error || 'Ошибка сохранения');
    }
  },

  async toggleNotification(subKey, val) {
    const notifs = {};
    notifs[subKey] = val;
    const res = await this.request('/api/settings', {
      method: 'POST',
      body: JSON.stringify({ notifications: notifs })
    });
    if (res && res.ok) {
      this.toast('Уведомление обновлено');
    }
  },

  // -------------------------------------------------------------------
  // DEALS MANAGEMENT
  // -------------------------------------------------------------------

  async loadDeals(silent = false) {
    const container = document.getElementById('dealsList');
    if (!container) return;
    if (!silent) container.innerHTML = '<div class="card-meta">Загрузка данных...</div>';

    const res = await this.request('/api/playerok/deals');
    if (!res || !res.ok) {
      container.innerHTML = '<div class="card-meta">Не удалось получить сделки</div>';
      return;
    }

    const deals = res.deals || [];
    this.state.deals = deals;

    if (deals.length === 0) {
      container.innerHTML = '<div class="card-meta">Активные сделки отсутствуют</div>';
      return;
    }

    container.innerHTML = deals.map(d => `
      <div class="list-row">
        <div class="row-primary">
          <div class="row-title">#${d.id} - ${d.title}</div>
          <div class="row-subtitle">Покупатель: ${d.buyer} | Сумма: ${d.price} RUB</div>
        </div>
        <div style="display: flex; align-items: center; gap: 10px;">
          <span class="row-badge">${d.status}</span>
          ${d.can_confirm ? `<button class="liquid-btn liquid-btn-sm liquid-btn-primary" onclick="App.confirmDeal('${d.id}')">Подтвердить</button>` : ''}
        </div>
      </div>
    `).join('');
  },

  async confirmDeal(dealId) {
    const res = await this.request('/api/playerok/deals/confirm', {
      method: 'POST',
      body: JSON.stringify({ deal_id: dealId })
    });
    if (res && res.ok) {
      this.toast(`Сделка ${dealId} подтверждена`);
      this.loadDeals();
    } else {
      this.toast(res?.error || 'Ошибка подтверждения');
    }
  },

  // -------------------------------------------------------------------
  // CHATS & MESSAGES
  // -------------------------------------------------------------------

  async loadChats(silent = false) {
    const container = document.getElementById('chatsList');
    if (!container) return;
    if (!silent) container.innerHTML = '<div class="card-meta">Загрузка диалогов...</div>';

    const res = await this.request('/api/playerok/chats');
    if (!res || !res.ok) {
      container.innerHTML = '<div class="card-meta">Не удалось получить диалоги</div>';
      return;
    }

    const chats = res.chats || [];
    this.state.chats = chats;

    if (chats.length === 0) {
      container.innerHTML = '<div class="card-meta">Список диалогов пуст</div>';
      return;
    }

    container.innerHTML = chats.map(c => `
      <div class="list-row" onclick="App.openChat('${c.id}', '${c.username}')" style="cursor: pointer;">
        <div class="row-primary">
          <div class="row-title">${c.username}</div>
          <div class="row-subtitle">${c.last_message || 'Нет сообщений'}</div>
        </div>
        <div style="display: flex; align-items: center; gap: 6px;">
          ${c.unread ? '<span class="row-badge">НОВОЕ</span>' : ''}
          <button class="liquid-btn liquid-btn-sm">Открыть</button>
        </div>
      </div>
    `).join('');
  },

  openChat(chatId, username) {
    this.state.currentChatId = chatId;
    const modal = document.getElementById('chatModal');
    const title = document.getElementById('chatModalTitle');
    if (title) title.innerText = `Диалог: ${username} (ID ${chatId})`;
    if (modal) modal.classList.add('show');
  },

  async sendMessage() {
    const input = document.getElementById('chatInputText');
    const text = input ? input.value.trim() : '';
    if (!text || !this.state.currentChatId) return;

    const res = await this.request('/api/playerok/chats/send', {
      method: 'POST',
      body: JSON.stringify({ chat_id: this.state.currentChatId, text: text })
    });

    if (res && res.ok) {
      this.toast('Сообщение отправлено');
      if (input) input.value = '';
      const body = document.getElementById('chatModalBody');
      if (body) {
        body.innerHTML += `<div class="chat-bubble seller">${text}</div>`;
        body.scrollTop = body.scrollHeight;
      }
    } else {
      this.toast(res?.error || 'Ошибка отправки');
    }
  },

  // -------------------------------------------------------------------
  // AUTO-DELIVERY RULES
  // -------------------------------------------------------------------

  async loadDeliveryRules() {
    const container = document.getElementById('deliveryRulesList');
    if (!container) return;
    container.innerHTML = '<div class="card-meta">Загрузка правил автовыдачи...</div>';

    const res = await this.request('/api/delivery/rules');
    if (!res || !res.ok) {
      container.innerHTML = '<div class="card-meta">Ошибка загрузки правил</div>';
      return;
    }

    const rules = res.rules || [];
    this.state.rules = rules;

    if (rules.length === 0) {
      container.innerHTML = '<div class="card-meta">Правила автовыдачи еще не созданы</div>';
      return;
    }

    container.innerHTML = rules.map(r => `
      <div class="list-row">
        <div class="row-primary">
          <div class="row-title">Товар ID: ${r.item_id} | Режим: ${r.mode}</div>
          <div class="row-subtitle">Остаток на складе: ${r.stock_available} / ${r.stock_total} | ${r.message_template.slice(0, 40)}...</div>
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <button class="liquid-btn liquid-btn-sm" onclick="App.openStockModal(${r.id})">Пополнить склад</button>
          <button class="liquid-btn liquid-btn-sm liquid-btn-danger" onclick="App.deleteDeliveryRule(${r.id})">Удалить</button>
        </div>
      </div>
    `).join('');
  },

  openCreateDeliveryRule() {
    const modal = document.getElementById('deliveryRuleModal');
    if (modal) modal.classList.add('show');
  },

  async submitDeliveryRule() {
    const itemId = document.getElementById('deliveryItemId')?.value.trim();
    const mode = document.getElementById('deliveryMode')?.value;
    const template = document.getElementById('deliveryTemplate')?.value.trim();

    if (!itemId) {
      this.toast('Укажите ID товара');
      return;
    }

    const res = await this.request('/api/delivery/rules', {
      method: 'POST',
      body: JSON.stringify({ item_id: itemId, mode: mode, message_template: template, enabled: true })
    });

    if (res && res.ok) {
      this.toast('Правило автовыдачи сохранено');
      document.getElementById('deliveryRuleModal')?.classList.remove('show');
      this.loadDeliveryRules();
    } else {
      this.toast(res?.error || 'Ошибка создания правила');
    }
  },

  async deleteDeliveryRule(ruleId) {
    const res = await this.request(`/api/delivery/rules/${ruleId}`, { method: 'DELETE' });
    if (res && res.ok) {
      this.toast('Правило удалено');
      this.loadDeliveryRules();
    } else {
      this.toast(res?.error || 'Ошибка удаления');
    }
  },

  openStockModal(ruleId) {
    this.state.targetStockRuleId = ruleId;
    const modal = document.getElementById('stockModal');
    if (modal) modal.classList.add('show');
  },

  async submitStock() {
    const lines = document.getElementById('stockItemsInput')?.value;
    if (!lines || !this.state.targetStockRuleId) {
      this.toast('Введите данные ключей');
      return;
    }

    const res = await this.request(`/api/delivery/rules/${this.state.targetStockRuleId}/stock`, {
      method: 'POST',
      body: JSON.stringify({ lines: lines })
    });

    if (res && res.ok) {
      this.toast(`Добавлено единиц: ${res.added}`);
      document.getElementById('stockModal')?.classList.remove('show');
      document.getElementById('stockItemsInput').value = '';
      this.loadDeliveryRules();
    } else {
      this.toast(res?.error || 'Ошибка добавления на склад');
    }
  },

  // -------------------------------------------------------------------
  // AUTO-REPLY RULES
  // -------------------------------------------------------------------

  async loadAutoReplies() {
    const container = document.getElementById('autoReplyList');
    if (!container) return;
    container.innerHTML = '<div class="card-meta">Загрузка правил автоответа...</div>';

    const res = await this.request('/api/autoreply/rules');
    if (!res || !res.ok) {
      container.innerHTML = '<div class="card-meta">Ошибка загрузки автоответов</div>';
      return;
    }

    const rules = res.rules || [];
    this.state.autoReplies = rules;

    if (rules.length === 0) {
      container.innerHTML = '<div class="card-meta">Правила автоответа отсутствуют</div>';
      return;
    }

    container.innerHTML = rules.map(r => `
      <div class="list-row">
        <div class="row-primary">
          <div class="row-title">Триггер: "${r.trigger}"</div>
          <div class="row-subtitle">Ответ: ${r.response}</div>
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <button class="liquid-btn liquid-btn-sm liquid-btn-danger" onclick="App.deleteAutoReply(${r.id})">Удалить</button>
        </div>
      </div>
    `).join('');
  },

  async addAutoReply() {
    const trigger = document.getElementById('autoreplyTrigger')?.value.trim();
    const response = document.getElementById('autoreplyResponse')?.value.trim();

    if (!trigger || !response) {
      this.toast('Заполните триггер и текст ответа');
      return;
    }

    const res = await this.request('/api/autoreply/rules', {
      method: 'POST',
      body: JSON.stringify({ trigger: trigger, response: response, enabled: true })
    });

    if (res && res.ok) {
      this.toast('Правило автоответа добавлено');
      document.getElementById('autoreplyTrigger').value = '';
      document.getElementById('autoreplyResponse').value = '';
      this.loadAutoReplies();
    } else {
      this.toast(res?.error || 'Ошибка сохранения');
    }
  },

  async deleteAutoReply(ruleId) {
    const res = await this.request(`/api/autoreply/rules/${ruleId}`, { method: 'DELETE' });
    if (res && res.ok) {
      this.toast('Правило удалено');
      this.loadAutoReplies();
    } else {
      this.toast(res?.error || 'Ошибка');
    }
  },

  // -------------------------------------------------------------------
  // PLUGINS MANAGEMENT
  // -------------------------------------------------------------------

  async loadPlugins() {
    const container = document.getElementById('pluginsList');
    if (!container) return;
    container.innerHTML = '<div class="card-meta">Загрузка модулей расширения...</div>';

    const res = await this.request('/api/plugins');
    if (!res || !res.ok) {
      container.innerHTML = '<div class="card-meta">Ошибка загрузки модулей</div>';
      return;
    }

    const plugins = res.plugins || [];
    this.state.plugins = plugins;

    container.innerHTML = plugins.map(p => `
      <div class="list-row">
        <div class="row-primary">
          <div class="row-title">${p.name}</div>
          <div class="row-subtitle">${p.description}</div>
        </div>
        <div style="display: flex; align-items: center; gap: 12px;">
          <span class="row-badge">${p.enabled ? 'АКТИВЕН' : 'ОТКЛЮЧЕН'}</span>
          <label class="switch">
            <input type="checkbox" ${p.enabled ? 'checked' : ''} onchange="App.togglePlugin('${p.id}', this.checked)">
            <span class="slider"></span>
          </label>
        </div>
      </div>
    `).join('');
  },

  async togglePlugin(pluginId, enabled) {
    const res = await this.request('/api/plugins', {
      method: 'POST',
      body: JSON.stringify({ plugin_id: pluginId, enabled: enabled })
    });
    if (res && res.ok) {
      this.toast(`Модуль ${enabled ? 'включен' : 'отключен'}`);
      this.loadPlugins();
    } else {
      this.toast(res?.error || 'Ошибка переключения');
    }
  }
};

document.addEventListener('DOMContentLoaded', () => {
  App.init();
});
