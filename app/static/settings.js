/* Логика страницы: настройки в localStorage (SPEC §7) и живые подписи.
   Игровых чисел в этом файле нет — всё приходит из data-атрибутов сервера. */
(function () {
  "use strict";

  var STORAGE_KEY = "gascalc.settings";
  var form = document.getElementById("calc-form");
  if (!form) { return; }

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
      location.reload();
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
    var depth = form.elements[el.name + "_depth"];
    if (depth) { depth.value = ""; }
  });

  form.addEventListener("change", function () {
    updateEta();
    updateVolumes();
    saveSettings();
  });

  /* Восстановление — сразу при загрузке: скрипт подключён в конце body,
     выполняется до первой отрисовки, форма не мигает. */
  restoreSettings();
  updateEta();
  updateVolumes();
})();
