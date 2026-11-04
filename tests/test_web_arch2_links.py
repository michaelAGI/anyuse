"""An image-only link or a link to a file is never followed as item
navigation; the title link of the result row is taken. Offline.

On www.debbieblissonline.com the map resolved "British Wool Aran" to a
link whose target was an image file (/wp-content/uploads/.../My-project-7.jpg),
and the run ended on a raw picture.

What must hold:

1. the page reader marks a link with no visible text but a picture
   (``img_only``); ``item_link`` refuses such links and links to files
   (images, PDFs, assets) as items;
2. the result region, the content links and so the item map never offer
   them; a row's title link is resolved instead, also when the image link
   carries the item's name as its alt text;
3. navigation never follows a link to a file.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anyuse.execution import web_arch as W  # noqa: E402
from anyuse.execution import web_arch_run as R  # noqa: E402
from test_web_arch import BASE, HOST, a  # noqa: E402


def img(i, text, href, area="main"):
    e = a(i, text, href, area)
    e["img_only"] = True
    return e


def test_the_reader_marks_image_only_links():
    assert "e.img_only = true" in R.READ_JS and "innerText" in R.READ_JS


def test_files_and_image_only_links_are_never_items():
    for h in ("/wp-content/uploads/2023/03/My-project-7.jpg", "/media/x.PNG", "/files/catalogue.pdf",
              "/img/a.webp?w=300"):
        assert W.asset_href(BASE + h), h
        assert not W.item_link(a(1, "British Wool Aran", h))
    assert not W.asset_href(BASE + "/product/yarn/british-wool-aran/")
    assert W.item_link(a(1, "British Wool Aran", "/product/yarn/british-wool-aran/"))
    assert not W.item_link(img(1, "British Wool Aran", "/product/yarn/british-wool-aran/"))


def test_prior_pattern_the_image_file_is_not_the_item():
    read = {"url": BASE + "/product/yarn/", "elements": [
        a(1, "Yarn", "/product/yarn/", "nav"),
        a(2, "British Wool Aran", "/wp-content/uploads/2023/03/My-project-7.jpg", "main"),
        a(3, "British Wool Aran - 50g", "/product/yarn/british-wool-aran/", "main"),
        a(4, "Cashmerino Aran", "/product/yarn/cashmerino-aran/", "main")]}
    el, short, how = W.resolve_item("British Wool Aran", read, "", HOST)
    assert el is not None and el["href"].endswith("/product/yarn/british-wool-aran/"), (el, how)


def test_the_rows_title_link_wins_over_its_image_link():
    rows = []
    for k, name in enumerate(("Merino Crew Sock", "Merino Hiking Sock", "Cotton Ankle Sock")):
        slug = name.lower().replace(" ", "-")
        rows += [img(10 + 2 * k, name, f"/products/{slug}?img=1"), a(11 + 2 * k, name, f"/products/{slug}")]
    read = {"url": BASE + "/search?q=sock", "elements": rows}
    dom = "<html><body><main><ul>" + "".join(
        f'<li><a href="{e["href"]}">{e["text"]}</a></li>' for e in rows) + "</ul></main></body></html>"
    region, how = W.result_links(read, dom, HOST)
    assert region and all(not e.get("img_only") for e in region), region
    el, short, how = W.resolve_item("Merino Hiking Sock", read, dom, HOST)
    assert el["href"] == BASE + "/products/merino-hiking-sock", el
    assert all(not c.get("img_only") for c in W.content_links(read, HOST))


def test_navigation_never_follows_a_file():
    read = {"url": BASE + "/", "elements": [a(1, "Size guide", "/files/size-guide.pdf", "nav"),
                                            a(2, "Size guide", "/pages/size-guide", "footer")]}
    cands = W.nav_candidates(read, HOST)
    assert [c["href"] for c in cands] == [BASE + "/pages/size-guide"]


if __name__ == "__main__":
    import inspect
    import traceback
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if any(x.default is inspect.Parameter.empty for x in inspect.signature(fn).parameters.values()):
            continue
        try:
            fn()
            p += 1
            print("OK  ", n)
        except Exception as e:                            # noqa: BLE001
            f += 1
            print("FAIL", n, "::", repr(e)[:400])
            traceback.print_exc(limit=6)
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
