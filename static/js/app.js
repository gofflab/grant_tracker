/* Grant Tracker front-end glue: HTMX + Alpine + Sortable + Chart.js.
   Loaded (deferred) before Alpine so the alpine:init listeners register in time. */
(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // Alpine stores and components
  // ---------------------------------------------------------------------------
  document.addEventListener("alpine:init", function () {
    Alpine.store("toasts", {
      items: [],
      push: function (message, level) {
        var id = Date.now() + Math.random();
        this.items.push({ id: id, message: message, level: level || "success" });
        var self = this;
        setTimeout(function () { self.remove(id); }, level === "error" ? 7000 : 4200);
      },
      remove: function (id) {
        this.items = this.items.filter(function (t) { return t.id !== id; });
      },
    });

    Alpine.data("themeToggle", function () {
      return {
        mode: document.documentElement.getAttribute("data-theme") || "auto",
        get label() { return { light: "Light", dark: "Dark", auto: "Auto" }[this.mode]; },
        cycle: function () {
          this.mode = { auto: "light", light: "dark", dark: "auto" }[this.mode];
          try {
            if (this.mode === "auto") localStorage.removeItem("gt-theme");
            else localStorage.setItem("gt-theme", this.mode);
          } catch (e) { /* ignore */ }
          if (this.mode === "auto") document.documentElement.removeAttribute("data-theme");
          else document.documentElement.setAttribute("data-theme", this.mode);
          window.dispatchEvent(new CustomEvent("gt-theme-changed"));
        },
      };
    });

    // Key/value editor for the "custom fields" JSON on applications and opportunities.
    Alpine.data("kvEditor", function (inputId) {
      return {
        rows: [],
        init: function () {
          var el = document.getElementById(inputId);
          var data = {};
          try { data = JSON.parse(el.value || "{}") || {}; } catch (e) { data = {}; }
          this.rows = Object.keys(data).map(function (k) { return { key: k, value: String(data[k]) }; });
          this.$watch("rows", this.sync.bind(this), { deep: true });
        },
        add: function () { this.rows.push({ key: "", value: "" }); },
        remove: function (i) { this.rows.splice(i, 1); },
        sync: function () {
          var out = {};
          this.rows.forEach(function (r) { if (r.key.trim()) out[r.key.trim()] = r.value; });
          document.getElementById(inputId).value = JSON.stringify(out);
        },
      };
    });

    // Dynamic Django formset rows (uses a <template> holding the __prefix__ form).
    Alpine.data("formset", function (prefix) {
      return {
        add: function () {
          var total = this.$root.querySelector("#id_" + prefix + "-TOTAL_FORMS");
          var idx = parseInt(total.value, 10);
          var tpl = this.$root.querySelector("template[data-empty-form]");
          var html = tpl.innerHTML.replace(/__prefix__/g, idx);
          var container = this.$root.querySelector("[data-forms]");
          var holder = document.createElement(container.tagName === "TBODY" ? "tbody" : "div");
          holder.innerHTML = html.trim();
          container.appendChild(holder.firstElementChild);
          total.value = idx + 1;
        },
      };
    });

    // Person-months <-> percent effort linked inputs.
    Alpine.data("effortInput", function (initialPm) {
      return {
        pm: initialPm === "" || initialPm === null ? "" : Number(initialPm),
        get pct() { return this.pm === "" ? "" : Math.round((this.pm / 12) * 1000) / 10; },
        setPct: function (v) { this.pm = v === "" ? "" : Math.round((Number(v) * 12 / 100) * 100) / 100; },
      };
    });
  });

  // Django messages rendered into the page become toasts.
  document.addEventListener("alpine:initialized", function () {
    var el = document.getElementById("flash-messages");
    if (!el) return;
    try {
      JSON.parse(el.textContent).forEach(function (m) { Alpine.store("toasts").push(m.message, m.level); });
    } catch (e) { /* ignore */ }
  });

  // ---------------------------------------------------------------------------
  // HTMX integration
  // ---------------------------------------------------------------------------
  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  document.addEventListener("htmx:configRequest", function (evt) {
    evt.detail.headers["X-CSRFToken"] = csrfToken();
  });

  function toast(message, level) {
    if (window.Alpine && Alpine.store("toasts")) Alpine.store("toasts").push(message, level);
  }
  window.gtToast = toast;

  document.addEventListener("toast", function (evt) {
    var d = evt.detail || {};
    toast(d.message || d.value || "Saved", d.level);
  });

  function closeModal() {
    var m = document.getElementById("modal");
    if (m) m.innerHTML = "";
    document.body.style.overflow = "";
  }
  window.gtCloseModal = closeModal;
  // Deferred so sibling HX-Trigger events (section refreshes) still bubble from the form first.
  document.addEventListener("closeModal", function () { setTimeout(closeModal, 0); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && document.querySelector("#modal .modal")) closeModal();
  });
  document.addEventListener("click", function (e) {
    if (e.target.classList && e.target.classList.contains("modal-backdrop")) closeModal();
    var closer = e.target.closest && e.target.closest("[data-close-modal]");
    if (closer) { e.preventDefault(); closeModal(); }
  });

  document.addEventListener("htmx:afterSwap", function (evt) {
    if (evt.detail.target && evt.detail.target.id === "modal" && evt.detail.target.querySelector(".modal")) {
      document.body.style.overflow = "hidden";
      var first = evt.detail.target.querySelector("input:not([type=hidden]):not([type=checkbox]), select, textarea");
      if (first) first.focus();
    }
  });

  document.addEventListener("htmx:responseError", function (evt) {
    var status = evt.detail.xhr.status;
    var msg = status === 403 ? "You don't have permission to do that." :
      status === 404 ? "That item no longer exists." :
      "Something went wrong (" + status + "). Your change may not have been saved.";
    toast(msg, "error");
  });
  document.addEventListener("htmx:sendError", function () {
    toast("Network error. Check your connection and try again.", "error");
  });

  document.addEventListener("htmx:load", function (evt) {
    initBoards(evt.detail.elt);
    renderCharts(evt.detail.elt);
  });

  // "/" focuses global search
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !e.metaKey && !e.ctrlKey) {
      var s = document.getElementById("global-search");
      if (s) { e.preventDefault(); s.focus(); }
    }
  });

  // ---------------------------------------------------------------------------
  // Kanban board (drag a card to change status)
  // ---------------------------------------------------------------------------
  function updateCounts(board) {
    board.querySelectorAll(".column").forEach(function (col) {
      var n = col.querySelectorAll(".kcard").length;
      var c = col.querySelector("[data-count]");
      if (c) c.textContent = n;
    });
  }

  function initBoards(root) {
    if (!window.Sortable) return;
    (root || document).querySelectorAll("[data-sortable-status]").forEach(function (col) {
      if (col._sortable) return;
      col._sortable = Sortable.create(col, {
        group: "applications",
        animation: 150,
        forceFallback: true,
        fallbackTolerance: 4,
        ghostClass: "sortable-ghost",
        dragClass: "sortable-drag",
        disabled: col.hasAttribute("data-readonly"),
        onAdd: function (evt) {
          var card = evt.item;
          var status = evt.to.getAttribute("data-sortable-status");
          htmx.ajax("POST", card.getAttribute("data-status-url"), {
            values: { status: status, quick: "1" },
            swap: "none",
            source: card,
          });
          updateCounts(evt.to.closest(".board"));
        },
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Charts. Each <canvas data-chart="id"> reads its spec from <script type="application/json" id="id">.
  // Spec: {type, labels, series:[{name, data, slot}], stacked, horizontal, format, legend}
  // ---------------------------------------------------------------------------
  var charts = [];

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function fmt(value, format) {
    if (value === null || value === undefined || isNaN(value)) return "–";
    if (format === "money") {
      var a = Math.abs(value);
      if (a >= 1e6) return "$" + (value / 1e6).toFixed(a >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
      if (a >= 1e3) return "$" + Math.round(value / 1e3) + "K";
      return "$" + Math.round(value);
    }
    if (format === "pct") return Math.round(value) + "%";
    if (format === "pm") return (Math.round(value * 100) / 100) + " PM";
    return Number(value).toLocaleString();
  }

  function seriesColor(slot) {
    if (slot === "muted") return cssVar("--series-muted");
    return cssVar("--series-" + (slot || 1));
  }

  function buildChart(canvas) {
    var src = document.getElementById(canvas.getAttribute("data-chart"));
    if (!src || !window.Chart) return;
    var spec;
    try { spec = JSON.parse(src.textContent); } catch (e) { return; }
    var surface = cssVar("--surface");
    var ink2 = cssVar("--ink-2");
    var ink3 = cssVar("--ink-3");
    var grid = cssVar("--grid");
    var horizontal = !!spec.horizontal;
    var stacked = !!spec.stacked;
    var type = spec.type || "bar";
    var n = spec.series.length;

    var datasets = spec.series.map(function (s, i) {
      var color = seriesColor(s.slot || i + 1);
      var ds = { label: s.name, data: s.data, backgroundColor: color, borderColor: color };
      if (type === "bar") {
        ds.maxBarThickness = 24;
        ds.borderSkipped = "start";
        var isTop = !stacked || i === n - 1;
        ds.borderRadius = isTop ? 4 : 0;
        if (stacked) {
          ds.borderColor = surface;
          ds.borderWidth = horizontal ? { right: 2 } : { top: 2 };
        } else {
          ds.borderWidth = 0;
        }
      } else if (type === "line") {
        ds.borderWidth = 2;
        ds.pointRadius = 4;
        ds.pointHoverRadius = 5;
        ds.pointBorderColor = surface;
        ds.pointBorderWidth = 2;
        ds.tension = 0.25;
        ds.fill = !!s.fill;
        if (s.fill) ds.backgroundColor = color + "1a";
        if (s.dashed) ds.borderDash = [5, 4];
      } else if (type === "scatter") {
        ds.pointRadius = 5;
        ds.pointHoverRadius = 6;
        ds.pointBorderColor = surface;
        ds.pointBorderWidth = 2;
      }
      return ds;
    });

    var valueAxis = {
      stacked: stacked,
      beginAtZero: true,
      grid: { color: grid, lineWidth: 1, drawTicks: false },
      border: { display: false },
      ticks: { color: ink3, padding: 6, callback: function (v) { return fmt(v, spec.format); }, maxTicksLimit: 6 },
    };
    if (spec.max !== undefined) valueAxis.max = spec.max;
    if (!spec.format || spec.format === "int") valueAxis.ticks.precision = 0;
    var catAxis = {
      stacked: stacked,
      grid: { display: false },
      border: { color: cssVar("--border-strong") },
      ticks: { color: ink3, autoSkip: true, maxRotation: 0 },
    };
    if (spec.xFormat) catAxis.ticks.callback = function (v) { return fmt(v, spec.xFormat); };

    var options = {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      indexAxis: horizontal ? "y" : "x",
      interaction: { mode: type === "scatter" ? "nearest" : "index", intersect: type === "scatter" },
      layout: { padding: { top: 4 } },
      plugins: {
        legend: {
          display: spec.legend !== undefined ? spec.legend : n > 1,
          position: "top",
          align: "start",
          labels: {
            color: ink2, boxWidth: 10, boxHeight: 10, padding: 14,
            usePointStyle: type === "line", pointStyle: type === "line" ? "line" : "rect",
          },
        },
        tooltip: {
          backgroundColor: cssVar("--ink"),
          titleColor: surface,
          bodyColor: surface,
          padding: 10,
          cornerRadius: 8,
          boxWidth: 10,
          boxHeight: 2,
          callbacks: {
            label: function (ctx) {
              var v = type === "scatter" ? ctx.raw.y : (horizontal ? ctx.parsed.x : ctx.parsed.y);
              var label = fmt(v, spec.format) + "  " + (ctx.dataset.label || "");
              if (type === "scatter" && ctx.raw.label) label = ctx.raw.label + ": " + fmt(ctx.raw.x, spec.xFormat) + ", " + fmt(ctx.raw.y, spec.format);
              return label;
            },
          },
        },
      },
      scales: {},
    };
    if (type === "doughnut") {
      delete options.scales;
      options.cutout = "68%";
      datasets.forEach(function (d) { d.borderColor = surface; d.borderWidth = 2; d.backgroundColor = spec.series[0].colors.map(seriesColor); });
      options.plugins.legend.display = true;
      options.plugins.legend.position = "right";
      options.interaction = { mode: "nearest", intersect: true };
      options.plugins.tooltip.callbacks.label = function (ctx) { return fmt(ctx.parsed, spec.format) + "  " + ctx.label; };
    } else if (type === "scatter") {
      options.scales.x = Object.assign({}, valueAxis, { beginAtZero: false, title: { display: !!spec.xTitle, text: spec.xTitle, color: ink3 } });
      options.scales.x.ticks = Object.assign({}, valueAxis.ticks, { callback: function (v) { return fmt(v, spec.xFormat); } });
      options.scales.y = Object.assign({}, valueAxis, { beginAtZero: false, title: { display: !!spec.yTitle, text: spec.yTitle, color: ink3 } });
      if (spec.yReverse) options.scales.y.reverse = true;
      if (spec.xReverse) options.scales.x.reverse = true;
    } else {
      options.scales[horizontal ? "x" : "y"] = valueAxis;
      options.scales[horizontal ? "y" : "x"] = catAxis;
    }
    var chart = new Chart(canvas.getContext("2d"), { type: type, data: { labels: spec.labels, datasets: datasets }, options: options });
    canvas._chart = chart;
    charts.push(chart);
  }

  function renderCharts(root) {
    (root || document).querySelectorAll("canvas[data-chart]").forEach(function (c) {
      if (c._chart) c._chart.destroy();
      buildChart(c);
    });
  }

  function rerenderAll() {
    charts.forEach(function (c) { try { c.destroy(); } catch (e) { /* noop */ } });
    charts = [];
    document.querySelectorAll("canvas[data-chart]").forEach(function (c) { c._chart = null; buildChart(c); });
  }
  window.addEventListener("gt-theme-changed", rerenderAll);
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rerenderAll);
  }

  document.addEventListener("DOMContentLoaded", function () {
    initBoards(document);
    renderCharts(document);
  });
})();
