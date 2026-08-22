/* Логика страницы: настройки формы (SPEC §7) и живые подписи.
   Игровых чисел в этом файле нет — всё приходит из data-атрибутов сервера. */
(function () {
  "use strict";

  var STORAGE_KEY = "gascalc.settings";
  var THEME_KEY = "gascalc.theme";

  /* Вошедший хранит настройки на сервере: браузер может быть чужим или
     смениться. Аноним остаётся в localStorage — это по-прежнему рабочий режим. */
  var loggedIn = document.body.hasAttribute("data-character");

  /* Тема. У анонима она про этот браузер и живёт в localStorage; у вошедшего
     побеждает аккаунт и уезжает в базу, иначе настройка не переезжает между
     машинами — ровно то, что чинит этап 18. Первичное применение в <head>,
     до отрисовки; здесь только переключение (SPEC §10.3). */
  var themeButton = document.getElementById("theme-toggle");
  if (themeButton) {
    /* Кнопка обязана сойтись с тем, что на экране: у анонима атрибут
       проставил скрипт из <head>, и сервер про его выбор ничего не знал. */
    themeButton.setAttribute(
      "aria-pressed",
      document.documentElement.getAttribute("data-theme") === "light" ? "true" : "false"
    );
    themeButton.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      themeButton.setAttribute("aria-pressed", next === "light" ? "true" : "false");
      try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* приватный режим */ }
      if (!loggedIn) { return; }
      /* Отдельный запрос с одним полем: общий save затирает незаполненные
         поля, и полный набор значений тут пришлось бы собирать заново. */
      fetch("/settings/save", {
        method: "POST",
        body: new URLSearchParams({theme: next})
      }).catch(function () { /* сеть моргнула — тема уже на экране */ });
    });
  }

  var form = document.getElementById("calc-form");
  if (!form) { return; }

  /* Сохраняем: ставки доставки, структуру, GDE, брокера, процент обеспечения,
     чекбоксы. Цены НЕ сохраняем — устаревают за минуты. */
  function savedFieldNames() {
    var names = ["structure", "gde_level", "broker_fee", "collateral_pct",
                 "sell_only", "buy_only", "hide_illiquid", "best_per_hub",
                 "sort", "sort_dir"];
    Array.prototype.forEach.call(form.elements, function (el) {
      if (el.name && /_rate$/.test(el.name)) { names.push(el.name); }
    });
    return names;
  }

  function getField(name) {
    var el = form.elements[name];
    if (!el) { return null; }
    if (typeof RadioNodeList !== "undefined" && el instanceof RadioNodeList) { return el.value; }
    if (el.type === "checkbox") { return el.checked; }
    return el.value;
  }

  function setField(name, value) {
    var el = form.elements[name];
    if (!el) { return; }
    if (typeof RadioNodeList !== "undefined" && el instanceof RadioNodeList) { el.value = value; return; }
    if (el.type === "checkbox") { el.checked = value === true; return; }
    el.value = value;
  }

  function saveSettings() {
    var data = {};
    savedFieldNames().forEach(function (name) { data[name] = getField(name); });
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(data)); } catch (e) { /* приватный режим */ }
  }

  function restoreSettings() {
    var raw = null;
    try { raw = localStorage.getItem(STORAGE_KEY); } catch (e) { return; }
    if (!raw) { return; }
    var data;
    try { data = JSON.parse(raw); } catch (e) { return; }
    savedFieldNames().forEach(function (name) {
      if (Object.prototype.hasOwnProperty.call(data, name)) { setField(name, data[name]); }
    });
  }

  /* «Разжатие: N%» — карта процентов посчитана на сервере тем же ядром.
     Полоска рядом показывает ту же величину, поэтому едет вместе с числом. */
  var etaValue = document.getElementById("eta-value");
  var etaBar = document.getElementById("eta-bar");
  var etaMap = null;
  if (etaValue) {
    try { etaMap = JSON.parse(etaValue.getAttribute("data-eta-map")); } catch (e) { etaMap = null; }
  }
  function updateEta() {
    if (!etaMap || !etaValue) { return; }
    var byStructure = etaMap[getField("structure")];
    var pct = byStructure && byStructure[getField("gde_level")];
    if (pct === undefined) { return; }
    etaValue.textContent = pct;
    if (etaBar) { etaBar.style.width = pct + "%"; }
  }

  /* «5 м³ сырой / 0.5 м³ сжатый» — строка готова на сервере, в data-volumes.
     Имя газа и его объёмы стоят в шапке: она обязана говорить про то, что
     выбрано сейчас, а не про то, с чем страница открылась. */
  var gasSelect = document.getElementById("gas-select");
  var gasVolumes = document.getElementById("gas-volumes");
  var gasName = document.getElementById("header-gas");
  function updateVolumes() {
    if (!gasSelect) { return; }
    var option = gasSelect.selectedOptions[0];
    if (!option) { return; }
    if (gasVolumes) { gasVolumes.textContent = option.getAttribute("data-volumes"); }
    if (gasName) { gasName.textContent = option.textContent.trim(); }
  }

  var resetButton = document.getElementById("reset-settings");
  if (resetButton) {
    resetButton.addEventListener("click", function () {
      try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
      if (!loggedIn) { location.reload(); return; }
      /* У вошедшего настройки лежат в базе, и очистить один браузер мало:
         после перезагрузки они вернулись бы с сервера. Пустая форма затирает
         сохранённое, и страница снова открывается на умолчаниях. */
      fetch("/settings/save", {method: "POST", body: new URLSearchParams()})
        .catch(function () { /* не вышло — покажем как есть, а не соврём */ })
        .then(function () { location.reload(); });
    });
  }

  /* Ручной ввод — мастер. Правка ячейки снимает пометку «подтянуто из ESI»
     и стирает глубину стакана: глубина относилась к той цене, которой больше нет.
     Слушатель висит на форме, поэтому переживает подмену сетки через HTMX. */
  form.addEventListener("input", function (event) {
    var el = event.target;
    if (!el || !el.hasAttribute || !el.hasAttribute("data-price-cell")) { return; }
    el.classList.remove("fetched");
    el.removeAttribute("title");
    /* Снимаем пометку «из базы»: с этого момента сервер обязан оставить
       ячейку в покое и не подставлять сюда пересчитанную цену. */
    var auto = form.elements[el.name + "_auto"];
    if (auto) { auto.value = ""; }
    var depth = form.elements[el.name + "_depth"];
    if (depth) { depth.value = ""; }
  });

  /* Сетку перерисовал сервер под новую потребность — значит и результат надо
     пересчитать: иначе на экране остались бы цифры, посчитанные по прежним ценам.
     Событие recalc перечислено в hx-trigger формы. */
  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (event.target && event.target.id === "price-grid") {
      window.htmx.trigger(form, "recalc");
    }
  });

  function saveToServer() {
    var data = new URLSearchParams();
    savedFieldNames().forEach(function (name) {
      var value = getField(name);
      if (value === true) { data.append(name, "on"); }
      else if (value !== false && value !== null) { data.append(name, value); }
    });
    ["gas", "n_units"].forEach(function (name) {
      var value = getField(name);
      if (value !== null) { data.append(name, value); }
    });
    fetch("/settings/save", {method: "POST", body: data}).catch(function () {
      /* сеть моргнула — настройки не потеряны, они на экране */
    });
  }

  /* «Только sell» и «только buy» гасят друг друга: вместе они схлопнули бы
     выдачу в ноль, и сервер ответил бы ошибкой формы. Слушатель стоит раньше
     сохраняющего, поэтому в localStorage и на сервер уезжает уже разведённая
     пара. Делегирование на форму — привычка держать слушатели там же. */
  var SIDE_FILTERS = {sell_only: "buy_only", buy_only: "sell_only"};
  form.addEventListener("change", function (event) {
    var el = event.target;
    if (!el || !el.name || !SIDE_FILTERS.hasOwnProperty(el.name)) { return; }
    if (!el.checked) { return; }
    var other = form.elements[SIDE_FILTERS[el.name]];
    if (other) { other.checked = false; }
  });

  /* Крестик в панели разбора. Панель живёт внутри блока результата и при любом
     пересчёте приезжает пустой сама; закрыть её руками — отдельное действие,
     и оно ничего не пересчитывает. */
  form.addEventListener("click", function (event) {
    if (!event.target.closest || !event.target.closest("[data-close-detail]")) { return; }
    var panel = document.getElementById("row-detail");
    if (panel) { panel.innerHTML = ""; }
  });

  /* Клик по заголовку таблицы: сортирует сервер, браузер только записывает
     выбор в скрытые поля и просит пересчитать. Второй порядок правды в JS
     разъехался бы с полосками, которые считаются от размаха (SPEC §5.7).
     Делегирование обязательно: таблицу подменяет HTMX, кнопки в ней новые. */
  form.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("[data-sort]") : null;
    if (!button || !form.contains(button)) { return; }
    setField("sort", button.getAttribute("data-sort"));
    setField("sort_dir", button.getAttribute("data-sort-dir"));
    if (loggedIn) { saveToServer(); } else { saveSettings(); }
    window.htmx.trigger(form, "recalc");
  });

  form.addEventListener("change", function () {
    updateEta();
    updateVolumes();
    if (loggedIn) { saveToServer(); } else { saveSettings(); }
  });

  /* Первый вход: предложить забрать то, что накопилось в браузере анонимно. */
  var importBox = document.getElementById("import-offer");
  if (importBox) {
    var importButton = document.getElementById("import-settings");
    var dismissButton = document.getElementById("dismiss-import");
    if (importButton) {
      importButton.addEventListener("click", function () {
        restoreSettings();
        saveToServer();
        importBox.remove();
        location.reload();
      });
    }
    if (dismissButton) {
      dismissButton.addEventListener("click", function () { importBox.remove(); });
    }
  }

  /* Восстановление — сразу при загрузке: скрипт подключён в конце body,
     выполняется до первой отрисовки, форма не мигает. */
  if (!loggedIn) { restoreSettings(); }
  updateEta();
  updateVolumes();
})();
