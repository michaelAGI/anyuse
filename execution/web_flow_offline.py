"""An OFFLINE page for open tasks: ``dom_page.DomPage`` (the demonstrator's readers
emulated on the parsed document) plus ``web_flow``'s readers and actions emulated the same way, over a site that a
callable answers - ``fetch(method, url, pairs) -> (status, final_url, html)`` with its own cookies and redirects
(the open-tasks test app in-process, the measurement harness). Forms are submitted as a browser
would: GET forms to their URL, POST forms with every named field, the submitter's name and value.

PURE apart from the injected ``fetch``; nothing here touches the network.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from anyuse.execution import demo_cards as K
from anyuse.execution import dom_page as D
from anyuse.execution import web_flow as WF

Fetch = Callable[[str, str, Optional[Sequence[Tuple[str, str]]]], Tuple[int, str, str]]
_BLOCK = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "form", "br",
          "ul", "ol", "header", "footer", "main", "nav", "fieldset", "legend", "dt", "dd", "aside", "td", "th"}
_SKIP = {"script", "style", "template", "noscript", "head", "title"}


def block_text(n: K.Node, limit: int = 20000) -> str:
    """``innerText``-like: blocks on their own lines, cells separated by a space."""
    out: List[str] = []

    def walk(x: Any) -> None:
        if sum(len(s) for s in out) > limit:
            return
        if isinstance(x, str):
            out.append(x)
            return
        if x.inert or x.tag in _SKIP or D.hidden(x):
            return
        if x.tag in _BLOCK and x.tag not in ("td", "th"):
            out.append("\n")
        for c in x.children:
            walk(c)
        if x.tag in ("td", "th"):
            out.append(" ")
        elif x.tag in _BLOCK:
            out.append("\n")
    walk(n)
    lines = [" ".join(l.split()) for l in "".join(out).split("\n")]
    return "\n".join(l for l in lines if l)[:limit]


class FlowSite(D.DomSite):
    """Pages answered by ``fetch`` (GET), never from a recording."""

    def __init__(self, fetch: Fetch):
        super().__init__([])
        self.fetch = fetch

    def route(self, url: str) -> Tuple[D.Recorded, str]:
        status, final, html_text = self.fetch("GET", url, None)
        return D.Recorded(final, html_text, {}, status), final


class FlowDomPage(D.DomPage):
    """The duck-typed page ``web_flow.FlowBrowserTools`` drives, offline. ``sent``: every request it made."""

    def __init__(self, site: FlowSite, url: str = "about:blank"):
        super().__init__(site, url)
        self.overlay: Dict[int, Any] = {}
        self.focused: Optional[int] = None
        self.sent: List[Dict[str, Any]] = []

    # -- navigation -----------------------------------------------------------------------------
    async def goto(self, url: str) -> None:
        self.overlay, self.focused = {}, None
        url = urllib.parse.urljoin(self.cur_url, str(url)) if self.cur_url.startswith("http") else str(url)
        self.sent.append({"method": "GET", "url": url})
        await super().goto(url)

    def _post(self, url: str, pairs: List[Tuple[str, str]]) -> None:
        self.overlay, self.focused = {}, None
        self.sent.append({"method": "POST", "url": url, "fields": [k for k, _v in pairs]})
        status, final, html_text = self.site.fetch("POST", url, pairs)
        self.gotos.append(final)
        self.cur, self.cur_url = D.Recorded(final, html_text, {}, status), final

    # -- the document ---------------------------------------------------------------------------
    def _doc(self):
        root, base = K.parse(self.cur.dom)
        nodes = list(K.walk(root))
        return root, base, nodes

    @staticmethod
    def _type(n: K.Node) -> str:
        return (n.attrs.get("type") or "text").lower() if n.tag == "input" else n.tag

    def _label(self, n: K.Node, root: K.Node) -> str:
        lab = D._label_for(n, root)
        t = K.text_of(lab, 200) if lab is not None else ""
        t = t or n.attrs.get("aria-label") or n.attrs.get("placeholder") or n.attrs.get("title") or ""
        return " ".join((t or n.attrs.get("name") or n.attrs.get("id") or "").split())[:120]

    def _forms(self, nodes: List[K.Node]) -> List[K.Node]:
        return [n for n in nodes if n.tag == "form"]

    def _key(self, f: Optional[K.Node], forms: List[K.Node]) -> str:
        if f is None:
            return ""
        if f.attrs.get("id"):
            return "#" + f.attrs["id"]
        if f.attrs.get("name"):
            return "name=" + f.attrs["name"]
        return "form" + str(forms.index(f))

    def _value(self, k: int, n: K.Node) -> Any:
        if k in self.overlay:
            return self.overlay[k]
        t = self._type(n)
        if t in ("checkbox", "radio"):
            return "checked" in n.attrs
        if n.tag == "select":
            opts = [o for o in K.walk(n) if o.tag == "option"]
            sel = next((o for o in opts if "selected" in o.attrs), opts[0] if opts else None)
            return sel.attrs.get("value", K.text_of(sel, 80)) if sel is not None else ""
        if n.tag == "textarea":
            return K.text_of(n, 2000)
        return n.attrs.get("value", "")

    def _fields(self, root: K.Node, nodes: List[K.Node]) -> List[Tuple[int, K.Node]]:
        return [(k, n) for k, n in enumerate([x for x in nodes if x.tag in ("input", "textarea", "select")])]

    def _flow_read(self) -> Dict[str, Any]:
        root, base, nodes = self._doc()
        controls = K.control_nodes(root)
        inputs = [n for n in nodes if n.tag == "input"]
        forms = self._forms(nodes)
        fields = []
        for k, n in self._fields(root, nodes):
            t = self._type(n)
            if t in ("hidden", "submit", "button", "image", "reset", "file"):
                continue
            if D.hidden(n) and t not in ("checkbox", "radio"):
                continue
            f = D.form_of(n)
            rec: Dict[str, Any] = {"k": k, "ii": inputs.index(n) if n.tag == "input" else -1,
                                   "ci": controls.index(n) if n in controls else -1, "tag": n.tag, "type": t,
                                   "name": n.attrs.get("name", ""), "id": n.attrs.get("id", ""),
                                   "label": self._label(n, root), "required": "required" in n.attrs,
                                   "autocomplete": (n.attrs.get("autocomplete") or "").lower(),
                                   "inputmode": n.attrs.get("inputmode", ""),
                                   "maxlength": int(n.attrs.get("maxlength") or 0) if
                                   str(n.attrs.get("maxlength") or "").isdigit() else 0,
                                   "form": self._key(f, forms), "disabled": "disabled" in n.attrs}
            v = self._value(k, n)
            if t == "password":
                rec.update(filled=bool(v), length=len(str(v or "")))
            elif t in ("checkbox", "radio"):
                rec.update(checked=bool(v), value=n.attrs.get("value", "on"))
            elif n.tag == "select":
                opts = [o for o in K.walk(n) if o.tag == "option"]
                rec["options"] = [{"value": o.attrs.get("value", K.text_of(o, 80)), "text": K.text_of(o, 80),
                                   "selected": o.attrs.get("value", K.text_of(o, 80)) == v} for o in opts][:60]
                rec["value"] = v
            else:
                rec["value"] = str(v or "")[:300]
            fields.append(rec)
        fmeta = []
        for f in forms:
            heading = ""
            lg = next((x for x in K.walk(f) if x.tag in ("h1", "h2", "h3")), None) or \
                next((x for x in f.elements() if x.tag == "legend"), None)
            if lg is not None:
                heading = K.text_of(lg, 120)
            if not heading and f.parent is not None:
                sib = [c for c in f.parent.elements()]
                i = sib.index(f)
                for p in reversed(sib[max(0, i - 3):i]):
                    if p.tag in ("h1", "h2", "h3"):
                        heading = K.text_of(p, 120)
                        break
            if not heading:
                x = f.parent
                while x is not None and x.tag not in ("section", "article", "main") and not x.tag.startswith("#"):
                    x = x.parent
                if x is not None and not x.tag.startswith("#"):
                    h = next((y for y in K.walk(x) if y.tag in ("h1", "h2", "h3")), None)
                    heading = K.text_of(h, 120) if h is not None else ""
            subs = [x for x in K.walk(f) if (x.tag == "button" and (x.attrs.get("type") or "submit").lower()
                                             not in ("button", "reset")) or
                    (x.tag == "input" and (x.attrs.get("type") or "").lower() in ("submit", "image"))]
            act = f.attrs.get("action")
            fmeta.append({"key": self._key(f, forms), "id": f.attrs.get("id", ""), "name": f.attrs.get("name", ""),
                          "action": urllib.parse.urljoin(self.cur_url, act) if act else self.cur_url,
                          "method": (f.attrs.get("method") or "get").lower(),
                          "has_password": any(x.tag == "input" and (x.attrs.get("type") or "").lower() == "password"
                                              for x in K.walk(f)),
                          "heading": " ".join(heading.split())[:120],
                          "submits": [{"ci": controls.index(x) if x in controls else -1,
                                       "text": (K.text_of(x, 80) or x.attrs.get("value", "")).strip()[:80]}
                                      for x in subs]})
        buttons = [{"ci": i, "form": self._key(D.form_of(c), forms)} for i, c in enumerate(controls)
                   if D.form_of(c) is not None]
        cap = next((n for n in nodes if re.search(r"g-recaptcha|h-captcha|cf-turnstile", n.attrs.get("class") or "")
                    or "data-sitekey" in n.attrs or (n.tag == "iframe" and re.search(
                        r"recaptcha|hcaptcha|challenges\.cloudflare", n.attrs.get("src") or ""))
                    or (n.tag == "input" and "captcha" in (n.attrs.get("name") or "").lower())), None)
        text = block_text(root)
        captcha = ("widget " + (cap.attrs.get("class") or cap.tag))[:80] if cap is not None else \
            ("text: not a robot" if re.search(r"i'?m not a robot|ich bin kein roboter|verify you are human|"
                                              r"confirm that you are not a robot", text, re.I) else "")
        rows = []
        for r in nodes:
            if r.tag not in ("tr", "li") or D.hidden(r):
                continue
            t = K.text_of(r, 400)
            if not t or not re.search(r"\d", t):
                continue
            cells = [K.text_of(c, 200) for c in r.elements()] if r.tag == "tr" else [t]
            box = r.parent
            while box is not None and box.tag not in ("table", "ul", "ol", "section", "article", "div") \
                    and not box.tag.startswith("#"):
                box = box.parent
            ctx = ((box.attrs.get("id", "") + " " + box.attrs.get("class", "")) if box is not None and
                   not box.tag.startswith("#") else "")
            sec = r.parent
            while sec is not None and sec.tag not in ("section", "article", "main") and not sec.tag.startswith("#"):
                sec = sec.parent
            if sec is not None and not sec.tag.startswith("#"):
                h = next((y for y in K.walk(sec) if y.tag in ("h1", "h2", "h3")), None)
                if h is not None:
                    ctx += " " + K.text_of(h, 120)
            rows.append({"cells": cells[:8], "ctx": " ".join(ctx.split())[:160]})
        h1 = [K.text_of(n, 200) for n in nodes if n.tag == "h1" and not D.hidden(n)][:2]
        title = next((K.text_of(n, 200) for n in K.walk(root) if n.tag == "title"), "")
        return {"url": self.cur_url, "title": title, "h1": h1, "fields": fields, "forms": fmeta, "buttons": buttons,
                "captcha": captcha, "rows": rows[:120], "text": text}

    def _field_node(self, k: int) -> Optional[K.Node]:
        root, base, nodes = self._doc()
        fs = [x for x in nodes if x.tag in ("input", "textarea", "select")]
        return fs[k] if 0 <= k < len(fs) else None

    def _fill(self, k: int, want: Dict[str, str], value: str) -> Dict[str, Any]:
        root, base, nodes = self._doc()
        fs = [x for x in nodes if x.tag in ("input", "textarea", "select")]
        n = fs[k] if 0 <= k < len(fs) else None
        if n is None:
            return {"ok": False, "why": "gone"}
        if (want.get("name") and n.attrs.get("name") != want["name"]) or (want.get("id") and n.attrs.get("id") != want["id"]):
            return {"ok": False, "why": "changed"}
        t = self._type(n)
        yes = str(value).strip().lower() in WF._YES
        norm = lambda s: " ".join(str(s or "").split()).lower()  # noqa: E731
        if t == "checkbox":
            self.overlay[k] = yes
            return {"ok": True, "checked": yes}
        if t == "radio":
            fn = D.form_of(n)
            group = [(i, x) for i, x in enumerate(fs) if self._type(x) == "radio"
                     and x.attrs.get("name") == n.attrs.get("name") and D.form_of(x) is fn]
            pick = (k, n) if yes else next(((i, x) for i, x in group if norm(x.attrs.get("value")) == norm(value)),
                                           None) or next(((i, x) for i, x in group
                                                          if norm(self._label(x, root)) == norm(value)), None)
            if pick is None:
                return {"ok": False, "why": "no such choice"}
            for i, _x in group:
                self.overlay[i] = i == pick[0]
            return {"ok": True, "value": pick[1].attrs.get("value", "on")}
        if n.tag == "select":
            opts = [o for o in K.walk(n) if o.tag == "option"]
            o = next((x for x in opts if norm(K.text_of(x, 80)) == norm(value)), None) or \
                next((x for x in opts if norm(x.attrs.get("value", "")) == norm(value)), None) or \
                next((x for x in opts if value and norm(value) in norm(K.text_of(x, 80))), None)
            if o is None:
                return {"ok": False, "why": "no such option"}
            v = o.attrs.get("value", K.text_of(o, 80))
            self.overlay[k] = v
            return {"ok": True, "value": v, "text": K.text_of(o, 80)}
        self.overlay[k] = str(value)
        return {"ok": True, "value": str(value)[:300]}

    def _values(self, key: str) -> List[Dict[str, Any]]:
        root, base, nodes = self._doc()
        forms = self._forms(nodes)
        out = []
        for k, n in self._fields(root, nodes):
            if self._key(D.form_of(n), forms) != key:
                continue
            t = self._type(n)
            if t in ("hidden", "submit", "button", "image", "reset", "file"):
                continue
            v = self._value(k, n)
            f: Dict[str, Any] = {"k": k, "name": n.attrs.get("name", ""), "type": t}
            if t == "password":
                f.update(filled=bool(v), length=len(str(v or "")))
            elif t in ("checkbox", "radio"):
                f.update(checked=bool(v), value=n.attrs.get("value", "on"))
            elif n.tag == "select":
                opt = next((o for o in K.walk(n) if o.tag == "option" and o.attrs.get("value", K.text_of(o, 80)) == v), None)
                f.update(value=v, text=K.text_of(opt, 80) if opt is not None else "")
            else:
                f["value"] = str(v or "")[:300]
            out.append(f)
        return out

    def _click(self, ci: int, want: str) -> Dict[str, Any]:
        root, base, nodes = self._doc()
        controls = K.control_nodes(root)
        if not 0 <= ci < len(controls):
            return {"ok": False, "why": "gone"}
        n = controls[ci]
        t = (n.attrs.get("aria-label") or K.text_of(n, 200) or n.attrs.get("title") or n.attrs.get("value", "")).strip()
        if want and t and t[:30].lower() != want[:30].lower():
            return {"ok": False, "why": "changed", "text": t[:60]}
        self.acts.append({"i": ci, "tag": n.tag, "flow": True})
        if n.tag == "a" and n.attrs.get("href"):
            import asyncio as _a  # noqa: F401 - the caller awaits js(); goto is applied synchronously here
            url = K.href_of(n, self.cur_url, base)
            self.overlay, self.focused = {}, None
            self.sent.append({"method": "GET", "url": url})
            self.gotos.append(url)
            self.cur, self.cur_url = self.site.route(url)
            return {"ok": True}
        f = D.form_of(n)
        typ = (n.attrs.get("type") or ("submit" if n.tag == "button" else "")).lower()
        if f is not None and ((n.tag == "button" and typ not in ("button", "reset")) or
                              (n.tag == "input" and typ in ("submit", "image"))):
            self._submit(f, n, root, base, nodes)
            return {"ok": True}
        if n.tag == "input" and typ in ("checkbox", "radio"):
            fs = [x for x in nodes if x.tag in ("input", "textarea", "select")]
            k = fs.index(n)
            self._fill(k, {}, "yes" if not self._value(k, n) or typ == "radio" else "no")
            return {"ok": True}
        return {"ok": True}

    def _submit(self, form: K.Node, submitter: K.Node, root, base, nodes) -> None:
        fs = [x for x in nodes if x.tag in ("input", "textarea", "select")]
        pairs: List[Tuple[str, str]] = []
        for k, x in enumerate(fs):
            if D.form_of(x) is not form or not x.attrs.get("name") or "disabled" in x.attrs:
                continue
            t = self._type(x)
            if t in ("submit", "button", "image", "reset", "file"):
                continue
            v = self._value(k, x)
            if t in ("checkbox", "radio"):
                if v:
                    pairs.append((x.attrs["name"], x.attrs.get("value", "on")))
                continue
            pairs.append((x.attrs["name"], str(v or "")))
        if submitter.attrs.get("name"):
            pairs.append((submitter.attrs["name"], submitter.attrs.get("value", "")))
        act = form.attrs.get("action")
        url = urllib.parse.urljoin(self.cur_url, act) if act else self.cur_url
        if (form.attrs.get("method") or "get").lower() == "post":
            self._post(url, pairs)
        else:
            sp = urllib.parse.urlsplit(url)
            target = urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, urllib.parse.urlencode(pairs), ""))
            self.overlay, self.focused = {}, None
            self.sent.append({"method": "GET", "url": target})
            self.gotos.append(target)
            self.cur, self.cur_url = self.site.route(target)

    # -- the page surface -----------------------------------------------------------------------
    async def js(self, expr: str, read_only: bool = False) -> Any:
        from anyuse.execution.typed_readiness import SETTLE_PROBE_JS
        e = str(expr)
        if "/*anyuse:flow_read*/" in e:
            return self._flow_read()
        if "/*anyuse:flow_fill*/" in e:
            k, want, value = WF.js_args(e)
            return self._fill(int(k), dict(want or {}), str(value))
        if "/*anyuse:flow_focus*/" in e:
            k, want = WF.js_args(e)
            n = self._field_node(int(k))
            if n is None or (want.get("name") and n.attrs.get("name") != want["name"]):
                return False
            self.focused = int(k)
            self.overlay[int(k)] = ""
            return True
        if "/*anyuse:flow_values*/" in e:
            return self._values(str(WF.js_args(e)[0]))
        if "/*anyuse:flow_click*/" in e:
            ci, want = WF.js_args(e)
            return self._click(int(ci), str(want or ""))
        if "/*anyuse:flow_len*/" in e:
            k = int(WF.js_args(e)[0])
            return len(str(self.overlay.get(k, "") or ""))
        if e == SETTLE_PROBE_JS:
            return {"ready": "complete", "quiet_ms": 100000}
        return await super().js(expr, read_only)

    async def type(self, text: str) -> None:
        if self.focused is not None:
            self.overlay[self.focused] = str(self.overlay.get(self.focused) or "") + str(text)

    async def focus(self, index: int) -> bool:
        return True
