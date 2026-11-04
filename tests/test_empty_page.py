"""The site's own empty page (execution/empty_page.py): learning it and matching it.

Pure: pages are strings, a fetch is a function. Nothing opens a socket and no
model is asked. The sites are synthetic, in languages the statement rule's
lists do not cover, so every EMPTY here comes from the site's own pages.
"""
import sys
import time

import pytest

from anyuse.execution import empty_page as EP
from anyuse.execution import result_contract as RC
from anyuse.execution.html_extract import extract

SEL = "ul.hits li.hit"


def shell(body, promo="Khuyến mãi mùa hè"):
    return ("<html><body><header><nav><a href='/'>Trang chủ</a></nav></header>"
            f"<main><h1>Tìm kiếm</h1>{body}</main><aside class='promo'><p>{promo}</p></aside>"
            "<footer><p>Liên hệ hỗ trợ khách hàng</p></footer></body></html>")


def results(q, names=("Ấm", "Bình", "Chén"), promo="Khuyến mãi mùa hè"):
    rows = "".join(f"<li class='hit'><a href='/p/{n}'>{n} {q}</a></li>" for n in names)
    return shell(f"<p class='sum'>Kết quả cho “{q}”</p><ul class='hits'>{rows}</ul>", promo)


def empty(q, promo="Khuyến mãi mùa hè", extra=""):
    return shell(f"<p class='msg'>Không tìm thấy sản phẩm nào cho “{q}”.</p>{extra}", promo)


def page(markup, q, **kw):
    return EP.Page("html", q, markup=markup, items=extract(markup, SEL), **kw)


def learned(empty_page=empty, result_page=results):
    res = [page(result_page("alpha"), "alpha"), page(result_page("beta"), "beta")]
    vals = EP.nonsense_values(["alpha", "beta"], seed="https://shop.vn/s?q={q}")
    promos = ["Giảm giá cuối tuần", "Quà tặng hôm nay"]
    probes = [page(empty_page(v, promo=p), v) for v, p in zip(vals, promos)]
    return EP.learn("html", res, probes)


def fetch_from(fn):
    sent = []

    def fetch(v):
        sent.append(v)
        return page(fn(v), v)
    fetch.sent = sent
    return fetch


# ---------------------------------------------------------------------------
# made-up inputs and masking
# ---------------------------------------------------------------------------

def test_made_up_inputs_are_deterministic_and_share_no_word_with_the_real_ones():
    a = EP.nonsense_values(["alpha", "beta"], seed="https://x.test/s")
    assert a == EP.nonsense_values(["alpha", "beta"], seed="https://x.test/s")
    assert len(a) == 2 and a[0] != a[1]
    assert a != EP.nonsense_values(["alpha", "beta"], seed="https://y.test/s")
    assert not {w for v in a for w in v.split()} & {"alpha", "beta"}


def test_made_up_inputs_follow_the_shape_of_the_real_ones():
    digits = EP.nonsense_values(["10115", "80331"], seed="s")
    assert all(v.isdigit() and len(v) == 5 for v in digits)
    paths = EP.nonsense_values(["guzzlehttp/guzzle", "symfony/console"], seed="s")
    assert all(v.count("/") == 1 and all(p.isalpha() for p in v.split("/")) for v in paths)
    upper = EP.nonsense_values(["Paris", "Berlin"], seed="s")
    assert all(v[0].isupper() for v in upper)


def test_the_mask_replaces_the_input_in_any_case_form_and_masks_digits():
    # accents of letters with case are dropped (api_compile.normalise)
    assert EP.mask("Không tìm thấy “ZZQX”!", "zzqx") == "khong tim thay {q}"
    # a decomposed accent is the same input
    assert EP.mask("Sonuç yok: Café", "Café") == "sonuc yok {q}"
    assert EP.mask("0 kết quả", "x") == "0 ket qua"
    assert EP.mask("12 kết quả, trang 3", "x") == "# ket qua trang #"
    assert EP.mask("000 results", "x") == "0 results"


# ---------------------------------------------------------------------------
# learning an HTML empty page
# ---------------------------------------------------------------------------

def test_the_empty_page_of_a_vietnamese_shop_is_learned():
    sig = learned()
    assert sig["kind"] == "html" and EP.usable(sig)
    assert sig["marks"] == ["khong tim thay san pham nao cho {q}"]
    assert sig["echo"] is True
    # the footer stands on every page; the promo changes between the two loads
    footer = EP._h(EP.mask("Liên hệ hỗ trợ khách hàng", ""))
    assert footer in sig["shared"]
    promo = [ln.path for ln in EP.lines(page(empty("x", promo="Giảm giá cuối tuần"), "x"))
             if ln.raw == "Giảm giá cuối tuần"][0]
    assert promo in sig["volatile"]


def test_a_site_that_lists_results_for_a_made_up_input_has_no_empty_page():
    res = [page(results("alpha"), "alpha")]
    probes = [page(results(v), v) for v in ("zorqua", "vexlim")]
    sig = EP.learn("html", res, probes)
    assert sig["kind"] == "none" and "made-up input" in sig["why"]


def test_a_made_up_search_without_an_answer_learns_nothing_yet():
    res = [page(results("alpha"), "alpha")]
    probes = [page(empty("zorqua"), "zorqua"), EP.Page("html", "vexlim", ok=False, error="timeout")]
    assert EP.learn("html", res, probes) is None
    # a 404 is the site's own answer: nothing to learn, for good
    probes[1] = EP.Page("html", "vexlim", ok=False, definite=True, status=404)
    assert EP.learn("html", res, probes)["kind"] == "none"


# ---------------------------------------------------------------------------
# matching an HTML empty page
# ---------------------------------------------------------------------------

def test_the_real_empty_page_for_a_new_input_is_empty_where_the_statement_rule_says_nothing():
    sig = learned()
    p = page(empty("zzqx", promo="Ưu đãi đặc biệt"), "zzqx")
    ev = EP.match(sig, p)
    assert ev.state == EP.EMPTY and ev.by == EP.BY_SITE, ev.why
    assert ev.line == "Không tìm thấy sản phẩm nào cho “zzqx”."
    # a language outside every list: the statement rule finds nothing
    assert RC.empty_evidence_page(p.markup, "zzqx")[0] == ""


def test_a_page_without_the_empty_pages_line_is_left_to_the_next_step():
    sig = learned()
    broken = results("gamma").replace("class='hit'", "class='hit-v2'")
    ev = EP.match(sig, page(broken, "gamma"))
    assert ev.state == EP.UNKNOWN and "lacks the line" in ev.why


def test_the_empty_pages_line_beside_rows_the_selector_no_longer_reads_is_not_empty():
    sig = learned()
    rows = "<ul class='hits'><li class='hit-v2'><a href='/p/x'>Ấm gamma</a></li></ul>"
    p = page(empty("gamma", extra=rows), "gamma")
    assert p.items == []
    fetch = fetch_from(empty)
    ev = EP.match(sig, p, fetch=fetch)
    assert ev.state == EP.NOT_EMPTY and "line(s) the site's empty page lacks" in ev.why
    assert fetch.sent == [sig["probes"][0]]
    # without a request the same page is not decided here
    assert EP.match(sig, p).state == EP.UNKNOWN


def test_a_new_banner_on_every_page_needs_an_input_known_to_have_results():
    sig = learned()
    banner = "<div class='notice'><p>Miễn phí vận chuyển toàn quốc</p></div>"
    p = page(empty("zzqx", extra=banner), "zzqx")
    ok = RC.positive_ok_for(None)
    # the made-up search shows the banner too: the site's pages changed
    def fetch_now():
        return fetch_from(lambda v: results(v) if v == "alpha" else empty(v, extra=banner))
    assert EP.match(sig, p, fetch=fetch_now()).state == EP.UNKNOWN
    fetch = fetch_now()
    ev = EP.match(sig, p, fetch=fetch, positives=["alpha"], positive_ok=ok)
    assert ev.state == EP.EMPTY, ev.why
    assert fetch.sent == [sig["probes"][0], "alpha"]
    # an error on every page: the search finds nothing for that input either
    err = "<div class='alert'><p>Đã xảy ra lỗi, vui lòng thử lại sau</p></div>"
    ev = EP.match(sig, page(empty("zzqx", extra=err), "zzqx"),
                  fetch=fetch_from(lambda v: empty(v, extra=err)), positives=["alpha"],
                  positive_ok=ok)
    assert ev.state == EP.NOT_EMPTY and "finds none either" in ev.why


def test_a_page_that_is_not_the_empty_page_a_made_up_search_still_shows_is_marked():
    sig = learned()
    blank = page(shell("<p class='sum'>Kết quả cho “zzqx”</p>"), "zzqx")
    ev = EP.match(sig, blank, fetch=fetch_from(empty))
    assert ev.state == EP.UNKNOWN and ev.not_site_page is True
    # when the made-up search lacks the empty page's line too, the site's empty page changed
    ev = EP.match(sig, blank, fetch=fetch_from(lambda v: shell("<p>khác</p>")))
    assert ev.state == EP.UNKNOWN and ev.not_site_page is False


def test_lines_that_name_the_input_and_link_somewhere_read_as_result_rows():
    rows = "<ul class='hits-v2'><li><a href='/p/1'>Ấm zzqx</a></li><li><a href='/p/2'>Bình zzqx</a></li></ul>"
    p = page(shell(f"<p>Không có zzqx.</p>{rows}"), "zzqx")
    assert EP.unread_rows(p) == ["Ấm zzqx", "Bình zzqx"]
    assert EP.unread_rows(page(empty("zzqx"), "zzqx")) == []


URL = "https://shop.vn/s?q=chenn"
DYM = "<p class='goiy'>Có phải bạn muốn tìm <a href='/s?q=chen'>chén</a>?</p>"


def test_a_suggestion_for_another_search_is_not_a_line_the_empty_page_lacks():
    sig = learned()
    # an input near a real one: the site adds a "did you mean" no made-up search shows
    p = page(empty("chenn", extra=DYM), "chenn", url=URL)
    ev = EP.match(sig, p, fetch=fetch_from(empty))
    assert ev.state == EP.EMPTY and ev.line.startswith("Không tìm thấy"), ev.why
    # a line that links anywhere else is still a line the empty page lacks
    other = DYM.replace("/s?q=chen", "/p/chen")
    p = page(empty("chenn", extra=other), "chenn", url=URL)
    assert EP.match(sig, p, fetch=fetch_from(empty)).state == EP.NOT_EMPTY
    # so is a link to the search for this same input, and one to another page's search
    for href in ("/s?q=chenn&page=2", "/blog/s?q=chen", "https://other.vn/s?q=chen"):
        p = page(empty("chenn", extra=DYM.replace("/s?q=chen", href)), "chenn", url=URL)
        assert EP.match(sig, p, fetch=fetch_from(empty)).state == EP.NOT_EMPTY, href
    # without the page's address nothing says where the link goes
    p = page(empty("chenn", extra=DYM), "chenn")
    assert EP.match(sig, p, fetch=fetch_from(empty)).state == EP.NOT_EMPTY


def test_the_empty_pages_line_followed_by_a_suggestion_is_the_empty_page():
    sig = learned()
    inline = shell("<p class='msg'>Không tìm thấy sản phẩm nào cho “chenn”. Có phải bạn muốn "
                   "tìm <a href='/s?q=chen'>chén</a>?</p>")
    ev = EP.match(sig, page(inline, "chenn", url=URL), fetch=fetch_from(empty))
    assert ev.state == EP.EMPTY and "Có phải bạn muốn tìm" in ev.line, ev.why
    # the same sentence without the link is not the empty page a made-up search still shows
    plain = inline.replace("<a href='/s?q=chen'>chén</a>", "chén")
    ev = EP.match(sig, page(plain, "chenn", url=URL), fetch=fetch_from(empty))
    assert ev.state == EP.UNKNOWN and ev.not_site_page is True


def test_related_searches_that_name_the_input_are_not_result_rows():
    related = ("<ul class='lienquan'><li><a href='/s?q=chenn+xanh'>chenn xanh</a></li>"
               "<li><a href='/s?q=chenn+do'>chenn đỏ</a></li></ul>")
    assert EP.unread_rows(page(empty("chenn", extra=related), "chenn", url=URL)) == []
    rows = related.replace("/s?q=", "/p/")
    assert EP.unread_rows(page(empty("chenn", extra=rows), "chenn", url=URL)) == [
        "chenn xanh", "chenn đỏ"]


def test_an_error_beside_the_empty_pages_line_is_not_empty():
    sig = learned()
    err = "<div class='alert'><p>Đã xảy ra lỗi, vui lòng thử lại sau</p></div>"
    ev = EP.match(sig, page(empty("zzqx", extra=err), "zzqx"), fetch=fetch_from(empty))
    assert ev.state == EP.NOT_EMPTY and "Đã xảy ra lỗi" in ev.why


def test_an_empty_page_that_does_not_name_the_input_needs_an_input_known_to_have_results():
    def bare(q, promo="Khuyến mãi mùa hè", extra=""):
        return shell(f"<p class='msg'>Không có sản phẩm nào phù hợp.</p>{extra}", promo)
    sig = learned(empty_page=bare)
    assert sig["echo"] is False
    p = page(bare("zzqx"), "zzqx")
    ok = RC.positive_ok_for(None)
    # no input known to have results: not decided here
    assert EP.match(sig, p, fetch=fetch_from(bare), positive_ok=ok).state == EP.UNKNOWN
    # the search still finds results for it: empty
    ev = EP.match(sig, p, fetch=fetch_from(lambda v: results(v) if v == "alpha" else bare(v)),
                  positives=["zzqx", "alpha"], positive_ok=ok)
    assert ev.state == EP.EMPTY, ev.why
    # the search finds nothing for it either: it no longer reads its input
    ev = EP.match(sig, p, fetch=fetch_from(bare), positives=["alpha"], positive_ok=ok)
    assert ev.state == EP.NOT_EMPTY and "no longer reads its input" in ev.why


def test_an_input_that_lost_its_results_is_not_enough_to_refuse():
    def bare(q, promo="Khuyến mãi mùa hè", extra=""):
        return shell(f"<p class='msg'>Không có sản phẩm nào phù hợp.</p>{extra}", promo)
    sig = learned(empty_page=bare)
    fetch = fetch_from(lambda v: results(v) if v == "beta" else bare(v))
    ev = EP.match(sig, page(bare("zzqx"), "zzqx"), fetch=fetch, positives=["alpha", "beta"],
                  positive_ok=RC.positive_ok_for(None))
    assert ev.state == EP.EMPTY, ev.why
    assert fetch.sent == ["alpha", "beta"]


def test_a_signature_from_another_normaliser_version_is_not_used():
    sig = dict(learned(), norm="0.0")
    assert not EP.usable(sig)
    assert EP.match(sig, page(empty("zzqx"), "zzqx")).state == EP.UNKNOWN


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def jpage(q, n, *, facets=None, count=None, echo=None):
    data = {"request": {"term": q if echo is None else echo},
            "stats": {"n_found": n if count is None else count, "page": 1},
            "facets": {"brand": facets if facets is not None else (["acme"] if n else [])},
            "results": [{"name": f"{q} {i}"} for i in range(n)]}
    return EP.Page("json", q, data=data, items=list(data["results"]), list_path="results")


def test_the_empty_answer_of_a_json_api_is_learned_from_its_own_fields():
    sig = EP.learn("json", [jpage("alpha", 4), jpage("beta", 3)],
                   [jpage("zorqua", 0), jpage("vexlim", 0)])
    assert sig["kind"] == "json"
    assert sig["count_path"] == ["stats", "n_found"]       # not one of the listed count keys
    assert sig["echo_path"] == ["request", "term"]
    assert sig["result_lists"] == [["facets", "brand"]]
    assert EP.match(sig, jpage("zzqx", 0)).state == EP.EMPTY
    assert EP.match(sig, jpage("zzqx", 0, count=3)).state == EP.NOT_EMPTY
    assert EP.match(sig, jpage("zzqx", 0, facets=["acme"])).state == EP.NOT_EMPTY
    assert EP.match(sig, jpage("zzqx", 0, echo="other")).state == EP.NOT_EMPTY


def test_a_json_answer_with_fields_no_made_up_answer_held_is_not_settled_by_its_echo():
    def api(q, n, **more):
        data = {"query": q, "results": [{"name": f"{q} {i}"} for i in range(n)], **more}
        return EP.Page("json", q, data=data, items=list(data["results"]), list_path="results")
    sig = EP.learn("json", [api("alpha", 4), api("beta", 3)], [api("zorqua", 0), api("vexlim", 0)])
    assert sig["count_path"] is None and sig["echo_path"] == ["query"]
    assert EP.match(sig, api("zzqx", 0)).state == EP.EMPTY
    # a 200 answer that reports an error beside the echo and an empty list
    failed = api("zzqx", 0, errors=[{"message": "search backend timed out"}])
    ev = EP.match(sig, failed)
    assert ev.state == EP.UNKNOWN and "'errors'" in ev.why, ev.why
    ev = EP.match(sig, failed, fetch=lambda v: api(v, 0))
    assert ev.state == EP.UNKNOWN and "a made-up search at the same moment lacks" in ev.why
    # the same new field on the made-up search: the site's answers changed, so an input known
    # to have results must still find them
    def now(v):
        return api(v, 4 if v == "alpha" else 0, version=2)
    newer = api("zzqx", 0, version=2)
    ok = RC.positive_ok_for(None)
    assert EP.match(sig, newer, fetch=now, positives=["alpha"], positive_ok=ok).state == EP.EMPTY
    assert EP.match(sig, newer, fetch=lambda v: api(v, 0, version=2), positives=["alpha"],
                    positive_ok=ok).state == EP.NOT_EMPTY


def test_a_json_answer_without_a_count_or_an_echo_has_no_empty_answer():
    def bare(q, n):
        data = {"results": [{"name": f"{q} {i}"} for i in range(n)]}
        return EP.Page("json", q, data=data, items=list(data["results"]), list_path="results")
    sig = EP.learn("json", [bare("alpha", 4)], [bare("zorqua", 0), bare("vexlim", 0)])
    assert sig["kind"] == "none"


# ---------------------------------------------------------------------------
# learning at the first empty answer
# ---------------------------------------------------------------------------

def test_a_positive_input_that_finds_nothing_stores_nothing_and_refuses():
    ctx = EP.Context(positives=["alpha"], digest="d")
    sig, ev = EP.learn_now(page(empty("zzqx"), "zzqx"), ctx, EP.Fetcher(fetch_from(empty)),
                           RC.positive_ok_for(None))
    assert sig is None and ev.state == EP.NOT_EMPTY and ctx.new_learned is None


def test_a_made_up_search_without_an_answer_leaves_a_retry_marker():
    def flaky(v):
        if v == "alpha":
            return page(results(v), v)
        return EP.Page("html", v, ok=False, error="transport error")
    ctx = EP.Context(positives=["alpha"], digest="d")
    sig, ev = EP.learn_now(page(empty("zzqx"), "zzqx"), ctx, EP.Fetcher(flaky),
                           RC.positive_ok_for(None))
    assert sig is None and ev is None
    assert ctx.new_learned["kind"] == "none" and ctx.new_learned["retry_after"] > time.time()
    # the marker blocks learning until its time
    record = {"empty_page": {"request": ctx.new_learned}}
    from anyuse.execution import workflow_schema as WS
    tdict = {"url": "u"}
    record["empty_page"]["request"]["for"] = WS.template_digest(tdict)
    assert EP.context_for(record, "request", tdict, learn=True).learn is False
    record["empty_page"]["request"]["retry_after"] = time.time() - 1
    assert EP.context_for(record, "request", tdict, learn=True).learn is True


def test_a_learned_page_counts_only_for_the_template_it_was_learned_for():
    from anyuse.execution import workflow_schema as WS
    sig = dict(learned(), **{"for": WS.template_digest({"url": "a"})})
    record = {"empty_page": {"request": sig}}
    assert EP.context_for(record, "request", {"url": "a"}, learn=True).learned is sig
    assert EP.context_for(record, "request", {"url": "b"}, learn=True).learned is None
    assert EP.context_for(record, "request", {"url": "a"}, learn=False).learn is False


# ---------------------------------------------------------------------------
# the model step's candidates
# ---------------------------------------------------------------------------

def test_the_statement_zone_holds_the_lines_that_name_the_input_and_says_when_it_is_whole():
    p = page(shell("<h2>Kết quả cho zzqx</h2><p>Không có gì.</p><p>Hãy thử lại.</p>"), "zzqx")
    cands, complete = EP.statement_zone(p)
    # the naming line and the three lines below it
    assert [c.text for c in cands] == ["Kết quả cho zzqx", "Không có gì.", "Hãy thử lại.",
                                       "Khuyến mãi mùa hè"]
    assert complete is True
    linked = page(shell("<p>Không có gì cho zzqx. <a href='/help'>Trợ giúp</a></p>"), "zzqx")
    cands, complete = EP.statement_zone(linked)
    # a line with a link is never asked about, so a "no" for the others decides nothing
    assert all("zzqx" not in c.text for c in cands) and complete is False


def test_a_made_up_input_answered_with_an_error_page_learns_no_empty_page():
    def failing(q, promo="Khuyến mãi mùa hè", extra=""):
        return shell("<p class='msg'>Something went wrong. Please try again later.</p>", promo)
    sig = learned(empty_page=failing)
    assert sig["kind"] == "none" and "reports an error" in sig["why"]
    # an error line every page shows (the result pages too) is the site's chrome, not its answer
    def footer(body, promo="Khuyến mãi mùa hè"):
        return shell(body, promo).replace("</footer>", "<p>Something went wrong? Write to us.</p></footer>")
    sig = learned(empty_page=lambda q, promo="", extra="": footer(
                      f"<p class='msg'>Không tìm thấy sản phẩm nào cho “{q}”.</p>", promo),
                  result_page=lambda q, promo="Khuyến mãi mùa hè": footer(
                      results(q).split("<main>")[1].split("</main>")[0], promo))
    assert sig["kind"] == "html", sig.get("why")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
