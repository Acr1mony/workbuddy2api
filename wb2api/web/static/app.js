// workbuddy2api 面板。零构建：一个文件、无框架、直接跑。
// 约定：所有请求都过 api()，它负责带令牌、解析 JSON、把错误冒泡到页面提示。

const TOKEN_KEY = "wb2a_panel_token";

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  accounts: [],
  settings: null,
  pollTimer: null,
  refreshTimer: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function fmtNum(value) {
  const n = Number(value || 0);
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e4) return (n / 1e3).toFixed(1) + "k";
  return String(Math.round(n));
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds || 0)));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}

function fmtTime(epoch) {
  if (!epoch) return "-";
  const d = new Date(Number(epoch) * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (state.token) headers["X-Panel-Token"] = state.token;
  const response = await fetch(path, Object.assign({}, options, { headers }));
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
  if (!response.ok) {
    if (response.status === 401) showLogin();
    const message = (data && data.error && data.error.message) || ("HTTP " + response.status);
    throw new Error(message);
  }
  return data;
}

// ---------------------------------------------------------------- 启动

async function boot() {
  let status = {};
  try { status = await api("/panel/status"); } catch (_) { status = {}; }
  if (!status.enabled) {
    document.body.innerHTML = '<div class="login"><div class="card"><h1>面板未启用</h1><p class="muted">在配置里把 panel.enabled 设为 true 后重启网关。</p></div></div>';
    return;
  }
  $("#version").textContent = status.version || "";
  if (status.authenticated) {
    await enterApp(status);
  } else {
    showLogin();
  }
}

function showLogin() {
  state.token = "";
  localStorage.removeItem(TOKEN_KEY);
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  clearInterval(state.refreshTimer);
  $("#password").focus();
}

async function enterApp(status) {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  if (status && status.is_default_password) {
    console.warn("面板正在使用默认密码 admin，请尽快修改。");
  }
  switchTab("dashboard");
  await Promise.all([loadDashboard(), loadSettings()]);
  clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => {
    if ($("#tab-dashboard").classList.contains("active")) loadDashboard();
  }, 5000);
}

// ---------------------------------------------------------------- 概览

async function loadDashboard() {
  try {
    const [usage, pool, recent] = await Promise.all([
      api("/api/usage"),
      api("/api/pool"),
      api("/api/usage/recent?limit=30"),
    ]);
    renderKpis(usage.total || {});
    renderPool(pool);
    renderRecent(recent.items || []);
    renderByModel(usage.by_model || {});
  } catch (error) {
    console.warn("概览刷新失败", error);
  }
}

function renderKpis(total) {
  $("#kpi-requests").textContent = fmtNum(total.requests);
  $("#kpi-errors").textContent = fmtNum(total.errors);
  $("#kpi-tokens").textContent = fmtNum((total.prompt_tokens || 0) + (total.completion_tokens || 0));
  $("#kpi-latency").textContent = (total.avg_latency_ms || 0).toFixed(0) + " ms";
  $("#kpi-cache").textContent = Math.round((total.cache_hit_rate || 0) * 100) + "%";
}

function renderPool(pool) {
  const total = Math.max(1, pool.total || 0);
  const seg = (value, cls) => `<span class="${cls}" style="width:${(value / total) * 100}%"></span>`;
  const holds = [];
  if (pool.hold_cn_remaining) holds.push(`CN 风控暂停 ${fmtDuration(pool.hold_cn_remaining)}`);
  if (pool.hold_global_remaining) holds.push(`Global 风控暂停 ${fmtDuration(pool.hold_global_remaining)}`);
  $("#pool").innerHTML = `
    <div class="pool-row"><span class="muted">容量</span>
      <div class="bar">${seg(pool.ready, "ready")}${seg(pool.cooling, "cooling")}${seg(pool.disabled, "dead")}</div>
    </div>
    <div class="muted">可用 ${pool.ready || 0} · 冷却 ${pool.cooling || 0} · 停用 ${pool.disabled || 0} · 并发中 ${pool.in_flight || 0}</div>
    <div class="muted">额度合计 ${pool.credits || 0}${pool.credit_unknown ? `（${pool.credit_unknown} 个未知）` : ""}</div>
    <div class="muted">策略 CN=${escapeHtml((pool.strategy || {}).cn || "-")} · Global=${escapeHtml((pool.strategy || {}).global || "-")}</div>
    ${holds.length ? `<div class="badge warn">${escapeHtml(holds.join("；"))}</div>` : ""}
  `;
}

function renderRecent(items) {
  const body = $("#recent tbody");
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">还没有请求</td></tr>';
    return;
  }
  body.innerHTML = items.map((row) => {
    const status = Number(row.status || 0);
    const cls = status >= 400 ? "err" : row.outcome === "completed" ? "ok" : "warn";
    const tokens = (Number(row.prompt_tokens || 0) + Number(row.completion_tokens || 0));
    return `<tr>
      <td>${escapeHtml(fmtTime(row.at))}</td>
      <td>${escapeHtml(row.model || "-")}</td>
      <td>${escapeHtml(row.account || "-")}</td>
      <td><span class="badge ${cls}">${escapeHtml(row.outcome || status)}</span></td>
      <td>${fmtNum(row.elapsed_ms)} ms</td>
      <td>${fmtNum(tokens)}</td>
    </tr>`;
  }).join("");
}

function renderByModel(byModel) {
  const rows = Object.entries(byModel).sort((a, b) => (b[1].requests || 0) - (a[1].requests || 0));
  const body = $("#by-model tbody");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="muted">暂无数据</td></tr>';
    return;
  }
  body.innerHTML = rows.map(([model, bucket]) => `<tr>
    <td>${escapeHtml(model)}</td>
    <td>${fmtNum(bucket.requests)}</td>
    <td>${fmtNum(bucket.errors)}</td>
    <td>${fmtNum((bucket.prompt_tokens || 0) + (bucket.completion_tokens || 0))}</td>
  </tr>`).join("");
}

// ---------------------------------------------------------------- 账号

async function loadAccounts() {
  const realm = $("#realm-filter").value;
  const data = await api("/api/accounts" + (realm ? "?realm=" + encodeURIComponent(realm) : ""));
  state.accounts = data.accounts || [];
  renderAccounts();
}

function accountStatus(row) {
  if (row.disabled) return ['<span class="badge err">停用</span>', "disabled"];
  if (row.throttle_for > 0) {
    return [`<span class="badge warn">${escapeHtml(row.throttle_reason || "冷却")} ${fmtDuration(row.throttle_for)}</span>`, "cooling"];
  }
  if (row.token_expired) return ['<span class="badge warn">令牌过期</span>', "expired"];
  if (!row.has_access_token && !row.has_refresh_token) return ['<span class="badge err">无凭据</span>', "dead"];
  return ['<span class="badge ok">就绪</span>', "ready"];
}

function renderAccounts() {
  const body = $("#accounts tbody");
  if (!state.accounts.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">还没有账号。点"添加账号"开始。</td></tr>';
    return;
  }
  body.innerHTML = state.accounts.map((row) => {
    const [badge] = accountStatus(row);
    const credit = row.credit == null ? '<span class="muted">未知</span>' : Number(row.credit).toFixed(0);
    const expiry = row.token_expires_at ? fmtTime(row.token_expires_at) : "-";
    const uid = escapeHtml(row.uid || row.id);
    const toggle = row.disabled
      ? `<button class="ghost" data-act="enable" data-uid="${uid}">启用</button>`
      : `<button class="ghost" data-act="disable" data-uid="${uid}">停用</button>`;
    return `<tr>
      <td>${escapeHtml(row.nickname || row.email || row.uid || row.id)}${row.note ? `<div class="muted">${escapeHtml(row.note)}</div>` : ""}</td>
      <td>${escapeHtml(row.realm || "-")}</td>
      <td>${credit}</td>
      <td>${escapeHtml(expiry)}</td>
      <td>${badge}</td>
      <td class="actions">
        <button class="ghost" data-act="test" data-uid="${uid}">测试</button>
        <button class="ghost" data-act="refresh" data-uid="${uid}">续期</button>
        <button class="ghost" data-act="balance" data-uid="${uid}">查额度</button>
        ${row.realm === "cn" ? `<button class="ghost" data-act="checkin" data-uid="${uid}">签到</button>` : ""}
        ${toggle}
        <button class="danger" data-act="delete" data-uid="${uid}">删除</button>
      </td>
    </tr>`;
  }).join("");
}

async function accountAction(uid, action, button) {
  if (action === "delete" && !confirm("确定删除该账号？")) return;
  const label = button ? button.textContent : "";
  if (button) { button.disabled = true; button.textContent = "…"; }
  try {
    const result = await api(`/api/accounts/${encodeURIComponent(uid)}/${action}`, { method: "POST", body: "{}" });
    if (action === "test") {
      alert(result.ok ? `账号可用（模型 ${result.model}）` : `测试失败：${result.error || "未知"}`);
    } else if (action === "balance") {
      alert(result.ok ? `余额 ${result.credits}` : "读取余额失败（上游没给可用字段）");
    } else if (action === "checkin") {
      alert(result.message || (result.ok ? "已签到" : "签到失败"));
    }
  } catch (error) {
    alert(error.message);
  } finally {
    if (button) { button.disabled = false; button.textContent = label; }
    await loadAccounts();
  }
}

async function submitAccount() {
  const payload = {
    realm: $("#add-realm").value,
    note: $("#add-note").value.trim(),
    refresh_token: $("#add-refresh").value.trim(),
    access_token: $("#add-access").value.trim(),
  };
  if (!payload.refresh_token && !payload.access_token) {
    $("#add-error").textContent = "至少填 refresh token 或 access token";
    return;
  }
  $("#add-error").textContent = "";
  try {
    await api("/api/accounts/add", { method: "POST", body: JSON.stringify(payload) });
    $("#add-refresh").value = "";
    $("#add-access").value = "";
    $("#add-note").value = "";
    $("#add-panel").classList.add("hidden");
    await loadAccounts();
  } catch (error) {
    $("#add-error").textContent = error.message;
  }
}

async function startLoginFlow() {
  try {
    const data = await api("/api/accounts/login/start", {
      method: "POST",
      body: JSON.stringify({ realm: $("#add-realm").value }),
    });
    $("#login-flow").classList.remove("hidden");
    const link = $("#login-url");
    link.textContent = data.url;
    link.href = data.url;
    $("#login-state").textContent = "等待登录…";
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(async () => {
      try {
        const poll = await api("/api/accounts/login/poll?state=" + encodeURIComponent(data.state));
        $("#login-state").textContent = `${poll.state} ${poll.message || ""}`;
        if (poll.state === "ok") {
          clearInterval(state.pollTimer);
          $("#login-state").textContent = "登录成功，账号已添加。";
          await loadAccounts();
        } else if (poll.state === "expired" || poll.state === "error") {
          clearInterval(state.pollTimer);
        }
      } catch (error) {
        $("#login-state").textContent = error.message;
        clearInterval(state.pollTimer);
      }
    }, 3000);
  } catch (error) {
    $("#add-error").textContent = error.message;
  }
}

// ---------------------------------------------------------------- 设置

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    state.settings = data;
    $("#set-listen").value = data.listen || "";
    $("#set-prompt").value = data.prompt_mode || "passthrough";
    $("#set-apikey").value = "";
    $("#set-apikey").placeholder = data.default_api_key_set ? "已设置（留空则不修改）" : "sk-...";
    renderKeys(data.api_keys || []);
  } catch (error) {
    console.warn("读取设置失败", error);
  }
}

function renderKeys(keys) {
  const body = $("#keys tbody");
  if (!keys.length) {
    body.innerHTML = '<tr><td colspan="6" class="muted">没有客户端密钥；网关当前对本机开放。</td></tr>';
    return;
  }
  body.innerHTML = keys.map((key) => `<tr>
    <td>${escapeHtml(key.name || "-")}</td>
    <td><code>${escapeHtml(key.key || "")}</code></td>
    <td>${escapeHtml(key.realm || "跟随")}</td>
    <td>${escapeHtml((key.models || []).join(", ") || "全部")}</td>
    <td>${fmtNum(key.requests || 0)}</td>
    <td><button class="danger" data-key="${escapeHtml(key.name)}" data-act="del-key">删除</button></td>
  </tr>`).join("");
}

async function saveGateway() {
  const payload = { listen: $("#set-listen").value.trim(), prompt_mode: $("#set-prompt").value };
  const apiKey = $("#set-apikey").value.trim();
  if (apiKey) payload.api_key = apiKey;
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    $("#settings-error").textContent = "";
    await loadSettings();
    alert("已保存（监听地址需重启生效）");
  } catch (error) {
    $("#settings-error").textContent = error.message;
  }
}

async function addKey() {
  const name = $("#key-name").value.trim();
  const models = $("#key-models").value.split(",").map((s) => s.trim()).filter(Boolean);
  if (!name) { $("#settings-error").textContent = "先填名称"; return; }
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ keys_add: [{ name, models, realm: $("#key-realm").value }] }),
    });
    $("#key-name").value = "";
    $("#key-models").value = "";
    $("#settings-error").textContent = "";
    await loadSettings();
  } catch (error) {
    $("#settings-error").textContent = error.message;
  }
}

async function deleteKey(name) {
  if (!confirm(`删除密钥 ${name}？`)) return;
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ keys_delete: [name] }) });
    await loadSettings();
  } catch (error) {
    $("#settings-error").textContent = error.message;
  }
}

async function savePassword() {
  const password = $("#set-password").value;
  if (password.length < 6) { $("#settings-error").textContent = "密码至少 6 位"; return; }
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ panel_password: password }) });
    $("#set-password").value = "";
    $("#settings-error").textContent = "";
    alert("密码已更新，请重新登录。");
    showLogin();
  } catch (error) {
    $("#settings-error").textContent = error.message;
  }
}

// ---------------------------------------------------------------- 交互

function switchTab(name) {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === "tab-" + name));
  if (name === "accounts") loadAccounts().catch((error) => console.warn(error));
  if (name === "settings") loadSettings();
}

document.addEventListener("DOMContentLoaded", () => {
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const data = await api("/panel/login", {
        method: "POST",
        body: JSON.stringify({ password: $("#password").value }),
      });
      state.token = data.token;
      localStorage.setItem(TOKEN_KEY, data.token);
      $("#login-error").textContent = "";
      $("#password").value = "";
      const status = await api("/panel/status");
      await enterApp(status);
    } catch (error) {
      $("#login-error").textContent = error.message;
    }
  });

  $$(".tab").forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));

  $("#logout").addEventListener("click", () => {
    api("/panel/logout", { method: "POST", body: "{}" }).catch(() => {});
    showLogin();
  });

  $("#refresh-accounts").addEventListener("click", () => loadAccounts().catch((e) => alert(e.message)));
  $("#realm-filter").addEventListener("change", () => loadAccounts().catch((e) => alert(e.message)));
  $("#show-add").addEventListener("click", () => $("#add-panel").classList.toggle("hidden"));
  $("#add-submit").addEventListener("click", submitAccount);
  $("#login-by-url").addEventListener("click", startLoginFlow);

  $("#accounts").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-act]");
    if (!button) return;
    accountAction(button.dataset.uid, button.dataset.act, button);
  });

  $("#save-gateway").addEventListener("click", saveGateway);
  $("#add-key").addEventListener("click", addKey);
  $("#save-password").addEventListener("click", savePassword);
  $("#keys").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-act='del-key']");
    if (button) deleteKey(button.dataset.key);
  });

  boot();
});
