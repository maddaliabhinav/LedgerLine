/* LedgerLine web UI. Vanilla JS, no build step, no innerHTML for data (memos are user input). */
"use strict";

/* ------------------------------------------------------------------ utils */
const NS = "http://www.w3.org/2000/svg";

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}
function set(el, ...children) { el.replaceChildren(); return append(el, children); }
function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
function s(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}
const $ = (sel, root = document) => root.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function newKey() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  const b = new Uint8Array(16); crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
function money(mv) {
  if (!mv) return "—";
  const neg = mv.paise < 0;
  return (neg ? "−" : "") + "₹" + mv.display.replace(/^-?Rs /, "");
}
function when(iso) {
  try { return new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }); }
  catch { return iso; }
}
function rupee(text) { return String(text || "").replace(/Rs /g, "₹"); }
function short(hash, n = 10) { return hash ? hash.slice(0, n) + "…" : ""; }
function gcd(a, b) { return b ? gcd(b, a % b) : a; }

const store = {
  get(key) { try { return JSON.parse(sessionStorage.getItem("ll." + key)); } catch { return null; } },
  set(key, value) { try { sessionStorage.setItem("ll." + key, JSON.stringify(value)); } catch { /* private mode */ } },
  clear() { try { sessionStorage.removeItem("ll.token"); sessionStorage.removeItem("ll.user"); } catch { /* ignore */ } },
};
const state = { token: store.get("token"), user: store.get("user") };

/* ------------------------------------------------------------------ api */
async function api(method, path, body, opts = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = "Bearer " + state.token;
  if (opts.key) headers["Idempotency-Key"] = opts.key;
  const retriable = method === "GET" || Boolean(opts.key);    // same key => retry can never pay twice
  let res = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (err) {
      if (!retriable || attempt === 2) return { ok: false, status: 0, data: { error: { code: "NETWORK", message: "Network error. Nothing is lost: retrying with the same request key is safe." } }, headers: new Headers() };
      await sleep(400 * (attempt + 1));
      continue;
    }
    const busy = res.status === 503 || (res.status === 409 && res.headers.get("Retry-After"));
    if (busy && retriable && attempt < 2) { await sleep(600 * (attempt + 1)); continue; }
    break;
  }
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (res.status === 401 && !opts.anonymous) {
    signOut("Your session ended. Sign in again.");
  }
  return { ok: res.ok, status: res.status, data, headers: res.headers };
}
function errorText(r) {
  return (r.data && r.data.error && r.data.error.message) || `Request failed (${r.status})`;
}

/* ------------------------------------------------------------------ toasts & drawer */
function toast(message, bad = false) {
  const t = h("div", { class: "toast" + (bad ? " bad" : ""), role: bad ? "alert" : "status" }, message);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 4200);
}

function closeDrawer() { $("#drawer-root").replaceChildren(); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

async function openEntry(seq) {
  const root = $("#drawer-root");
  const panel = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": `Journal entry ${seq}` },
    h("p", { class: "muted" }, "Loading entry…"));
  set(root, h("div", { class: "scrim", onclick: closeDrawer }), panel);
  const r = await api("GET", `/api/entries/${seq}`);
  if (!r.ok) { set(panel, h("p", {}, errorText(r))); return; }
  const e = r.data;
  const v = e.verification;
  const party = (p) => (p ? `${p.account_no} (${p.owner})` : "—");
  set(panel, 
    h("div", { class: "spread" },
      h("h2", {}, `Entry #${e.seq}: ${e.kind.replaceAll("_", " ").toLowerCase()}`),
      h("button", { class: "btn secondary small", onclick: closeDrawer }, "Close")),
    h("p", { class: "row" },
      statusMark(e.status, e.reason_code),
      v.hash_matches_content && v.signature_valid
        ? h("span", { class: "mark ok" }, "Hash and signature verified")
        : h("span", { class: "mark bad" }, "Verification failed")),
    h("dl", { class: "kv" },
      h("dt", {}, "When"), h("dd", {}, when(e.created_at)),
      h("dt", {}, "Initiated by"), h("dd", {}, e.actor ? `${e.actor.name} (${e.actor.role})` : "System"),
      h("dt", {}, "Amount"), h("dd", { class: "num" }, money(e.amount)),
      h("dt", {}, "Account"), h("dd", {}, party(e.account)),
      h("dt", {}, "Counterparty"), h("dd", {}, party(e.counterparty)),
      e.memo ? [h("dt", {}, "Memo"), h("dd", {}, e.memo)] : null,
      e.reason_code ? [h("dt", {}, "Refusal reason"), h("dd", {}, e.reason_code)] : null,
      e.ref_seq ? [h("dt", {}, "Refers to"), h("dd", {}, h("button", { class: "btn link", onclick: () => openEntry(e.ref_seq) }, `entry #${e.ref_seq}`))] : null,
      e.idempotency_key ? [h("dt", {}, "Request key"), h("dd", { class: "hash" }, e.idempotency_key)] : null,
      h("dt", {}, "Hash"), h("dd", { class: "hash" }, e.hash),
      h("dt", {}, "Previous hash"), h("dd", { class: "hash" }, e.prev_hash),
      h("dt", {}, "Signature"), h("dd", { class: "hash" }, e.signature),
      h("dt", {}, "Signing key"), h("dd", { class: "hash" }, e.key_id)),
    e.details ? [h("h3", {}, "Recorded details"), h("pre", { class: "json" }, JSON.stringify(e.details, null, 2))] : null,
    h("p", { class: "small muted" }, "The hash covers every field above plus the hash of the entry before it, so this entry pins the whole history up to it. The signature is made with a key that is not stored in the database."),
  );
  $("button", panel)?.focus();
}

function statusMark(status, reason) {
  if (status === "refused") return h("span", { class: "stamp", title: reason || "" }, "Refused");
  if (status === "held") return h("span", { class: "mark held" }, "Held for review");
  if (status === "info") return h("span", { class: "mark info" }, "Record");
  return h("span", { class: "mark ok" }, "Posted");
}

/* ------------------------------------------------------------------ the seal */
function seal(hash, seq, { ok = true, draw = false } = {}) {
  hash = hash && /^[0-9a-f]{64}$/.test(hash) ? hash : "0".repeat(64);
  const bytes = hash.match(/../g).map((x) => parseInt(x, 16));
  const svg = s("svg", { viewBox: "0 0 100 100", role: "img",
    "aria-label": `Ledger seal: entry ${seq}, head hash ${hash.slice(0, 12)}, ${ok ? "all checks passed" : "a check failed"}` });
  svg.classList.add("seal");
  if (!ok) svg.classList.add("fail");
  if (draw) svg.classList.add("draw");

  // A hypotrochoid (guilloche rosette) whose shape is chosen by the head hash:
  // when the ledger changes, the seal changes.
  const R = 120;
  const r = [26, 34, 38, 46, 52, 56][bytes[0] % 6];
  const d = r * (0.6 + (bytes[1] / 255) * 0.9);
  const turns = r / gcd(R, r);
  const steps = 160 * turns;
  const scale = 33 / (R - r + d);
  const rot = (bytes[2] / 255) * Math.PI * 2;
  let path = "";
  for (let i = 0; i <= steps; i++) {
    const t = (i / steps) * 2 * Math.PI * turns;
    const x = (R - r) * Math.cos(t) + d * Math.cos(((R - r) / r) * t);
    const y = (R - r) * Math.sin(t) - d * Math.sin(((R - r) / r) * t);
    const xr = x * Math.cos(rot) - y * Math.sin(rot);
    const yr = x * Math.sin(rot) + y * Math.cos(rot);
    path += (i ? "L" : "M") + (50 + xr * scale).toFixed(2) + " " + (50 + yr * scale).toFixed(2);
  }
  const id = "ring" + Math.random().toString(36).slice(2, 8);
  svg.append(
    s("circle", { cx: 50, cy: 50, r: 48, class: "seal-ring" }),
    s("circle", { cx: 50, cy: 50, r: 40.5, class: "seal-ring", "stroke-width": "0.4" }),
    Object.assign(s("path", { d: path, class: "seal-path" })),
    s("circle", { cx: 50, cy: 50, r: 12.5, class: "seal-disc" }),
    s("path", { id, d: "M50 6.2 a43.8 43.8 0 1 1 -0.01 0", fill: "none" }),
  );
  const ring = s("text", { class: "seal-text" });
  const tp = s("textPath", { href: "#" + id });
  tp.textContent = `ledgerline   entry ${seq}   ${hash.slice(0, 44)}`;
  ring.append(tp);
  const center = s("text", { x: 50, y: 52.5, class: "seal-center" });
  center.textContent = ok ? "✓" : "✗";
  const sub = s("text", { x: 50, y: 59, class: "seal-sub" });
  sub.textContent = `#${seq}`;
  svg.append(ring, center, sub);
  return svg;
}

function scoreDial(score, threshold) {
  const svg = s("svg", { viewBox: "0 0 92 92", class: "score", role: "img", "aria-label": `Risk score ${score} of 100, hold threshold ${threshold}` });
  const C = 2 * Math.PI * 38;
  svg.append(
    s("circle", { cx: 46, cy: 46, r: 38, class: "dial-track" }),
    s("circle", { cx: 46, cy: 46, r: 38, class: "dial-bar" + (score >= threshold ? " held" : ""),
      "stroke-dasharray": `${(C * score) / 100} ${C}`, transform: "rotate(-90 46 46)" }),
  );
  const t = s("text", { x: 46, y: 53, class: "dial-num" });
  t.textContent = score;
  svg.append(t);
  return svg;
}

/* ------------------------------------------------------------------ shell & router */
const NAV = {
  holder: [["#/accounts", "Accounts"], ["#/move", "Move money"], ["#/race", "Race lab"], ["#/details", "My details"]],
  admin: [["#/overview", "Overview"], ["#/reviews", "Reviews"], ["#/all-accounts", "Accounts"], ["#/users", "Users"], ["#/journal", "Journal"], ["#/proof", "Proof"], ["#/flags", "Flags"]],
  auditor: [["#/overview", "Overview"], ["#/proof", "Proof"], ["#/journal", "Journal"], ["#/flags", "Flags"], ["#/duplicates", "Duplicates"], ["#/reviews", "Reviews"], ["#/all-accounts", "Accounts"], ["#/users", "Users"]],
};
const HOME = { holder: "#/accounts", admin: "#/overview", auditor: "#/overview" };

const ROUTES = {
  accounts: { roles: ["holder"], render: pageAccounts },
  move: { roles: ["holder"], render: pageMove },
  race: { roles: ["holder"], render: pageRace },
  statement: { roles: ["holder", "admin", "auditor"], render: pageStatement },
  overview: { roles: ["admin", "auditor"], render: pageOverview },
  reviews: { roles: ["admin", "auditor"], render: pageReviews },
  "all-accounts": { roles: ["admin", "auditor"], render: pageAllAccounts },
  users: { roles: ["admin", "auditor"], render: pageUsers },
  journal: { roles: ["admin", "auditor"], render: pageJournal },
  proof: { roles: ["admin", "auditor"], render: pageProof },
  flags: { roles: ["admin", "auditor"], render: pageFlags },
  duplicates: { roles: ["admin", "auditor"], render: pageDuplicates },
  details: { roles: ["holder", "admin", "auditor"], render: pageMyDetails },
  password: { roles: ["holder", "admin", "auditor"], render: pageChangePassword },
};

function renderShell(active) {
  const bar = $("#topbar");
  bar.classList.remove("hidden");
  const sealBtn = h("button", { class: "seal-mini", title: "Ledger proof", onclick: () => { location.hash = state.user.role === "holder" ? "#/accounts" : "#/proof"; } });
  set(bar, h("div", { class: "topbar-inner" },
    h("a", { class: "brand", href: HOME[state.user.role] },
      h("img", { src: "/static/img/logo.png", alt: "LedgerLine", width: 581, height: 128 })),
    h("nav", { class: "nav", "aria-label": "Main" },
      NAV[state.user.role].map(([href, label]) => h("a", { href, "aria-current": href === "#/" + active ? "page" : null }, label))),
    h("div", { class: "who" },
      sealBtn,
      h("a", { class: "who-name", href: "#/details", title: "My details" },
        h("div", {}, state.user.name), h("div", { class: "role" }, state.user.main_admin ? "main admin" : state.user.role)),
      h("button", { class: "btn secondary small", onclick: () => logout() }, "Sign out"))));
  api("GET", "/api/health", undefined, { anonymous: true }).then((r) => {
    if (r.ok) set(sealBtn, seal(r.data.ledger_head.hash, r.data.ledger_head.seq));
  });
}

async function route() {
  closeDrawer();
  const [name, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  if (!state.token || !state.user) { $("#topbar").classList.add("hidden"); return pageLogin(); }
  const target = ROUTES[name];
  if (!target || !target.roles.includes(state.user.role)) { location.hash = HOME[state.user.role]; return; }
  renderShell(name);
  const main = $("#app");
  set(main, h("div", { class: "page" }, h("p", { class: "muted" }, "Loading…")));
  try {
    await target.render(main, rest.map(decodeURIComponent));
  } catch (err) {
    console.error(err);
    set(main, h("div", { class: "page" }, h("p", {}, "Something went wrong loading this page. Reload to try again.")));
  }
  main.focus({ preventScroll: true });
}
async function start() {
  if (state.token) {                       // the server, not the browser's saved copy, says who this is
    const me = await api("GET", "/api/me");
    if (me.status === 401) return;         // signOut() has already shown the sign-in page
    if (me.ok) { state.user = me.data; store.set("user", state.user); }
  }
  route();
}
window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", start);

function page(main, title, lede, ...actions) {
  const wrap = h("div", { class: "page" },
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, title), lede ? h("p", { class: "muted" }, lede) : null),
      actions.length ? h("div", { class: "row" }, actions) : null));
  set(main, wrap);
  return wrap;
}

function signOut(message) {
  state.token = null; state.user = null; store.clear();
  if (message) toast(message, true);
  location.hash = "#/login";
  route();
}
async function logout() {
  await api("POST", "/api/auth/logout");
  signOut();
}

/* ------------------------------------------------------------------ sign in / create user */
function signedIn(data) {
  state.token = data.access_token; state.user = data.user;
  store.set("token", state.token); store.set("user", state.user);
  location.hash = HOME[state.user.role];
}

const SIGN_IN_ANSWERS = {
  APPROVAL_PENDING: "Your auditor request is waiting for an administrator. You can sign in once it is approved.",
  ACCESS_DECLINED: "An administrator declined your auditor request. Ask them if you think this is a mistake.",
};

function signInForm(notice) {
  const err = h("p", { class: "form-error", role: "alert" });
  const user = h("input", { id: "u", name: "username", autocomplete: "username", required: true, value: notice ? notice.username : null });
  const pass = h("input", { id: "p", name: "password", type: "password", autocomplete: "current-password", required: true });
  const button = h("button", { class: "btn", type: "submit" }, "Sign in");
  return h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      button.disabled = true; err.textContent = "";
      const r = await api("POST", "/api/auth/login", { username: user.value, password: pass.value }, { anonymous: true });
      button.disabled = false;
      if (!r.ok) {
        const code = r.data && r.data.error ? r.data.error.code : null;
        err.classList.toggle("waiting", code === "APPROVAL_PENDING");
        err.textContent = SIGN_IN_ANSWERS[code] || errorText(r);
        return;
      }
      signedIn(r.data);
    },
  },
    h("h2", { class: "sr-only" }, "Sign in"),
    notice ? h("div", { class: "auth-notice", role: "status" }, notice.text) : null,
    h("div", { class: "field" }, h("label", { for: "u" }, "Username"), user),
    h("div", { class: "field" }, h("label", { for: "p" }, "Password"), pass),
    err, button);
}

const ACCOUNT_TYPES = [
  { role: "holder", title: "Account holder", text: "Send and receive money. Ready straight away, with an empty ₹0 account." },
  { role: "auditor", title: "Auditor", text: "Read-only view of every account and the full journal, once an administrator approves it." },
];

function createUserForm(onRequested) {
  const err = h("p", { class: "form-error", role: "alert" });
  const user = h("input", { id: "c-user", name: "username", autocomplete: "username", required: true, minlength: 3, maxlength: 32, spellcheck: "false" });
  const name = h("input", { id: "c-name", name: "name", autocomplete: "name", required: true, maxlength: 80 });
  const pass = h("input", { id: "c-pass", name: "password", type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const again = h("input", { id: "c-again", name: "confirm", type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const button = h("button", { class: "btn", type: "submit" }, "Create user");
  const note = h("p", { class: "auth-note" });
  const radios = ACCOUNT_TYPES.map((t) => h("input", { type: "radio", name: "c-role", id: `c-role-${t.role}`, value: t.role, checked: t.role === "holder" }));
  const choices = ACCOUNT_TYPES.map((t, i) => h("label", { class: "choice", for: `c-role-${t.role}` }, radios[i],
    h("span", { class: "choice-title" }, t.title), h("span", { class: "choice-text" }, t.text)));
  const role = () => radios.find((r) => r.checked).value;
  const refresh = () => {
    choices.forEach((c, i) => c.classList.toggle("selected", radios[i].checked));
    const auditor = role() === "auditor";
    button.textContent = auditor ? "Request auditor access" : "Create user";
    note.textContent = auditor
      ? "This sends a request to the administrators. No account exists until one of them approves it; then you sign in with the password you choose here."
      : "Your first account opens with ₹0.00. Money arrives when an administrator records a deposit.";
  };
  for (const r of radios) r.addEventListener("change", refresh);
  refresh();
  return h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      if (pass.value !== again.value) { err.textContent = "The two passwords are different."; again.focus(); return; }
      button.disabled = true;
      const r = await api("POST", "/api/auth/register",
        { username: user.value.trim().toLowerCase(), display_name: name.value.trim(), password: pass.value, role: role() }, { anonymous: true });
      button.disabled = false;
      if (!r.ok) { err.textContent = errorText(r); return; }
      if (r.status === 202) { onRequested(r.data.request); return; }
      toast(`Welcome, ${r.data.user.name}. Your account ${r.data.account.account_no} is open with ₹0.00.`);
      signedIn(r.data);
    },
  },
    h("h2", { class: "sr-only" }, "Create user"),
    h("fieldset", { class: "choices" }, h("legend", {}, "Account type"), choices),
    h("div", { class: "field" }, h("label", { for: "c-user" }, "Username"), user,
      h("span", { class: "hint" }, "3 to 32 characters: lowercase letters, digits, . _ -")),
    h("div", { class: "field" }, h("label", { for: "c-name" }, "Full name"), name),
    h("div", { class: "field" }, h("label", { for: "c-pass" }, "Password"), pass,
      h("span", { class: "hint" }, "At least 10 characters.")),
    h("div", { class: "field" }, h("label", { for: "c-again" }, "Confirm password"), again),
    err, button, note);
}

async function pageLogin() {
  const panel = h("div");
  const tabs = {};
  const show = (which, notice) => {
    for (const [k, b] of Object.entries(tabs)) b.setAttribute("aria-pressed", String(k === which));
    set(panel, which === "signin" ? signInForm(notice) : createUserForm((req) => show("signin", {
      username: req.username,
      text: `Request sent. An administrator has to approve auditor access for ${req.username}; after that, sign in here with the password you chose.`,
    })));
    $(notice ? "#p" : "input", panel)?.focus();
  };
  tabs.signin = h("button", { type: "button", class: "auth-tab", onclick: () => show("signin") }, "Sign in");
  tabs.create = h("button", { type: "button", class: "auth-tab", onclick: () => show("create") }, "Create user");

  const sealBox = h("div", { class: "hero-seal" }, h("p", {}, "Reading the ledger…"));
  set($("#app"), h("div", { class: "login" },
    h("section", { class: "login-hero" },
      h("a", { class: "brand", href: "#/" },
        h("img", { class: "hero-logo", src: "/static/img/logo-full.png", alt: "LedgerLine: secure transactions, auditable ledger, real-time", width: 1200, height: 263 })),
      h("div", {},
        h("h1", {}, "Move money. Prove every move."),
        h("p", { class: "lede" }, "Balances the database itself enforces, requests that pay exactly once however often they arrive, and a signed journal anyone can check.")),
      sealBox),
    h("section", { class: "login-form" },
      h("div", { class: "auth" },
        h("div", { class: "auth-tabs", role: "group", "aria-label": "Sign in or create a user" }, tabs.signin, tabs.create),
        panel))));
  show("signin");
  const r = await api("GET", "/api/health", undefined, { anonymous: true });
  if (r.ok) {
    const head = r.data.ledger_head;
    set(sealBox, seal(head.hash, head.seq),
      h("p", {}, `This seal is drawn from the ledger's latest hash, entry ${head.seq}. Change any past entry and the seal changes.`));
  }
}

/* ------------------------------------------------------------------ holder: accounts */
async function myAccounts() {
  const r = await api("GET", "/api/accounts");
  return r.ok ? r.data.accounts : [];
}

async function pageAccounts(main) {
  const wrap = page(main, "Your accounts", "Balances are read straight from the ledger.");
  const [list, mine] = await Promise.all([myAccounts(), api("GET", "/api/me/details")]);
  const reminder = mine.ok ? DETAILS_REMINDER[mine.data.status] : null;
  if (reminder) wrap.append(h("p", { class: "auth-notice" }, reminder, " ", h("a", { href: "#/details" }, "Open my details")));
  if (!list.length) { wrap.append(h("p", { class: "empty" }, "You have no accounts yet. An administrator opens accounts for you.")); return; }
  wrap.append(h("div", { class: "accounts" }, list.map((a) =>
    h("article", { class: "acct" + (a.status !== "active" ? " frozen" : "") },
      h("div", { class: "spread" }, h("h2", {}, a.name), a.status !== "active" ? h("span", { class: "mark bad" }, a.status) : null),
      h("div", { class: "no" }, a.account_no),
      h("div", { class: "balance wide-num" }, money(a.balance)),
      h("div", { class: "meta" },
        h("span", {}, "Available ", h("strong", { class: "num" }, money(a.available))),
        a.pending_review && a.pending_review.paise ? h("span", {}, "Under review ", h("strong", { class: "num" }, money(a.pending_review))) : null,
        h("span", {}, "Daily limit ", h("span", { class: "num" }, money(a.daily_limit)))),
      h("div", { class: "row" },
        h("a", { class: "btn small", href: `#/move/${a.account_no}` }, "Send money"),
        h("a", { class: "btn secondary small", href: `#/statement/${a.account_no}` }, "Statement"))))));
}

/* ------------------------------------------------------------------ holder: move money */
function requestKeyLine(form) {
  const line = h("p", { class: "reqkey" });
  form.renewKey = () => {
    form.key = newKey();
    set(line, "Request key ", h("code", {}, form.key.slice(0, 13) + "…"), ". If this is sent twice, it pays once.");
  };
  form.renewKey();
  return line;
}

function resultBox() { return h("div", { class: "result", "aria-live": "polite" }); }

function showOutcome(box, r, verb) {
  const d = r.data || {};
  if (r.ok && d.status === "posted") {
    const bal = d.balances ? Object.entries(d.balances).map(([no, mv]) => `${no} now ${money(mv)}`).join(", ") : "";
    set(box, h("p", { class: "verdict ok" }, `${verb}. Entry #${d.entry.seq}. ${bal}`),
      h("button", { class: "btn link small", onclick: () => openEntry(d.entry.seq) }, "View signed receipt"),
      r.headers.get("Idempotent-Replayed") ? h("p", { class: "small muted" }, "This was a repeat of an earlier request: nothing moved twice.") : null);
  } else if (r.ok && d.status === "held") {
    set(box, h("p", { class: "verdict" }, h("span", { class: "mark held" }, "Held for review"), " ", d.review ? d.review.message || "" : ""),
      h("button", { class: "btn link small", onclick: () => openEntry(d.entry.seq) }, "View signed receipt"));
  } else {
    set(box, h("p", { class: "verdict bad" }, h("span", { class: "stamp" }, "Refused"), " ", errorText(r)),
      d.entry ? h("button", { class: "btn link small", onclick: () => openEntry(d.entry.seq) }, "View the recorded refusal") : null);
  }
}

async function pageMove(main, [fromNo]) {
  const wrap = page(main, "Move money", "Every request carries a key. Repeat it, double-click it, lose the connection mid-way: it moves money once.");
  const accounts = (await myAccounts()).filter((a) => a.status === "active");
  if (!accounts.length) { wrap.append(h("p", { class: "empty" }, "You have no active accounts.")); return; }
  const options = () => accounts.map((a) => h("option", { value: a.account_no, selected: a.account_no === fromNo }, `${a.name} (${a.account_no}), ${money(a.available)} available`));
  const refresh = async (...selects) => {          // keep "available" honest after money moves
    const fresh = (await myAccounts()).filter((a) => a.status === "active");
    for (const sel of selects) {
      const keep = sel.value;
      set(sel, fresh.map((a) => h("option", { value: a.account_no, selected: a.account_no === keep }, `${a.name} (${a.account_no}), ${money(a.available)} available`)));
    }
  };

  // transfer
  const tFrom = h("select", { id: "t-from" }, options());
  const tTo = h("input", { id: "t-to", placeholder: "LL-1003", autocomplete: "off", spellcheck: "false" });
  const payee = h("p", { class: "payee", id: "t-payee" });
  const tAmt = h("input", { id: "t-amt", class: "amount", inputmode: "decimal", placeholder: "0.00", autocomplete: "off" });
  const tMemo = h("input", { id: "t-memo", maxlength: 140, placeholder: "What is it for?" });
  const tBtn = h("button", { class: "btn", type: "submit" }, "Send money");
  const tBox = resultBox();
  const lookup = async () => {
    const no = tTo.value.trim().toUpperCase();
    if (!/^LL-\d{4,8}$/.test(no)) { payee.className = "payee"; payee.textContent = no ? "Account numbers look like LL-1003." : ""; return; }
    const r = await api("GET", `/api/payees/${no}`);
    if (r.ok) { payee.className = "payee ok"; payee.textContent = `Pays ${r.data.owner_name}, ${r.data.account_name}${r.data.status !== "active" ? ` (${r.data.status})` : ""}`; }
    else { payee.className = "payee bad"; payee.textContent = "No customer account with that number."; }
  };
  tTo.addEventListener("change", lookup);
  tTo.addEventListener("blur", lookup);
  const tForm = h("form", { class: "panel", onsubmit: async (e) => {
    e.preventDefault();
    tBtn.disabled = true;
    const r = await api("POST", "/api/transfers", { from_account: tFrom.value, to_account: tTo.value.trim().toUpperCase(), amount: tAmt.value.trim(), memo: tMemo.value || null }, { key: tForm.key });
    tBtn.disabled = false;
    showOutcome(tBox, r, "Sent");
    if (r.status !== 0) { tForm.renewKey(); if (r.ok) { tAmt.value = ""; tMemo.value = ""; refresh(tFrom, wFrom); } }
  } },
    h("h2", {}, "Send to another account"),
    h("p", { class: "muted small" }, "To any active LedgerLine account. Large or unusual payments wait for a person to approve them."),
    h("div", { class: "field" }, h("label", { for: "t-from" }, "From"), tFrom),
    h("div", { class: "field" }, h("label", { for: "t-to" }, "To account"), tTo, payee),
    h("div", { class: "field" }, h("label", { for: "t-amt" }, "Amount in rupees"), tAmt),
    h("div", { class: "field" }, h("label", { for: "t-memo" }, "Memo (optional)"), tMemo),
    tBtn);
  tForm.append(requestKeyLine(tForm), tBox);

  // withdrawal
  const wFrom = h("select", { id: "w-from" }, options());
  const wAmt = h("input", { id: "w-amt", class: "amount", inputmode: "decimal", placeholder: "0.00", autocomplete: "off" });
  const wMemo = h("input", { id: "w-memo", maxlength: 140, placeholder: "Optional" });
  const wBtn = h("button", { class: "btn", type: "submit" }, "Withdraw");
  const wBox = resultBox();
  const wForm = h("form", { class: "panel", onsubmit: async (e) => {
    e.preventDefault();
    wBtn.disabled = true;
    const r = await api("POST", "/api/withdrawals", { account: wFrom.value, amount: wAmt.value.trim(), memo: wMemo.value || null }, { key: wForm.key });
    wBtn.disabled = false;
    showOutcome(wBox, r, "Withdrawn");
    if (r.status !== 0) { wForm.renewKey(); if (r.ok) { wAmt.value = ""; wMemo.value = ""; refresh(tFrom, wFrom); } }
  } },
    h("h2", {}, "Withdraw to your bank"),
    h("p", { class: "muted small" }, "Money leaves LedgerLine for the outside world."),
    h("div", { class: "field" }, h("label", { for: "w-from" }, "From"), wFrom),
    h("div", { class: "field" }, h("label", { for: "w-amt" }, "Amount in rupees"), wAmt),
    h("div", { class: "field" }, h("label", { for: "w-memo" }, "Memo (optional)"), wMemo),
    wBtn);
  wForm.append(requestKeyLine(wForm), wBox);
  wrap.append(h("div", { class: "grid-2" }, tForm, wForm));
}

/* ------------------------------------------------------------------ statements */
async function pageStatement(main, [accountNo]) {
  if (!accountNo) { location.hash = HOME[state.user.role]; return; }
  let before = null;
  const r = await api("GET", `/api/accounts/${accountNo}/statement?limit=100`);
  if (!r.ok) { page(main, "Statement", errorText(r)); return; }
  const acct = r.data.account;
  const actions = [h("button", { class: "btn secondary", onclick: () => exportCsv(acct, rows) }, "Download CSV")];
  if (state.user.role === "admin" && acct.kind === "customer") {
    actions.push(h("button", { class: "btn secondary", onclick: () => changeStatus(acct) }, acct.status === "frozen" ? "Unfreeze" : "Freeze"));
  }
  const wrap = page(main, `${acct.name}`, `${acct.account_no}${acct.owner ? `, owned by ${acct.owner.name}` : ""}. Every debit, credit and refused attempt, with the balance after each.`, ...actions);
  wrap.append(h("div", { class: "row" },
    h("div", { class: "panel" }, h("div", { class: "small muted" }, "Balance"), h("div", { class: "wide-num balance" }, money(acct.balance))),
    acct.available ? h("div", { class: "panel" }, h("div", { class: "small muted" }, "Available"), h("div", { class: "wide-num" }, money(acct.available))) : null,
    acct.pending_review && acct.pending_review.paise ? h("div", { class: "panel" }, h("div", { class: "small muted" }, "Under review"), h("div", { class: "wide-num" }, money(acct.pending_review))) : null,
    acct.overdraft_limit && acct.overdraft_limit.paise ? h("div", { class: "panel" }, h("div", { class: "small muted" }, "Authorised overdraft"), h("div", { class: "wide-num" }, money(acct.overdraft_limit))) : null));
  const rows = r.data.rows;
  const body = h("tbody");
  const table = h("div", { class: "table-wrap" }, h("table", {},
    h("caption", { class: "sr-only" }, `Statement for ${acct.account_no}`),
    h("thead", {}, h("tr", {}, h("th", {}, "Entry"), h("th", {}, "When"), h("th", {}, "Details"), h("th", {}, "Initiated by"),
      h("th", { class: "money" }, "Debit"), h("th", { class: "money" }, "Credit"), h("th", { class: "money" }, "Balance"), h("th", {}, "Status"))),
    body));
  const more = h("button", { class: "btn secondary", onclick: async () => {
    const next = await api("GET", `/api/accounts/${accountNo}/statement?limit=100&before_seq=${before}`);
    if (next.ok) { rows.push(...next.data.rows); fill(next.data.rows); before = next.data.next_before_seq; more.classList.toggle("hidden", !before); }
  } }, "Load older entries");
  const fill = (list) => {
    for (const row of list) {
      const holdState = row.hold ? row.hold.state : null;
      const status = row.status === "refused" ? statusMark("refused", row.reason_code)
        : holdState === "pending" ? h("span", { class: "mark held" }, "Under review")
        : holdState === "approved" ? h("span", { class: "mark ok" }, "Approved")
        : holdState === "rejected" ? h("span", { class: "mark info" }, "Rejected")
        : statusMark("posted");
      body.append(h("tr", { class: "clickable" + (row.status === "refused" ? " refused" : ""), tabindex: 0,
        onclick: () => openEntry(row.seq), onkeydown: (e) => { if (e.key === "Enter") openEntry(row.seq); } },
        h("td", { class: "num" }, `#${row.seq}`),
        h("td", {}, when(row.at)),
        h("td", { class: "desc" }, row.description, row.memo ? h("small", {}, row.memo) : null),
        h("td", {}, row.initiated_by ? row.initiated_by.name : "System"),
        h("td", { class: "money" }, row.direction === "debit" ? money(row.amount) : ""),
        h("td", { class: "money" }, row.direction === "credit" ? money(row.amount) : ""),
        h("td", { class: "money balance" }, money(row.balance_after)),
        h("td", {}, status)));
    }
  };
  if (!rows.length) body.append(h("tr", {}, h("td", { colspan: 8, class: "empty" }, "Nothing has happened on this account yet.")));
  fill(rows);
  before = r.data.next_before_seq;
  more.classList.toggle("hidden", !before);
  wrap.append(h("div", { class: "stack" }, table, more));
}

function exportCsv(acct, rows) {
  const esc = (v) => `"${String(v ?? "").replaceAll('"', '""')}"`;
  const lines = [["entry", "when", "details", "memo", "initiated_by", "debit", "credit", "balance", "status", "reason", "hash"].join(",")];
  for (const r of rows) {
    lines.push([r.seq, r.at, r.description, r.memo, r.initiated_by?.username, r.direction === "debit" ? r.amount.rupees : "",
      r.direction === "credit" ? r.amount.rupees : "", r.balance_after?.rupees, r.status, r.reason_code, r.hash].map(esc).join(","));
  }
  download(`${acct.account_no}-statement.csv`, new Blob([lines.join("\n")], { type: "text/csv" }));
}
function download(name, blob) {
  const a = h("a", { href: URL.createObjectURL(blob), download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function changeStatus(acct) {
  const action = acct.status === "frozen" ? "unfreeze" : "freeze";
  const reason = prompt(`Reason to ${action} ${acct.account_no} (recorded in the journal):`);
  if (!reason) return;
  const r = await api("POST", `/api/accounts/${acct.account_no}/${action}`, { reason });
  toast(r.ok ? `${acct.account_no} is now ${r.data.status}.` : errorText(r), !r.ok);
  route();
}

/* ------------------------------------------------------------------ holder: race lab */
async function pageRace(main) {
  const wrap = page(main, "Race lab", "Fire many requests at the same instant, the way an attacker or a flaky network would. These are real transfers, so use your own accounts.");
  const accounts = (await myAccounts()).filter((a) => a.status === "active");
  if (!accounts.length) { wrap.append(h("p", { class: "empty" }, "You need an active account.")); return; }
  const other = accounts[1] ? accounts[1].account_no : "";

  // experiment 1: overdraw race
  const from = h("select", { id: "r-from" }, accounts.map((a) => h("option", { value: a.account_no }, `${a.account_no}, ${money(a.available)}`)));
  const to = h("input", { id: "r-to", value: other, placeholder: "LL-1002" });
  const each = h("input", { id: "r-each", class: "amount", inputmode: "decimal", value: "" });
  const count = h("input", { id: "r-count", type: "number", min: 2, max: 30, value: 10 });
  const out1 = h("div", { "aria-live": "polite" });
  const suggest = () => {
    const a = accounts.find((x) => x.account_no === from.value);
    if (a) each.value = ((a.available.paise / 100) * 0.35).toFixed(2);
  };
  from.addEventListener("change", suggest);
  suggest();
  const fire = h("button", { class: "btn", type: "submit" }, "Fire them all at once");
  const f1 = h("form", { class: "panel", onsubmit: async (e) => {
    e.preventDefault();
    const n = Math.max(2, Math.min(30, parseInt(count.value, 10) || 10));
    fire.disabled = true;
    set(out1, h("p", { class: "muted" }, `Sending ${n} transfers at the same instant…`));
    const before = (await api("GET", `/api/accounts/${from.value}`)).data;
    const body = { from_account: from.value, to_account: to.value.trim().toUpperCase(), amount: each.value.trim(), memo: "Race lab" };
    const results = await Promise.all(Array.from({ length: n }, () => api("POST", "/api/transfers", body, { key: newKey() })));
    const after = (await api("GET", `/api/accounts/${from.value}`)).data;
    fire.disabled = false;
    const posted = results.filter((r) => r.data && r.data.status === "posted").length;
    const held = results.filter((r) => r.data && r.data.status === "held").length;
    const refused = results.filter((r) => r.data && r.data.status === "refused");
    const insufficient = refused.filter((r) => r.data.error && r.data.error.code === "INSUFFICIENT_FUNDS").length;
    const other = results.length - posted - held - refused.length;
    const floorOk = after.balance.paise >= -after.overdraft_limit.paise;
    set(out1, 
      h("div", { class: "result-grid" },
        cell(n, "sent together"), cell(posted, "paid"), cell(held, "held for review"), cell(insufficient, "refused: no money left"),
        other ? cell(other, "other answers") : null),
      h("p", {}, "Balance before ", h("strong", { class: "num" }, money(before.balance)), ", after ", h("strong", { class: "num" }, money(after.balance)), "."),
      h("p", { class: "verdict " + (floorOk ? "ok" : "bad") }, floorOk
        ? `The balance never went below its floor. Each request checked the balance while holding a lock on the account row, so later requests saw the money already spent.`
        : "The balance is below its floor. This should be impossible: report it."),
      h("a", { href: `#/statement/${from.value}`, class: "btn secondary small" }, "See them on the statement"));
  } },
    h("h2", {}, "Overdraw race"),
    h("p", { class: "muted small" }, "Many transfers that together ask for more than the account holds. At most the affordable ones can succeed."),
    h("div", { class: "field" }, h("label", { for: "r-from" }, "From"), from),
    h("div", { class: "field" }, h("label", { for: "r-to" }, "To account"), to),
    h("div", { class: "field" }, h("label", { for: "r-each" }, "Amount per transfer (rupees)"), each),
    h("div", { class: "field" }, h("label", { for: "r-count" }, "How many at once"), count),
    fire, out1);

  // experiment 2: replay storm (one key, many copies)
  const from2 = h("select", { id: "s-from" }, accounts.map((a) => h("option", { value: a.account_no }, a.account_no)));
  const to2 = h("input", { id: "s-to", value: other, placeholder: "LL-1002" });
  const amt2 = h("input", { id: "s-amt", class: "amount", inputmode: "decimal", value: "100.00" });
  const count2 = h("input", { id: "s-count", type: "number", min: 2, max: 30, value: 12 });
  const out2 = h("div", { "aria-live": "polite" });
  const fire2 = h("button", { class: "btn", type: "submit" }, "Send one request many times");
  const f2 = h("form", { class: "panel", onsubmit: async (e) => {
    e.preventDefault();
    const n = Math.max(2, Math.min(30, parseInt(count2.value, 10) || 12));
    const key = newKey();
    fire2.disabled = true;
    const body = { from_account: from2.value, to_account: to2.value.trim().toUpperCase(), amount: amt2.value.trim(), memo: "Replay storm" };
    const results = await Promise.all(Array.from({ length: n }, () => api("POST", "/api/transfers", body, { key })));
    fire2.disabled = false;
    const seqs = new Set(results.filter((r) => r.data && r.data.entry).map((r) => r.data.entry.seq));
    const replayed = results.filter((r) => r.headers.get("Idempotent-Replayed")).length;
    const bodies = new Set(results.map((r) => JSON.stringify(r.data)));
    const once = seqs.size === 1 && bodies.size === 1;
    set(out2, 
      h("div", { class: "result-grid" }, cell(n, "copies sent"), cell(seqs.size, "journal entries"), cell(replayed, "answered from storage")),
      h("p", {}, "Request key ", h("code", { class: "hash" }, key)),
      h("p", { class: "verdict " + (once ? "ok" : "bad") }, once
        ? `Money moved once (entry #${[...seqs][0]}). Every copy got the identical answer; the duplicates are listed for the auditor.`
        : "Copies got different answers. This should be impossible: report it."),
      seqs.size === 1 ? h("button", { class: "btn link small", onclick: () => openEntry([...seqs][0]) }, "View the single entry") : null);
  } },
    h("h2", {}, "Replay storm"),
    h("p", { class: "muted small" }, "The same request, with the same key, sent many times at once: a double-click or an over-eager retry."),
    h("div", { class: "field" }, h("label", { for: "s-from" }, "From"), from2),
    h("div", { class: "field" }, h("label", { for: "s-to" }, "To account"), to2),
    h("div", { class: "field" }, h("label", { for: "s-amt" }, "Amount (rupees)"), amt2),
    h("div", { class: "field" }, h("label", { for: "s-count" }, "Copies"), count2),
    fire2, out2);
  wrap.append(h("div", { class: "grid-2" }, f1, f2));
}
function cell(v, k) { return h("div", { class: "cell" }, h("div", { class: "v wide-num" }, String(v)), h("div", { class: "k" }, k)); }

/* ------------------------------------------------------------------ admin / auditor: overview */
async function pageOverview(main) {
  const wrap = page(main, "Overview", "The state of the books right now.");
  const isAdmin = state.user.role === "admin";
  const [proof, reviews, asks] = await Promise.all([api("GET", "/api/audit/proof"), api("GET", "/api/reviews?state=pending"),
    isAdmin ? api("GET", "/api/access-requests") : null]);
  if (!proof.ok) { wrap.append(h("p", {}, errorText(proof))); return; }
  const p = proof.data;
  const failed = p.checks.filter((c) => !c.ok);
  const pending = reviews.ok ? reviews.data.reviews.length : 0;
  const asking = asks && asks.ok ? asks.data.requests.length : 0;
  wrap.append(
    h("section", { class: "panel proof-top" },
      seal(p.head.hash, p.head.seq, { ok: p.ok }),
      h("div", {},
        h("h2", {}, p.ok ? `All ${p.checks.length} checks pass over ${p.entries} entries` : `${failed.length} of ${p.checks.length} checks failed`),
        h("p", { class: "muted" }, rupee(p.checks.find((c) => c.id === "conservation").summary)),
        h("div", { class: "totals" },
          total(money(p.totals.inside_ledgerline), "money inside LedgerLine"),
          total(money(p.totals.under_review), `under review in ${p.totals.pending_reviews} payment(s)`),
          total(String(p.counts.by_status.refused || 0), "refused attempts on record"),
          total(String(p.counts.replays.replayed || 0), "duplicate requests answered from storage")),
        h("div", { class: "row actions" },
          h("a", { class: "btn", href: "#/reviews" }, pending ? `Review ${pending} held payment${pending > 1 ? "s" : ""}` : "No payments waiting"),
          asking ? h("a", { class: "btn secondary", href: "#/users" }, "Auditor requests", h("span", { class: "count" }, String(asking))) : null,
          h("a", { class: "btn secondary", href: "#/proof" }, "See every check"),
          isAdmin ? h("a", { class: "btn secondary", href: "#/users" }, "Create user") : null))));
}
function total(v, k) { return h("div", { class: "total" }, h("div", { class: "v wide-num" }, v), h("div", { class: "k" }, k)); }

/* ------------------------------------------------------------------ reviews */
async function pageReviews(main, [which]) {
  const view = which === "decided" ? "decided" : "pending";
  const wrap = page(main, "Held payments", "Sentinel flags; a person decides. The flag, any AI note and the decision all go into the journal.",
    h("a", { class: "btn secondary" + (view === "pending" ? "" : ""), href: "#/reviews", "aria-current": view === "pending" ? "page" : null }, "Waiting"),
    h("a", { class: "btn secondary", href: "#/reviews/decided", "aria-current": view === "decided" ? "page" : null }, "Decided"));
  const r = await api("GET", `/api/reviews?state=${view}`);
  if (!r.ok) { wrap.append(h("p", {}, errorText(r))); return; }
  if (!r.data.reviews.length) { wrap.append(h("p", { class: "empty" }, view === "pending" ? "Nothing is waiting for review." : "No decisions yet.")); return; }
  wrap.append(h("div", { class: "stack" }, r.data.reviews.map(reviewCard)));
}

function reviewCard(item) {
  const risk = item.risk || { score: 0, threshold: 60, reasons: [] };
  const noteBox = h("div");
  const showNote = (n) => set(noteBox, h("div", { class: "ai-note" }, rupee(n.text),
    h("div", { class: "src" }, n.source === "llm"
      ? `Written by ${n.model}; every number in it was checked against the recorded facts.`
      : n.rejected_numbers && n.rejected_numbers.length
        ? `The AI's text was rejected because it stated numbers that are not in the facts (${n.rejected_numbers.join(", ")}). This is the fixed template instead.`
        : "Fixed template (no AI provider configured).")));
  if (item.ai_note) showNote(item.ai_note);
  const askBtn = h("button", { class: "btn secondary small", onclick: async () => {
    askBtn.disabled = true;
    const r = await api("POST", `/api/reviews/${item.seq}/ai-note`);
    askBtn.disabled = false;
    if (r.ok) showNote(r.data); else toast(errorText(r), true);
  } }, item.ai_note ? "Refresh AI note" : "Ask AI for a note");

  const decided = item.state.state !== "pending";
  let decide = null;
  if (!decided && state.user.role === "admin") {
    const note = h("input", { maxlength: 140, placeholder: "What did you check? (recorded)", "aria-label": "Decision note" });
    const keyFor = { approve: newKey(), reject: newKey() };
    const act = async (decision) => {
      for (const b of decide.querySelectorAll("button")) b.disabled = true;
      const r = await api("POST", `/api/reviews/${item.seq}/${decision}`, { note: note.value || null }, { key: keyFor[decision] });
      if (r.ok && r.data.status === "posted") { toast(decision === "approve" ? `Approved. Entry #${r.data.entry.seq} paid it.` : `Rejected. Entry #${r.data.entry.seq} returned the money.`); route(); }
      else { toast(errorText(r), true); for (const b of decide.querySelectorAll("button")) b.disabled = false; }
    };
    decide = h("div", { class: "decide" }, note, h("div", { class: "row" },
      h("button", { class: "btn", onclick: () => act("approve") }, "Approve and pay"),
      h("button", { class: "btn danger", onclick: () => act("reject") }, "Reject and return")));
  }
  return h("article", { class: "panel review" },
    scoreDial(risk.score, risk.threshold),
    h("div", {},
      h("div", { class: "spread" },
        h("h2", { class: "num" }, `${money(item.amount)} from ${item.account.account_no} to ${item.destination ? item.destination.account_no : "outside"}`),
        decided ? h("span", { class: "mark " + (item.state.state === "approved" ? "ok" : "info") }, item.state.state) : h("span", { class: "mark held" }, "waiting")),
      h("p", { class: "muted small" }, `${item.intended_kind === "WITHDRAWAL" ? "Withdrawal" : "Transfer"} requested by ${item.actor ? item.actor.name : "?"} on ${when(item.created_at)}`,
        item.destination ? `, payee ${item.destination.owner}` : "", item.memo ? `. Memo: “${item.memo}”` : "."),
      h("ul", { class: "reasons" }, risk.reasons.map((x) => h("li", {}, h("span", { class: "pts" }, `+${x.points}`), h("span", {}, rupee(x.text))))),
      noteBox,
      h("div", { class: "row actions" }, askBtn, h("button", { class: "btn link small", onclick: () => openEntry(item.seq) }, `Entry #${item.seq}`),
        item.state.decided_by_seq ? h("button", { class: "btn link small", onclick: () => openEntry(item.state.decided_by_seq) }, `Decision #${item.state.decided_by_seq}`) : null),
      decide));
}

/* ------------------------------------------------------------------ all accounts / users */
async function pageAllAccounts(main) {
  const isAdmin = state.user.role === "admin";
  const wrap = page(main, "All accounts", isAdmin ? "Open accounts, record deposits, freeze, and set overdrafts. Every change is journaled." : "Read-only view of every account.");
  const [r, users] = await Promise.all([api("GET", "/api/accounts"), api("GET", "/api/users")]);
  if (!r.ok) { wrap.append(h("p", {}, errorText(r))); return; }
  const customers = r.data.accounts.filter((a) => a.kind === "customer");
  if (isAdmin) {
    const holders = users.ok ? users.data.users.filter((u) => u.role === "holder" && u.active) : [];
    // deposit
    const dAcct = h("select", { id: "d-acct" }, customers.filter((a) => a.status === "active").map((a) => h("option", { value: a.account_no }, `${a.account_no}, ${a.owner ? a.owner.name : ""}`)));
    const dAmt = h("input", { id: "d-amt", class: "amount", inputmode: "decimal", placeholder: "0.00" });
    const dMemo = h("input", { id: "d-memo", maxlength: 140, placeholder: "e.g. NEFT ref 0042" });
    const dBox = resultBox();
    const dBtn = h("button", { class: "btn", type: "submit" }, "Record deposit");
    const dForm = h("form", { class: "panel", onsubmit: async (e) => {
      e.preventDefault(); dBtn.disabled = true;
      const res = await api("POST", "/api/deposits", { account: dAcct.value, amount: dAmt.value.trim(), memo: dMemo.value || null }, { key: dForm.key });
      dBtn.disabled = false; showOutcome(dBox, res, "Deposited");
      if (res.status !== 0) { dForm.renewKey(); if (res.ok) { dAmt.value = ""; dMemo.value = ""; } }
    } }, h("h2", {}, "Record a deposit"), h("p", { class: "muted small" }, "Money that arrived from outside LedgerLine."),
      h("div", { class: "field" }, h("label", { for: "d-acct" }, "Into account"), dAcct),
      h("div", { class: "field" }, h("label", { for: "d-amt" }, "Amount in rupees"), dAmt),
      h("div", { class: "field" }, h("label", { for: "d-memo" }, "Reference"), dMemo), dBtn);
    dForm.append(requestKeyLine(dForm), dBox);
    // open account
    const oOwner = h("select", { id: "o-owner" }, holders.map((u) => h("option", { value: u.username }, `${u.name} (${u.username})`)));
    const oName = h("input", { id: "o-name", maxlength: 60, placeholder: "e.g. Payroll pool" });
    const oForm = h("form", { class: "panel", onsubmit: async (e) => {
      e.preventDefault();
      const res = await api("POST", "/api/accounts", { owner_username: oOwner.value, name: oName.value });
      toast(res.ok ? `Opened ${res.data.account_no}.` : errorText(res), !res.ok);
      if (res.ok) route();
    } }, h("h2", {}, "Open an account"), h("p", { class: "muted small" }, "Accounts always open with a zero balance."),
      h("div", { class: "field" }, h("label", { for: "o-owner" }, "Holder"), oOwner),
      h("div", { class: "field" }, h("label", { for: "o-name" }, "Account name"), oName),
      h("button", { class: "btn", type: "submit" }, "Open account"));
    wrap.append(h("div", { class: "grid-2" }, dForm, oForm), h("div", { class: "page-head" }));
  }
  const body = h("tbody");
  for (const a of r.data.accounts) {
    body.append(h("tr", {},
      h("td", {}, h("a", { href: `#/statement/${a.account_no}` }, a.account_no)),
      h("td", {}, a.owner ? a.owner.name : "System"),
      h("td", {}, a.name),
      h("td", {}, a.status === "active" ? h("span", { class: "mark ok" }, "active") : h("span", { class: "mark bad" }, a.status)),
      h("td", { class: "money" }, money(a.balance)),
      h("td", { class: "money" }, a.overdraft_limit && a.overdraft_limit.paise ? money(a.overdraft_limit) : "—"),
      h("td", {}, isAdmin && a.kind === "customer" ? h("div", { class: "row" },
        h("button", { class: "btn secondary small", onclick: () => changeStatus(a) }, a.status === "frozen" ? "Unfreeze" : "Freeze"),
        h("button", { class: "btn secondary small", onclick: () => setOverdraft(a) }, "Overdraft")) : null)));
  }
  wrap.append(h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Account"), h("th", {}, "Owner"), h("th", {}, "Name"), h("th", {}, "Status"),
      h("th", { class: "money" }, "Balance"), h("th", { class: "money" }, "Overdraft"), h("th", {}, isAdmin ? "Actions" : ""))), body)));
}

async function setOverdraft(acct) {
  const limit = prompt(`Authorised overdraft for ${acct.account_no} in rupees (0 to 50000):`, acct.overdraft_limit ? acct.overdraft_limit.rupees : "0");
  if (limit === null) return;
  const reason = prompt("Reason (recorded in the journal):");
  if (!reason) return;
  const r = await api("POST", `/api/accounts/${acct.account_no}/overdraft`, { limit: limit.trim(), reason });
  toast(r.ok ? `Overdraft for ${acct.account_no} set to ${money(r.data.overdraft_limit)}.` : errorText(r), !r.ok);
  route();
}

async function pageUsers(main) {
  const isAdmin = state.user.role === "admin";
  const isMain = isAdmin && Boolean(state.user.main_admin);
  const wrap = page(main, "People", "Who can sign in, and what they can do.");
  const [r, waiting, decided] = await Promise.all([api("GET", "/api/users"), api("GET", "/api/access-requests"),
    api("GET", "/api/access-requests?state=decided")]);
  if (!r.ok) { wrap.append(h("p", {}, errorText(r))); return; }
  wrap.append(accessRequests(waiting.ok ? waiting.data.requests : [], decided.ok ? decided.data.requests : [], isAdmin),
    h("div", { class: "page-head" }));
  if (isAdmin) wrap.append(detailsQueue(r.data.users.filter((p) => p.details === "waiting")), h("div", { class: "page-head" }));
  if (isAdmin) {
    const u = h("input", { id: "n-user", placeholder: "username" });
    const n = h("input", { id: "n-name", placeholder: "Full name" });
    const roles = isMain ? ["holder", "auditor", "admin"] : ["holder", "auditor"];
    const role = h("select", { id: "n-role" }, roles.map((x) => h("option", { value: x }, x)));
    const pw = h("input", { id: "n-pw", type: "password", autocomplete: "new-password", placeholder: "At least 10 characters" });
    wrap.append(h("form", { class: "panel", onsubmit: async (e) => {
      e.preventDefault();
      const res = await api("POST", "/api/users", { username: u.value, display_name: n.value, role: role.value, password: pw.value });
      toast(res.ok ? `Created ${res.data.username}.` : errorText(res), !res.ok);
      if (res.ok) route();
    } }, h("h2", {}, "Create a user"),
      h("div", { class: "grid-2" },
        h("div", { class: "field" }, h("label", { for: "n-user" }, "Username"), u),
        h("div", { class: "field" }, h("label", { for: "n-name" }, "Name"), n),
        h("div", { class: "field" }, h("label", { for: "n-role" }, "Role"), role),
        h("div", { class: "field" }, h("label", { for: "n-pw" }, "Password"), pw)),
      h("p", { class: "muted form-note" }, isMain
        ? "Administrators you create do the daily work: deposits, approvals, reviews and auditor requests. Only you can create administrators or remove their access."
        : "Administrators are created by the main administrator."),
      h("button", { class: "btn", type: "submit" }, "Create user")), h("div", { class: "page-head" }));
  }
  const body = h("tbody");
  for (const p of r.data.users) {
    body.append(h("tr", {}, h("td", {}, p.username), h("td", {}, p.name),
      h("td", {}, p.role, p.main_admin ? h("span", { class: "mark info role-tag" }, "main admin") : null),
      h("td", { class: "num" }, String(p.accounts)),
      h("td", {}, p.active ? h("span", { class: "mark ok" }, "active") : h("span", { class: "mark bad" }, "access removed")),
      h("td", {}, detailsMark(p.details),
        isAdmin && p.details !== "none" ? h("button", { class: "btn link small details-view", onclick: () => openDetails(p.username) }, "View") : null),
      h("td", {}, isAdmin ? accessAction(p, isMain) : null)));
  }
  wrap.append(h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Username", "Name", "Role", "Accounts", "Status", "Personal details", ""].map((x) => h("th", {}, x)))), body)));
  if (!isAdmin) wrap.append(h("p", { class: "small muted" }, "Personal details are shown only to the person and to administrators. Auditors see whether they are verified."));
}

/* ------------------------------------------------------------------ personal details */
const DETAILS_STATUS = {
  none: ["info", "not given"], waiting: ["held", "waiting"], verified: ["ok", "verified"], returned: ["bad", "sent back"],
};
const DETAILS_REMINDER = {
  none: "Add your personal details (date of birth, mobile, email, address). An administrator checks them, as a bank would.",
  returned: "An administrator asked you to correct your personal details.",
};
const DETAILS_LABELS = [["date_of_birth", "Date of birth"], ["mobile", "Mobile"], ["email", "Email"],
  ["address_line1", "Address line 1"], ["address_line2", "Address line 2"], ["city", "City"], ["state", "State"],
  ["pin_code", "PIN code"], ["occupation", "Occupation"], ["nominee_name", "Nominee"], ["nominee_relation", "Nominee's relation"]];
const OCCUPATION_LABEL = { salaried: "Salaried", self_employed: "Self-employed", business: "Business owner", student: "Student", homemaker: "Homemaker", retired: "Retired", other: "Other" };
const RELATION_LABEL = { spouse: "Spouse", father: "Father", mother: "Mother", son: "Son", daughter: "Daughter", brother: "Brother", sister: "Sister", other: "Other" };

function detailsMark(status) {
  const [cls, text] = DETAILS_STATUS[status] || DETAILS_STATUS.none;
  return h("span", { class: "mark " + cls }, text);
}
function detailValue(key, value) {
  if (value === null || value === undefined || value === "") return "—";
  if (key === "occupation") return OCCUPATION_LABEL[value] || value;
  if (key === "nominee_relation") return RELATION_LABEL[value] || value;
  if (key === "date_of_birth") return new Date(value + "T00:00:00").toLocaleDateString("en-IN", { dateStyle: "long" });
  return value;
}
function detailsStatusLine(v, own) {
  if (v.status === "none") return own ? "You have not given your personal details yet." : "No personal details given yet.";
  if (v.status === "waiting") return `Version ${v.version}, saved ${when(v.submitted_at)}, is waiting for an administrator to check it.`;
  if (v.status === "verified") return `Version ${v.version} was verified by ${v.decided_by.name} on ${when(v.decided_at)}.`;
  return `${v.decided_by.name} sent version ${v.version} back on ${when(v.decided_at)}: “${v.return_note}”`;
}

async function pageMyDetails(main) {
  const wrap = page(main, "My details", "The basic details a bank keeps about you. No ID numbers.",
    h("a", { class: "btn secondary small", href: "#/password" }, "Change password"));
  const r = await api("GET", "/api/me/details");
  if (!r.ok) { wrap.append(h("p", {}, errorText(r))); return; }
  const v = r.data, c = v.choices, d = v.details || {};
  const status = h("section", { class: "panel details-status" },
    h("div", { class: "spread" }, h("h2", {}, "Status"), detailsMark(v.status)),
    h("p", {}, detailsStatusLine(v, true)));
  const input = (name, attrs = {}) => h("input", { id: `d-${name}`, name, value: d[name] || "", ...attrs });
  const select = (name, options, blank) => h("select", { id: `d-${name}`, name },
    blank ? h("option", { value: "" }, blank) : null,
    options.map((o) => h("option", { value: o.value, selected: d[name] === o.value }, o.label)));
  const fields = {
    date_of_birth: input("date_of_birth", { type: "date", required: true }),
    mobile: input("mobile", { type: "tel", inputmode: "numeric", autocomplete: "tel", placeholder: "10 digits, e.g. 98765 43210", required: true }),
    email: input("email", { type: "email", autocomplete: "email", placeholder: "name@example.com", required: true }),
    address_line1: input("address_line1", { autocomplete: "address-line1", maxlength: 120, placeholder: "House / flat, street", required: true }),
    address_line2: input("address_line2", { autocomplete: "address-line2", maxlength: 120, placeholder: "Area, landmark (optional)" }),
    city: input("city", { autocomplete: "address-level2", maxlength: 60, required: true }),
    state: select("state", c.states.map((x) => ({ value: x, label: x })), "Choose a state"),
    pin_code: input("pin_code", { inputmode: "numeric", autocomplete: "postal-code", maxlength: 6, placeholder: "6 digits", required: true }),
    occupation: select("occupation", c.occupations, "Choose one"),
    nominee_name: input("nominee_name", { maxlength: 80, placeholder: "Optional" }),
    nominee_relation: select("nominee_relation", c.relations, "—"),
  };
  const errors = {};
  const field = (name, label) => {
    errors[name] = h("p", { class: "field-error", role: "alert" });
    return h("div", { class: "field" }, h("label", { for: `d-${name}` }, label), fields[name], errors[name]);
  };
  const formError = h("p", { class: "form-error", role: "alert" });
  const form = h("form", { class: "panel", novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    Object.values(errors).forEach((x) => set(x)); set(formError);
    const body = Object.fromEntries(Object.entries(fields).map(([k, el]) => [k, el.value.trim() === "" ? null : el.value.trim()]));
    const res = await api("PUT", "/api/me/details", body);
    if (!res.ok) {
      const list = (res.data && res.data.error && res.data.error.fields) || [];
      list.forEach((f) => { if (errors[f.field]) set(errors[f.field], f.message); });
      if (!list.some((f) => errors[f.field])) set(formError, errorText(res));
      return;
    }
    toast(res.data.version === v.version ? "Nothing changed." : `Saved. Version ${res.data.version} is waiting for an administrator to check it.`);
    route();
  } },
    h("h2", {}, v.status === "none" ? "Add your details" : "Your details"),
    h("div", { class: "grid-2" }, field("date_of_birth", "Date of birth"), field("mobile", "Mobile number"),
      field("email", "Email"), field("occupation", "Occupation")),
    h("h3", {}, "Address"),
    h("div", { class: "grid-2" }, field("address_line1", "Address line 1"), field("address_line2", "Address line 2"),
      field("city", "City"), field("state", "State or union territory"), field("pin_code", "PIN code")),
    h("h3", {}, "Nominee (optional)"),
    h("div", { class: "grid-2" }, field("nominee_name", "Nominee's name"), field("nominee_relation", "Relation to you")),
    formError,
    h("p", { class: "muted form-note" }, "Only you can change these. Any change goes back to an administrator to check. Your details are shown only to you and to administrators, and they are never written into the permanent journal: it records only that they changed, and who checked them."),
    h("button", { class: "btn", type: "submit" }, v.status === "none" ? "Save and send for checking" : "Save changes"));
  wrap.append(status, h("div", { class: "page-head" }), form);
}

async function pageChangePassword(main) {
  const wrap = page(main, "Change password", "You need your current password. Changing it signs you out on every other device; you stay signed in here.");
  const box = (id, autocomplete, label, hint) => {
    const input = h("input", { id, type: "password", autocomplete, required: true, maxlength: 256 });
    const error = h("p", { class: "field-error", role: "alert" });
    return { input, error, el: h("div", { class: "field" }, h("label", { for: id }, label), input, hint ? h("p", { class: "hint" }, hint) : null, error) };
  };
  const current = box("pw-current", "current-password", "Current password");
  const fresh = box("pw-new", "new-password", "New password", "At least 10 characters. Not the one you have now.");
  const again = box("pw-again", "new-password", "Re-enter new password");
  const formError = h("p", { class: "form-error", role: "alert" });
  const fieldFor = { current_password: current, new_password: fresh, confirm_password: again };
  const submit = h("button", { class: "btn", type: "submit" }, "Change password");
  wrap.append(h("form", { class: "panel narrow", novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    [current, fresh, again].forEach((b) => set(b.error)); set(formError);
    if (!current.input.value) { set(current.error, "Enter your current password."); current.input.focus(); return; }
    if (fresh.input.value.length < 10) { set(fresh.error, "Use at least 10 characters."); fresh.input.focus(); return; }
    if (fresh.input.value !== again.input.value) { set(again.error, "The two new passwords are not the same."); again.input.focus(); return; }
    submit.disabled = true;
    const r = await api("POST", "/api/me/password", { current_password: current.input.value, new_password: fresh.input.value, confirm_password: again.input.value });
    submit.disabled = false;
    if (r.ok) {
      state.token = r.data.access_token; state.user = r.data.user;              // this session continues
      store.set("token", state.token); store.set("user", state.user);
      toast("Password changed. You were signed out everywhere else.");
      location.hash = "#/details";
      return;
    }
    const code = r.data && r.data.error && r.data.error.code;
    if (code === "WRONG_PASSWORD") { set(current.error, "That is not your current password."); current.input.select(); return; }
    if (code === "SAME_PASSWORD") { set(fresh.error, "Choose a new password, not the one you have now."); fresh.input.select(); return; }
    const list = (r.data && r.data.error && r.data.error.fields) || [];
    list.forEach((f) => { if (fieldFor[f.field]) set(fieldFor[f.field].error, f.message); });
    if (!list.some((f) => fieldFor[f.field])) set(formError, errorText(r));
  } }, current.el, fresh.el, again.el, formError, submit));
  current.input.focus();
}

function detailsQueue(waiting) {
  return h("section", { class: "panel" },
    h("div", { class: "spread" }, h("h2", {}, "Personal details to check"),
      waiting.length ? h("span", { class: "mark held" }, `${waiting.length} waiting`) : null),
    h("p", { class: "muted small" }, "Check each person's details against their documents, then verify them or send them back with a note. Nobody checks their own details, and each decision is a signed journal entry."),
    waiting.length ? h("div", { class: "requests" }, waiting.map((p) => h("div", { class: "request" },
      h("div", { class: "spread" },
        h("div", {}, h("strong", {}, p.name), h("div", { class: "small muted" }, `${p.username}, version ${p.details_version}`)),
        p.username === state.user.username ? h("span", { class: "muted small-note" }, "your own: another admin checks it")
          : h("button", { class: "btn small", onclick: () => openDetails(p.username) }, "Check details")))))
      : h("p", { class: "empty-line" }, "No details waiting."));
}

async function openDetails(username) {
  const root = $("#drawer-root");
  const panel = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": `Personal details of ${username}` },
    h("p", { class: "muted" }, "Loading…"));
  set(root, h("div", { class: "scrim", onclick: closeDrawer }), panel);
  const r = await api("GET", `/api/users/${encodeURIComponent(username)}/details`);
  if (!r.ok) { set(panel, h("p", {}, errorText(r))); return; }
  const v = r.data;
  const own = v.username === state.user.username;
  const note = h("input", { maxlength: 200, placeholder: "What needs correcting? (shown to them)", "aria-label": "Note for sending back" });
  const decide = async (verb) => {
    if (verb === "return" && !note.value.trim()) { toast("Write a note saying what needs correcting.", true); note.focus(); return; }
    const res = await api("POST", `/api/users/${encodeURIComponent(username)}/details/${verb}`,
      verb === "verify" ? { version: v.version } : { version: v.version, note: note.value.trim() });
    toast(res.ok ? (verb === "verify" ? `Verified ${v.name}'s details (version ${v.version}).` : `Sent ${v.name}'s details back.`) : errorText(res), !res.ok);
    if (res.ok) { closeDrawer(); route(); }
  };
  set(panel,
    h("div", { class: "spread" }, h("h2", {}, v.name), h("button", { class: "btn secondary small", onclick: closeDrawer }, "Close")),
    h("p", { class: "row" }, detailsMark(v.status), h("span", { class: "small muted" }, `${v.username}, version ${v.version}`)),
    h("p", { class: "small" }, detailsStatusLine(v, false)),
    v.details ? h("dl", { class: "kv" }, DETAILS_LABELS.map(([k, label]) => [h("dt", {}, label), h("dd", {}, detailValue(k, v.details[k]))])) : null,
    v.status === "waiting" && !own ? h("div", { class: "details-actions" },
      h("button", { class: "btn", onclick: () => decide("verify") }, `Verify version ${v.version}`),
      h("div", { class: "row request-actions" }, note, h("button", { class: "btn danger small", onclick: () => decide("return") }, "Send back"))) : null,
    own && v.status === "waiting" ? h("p", { class: "small muted" }, "These are your own details. Another administrator checks them.") : null,
    h("p", { class: "small muted" }, "Personal details are never written into the journal. The journal records that they changed, and who verified or sent back which version."));
  $("button", panel)?.focus();
}

// Who may remove or restore whose access. The server and the database enforce the
// same rules; this only avoids offering buttons that would be refused.
function accessAction(p, isMain) {
  if (p.username === state.user.username) return null;                          // never yourself
  if (p.main_admin) return h("span", { class: "muted small-note" }, "protected");   // nobody removes the main admin
  if (p.role === "admin" && !isMain) return h("span", { class: "muted small-note" }, "main admin only");
  return h("button", { class: "btn secondary small", onclick: async () => {
    if (p.active && !confirm(`Remove ${p.name}'s access? They are signed out at once and cannot sign in. Nothing is deleted: their history stays in the journal, and access can be restored.`)) return;
    const res = await api("POST", `/api/users/${encodeURIComponent(p.username)}/${p.active ? "deactivate" : "activate"}`);
    toast(res.ok ? `${p.username} ${res.data.active ? "can sign in again" : "can no longer sign in"}.` : errorText(res), !res.ok);
    route();
  } }, p.active ? "Remove access" : "Restore access");
}

function accessRequests(waiting, decided, isAdmin) {
  const history = decided.length ? h("details", { class: "history" },
    h("summary", {}, `Decided requests (${decided.length})`),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Name", "Username", "Decision", "By", "When", "Entry"].map((x) => h("th", {}, x)))),
      h("tbody", {}, decided.slice(0, 25).map((q) => h("tr", {},
        h("td", {}, q.name), h("td", {}, q.username),
        h("td", {}, h("span", { class: "mark " + (q.status === "approved" ? "ok" : "bad") }, q.status)),
        h("td", {}, q.decided_by ? q.decided_by.name : "—"), h("td", {}, when(q.decided_at)),
        h("td", {}, h("button", { class: "btn link small", onclick: () => openEntry(q.decision_seq) }, `#${q.decision_seq}`)))))))) : null;
  return h("section", { class: "panel" },
    h("div", { class: "spread" }, h("h2", {}, "Auditor requests"),
      waiting.length ? h("span", { class: "mark held" }, `${waiting.length} waiting`) : null),
    h("p", { class: "muted small" }, isAdmin
      ? "People who asked for auditor access when they signed up. Nobody can sign in with a request until you approve it, and your decision is a signed journal entry."
      : "People who asked for auditor access when they signed up. Only an administrator can decide, and every decision is a signed journal entry."),
    waiting.length ? h("div", { class: "requests" }, waiting.map((q) => requestRow(q, isAdmin))) : h("p", { class: "empty-line" }, "No requests waiting."),
    history);
}

function requestRow(q, isAdmin) {
  let actions = null;
  if (isAdmin) {
    const note = h("input", { maxlength: 140, placeholder: "What did you check? (recorded)", "aria-label": `Decision note for ${q.username}` });
    const approve = h("button", { class: "btn small" }, "Approve");
    const decline = h("button", { class: "btn danger small" }, "Decline");
    const act = async (verb) => {
      approve.disabled = decline.disabled = true;
      const r = await api("POST", `/api/access-requests/${q.id}/${verb}`, { note: note.value.trim() || null });
      if (r.ok) {
        toast(verb === "approve" ? `Approved. ${q.username} can now sign in as an auditor (entry #${r.data.entry.seq}).`
          : `Declined. ${q.username} cannot sign in (entry #${r.data.entry.seq}).`);
        route();
      } else { toast(errorText(r), true); approve.disabled = decline.disabled = false; }
    };
    approve.addEventListener("click", () => act("approve"));
    decline.addEventListener("click", () => act("decline"));
    actions = h("div", { class: "row request-actions" }, note, approve, decline);
  }
  return h("div", { class: "request" },
    h("div", { class: "spread" },
      h("div", {}, h("strong", {}, q.name), h("div", { class: "small muted" }, `${q.username}, asked ${when(q.requested_at)}`)),
      h("span", { class: "mark info" }, "wants auditor access")),
    actions);
}

/* ------------------------------------------------------------------ proof */
async function pageProof(main) {
  const wrap = page(main, "Proof", "Replays the whole journal from entry 1 and checks every rule independently of the stored balances.");
  const area = h("div", { class: "stack" }, h("p", { class: "muted" }, "Replaying the ledger…"));
  wrap.append(area);
  const r = await api("GET", "/api/audit/proof");
  if (!r.ok) { set(area, h("p", {}, errorText(r))); return; }
  const p = r.data;
  const failed = p.checks.filter((c) => !c.ok);
  const recon = h("div");
  const reconBtn = h("button", { class: "btn secondary", onclick: async () => {
    reconBtn.disabled = true;
    const res = await api("POST", "/api/audit/reconciliation");
    reconBtn.disabled = false;
    if (!res.ok) { toast(errorText(res), true); return; }
    const n = res.data.note;
    set(recon, h("div", { class: "ai-note" }, rupee(n.text), h("div", { class: "src" },
      n.source === "llm" ? `Written by ${n.model}; every number checked against the proof report.` : "Fixed template (no AI provider configured, or the AI's text failed the number check).",
      ` Journaled as entry #${n.seq}${n.cached ? " (reused: nothing changed since)" : ""}.`)));
  } }, "Write a reconciliation summary");
  const exportBtn = h("button", { class: "btn secondary", onclick: async () => {
    const res = await fetch("/api/audit/export", { headers: { Authorization: "Bearer " + state.token } });
    if (!res.ok) { toast("Export failed.", true); return; }
    download("ledgerline-evidence.json", await res.blob());
  } }, "Download evidence bundle");
  set(area, 
    h("section", { class: "panel proof-top" },
      seal(p.head.hash, p.head.seq, { ok: p.ok, draw: true }),
      h("div", {},
        h("h2", {}, p.ok ? `All ${p.checks.length} checks pass` : `${failed.length} check${failed.length > 1 ? "s" : ""} failed`),
        h("p", { class: "muted" }, `${p.entries} entries replayed at ${when(p.checked_at)}. Head entry #${p.head.seq}.`),
        h("p", { class: "hash" }, p.head.hash),
        h("div", { class: "totals" },
          total(money(p.totals.inside_ledgerline), "inside LedgerLine"),
          total(money(p.totals.external_in), "came in from outside"),
          total(money(p.totals.external_out), "went out"),
          total(String(p.totals.sum_of_all_balances), "sum of every balance, in paise")),
        h("div", { class: "row actions" }, h("button", { class: "btn", onclick: route }, "Run again"), reconBtn, exportBtn))),
    recon,
    h("section", { class: "panel" },
      h("ul", { class: "checks" }, p.checks.map((c) => h("li", {},
        h("span", { class: c.ok ? "tick" : "cross", "aria-label": c.ok ? "passed" : "failed" }, c.ok ? "✓" : "✗"),
        h("div", {}, h("div", {}, c.title), c.summary ? h("div", { class: "small muted" }, rupee(c.summary)) : null,
          c.problems.length ? h("ul", { class: "problems" }, c.problems.map((x) => h("li", {}, x))) : null))))),
    h("section", { class: "panel" },
      h("h2", {}, "Check it without trusting us"),
      h("p", { class: "muted" }, "Download the evidence bundle and run the independent verifier with the public key below. It needs only Python and the cryptography package."),
      h("pre", { class: "json" }, `python tools/verify_bundle.py ledgerline-evidence.json --public-key ${p.public_key}`),
      h("p", { class: "small muted" }, `Signing key id ${p.key_id}.`)));
}

/* ------------------------------------------------------------------ journal */
async function pageJournal(main) {
  const wrap = page(main, "Journal", "Everything that happened, in order. Each entry's hash includes the one before it.");
  const kind = h("select", { id: "j-kind" }, ["", "DEPOSIT", "WITHDRAWAL", "TRANSFER", "HOLD", "HOLD_APPROVED", "HOLD_REJECTED", "REVERSAL", "ACCOUNT_OPENED", "ACCOUNT_STATUS", "OVERDRAFT_LIMIT", "USER_CREATED", "USER_STATUS", "ACCESS_DECISION", "MAIN_ADMIN", "DETAILS_SUBMITTED", "DETAILS_VERIFIED", "DETAILS_RETURNED", "PASSWORD_CHANGED", "AI_NOTE", "GENESIS"]
    .map((k) => h("option", { value: k }, k ? k.replaceAll("_", " ").toLowerCase() : "all kinds")));
  const status = h("select", { id: "j-status" }, [["", "any outcome"], ["posted", "posted"], ["refused", "refused"], ["info", "records"]].map(([v, l]) => h("option", { value: v }, l)));
  const acct = h("input", { id: "j-acct", placeholder: "LL-1001" });
  const list = h("div", { class: "chain" });
  const more = h("button", { class: "btn secondary hidden" }, "Load older entries");
  let before = null;
  const load = async (reset) => {
    if (reset) { set(list, ); before = null; }
    const q = new URLSearchParams({ limit: "60" });
    if (before) q.set("before_seq", before);
    if (kind.value) q.set("kind", kind.value);
    if (status.value) q.set("status", status.value);
    if (acct.value.trim()) q.set("account", acct.value.trim());
    const r = await api("GET", "/api/audit/journal?" + q);
    if (!r.ok) { toast(errorText(r), true); return; }
    for (const e of r.data.entries) list.append(chainLink(e));
    if (reset && !r.data.entries.length) list.append(h("p", { class: "empty" }, "No entries match."));
    before = r.data.next_before_seq;
    more.classList.toggle("hidden", !before);
  };
  more.addEventListener("click", () => load(false));
  wrap.append(h("form", { class: "row filters", onsubmit: (e) => { e.preventDefault(); load(true); } },
    h("label", { class: "sr-only", for: "j-kind" }, "Kind"), kind,
    h("label", { class: "sr-only", for: "j-status" }, "Outcome"), status,
    h("label", { class: "sr-only", for: "j-acct" }, "Account"), acct,
    h("button", { class: "btn", type: "submit" }, "Filter")), list, h("div", { class: "actions" }, more));
  await load(true);
}

function chainLink(e) {
  const parties = [e.account && e.account.account_no, e.counterparty && e.counterparty.account_no].filter(Boolean).join(" to ");
  return h("div", { class: "chain-entry", tabindex: 0, onclick: () => openEntry(e.seq), onkeydown: (ev) => { if (ev.key === "Enter") openEntry(e.seq); } },
    h("div", { class: "seq num" }, `#${e.seq}`),
    h("div", {},
      h("div", { class: "row" }, h("strong", {}, e.kind.replaceAll("_", " ").toLowerCase()), statusMark(e.status, e.reason_code),
        e.reason_code ? h("span", { class: "small muted" }, e.reason_code) : null),
      h("div", { class: "small muted" }, [e.actor ? e.actor.name : "System", parties, e.memo ? `“${e.memo}”` : null].filter(Boolean).join(", ")),
      h("div", { class: "hash" }, `${short(e.prev_hash, 12)} then ${short(e.hash, 12)}`)),
    h("div", { class: "num" }, e.amount ? money(e.amount) : ""));
}

/* ------------------------------------------------------------------ flags & duplicates */
async function pageFlags(main) {
  const wrap = page(main, "Flags", "What an auditor should look at first.");
  const r = await api("GET", "/api/audit/flags");
  if (!r.ok) { wrap.append(h("p", {}, errorText(r))); return; }
  for (const [key, flag] of Object.entries(r.data)) {
    if (!flag.entries) {
      wrap.append(h("section", { class: "panel" }, h("h2", {}, flag.title), h("p", { class: "wide-num" }, String(flag.count))), h("div", { class: "page-head" }));
      continue;
    }
    wrap.append(h("section", { class: "stack" },
      h("div", { class: "spread" }, h("h2", {}, flag.title), h("span", { class: "mark " + (flag.entries.length ? (key === "backstops" ? "bad" : "held") : "ok") }, String(flag.entries.length))),
      flag.entries.length ? h("div", { class: "chain" }, flag.entries.slice(0, 12).map(chainLink)) : h("p", { class: "muted small" }, "None.")),
      h("div", { class: "page-head" }));
  }
}

async function pageDuplicates(main) {
  const wrap = page(main, "Duplicate requests", "Every request that arrived again with a key we had already answered. None of them moved money.");
  const r = await api("GET", "/api/audit/duplicates");
  if (!r.ok) { wrap.append(h("p", {}, errorText(r))); return; }
  const t = r.data.totals;
  wrap.append(h("div", { class: "totals" }, total(String(t.replayed || 0), "answered from storage"),
    total(String(t.key_reused_with_different_request || 0), "key reused for a different request (refused)"),
    total(String(t.in_flight_timeout || 0), "arrived while the first was still running")), h("div", { class: "page-head" }));
  const body = h("tbody");
  for (const d of r.data.replays) {
    body.append(h("tr", {}, h("td", {}, when(d.received_at)), h("td", {}, d.user), h("td", { class: "hash" }, d.key), h("td", {}, d.endpoint),
      h("td", {}, d.outcome === "replayed" ? h("span", { class: "mark ok" }, "replayed") : h("span", { class: "mark bad" }, d.outcome.replaceAll("_", " "))),
      h("td", {}, d.original_entry_seq ? h("button", { class: "btn link small", onclick: () => openEntry(d.original_entry_seq) }, `#${d.original_entry_seq}`) : "—")));
  }
  if (!r.data.replays.length) body.append(h("tr", {}, h("td", { colspan: 6, class: "empty" }, "No duplicates yet.")));
  wrap.append(h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Received"), h("th", {}, "User"), h("th", {}, "Key"), h("th", {}, "Endpoint"), h("th", {}, "Outcome"), h("th", {}, "Original entry"))), body)));
}
