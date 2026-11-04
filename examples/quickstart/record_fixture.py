"""Record the fixture search three times, in AnyUse's recording format.

On a real site this step is a HAR export from your browser or `anyuse learn`
(docs/QUICKSTART_COMPILE.md §1), or `anyuse attach` observing a browser another
agent drives (docs/ATTACH.md). Neither is available offline in a bare
container, so this stand-in does what the browser would have done on the
fixture page: load ``/search?q=<value>``, let the page's one script load
``/api/search?q=<value>``, and note which result labels were on screen.

It writes ``<name>.rec.json`` (the one the compiler may look at) and
``<name>.fresh1.json`` / ``<name>.fresh2.json`` (the held-out inputs the proof
replays; the compiler never fits anything to them).

    python record_fixture.py [--page-only] https://127.0.0.1:8443 OUTDIR NAME VALUE1 VALUE2 VALUE3

``--page-only`` keeps only the results page in each recording (no API call), so
the compiler can only compile a request that reads the page itself.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request


def fetch(url: str, ctx):
    with urllib.request.urlopen(url, context=ctx, timeout=10) as resp:
        body = resp.read().decode()
        return {"url": url, "method": "GET", "status": resp.status,
                "mime": resp.headers.get("Content-Type", "").split(";")[0],
                "bytes": len(body), "body": body}


def record(origin: str, name: str, value: str, tag: str, ctx, page_only: bool = False):
    page_url = f"{origin}/search?q={urllib.parse.quote(value)}"
    page = fetch(page_url, ctx)
    # What the page's script requests (see fixture_site.py): the browser would
    # have made exactly this call.
    api = fetch(f"{origin}/api/search?q={urllib.parse.quote(value)}", ctx)
    items = [re.sub(r"<[^>]+>", "", m) for m in
             re.findall(r'<li class="(?:result|hit)">(.*?)</li>', page["body"])]
    if not items and "<ul id='results'></ul>" in page["body"]:
        # the server sent an empty list and the page's script fills it from the
        # API response: these are the names a browser shows
        data = json.loads(api["body"])
        items = [row.get("name") or row.get("title")
                 for row in data.get("results") or data.get("packages") or []]
    text = re.sub(r"<script.*?</script>", "", page["body"], flags=re.S)
    text = "\n".join(t for t in re.sub(r"<[^>]+>", "\n", text).split("\n") if t.strip())
    if items and not any(item in text for item in items):
        text = "\n".join([text, *items])
    return {"_note": "Recorded from the offline quickstart fixture (examples/quickstart).",
            "workflow": name, "tag": tag, "site": urllib.parse.urlsplit(origin).netloc,
            "param": {"name": "q", "value": value}, "final_url": page_url,
            "ui": {"items": items, "text": text},
            "network": [page] if page_only else [page, api]}


def main(argv):
    page_only = "--page-only" in argv
    argv = [a for a in argv if a != "--page-only"]
    origin, outdir, name, *values = argv
    if len(values) != 3:
        raise SystemExit("give exactly three values: one to compile from, two held out")
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))
    os.makedirs(outdir, exist_ok=True)
    for value, tag in zip(values, ["rec", "fresh1", "fresh2"]):
        rec = record(origin, name, value, tag, ctx, page_only=page_only)
        path = os.path.join(outdir, f"{name}.{tag}.json")
        with open(path, "w") as fh:
            json.dump(rec, fh, indent=1)
        print(f"recorded {value!r:10} -> {path}  ({len(rec['ui']['items'])} results on screen)")


if __name__ == "__main__":
    main(sys.argv[1:])
