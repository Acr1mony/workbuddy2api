/* workbuddy2api 控制台 —— 零构建、零依赖。
   状态全在内存里，刷新即重建；只有面板令牌存 localStorage。
   URL hash 决定当前页，刷新不会丢 tab。 */

(function () {
  "use strict";

  // ---------------- 基础状态 ----------------

  var TOKEN_KEY = "wb2a_panel_token";
  var PAGES = ["overview", "accounts", "usage", "logs", "requests", "settings"];

  var state = {
    token: localStorage.getItem(TOKEN_KEY) || "",
    page: "overview",
    accounts: [],
    selected: Object.create(null),
    filter: { text: "", status: "all", realm: "" },
    sort: "state",
    pageNo: 1,
    pageSize: 10,
    login: { state: "", timer: 0, url: "" },
    logFollow: true,
    timers: {},
    status: null,
    tunables: []
  };

  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  function esc(value) {
    var text = value === null || value === undefined ? "" : String(value);
    return text.replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  // ---------------- 格式化 ----------------

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function fmtTime(epoch) {
    var v = Number(epoch || 0);
    if (!v) return "—";
    var d = new Date(v * 1000);
    var now = new Date();
    var sameDay = d.toDateString() === now.toDateString();
    var hm = pad(d.getHours()) + ":" + pad(d.getMinutes());
    if (sameDay) return hm;
    return (d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + hm;
  }

  function fmtAgo(epoch) {
    var v = Number(epoch || 0);
    if (!v) return "从未";
    var diff = Date.now() / 1000 - v;
    if (diff < 0) return fmtCountdown(-diff) + "后";
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    return Math.floor(diff / 86400) + " 天前";
  }

  function fmtCountdown(seconds) {
    var s = Math.max(0, Math.floor(Number(seconds) || 0));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m" + pad(s % 60) + "s";
    var h = Math.floor(s / 3600);
    if (h < 48) return h + "h" + pad(Math.floor((s % 3600) / 60)) + "m";
    return Math.floor(h / 24) + "d" + (h % 24) + "h";
  }

  function fmtNum(value) {
    var v = Number(value || 0);
    if (!v) return "0";
    if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (v >= 10000) return (v / 1000).toFixed(1) + "k";
    return String(Math.round(v));
  }

  function fmtCredit(value) {
    if (value === null || value === undefined) return "未知";
    var v = Number(value);
    if (!isFinite(v)) return "未知";
    if (v === 0) return "0";
    if (v >= 100) return v.toFixed(0);
    if (v >= 1) return v.toFixed(2);
    return v.toFixed(3);
  }

  function fmtPct(ratio) {
    var v = Number(ratio || 0);
    return (v * 100).toFixed(v >= 0.995 || v === 0 ? 0 : 1) + "%";
  }

  // ---------------- 提示条 ----------------

  function toast(kind, title, body) {
    var host = $("#toasts");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.innerHTML = '<div class="title">' + esc(title) + "</div>" +
      (body ? '<div class="body">' + esc(body) + "</div>" : "");
    host.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .2s";
      el.style.opacity = "0";
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }, kind === "bad" ? 6000 : 3600);
  }

  // ---------------- HTTP ----------------

  function api(path, options) {
    var opts = options || {};
    var headers = { "Accept": "application/json" };
    if (state.token) headers["X-Panel-Token"] = state.token;
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: opts.method || (opts.body !== undefined ? "POST" : "GET"),
      headers: headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body)
    }).then(function (res) {
      if (res.status === 401) {
        showLogin();
        throw new Error("面板会话已过期，请重新登录");
      }
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (!res.ok) {
          var msg = (data && data.error && data.error.message) || text || ("HTTP " + res.status);
          var err = new Error(msg);
          err.status = res.status;
          err.code = data && data.error ? data.error.code : "";
          throw err;
        }
        return data;
      });
    });
  }

  // ---------------- 登录 ----------------

  function showLogin() {
    localStorage.removeItem(TOKEN_KEY);
    state.token = "";
    stopTimers();
    $("#app-view").hidden = true;
    $("#login-view").hidden = false;
    var input = $("#login-password");
    if (input) { input.value = ""; input.focus(); }
  }

  function showApp() {
    $("#login-view").hidden = true;
    $("#app-view").hidden = false;
    boot();
  }

  function doLogin(password) {
    return fetch("/panel/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ password: password })
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (!res.ok) {
          var msg = (data && data.error && data.error.message) || "登录失败";
          throw new Error(msg);
        }
        if (!data || !data.token) throw new Error("登录响应缺少 token");
        state.token = data.token;
        localStorage.setItem(TOKEN_KEY, state.token);
        return data;
      });
    });
  }

  // ---------------- 路由 ----------------

  function currentPageFromHash() {
    var hash = (location.hash || "").replace(/^#/, "");
    return PAGES.indexOf(hash) >= 0 ? hash : "overview";
  }

  function go(page, replace) {
    var target = PAGES.indexOf(page) >= 0 ? page : "overview";
    if (replace) {
      history.replaceState(null, "", "#" + target);
      render();
    } else if (("#" + target) !== location.hash) {
      location.hash = target;
    } else {
      render();
    }
  }

  function render() {
    state.page = currentPageFromHash();
    $$("#tabs a").forEach(function (a) {
      a.classList.toggle("active", a.getAttribute("data-page") === state.page);
    });
    PAGES.forEach(function (name) {
      var el = $("#page-" + name);
      if (el) el.hidden = name !== state.page;
    });
    refreshPage();
  }

  // ---------------- 定时器 ----------------

  function every(name, ms, fn) {
    stopTimer(name);
    state.timers[name] = setInterval(function () {
      if (document.hidden) return;
      fn();
    }, ms);
  }

  function stopTimer(name) {
    if (state.timers[name]) { clearInterval(state.timers[name]); state.timers[name] = 0; }
  }

  function stopTimers() {
    Object.keys(state.timers).forEach(stopTimer);
    if (state.login.timer) { clearInterval(state.login.timer); state.login.timer = 0; }
  }

  function refreshPage() {
    stopTimers();
    if (state.page === "overview") {
      loadOverview();
      every("overview", 5000, loadOverview);
    } else if (state.page === "accounts") {
      loadAccounts();
      every("accounts", 15000, function () { loadAccounts(true); });
    } else if (state.page === "usage") {
      loadUsage();
      every("usage", 15000, loadUsage);
    } else if (state.page === "logs") {
      loadLogs();
      if (state.logFollow) every("logs", 3000, loadLogs);
    } else if (state.page === "requests") {
      loadRequests();
      loadDumps();
      if (reqFilter.follow) every("requests", 5000, loadRequests);
    } else if (state.page === "settings") {
      loadSettings();
      loadProxies();
    }
  }

  // ---------------- 启动 ----------------

  function boot() {
    api("/panel/status").then(function (status) {
      state.status = status;
      $("#version-label").textContent = status.version ? "v" + status.version : "";
      // 页面与服务版本不一致，说明浏览器还在用旧缓存里的脚本：旧脚本绑的是旧 DOM，
      // 表现就是"登录按钮点了没反应"。与其让人怀疑密码，不如直接说清楚。
      var uiVersion = document.documentElement.getAttribute("data-version") || "";
      if (status.version && uiVersion && status.version !== uiVersion) {
        toast("warn", "页面脚本不是最新的",
          "页面 " + uiVersion + " / 服务 " + status.version + "，按 Ctrl+F5（Mac 上 Cmd+Shift+R）强制刷新");
      }
      if (status.is_default_password || status.panel_password_is_default) {
        $("#default-pw-banner").hidden = false;
      }
      if (state.token && !status.authenticated) { showLogin(); return; }
      render();
      every("health", 10000, updateHealth);
      updateHealth();
    }).catch(function (err) {
      toast("bad", "无法连接网关", err.message);
    });
  }

  function updateHealth() {
    fetch("/healthz").then(function (res) { return res.json(); }).then(function (data) {
      var dot = $("#health-dot");
      if (dot) dot.className = "dot " + (data && data.ok ? "ok" : "bad");
    }).catch(function () {
      var dot = $("#health-dot");
      if (dot) dot.className = "dot bad";
    });
  }

  function markUpdated() {
    var el = $("#updated-at");
    if (el) el.textContent = "更新于 " + fmtTime(Date.now() / 1000);
  }

  // ---------------- 概览 ----------------

  function loadOverview() {
    return Promise.all([
      api("/api/usage").catch(function () { return null; }),
      api("/api/usage/recent?limit=20").catch(function () { return null; }),
      api("/api/pool").catch(function () { return null; }),
      api("/api/scheduler").catch(function () { return null; }),
      api("/api/metrics").catch(function () { return null; })
    ]).then(function (res) {
      renderKpis(res[0]);
      renderRecent(res[1]);
      renderPool(res[2]);
      renderScheduler(res[3]);
      renderMetrics(res[4]);
      markUpdated();
    });
  }

  function renderMetrics(metrics) {
    var host = $("#metrics-body");
    if (!host) return;
    if (!metrics || !metrics.window) {
      host.innerHTML = '<p class="muted">读不到指标</p>';
      return;
    }
    var ttfb = metrics.ttfb_ms || {};
    var elapsed = metrics.elapsed_ms || {};
    var requests = metrics.requests || {};
    var outcomes = metrics.outcomes || {};
    var attempts = metrics.attempts || {};
    var models = metrics.models || [];
    var sampled = (metrics.window && metrics.window.samples) || 0;
    $("#metrics-note").textContent = sampled
      ? "最近 " + sampled + " 条样本 · 进程已运行 " + fmtCountdown(metrics.window.uptime_seconds)
      : "还没有样本";

    var outcomeText = ["completed", "client_aborted", "upstream_aborted", "failed"].map(function (name) {
      var label = { completed: "完成", client_aborted: "客户端断", upstream_aborted: "上游断", failed: "失败" }[name];
      return label + " " + fmtNum(outcomes[name] || 0);
    }).join(" · ");
    var attemptText = Object.keys(attempts).sort().map(function (key) {
      return key + "次:" + fmtNum(attempts[key]);
    }).join(" · ");

    var rows = models.slice(0, 8).map(function (row) {
      return "<tr><td>" + esc(row.model) + "</td>" +
        '<td class="num">' + esc(fmtNum(row.requests)) + "</td>" +
        '<td class="num">' + esc(row.avg_ttfb_ms == null ? "—" : Math.round(row.avg_ttfb_ms) + "ms") + "</td>" +
        '<td class="num">' + esc(Math.round(row.avg_elapsed_ms) + "ms") + "</td>" +
        '<td class="num">' + esc(row.errors ? fmtNum(row.errors) : "—") + "</td></tr>";
    }).join("");

    host.innerHTML =
      '<div class="metrics-grid">' +
        kpi("首字 p50", ttfb.p50 ? Math.round(ttfb.p50) + " ms" : "—", "p90 " + Math.round(ttfb.p90 || 0) + " / p99 " + Math.round(ttfb.p99 || 0)) +
        kpi("总耗时 p50", elapsed.p50 ? Math.round(elapsed.p50) + " ms" : "—", "p99 " + Math.round(elapsed.p99 || 0) + " ms") +
        kpi("错误率", requests.sampled ? fmtPct(requests.error_rate) : "—", "样本内 " + fmtNum(requests.errors || 0) + " 次失败", requests.error_rate > 0.2 ? "bad" : (requests.error_rate > 0.05 ? "warn" : "")) +
        kpi("缓存命中", requests.sampled ? fmtPct(requests.cache_hit_rate) : "—", "按带 prompt 的请求算") +
      "</div>" +
      '<p class="tiny muted">结果分布：' + esc(outcomeText) + "</p>" +
      (attemptText ? '<p class="tiny muted">尝试次数：' + esc(attemptText) + "</p>" : "") +
      (rows
        ? '<div class="table-wrap"><table class="data"><thead><tr><th>模型</th><th class="num">请求</th><th class="num">平均首字</th><th class="num">平均总耗时</th><th class="num">失败</th></tr></thead><tbody>' + rows + "</tbody></table></div>"
        : "");
  }

  function kpi(label, value, sub, tone) {
    return '<div class="kpi ' + (tone || "") + '">' +
      '<div class="label">' + esc(label) + "</div>" +
      '<div class="value">' + esc(value) + "</div>" +
      (sub ? '<div class="sub">' + esc(sub) + "</div>" : "") +
      "</div>";
  }

  function renderKpis(usage) {
    var host = $("#kpis");
    if (!host) return;
    if (!usage || !usage.total) {
      host.innerHTML = kpi("状态", "无数据", "还没有处理过请求");
      return;
    }
    var t = usage.total;
    var success = t.requests ? (t.ok / t.requests) : 0;
    var cacheRate = t.cache_hit_rate || 0;
    var errTone = t.errors ? "warn" : "";
    host.innerHTML = [
      kpi("请求", fmtNum(t.requests), success ? "成功 " + fmtPct(success) : "全部失败", t.requests && !t.ok ? "bad" : ""),
      kpi("失败", fmtNum(t.errors), t.errors ? "看日志里的错误行" : "没有失败", errTone),
      kpi("输入 token", fmtNum(t.prompt_tokens), cacheRate ? "缓存命中 " + fmtPct(cacheRate) : "缓存未命中"),
      kpi("输出 token", fmtNum(t.completion_tokens), "含思考 " + fmtNum(t.cached_tokens ? t.cached_tokens : 0) + " 缓存"),
      kpi("消耗积分", fmtCredit(t.credits), usage.retention_days ? "保留 " + usage.retention_days + " 天" : ""),
      kpi("平均耗时", t.avg_latency_ms ? Math.round(t.avg_latency_ms) + " ms" : "—", "含排队时间")
    ].join("");
  }

  function renderRecent(payload) {
    var tbody = $("#recent-table tbody");
    if (!tbody) return;
    var items = (payload && payload.items) || [];
    $("#recent-note").textContent = items.length ? "最近 " + items.length + " 条" : "";
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="empty">还没有请求</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(function (row) {
      var outcome = row.outcome || "";
      var badge = outcome === "completed" ? "ok" : (outcome === "client_aborted" ? "" : "bad");
      var label = { completed: "完成", client_aborted: "客户端断开", upstream_aborted: "上游中断", failed: "失败" }[outcome] || outcome || "—";
      var cacheRate = row.prompt_tokens ? (row.cached_tokens || 0) / row.prompt_tokens : 0;
      return "<tr>" +
        "<td>" + esc(fmtTime(row.at)) + "</td>" +
        "<td>" + esc(row.model || "—") + "</td>" +
        "<td>" + esc((row.account || "").slice(0, 8)) + "</td>" +
        '<td><span class="badge ' + badge + '">' + esc(label) + "</span></td>" +
        '<td class="num">' + esc(row.elapsed_ms ? Math.round(row.elapsed_ms) + "ms" : "—") + "</td>" +
        '<td class="num">' + esc(row.ttft_ms ? Math.round(row.ttft_ms) + "ms" : "—") + "</td>" +
        '<td class="num">' + esc(fmtNum(row.prompt_tokens)) + "</td>" +
        '<td class="num">' + esc(fmtNum(row.completion_tokens)) + "</td>" +
        '<td class="num">' + esc(cacheRate ? fmtPct(cacheRate) : "—") + "</td>" +
        '<td class="num">' + esc(fmtCredit(row.credit)) + "</td>" +
        "<td>" + (row.error ? "<span title=\"" + esc(row.error) + "\">" + esc(String(row.error).slice(0, 40)) + "</span>" : "") + "</td>" +
        "</tr>";
    }).join("");
  }

  function renderPool(pool) {
    var host = $("#pool-body");
    if (!host) return;
    if (!pool) { host.innerHTML = '<p class="muted">读不到池状态</p>'; return; }
    $("#pool-strategy").textContent = "策略 " + (pool.strategy ? ((pool.strategy.cn || "?") + " / " + (pool.strategy.global || "?")) : "");
    var total = pool.total || 0;
    var width = function (n) { return total ? (n / total * 100) + "%" : "0"; };
    var holds = [];
    if (pool.hold_cn_remaining) holds.push("cn 风控暂停 " + fmtCountdown(pool.hold_cn_remaining));
    if (pool.hold_global_remaining) holds.push("global 风控暂停 " + fmtCountdown(pool.hold_global_remaining));
    var waf = pool.waf || {};
    Object.keys(waf).forEach(function (realm) {
      if (waf[realm] && waf[realm].active) {
        holds.push(realm + " 出口被 WAF 盯上（" + waf[realm].uids + " 个号被拦），停手 " +
          fmtCountdown(waf[realm].for_seconds));
      }
    });
    var locks = pool.model_locks || {};
    var lockNames = Object.keys(locks);
    host.innerHTML =
      '<div class="pool-realm">' +
        '<div class="pool-row"><strong>' + fmtNum(pool.ready) + "</strong> 可用 · " +
        fmtNum(pool.cooling) + " 冷却 · " + fmtNum(pool.disabled) + " 停用 · " +
        (pool.paused ? fmtNum(pool.paused) + " 暂停 · " : "") +
        fmtNum(pool.in_flight) + " 在途</div>" +
        '<div class="pool-bars">' +
          '<i class="ready" style="width:' + width(pool.ready) + '"></i>' +
          '<i class="cooling" style="width:' + width(pool.cooling) + '"></i>' +
          '<i class="disabled" style="width:' + width(pool.disabled) + '"></i>' +
        "</div>" +
        '<div class="muted tiny">余额合计 ' + esc(fmtCredit(pool.credits)) +
        (pool.credit_unknown ? " · " + pool.credit_unknown + " 个号余额未知" : "") + "</div>" +
        affinityLine(pool.affinity) +
        (lockNames.length
          ? '<div class="muted tiny" style="margin-top:4px">被锁的模型：' +
            lockNames.slice(0, 4).map(function (name) {
              return esc(name) + "（" + locks[name].accounts + " 个号，" +
                fmtCountdown(locks[name].for_seconds) + "）";
            }).join("；") +
            (lockNames.length > 4 ? " 等 " + lockNames.length + " 个" : "") + "</div>"
          : "") +
      "</div>" +
      (holds.length ? '<div class="muted tiny" style="color:var(--warn)">' + esc(holds.join("；")) + "</div>" : "");
  }

  // 亲和命中率高 = 同一段对话一直落在同一个号上（TTFT 低、不白烧 prompt）；
  // rotations 高 = 长会话在按页换号，说明有人真的在连续用。
  function affinityLine(aff) {
    if (!aff) return "";
    var bits = ["会话粘滞 " + fmtNum(aff.sessions) + " 个 · 命中率 " + fmtPct(aff.hit_rate)];
    if (aff.page_size) bits.push("每 " + aff.page_size + " 轮换号（已换 " + fmtNum(aff.rotations) + " 次）");
    return '<div class="muted tiny">' + esc(bits.join(" · ")) + "</div>";
  }

  function renderScheduler(sched) {
    var host = $("#sched-body");
    if (!host) return;
    if (!sched) { host.innerHTML = '<p class="muted">读不到调度状态</p>'; return; }
    $("#sched-state").textContent = sched.running ? "运行中" : "未启用";
    var jobs = sched.jobs || [];
    if (!jobs.length) { host.innerHTML = '<p class="muted tiny">当前没有任何任务</p>'; return; }
    host.innerHTML = jobs.map(function (job) {
      var when = job.at_hour === null || job.at_hour === undefined
        ? ("每 " + fmtCountdown(job.interval))
        : ("每天 " + pad(job.at_hour) + ":00");
      var last = job.last_run ? fmtAgo(job.last_run) : "未跑过";
      var tone = job.errors ? "warn" : "ok";
      return '<div class="job-row"><span>' + esc(job.name) + "</span>" +
        '<span class="muted tiny">' + esc(when) + " · " + esc(last) +
        (job.last_result ? " · " + esc(String(job.last_result).slice(0, 60)) : "") + "</span>" +
        '<span class="badge ' + (tone === "warn" ? "warn" : "") + '">' + esc(job.runs) + " 次</span></div>";
    }).join("");
  }

  // ---------------- 账号 ----------------

  function loadAccounts(quiet) {
    return api("/api/accounts").then(function (data) {
      var list = (data && data.accounts) || [];
      state.accounts = list;
      // 选择里已经不存在的 uid 要摘掉，否则批量操作会打到不存在的号
      var alive = Object.create(null);
      list.forEach(function (a) { alive[a.uid] = true; });
      Object.keys(state.selected).forEach(function (uid) {
        if (!alive[uid]) delete state.selected[uid];
      });
      renderAccounts();
      markUpdated();
      if (!quiet) {
        var disabledCount = list.filter(function (a) { return a.disabled; }).length;
        $("#acc-summary").textContent = list.length
          ? (list.length + " 个号" + (disabledCount ? " · " + disabledCount + " 个停用" : ""))
          : "还没有账号";
      }
    }).catch(function (err) {
      toast("bad", "读取账号失败", err.message);
    });
  }

  function accountState(account) {
    if (account.disabled) return { key: "disabled", label: "已停用", tone: "" };
    if (account.paused) return { key: "paused", label: "已暂停", tone: "warn" };
    var now = Date.now() / 1000;
    var until = Number(account.throttle_for || 0);
    if (until > now) {
      return { key: "cooling", label: "冷却 " + fmtCountdown(until - now), tone: "warn" };
    }
    if (account.token_expired && !account.has_refresh_token) {
      return { key: "cooling", label: "令牌过期", tone: "bad" };
    }
    return { key: "ready", label: "可用", tone: "ok" };
  }

  /* 一行里说清楚"为什么不接单"：把已经算好的窗口一次性摊开，
     而不是只写"冷却中"让人去翻日志。 */
  function windowBadges(account) {
    var now = Date.now() / 1000;
    var out = [];
    function add(label, until, tone) {
      var v = Number(until || 0);
      if (v > now) out.push('<span class="badge ' + (tone || "") + '">' + esc(label + " " + fmtCountdown(v - now)) + "</span>");
    }
    add("冷却", account.cooldown_until, "warn");
    add("熔断", account.breaker_until, "warn");
    add("降权", account.degrade_until, "warn");
    add("余额", account.balance_until, "warn");
    if (Number(account.restricted_until || 0) > now) {
      out.push('<span class="badge bad" title="11140 风控：换号只会连坐，别换">风控 ' +
        esc(fmtCountdown(Number(account.restricted_until) - now)) + "</span>");
    }
    var models = account.model_cooldowns || {};
    Object.keys(models).slice(0, 2).forEach(function (model) {
      add(model, models[model], "");
    });
    if (Number(account.consecutive_fails || 0) >= 3) {
      out.push('<span class="badge warn">连败 ' + esc(account.consecutive_fails) + "</span>");
    }
    if (account.in_flight) out.push('<span class="badge accent">在途 ' + esc(account.in_flight) + "</span>");
    return out.join("");
  }

  function filteredAccounts() {
    var f = state.filter;
    var text = f.text.trim().toLowerCase();
    var list = state.accounts.filter(function (a) {
      if (f.realm && a.realm !== f.realm) return false;
      if (f.status !== "all") {
        var st = accountState(a);
        if (st.key !== f.status) return false;
      }
      if (!text) return true;
      var hay = [a.nickname, a.uid, a.note, a.group, a.email].join(" ").toLowerCase();
      return hay.indexOf(text) >= 0;
    });
    var now = Date.now() / 1000;
    var rank = { ready: 0, cooling: 1, disabled: 2 };
    list.sort(function (a, b) {
      if (state.sort === "credit") return Number(b.credit || 0) - Number(a.credit || 0);
      if (state.sort === "nickname") return String(a.nickname || "").localeCompare(String(b.nickname || ""));
      if (state.sort === "last_used_at") return Number(b.last_used_at || 0) - Number(a.last_used_at || 0);
      var ra = rank[accountState(a).key] || 9;
      var rb = rank[accountState(b).key] || 9;
      if (ra !== rb) return ra - rb;
      return Number(b.last_used_at || 0) - Number(a.last_used_at || 0);
    });
    void now;
    return list;
  }

  function renderAccounts() {
    var tbody = $("#acc-table tbody");
    if (!tbody) return;
    var list = filteredAccounts();
    var pages = Math.max(1, Math.ceil(list.length / state.pageSize));
    if (state.pageNo > pages) state.pageNo = pages;
    var start = (state.pageNo - 1) * state.pageSize;
    var rows = list.slice(start, start + state.pageSize);

    if (!list.length) {
      var empty = state.accounts.length
        ? "没有符合筛选条件的账号"
        : "还没有账号 —— 点右上角「添加账号」用登录链接接一个";
      tbody.innerHTML = '<tr><td colspan="7" class="empty">' + esc(empty) + "</td></tr>";
    } else {
      tbody.innerHTML = rows.map(function (a) {
        var st = accountState(a);
        var creditText = a.credit === null || a.credit === undefined
          ? '<span class="muted">未知</span>'
          : esc(fmtCredit(a.credit));
        var tokenText = a.has_refresh_token
          ? '<span class="badge ok">可续期</span>'
          : (a.has_access_token ? '<span class="badge warn">仅 access</span>' : '<span class="badge bad">无令牌</span>');
        if (a.token_expired) tokenText += '<span class="badge bad">已过期</span>';
        var checked = state.selected[a.uid] ? " checked" : "";
        return '<tr data-uid="' + esc(a.uid) + '">' +
          '<td class="check"><input type="checkbox" class="acc-check"' + checked + "></td>" +
          "<td>" +
            '<div><strong>' + esc(a.nickname || a.uid.slice(0, 8)) + "</strong> " +
            '<span class="badge">' + esc(a.realm || "cn") + "</span>" +
            (a.group ? ' <span class="badge accent">' + esc(a.group) + "</span>" : "") + "</div>" +
            '<div class="muted tiny mono" title="' + esc(a.last_error || "") + '">' + esc(a.uid) + "</div>" +
            (a.last_error ? '<div class="muted tiny" title="' + esc(a.last_error) + '">最近错误：' + esc(String(a.last_error).slice(0, 46)) + "</div>" : "") +
          "</td>" +
          '<td><span class="badge ' + st.tone + '">' + esc(st.label) + "</span>" + windowBadges(a) + "</td>" +
          '<td class="num">' + creditText + "</td>" +
          '<td class="num">' + tokenText +
            '<div class="muted tiny">' + esc(a.token_expires_at ? fmtTime(a.token_expires_at) : "—") + "</div></td>" +
          "<td>" +
            '<input type="text" class="tiny-btn acc-note" value="' + esc(a.note || "") + '" placeholder="备注" style="width:100%">' +
            '<div class="inline" style="margin-top:4px">' +
              '<input type="text" class="tiny-btn acc-group" value="' + esc(a.group || "") + '" placeholder="分组" style="flex:1 1 70px">' +
              '<input type="number" class="tiny-btn acc-weight" value="' + esc(a.weight || 1) + '" min="1" max="100" title="权重：越大越容易被选中" style="flex:0 0 64px">' +
            "</div>" +
          "</td>" +
          '<td><div class="row-actions">' +
            '<button class="ghost tiny-btn acc-detail">详情</button>' +
            '<button class="ghost tiny-btn acc-test">测试</button>' +
            '<button class="ghost tiny-btn" data-act="refresh">续期</button>' +
            '<button class="ghost tiny-btn" data-act="balance">查额度</button>' +
            (a.realm === "cn" ? '<button class="ghost tiny-btn" data-act="checkin">签到</button>' : "") +
            '<button class="ghost tiny-btn" data-act="report" title="活跃上报：点亮连登天数，企业号是零收益">上报</button>' +
            (a.disabled
              ? '<button class="ghost tiny-btn" data-act="enable">启用</button>'
              : '<button class="ghost tiny-btn" data-act="disable">停用</button>') +
            (a.paused
              ? '<button class="ghost tiny-btn" data-act="resume">恢复</button>'
              : '<button class="ghost tiny-btn" data-act="pause" title="暂停：保留令牌与窗口，只是暂时不接单">暂停</button>') +
            '<button class="ghost tiny-btn danger" data-act="delete">删除</button>' +
          "</div></td>" +
          "</tr>";
      }).join("");
    }

    renderPager(pages, list.length);
    renderBulkBar();
    $("#acc-check-all").checked = rows.length > 0 && rows.every(function (a) { return state.selected[a.uid]; });
  }

  function renderPager(pages, total) {
    var host = $("#acc-pager");
    if (!host) return;
    if (pages <= 1) { host.innerHTML = '<span class="muted tiny">共 ' + total + " 个</span>"; return; }
    var html = '<span class="muted tiny">共 ' + total + " 个 · 第 " + state.pageNo + "/" + pages + " 页</span>";
    html += '<button class="ghost tiny-btn" data-page-go="' + (state.pageNo - 1) + '"' +
      (state.pageNo <= 1 ? " disabled" : "") + ">上一页</button>";
    html += '<button class="ghost tiny-btn" data-page-go="' + (state.pageNo + 1) + '"' +
      (state.pageNo >= pages ? " disabled" : "") + ">下一页</button>";
    host.innerHTML = html;
  }

  function renderBulkBar() {
    var count = Object.keys(state.selected).length;
    var bar = $("#bulkbar");
    if (!bar) return;
    bar.hidden = count === 0;
    $("#bulk-count").textContent = String(count);
  }

  function selectedUids() {
    return Object.keys(state.selected);
  }

  // ---------------- 单号详情 ----------------
  var WINDOW_LABELS = [
    ["cooldown_until", "限流冷却"],
    ["breaker_until", "熔断"],
    ["degrade_until", "降权"],
    ["balance_until", "余额耗尽"],
    ["restricted_until", "风控封禁"]
  ];

  function closeAccountDetail() {
    var drawer = $("#acc-detail");
    if (drawer) drawer.hidden = true;
  }

  function openAccountDetail(uid) {
    var drawer = $("#acc-detail");
    if (!drawer) return;
    drawer.hidden = false;
    $("#detail-title").textContent = "账号详情";
    $("#detail-body").innerHTML = '<p class="muted">加载中…</p>';
    api("/api/accounts/" + encodeURIComponent(uid)).then(function (data) {
      renderAccountDetail(data || {});
    }).catch(function (err) {
      $("#detail-body").innerHTML = '<p class="muted">' + esc(err.message || "加载失败") + "</p>";
    });
  }

  function detailRow(label, value) {
    return '<div class="detail-row"><span class="muted">' + esc(label) + "</span><span>" + value + "</span></div>";
  }

  function renderAccountDetail(data) {
    var a = data.account || {};
    var usage = data.usage || {};
    var now = Date.now() / 1000;
    var html = "";
    $("#detail-title").textContent = a.nickname || (a.uid || "").slice(0, 8) || "账号详情";

    html += '<div class="detail-section">';
    html += detailRow("昵称", esc(a.nickname || "—"));
    html += detailRow("域", '<span class="badge">' + esc(a.realm || "cn") + "</span>");
    html += detailRow("UID", '<span class="mono tiny">' + esc(a.uid || "") + "</span>");
    html += detailRow("状态", a.disabled ? '<span class="badge bad">已停用</span>' : (a.paused ? '<span class="badge warn">已暂停</span>' : '<span class="badge ok">启用中</span>'));
    html += detailRow("余额", a.credit === null || a.credit === undefined ? '<span class="muted">未知</span>' : esc(fmtCredit(a.credit)));
    html += detailRow("令牌", (a.has_refresh_token ? "可续期" : (a.has_access_token ? "仅 access" : "无令牌")) +
      (a.token_expired ? " · <span class=\"badge bad\">已过期</span>" : ""));
    html += detailRow("到期", esc(a.token_expires_at ? fmtTime(a.token_expires_at) : "—"));
    html += detailRow("在途", esc(String(a.in_flight || 0)));
    html += "</div>";

    // 余额的"钱"视角：这些钱分别什么时候过期。只看总数会在最大那个包到期的
    // 早上突然变成 0，那时候再排查就晚了。
    var creditInfo = data.credits || {};
    var bucketMap = creditInfo.buckets || {};
    var bucketLabels = { d3: "3 天内到期", d7: "7 天内到期", d30: "30 天内到期", later: "30 天以上", expired: "已过期（不算额度）" };
    var bucketKeys = Object.keys(bucketLabels).filter(function (key) { return bucketMap[key]; });
    if (bucketKeys.length || creditInfo.expiring_soon) {
      html += '<div class="detail-section"><div class="muted tiny">余额什么时候过期</div>';
      if (creditInfo.expiring_soon) {
        html += '<div class="badge warn">7 天内要过期 ' + esc(fmtCredit(creditInfo.expiring_soon)) + "</div>";
      }
      bucketKeys.forEach(function (key) {
        html += detailRow(bucketLabels[key], esc(fmtCredit(bucketMap[key])));
      });
      if (creditInfo.soonest_expire_at) {
        html += detailRow("最近一次到期", esc(fmtTime(creditInfo.soonest_expire_at)) + "（" + esc(fmtAgo(creditInfo.soonest_expire_at)) + "）");
      }
      html += detailRow("资源包", esc(String(creditInfo.package_count || 0)) + " 个");
      html += "</div>";
    }
    var delta = creditInfo.delta || {};
    if (delta.samples) {
      html += '<div class="detail-section"><div class="muted tiny">最近 7 天的余额变化</div>';
      html += detailRow("净变化", esc((delta.delta > 0 ? "+" : "") + fmtCredit(delta.delta)));
      html += detailRow("其中消耗", esc(fmtCredit(delta.spent)));
      html += "</div>";
    }

    if (a.last_error) {
      html += '<div class="detail-section"><div class="muted tiny">最近错误</div>' +
        '<pre class="log tiny">' + esc(a.last_error) + "</pre></div>";
    }

    var windows = [];
    WINDOW_LABELS.forEach(function (pair) {
      var until = a[pair[0]];
      if (until && until > now) windows.push(pair[1] + " 还剩 " + fmtCountdown(until - now));
    });
    if (windows.length) {
      html += '<div class="detail-section"><div class="muted tiny">为什么现在不接单</div>' +
        windows.map(function (text) { return '<div class="badge warn">' + esc(text) + "</div>"; }).join(" ") +
        "</div>";
    }

    var models = data.model_cooldowns || {};
    var modelKeys = Object.keys(models);
    if (modelKeys.length) {
      html += '<div class="detail-section"><div class="muted tiny">被单独封的模型</div>' +
        modelKeys.map(function (name) {
          return detailRow(name, esc(fmtCountdown(models[name]["for"] || 0)));
        }).join("") + "</div>";
    }

    html += '<div class="detail-section"><div class="muted tiny">这个号花掉的</div>';
    html += detailRow("请求数", esc(String(usage.requests || 0)) + "（失败 " + esc(String(usage.errors || 0)) + "）");
    html += detailRow("消耗积分", esc(fmtCredit(usage.credits || 0)));
    html += detailRow("tokens", "入 " + esc(fmtNum(usage.prompt_tokens || 0)) + " · 出 " + esc(fmtNum(usage.completion_tokens || 0)));
    html += detailRow("平均延迟", usage.requests ? esc(Math.round((usage.latency_ms || 0) / usage.requests)) + " ms" : "—");
    html += "</div>";

    var recent = data.recent || [];
    if (recent.length) {
      html += '<div class="detail-section"><div class="muted tiny">最近请求</div>' +
        recent.map(function (item) {
          var ok = Number(item.status || 0) >= 200 && Number(item.status || 0) < 400;
          return '<div class="detail-row"><span class="muted tiny">' + esc(fmtTime(item.at)) + "</span><span>" +
            '<span class="badge ' + (ok ? "ok" : "bad") + '">' + esc(String(item.status || "—")) + "</span> " +
            esc(item.model || "") + ' <span class="muted tiny">' + esc(String(item.elapsed_ms || 0)) + " ms · " +
            esc(fmtCredit(item.credit || 0)) + "</span></span></div>";
        }).join("") + "</div>";
    }

    $("#detail-body").innerHTML = html;
  }

  function rowAction(uid, action, button) {
    var labels = {
      enable: "启用账号", disable: "停用账号", delete: "删除账号",
      refresh: "续期令牌", checkin: "签到", balance: "查余额", test: "测试账号",
      report: "活跃上报", pause: "暂停账号", resume: "恢复账号"
    };
    var text = labels[action] || action;
    var account = state.accounts.filter(function (a) { return a.uid === uid; })[0];
    var name = account ? (account.nickname || uid.slice(0, 8)) : uid.slice(0, 8);

    // 删除是不可恢复的：把后果写进确认文案，而不是"确定吗"
    if (action === "delete" && !confirm("删除账号 " + name + "？\n\n这会把它从池里移除，本地保存的令牌一并删除，且不可恢复。")) return;

    var old = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "…"; }

    api("/api/accounts/" + encodeURIComponent(uid) + "/" + action, { method: "POST", body: {} })
      .then(function (data) {
        var detail = (data && (data.message || data.detail)) || "";
        var ok = !data || data.ok !== false;
        toast(ok ? "ok" : "warn", text + (ok ? "完成" : "未成功"), detail || name);
        if (action === "delete" && account) delete state.selected[uid];
        if (action === "test" && data) {
          var lines = [];
          if (data.model) lines.push("模型 " + data.model);
          if (data.sample) lines.push("返回 " + String(data.sample).slice(0, 60));
          if (lines.length) toast("ok", name + " 通了", lines.join(" · "));
        }
        return loadAccounts(true);
      })
      .catch(function (err) {
        toast("bad", text + "失败", err.message);
      })
      .then(function () {
        if (button) { button.disabled = false; button.textContent = old; }
      });
  }

  function bulkAction(action) {
    var uids = selectedUids();
    if (!uids.length) return;
    var body = { action: action, uids: uids };
    var label = { test: "测试", refresh: "续期", balance: "查额度", enable: "启用",
                  disable: "停用", checkin: "签到", report: "上报",
                  pause: "暂停", resume: "恢复" }[action] || action;
    toast("", "正在" + label + " " + uids.length + " 个账号…", "逐个发，不会同时打上游");
    api("/api/accounts/bulk", { body: body }).then(function (data) {
      var okCount = (data && data.ok_count) || 0;
      var total = (data && data.total) || uids.length;
      var failed = (data && data.results ? data.results.filter(function (r) { return !r.ok; }) : []);
      var tone = failed.length ? "warn" : "ok";
      toast(tone, label + "完成 " + okCount + "/" + total,
        failed.slice(0, 3).map(function (r) { return (r.detail || r.uid.slice(0, 8)); }).join("；"));
      if (action === "enable" || action === "disable" || action === "pause" || action === "resume") {
        state.selected = Object.create(null);
      }
      return loadAccounts(true);
    }).catch(function (err) {
      toast("bad", label + "失败", err.message);
    });
  }

  function saveAccountField(uid, field, value) {
    var body = { uid: uid };
    body[field] = value;
    return api("/api/accounts/set", { body: body }).then(function () {
      return loadAccounts(true);
    }).catch(function (err) {
      toast("bad", "保存失败", err.message);
    });
  }

  // ---------------- 添加账号 ----------------

  function setAddMode(mode) {
    $$("#add-tabs button").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-mode") === mode);
    });
    $("#add-link").hidden = mode !== "link";
    $("#add-refresh").hidden = mode !== "refresh";
    $("#add-access").hidden = mode !== "access";
  }

  function stopLoginPoll() {
    if (state.login.timer) { clearInterval(state.login.timer); state.login.timer = 0; }
  }

  function startLoginFlow() {
    var realm = $("#add-realm").value;
    stopLoginPoll();
    $("#add-link-state").textContent = "正在申请登录链接…";
    api("/api/accounts/login/start", { body: { realm: realm } }).then(function (data) {
      state.login.state = data.state || "";
      state.login.url = data.url || "";
      $("#add-link-url").value = state.login.url;
      $("#add-link-result").hidden = false;
      $("#add-link-state").textContent = "等待浏览器授权（每 2s 查一次）";
      try { window.open(state.login.url, "_blank", "noopener"); } catch (e) { /* 弹窗被拦就靠复制 */ }
      state.login.timer = setInterval(pollLogin, 2000);
    }).catch(function (err) {
      $("#add-link-state").textContent = "";
      toast("bad", "生成登录链接失败", err.message);
    });
  }

  function pollLogin() {
    if (!state.login.state) return;
    api("/api/accounts/login/poll?state=" + encodeURIComponent(state.login.state))
      .then(function (data) {
        var status = (data && data.status) || "pending";
        var message = (data && data.message) || "";
        if (status === "ok") {
          stopLoginPoll();
          $("#add-link-state").textContent = "";
          $("#add-link-result").hidden = true;
          state.login.state = "";
          toast("ok", "账号已添加", message || "");
          loadAccounts();
        } else if (status === "expired" || status === "error") {
          stopLoginPoll();
          $("#add-link-state").textContent = "";
          toast("bad", status === "expired" ? "链接已过期" : "登录失败", message);
        } else {
          $("#add-link-state").textContent = message || "等待授权…";
        }
      })
      .catch(function (err) {
        stopLoginPoll();
        toast("bad", "轮询失败", err.message);
      });
  }

  function cancelLoginFlow() {
    stopLoginPoll();
    state.login.state = "";
    $("#add-link-result").hidden = true;
    $("#add-link-state").textContent = "已取消";
  }

  function addAccountByToken(kind) {
    var realm = $("#add-" + kind + "-realm").value;
    var input = $("#add-" + kind + "-token");
    var token = (input.value || "").trim();
    if (!token) { toast("warn", "请先填令牌", ""); return; }
    var body = { realm: realm };
    body[kind === "refresh" ? "refresh_token" : "access_token"] = token;
    api("/api/accounts/add", { body: body }).then(function (data) {
      input.value = "";
      toast("ok", "账号已添加", (data && data.account && data.account.nickname) || "");
      loadAccounts();
    }).catch(function (err) {
      toast("bad", "添加失败", err.message);
    });
  }

  // ---------------- 用量 ----------------

  function loadUsage() {
    return api("/api/usage").then(function (data) {
      renderUsage(data);
      markUpdated();
    }).catch(function (err) {
      toast("bad", "读取用量失败", err.message);
    });
  }

  function rowsToTable(tbody, rows, cols, emptyText) {
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="' + cols + '" class="empty">' + esc(emptyText || "暂无数据") + "</td></tr>";
      return;
    }
    tbody.innerHTML = rows.join("");
  }

  function renderUsage(usage) {
    if (!usage) return;
    var host = $("#usage-kpis");
    if (host) {
      var t = usage.total || {};
      host.innerHTML = [
        kpi("请求", fmtNum(t.requests), "成功 " + fmtNum(t.ok)),
        kpi("失败", fmtNum(t.errors), t.requests ? fmtPct(t.errors / t.requests) : ""),
        kpi("输入 token", fmtNum(t.prompt_tokens), t.prompt_tokens ? "缓存 " + fmtPct(t.cache_hit_rate || 0) : ""),
        kpi("输出 token", fmtNum(t.completion_tokens), ""),
        kpi("积分", fmtCredit(t.credits), "")
      ].join("");
    }
    $("#usage-retention").textContent = usage.retention_days ? "台账保留 " + usage.retention_days + " 天" : "";

    var models = Object.keys(usage.by_model || {}).map(function (name) {
      var m = usage.by_model[name] || {};
      return { name: name, m: m };
    }).sort(function (a, b) { return (b.m.requests || 0) - (a.m.requests || 0); });

    rowsToTable($("#usage-model-table tbody"), models.map(function (row) {
      var m = row.m;
      return "<tr><td>" + esc(row.name) + "</td>" +
        '<td class="num">' + esc(fmtNum(m.requests)) + "</td>" +
        '<td class="num">' + esc(fmtNum(m.prompt_tokens)) + "</td>" +
        '<td class="num">' + esc(fmtNum(m.completion_tokens)) + "</td>" +
        '<td class="num">' + esc(m.cache_hit_rate ? fmtPct(m.cache_hit_rate) : "—") + "</td>" +
        '<td class="num">' + esc(fmtCredit(m.credits)) + "</td></tr>";
    }), 6, "还没有请求");

    rowsToTable($("#usage-account-table tbody"),
      Object.keys(usage.by_account || {}).map(function (uid) {
        var a = usage.by_account[uid] || {};
        return "<tr><td>" + esc((a.nickname || uid).slice(0, 20)) + "</td>" +
          '<td class="num">' + esc(fmtNum(a.requests)) + "</td>" +
          '<td class="num">' + esc(fmtCredit(a.credits)) + "</td></tr>";
      }), 3, "没有按账号的数据");

    rowsToTable($("#usage-key-table tbody"),
      Object.keys(usage.by_key || {}).map(function (name) {
        var k = usage.by_key[name] || {};
        return "<tr><td>" + esc(name) + "</td>" +
          '<td class="num">' + esc(fmtNum(k.requests)) + "</td>" +
          '<td class="num">' + esc(fmtCredit(k.credits)) + "</td></tr>";
      }), 3, "没有按 key 的数据");

    rowsToTable($("#usage-day-table tbody"),
      Object.keys(usage.by_day || {}).sort().reverse().map(function (day) {
        var d = usage.by_day[day] || {};
        return "<tr><td>" + esc(day) + "</td>" +
          '<td class="num">' + esc(fmtNum(d.requests)) + "</td>" +
          '<td class="num">' + esc(fmtNum(d.errors)) + "</td>" +
          '<td class="num">' + esc(fmtNum(d.prompt_tokens)) + "</td>" +
          '<td class="num">' + esc(fmtNum(d.completion_tokens)) + "</td>" +
          '<td class="num">' + esc(fmtCredit(d.credits)) + "</td></tr>";
      }), 6, "还没有数据");
  }

  // ---------------- 日志 ----------------

  function loadLogs() {
    var lines = $("#log-lines") ? $("#log-lines").value : "500";
    return api("/api/logs?lines=" + encodeURIComponent(lines)).then(function (data) {
      var text = (data && (data.text || (data.lines || []).join("\n"))) || "";
      var filter = ($("#log-search").value || "").trim();
      if (filter) {
        var re = null;
        try { re = new RegExp(filter, "i"); } catch (e) { re = null; }
        text = text.split("\n").filter(function (line) {
          return re ? re.test(line) : line.toLowerCase().indexOf(filter.toLowerCase()) >= 0;
        }).join("\n");
      }
      var body = $("#log-body");
      if (body) {
        body.textContent = text || "（空）";
        body.scrollTop = body.scrollHeight;
      }
      markUpdated();
    }).catch(function (err) {
      var body = $("#log-body");
      if (body) body.textContent = "读取日志失败：" + err.message;
    });
  }

  // ---------------- 设置 ----------------

  function loadSettings() {
    return api("/api/settings").then(function (data) {
      var panel = data.panel || {};
      $("#set-listen").value = data.listen || "";
      $("#set-api-key").value = "";
      $("#set-api-key").placeholder = data.default_api_key_set ? "已设置（留空 = 不修改）" : "留空 = 不设默认 key";
      $("#set-prompt-mode").value = data.prompt_mode || "passthrough";
      if ($("#set-prompt-append")) $("#set-prompt-append").value = data.prompt_append || "";
      if (panel.session_hours) $("#keys-note").textContent = "会话 " + panel.session_hours + " 小时";
      renderKeys(data.api_keys || []);
      renderTunables(data.tunables || []);
      renderGuide(data.api_keys || []);
      markUpdated();
    }).catch(function (err) {
      toast("bad", "读取设置失败", err.message);
    });
  }

  function renderKeys(keys) {
    rowsToTable($("#keys-table tbody"), keys.map(function (k) {
      return "<tr><td>" + esc(k.name || "—") + "</td>" +
        '<td class="mono tiny">' + esc(k.key || "") + "</td>" +
        "<td>" + esc(k.realm || "跟随") + "</td>" +
        '<td class="tiny">' + esc((k.models || []).join(",") || "全部") + "</td>" +
        '<td class="tiny">' + esc(k.max_concurrency ? String(k.max_concurrency) : "不限") + "</td>" +
        '<td class="tiny">' + esc(k.rate_limit ? String(k.rate_limit) + "/分" : "不限") + "</td>" +
        '<td><button class="ghost tiny-btn danger" data-key-del="' + esc(k.name || k.key) + '">删除</button></td></tr>';
    }), 7, "还没有 key（客户端不带 key 时走开放模式）");
  }

  // 接入指南：把"填哪里"写清楚。密钥在列表里永远是打码的，所以这里给占位说明
  // 而不是假装能读回明文——能读回明文才是真问题。
  function renderGuide(keys) {
    var host = $("#guide-body");
    if (!host) return;
    var base = location.origin;
    var first = (keys && keys[0] && keys[0].name) ? keys[0].name : "default";
    host.textContent = [
      "# OpenAI SDK（Python）",
      "from openai import OpenAI",
      'client = OpenAI(base_url="' + base + '/v1", api_key="sk-…")  # 密钥：客户端密钥里新建时显示一次（' + first + '）',
      'print(client.chat.completions.create(model="deepseek-v4-flash",',
      '      messages=[{"role":"user","content":"你好"}]).choices[0].message.content)',
      "",
      "# 任何 OpenAI 兼容客户端",
      "Base URL: " + base + "/v1",
      "API Key : sk-…",
      "",
      "# Claude Code / Codex（Anthropic 协议，走 /v1/messages）",
      "ANTHROPIC_BASE_URL=" + base,
      "ANTHROPIC_API_KEY=sk-…",
      "",
      "# 自测",
      'curl -s ' + base + '/v1/models -H "Authorization: Bearer sk-…"',
      'curl -s ' + base + '/v1/messages -H "x-api-key: sk-…" -H "content-type: application/json" \\',
      '  -d \'{"model":"deepseek-v4-flash","max_tokens":64,',
      '       "messages":[{"role":"user","content":"你好"}]}\''
    ].join("\n");
  }

  function renderTunables(items) {
    state.tunables = items;
    var host = $("#tunables");
    if (!host) return;
    host.innerHTML = items.map(function (item) {
      var value = item.value === null || item.value === undefined ? "" : String(item.value);
      var hint = item.restart ? "（需重启）" : "";
      return '<label class="field">' + esc(item.label || item.path) + hint +
        '<input type="text" data-tunable="' + esc(item.path) + '" value="' + esc(value) + '"></label>';
    }).join("");
  }

  // ---- 限定模型的多选下拉 -------------------------------------------------
  // 可选项来自上游目录（/api/models），而不是让用户背模型名：拼错一个字母的代价
  // 是"这把 key 什么模型都用不了"，而且看起来像网关坏了。
  var modelPick = { all: [], selected: [], loaded: false };

  function addModelChoice(id) {
    var value = (id || "").trim();
    if (!value) return;
    if (modelPick.selected.indexOf(value) === -1) modelPick.selected.push(value);
    renderModelChips();
    renderModelList($("#key-models-search").value);
  }

  function removeModelChoice(id) {
    modelPick.selected = modelPick.selected.filter(function (item) { return item !== id; });
    renderModelChips();
    renderModelList($("#key-models-search").value);
  }

  function renderModelChips() {
    var host = $("#key-models-chips");
    if (!host) return;
    host.innerHTML = modelPick.selected.map(function (id) {
      return '<span class="ms-chip">' + esc(id) +
        '<button type="button" data-drop="' + esc(id) + '" title="移除">\u00d7</button></span>';
    }).join("");
    var count = $("#key-models-count");
    if (count) {
      count.textContent = modelPick.selected.length
        ? "\u5df2\u9009 " + modelPick.selected.length + " / \u5171 " + modelPick.all.length
        : "\u672a\u9009\uff08= \u5168\u90e8\u6a21\u578b\uff09";
    }
  }

  function renderModelList(filter) {
    var host = $("#key-models-list");
    if (!host) return;
    var needle = (filter || "").trim().toLowerCase();
    var rows = modelPick.all.filter(function (item) {
      if (!needle) return true;
      return (item.id || "").toLowerCase().indexOf(needle) !== -1 ||
        (item.name || "").toLowerCase().indexOf(needle) !== -1;
    });
    if (!rows.length) {
      host.innerHTML = '<div class="ms-empty">' +
        (modelPick.all.length ? "\u6ca1\u6709\u5339\u914d\u7684\u6a21\u578b"
          : "\u8fd8\u6ca1\u6709\u6a21\u578b\u76ee\u5f55\u2014\u2014\u5148\u53bb\u300c\u8d26\u53f7\u300d\u9875\u52a0\u4e00\u4e2a\u53f7") +
        "</div>";
      return;
    }
    host.innerHTML = rows.map(function (item) {
      var checked = modelPick.selected.indexOf(item.id) !== -1;
      return '<label class="ms-item">' +
        '<input type="checkbox" data-pick="' + esc(item.id) + '"' + (checked ? " checked" : "") + ">" +
        '<span class="ms-id">' + esc(item.id) + "</span>" +
        '<span class="muted tiny">' + esc(item.name || "") + "</span>" +
        '<span class="ms-realm">' + esc(item.realm) + "</span></label>";
    }).join("");
  }

  function openModelMenu(open) {
    var menu = $("#key-models-menu");
    if (!menu) return;
    menu.hidden = !open;
    if (open) {
      loadModelChoices(false);
      renderModelList($("#key-models-search").value);
    }
  }

  function loadModelChoices(force) {
    if (modelPick.loaded && !force) return;
    api("/api/models").then(function (data) {
      modelPick.all = (data && data.models) || [];
      modelPick.loaded = true;
      renderModelChips();
      renderModelList($("#key-models-search").value);
    }).catch(function (err) {
      modelPick.loaded = true;
      toast("warn", "\u6a21\u578b\u76ee\u5f55\u6ca1\u8bfb\u51fa\u6765", err.message);
    });
  }

  function bindModelPicker() {
    var box = $("#key-models");
    if (!box) return;
    var search = $("#key-models-search");

    $("#key-models-control").addEventListener("click", function (ev) {
      if (ev.target.closest("[data-drop]")) return;
      openModelMenu(true);
      search.focus();
    });

    $("#key-models-chips").addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-drop]");
      if (!btn) return;
      ev.stopPropagation();
      removeModelChoice(btn.getAttribute("data-drop"));
    });

    $("#key-models-list").addEventListener("change", function (ev) {
      var input = ev.target.closest("[data-pick]");
      if (!input) return;
      if (input.checked) addModelChoice(input.getAttribute("data-pick"));
      else removeModelChoice(input.getAttribute("data-pick"));
    });

    search.addEventListener("input", function () { renderModelList(search.value); });

    search.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { openModelMenu(false); return; }
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      var value = search.value.trim();
      if (!value) return;
      // \u7cbe\u786e\u547d\u4e2d\u5c31\u5207\u6362\uff0c\u5426\u5219\u5f53\u6210\u901a\u914d\uff08glm-*\uff09\u76f4\u63a5\u52a0\u4e00\u4e2a chip
      var exact = modelPick.all.filter(function (item) {
        return item.id.toLowerCase() === value.toLowerCase();
      })[0];
      if (exact) {
        if (modelPick.selected.indexOf(exact.id) === -1) addModelChoice(exact.id);
        else removeModelChoice(exact.id);
      } else {
        addModelChoice(value);
      }
      search.value = "";
      renderModelList("");
    });

    $("#key-models-all").addEventListener("click", function () {
      modelPick.selected = modelPick.all.map(function (item) { return item.id; });
      renderModelChips();
      renderModelList(search.value);
    });

    $("#key-models-none").addEventListener("click", function () {
      modelPick.selected = [];
      renderModelChips();
      renderModelList(search.value);
    });

    document.addEventListener("click", function (ev) {
      if (!box.contains(ev.target)) openModelMenu(false);
    });

    renderModelChips();
  }

  // ---- 设置页左侧目录 -----------------------------------------------------
  function bindSettingsNav() {
    var nav = $("#settings-nav");
    if (!nav) return;
    var buttons = Array.prototype.slice.call(nav.querySelectorAll("button[data-target]"));

    function setActive(target) {
      buttons.forEach(function (btn) {
        btn.classList.toggle("active", btn.getAttribute("data-target") === target);
      });
    }

    nav.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-target]");
      if (!btn) return;
      var target = btn.getAttribute("data-target");
      var section = document.getElementById(target);
      if (!section) return;
      setActive(target);
      section.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    if (typeof IntersectionObserver === "function") {
      var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) setActive(entry.target.id);
        });
      }, { root: null, rootMargin: "-8% 0px -70% 0px", threshold: 0 });
      $$("#page-settings .set-block").forEach(function (block) { observer.observe(block); });
    }

    setActive(buttons.length ? buttons[0].getAttribute("data-target") : "");
  }

  function saveSettings() {
    var body = {};
    var listen = $("#set-listen").value.trim();
    if (listen) body.listen = listen;
    var key = $("#set-api-key").value.trim();
    if (key) body.api_key = key;
    var password = $("#set-panel-password").value;
    if (password) body.panel_password = password;
    body.prompt_mode = $("#set-prompt-mode").value;
    if ($("#set-prompt-append")) body.prompt_append = $("#set-prompt-append").value;
    api("/api/settings", { body: body }).then(function () {
      $("#set-panel-password").value = "";
      $("#set-api-key").value = "";
      toast("ok", "设置已保存", body.listen ? "监听地址改动需要重启进程" : "");
      loadSettings();
    }).catch(function (err) {
      toast("bad", "保存失败", err.message);
    });
  }

  function saveTunables() {
    var raw = {};
    $$("#tunables input[data-tunable]").forEach(function (input) {
      raw[input.getAttribute("data-tunable")] = input.value.trim();
    });
    api("/api/settings", { body: { tunables: raw } }).then(function () {
      toast("ok", "调优参数已保存", "策略/阈值立即生效");
      loadSettings();
    }).catch(function (err) {
      toast("bad", "保存失败", err.message);
    });
  }

  function addKey() {
    var name = $("#key-name").value.trim() || ("key-" + Date.now().toString(36).slice(-5));
    var realm = $("#key-realm").value;
    var models = modelPick.selected.slice();
    var maxConc = parseInt(($("#key-maxconc") && $("#key-maxconc").value) || "0", 10);
    if (!isFinite(maxConc) || maxConc < 0) maxConc = 0;
    var rateLimit = parseInt(($("#key-rate") && $("#key-rate").value) || "0", 10);
    if (!isFinite(rateLimit) || rateLimit < 0) rateLimit = 0;
    api("/api/settings", { body: { keys_add: [{ name: name, realm: realm, models: models, max_concurrency: maxConc, rate_limit: rateLimit }] } })
      .then(function (data) {
        var created = (data && data.added && data.added[0]) || null;
        var box = $("#key-created");
        if (created && created.key) {
          box.hidden = false;
          box.textContent = "新 key（只显示这一次）：" + created.key;
          try { navigator.clipboard.writeText(created.key); } catch (e) { /* 无剪贴板权限就手动复制 */ }
          toast("ok", "已创建 " + name, "明文已复制到剪贴板");
        } else {
          toast("ok", "已创建 " + name, "");
        }
        $("#key-name").value = "";
        modelPick.selected = [];
        renderModelChips();
        renderModelList("");
        loadSettings();
      })
      .catch(function (err) { toast("bad", "创建失败", err.message); });
  }

  function deleteKey(name) {
    if (!confirm("删除 key " + name + "？用它的客户端会立刻 401。")) return;
    api("/api/settings", { body: { keys_delete: [name] } }).then(function (data) {
      var deleted = (data && data.deleted) || [];
      toast(deleted.length ? "ok" : "warn", deleted.length ? "已删除 " + name : "没找到 " + name, "");
      loadSettings();
    }).catch(function (err) { toast("bad", "删除失败", err.message); });
  }

  // ---------------- 事件绑定 ----------------

  function bind() {
    $("#login-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var button = $("#login-submit");
      var error = $("#login-error");
      error.hidden = true;
      button.disabled = true;
      button.textContent = "登录中…";
      doLogin($("#login-password").value).then(function () {
        button.disabled = false;
        button.textContent = "登录";
        showApp();
      }).catch(function (err) {
        button.disabled = false;
        button.textContent = "登录";
        error.hidden = false;
        error.textContent = err.message;
      });
    });

    $("#logout-btn").addEventListener("click", function () {
      api("/panel/logout", { method: "POST", body: {} }).catch(function () { });
      showLogin();
    });

    $("#refresh-btn").addEventListener("click", function () { refreshPage(); });

    window.addEventListener("hashchange", render);

    // 概览
    $("#recent-copy-btn").addEventListener("click", function () {
      var text = Array.prototype.map.call($$("#recent-table tbody tr"), function (tr) {
        return Array.prototype.map.call(tr.children, function (td) { return td.textContent.trim(); }).join("\t");
      }).join("\n");
      try { navigator.clipboard.writeText(text); toast("ok", "已复制", ""); }
      catch (e) { toast("warn", "复制失败", "浏览器拒绝了剪贴板"); }
    });

    $("#sched-run-btn").addEventListener("click", function () {
      var button = this;
      button.disabled = true;
      api("/api/scheduler/trigger", { body: {} }).then(function (data) {
        toast("ok", "已执行一轮", (data && data.running === false) ? "调度器没在跑，只跑了一次" : "");
        renderScheduler(data);
      }).catch(function (err) {
        toast("bad", "执行失败", err.message);
      }).then(function () { button.disabled = false; });
    });

    // 账号页
    $("#acc-add-toggle").addEventListener("click", function () {
      var panel = $("#acc-add-panel");
      panel.hidden = !panel.hidden;
    });
    $("#add-tabs").addEventListener("click", function (ev) {
      var button = ev.target.closest("button[data-mode]");
      if (button) setAddMode(button.getAttribute("data-mode"));
    });
    $("#add-link-start").addEventListener("click", startLoginFlow);
    $("#add-link-cancel").addEventListener("click", cancelLoginFlow);
    $("#add-link-open").addEventListener("click", function () {
      if (state.login.url) window.open(state.login.url, "_blank", "noopener");
    });
    $("#add-link-copy").addEventListener("click", function () {
      try { navigator.clipboard.writeText(state.login.url); toast("ok", "链接已复制", ""); }
      catch (e) { toast("warn", "复制失败", "手动选中输入框内容复制"); }
    });
    $("#add-refresh-submit").addEventListener("click", function () { addAccountByToken("refresh"); });
    $("#add-access-submit").addEventListener("click", function () { addAccountByToken("access"); });

    $("#acc-search").addEventListener("input", function () {
      state.filter.text = this.value;
      state.pageNo = 1;
      renderAccounts();
    });

    $("#acc-status-chips").addEventListener("click", function (ev) {
      var button = ev.target.closest("button[data-status]");
      if (!button) return;
      state.filter.status = button.getAttribute("data-status");
      $$("#acc-status-chips button").forEach(function (b) { b.classList.toggle("active", b === button); });
      state.pageNo = 1;
      renderAccounts();
    });

    $("#acc-realm-filter").addEventListener("change", function () {
      state.filter.realm = this.value;
      state.pageNo = 1;
      renderAccounts();
    });

    $("#acc-sort").addEventListener("change", function () {
      state.sort = this.value;
      renderAccounts();
    });

    // 备份：导出走 fetch 拿 blob（令牌走请求头，不进 URL/下载记录）
    $("#acc-export").addEventListener("click", function () {
      fetch("/api/accounts/export", { headers: { "X-Panel-Token": state.token || "" } })
        .then(function (res) {
          if (!res.ok) throw new Error("导出失败（HTTP " + res.status + "）");
          return res.blob();
        })
        .then(function (blob) {
          var url = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = url;
          a.download = "wb2api-accounts.json";
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
          toast("ok", "已导出", "文件里有 refresh token，等于账号密码，注意保管");
        })
        .catch(function (err) { toast("bad", "导出失败", err.message || ""); });
    });

    $("#acc-import").addEventListener("click", function () {
      $("#acc-import-file").click();
    });

    $("#acc-import-file").addEventListener("change", function () {
      var file = this.files && this.files[0];
      this.value = "";
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        var accounts;
        try {
          var parsed = JSON.parse(String(reader.result || ""));
          accounts = Array.isArray(parsed) ? parsed : parsed && parsed.accounts;
        } catch (err) {
          toast("bad", "文件不是合法 JSON", err.message || "");
          return;
        }
        if (!Array.isArray(accounts)) {
          toast("bad", "文件里没有 accounts 数组", "");
          return;
        }
        api("/api/accounts/import", { body: { accounts: accounts } })
          .then(function (res) {
            toast("ok", "已导入", "新增 " + res.added + "，更新 " + res.updated + "，跳过 " + res.skipped);
            return loadAccounts();
          })
          .catch(function (err) { toast("bad", "导入失败", err.message || ""); });
      };
      reader.onerror = function () { toast("bad", "读文件失败", ""); };
      reader.readAsText(file);
    });

    $("#acc-check-all").addEventListener("change", function () {
      var checked = this.checked;
      var start = (state.pageNo - 1) * state.pageSize;
      filteredAccounts().slice(start, start + state.pageSize).forEach(function (a) {
        if (checked) state.selected[a.uid] = true;
        else delete state.selected[a.uid];
      });
      renderAccounts();
    });

    $("#detail-close").addEventListener("click", closeAccountDetail);
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") closeAccountDetail();
    });

    $("#acc-table").addEventListener("click", function (ev) {
      var tr = ev.target.closest("tr[data-uid]");
      if (!tr) return;
      var uid = tr.getAttribute("data-uid");
      if (ev.target.classList.contains("acc-check")) {
        if (ev.target.checked) state.selected[uid] = true;
        else delete state.selected[uid];
        renderAccounts();
        return;
      }
      if (ev.target.classList.contains("acc-detail")) { openAccountDetail(uid); return; }
      if (ev.target.classList.contains("acc-test")) { rowAction(uid, "test", ev.target); return; }
      var act = ev.target.getAttribute("data-act");
      if (act) rowAction(uid, act, ev.target);
    });

    // 行内编辑：失焦即保存，不用再点"保存"
    $("#acc-table").addEventListener("change", function (ev) {
      var tr = ev.target.closest("tr[data-uid]");
      if (!tr) return;
      var uid = tr.getAttribute("data-uid");
      if (ev.target.classList.contains("acc-note")) saveAccountField(uid, "note", ev.target.value);
      else if (ev.target.classList.contains("acc-group")) saveAccountField(uid, "group", ev.target.value);
      else if (ev.target.classList.contains("acc-weight")) {
        var weight = parseInt(ev.target.value, 10);
        if (!weight || weight < 1) { ev.target.value = "1"; weight = 1; }
        saveAccountField(uid, "weight", weight);
      }
    });

    $("#acc-pager").addEventListener("click", function (ev) {
      var button = ev.target.closest("button[data-page-go]");
      if (!button || button.disabled) return;
      state.pageNo = parseInt(button.getAttribute("data-page-go"), 10) || 1;
      renderAccounts();
    });

    $("#bulkbar").addEventListener("click", function (ev) {
      var button = ev.target.closest("button[data-bulk]");
      if (!button) return;
      var action = button.getAttribute("data-bulk");
      if (action === "clear") {
        state.selected = Object.create(null);
        renderAccounts();
        return;
      }
      bulkAction(action);
    });

    // 日志
    $("#log-lines").addEventListener("change", loadLogs);
    $("#log-refresh-btn").addEventListener("click", loadLogs);
    $("#log-search").addEventListener("input", function () {
      clearTimeout(state.timers.logSearch);
      state.timers.logSearch = setTimeout(loadLogs, 250);
    });
    $("#log-follow").addEventListener("change", function () {
      state.logFollow = this.checked;
      if (this.checked) every("logs", 3000, loadLogs);
      else stopTimer("logs");
    });
    $("#log-copy-btn").addEventListener("click", function () {
      var text = $("#log-body").textContent || "";
      try { navigator.clipboard.writeText(text); toast("ok", "日志已复制", ""); }
      catch (e) { toast("warn", "复制失败", "浏览器拒绝了剪贴板"); }
    });

    // 设置
    $$(".set-save").forEach(function (btn) { btn.addEventListener("click", saveSettings); });
    $("#tunables-save").addEventListener("click", saveTunables);
    if ($("#copy-guide")) {
      $("#copy-guide").addEventListener("click", function () {
        var text = ($("#guide-body") || {}).textContent || "";
        if (!text) { toast("warn", "没有可复制的内容", ""); return; }
        try { navigator.clipboard.writeText(text); toast("ok", "接入指南已复制", ""); }
        catch (e) { toast("warn", "复制失败", "浏览器拒绝了剪贴板"); }
      });
    }
    $("#key-add").addEventListener("click", addKey);
    $("#keys-table").addEventListener("click", function (ev) {
      var button = ev.target.closest("button[data-key-del]");
      if (button) deleteKey(button.getAttribute("data-key-del"));
    });
  }

  // ---------------- 入口 ----------------


  // ---------------- 请求明细 / 失败报文 / 出口代理 ----------------

  var reqFilter = { status: "", model: "", account: "", q: "", follow: false };

  function reqQuery() {
    var parts = [];
    if (reqFilter.status) parts.push("status=" + encodeURIComponent(reqFilter.status));
    if (reqFilter.model) parts.push("model=" + encodeURIComponent(reqFilter.model));
    if (reqFilter.account) parts.push("account=" + encodeURIComponent(reqFilter.account));
    if (reqFilter.q) parts.push("q=" + encodeURIComponent(reqFilter.q));
    return parts.length ? "?" + parts.join("&") : "";
  }

  function statusTone(status, outcome) {
    var code = Number(status) || 0;
    if (outcome && outcome !== "completed") return "bad";
    if (code >= 500) return "bad";
    if (code >= 400) return "warn";
    return "ok";
  }

  function loadRequests() {
    api("/api/requests?limit=200" + (reqQuery() ? "&" + reqQuery().slice(1) : ""))
      .then(function (data) {
        renderRequests(data.items || [], data.stats || {});
      })
      .catch(function (err) { toast("读请求明细失败：" + err.message, "bad"); });
  }

  function renderRequests(items, stats) {
    var body = $("#req-table tbody");
    if (!body) return;
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="9" class="muted">还没有请求记录（或者刚重启、明细已过期）。</td></tr>';
    } else {
      body.innerHTML = items.map(function (row) {
        var tone = statusTone(row.status, row.outcome);
        var tokens = (row.prompt_tokens || 0) + "/" + (row.completion_tokens || 0);
        return "<tr>" +
          "<td>" + esc(fmtTime(row.at)) + "</td>" +
          '<td><span class="badge ' + tone + '">' + esc(row.status || "-") + "</span>" +
          (row.outcome && row.outcome !== "completed" ? ' <span class="muted tiny">' + esc(row.outcome) + "</span>" : "") + "</td>" +
          "<td>" + esc(row.model || "") + "</td>" +
          "<td>" + esc(row.account || "") + "</td>" +
          "<td>" + (row.elapsed_ms ? row.elapsed_ms + "ms" : "-") + "</td>" +
          "<td>" + (row.ttft_ms ? row.ttft_ms + "ms" : "-") + "</td>" +
          '<td class="muted">' + esc(tokens) + "</td>" +
          "<td>" + esc(fmtCredit(row.credit)) + "</td>" +
          '<td class="muted tiny">' + esc(row.request_id || "") + "</td>" +
          "</tr>";
      }).join("");
    }
    var note = $("#req-note");
    if (note) {
      note.textContent = "缓冲 " + (stats.buffered || 0) + " 条 · 落盘 " + (stats.files || 0) +
        " 个文件 · 保留 " + (stats.retention_days || 0) + " 天" + (stats.enabled ? "" : "（未启用）");
    }
  }

  function loadDumps() {
    api("/api/dumps?limit=50")
      .then(function (data) { renderDumps(data.items || [], data.stats || {}); })
      .catch(function (err) { toast("读失败报文失败：" + err.message, "bad"); });
  }

  function renderDumps(items, stats) {
    var body = $("#dump-table tbody");
    if (!body) return;
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="6" class="muted">没有留存（只留被上游拒绝的请求）。</td></tr>';
    } else {
      body.innerHTML = items.map(function (row) {
        return "<tr>" +
          "<td>" + esc(fmtTime(row.at)) + "</td>" +
          '<td><span class="badge ' + statusTone(row.status) + '">' + esc(row.status || "-") + "</span></td>" +
          '<td><code class="tiny">' + esc(row.code || "-") + "</code></td>" +
          "<td>" + esc(row.model || "") + "</td>" +
          '<td class="muted">' + esc((row.message || "").slice(0, 90)) + "</td>" +
          '<td><button class="ghost tiny-btn dump-open" data-id="' + esc(row.request_id) + '">看原文</button></td>' +
          "</tr>";
      }).join("");
    }
    var note = $("#dump-note");
    if (note) {
      note.textContent = "今天已留 " + (stats.today || 0) + "/" + (stats.daily_max || 0) +
        " 条 · 保留 " + (stats.retention_days || 0) + " 天" + (stats.enabled ? "" : "（未启用）");
    }
  }

  function openDump(requestId) {
    api("/api/dumps/" + encodeURIComponent(requestId))
      .then(function (row) {
        var box = $("#dump-body");
        if (!box) return;
        var headers = row.request_headers || {};
        var lines = [];
        lines.push("request_id : " + (row.request_id || ""));
        lines.push("时间       : " + (row.iso || fmtTime(row.at)));
        lines.push("模型       : " + (row.model || "") + (row.alias ? "（别名 " + row.alias + "）" : ""));
        lines.push("账号       : " + (row.account || ""));
        lines.push("状态       : " + (row.status || "") + "  code=" + (row.code || ""));
        lines.push("上游说     : " + (row.message || ""));
        lines.push("URL        : " + (row.url || ""));
        lines.push("");
        lines.push("--- 我们发出去的头（已脱敏）---");
        Object.keys(headers).sort().forEach(function (name) {
          lines.push(name + ": " + headers[name]);
        });
        lines.push("");
        lines.push("--- 上游回的 body（" + (row.truncated ? "已截断" : "完整") + "）---");
        lines.push(row.body || "");
        box.hidden = false;
        box.textContent = lines.join("\n");
      })
      .catch(function (err) { toast("读原文失败：" + err.message, "bad"); });
  }

  function loadProxies() {
    api("/api/proxies")
      .then(function (data) { renderProxies(data); })
      .catch(function (err) { toast("读出口池失败：" + err.message, "bad"); });
  }

  function renderProxies(data) {
    var body = $("#proxy-table tbody");
    if (!body) return;
    var rows = data.entries || [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="8" class="muted">没有配置出口——所有请求直连。</td></tr>';
    } else {
      body.innerHTML = rows.map(function (row) {
        var cooling = Number(row.cooling_for) > 0;
        return "<tr>" +
          '<td class="tiny">' + esc(row.url) + "</td>" +
          "<td>" + esc(row.realm) + "</td>" +
          "<td>" + esc(row.weight) + "</td>" +
          '<td class="muted">' + esc(row.note || "") + "</td>" +
          "<td>" + (cooling ? '<span class="badge warn">' + fmtCountdown(row.cooling_for) + "</span>" : '<span class="badge ok">可用</span>') + "</td>" +
          '<td class="muted">' + esc(row.used || 0) + "</td>" +
          "<td>" + (row.enabled ? "启用" : '<span class="badge warn">停用</span>') + "</td>" +
          '<td><button class="ghost tiny-btn proxy-toggle" data-url="' + esc(row.url) + '" data-enabled="' + (row.enabled ? "1" : "0") + '">' +
          (row.enabled ? "停用" : "启用") + "</button></td>" +
          "</tr>";
      }).join("");
    }
    var note = $("#proxy-note");
    if (note) {
      note.textContent = "共 " + (data.total || 0) + " 个 · 可用 " + (data.ready || 0) + " · 冷却 " + (data.cooling || 0);
    }
  }

  function proxyMutate(payload, okText) {
    return api("/api/proxies", { method: "POST", body: payload })
      .then(function (data) {
        renderProxies(data);
        toast(okText, "ok");
      })
      .catch(function (err) { toast("操作失败：" + err.message, "bad"); });
  }

  function addProxy() {
    var url = ($("#proxy-url").value || "").trim();
    if (!url) { toast("warn", "先填出口地址，例如 socks5://user:pass@host:1080"); return; }
    proxyMutate({
      action: "add",
      url: url,
      realm: $("#proxy-realm").value,
      weight: Number($("#proxy-weight").value) || 1,
      note: ($("#proxy-note-input").value || "").trim()
    }, "已添加出口").then(function () {
      $("#proxy-url").value = "";
      $("#proxy-note-input").value = "";
    });
  }

  function bindObservability() {
    var status = $("#req-status");
    if (status) status.addEventListener("change", function () { reqFilter.status = status.value; loadRequests(); });
    ["req-model", "req-account", "req-q"].forEach(function (id) {
      var el = $("#" + id);
      if (!el) return;
      var key = id.split("-")[1];
      el.addEventListener("input", function () {
        reqFilter[key] = el.value.trim();
        clearTimeout(el._timer);
        el._timer = setTimeout(loadRequests, 250);
      });
    });
    var follow = $("#req-follow");
    if (follow) follow.addEventListener("change", function () {
      reqFilter.follow = follow.checked;
      stopTimer("requests");
      if (reqFilter.follow) every("requests", 5000, loadRequests);
    });
    var refresh = $("#req-refresh-btn");
    if (refresh) refresh.addEventListener("click", function () { loadRequests(); loadDumps(); });
    var dumpRefresh = $("#dump-refresh-btn");
    if (dumpRefresh) dumpRefresh.addEventListener("click", loadDumps);
    var dumpTable = $("#dump-table");
    if (dumpTable) dumpTable.addEventListener("click", function (ev) {
      var btn = ev.target.closest(".dump-open");
      if (btn) openDump(btn.getAttribute("data-id"));
    });
    var addBtn = $("#proxy-add");
    if (addBtn) addBtn.addEventListener("click", addProxy);
    var resetBtn = $("#proxy-reset");
    if (resetBtn) resetBtn.addEventListener("click", function () {
      proxyMutate({ action: "reset" }, "冷却已清空");
    });
    var proxyTable = $("#proxy-table");
    if (proxyTable) proxyTable.addEventListener("click", function (ev) {
      var btn = ev.target.closest(".proxy-toggle");
      if (!btn) return;
      proxyMutate({ action: "update", url: btn.getAttribute("data-url"), enabled: btn.getAttribute("data-enabled") !== "1" },
        "已更新出口");
    });
  }

  function main() {
    bind();
    bindObservability();
    bindModelPicker();
    bindSettingsNav();
    setAddMode("link");
    // 走到这里说明绑定都成功了；index.html 用它区分"脚本没跑"和"脚本跑了但没登录"。
    window.__WB2A_READY = true;
    if (!state.token) {
      showLogin();
      return;
    }
    showApp();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", main);
  else main();
})();
