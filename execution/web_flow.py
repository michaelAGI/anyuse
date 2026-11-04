"""The browser tool set for OPEN tasks: logins, forms and purchases as ordinary
workflow steps, every commit step decided by ``commit_policy`` before it runs.

``FlowBrowserTools`` extends the read-only ``harness_tools.BrowserTools``: its observation is the demonstrator's
(the same numbering, so a pure search run still compiles through ``harness_compile``) PLUS every form field of the
page (text, password, e-mail, date, number, text area, drop-down options, checkbox and radio state, the field's
form), a forms line, and the read-backs a commit needs (the form's values, the order's line items, the total).

* ``fill`` sets several fields in ONE step (a drop-down by its option, a checkbox yes/no, a radio by its label, a
  date). A ``${secret:NAME}`` reference is resolved by THIS executor and typed (CDP ``Input.insertText``) only into
  a login form posted to the run's own site; it never appears in a JS expression, an observation, a trace or a
  model prompt (``secret_refs.Redactor``). A password field takes nothing but a reference.
* ``login`` finds the login form (following the site's login link once), types the username and the password
  reference, submits, and asks the CALLER for a one-time code when the site wants one (``commit_policy.Question``
  kind ``otp``; no answer = stop).
* a click on a control that commits (a POST form's submit, "Add to cart", "Pay now", "Delete" ...) is classified
  (``commit_of``) with its target and read-back; the harness decides covered / ask / deviation; only then does the
  click run, inside a ``cdp_guard.WriteGrant`` window that lets exactly that step's POST through.
* a CAPTCHA widget or a bot wall is a typed stop; nothing is solved or bypassed.

PURE apart from the injected page (``cdp_guard.GuardedSession`` live, ``web_flow_offline.FlowDomPage`` offline).
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import commit_policy as CP
from anyuse.execution import harness_tools as HT
from anyuse.execution import harness_types as T
from anyuse.execution import secret_refs as SR
from anyuse.execution.web_arch_run import CONTROL_SEL

FIELD_SEL = "input, textarea, select"
ARGS = "/*ARGS*/"

#: the page's fields, forms, form buttons, CAPTCHA evidence, table/list rows and text: a local read
FLOW_READ_JS = r"""
(() => { /*anyuse:flow_read*/
  const CSEL = %s, FSEL = %s;
  const controls = Array.from(document.querySelectorAll(CSEL));
  const inputs = Array.from(document.querySelectorAll('input'));
  const vis = n => { const r = n.getBoundingClientRect(); if (!r.width || !r.height) return false;
    const cs = getComputedStyle(n); return cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const clean = t => String(t || '').replace(/\s+/g, ' ').trim();
  const forms = Array.from(document.forms);
  const keyOf = f => { if (!f) return ''; const k = forms.indexOf(f);
    return (f.id ? '#' + f.id : (f.getAttribute('name') ? 'name=' + f.getAttribute('name') : 'form' + k)); };
  const labelOf = n => { let t = '';
    if (n.labels && n.labels.length) t = Array.from(n.labels).map(l => l.innerText).join(' ');
    if (!t) t = n.getAttribute('aria-label') || n.getAttribute('placeholder') || n.getAttribute('title') || '';
    if (!t && n.closest('label')) t = n.closest('label').innerText;
    if (!t) t = n.name || n.id || ''; return clean(t).slice(0, 120); };
  const fields = [];
  Array.from(document.querySelectorAll(FSEL)).forEach((n, k) => {
    const tag = n.tagName.toLowerCase();
    const type = (tag === 'input' ? (n.getAttribute('type') || 'text') : tag).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'file'].includes(type)) return;
    if (!vis(n) && !(type === 'radio' || type === 'checkbox')) return;
    const f = {k: k, ii: tag === 'input' ? inputs.indexOf(n) : -1, ci: controls.indexOf(n), tag: tag, type: type,
      name: n.getAttribute('name') || '', id: n.id || '', label: labelOf(n), required: !!n.required,
      autocomplete: (n.getAttribute('autocomplete') || '').toLowerCase(), inputmode: n.getAttribute('inputmode') || '',
      maxlength: n.maxLength > 0 ? n.maxLength : 0, form: keyOf(n.form), disabled: !!n.disabled};
    if (type === 'password') { f.filled = !!n.value; f.length = n.value.length; }
    else if (type === 'checkbox' || type === 'radio') { f.checked = !!n.checked; f.value = n.value || 'on'; }
    else if (tag === 'select') { f.options = Array.from(n.options).slice(0, 60).map(o =>
        ({value: o.value, text: clean(o.text).slice(0, 80), selected: o.selected})); f.value = n.value; }
    else f.value = String(n.value || '').slice(0, 300);
    fields.push(f);
  });
  const fmeta = forms.map(f => { let h = '';
    const lg = f.querySelector('h1, h2, h3') || f.querySelector(':scope > legend'); if (lg) h = lg.innerText;
    if (!h) { let p = f.previousElementSibling; for (let j = 0; j < 3 && p && !h; j++, p = p.previousElementSibling)
      if (/^H[1-3]$/.test(p.tagName)) h = p.innerText; }
    if (!h) { const sec = f.closest('section, article, main'); const hh = sec && sec.querySelector('h1, h2, h3');
      if (hh) h = hh.innerText; }
    return {key: keyOf(f), id: f.id || '', name: f.getAttribute('name') || '', action: f.action || location.href,
      method: (f.getAttribute('method') || 'get').toLowerCase(), has_password: !!f.querySelector('input[type=password]'),
      heading: clean(h).slice(0, 120),
      submits: Array.from(f.querySelectorAll('button:not([type=button]):not([type=reset]), input[type=submit], ' +
        'input[type=image]')).map(b => ({ci: controls.indexOf(b), text: clean(b.innerText || b.value ||
        b.getAttribute('aria-label') || '').slice(0, 80)}))}; });
  const buttons = []; controls.forEach((n, i) => { if (n.form) buttons.push({ci: i, form: keyOf(n.form)}); });
  const cap = document.querySelector('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], ' +
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare"], ' +
    'input[name*="captcha" i], img[src*="captcha" i]');
  const body = (document.body && document.body.innerText) || '';
  const captcha = cap ? ('widget ' + clean(cap.className || cap.tagName)).slice(0, 80) :
    (/i'?m not a robot|ich bin kein roboter|verify you are human|confirm that you are not a robot/i.test(body) ?
     'text: not a robot' : '');
  const rows = []; Array.from(document.querySelectorAll('tr, li')).slice(0, 300).forEach(r => {
    if (!vis(r)) return; const t = clean(r.innerText); if (!t || !/\d/.test(t)) return;
    const cells = r.tagName === 'TR' ? Array.from(r.children).map(c => clean(c.innerText)) : [t];
    const box = r.closest('table, ul, ol, section, article, div');
    let ctx = box ? ((box.id || '') + ' ' + (box.className || '')) : '';
    const sec = r.closest('section, article, main'); const h = sec && sec.querySelector('h1, h2, h3');
    if (h) ctx += ' ' + clean(h.innerText); rows.push({cells: cells.slice(0, 8), ctx: clean(ctx).slice(0, 160)}); });
  const h1 = Array.from(document.querySelectorAll('h1')).filter(vis).map(h => clean(h.innerText)).filter(Boolean)
    .slice(0, 2);
  return {url: location.href, title: document.title || '', h1: h1, fields: fields, forms: fmeta, buttons: buttons,
          captcha: captcha, rows: rows.slice(0, 120), text: body.slice(0, 20000)};
})()
""" % (json.dumps(CONTROL_SEL), json.dumps(FIELD_SEL))

#: set ONE field (never a secret: those are typed with Input.insertText after FLOW_FOCUS_JS)
FLOW_FILL_JS = r"""
(function (args) { /*anyuse:flow_fill*/
  const [k, want, value] = args;
  const n = document.querySelectorAll(%s)[k];
  if (!n) return {ok: false, why: 'gone'};
  if ((want.name && n.getAttribute('name') !== want.name) || (want.id && n.id !== want.id))
    return {ok: false, why: 'changed'};
  const fire = () => { n.dispatchEvent(new Event('input', {bubbles: true}));
    n.dispatchEvent(new Event('change', {bubbles: true})); };
  const tag = n.tagName.toLowerCase(); const type = (n.getAttribute('type') || tag).toLowerCase();
  const v = String(value); const yes = /^(yes|true|on|1|checked|selected|ja|x)$/i.test(v.trim());
  const norm = t => String(t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  if (type === 'checkbox') { n.checked = yes; fire(); return {ok: true, checked: n.checked}; }
  if (type === 'radio') { let pick = null;
    if (yes) pick = n; else { const group = Array.from((n.form || document).querySelectorAll('input[type=radio]'))
      .filter(r => r.name === n.name);
      pick = group.find(r => norm(r.value) === norm(v)) || group.find(r => r.labels && r.labels.length &&
        norm(r.labels[0].innerText) === norm(v)) || null; }
    if (!pick) return {ok: false, why: 'no such choice'}; pick.checked = true;
    pick.dispatchEvent(new Event('change', {bubbles: true}));
    return {ok: true, value: pick.value}; }
  if (tag === 'select') { const opts = Array.from(n.options);
    const o = opts.find(x => norm(x.text) === norm(v)) || opts.find(x => norm(x.value) === norm(v)) ||
      opts.find(x => v && norm(x.text).includes(norm(v)));
    if (!o) return {ok: false, why: 'no such option'}; n.value = o.value; fire();
    return {ok: true, value: n.value, text: o.text}; }
  const proto = Object.getPrototypeOf(n); const d = Object.getOwnPropertyDescriptor(proto, 'value');
  if (d && d.set) d.set.call(n, v); else n.value = v; fire();
  return {ok: true, value: String(n.value).slice(0, 300)};
})(/*ARGS*/%%s)
""" % json.dumps(FIELD_SEL)

#: focus ONE field and empty it (the secret or the code is then typed with Input.insertText)
FLOW_FOCUS_JS = r"""
(function (args) { /*anyuse:flow_focus*/
  const [k, want] = args; const n = document.querySelectorAll(%s)[k];
  if (!n || (want.name && n.getAttribute('name') !== want.name) || (want.id && n.id !== want.id)) return false;
  n.focus(); const proto = Object.getPrototypeOf(n); const d = Object.getOwnPropertyDescriptor(proto, 'value');
  if (d && d.set) d.set.call(n, ''); else n.value = ''; return document.activeElement === n;
})(/*ARGS*/%%s)
""" % json.dumps(FIELD_SEL)

#: the read-back of one form's fields (a password: only whether it is filled and its length)
FLOW_VALUES_JS = r"""
(function (args) { /*anyuse:flow_values*/
  const [key] = args; const forms = Array.from(document.forms);
  const keyOf = f => { if (!f) return ''; const k = forms.indexOf(f);
    return (f.id ? '#' + f.id : (f.getAttribute('name') ? 'name=' + f.getAttribute('name') : 'form' + k)); };
  const out = []; Array.from(document.querySelectorAll(%s)).forEach((n, k) => {
    if (keyOf(n.form) !== key) return; const tag = n.tagName.toLowerCase();
    const type = (tag === 'input' ? (n.getAttribute('type') || 'text') : tag).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'file'].includes(type)) return;
    const f = {k: k, name: n.getAttribute('name') || '', type: type};
    if (type === 'password') { f.filled = !!n.value; f.length = n.value.length; }
    else if (type === 'checkbox' || type === 'radio') { f.checked = !!n.checked; f.value = n.value || 'on'; }
    else if (tag === 'select') { const o = n.options[n.selectedIndex]; f.value = n.value; f.text = o ? o.text : ''; }
    else f.value = String(n.value || '').slice(0, 300);
    out.push(f); });
  return out;
})(/*ARGS*/%%s)
""" % json.dumps(FIELD_SEL)

#: click control ``ci`` of CONTROL_SEL (its text must still match); unlike web_arch_run.CLICK_JS it may click a login
#: form's button: the harness decided the step before this runs
FLOW_CLICK_JS = r"""
(function (args) { /*anyuse:flow_click*/
  const [ci, want] = args; const n = document.querySelectorAll(%s)[ci];
  if (!n) return {ok: false, why: 'gone'};
  const t = (n.getAttribute('aria-label') || (n.innerText || '').trim() || n.getAttribute('title') || n.value || '')
    .replace(/\s+/g, ' ').trim();
  if (want && t && t.slice(0, 30).toLowerCase() !== want.slice(0, 30).toLowerCase())
    return {ok: false, why: 'changed', text: t.slice(0, 60)};
  n.scrollIntoView({block: 'center'}); n.click(); return {ok: true};
})(/*ARGS*/%%s)
""" % json.dumps(CONTROL_SEL)

FIELD_LEN_JS = r"""
(function (args) { /*anyuse:flow_len*/ const n = document.querySelectorAll(%s)[args[0]];
  return n ? String(n.value || '').length : -1; })(/*ARGS*/%%s)
""" % json.dumps(FIELD_SEL)


async def act_and_settle(page, expr: str, max_s: float = 8.0) -> Any:
    """Run one acting expression inside the session's action window and wait until the page settled (the form's POST,
    its redirect and the next page's requests pass the guard while the session reads its events)."""
    from anyuse.execution import web_arch_run as RR
    async with RR.acting(page):
        res = await page.js(expr)
        if isinstance(res, dict) and res.get("ok"):
            await page.settle(1.5)
            await RR.settle_bounded(page, max_s=max_s)
    return res


def js_call(template: str, *args: Any) -> str:
    return template % json.dumps(list(args))


def js_args(expr: str) -> List[Any]:
    """The arguments of a ``js_call`` expression (the offline page's emulation)."""
    i = expr.index(ARGS) + len(ARGS)
    return json.loads(expr[i:expr.rindex(")")])


# ---------------------------------------------------------------------------
# reading a page
# ---------------------------------------------------------------------------
_OTP = re.compile(r"one.?time|otp|totp|2fa|two.?(?:step|factor)|verification code|security code|authenticat\w* code|"
                  r"bestatigungscode|einmal|sicherheitscode", re.I)
_USER = re.compile(r"user|e-?mail|login|benutzer|account|kennung", re.I)
_LOGIN_LINK = re.compile(r"\b(?:log ?in|sign ?in|einloggen|anmelden|login)\b", re.I)
_LOGIN_PATH = re.compile(r"(?i)(^|/)(login|log-in|signin|sign-in|sign_in|anmelden|session/new|users/sign_in)(/|$|\?)")
_ITEM_CTX = re.compile(r"cart|basket|bag|order|summary|checkout|warenkorb|bestellung|zusammenfassung|einkaufswagen",
                       re.I)
_NOT_ITEM = re.compile(r"\b(?:sub ?total|total|shipping|delivery|tax|vat|discount|gesamt\w*|summe|versand|mwst|"
                       r"rabatt|zwischensumme)\b", re.I)
_PATH_CLASS = ((CP.CART, r"cart|basket|warenkorb"), (CP.PAY, r"checkout|pay|order|kasse|bestell"),
               (CP.REGISTER, r"register|signup|sign-up|registr"), (CP.DELETE, r"delete|remove|destroy|losch"),
               (CP.BOOK, r"book|reserv|buch"), (CP.SUBMIT, r"contact|message|support|feedback|kontakt|send"))
_YES = {"yes", "true", "on", "1", "checked", "selected", "ja", "x"}


def field_kind(f: Dict[str, Any]) -> str:
    t, tag = str(f.get("type") or ""), str(f.get("tag") or "")
    blob = " ".join(str(f.get(k) or "") for k in ("name", "id", "label", "autocomplete"))
    if t == "password":
        return "password field"
    if f.get("autocomplete") == "one-time-code" or (t in ("text", "number", "tel") and _OTP.search(blob)):
        return "one-time code field"
    return {"email": "email field", "date": "date field", "number": "number field", "tel": "phone field",
            "textarea": "text area", "select": "drop-down", "checkbox": "checkbox", "radio": "radio",
            "search": "search field", "url": "url field"}.get(t if tag != "textarea" else "textarea", "text field")


def display_value(f: Dict[str, Any]) -> str:
    t = str(f.get("type") or "")
    if t == "password":
        return "(filled)" if f.get("filled") else ""
    if t in ("checkbox", "radio"):
        return "checked" if f.get("checked") else "not checked"
    if f.get("tag") == "select":
        sel = next((o for o in f.get("options") or [] if o.get("selected")), None)
        return str(sel.get("text") or "") if sel else ""
    return str(f.get("value") or "")[:60]


def line_items(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """The order's line items: rows of a cart / order / summary region that carry an amount, without the subtotal,
    shipping and total lines; the item's name (its first cell)."""
    out: List[str] = []
    for r in rows or []:
        if not _ITEM_CTX.search(str(r.get("ctx") or "")):
            continue
        cells = [str(c) for c in r.get("cells") or [] if str(c).strip()]
        text = " ".join(cells)
        if not cells or not CP.money_in(text) or _NOT_ITEM.search(cells[0]):
            continue
        name = cells[0] if len(cells) > 1 else CP._MONEY.sub("", text).strip()
        if name and not CP.money_in(name):
            out.append(name[:120])
    return out


def norm_date(v: str) -> str:
    v = str(v or "").strip()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", v)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return v


def _path(url: str) -> str:
    return urllib.parse.urlsplit(str(url or "")).path or "/"


#: how a run with a secret reference among its values signs in (the robots gate's record)
SIGNS_IN = "your credentials"


def signs_in(values: Optional[Dict[str, Any]]) -> str:
    """How a run signs in on its site, or ``""``: a value that is a ``${secret:NAME}`` reference is a credential, and
    a secret is typed only into a login form posted to the run's own site."""
    return SIGNS_IN if any(SR.is_ref(v) for v in (values or {}).values()) else ""


def note_sign_in(gate: Any, start_url: str, related_origins: Sequence[str], values: Optional[Dict[str, Any]]) -> str:
    """Tell the run's robots gate that the run signs in on its own origins (``harness_product.robots_gate``): in a task
    the user started, those requests are not held to robots.txt. Returns the gate's reason, ``""`` when nothing
    changes."""
    how = signs_in(values)
    sign = getattr(gate, "sign_in", None)
    if not how or not callable(sign):
        return ""
    return str(sign([start_url, *related_origins], how) or "")


class FlowBrowserTools(HT.BrowserTools):
    """The open-task browser (module doc). ``values``: the run's named values (credentials by reference);
    ``secrets``: the run's ``SecretStore``; ``ask``: set by the harness (the caller's questions)."""

    def __init__(self, page, start_url: str, site: str = "", *, values: Optional[Dict[str, str]] = None,
                 secrets: Optional[SR.SecretStore] = None, **kw):
        super().__init__(page, start_url, site, **kw)
        self.values = {str(k): str(v) for k, v in (values or {}).items()}
        self.secrets = secrets or SR.SecretStore()
        self.ask: Optional[CP.Asker] = None
        #: form key -> {field key -> what the run filled (a reference for a secret)}
        self.filled: Dict[str, Dict[str, Any]] = {}
        self.flow: Dict[str, Any] = {}
        self.commit: Optional[CP.Commit] = None
        self.grant = None
        budget = getattr(page, "budget", None)
        if budget is not None:
            from anyuse.execution.cdp_guard import WriteGrant
            self.grant = WriteGrant(start_url)
            budget.write_grant = self.grant
        #: a run that signs in is a signed-in task: its gate stops holding the site's own origins to robots.txt when
        #: the user started it ("" when nothing changed)
        self.signed_in = note_sign_in(self.allow_url, start_url, self.related_origins, self.values)

    @property
    def redactor(self) -> SR.Redactor:
        return self.secrets.redactor

    # -- reading --------------------------------------------------------------------------------
    def _read_flow(self) -> Dict[str, Any]:
        try:
            r = HT.run_sync(self.page.js(FLOW_READ_JS), self.loop)
        except Exception:  # noqa: BLE001 - a read never fails a run
            r = None
        self.flow = r if isinstance(r, dict) else {}
        return self.flow

    def forms(self) -> Dict[str, Dict[str, Any]]:
        return {f.get("key"): f for f in self.flow.get("forms") or []}

    def observe(self) -> T.Observation:
        obs = super().observe()
        if obs.blocked:
            return obs
        flow = self._read_flow()
        page = dict(obs.page or {})
        page["flow"] = {k: v for k, v in flow.items() if k != "text"}
        obs.page = page
        if flow.get("captcha"):
            obs.blocked = "captcha: " + str(flow["captcha"])[:100]
            return obs
        fields = [f for f in flow.get("fields") or [] if isinstance(f, dict)]
        by_ci = {int(f.get("ci", -1)): f for f in fields if int(f.get("ci", -1)) >= 0}
        btn_form = {int(b.get("ci", -1)): str(b.get("form") or "") for b in flow.get("buttons") or []}
        forms = self.forms()
        listed_ci, search_ii = set(), set()
        for e in obs.elements:
            m = obs.mapping.get(e.index) or {}
            if m.get("src") == "el":
                ci = int(m.get("i") if m.get("i") is not None else -1)
                listed_ci.add(ci)
                if ci in btn_form:
                    m["form"] = btn_form[ci]
                f = by_ci.get(ci)
                if f is not None:
                    m["field"] = f
                    e.kind = field_kind(f)
                    e.value = display_value(f)
                    if f.get("options"):
                        e.options = [str(o.get("text") or "") for o in f["options"]][:40]
            elif m.get("src") == "input":
                ii = int((m.get("input") or {}).get("index", -1))
                search_ii.add(ii)
                f = next((x for x in fields if int(x.get("ii", -2)) == ii), None)
                if f is not None:
                    m["field"] = f
        n = max([e.index for e in obs.elements] + [-1]) + 1
        for f in fields:
            ci, ii = int(f.get("ci", -1)), int(f.get("ii", -1))
            if (ci >= 0 and ci in listed_ci) or (ii >= 0 and ii in search_ii):
                continue
            form = forms.get(f.get("form")) or {}
            fname = (form.get("id") or form.get("name") or _path(form.get("action") or "")).strip("/#") or "page"
            obs.elements.append(T.Element(index=n, kind=f"{field_kind(f)} [form {fname}]", text=str(f.get("label") or ""),
                                          type=str(f.get("type") or ""), value=display_value(f),
                                          options=[str(o.get("text") or "") for o in f.get("options") or []][:40],
                                          enabled=not f.get("disabled")))
            obs.mapping[n] = {"src": "field", "field": f, "form": f.get("form")}
            n += 1
        line = []
        for key, fm in forms.items():
            subs = ", ".join(repr(s.get("text")) for s in fm.get("submits") or [] if s.get("text"))
            line.append(f"{key}: {str(fm.get('method') or 'get').upper()} {_path(fm.get('action') or '')}"
                        + (" (login form)" if fm.get("has_password") else "") + (f" buttons {subs}" if subs else ""))
        if line:
            obs.lists = list(obs.lists) + ["FORMS (data): " + "; ".join(line)[:1200]]
        return obs

    # -- effects, the gate and commits ----------------------------------------------------------
    def _field(self, index: int, obs: T.Observation) -> Optional[Dict[str, Any]]:
        m = obs.mapping.get(int(index)) or {}
        return m.get("field")

    def target_label(self, action: T.Action, obs: T.Observation) -> str:
        if action.kind == T.Login.kind:
            return self.site
        if action.kind == T.Fill.kind:
            return ", ".join(str((self._field(int(k), obs) or {}).get("label") or k) for k in action.fields)[:200]
        return super().target_label(action, obs)

    def effect_of(self, action: T.Action, obs: T.Observation) -> str:
        if action.kind in (T.Fill.kind, T.Login.kind):
            return T.WRITE
        if action.kind in (T.Click.kind, T.Select.kind, T.Open.kind):
            c = self.commit_class(action, obs)
            return CP.EFFECT[c] if c else T.READ
        return super().effect_of(action, obs)

    def _search_field(self, index: int, obs: T.Observation) -> Optional[Dict[str, Any]]:
        """A form text field the observation numbered (flow numbering) that IS a search box: a text or search input of
        a GET form or of no form (www.fotografia.it: the page's search field was listed only as
        "text field [form page]" and "search" on it was refused as "not a search box")."""
        m = obs.mapping.get(int(index)) or {}
        f = m.get("field") or {}
        if m.get("src") != "field" or f.get("tag") != "input" or int(f.get("ii", -1)) < 0:
            return None
        if str(f.get("type") or "text") not in ("text", "search"):
            return None
        form = self.forms().get(f.get("form")) or {}
        if form and str(form.get("method") or "get").lower() != "get":
            return None
        if form.get("has_password"):
            return None
        return f

    def correction(self, action: T.Action, obs: T.Observation) -> str:
        note = super().correction(action, obs)
        if action.kind in (T.Search.kind, T.Type.kind):
            extra = [f"[{n}] {(m.get('field') or {}).get('label') or (m.get('field') or {}).get('name') or 'text'}"
                     for n in sorted(obs.mapping) for m in [obs.mapping[n]] if self._search_field(n, obs)]
            if extra:
                note += " Search fields of a GET form: " + ", ".join(extra[:6]) + "."
        return note

    def gate(self, action: T.Action, obs: T.Observation) -> Tuple[str, Optional[str]]:
        from anyuse.execution.demo_compile import same_site
        from anyuse.execution.request_policy import destination_refusal, exact_origin_allowlist
        if action.kind == T.Login.kind:
            return "", None
        if action.kind == T.Search.kind and self._search_field(action.index, obs) is not None:
            return "", None
        if action.kind == T.Fill.kind:
            bad = [k for k in action.fields if self._field(int(k), obs) is None]
            return (f"element(s) {', '.join(bad)} are not form fields on this page" if bad else ""), None
        if action.kind == T.Open.kind:
            here = obs.url or self.start_url
            u = urllib.parse.urljoin(here, action.url) if not action.url.startswith("http") else action.url
            why = HT.gov_refusal(u)
            if why:
                return why, T.Stop.GOV_HOST
            if not same_site(u, self.site):
                return f"blocked: another website ({urllib.parse.urlsplit(u).hostname})", None
            why = destination_refusal(u, exact_origin_allowlist(self.start_url, self.related_origins))
            if why:
                return f"blocked: {why}", None
            if self.allow_url is not None and not self.allow_url(u):
                return "blocked: robots.txt disallows this URL", None
            return "", None
        if action.kind in (T.Click.kind, T.Select.kind):
            m = obs.mapping.get(int(action.index)) or {}
            if not m:
                return f"element {action.index} is not on this page", None
            if m.get("src") == "field" and action.kind == T.Click.kind:
                return f"element {action.index} is a form field: use fill", None
            f = m.get("field") or {}
            if action.kind == T.Click.kind and f and f.get("tag") == "select":
                # a click on a drop-down chooses nothing: a run lost three steps that way
                return (f"element {action.index} is a drop-down: choose its option with fill "
                        f"{{\"{action.index}\": \"<option>\"}}"), None
            if m.get("src") == "input":
                return f"element {action.index} is a search box, not a link or button", None
            href = str(m.get("href") or "")
            if href.startswith(("mailto:", "tel:", "sms:", "javascript:")):
                return f"element {action.index} is a {href.split(':')[0]} link (never followed)", None
            if href.startswith("http"):
                why = HT.gov_refusal(href)
                if why:
                    return why, T.Stop.GOV_HOST
                if not same_site(href, self.site) and not self.commit_class(action, obs):
                    return f"blocked: another website ({urllib.parse.urlsplit(href).hostname})", None
                if self.allow_url is not None and same_site(href, self.site) and not self.allow_url(href):
                    return "blocked: robots.txt disallows this link", None
            return "", None
        return super().gate(action, obs)

    def commit_class(self, action: T.Action, obs: T.Observation) -> str:
        """The commit class of a click (``""``: not a commit - a navigation, a search, a local control)."""
        if action.kind == T.Login.kind:
            return CP.LOGIN
        if action.kind not in (T.Click.kind,):
            return ""
        m = obs.mapping.get(int(action.index)) or {}
        if m.get("src") != "el":
            return ""
        text, href, tag = str(m.get("text") or ""), str(m.get("href") or ""), str(m.get("tag") or "")
        if m.get("field"):
            return ""                      # a checkbox / radio / drop-down: local until the form is sent
        if tag == "a" and href:
            c = CP.class_of_label(text)
            q = urllib.parse.urlsplit(href).query
            if c == CP.CART or (q and re.search(r"(?i)(^|&)(add-to-cart|add_to_cart|addtocart|action=add|add)=", q)):
                return CP.CART
            if c in (CP.DELETE, CP.BOOK):
                return c
            if c == CP.PAY and re.search(r"\b(?:buy now|buy it now|jetzt kaufen|order now|sofort kaufen)\b",
                                         CP.fold(text)):
                return CP.CART             # a "buy now" link starts the purchase: it puts the item in the cart
            return ""                      # a link to the cart, the checkout, the login page: reading
        form = self.forms().get(str(m.get("form") or "")) if m.get("form") else None
        if form is not None and str(form.get("method") or "get") == "get":
            return ""                      # a search or a filter
        c = CP.class_of_label(text)
        if form is not None:
            if form.get("has_password") and c in (CP.UNKNOWN, CP.SUBMIT, CP.LOGIN):
                return CP.LOGIN
            if c == CP.UNKNOWN:
                path = _path(form.get("action") or "")
                return next((k for k, rx in _PATH_CLASS if re.search(rx, path, re.I)), CP.SUBMIT)
            return c
        return c if c != CP.UNKNOWN else ""

    def _form_identity(self, form: Dict[str, Any], button: str = "") -> str:
        bits = [str(form.get("heading") or ""), str(form.get("id") or form.get("name") or ""),
                _path(form.get("action") or ""), button]
        return " - ".join(b for b in bits if b)[:200]

    def commit_of(self, action: T.Action, obs: T.Observation) -> Optional[CP.Commit]:
        from anyuse.execution.request_policy import origin
        c = self.commit_class(action, obs)
        if not c:
            return None
        site = origin(obs.url or self.start_url) or self.site
        if action.kind == T.Login.kind:
            return CP.Commit(CP.LOGIN, label="log in", target=self.site, site=site, url=obs.url,
                             action=action.to_dict())
        m = obs.mapping.get(int(action.index)) or {}
        label = str(m.get("text") or "")
        commit = CP.Commit(c, label=label, site=site, url=obs.url, action=action.to_dict())
        flow = self.flow
        form = self.forms().get(str(m.get("form") or "")) if m.get("form") else None
        if c == CP.CART:
            h1 = [h for h in flow.get("h1") or [] if h]
            commit.target = h1[0] if h1 else str(m.get("card") or obs.title or "")
        elif c == CP.PAY:
            commit.items = line_items(flow.get("rows") or [])
            commit.total = CP.read_total(flow.get("text") or obs.text)
            commit.target = "order: " + ", ".join(commit.items)[:160] if commit.items else "order"
        elif c == CP.LOGIN:
            commit.target = self.site
        elif form is not None:
            commit.target = self._form_identity(form, label)
            commit.other_targets = [self._form_identity(f) for k, f in self.forms().items()
                                    if k != form.get("key") and str(f.get("method")) == "post"]
        if form is not None:
            commit.form, commit.readback_issues = self.readback(str(form.get("key") or ""))
        return commit

    def begin_commit(self, commit: CP.Commit) -> None:
        self.commit = commit
        if self.grant is not None:
            self.grant.open(f"{commit.cls}: {commit.label}"[:80])

    def end_commit(self) -> None:
        self.commit = None
        if self.grant is not None:
            self.grant.close()

    # -- read-back ------------------------------------------------------------------------------
    def readback(self, form_key: str) -> Tuple[Dict[str, Any], List[str]]:
        """``({label: value}, issues)``: every field of the form as it holds now (a password as the reference the run
        typed, or "(filled)"); an issue for each field whose value differs from what the run filled."""
        try:
            got = HT.run_sync(self.page.js(js_call(FLOW_VALUES_JS, form_key)), self.loop) or []
        except Exception:  # noqa: BLE001
            got = []
        labels = {int(f.get("k", -1)): str(f.get("label") or f.get("name") or "") for f in self.flow.get("fields") or []}
        want = self.filled.get(form_key) or {}
        shown: Dict[str, Any] = {}
        issues: List[str] = []
        radios_done = set()
        for f in got if isinstance(got, list) else []:
            k, t, name = int(f.get("k", -1)), str(f.get("type") or ""), str(f.get("name") or "")
            lab = labels.get(k) or name or str(k)
            exp = want.get(str(k))
            if t == "password":
                ref = (exp or {}).get("ref") if isinstance(exp, dict) else ""
                val = ref if ref and f.get("filled") else ("(filled)" if f.get("filled") else "(empty)")
                if isinstance(exp, dict) and int(f.get("length") or 0) != int(exp.get("length") or -1):
                    issues.append(f"{lab}: the password field does not hold what was typed")
                shown[lab] = val
                continue
            if t == "radio":
                if f.get("checked"):
                    shown[name or lab] = str(f.get("value") or "")
                    radios_done.add(name)
                elif name not in radios_done:
                    shown.setdefault(name or lab, "")
                if isinstance(exp, dict) and exp.get("radio") and f.get("checked") and \
                        CP.fold(f.get("value")) != CP.fold(exp["radio"]):
                    issues.append(f"{name}: {f.get('value')!r} is chosen, the run chose {exp['radio']!r}")
                continue
            if t == "checkbox":
                shown[lab] = "yes" if f.get("checked") else "no"
                if isinstance(exp, dict) and "checked" in exp and bool(f.get("checked")) != bool(exp["checked"]):
                    issues.append(f"{lab}: the checkbox is {'on' if f.get('checked') else 'off'}")
                continue
            val = str(f.get("text") or f.get("value") or "") if f.get("text") is not None else str(f.get("value") or "")
            shown[lab] = self.redactor.text(val)
            if isinstance(exp, dict) and "value" in exp:
                ok = CP.fold(f.get("value")) == CP.fold(exp["value"]) or CP.fold(f.get("text")) == CP.fold(exp["value"])
                if not ok:
                    issues.append(f"{lab}: holds {shown[lab][:40]!r}, the run filled {str(exp.get('shown'))[:40]!r}")
        return shown, issues

    # -- dispatch -------------------------------------------------------------------------------
    def dispatch(self, action: T.Action, obs: T.Observation) -> T.Outcome:
        if action.kind == T.Fill.kind:
            return self._fill(action, obs)
        if action.kind == T.Search.kind and self._search_field(action.index, obs) is not None:
            # a GET form's text field searched like the page's search box (the same input index space as READ's)
            f = self._search_field(action.index, obs)
            o = T.Observation(**{k: v for k, v in obs.__dict__.items()})
            o.mapping = dict(obs.mapping)
            o.mapping[int(action.index)] = {"src": "input", "field": f,
                                            "input": {"index": int(f.get("ii")), "name": f.get("name") or "",
                                                      "id": f.get("id") or "", "type": f.get("type") or "text",
                                                      "visible": True}}
            return super().dispatch(action, o)
        if action.kind in (T.Select.kind, T.Click.kind) and (obs.mapping.get(int(action.index)) or {}).get("field") \
                and (obs.mapping.get(int(action.index)) or {}).get("src") == "el":
            # a form's drop-down, checkbox or radio is set like any field (``select`` went to the
            # demonstrator's sort drop-down path, which knows no options of a form field, twice "no option 'Billing'")
            f = obs.mapping[int(action.index)]["field"]
            if action.kind == T.Select.kind:
                value = action.option
            elif f.get("type") == "checkbox":
                value = "no" if f.get("checked") else "yes"
            else:
                value = "yes"
            return self._fill(T.Fill({str(int(action.index)): value}), obs)
        if action.kind == T.Login.kind:
            return self._login(obs)
        if action.kind == T.Click.kind:
            m = obs.mapping.get(int(action.index)) or {}
            if m.get("src") == "el" and self.commit is not None:
                return self._click(m)
            href = str(m.get("href") or "")
            if m.get("src") == "el" and m.get("tag") == "a" and href.startswith("http"):
                from anyuse.execution.web_arch import forbidden_control
                if forbidden_control(m.get("text"), href):
                    # a link to the login, account, cart or checkout page: reading it changes nothing
                    return self._goto(href)
            if m.get("src") == "el" and m.get("tag") != "a":
                from anyuse.execution.web_arch import forbidden_control
                if forbidden_control(m.get("text"), href):
                    return self._click(m)
        if action.kind == T.Open.kind:
            from anyuse.execution.web_arch import forbidden_control
            here = obs.url or self.start_url
            u = urllib.parse.urljoin(here, action.url) if not action.url.startswith("http") else action.url
            if forbidden_control("", u):
                return self._goto(u)
        return super().dispatch(action, obs)

    def _goto(self, url: str) -> T.Outcome:
        HT.run_sync(self.page.goto(url), self.loop)
        HT.run_sync(self.settle(self.page), self.loop)
        after = HT.run_sync(self.page.url(), self.loop)
        if not self.history or self.history[-1] != after:
            self.history.append(after)
        return T.Outcome(True, how="goto", href=url, url_after=after)

    def _click(self, m: Dict[str, Any]) -> T.Outcome:
        res = HT.run_sync(act_and_settle(self.page, js_call(FLOW_CLICK_JS, int(m.get("i", -1)),
                                                            str(m.get("text") or ""))), self.loop)
        if not isinstance(res, dict) or not res.get("ok"):
            return T.Outcome(False, f"click failed: {(res or {}).get('why') if isinstance(res, dict) else res}")
        HT.run_sync(self.settle(self.page), self.loop)
        after = HT.run_sync(self.page.url(), self.loop)
        if not self.history or self.history[-1] != after:
            self.history.append(after)
        return T.Outcome(True, how="click", url_after=after)

    def _set(self, f: Dict[str, Any], value: str) -> Dict[str, Any]:
        want = {"name": f.get("name") or "", "id": f.get("id") or ""}
        r = HT.run_sync(self.page.js(js_call(FLOW_FILL_JS, int(f["k"]), want, value)), self.loop)
        return r if isinstance(r, dict) else {"ok": False, "why": "no answer"}

    def _type_secret(self, f: Dict[str, Any], secret: str) -> bool:
        want = {"name": f.get("name") or "", "id": f.get("id") or ""}
        if not HT.run_sync(self.page.js(js_call(FLOW_FOCUS_JS, int(f["k"]), want)), self.loop):
            return False
        HT.run_sync(self.page.type(secret), self.loop)
        n = HT.run_sync(self.page.js(js_call(FIELD_LEN_JS, int(f["k"]))), self.loop)
        return isinstance(n, int) and n == len(secret)

    def _ask_otp(self, f: Dict[str, Any], url: str) -> str:
        if self.ask is None:
            return ""
        from anyuse.execution.request_policy import origin
        q = CP.Question(CP.OTP, "the site asks for a one-time code (two-step verification)",
                        CP.Commit(CP.LOGIN, label=str(f.get("label") or "one-time code"), target=self.site,
                                  site=origin(url) or self.site, url=url),
                        prompt=str(f.get("label") or "one-time code")[:80])
        ans = self.ask(q)
        code = re.sub(r"\s", "", str(getattr(ans, "code", "") or ""))
        if not re.fullmatch(r"[0-9A-Za-z]{4,10}", code):
            return ""
        self.secrets.note_otp(code)
        return code

    def _fill(self, action: T.Fill, obs: T.Observation) -> T.Outcome:
        filled: Dict[str, str] = {}
        issues: List[str] = []
        forms = self.forms()
        touched = set()
        for key, raw in action.fields.items():
            f = self._field(int(key), obs)
            if f is None:
                issues.append(f"element {key} is not a form field")
                continue
            form_key = str(f.get("form") or "")
            form = forms.get(form_key) or {}
            label = str(f.get("label") or f.get("name") or key)
            value = str(raw)
            kind = field_kind(f)
            exp: Dict[str, Any] = {}
            if kind == "one-time code field":
                code = self._ask_otp(f, obs.url)
                if not code:
                    return T.Outcome(False, "the caller gave no one-time code", how="fill", status="otp_unanswered")
                if not self._type_secret(f, code):
                    issues.append(f"{label}: the code could not be typed")
                    continue
                filled[label] = SR.OTP_REF
                touched.add(form_key)
                continue
            if SR.refs_in(value):
                if not SR.is_ref(value):
                    issues.append(f"{label}: a secret reference must be the whole value")
                    continue
                login_form = bool(form.get("has_password")) and str(form.get("method") or "") == "post"
                if not login_form:
                    issues.append(f"{label}: a secret is typed only into a login form posted to this site")
                    continue
                name = SR.refs_in(value)[0]
                try:
                    secret = self.secrets.resolve(name)
                except SR.SecretUnavailable as exc:
                    issues.append(str(exc))
                    continue
                if not self._type_secret(f, secret):
                    issues.append(f"{label}: the field does not hold what was typed")
                    continue
                exp = {"ref": SR.ref(name), "length": len(secret)} if f.get("type") == "password" else \
                    {"value": secret, "shown": SR.ref(name)}
                filled[label] = SR.ref(name)
            elif f.get("type") == "password":
                issues.append(f"{label}: a password field takes a ${{secret:NAME}} reference (secrets by reference)")
                continue
            else:
                if f.get("type") == "date":
                    value = norm_date(value)
                r = self._set(f, value)
                if not r.get("ok"):
                    issues.append(f"{label}: {r.get('why') or 'not set'} ({value[:40]!r})")
                    continue
                if f.get("type") == "checkbox":
                    exp = {"checked": CP.fold(value) in _YES}
                elif f.get("type") == "radio":
                    exp = {"radio": str(r.get("value") or value)}
                else:
                    exp = {"value": str(r.get("value") if r.get("value") is not None else value), "shown": value}
                filled[label] = value
            self.filled.setdefault(form_key, {})[str(f["k"])] = exp
            touched.add(form_key)
        readback = {}
        for fk in touched:
            shown, probs = self.readback(fk)
            readback.update(shown)
            issues += probs
        return T.Outcome(not issues, "; ".join(issues)[:400], how="fill",
                         url_after=str((obs.page or {}).get("url") or obs.url),
                         data={"filled": filled, "readback": readback})

    def _credentials(self) -> Tuple[str, str]:
        user = next((self.values[k] for k in ("username", "user", "login", "email", "benutzername", "benutzer")
                     if self.values.get(k)), "")
        pw = next((self.values[k] for k in ("password", "passwort", "pw", "pass", "kennwort") if self.values.get(k)), "")
        return user, pw

    def _login_form(self) -> Optional[Dict[str, Any]]:
        return next((f for f in self.flow.get("forms") or [] if f.get("has_password")), None)

    def _login(self, obs: T.Observation) -> T.Outcome:
        user, pw = self._credentials()
        if not SR.is_ref(pw):
            return T.Outcome(False, "the login action needs the values username and password=${secret:NAME} "
                                    "(secrets by reference)", how="login")
        self._read_flow()
        form = self._login_form()
        if form is None:
            link = next((e for e in obs.elements if (obs.mapping.get(e.index) or {}).get("tag") == "a" and
                         (_LOGIN_LINK.search(e.text) or _LOGIN_PATH.search(_path(e.href)))), None)
            if link is None:
                if any((obs.mapping.get(e.index) or {}).get("tag") == "a" and
                       re.search(r"\b(?:log ?out|sign ?out|abmelden|ausloggen)\b", e.text or "", re.I)
                       for e in obs.elements):
                    # already signed in (a logout link and no login form): nothing to send
                    return T.Outcome(True, "already signed in (a log-out link is on the page)", how="login",
                                     url_after=obs.url, data={"already": True})
                return T.Outcome(False, "no login form and no login link on this page", how="login")
            self._goto(link.href)
            self._read_flow()
            form = self._login_form()
            if form is None:
                return T.Outcome(False, "the login link led to no login form", how="login")
        key = str(form.get("key") or "")
        fields = [f for f in self.flow.get("fields") or [] if f.get("form") == key]
        pwf = next((f for f in fields if f.get("type") == "password"), None)
        userf = next((f for f in fields if f.get("type") in ("text", "email") and
                      (f.get("autocomplete") in ("username", "email") or _USER.search(
                          " ".join(str(f.get(x) or "") for x in ("name", "id", "label"))))), None) or \
            next((f for f in fields if f.get("type") in ("text", "email")), None)
        if pwf is None:
            return T.Outcome(False, "the login form has no password field", how="login")
        if userf is not None and user:
            r = self._set(userf, user)
            if not r.get("ok"):
                return T.Outcome(False, f"the username field: {r.get('why')}", how="login")
            self.filled.setdefault(key, {})[str(userf["k"])] = {"value": user, "shown": user}
        name = SR.refs_in(pw)[0]
        try:
            secret = self.secrets.resolve(name)
        except SR.SecretUnavailable as exc:
            return T.Outcome(False, str(exc), how="login")
        if not self._type_secret(pwf, secret):
            return T.Outcome(False, "the password field does not hold what was typed", how="login")
        self.filled.setdefault(key, {})[str(pwf["k"])] = {"ref": SR.ref(name), "length": len(secret)}
        sub = next((s for s in form.get("submits") or [] if int(s.get("ci", -1)) >= 0), None)
        if sub is None:
            return T.Outcome(False, "the login form has no submit button", how="login")
        out = self._click({"i": sub["ci"], "text": sub.get("text") or ""})
        if not out.ok:
            return T.Outcome(False, out.why, how="login")
        asked_otp = False
        self._read_flow()
        otp = next((f for f in self.flow.get("fields") or [] if field_kind(f) == "one-time code field"), None)
        if otp is not None:
            asked_otp = True
            code = self._ask_otp(otp, out.url_after)
            if not code:
                return T.Outcome(False, "the caller gave no one-time code", how="login", status="otp_unanswered")
            if not self._type_secret(otp, code):
                return T.Outcome(False, "the code field does not hold the code", how="login")
            oform = next((f for f in self.flow.get("forms") or [] if f.get("key") == otp.get("form")), {})
            osub = next((s for s in oform.get("submits") or [] if int(s.get("ci", -1)) >= 0), None)
            if osub is None:
                return T.Outcome(False, "the code form has no submit button", how="login")
            out = self._click({"i": osub["ci"], "text": osub.get("text") or ""})
            self._read_flow()
        still = self._login_form() is not None or any(field_kind(f) == "one-time code field"
                                                      for f in self.flow.get("fields") or [])
        url = out.url_after
        if still:
            return T.Outcome(False, "the site did not accept the login (the login form is still shown)", how="login",
                             url_after=url, data={"otp": asked_otp})
        return T.Outcome(True, how="login", url_after=url, data={"user": user, "otp": asked_otp,
                                                                 "password": SR.ref(name)})
