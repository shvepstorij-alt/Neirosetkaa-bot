/* Shared client guidance. Status/support requests never call activation endpoints. */
(function () {
  'use strict';
  var claude = !!document.getElementById('org-input');
  var service = claude ? 'claude' : 'chatgpt';
  var name = claude ? 'Claude' : 'ChatGPT';
  var accent = claude ? 'orange' : 'green';
  var returnScreen = 's2', jobRef = '', busy = false;
  var waiting = 'Подтверждение ещё не получено. Подожди до 30 минут с начала активации: подписка может появиться автоматически. Оплата сохранена. Повторять активацию и оплачивать заново не нужно. Результат придёт в чат бота.';
  var style = document.createElement('style');
  style.textContent = '.activation-help{width:100%;margin:16px 0;text-align:left;color:var(--text);font-size:14px;line-height:1.6}.activation-help details{border-top:1px solid var(--border);padding:12px 0}.activation-help summary{cursor:pointer;min-height:28px;font-weight:600}.activation-help p{margin:8px 0;color:var(--text2)}.activation-help-actions{width:100%;display:grid;gap:9px;margin:12px 0}.activation-help-note{font-size:14px;line-height:1.6;color:var(--text2);white-space:pre-line;margin:14px 0}.activation-help button{white-space:normal}.activation-help-code{overflow-wrap:anywhere}';
  document.head.appendChild(style);
  style.textContent += '.activation-help-actions .btn-outline{color:var(--text);background:var(--bg2);border-color:var(--border)}';
  function button(text, action, primary) {
    var b = document.createElement('button'); b.type = 'button';
    b.className = 'btn ' + (primary ? 'btn-' + accent : 'btn-outline');
    b.textContent = text; b.addEventListener('click', action); return b;
  }
  function detail(title, text) {
    var d = document.createElement('details'), s = document.createElement('summary'), p = document.createElement('p');
    s.textContent = title; p.textContent = text; d.append(s, p); return d;
  }
  function current() { var e = document.querySelector('.screen.active'); return e ? e.id : 's2'; }
  function showHelp(missing) {
    returnScreen = current() === 'activation-help-screen' ? returnScreen : current();
    var body = document.getElementById('activation-help-body'); body.replaceChildren();
    var h = document.createElement('h2'); h.textContent = missing ? 'Подписка не появилась?' : 'Помощь с активацией'; body.appendChild(h);
    if (missing) {
      body.appendChild(detail('1. Обнови страницу сервиса', 'Открой сайт ' + name + ' и обнови страницу. Если на сайте тариф есть, а в приложении нет — переоткрой приложение.'));
      body.appendChild(detail('2. Проверь аккаунт', 'Убедись, что вошёл в тот аккаунт, данные которого указал при активации.'));
      body.appendChild(detail('3. Если прошло 30 минут', 'Если тариф по-прежнему не отображается, передай заказ на проверку. Повторно активировать или оплачивать его не нужно.'));
      body.querySelector('details').open = true;
    } else {
      body.appendChild(detail('Нужно ли повторять активацию?', 'При ожидании или обрыве связи — нет. Запрос мог уже поступить поставщику. Кнопка «Проверить статус» только уточняет результат и не расходует новый код.'));
      body.appendChild(detail('Можно закрыть мини-приложение?', 'Да. Запущенная на сервере активация и проверка продолжатся. Итог придёт в чат бота.'));
      body.appendChild(detail('Что будет через полчаса?', 'Если подтверждения не будет, бот зарегистрирует обращение и уведомит Александра. После регистрации сообщение придёт в чат бота.'));
      body.appendChild(detail('Нужно ли оплачивать ещё раз?', 'Нет. Для исправления текущей активации новый заказ не нужен.'));
    }
    var actions = document.createElement('div'); actions.className = 'activation-help-actions';
    actions.append(button('Проверить статус', function () { check('status'); }, true),
      button(missing ? 'Прошло 30 минут — нужна проверка' : 'Подсказки не помогли — передать на проверку', function () { check(missing ? 'missing' : 'support'); }),
      button('Назад', function () { showScreen(returnScreen); }));
    body.appendChild(actions);
    var note = document.createElement('div'); note.id = 'activation-help-feedback'; note.className = 'activation-help-note'; note.setAttribute('role', 'status'); body.appendChild(note);
    showScreen('activation-help-screen');
  }
  var panel = document.createElement('div'); panel.id = 'activation-help-screen'; panel.className = 'screen';
  panel.innerHTML = '<div style="padding:24px 18px"><div id="activation-help-body" class="activation-help"></div></div>';
  document.body.appendChild(panel);
  var input = document.getElementById(claude ? 'org-input' : 'token-input');
  if (input) {
    var tips = document.createElement('div'); tips.className = 'activation-help';
    tips.appendChild(detail(claude ? 'Где найти Organization ID?' : 'Страница пустая или показывает {}?', claude
      ? 'Открой claude.ai/settings/account в браузере, где ты вошёл в нужный аккаунт. Найди Organization ID и скопируй значение целиком, вместе с дефисами. Почта и User ID не подходят.'
      : 'Войди в нужный аккаунт на chatgpt.com в браузере. Затем в этом же браузере открой chatgpt.com/api/auth/session и обнови страницу.'));
    tips.appendChild(detail(claude ? 'Какой аккаунт будет подключён?' : 'Как скопировать текст целиком?', claude
      ? 'Подписка оформляется на аккаунт с указанным Organization ID. Перед копированием проверь, что вошёл в нужный аккаунт.'
      : 'На странице сессии выбери «Выбрать всё» → «Скопировать» и вставь сюда. Не копируй только предупреждение. Текст сессии вставляй только в поле активации, не отправляй в сообщения.'));
    input.parentElement.insertAdjacentElement('afterend', tips);
  }
  ['s5', 's6'].forEach(function (id) {
    var screen = document.getElementById(id); if (!screen) return;
    // Replace the primary route to personal messages with contextual help.
    screen.querySelectorAll('a[href*="t.me/neirosetkaalex"]').forEach(function (a) {
      a.replaceWith(button('Помощь с активацией', function () { showHelp(false); }));
    });
    var actions = screen.querySelector('.btn-wrap-inline');
    if (actions) {
      var b = button('Проверить статус заказа', function () { check('status'); });
      b.style.marginBottom = '9px'; actions.appendChild(b);
      if (!claude) actions.appendChild(button('Открыть ChatGPT ↗', openService));
    }
  });
  var success = document.querySelector('#s4 .btn-wrap-inline');
  if (success) success.prepend(button('Подписка не появилась', function () { showHelp(true); }));
  function openService() {
    var url = claude ? 'https://claude.ai' : 'https://chatgpt.com';
    if (typeof tg !== 'undefined' && tg && tg.openLink) tg.openLink(url); else window.open(url, '_blank', 'noopener');
  }
  var originalError = window.showError, originalProcessing = window.showProcessing;
  window.showError = function (msg, kind) {
    originalError(msg);
    // Retry only for a definite input/authorization issue, never an arbitrary failure.
    var retry = ['token', 'bad_org', 'has_plan', 'input', 'auth'].indexOf(kind) >= 0 || /токен не найден|скопирован не (?:полностью|целиком)|неверный формат.*Organization|не тот текст|не принял токен|ошибка авторизации|не принял Organization/i.test(msg || '');
    var b = document.querySelector('#s5 .btn-' + accent);
    if (b) b.style.display = retry ? '' : 'none';
    var title = document.querySelector('#s5 .error-title');
    if (title && !retry) title.textContent = 'Нужно проверить заказ';
  };
  window.showProcessing = function (msg) { originalProcessing(msg || waiting); };
  async function check(action) {
    if (busy) return;
    busy = true;
    var target = document.getElementById('activation-help-feedback');
    if (!target || current() !== 'activation-help-screen') {
      var screen = document.querySelector('.screen.active');
      target = screen && screen.querySelector('.activation-help-note');
      if (!target && screen) { target = document.createElement('div'); target.className = 'activation-help-note'; target.setAttribute('role', 'status'); (screen.querySelector('.result-screen') || screen).appendChild(target); }
    }
    if (target) target.textContent = 'Проверяем заказ…';
    var controller = new AbortController(), timeout = setTimeout(function () { controller.abort(); }, 15000);
    try {
      var code = document.getElementById('code-input');
      var response = await fetch('/api/activation-help', {method: 'POST', headers: {'Content-Type':'application/json'}, signal:controller.signal,
        body: JSON.stringify({service:service, code:code ? code.value.trim() : '', job_ref:jobRef, init_data:typeof tg !== 'undefined' && tg ? tg.initData : '', action:action})});
      var data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Не удалось проверить заказ. Попробуй чуть позже.');
      if (data.state === 'success' && action === 'status') { showScreen('s4'); return; }
      if (current() !== 'activation-help-screen') {
        if (data.state === 'pending') { showProcessing(data.message); return; }
        if (data.state === 'review' || data.state === 'manual') { showManual(data.message); return; }
        if (data.state === 'input') { showError(data.message, 'input'); return; }
      }
      if (target) target.textContent = data.message || 'Статус уточняется.';
    } catch (e) {
      if (target) target.textContent = e.name === 'AbortError' ? 'Ответ задерживается. Можно повторить проверку статуса. Саму активацию повторять не нужно.' : (e.message || 'Проверка временно недоступна.');
    } finally { clearTimeout(timeout); busy = false; }
  }
  window.activationHelp = {open:showHelp, check:check, waiting:waiting, setRef:function(ref) { jobRef=String(ref || ''); }};
  if (new URLSearchParams(location.search).get('help') === '1') {
    window.addEventListener('load', function () { showHelp(false); });
  }
})();
