// AstrBot 4.24.2 - 4.28 inject the bridge as `window.AstrBotPluginPage`.
// AstrBot 4.29+ inject it as `window.AstrBotPluginView`, keeping the older
// global as an alias. Accept whichever name the host provides so one bundle
// works across the whole 4.24.2+ range, and fall back to same-origin HTTP when
// neither global exists (for example if the page is opened outside Dashboard).
const bridge = (() => {
  const native = window.AstrBotPluginPage || window.AstrBotPluginView;
  if (native) {
    return native;
  }
  const base = "/api/plug/astrbot_plugin_cyber_life/";
  const call = async (method, endpoint, payload) => {
    const url = new URL(
      `${base}${String(endpoint || "").replace(/^\/+/, "")}`,
      window.location.origin,
    );
    const init = { method, credentials: "same-origin" };
    if (method === "GET") {
      Object.entries(payload || {}).forEach(([key, value]) => {
        if (value !== undefined && value !== null) {
          url.searchParams.set(key, String(value));
        }
      });
    } else {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(payload || {});
    }
    const response = await fetch(url, init);
    const text = await response.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
    if (!response.ok) {
      throw new Error((data && data.message) || `HTTP ${response.status}`);
    }
    if (data && data.status === "error") {
      throw new Error(data.message || "请求失败");
    }
    return data && data.data !== undefined ? data.data : data;
  };
  return {
    ready: async () => {},
    getContext: () => ({}),
    onContext: () => () => {},
    t: (_key, fallback) => fallback,
    apiGet: (endpoint, params) => call("GET", endpoint, params),
    apiPost: (endpoint, body) => call("POST", endpoint, body),
  };
})();
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const PLATFORM_META = {
  bilibili: { label: "B站", color: "#fb7299", short: "B" },
  xiaoheihe: { label: "小黑盒", color: "#e8b500", short: "盒" },
  tieba: { label: "贴吧", color: "#4e6ef2", short: "贴" },
};

const state = {
  overview: null,
  settings: null,
  loginId: "",
  loginPlatform: "",
  loginTimer: null,
  toastTimer: null,
  tab: "overview",
};

function toast(message, isError = false) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.toggle("error", Boolean(isError));
  box.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    box.hidden = true;
  }, 3600);
}

function setText(selector, value) {
  const node = $(selector);
  if (node) node.textContent = value ?? "";
}

function fmtMinutes(value) {
  const minutes = Number(value);
  if (!Number.isFinite(minutes) || minutes <= 0) return "—";
  if (minutes < 60) return `${Math.round(minutes)} 分`;
  const hours = Math.floor(minutes / 60);
  const rest = Math.round(minutes % 60);
  return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
}

function timeOf(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function percent(value, limit) {
  const max = Number(limit) || 0;
  if (!max) return 0;
  return Math.max(0, Math.min(100, (Number(value) / max) * 100));
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function platformMeta(name) {
  return PLATFORM_META[name] || { label: name, color: "#7c8aa5", short: String(name).slice(0, 1) };
}

/* --------------------------------------------------------------- rendering */

function renderOverview(data) {
  state.overview = data;
  const status = data.status || {};
  const mood = data.mood || {};
  const today = status.today || {};
  const settings = data.settings || {};

  const phasePill = $("#phase-pill");
  const effectiveRunning = status.effective_running ?? status.running;
  const phaseText = status.phase_label || "—";
  const modeText = status.access_mode_label || "";
  const scheduleLabelBase = modeText && modeText !== phaseText ? `${phaseText} · ${modeText}` : phaseText;
  const scheduleLabel = status.holiday_name ? `${scheduleLabelBase} · ${status.holiday_name}` : scheduleLabelBase;
  phasePill.textContent = `${scheduleLabel} · ${effectiveRunning ? "运行中" : "休息"}`;
  phasePill.dataset.phase = status.phase || "off";
  setText("#mood-pill", `${mood.emoji || "🙂"} ${mood.label || "平静"}`);
  setText("#today-date", today.date || "—");
  setText("#auto-phase-label", scheduleLabel);

  renderMood(mood);
  renderStats(today, settings, status);
  renderPlatformChips(data.platforms || [], status.platforms || []);
  renderDeps(data.dependencies || {});
  renderNextTimes(today);
  renderTimeline(status.history || []);
  renderAccounts(data.accounts || [], data.platforms || []);
  renderPlatformList(data.platforms || []);

  if (!state.settings) {
    state.settings = settings;
    fillSettings(settings, data.platforms || []);
  }
  const errorText = status.last_error ? String(status.last_error) : "";
  setText("#error-text", errorText ? `⚠ ${errorText.slice(0, 80)}` : "");
}

function renderMood(mood) {
  setText("#mood-emoji", mood.emoji || "🙂");
  setText("#mood-label", mood.label || "平静");
  setText("#mood-score", String(mood.score ?? 50));
  setText("#mood-tag", `高兴值 ${Number(mood.happy ?? 0) >= 0 ? "+" : ""}${Number(mood.happy ?? 0).toFixed(1)}`);
  const updated = mood.updated_at ? new Date(mood.updated_at) : null;
  setText("#mood-updated", updated && !Number.isNaN(updated.getTime())
    ? `${updated.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 更新`
    : "刚刚更新");

  const bars = $("#mood-bars");
  bars.replaceChildren();
  const values = mood.bars || {};
  for (const [label, value] of Object.entries(values)) {
    const row = el("div", "bar-row");
    if (label === "压力" || (label === "高兴" && Number(mood.happy) < 0)) row.classList.add("neg");
    row.append(el("span", "", label));
    const track = el("div", "bar-track");
    const fill = el("i", "bar-fill");
    fill.style.width = `${Math.max(0, Math.min(100, Number(value) || 0))}%`;
    track.append(fill);
    row.append(track);
    row.append(el("span", "bar-value", String(Math.round(Number(value) || 0))));
    bars.append(row);
  }

  const reasons = $("#mood-reasons");
  reasons.replaceChildren();
  const list = (mood.reasons || []).slice(0, 4);
  if (!list.length) list.push("还没有什么特别的心情起伏");
  for (const reason of list) reasons.append(el("span", "", String(reason)));

  drawSpark(mood.history || []);
}

function drawSpark(history) {
  const canvas = $("#mood-spark");
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || canvas.parentElement.clientWidth || 400;
  const height = 56;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const styles = getComputedStyle(document.documentElement);
  const line = styles.getPropertyValue("--line").trim() || "rgba(0,0,0,.1)";
  const accent = styles.getPropertyValue("--accent").trim() || "#5b8cff";
  ctx.strokeStyle = line;
  ctx.lineWidth = 1;
  for (let i = 1; i < 3; i += 1) {
    ctx.beginPath();
    ctx.moveTo(0, (height / 3) * i);
    ctx.lineTo(width, (height / 3) * i);
    ctx.stroke();
  }
  if (history.length < 2) return;

  const points = history.slice(-48).map((item, index, array) => ({
    x: (index / Math.max(1, array.length - 1)) * (width - 8) + 4,
    y: height - 5 - ((Number(item.score) || 50) / 100) * (height - 12),
  }));
  const gradient = ctx.createLinearGradient(0, 0, 0, height);
  gradient.addColorStop(0, "rgba(91,140,255,.28)");
  gradient.addColorStop(1, "rgba(91,140,255,0)");
  ctx.beginPath();
  ctx.moveTo(points[0].x, height);
  for (const point of points) ctx.lineTo(point.x, point.y);
  ctx.lineTo(points[points.length - 1].x, height);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.strokeStyle = accent;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.stroke();
  const last = points[points.length - 1];
  ctx.beginPath();
  ctx.arc(last.x, last.y, 3.2, 0, Math.PI * 2);
  ctx.fillStyle = accent;
  ctx.fill();
}

function renderStats(today, settings, status) {
  const watchMinutes = (Number(today.watch_seconds) || 0) / 60;
  setText("#watch-value", `${Math.round(watchMinutes)}`);
  $("#watch-value").append(el("small", "", "分"));
  setText("#comment-value", String(today.comments ?? 0));
  $("#comment-value").append(el("small", "", "条"));
  setText("#reply-value", String(today.replies ?? 0));
  $("#reply-value").append(el("small", "", "条"));
  setText("#draw-value", String(today.draws ?? 0));
  $("#draw-value").append(el("small", "", "张"));

  $("#watch-meter").style.width = `${percent(watchMinutes, settings.daily_watch_minutes || 60)}%`;
  $("#comment-meter").style.width = `${percent(today.comments, today.comment_limit || settings.max_comments_per_day || 8)}%`;
  $("#reply-meter").style.width = `${percent(today.replies, today.reply_limit || settings.max_replies_per_day || 6)}%`;
  $("#draw-meter").style.width = `${percent(today.draws, today.draw_limit || settings.max_draws_per_day || 3)}%`;

  $("#run-dot").classList.toggle("on", Boolean(status.effective_running ?? status.running));
  const nowText = status.busy
    ? "正在忙…"
    : status.paused
      ? "已暂停"
      : status.effective_running
        ? status.current || "在等你"
        : status.active_now === false
          ? `峰时休息（${status.access_mode_label || "仅谷时"}）`
          : "待机中";
  setText("#now-text", nowText);
}

function renderPlatformChips(platforms, eligible) {
  const box = $("#platform-chips");
  box.replaceChildren();
  for (const item of platforms) {
    const chip = el("span", "chip");
    const on = eligible.includes(item.platform);
    chip.classList.add(on ? "ok" : "warn");
    chip.append(el("i"));
    chip.append(el("span", "", `${item.label}${item.logged_in ? ` · ${item.nickname || item.account}` : " · 未登录"}`));
    box.append(chip);
  }
}

function renderDeps(deps) {
  const box = $("#deps");
  box.replaceChildren();
  const asr = deps.asr || {};
  const items = [
    ["Playwright", deps.playwright, deps.playwright ? "已安装" : "未安装，需 pip install"],
    ["二维码", deps.qrcode, deps.qrcode ? "可用" : "缺少 qrcode"],
    ["ffmpeg", deps.ffmpeg, deps.ffmpeg ? "可用" : "缺少，视频抽帧不可用"],
    ["视觉模型", deps.llm, deps.llm ? "已连接" : "未配置，会退回标题推断"],
    ["本地听写", asr.installed, asr.installed ? "faster-whisper 已安装" : "未安装，B站字幕仍可用"],
    ["语音模型", asr.model_ready, asr.model_ready ? `${asr.model} 已下载` : "首次听写时下载"],
    ["ComfyUI", deps.comfyui_configured, deps.comfyui_configured ? "已配置" : "未启用"],
  ];
  for (const [name, ok, hint] of items) {
    const node = el("div", `dep ${ok ? "ok" : "bad"}`);
    node.append(el("b", "", name));
    node.append(el("span", "", hint));
    box.append(node);
  }
}

function renderNextTimes(today) {
  const box = $("#next-times");
  box.replaceChildren();
  const items = [
    ["下次刷帖", today.next_watch_at],
    ["下次评论", today.next_comment_at],
    ["下次回复", today.next_reply_at],
    ["下次画画", today.next_draw_at],
  ];
  for (const [label, iso] of items) {
    const node = el("div", "next-item");
    node.append(el("b", "", label));
    node.append(el("span", "", timeOf(iso)));
    box.append(node);
  }
}

function renderPlatformList(platforms) {
  const box = $("#platform-list");
  box.replaceChildren();
  for (const item of platforms) {
    const meta = platformMeta(item.platform);
    const row = el("div", "platform-row");
    const left = el("div", "platform-left");
    const icon = el("div", "platform-icon", meta.short);
    icon.style.background = `linear-gradient(140deg, ${meta.color}, ${meta.color}bb)`;
    const info = el("div");
    info.append(el("div", "platform-name", item.label));
    info.append(el("div", "platform-sub", item.logged_in
      ? `已登录：${item.nickname || item.account}`
      : item.login === "api_qr" ? "扫码登录（API）" : "扫码登录（内置浏览器）"));
    left.append(icon, info);
    row.append(left);
    const action = el("button", "btn", item.logged_in ? "重新登录" : "扫码登录");
    action.addEventListener("click", () => startLogin(item.platform));
    row.append(action);
    box.append(row);
  }
}

function renderAccounts(accounts, platforms) {
  const box = $("#account-list");
  box.replaceChildren();
  if (!accounts.length) {
    box.append(el("div", "muted", "还没有保存的账号。先在上面的平台卡片里扫码登录。"));
    return;
  }
  for (const account of accounts) {
    const meta = platformMeta(account.platform);
    const row = el("div", "account-row");
    const main = el("div", "account-main");
    const title = el("div", "account-title", `${meta.label} · ${account.nickname || account.name}`);
    const sub = el("div", "account-meta",
      `${account.status === "logged_in" ? "登录有效" : "登录已失效"} · 保存于 ${timeOf(account.last_login_at)}`);
    main.append(title, sub);
    if (account.meta?.uid) main.append(el("div", "account-meta", `UID ${account.meta.uid}`));

    const tag = el("span", `tag ${account.status === "logged_in" ? "ok" : "off"}`,
      account.status === "logged_in" ? "在线" : "离线");
    main.append(tag);
    row.append(main);

    const actions = el("div", "row-actions");
    const active = platforms.find((item) => item.platform === account.platform && item.account === account.name);
    if (account.status === "logged_in" && !active) {
      const use = el("button", "small-btn", "设为当前");
      use.addEventListener("click", () => setActive(account.platform, account.name));
      actions.append(use);
    }
    const remove = el("button", "small-btn danger", "删除");
    remove.addEventListener("click", () => {
      if (remove.dataset.armed === "1") {
        removeAccount(account);
        return;
      }
      remove.dataset.armed = "1";
      remove.textContent = "再点一次删除";
      setTimeout(() => {
        remove.dataset.armed = "";
        remove.textContent = "删除";
      }, 3000);
    });
    actions.append(remove);
    row.append(actions);
    box.append(row);
  }
}

function renderTimeline(history) {
  const box = $("#timeline");
  box.replaceChildren();
  if (!history.length) {
    box.append(el("div", "muted", "还没有动态。登录之后，她会在允许的时间里自己找事做。"));
    return;
  }
  const kindLabel = {
    watch: "刷视频",
    browse: "浏览",
    comment: "评论",
    reply: "回复",
    draw: "画画",
    draw_failed: "画画失败",
    error: "出错",
  };
  const platformLabel = {
    bilibili: "B站",
    xiaoheihe: "小黑盒",
    tieba: "贴吧",
  };
  for (const event of history) {
    const row = el("div", "event");
    row.append(el("div", "event-time", timeOf(event.at)));
    const body = el("div", "event-body");
    const platform = platformLabel[event.platform] || event.platform || "";
    let kind = kindLabel[event.kind] || event.kind || "事件";
    if (event.kind === "watch" && platform && platform !== "B站") kind = "刷帖子";
    body.append(el("div", `event-kind kind-${event.kind || "watch"}`, platform ? `${kind} · ${platform}` : kind));
    if (event.title) {
      const title = el("div", "event-title");
      if (event.url) {
        const link = el("a", "", event.title);
        link.href = event.url;
        link.target = "_blank";
        link.rel = "noreferrer";
        title.append(link);
      } else {
        title.textContent = event.title;
      }
      body.append(title);
    }
    if (event.summary) body.append(el("div", "event-text", event.summary));
    if (event.transcript) {
      const source = event.transcript_source ? `（${event.transcript_source}）` : "";
      body.append(el("div", "event-text", `语音${source}：${event.transcript}`));
    }
    if (event.text) body.append(el("div", "event-text", event.text));
    if (event.to) body.append(el("div", "event-text", `回复给 ${event.to}`));
    if (event.prompt) body.append(el("div", "event-text", event.prompt));
    if (event.error) body.append(el("div", "event-text", event.error));
    if (event.kind === "watch" && event.frames) {
      body.append(el("div", "event-text", `关键帧 ${event.frames} 张合成为一张图后识别`));
    }
    row.append(body);
    box.append(row);
  }
}

/* --------------------------------------------------------------- settings */

function platformChecks(platforms) {
  const box = $("#platform-checks");
  box.replaceChildren();
  const current = new Set(state.settings?.platforms || ["bilibili"]);
  for (const item of platforms) {
    const label = el("label", "check-chip");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = item.platform;
    input.checked = current.has(item.platform);
    label.append(input, el("span", "", item.label));
    box.append(label);
  }
}

function fillSettings(settings, platforms) {
  for (const input of $$("[data-key]")) {
    const key = input.dataset.key;
    if (!(key in settings)) continue;
    const type = input.dataset.type;
    if (type === "bool") input.checked = Boolean(settings[key]);
    else if (type === "radio") input.checked = String(settings[key]) === input.value;
    else if (type === "list") input.value = Array.isArray(settings[key]) ? settings[key].join(", ") : String(settings[key] || "");
    else input.value = settings[key] ?? "";
  }
  platformChecks(platforms);
  updateProbLabel();
  updatePeakFrequencyState();
}

function updateProbLabel() {
  const input = $("[data-key='comment_probability']");
  if (input) setText("#prob-label", `${Math.round(Number(input.value) * 100)}%`);
}

function updatePeakFrequencyState() {
  const checked = $("input[name='access_mode']:checked");
  const row = $("#peak-frequency-row");
  const allDay = !checked || checked.value === "all_day";
  if (row) row.hidden = !allDay;
  const input = $("[data-key='peak_frequency_percent']");
  if (input) setText("#peak-frequency-label", `${input.value}%`);
}

function collectSettings() {
  const patch = {};
  for (const input of $$("[data-key]")) {
    const key = input.dataset.key;
    const type = input.dataset.type;
    if (type === "radio") {
      if (input.checked) patch[key] = input.value;
      continue;
    }
    if (type === "bool") patch[key] = input.checked;
    else if (type === "int") patch[key] = Number.parseInt(input.value, 10) || 0;
    else if (type === "float") patch[key] = Number.parseFloat(input.value) || 0;
    else if (type === "list") patch[key] = input.value.split(/[,，]/).map((part) => part.trim()).filter(Boolean);
    else patch[key] = input.value;
  }
  const selected = $$("#platform-checks input:checked").map((input) => input.value);
  patch.platforms = selected.length ? selected : ["bilibili"];
  return patch;
}

/* ----------------------------------------------------------------- actions */

function renderBalance(data) {
  const total = $("#balance-total");
  const currency = $("#balance-currency");
  const detail = $("#balance-detail");
  const status = $("#balance-status");
  if (!total || !detail) return;
  detail.replaceChildren();
  if (!data || data.ok === false) {
    total.textContent = "—";
    currency.textContent = "";
    detail.textContent = data?.error || "余额读取失败";
    detail.classList.add("balance-error");
    status.textContent = "余额接口不会返回 API Key，只读取公开的余额字段。";
    return;
  }
  detail.classList.remove("balance-error");
  const balances = Array.isArray(data.balances) ? data.balances : [];
  const first = balances[0] || {};
  total.textContent = first.total_balance || "0.00";
  currency.textContent = first.currency || "";
  if (!balances.length) {
    detail.textContent = "接口没有返回余额明细。";
  }
  for (const item of balances) {
    const row = el("div", "balance-row");
    row.append(el("span", "", `${item.currency || "余额"} · 赠送 ${item.granted_balance || "0"} / 充值 ${item.topped_up_balance || "0"}`));
    row.append(el("b", "", item.total_balance || "0"));
    detail.append(row);
  }
  const fetched = data.fetched_at ? new Date(data.fetched_at) : null;
  const timeText = fetched && !Number.isNaN(fetched.getTime())
    ? fetched.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
    : "";
  status.textContent = data.is_available
    ? `账户可用${timeText ? ` · ${timeText} 更新` : ""}${data.cached ? " · 缓存" : ""}`
    : "账户当前不可用，请检查 Key 或余额。";
}

async function loadBalance(force = false) {
  const button = $("#balance-refresh");
  if (button) button.disabled = true;
  try {
    const data = force
      ? await bridge.apiPost("deepseek/balance", { refresh: true })
      : await bridge.apiGet("deepseek/balance");
    renderBalance(data);
    if (force) toast("余额已刷新");
  } catch (error) {
    renderBalance({ ok: false, error: `读取余额失败：${error.message}` });
    if (force) toast(`余额读取失败：${error.message}`, true);
  } finally {
    if (button) button.disabled = false;
  }
}

async function loadOverview(silent = false) {
  try {
    const data = await bridge.apiGet("overview");
    renderOverview(data);
    if (!silent) toast("已刷新");
  } catch (error) {
    if (!silent) toast(`读取失败：${error.message}`, true);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const button = $(".save-bar .btn");
  button.disabled = true;
  try {
    const patch = collectSettings();
    const result = await bridge.apiPost("settings", patch);
    state.settings = result.settings || { ...state.settings, ...patch };
    fillSettings(state.settings, state.overview?.platforms || []);
    toast("设置已保存");
    await loadOverview(true);
  } catch (error) {
    toast(`保存失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function startLogin(platform) {
  state.loginPlatform = platform;
  const meta = platformMeta(platform);
  $("#modal").hidden = false;
  $("#modal-title").textContent = `${meta.label} 扫码登录`;
  $("#modal-desc").textContent = platform === "bilibili"
    ? "用 B站 App 扫一扫，扫完在手机上确认"
    : "用对应 App 扫码；如果页面没自动识别，点下方手动确认";
  $("#qr-image").removeAttribute("src");
  setText("#qr-status", "正在生成二维码…");
  $("#qr-confirm").disabled = true;
  $("#qr-image").style.opacity = "0.3";
  try {
    const result = await bridge.apiPost("login/start", { platform, name: "default" });
    const login = result.login;
    state.loginId = login.id;
    $("#qr-image").style.opacity = "1";
    if (login.image) $("#qr-image").src = login.image;
    setText("#qr-status", login.message || "等待扫码");
    $("#qr-confirm").disabled = platform === "bilibili";
    pollLogin(login.id);
  } catch (error) {
    setText("#qr-status", `发起失败：${error.message}`);
    toast(`登录失败：${error.message}`, true);
  }
}

function pollLogin(id) {
  clearInterval(state.loginTimer);
  state.loginTimer = setInterval(async () => {
    try {
      const result = await bridge.apiGet(`login/status/${id}`);
      const login = result.login;
      if (login.image) $("#qr-image").src = login.image;
      const statusText = login.nickname ? `${login.message}（${login.nickname}）` : login.message;
      setText("#qr-status", statusText);
      if (login.status === "success") {
        clearInterval(state.loginTimer);
        setText("#qr-status", `✅ ${statusText}`);
        toast("登录成功，登录态已保存");
        setTimeout(async () => {
          $("#modal").hidden = true;
          state.loginId = "";
          await loadOverview(true);
        }, 1400);
      } else if (login.status === "expired" || login.status === "error") {
        clearInterval(state.loginTimer);
        setText("#qr-status", `⚠ ${statusText}`);
      }
    } catch (error) {
      clearInterval(state.loginTimer);
      setText("#qr-status", `状态读取失败：${error.message}`);
    }
  }, 2000);
}

async function confirmLogin() {
  if (!state.loginId) return;
  try {
    const result = await bridge.apiPost("login/confirm", { id: state.loginId });
    setText("#qr-status", result.login?.message || "已提交确认");
    $("#qr-confirm").disabled = true;
    setTimeout(() => loadOverview(true), 800);
  } catch (error) {
    toast(`确认失败：${error.message}`, true);
  }
}

async function cancelLogin() {
  if (state.loginId) {
    try {
      await bridge.apiPost("login/cancel", { id: state.loginId });
    } catch (error) {
      // ignore
    }
  }
  clearInterval(state.loginTimer);
  state.loginId = "";
  $("#modal").hidden = true;
}

async function removeAccount(account) {
  try {
    await bridge.apiPost("accounts/remove", { platform: account.platform, name: account.name });
    toast("已删除");
    await loadOverview(true);
  } catch (error) {
    toast(`删除失败：${error.message}`, true);
  }
}

async function setActive(platform, name) {
  try {
    await bridge.apiPost("accounts/active", { platform, name });
    toast("已切换当前账号");
    await loadOverview(true);
  } catch (error) {
    toast(`切换失败：${error.message}`, true);
  }
}

async function runNow() {
  const button = $("#run-btn");
  button.disabled = true;
  try {
    await bridge.apiPost("run", { minutes: 10 });
    toast("开始活动，她去找事做了");
    await loadOverview(true);
  } catch (error) {
    toast(`启动失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function stopNow() {
  try {
    await bridge.apiPost("stop", {});
    toast("已暂停自动活动");
    await loadOverview(true);
  } catch (error) {
    toast(`暂停失败：${error.message}`, true);
  }
}

async function drawNow() {
  const button = $("#draw-btn");
  button.disabled = true;
  try {
    await bridge.apiPost("draw", {});
    toast("收到，画好会发给你");
  } catch (error) {
    toast(`画画失败：${error.message}`, true);
  } finally {
    setTimeout(() => (button.disabled = false), 1200);
  }
}

/* ------------------------------------------------------------------- init */

function bindTabs() {
  for (const tab of $$(".tab")) {
    tab.addEventListener("click", () => {
      state.tab = tab.dataset.tab;
      $$(".tab").forEach((item) => item.classList.toggle("active", item === tab));
      $$(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${state.tab}`));
    });
  }
}

function bindActions() {
  $("#refresh-btn").addEventListener("click", () => loadOverview());
  $("#run-btn").addEventListener("click", runNow);
  $("#stop-btn").addEventListener("click", stopNow);
  $("#draw-btn").addEventListener("click", drawNow);
  $("#balance-refresh").addEventListener("click", () => loadBalance(true));
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#modal-close").addEventListener("click", cancelLogin);
  $("#qr-cancel").addEventListener("click", cancelLogin);
  $("#qr-confirm").addEventListener("click", confirmLogin);
  const prob = $("[data-key='comment_probability']");
  if (prob) prob.addEventListener("input", updateProbLabel);
  for (const radio of $$("input[name='access_mode']")) {
    radio.addEventListener("change", updatePeakFrequencyState);
  }
  const peakFrequency = $("[data-key='peak_frequency_percent']");
  if (peakFrequency) peakFrequency.addEventListener("input", updatePeakFrequencyState);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#modal").hidden) cancelLogin();
  });
  window.addEventListener("resize", () => {
    if (state.overview?.mood) drawSpark(state.overview.mood.history || []);
  });
}

async function init() {
  bindTabs();
  bindActions();
  try {
    await bridge.ready();
  } catch (error) {
    toast("无法连接 AstrBot 页面桥接，请从插件页面进入", true);
    return;
  }
  await loadOverview(true);
  loadBalance();
  setInterval(() => {
    if (!document.hidden && !state.loginId) loadOverview(true);
  }, 15000);
}

init();
