/* Логика страницы: настройки формы (SPEC §7) и живые подписи.
   Игровых чисел в этом файле нет — всё приходит из data-атрибутов сервера. */
(function () {
  "use strict";

  var STORAGE_KEY = "gascalc.settings";
  var form = document.getElementById("calc-form");
  if (!form) { return; }

  /* Вошедший хранит настройки на сервере: браузер может быть чужим или
     смениться. Аноним остаётся в localStorage — это по-прежнему рабочий режим. */
  var loggedIn = document.body.hasAttribute("data-character");

  /* Сохраняем: ставки доставки, структуру, GDE, брокера, процент обеспечения,
     чекбоксы. Цены НЕ сохраняем — устаревают за минуты. */
  function savedFieldNames() {
    var names = ["structure", "gde_level", "broker_fee", "collateral_pct",
                 "sell_only"];
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

  /* «Разжатие: N%» — карта процентов посчитана на сервере тем же ядром. */
  var etaValue = document.getElementById("eta-value");
  var etaMap = null;
  if (etaValue) {
    try { etaMap = JSON.parse(etaValue.getAttribute("data-eta-map")); } catch (e) { etaMap = null; }
  }
  function updateEta() {
    if (!etaMap || !etaValue) { return; }
    var byStructure = etaMap[getField("structure")];
    var pct = byStructure && byStructure[getField("gde_level")];
    if (pct !== undefined) { etaValue.textContent = pct; }
  }

  /* «5 м³ сырой / 0.5 м³ сжатый» — строка готова на сервере, в data-volumes. */
  var gasSelect = document.getElementById("gas-select");
  var gasVolumes = document.getElementById("gas-volumes");
  function updateVolumes() {
    if (!gasSelect || !gasVolumes) { return; }
    var option = gasSelect.selectedOptions[0];
    if (option) { gasVolumes.textContent = option.getAttribute("data-volumes"); }
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
