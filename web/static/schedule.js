/* Form-based scheduler editor (no manual JSON). Reads /api/schedule, renders rules
   as day-chip + time + temp rows, and POSTs the assembled config back. */
(function () {
  "use strict";

  // i18n bridge — window.I18N is injected by index.html for the active lang.
  var T = function (k, p) {
    var s = (window.I18N && window.I18N[k]) || k;
    if (p) { for (var x in p) s = s.split('{' + x + '}').join(p[x]); }
    return s;
  };
  // "MTWTFSS" / "LMMJVSD" — one char per weekday, indexed 0=Mon … 6=Sun
  var DAYS = T('sched.days').split('');

  var CONTAINER = { heat: "rules-heat", filter: "rules-filter", ready: "rules-ready" };
  var DEFAULTS = {
    heat: { days: [0, 1, 2, 3, 4, 5, 6], time: "18:00", temp: 36 },
    filter: { days: [0, 1, 2, 3, 4, 5, 6], start: "07:00", end: "10:00" },
    ready: { days: [5, 6], time: "10:00", temp: 36 },
  };
  var $ = function (s) { return document.querySelector(s); };

  function chips(days) {
    return '<div class="days">' + DAYS.map(function (l, i) {
      return '<button type="button" class="day' + (days.indexOf(i) >= 0 ? " on" : "") +
        '" data-d="' + i + '">' + l + "</button>";
    }).join("") + "</div>";
  }

  function rowEl(kind, r) {
    var el = document.createElement("div");
    el.className = "rule";
    el.dataset.kind = kind;
    var fields;
    if (kind === "filter") {
      fields = '<input type="time" class="t" value="' + r.start + '">' +
        '<span class="dash">→</span><input type="time" class="t" value="' + r.end + '">';
    } else {
      fields = '<input type="time" class="t" value="' + r.time + '">' +
        '<span class="num"><input type="number" class="temp" min="20" max="40" value="' + r.temp + '">°</span>';
    }
    // structured strings, no untrusted input — values are number/HH:MM only
    el.innerHTML = chips(r.days) +
      '<div class="rfields">' + fields + '<button type="button" class="del" aria-label="' + T('sched.delete_aria') + '">✕</button></div>';
    return el;
  }

  function addRow(kind, r) {
    document.getElementById(CONTAINER[kind]).appendChild(rowEl(kind, r || DEFAULTS[kind]));
  }

  function daysOf(row) {
    return [].slice.call(row.querySelectorAll(".day.on")).map(function (b) { return +b.dataset.d; });
  }

  function collectRules(kind) {
    return [].slice.call(document.getElementById(CONTAINER[kind]).children).map(function (row) {
      var t = row.querySelectorAll(".t");
      if (kind === "filter") return { days: daysOf(row), start: t[0].value, end: t[1].value };
      return { days: daysOf(row), time: t[0].value, temp: +row.querySelector(".temp").value };
    });
  }

  function collect() {
    return {
      enabled: $("#sched-enabled").checked,
      eco_temp: +$("#eco-temp").value,
      heat_rules: collectRules("heat"),
      filter_windows: collectRules("filter"),
      ready_by: collectRules("ready"),
    };
  }

  function populate(cfg) {
    $("#sched-enabled").checked = !!cfg.enabled;
    $("#eco-temp").value = cfg.eco_temp;
    ["heat", "filter", "ready"].forEach(function (k) {
      var c = document.getElementById(CONTAINER[k]); c.textContent = "";
    });
    (cfg.heat_rules || []).forEach(function (r) { addRow("heat", r); });
    (cfg.filter_windows || []).forEach(function (r) { addRow("filter", r); });
    (cfg.ready_by || []).forEach(function (r) { addRow("ready", r); });
  }

  function renderPlan(p) {
    var el = $("#sched-plan");
    el.textContent = "";
    if (!p || !p.enabled) return;
    var bits = [];
    if (p.setpoint != null) bits.push(T('sched.plan_target', {temp: p.setpoint}));
    bits.push(p.heater ? T('sched.plan_heating') : T('sched.plan_resting'));
    if (p.filter != null) bits.push(p.filter ? T('sched.plan_filter_on') : T('sched.plan_filter_off'));
    if (p.heat_rate != null) bits.push(T('sched.plan_rate', {rate: p.heat_rate}));
    var div = document.createElement('div');
    div.className = 'plan-now';
    div.textContent = bits.join(" · ");
    el.appendChild(div);
  }

  document.addEventListener("click", function (e) {
    var add = e.target.closest && e.target.closest(".add");
    if (add) { addRow(add.dataset.add); return; }
    if (e.target.classList && e.target.classList.contains("del")) {
      e.target.closest(".rule").remove(); return;
    }
    if (e.target.classList && e.target.classList.contains("day")) {
      e.target.classList.toggle("on"); return;
    }
  });

  $("#sched-save").addEventListener("click", async function () {
    var msg = $("#sched-msg");
    var r = await fetch("/api/schedule", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(collect()),
    });
    var j = await r.json().catch(function () { return {}; });
    if (r.ok) { msg.textContent = T('sched.saved'); msg.className = "sched-msg ok"; renderPlan(j.plan); }
    else {
      msg.textContent = T('sched.error', {detail: j.detail || ('HTTP ' + r.status)});
      msg.className = "sched-msg err";
    }
  });

  // -- weather + algorithm explainer ----------------------------------------
  function fmtAge(s) {
    if (s == null) return "";
    if (s < 90) return T('weather.age_just_now');
    var m = Math.round(s / 60);
    return m < 60
      ? T('weather.age_minutes', {value: m})
      : T('weather.age_hours', {value: Math.round(m / 60)});
  }

  function renderWeather(w) {
    var card = $("#weather-card");
    if (window.applySky) window.applySky(w);
    if (!w || !w.enabled) { if (card) card.hidden = true; return; }
    card.hidden = false;
    var now = [];
    if (w.air != null) now.push("🌡️ " + w.air + "°");
    if (w.feels != null) now.push(T('weather.feels', {value: w.feels}));
    if (w.wind != null) now.push("💨 " + T('weather.wind', {value: Math.round(w.wind)}));
    if (w.low_12h != null) now.push(T('weather.low_12h', {value: w.low_12h}));
    $("#wx-now").textContent = now.join(" · ") || "—";
    $("#wx-age").textContent = fmtAge(w.age_s);

    // What the scheduler decided right now — structured reasons from the plan.
    // Translation key for each reason is `algo.<kind>`; unknown kinds fall
    // through to the kind string itself (visible debug signal).
    var algoNow = $("#wx-algo-now");
    if (algoNow) {
      algoNow.textContent = "";
      var reasons = (w.plan && w.plan.reasons) || [];
      if (reasons.length) {
        var ul = document.createElement('ul');
        ul.className = 'algo-now-list';
        reasons.forEach(function (r) {
          // Null/undefined params (e.g. current_temp when the spa is offline)
          // would render as the literal string "null"; swap to an em-dash so
          // the line stays readable.
          var params = {};
          for (var k in r) params[k] = (r[k] == null ? '—' : r[k]);
          var li = document.createElement('li');
          li.textContent = T('algo.' + r.kind, params);
          ul.appendChild(li);
        });
        algoNow.appendChild(ul);
      }
    }

    var ex = w.rate_explain, rate = $("#wx-rate");
    if (ex) {
      if (ex.source === "calibrated") {
        rate.textContent = T('weather.rate_calibrated', {
          effective: ex.effective, k_loss: ex.k_loss, water: ex.water, air: ex.air,
        });
      } else if (ex.source === "weather-derate") {
        rate.textContent = T('weather.rate_derate', {
          effective: ex.effective, base: ex.base, factor: ex.factor, air: ex.air,
        });
      } else {
        rate.textContent = T('weather.rate_measured', {effective: ex.effective});
      }
    } else { rate.textContent = "—"; }

    var ph = w.preheat, pe = $("#wx-preheat");
    if (ph) {
      var key = ph.active ? 'weather.preheat_active' : 'weather.preheat_inactive';
      pe.textContent = T(key, {temp: ph.temp, time: ph.time, start: ph.start, lead_h: ph.lead_h});
    } else { pe.textContent = ""; }
    $("#wx-note").textContent = T('weather.note');
  }

  async function loadAll() {
    try {
      var r = await fetch("/api/schedule"); if (!r.ok) return;
      var j = await r.json(); populate(j.config); renderPlan(j.plan);
    } catch (e) { /* ignore */ }
  }
  async function pollPlan() {
    try {
      var r = await fetch("/api/schedule"); if (!r.ok) return;
      renderPlan((await r.json()).plan);
    } catch (e) { /* ignore */ }
  }
  async function pollWeather() {
    try {
      var r = await fetch("/weather"); if (!r.ok) return;
      renderWeather(await r.json());
    } catch (e) { /* ignore */ }
  }

  loadAll();
  pollWeather();
  setInterval(function () { pollPlan(); pollWeather(); }, 30000);
})();
