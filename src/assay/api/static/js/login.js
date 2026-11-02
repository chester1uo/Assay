// login.js — /login: sign in, sign up with an invitation (?invite=), set a new password
// (?reset=), or create the first admin (when the server has no users yet).
// After success it goes to ?next= (a path on this site) or the app.

const T = {
  en: {
    signIn: "Sign in", signInSub: "Sign in to your Assay account.",
    username: "Username", password: "Password", displayName: "Name", optional: "optional",
    usernameHint: "a-z, 0-9 and . _ - @", passwordHint: "At least 10 characters.",
    confirm: "Confirm password", noAccount: "No account? Ask an admin for an invitation link.",
    register: "Create account", registerSub: (role) => `You were invited as ${role}.`,
    inviteBad: "This invitation is invalid, expired or used up. Ask an admin for a new link.",
    reset: "Set a new password", resetSub: "Choose a new password for your account.", save: "Save and sign in",
    setup: "Create the admin account", setupSub: "This server has no users yet. Enter the setup token from the server (setup_token in the state directory).",
    setupToken: "Setup token", create: "Create account",
    off: "Accounts are off", offSub: "This server does not use accounts.", openApp: "Open Assay",
    mismatch: "Passwords do not match.", failed: "Something went wrong. Try again.",
    wrongLogin: "Wrong username or password.", tooMany: "Too many attempts. Try again in a few minutes.",
    roles: { researcher: "a researcher", admin: "an admin" }, docs: "Docs", lang: "中文",
  },
  zh: {
    signIn: "登录", signInSub: "登录你的 Assay 账户。",
    username: "用户名", password: "密码", displayName: "显示名", optional: "可选",
    usernameHint: "a-z、0-9 和 . _ - @", passwordHint: "至少 10 个字符。",
    confirm: "确认密码", noAccount: "还没有账户？向管理员索取邀请链接。",
    register: "注册账户", registerSub: (role) => `你受邀成为${role}。`,
    inviteBad: "邀请链接无效、已过期或已用完。请向管理员索取新的链接。",
    reset: "设置新密码", resetSub: "为你的账户设置新密码。", save: "保存并登录",
    setup: "创建管理员账户", setupSub: "服务器上还没有用户。请输入服务器上的 setup token（状态目录里的 setup_token）。",
    setupToken: "Setup token", create: "创建账户",
    off: "未启用账户", offSub: "此服务器不使用账户登录。", openApp: "打开 Assay",
    mismatch: "两次输入的密码不一致。", failed: "出错了，请重试。",
    wrongLogin: "用户名或密码错误。", tooMany: "尝试次数过多，请几分钟后再试。",
    roles: { researcher: "研究员", admin: "管理员" }, docs: "文档", lang: "EN",
  },
};

let lang = (() => {
  try { const v = localStorage.getItem("assay_lang"); if (v === "en" || v === "zh") return v; } catch (_) {}
  return (navigator.language || "en").toLowerCase().startsWith("zh") ? "zh" : "en";
})();
const t = (k) => T[lang][k];
const params = new URLSearchParams(location.search);
const card = document.getElementById("card");

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function nextPath() {
  const n = params.get("next") || "/";
  return n.startsWith("/") && !n.startsWith("//") && !n.startsWith("/login") ? n : "/";
}

async function call(method, path, body) {
  const res = await fetch(path, {
    method, credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) {
    if (res.status === 429) throw new Error(t("tooMany"));
    if (res.status === 401 && path === "/auth/login") throw new Error(t("wrongLogin"));
    const msg = (data && data.error && data.error.message) || (data && data.detail) || t("failed");
    throw new Error(msg);
  }
  return data;
}

function field(name, label, { type = "text", hint = "", auto = "", optional = false, value = "" } = {}) {
  return `<label class="auth-field"><span>${esc(label)}${optional ? ` <small>(${esc(t("optional"))})</small>` : ""}</span>
    <input class="input" name="${name}" type="${type}" autocomplete="${auto}" value="${esc(value)}" ${optional ? "" : "required"} spellcheck="false" />
    ${hint ? `<small>${esc(hint)}</small>` : ""}</label>`;
}

function render(title, sub, fields, button, onSubmit, note = "") {
  card.innerHTML = `
    <div><h1>${esc(title)}</h1>${sub ? `<p class="auth-sub">${esc(sub)}</p>` : ""}</div>
    ${fields !== null ? `<form class="auth-form" novalidate>${fields}
      <div class="auth-err" role="alert"></div>
      <button class="btn btn--primary" type="submit">${esc(button)}</button></form>` : ""}
    ${note}`;
  const form = card.querySelector("form");
  if (!form) return;
  const err = form.querySelector(".auth-err");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    const data = Object.fromEntries(new FormData(form).entries());
    if ("confirm" in data && data.confirm !== data.password) { err.textContent = t("mismatch"); return; }
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      await onSubmit(data);
      location.replace(nextPath());
    } catch (ex) {
      err.textContent = ex.message || t("failed");
      btn.disabled = false;
    }
  });
  const first = form.querySelector("input");
  if (first) first.focus();
}

const passwordFields = () =>
  field("password", t("password"), { type: "password", auto: "new-password", hint: t("passwordHint") }) +
  field("confirm", t("confirm"), { type: "password", auto: "new-password" });

async function show() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.getElementById("lang").textContent = t("lang");
  document.getElementById("foot-docs").textContent = t("docs");
  let status = { accounts: false };
  try { status = await call("GET", "/auth/status"); } catch (_) { /* treat as off */ }

  if (!status.accounts) {
    document.title = `${t("off")} · Assay`;
    render(t("off"), t("offSub"), null, "", null, `<a class="btn btn--primary" href="/">${esc(t("openApp"))}</a>`);
    return;
  }
  if (status.setup_required) {
    document.title = `${t("setup")} · Assay`;
    render(t("setup"), t("setupSub"),
      field("token", t("setupToken"), { auto: "off" }) +
      field("username", t("username"), { auto: "username", hint: t("usernameHint") }) +
      field("display_name", t("displayName"), { auto: "name", optional: true }) + passwordFields(),
      t("create"), (d) => call("POST", "/auth/setup", d));
    return;
  }
  const invite = params.get("invite");
  if (invite) {
    document.title = `${t("register")} · Assay`;
    let info;
    try { info = await call("GET", `/auth/invite?code=${encodeURIComponent(invite)}`); } catch (_) {
      render(t("register"), t("inviteBad"), null, "", null, `<p class="auth-note"><a href="/login">${esc(t("signIn"))}</a></p>`);
      return;
    }
    render(t("register"), t("registerSub")(t("roles")[info.role] || info.role),
      field("username", t("username"), { auto: "username", hint: t("usernameHint") }) +
      field("display_name", t("displayName"), { auto: "name", optional: true }) + passwordFields(),
      t("create"), (d) => call("POST", "/auth/register", { ...d, invite_code: invite }));
    return;
  }
  const reset = params.get("reset");
  if (reset) {
    document.title = `${t("reset")} · Assay`;
    render(t("reset"), t("resetSub"), passwordFields(), t("save"),
      (d) => call("POST", "/auth/reset-password", { token: reset, password: d.password }));
    return;
  }
  try {
    const me = await call("GET", "/auth/me");
    if (me && me.user) { location.replace(nextPath()); return; }
  } catch (_) { /* not signed in */ }
  document.title = `${t("signIn")} · Assay`;
  render(t("signIn"), t("signInSub"),
    field("username", t("username"), { auto: "username" }) +
    field("password", t("password"), { type: "password", auto: "current-password" }),
    t("signIn"), (d) => call("POST", "/auth/login", d),
    `<p class="auth-note">${esc(t("noAccount"))}</p>`);
}

document.getElementById("lang").addEventListener("click", () => {
  lang = lang === "en" ? "zh" : "en";
  try { localStorage.setItem("assay_lang", lang); } catch (_) {}
  show();
});

show();
