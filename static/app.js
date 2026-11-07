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
  holder: [["#/accounts", "Accounts"], ["#/move", "Move money"], ["#/race", "Race lab"]],
  admin: [["#/overview", "Overview"], ["#/reviews", "Reviews"], ["#/all-accounts", "Accounts"], ["#/users", "Users"], ["#/journal", "Journal"], ["#/proof", "Proof"], ["#/flags", "Flags"]],
  auditor: [["#/overview", "Overview"], ["#/proof", "Proof"], ["#/journal", "Journal"], ["#/flags", "Flags"], ["#/duplicates", "Duplicates"], ["#/reviews", "Reviews"], ["#/all-accounts", "Accounts"]],
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
};

function renderShell(active) {
  const bar = $("#topbar");
  bar.classList.remove("hidden");
  const sealBtn = h("button", { class: "seal-mini", title: "Ledger proof", onclick: () => { location.hash = state.user.role === "holder" ? "#/accounts" : "#/proof"; } });
  set(bar, h("div", { class: "topbar-inner" },
    h("a", { class: "wordmark", href: HOME[state.user.role] }, "Ledger", h("span", {}, "Line")),
    h("nav", { class: "nav", "aria-label": "Main" },
      NAV[state.user.role].map(([href, label]) => h("a", { href, "aria-current": href === "#/" + active ? "page" : null }, label))),
    h("div", { class: "who" },
      sealBtn,
      h("div", {}, h("div", {}, state.user.name), h("div", { class: "role" }, state.user.role)),
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
window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);

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

function signInForm() {
  const err = h("p", { class: "form-error", role: "alert" });
  const user = h("input", { id: "u", name: "username", autocomplete: "username", required: true });
  const pass = h("input", { id: "p", name: "password", type: "password", autocomplete: "current-password", required: true });
  const button = h("button", { class: "btn", type: "submit" }, "Sign in");
  return h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      button.disabled = true; err.textContent = "";
      const r = await api("POST", "/api/auth/login", { username: user.value, password: pass.value }, { anonymous: true });
      button.disabled = false;
      if (!r.ok) { err.textContent = errorText(r); return; }
      signedIn(r.data);
    },
  },
    h("h2", { class: "sr-only" }, "Sign in"),
    h("div", { class: "field" }, h("label", { for: "u" }, "Username"), user),
    h("div", { class: "field" }, h("label", { for: "p" }, "Password"), pass),
    err, button);
}

function createUserForm() {
  const err = h("p", { class: "form-error", role: "alert" });
  const user = h("input", { id: "c-user", name: "username", autocomplete: "username", required: true, minlength: 3, maxlength: 32, spellcheck: "false" });
  const name = h("input", { id: "c-name", name: "name", autocomplete: "name", required: true, maxlength: 80 });
  const pass = h("input", { id: "c-pass", name: "password", type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const again = h("input", { id: "c-again", name: "confirm", type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const button = h("button", { class: "btn", type: "submit" }, "Create user");
  return h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      if (pass.value !== again.value) { err.textContent = "The two passwords are different."; again.focus(); return; }
      button.disabled = true;
      const r = await api("POST", "/api/auth/register",
        { username: user.value.trim().toLowerCase(), display_name: name.value.trim(), password: pass.value }, { anonymous: true });
      button.disabled = false;
      if (!r.ok) { err.textContent = errorText(r); return; }
      toast(`Welcome, ${r.data.user.name}. Your account ${r.data.account.account_no} is open with ₹0.00.`);
      signedIn(r.data);
    },
  },
    h("h2", { class: "sr-only" }, "Create user"),
    h("div", { class: "field" }, h("label", { for: "c-user" }, "Username"), user,
      h("span", { class: "hint" }, "3 to 32 characters: lowercase letters, digits, . _ -")),
    h("div", { class: "field" }, h("label", { for: "c-name" }, "Full name"), name),
    h("div", { class: "field" }, h("label", { for: "c-pass" }, "Password"), pass,
      h("span", { class: "hint" }, "At least 10 characters.")),
    h("div", { class: "field" }, h("label", { for: "c-again" }, "Confirm password"), again),
    err, button,
    h("p", { class: "auth-note" }, "New users are account holders. Your first account opens with ₹0.00; money arrives when an administrator records a deposit."));
}

async function pageLogin() {
  const panel = h("div");
  const tabs = {};
  const show = (which) => {
    for (const [k, b] of Object.entries(tabs)) b.setAttribute("aria-pressed", String(k === which));
    set(panel, which === "signin" ? signInForm() : createUserForm());
    $("input", panel)?.focus();
  };
  tabs.signin = h("button", { type: "button", class: "auth-tab", onclick: () => show("signin") }, "Sign in");
  tabs.create = h("button", { type: "button", class: "auth-tab", onclick: () => show("create") }, "Create user");

  const sealBox = h("div", { class: "hero-seal" }, h("p", {}, "Reading the ledger…"));
  set($("#app"), h("div", { class: "login" },
    h("section", { class: "login-hero" },
      h("a", { class: "wordmark", href: "#/" }, "Ledger", h("span", {}, "Line")),
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
  const list = await myAccounts();
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
  const [proof, reviews] = await Promise.all([api("GET", "/api/audit/proof"), api("GET", "/api/reviews?state=pending")]);
  if (!proof.ok) { wrap.append(h("p", {}, errorText(proof))); return; }
  const p = proof.data;
  const failed = p.checks.filter((c) => !c.ok);
  const pending = reviews.ok ? reviews.data.reviews.length : 0;
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
          h("a", { class: "btn secondary", href: "#/proof" }, "See every check"),
          state.user.role === "admin" ? h("a", { class: "btn secondary", href: "#/users" }, "Create user") : null))));
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
  const wrap = page(main, "People", "Who can sign in, and what they can do.");
  const r = await api("GET", "/api/users");
  if (!r.ok) { wrap.append(h("p", {}, errorText(r))); return; }
  if (isAdmin) {
    const u = h("input", { id: "n-user", placeholder: "username" });
    const n = h("input", { id: "n-name", placeholder: "Full name" });
    const role = h("select", { id: "n-role" }, ["holder", "auditor", "admin"].map((x) => h("option", { value: x }, x)));
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
      h("button", { class: "btn", type: "submit" }, "Create user")), h("div", { class: "page-head" }));
  }
  const body = h("tbody");
  for (const p of r.data.users) {
    body.append(h("tr", {}, h("td", {}, p.username), h("td", {}, p.name), h("td", {}, p.role), h("td", { class: "num" }, String(p.accounts)),
      h("td", {}, p.active ? h("span", { class: "mark ok" }, "active") : h("span", { class: "mark bad" }, "deactivated")),
      h("td", {}, isAdmin && p.username !== state.user.username ? h("button", { class: "btn secondary small", onclick: async () => {
        const res = await api("POST", `/api/users/${p.username}/${p.active ? "deactivate" : "activate"}`);
        toast(res.ok ? `${p.username} is now ${res.data.active ? "active" : "deactivated"}.` : errorText(res), !res.ok);
        route();
      } }, p.active ? "Deactivate" : "Activate") : null)));
  }
  wrap.append(h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Username"), h("th", {}, "Name"), h("th", {}, "Role"), h("th", {}, "Accounts"), h("th", {}, "Status"), h("th", {}, ""))), body)));
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
  const kind = h("select", { id: "j-kind" }, ["", "DEPOSIT", "WITHDRAWAL", "TRANSFER", "HOLD", "HOLD_APPROVED", "HOLD_REJECTED", "REVERSAL", "ACCOUNT_OPENED", "ACCOUNT_STATUS", "OVERDRAFT_LIMIT", "USER_CREATED", "USER_STATUS", "AI_NOTE", "GENESIS"]
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
