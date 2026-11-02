// pages/users.js — admins: invitations (registration is invite-only) and user accounts.

import { registerDict } from "../i18n.js";
import { when, secretBox, confirmButton } from "./account.js";

registerDict({
  en: {
    "users.title": "Users",
    "users.sub": "People sign up with an invitation link. Roles, access and password resets are managed here.",
    "users.invites": "Invitations",
    "users.newInvite": "Create invitation",
    "users.role": "Role",
    "users.uses": "Uses",
    "users.days": "Valid for (days)",
    "users.label": "Note",
    "users.labelHint": "who it is for",
    "users.inviteOnce": "Send this link to the person. It is not shown again.",
    "users.noInvites": "No invitations.",
    "users.status": "Status",
    "users.expires": "Expires",
    "users.live": "Open",
    "users.usedUp": "Used",
    "users.expired": "Expired",
    "users.revoked": "Revoked",
    "users.revoke": "Revoke",
    "users.revokeQ": "Revoke this invitation?",
    "users.accounts": "Accounts",
    "users.user": "User",
    "users.keys": "API keys",
    "users.lastLogin": "Last sign-in",
    "users.active": "Active",
    "users.disabled": "Disabled",
    "users.disable": "Disable",
    "users.enable": "Enable",
    "users.disableQ": "Disable? They are signed out and their keys revoked.",
    "users.reset": "Reset link",
    "users.resetOnce": "One-time link to set a new password (valid 1 hour):",
    "users.delete": "Delete",
    "users.deleteQ": "Delete this account?",
    "users.you": "you",
  },
  zh: {
    "users.title": "用户",
    "users.sub": "新用户凭邀请链接注册。在这里管理角色、启用状态和密码重置。",
    "users.invites": "邀请",
    "users.newInvite": "创建邀请",
    "users.role": "角色",
    "users.uses": "可用次数",
    "users.days": "有效天数",
    "users.label": "备注",
    "users.labelHint": "发给谁",
    "users.inviteOnce": "把这个链接发给对方，之后不再显示。",
    "users.noInvites": "没有邀请。",
    "users.status": "状态",
    "users.expires": "过期时间",
    "users.live": "可用",
    "users.usedUp": "已用完",
    "users.expired": "已过期",
    "users.revoked": "已撤销",
    "users.revoke": "撤销",
    "users.revokeQ": "撤销这个邀请？",
    "users.accounts": "账户",
    "users.user": "用户",
    "users.keys": "API 密钥",
    "users.lastLogin": "最近登录",
    "users.active": "正常",
    "users.disabled": "已停用",
    "users.disable": "停用",
    "users.enable": "启用",
    "users.disableQ": "停用？对方会被退出登录，密钥全部撤销。",
    "users.reset": "重置链接",
    "users.resetOnce": "设置新密码的一次性链接（1 小时内有效）：",
    "users.delete": "删除",
    "users.deleteQ": "删除这个账户？",
    "users.you": "你",
  },
});

function inviteStatus(inv, t) {
  if (inv.revoked) return ["gray", t("users.revoked")];
  if (inv.uses >= inv.max_uses) return ["blue", t("users.usedUp")];
  if (inv.expired) return ["amber", t("users.expired")];
  return ["green", t("users.live")];
}

export function render(root, ctx) {
  const { el, api, t } = ctx;
  const me = (ctx.session && ctx.session.user) || {};
  const page = el("div", { className: "page users-page" },
    el("div", { className: "page-header" },
      el("div", {}, el("h1", { className: "page-title" }, t("users.title")),
        el("div", { className: "page-subtitle" }, t("users.sub")))));
  root.replaceChildren(page);

  const roleSelect = (value) => el("select", { className: "select" },
    ["researcher", "admin"].map((r) => el("option", { value: r, selected: r === value }, t("role." + r))));

  // invitations
  const invRole = roleSelect("researcher");
  const invUses = el("input", { className: "input input-num", type: "number", min: 1, max: 100, value: 1 });
  const invDays = el("input", { className: "input input-num", type: "number", min: 1, max: 90, value: 7 });
  const invLabel = el("input", { className: "input", placeholder: t("users.labelHint"), maxlength: 120 });
  const fresh = el("div");
  const invList = el("div");
  const invForm = el("form", { className: "inline-form wrap" },
    el("label", { className: "form-field" }, el("span", { className: "label" }, t("users.role")), invRole),
    el("label", { className: "form-field" }, el("span", { className: "label" }, t("users.uses")), invUses),
    el("label", { className: "form-field" }, el("span", { className: "label" }, t("users.days")), invDays),
    el("label", { className: "form-field grow" }, el("span", { className: "label" }, t("users.label")), invLabel),
    el("button", { className: "btn btn--primary", type: "submit" }, t("users.newInvite")));
  invForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const res = await api.createInvite({
        role: invRole.value, max_uses: Number(invUses.value) || 1,
        expires_in_days: Number(invDays.value) || 7, label: invLabel.value.trim(),
      });
      invLabel.value = "";
      fresh.replaceChildren(secretBox(el, t, location.origin + res.path, t("users.inviteOnce")));
      loadInvites();
    } catch (err) {
      fresh.replaceChildren(el("div", { className: "form-msg" }, err.message));
    }
  });
  page.appendChild(el("div", { className: "card form-card" },
    el("h2", { className: "card-title" }, t("users.invites")), invForm, fresh, invList));

  async function loadInvites() {
    try {
      const rows = (await api.invites()).invites || [];
      if (!rows.length) { invList.replaceChildren(el("div", { className: "muted" }, t("users.noInvites"))); return; }
      invList.replaceChildren(el("div", { className: "table-scroll" }, el("table", { className: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, t("users.label")), el("th", {}, t("users.role")), el("th", {}, t("users.uses")),
          el("th", {}, t("users.expires")), el("th", {}, t("users.status")), el("th", {}))),
        el("tbody", {}, rows.map((inv) => {
          const [tone, label] = inviteStatus(inv, t);
          return el("tr", {},
            el("td", {}, inv.label || el("span", { className: "mono muted" }, inv.prefix + "…")),
            el("td", {}, t("role." + inv.role)),
            el("td", { className: "num" }, `${inv.uses} / ${inv.max_uses}`),
            el("td", {}, when(inv.expires_at)),
            el("td", {}, el("span", { className: `badge badge--${tone}` }, label)),
            el("td", { className: "text-right" }, inv.live
              ? confirmButton(el, t, t("users.revoke"), t("users.revokeQ"), async () => { await api.revokeInvite(inv.id); loadInvites(); })
              : null));
        })))));
    } catch (err) {
      invList.replaceChildren(el("div", { className: "form-msg" }, err.message));
    }
  }

  // accounts
  const userList = el("div");
  const resetOut = el("div");
  page.appendChild(el("div", { className: "card form-card" },
    el("h2", { className: "card-title" }, t("users.accounts")), resetOut, userList));

  async function loadUsers() {
    try {
      const rows = (await api.users()).users || [];
      userList.replaceChildren(el("div", { className: "table-scroll" }, el("table", { className: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, t("users.user")), el("th", {}, t("users.role")), el("th", {}, t("users.status")),
          el("th", { className: "num" }, t("users.keys")), el("th", {}, t("users.lastLogin")), el("th", {}))),
        el("tbody", {}, rows.map((u) => {
          const self = u.id === me.id;
          const role = roleSelect(u.role);
          role.disabled = self;
          role.addEventListener("change", async () => {
            try { await api.updateUser(u.id, { role: role.value }); } catch (err) { role.value = u.role; resetOut.replaceChildren(el("div", { className: "form-msg" }, err.message)); }
          });
          const actions = el("div", { className: "row-actions" });
          if (!self) {
            actions.appendChild(el("button", { className: "btn btn--sm", type: "button", onClick: async () => {
              try {
                const res = await api.resetLink(u.id);
                resetOut.replaceChildren(secretBox(el, t, location.origin + res.path, `${u.username} — ${t("users.resetOnce")}`));
              } catch (err) {
                resetOut.replaceChildren(el("div", { className: "form-msg" }, err.message));
              }
            } }, t("users.reset")));
            actions.appendChild(u.disabled
              ? el("button", { className: "btn btn--sm", type: "button", onClick: async () => { await api.updateUser(u.id, { disabled: false }); loadUsers(); } }, t("users.enable"))
              : confirmButton(el, t, t("users.disable"), t("users.disableQ"), async () => { await api.updateUser(u.id, { disabled: true }); loadUsers(); }));
            actions.appendChild(confirmButton(el, t, t("users.delete"), t("users.deleteQ"), async () => {
              try { await api.deleteUser(u.id); } catch (err) { resetOut.replaceChildren(el("div", { className: "form-msg" }, err.message)); }
              loadUsers();
            }));
          }
          return el("tr", {},
            el("td", {}, el("div", { className: "user-cell" },
              el("span", { className: "avatar" }, (u.display_name || u.username).slice(0, 1).toUpperCase()),
              el("div", {}, el("div", {}, u.display_name || u.username, self ? el("span", { className: "muted" }, ` (${t("users.you")})`) : null),
                el("div", { className: "mono muted small" }, u.username)))),
            el("td", {}, role),
            el("td", {}, el("span", { className: `badge badge--${u.disabled ? "gray" : "green"}` }, t(u.disabled ? "users.disabled" : "users.active"))),
            el("td", { className: "num" }, String(u.keys || 0)),
            el("td", {}, when(u.last_login_at)),
            el("td", { className: "text-right" }, actions));
        })))));
    } catch (err) {
      userList.replaceChildren(el("div", { className: "form-msg" }, err.message));
    }
  }

  loadInvites();
  loadUsers();
}
