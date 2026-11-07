// pages/account.js — the signed-in user's own account: name, password, API keys.

import { registerDict } from "../i18n.js";

registerDict({
  en: {
    "account.title": "Account",
    "account.sub": "Your profile, password and API keys.",
    "account.profile": "Profile",
    "account.username": "Username",
    "account.role": "Role",
    "account.name": "Name",
    "account.save": "Save",
    "account.saved": "Saved.",
    "account.password": "Password",
    "account.current": "Current password",
    "account.new": "New password",
    "account.confirm": "Confirm new password",
    "account.change": "Change password",
    "account.changed": "Password changed. Other browsers are signed out.",
    "account.mismatch": "Passwords do not match.",
    "account.keys": "API keys",
    "account.keysHint": "For the REST API, the Python SDK and agents: send the key as the X-API-Key header. A key acts as you; revoke it when it is no longer needed.",
    "account.keyName": "Key name",
    "account.newKey": "Create key",
    "account.copyOnce": "Copy this key now. It is not shown again.",
    "account.copy": "Copy",
    "account.copied": "Copied",
    "account.noKeys": "No keys yet.",
    "account.created": "Created",
    "account.lastUsed": "Last used",
    "account.never": "never",
    "account.revoke": "Revoke",
    "account.revokeConfirm": "Revoke this key? Programs using it stop working.",
    "account.cancel": "Cancel",
    "role.researcher": "Researcher",
    "role.admin": "Admin",
  },
  zh: {
    "account.title": "账户",
    "account.sub": "个人资料、密码和 API 密钥。",
    "account.profile": "个人资料",
    "account.username": "用户名",
    "account.role": "角色",
    "account.name": "显示名",
    "account.save": "保存",
    "account.saved": "已保存。",
    "account.password": "密码",
    "account.current": "当前密码",
    "account.new": "新密码",
    "account.confirm": "确认新密码",
    "account.change": "修改密码",
    "account.changed": "密码已修改，其他浏览器已退出登录。",
    "account.mismatch": "两次输入的密码不一致。",
    "account.keys": "API 密钥",
    "account.keysHint": "用于 REST API、Python SDK 和 agent：放在 X-API-Key 请求头里。密钥代表你本人，不用时请撤销。",
    "account.keyName": "密钥名称",
    "account.newKey": "创建密钥",
    "account.copyOnce": "请现在复制，之后不再显示。",
    "account.copy": "复制",
    "account.copied": "已复制",
    "account.noKeys": "还没有密钥。",
    "account.created": "创建于",
    "account.lastUsed": "最近使用",
    "account.never": "从未",
    "account.revoke": "撤销",
    "account.revokeConfirm": "撤销这个密钥？使用它的程序会立即失效。",
    "account.cancel": "取消",
    "role.researcher": "研究员",
    "role.admin": "管理员",
  },
});

export function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toISOString().slice(0, 16).replace("T", " ");
}

/** A value shown once with a copy button (keys, invitation links). */
export function secretBox(el, t, value, note) {
  const code = el("code", { className: "mono secret-value" }, value);
  const btn = el("button", { className: "btn btn--sm", type: "button" }, t("account.copy"));
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(value);
      btn.textContent = t("account.copied");
    } catch (_) {
      const range = document.createRange();
      range.selectNodeContents(code);
      getSelection().removeAllRanges();
      getSelection().addRange(range);
    }
  });
  return el("div", { className: "secret-box" }, el("div", { className: "secret-note" }, note), el("div", { className: "secret-row" }, code, btn));
}

/** Inline confirm: the button turns into "Revoke? / Cancel". */
export function confirmButton(el, t, label, question, onYes) {
  const wrap = el("span", { className: "confirm-wrap" });
  const show = () => {
    const yes = el("button", { className: "btn btn--sm btn--danger", type: "button" }, label);
    const no = el("button", { className: "btn btn--sm btn--ghost", type: "button" }, t("account.cancel"));
    yes.addEventListener("click", onYes);
    no.addEventListener("click", reset);
    wrap.replaceChildren(el("span", { className: "muted confirm-q" }, question), yes, no);
  };
  const reset = () => {
    const b = el("button", { className: "btn btn--sm", type: "button" }, label);
    b.addEventListener("click", show);
    wrap.replaceChildren(b);
  };
  reset();
  return wrap;
}

export function render(root, ctx) {
  const { el, api, t } = ctx;
  const page = el("div", { className: "page account-page" },
    el("div", { className: "page-header" },
      el("div", {}, el("h1", { className: "page-title" }, t("account.title")),
        el("div", { className: "page-subtitle" }, t("account.sub")))));
  root.replaceChildren(page);

  const user = (ctx.session && ctx.session.user) || null;
  if (!user) {
    page.appendChild(el("div", { className: "empty-state" }, el("div", { className: "empty-state-title" }, t("common.loadFailed"))));
    return;
  }

  // profile
  const nameInput = el("input", { className: "input", value: user.display_name || "", maxlength: 120 });
  const profileMsg = el("span", { className: "muted form-msg" });
  const profile = el("form", { className: "card form-card" },
    el("h2", { className: "card-title" }, t("account.profile")),
    el("div", { className: "kv" },
      el("span", { className: "label" }, t("account.username")), el("span", { className: "mono" }, user.username),
      el("span", { className: "label" }, t("account.role")), el("span", {}, t("role." + user.role))),
    el("label", { className: "form-field" }, el("span", { className: "label" }, t("account.name")), nameInput),
    el("div", { className: "form-actions" }, el("button", { className: "btn btn--primary", type: "submit" }, t("account.save")), profileMsg));
  profile.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api.updateProfile(nameInput.value.trim());
      profileMsg.textContent = t("account.saved");
      window.dispatchEvent(new Event("assay:session-changed"));
    } catch (err) {
      profileMsg.textContent = err.message;
    }
  });

  // password
  const cur = el("input", { className: "input", type: "password", autocomplete: "current-password" });
  const nw = el("input", { className: "input", type: "password", autocomplete: "new-password" });
  const cf = el("input", { className: "input", type: "password", autocomplete: "new-password" });
  const pwMsg = el("span", { className: "muted form-msg" });
  const password = el("form", { className: "card form-card" },
    el("h2", { className: "card-title" }, t("account.password")),
    el("label", { className: "form-field" }, el("span", { className: "label" }, t("account.current")), cur),
    el("label", { className: "form-field" }, el("span", { className: "label" }, t("account.new")), nw),
    el("label", { className: "form-field" }, el("span", { className: "label" }, t("account.confirm")), cf),
    el("div", { className: "form-actions" }, el("button", { className: "btn", type: "submit" }, t("account.change")), pwMsg));
  password.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (nw.value !== cf.value) { pwMsg.textContent = t("account.mismatch"); return; }
    try {
      await api.changePassword(cur.value, nw.value);
      cur.value = nw.value = cf.value = "";
      pwMsg.textContent = t("account.changed");
    } catch (err) {
      pwMsg.textContent = err.message;
    }
  });

  // API keys
  const keyName = el("input", { className: "input", placeholder: t("account.keyName"), maxlength: 80 });
  const fresh = el("div");
  const list = el("div");
  const keys = el("div", { className: "card form-card" },
    el("h2", { className: "card-title" }, t("account.keys")),
    el("p", { className: "muted hint" }, t("account.keysHint")),
    el("form", { className: "inline-form", onSubmit: async (e) => {
      e.preventDefault();
      try {
        const res = await api.createKey(keyName.value.trim());
        keyName.value = "";
        fresh.replaceChildren(secretBox(el, t, res.token, t("account.copyOnce")));
        loadKeys();
      } catch (err) {
        fresh.replaceChildren(el("div", { className: "form-msg" }, err.message));
      }
    } }, keyName, el("button", { className: "btn btn--primary", type: "submit" }, t("account.newKey"))),
    fresh, list);

  async function loadKeys() {
    try {
      const res = await api.myKeys();
      const rows = res.keys || [];
      if (!rows.length) { list.replaceChildren(el("div", { className: "muted" }, t("account.noKeys"))); return; }
      list.replaceChildren(el("div", { className: "table-scroll" }, el("table", { className: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, t("account.keyName")), el("th", {}, "ID"),
          el("th", {}, t("account.created")), el("th", {}, t("account.lastUsed")), el("th", {}))),
        el("tbody", {}, rows.map((k) => el("tr", {},
          el("td", {}, k.name), el("td", { className: "mono" }, k.prefix + "…"),
          el("td", {}, when(k.created_at)), el("td", {}, k.last_used_at ? when(k.last_used_at) : t("account.never")),
          el("td", { className: "text-right" }, confirmButton(el, t, t("account.revoke"), t("account.revokeConfirm"), async () => {
            await api.revokeKey(k.id);
            loadKeys();
          }))))))));
    } catch (err) {
      list.replaceChildren(el("div", { className: "form-msg" }, err.message));
    }
  }

  page.appendChild(el("div", { className: "grid grid-2 account-grid" }, profile, password));
  page.appendChild(keys);
  loadKeys();
}
