"""``anyuse watch add`` takes the tools people have (docs/WATCH.md).

TOOL is a name from ``anyuse workflow list`` or a file: a compiled web tool (``compiled_api.json``), a saved
browser tool (``compiled_demo.json``), or a bare compiled demonstration tool as before. A TOOL that is none of
these says what TOOL can be. A watched compiled web tool replays its pinned compile record through the checks
``anyuse workflow play --no-fallback`` makes (a passed proof, an acknowledged host, a read-only request): no
browser, 0 model calls, nothing written back to the tool.

Offline: a fake transport stands in for the site; the data dir, the host file and the watch store are temporary.
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from anyuse.execution import watch as WA  # noqa: E402
from anyuse.execution.api_compile import compiled_record, persist_compiled  # noqa: E402
from anyuse.execution.api_permission import acknowledge_host  # noqa: E402
from anyuse.execution.api_step import ApiResponse, ApiStepExecutor, RequestTemplate  # noqa: E402

T0 = dt.datetime(2026, 10, 1, 8, 0, tzinfo=dt.timezone.utc)
PRODUCT_TOOL = {"version": 6, "workflow": "product_lookup", "site": "www.shop.test",
                "start_url": "https://www.shop.test/",
                "search": {"mode": "url", "template": "https://www.shop.test/search?q={q}"},
                "fields": {"name": "jsonld", "price": "jsonld", "availability": "jsonld", "url": "canonical"},
                "region": {"regex": "^x$"}, "hosts": ["www.shop.test"], "model_calls": 0}


def crates(*ids):
    return 200, json.dumps({"crates": [{"id": i} for i in ids]})


class FakeTransport:
    """The site: the next scripted answer per request (the last one repeats); records every URL sent."""

    def __init__(self, answers):
        self.answers, self.sent = list(answers), []

    def send(self, method, url, headers, body, timeout_s):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            # what the real transport raises: a refused connection, or the pre-send policy before anything is sent
            if not isinstance(answer, PermissionError):
                self.sent.append(url)
            raise answer
        self.sent.append(url)
        status, text = answer
        return ApiResponse(status, {"Content-Type": "application/json"}, text)


def template(method="GET", params=("q",), **query):
    return RequestTemplate(
        method=method, url="https://crates.io/api/v1/crates", query=dict({"q": "{q}"}, **query),
        headers={"accept": "application/json"}, extract={"items": "crates[*].id"},
        expect={"status_in": [200], "content_type": "json", "path": "crates", "min_items": 1,
                "required_keys": ["id"]},
        params=list(params))


class Env:
    """A temporary data dir (workflow store, host file) and watch store; the environment is restored."""

    KEYS = ("ANYUSE_DATA_DIR", "ANYUSE_API_HOSTS_FILE", "ANYUSE_WATCH_DIR")

    def __enter__(self):
        from anyuse.memory.storage import WorkflowStorage
        self.dir = tempfile.mkdtemp(prefix="anyuse-watch-tools-")
        self.old = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["ANYUSE_DATA_DIR"] = os.path.join(self.dir, "data")
        self.hosts = os.environ["ANYUSE_API_HOSTS_FILE"] = os.path.join(self.dir, "data", "api_hosts.json")
        os.environ.pop("ANYUSE_WATCH_DIR", None)
        self.store = os.path.join(self.dir, "watch")
        self.storage = WorkflowStorage()
        self.st = WA.WatchStore(self.store)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)

    def compiled(self, name="crates_search", passed=True, method="GET", tmpl=None):
        proof = {"passed": passed, "passed_count": 2 if passed else 0, "case_count": 2,
                 "reason": "passed on 2 fresh value(s) not present in the recording"}
        record = compiled_record(tmpl or template(method), proof, workflow=name,
                                 source={"recording": f"{name}.rec.json"})
        return persist_compiled(self.storage, name, record)

    def saved_web(self, name, tool=PRODUCT_TOOL, passed=True):
        from anyuse.execution.harness_compile import save_web_tool
        proof = {"passed": passed, "cases": [{"value": "Line 6 POD Express Guitar", "passed": passed}],
                 "reason": "" if passed else "no fresh value passed"}
        return save_web_tool(self.storage, name, dict(tool), proof, {"site": tool["site"], "value": "x"},
                             param="q")["path"]

    def tool_file(self, data, name="t.tool.json"):
        p = os.path.join(self.dir, name)
        with open(p, "w") as fh:
            json.dump(data, fh)
        return p


def cli(*args):
    from click.testing import CliRunner
    from anyuse import cli as CLI
    return CliRunner().invoke(CLI.cli, list(args))


def hours(h):
    return T0 + dt.timedelta(hours=h)


# ---------------------------------------------------------------------------
# what TOOL can be
# ---------------------------------------------------------------------------

def test_watch_add_takes_a_compiled_web_tool_by_its_name():
    with Env() as e:
        e.compiled("crates_search")
        res = cli("watch", "add", "crates_search", "serde", "--store", e.store)
        assert res.exit_code == 0, res.output
        assert "watching crates-io--serde--" in res.output and "crates_search" in res.output, res.output
        # the host is not acknowledged yet: the command says what to run before `watch run`
        assert "anyuse workflow acknowledge-host crates.io" in res.output, res.output
        (item,) = WA.WatchStore(e.store).items().values()
        assert item["workflow"] == WA.API_WORKFLOW and item["tool_name"] == "crates_search", item
        assert item["param"] == "q" and item["input"] == "serde" and item["site"] == "crates.io", item
        # the pinned copy is the compile record without its run state
        pinned = WA.WatchStore(e.store).tool_of(item)
        assert pinned["template"]["url"] == "https://crates.io/api/v1/crates" and "health" not in pinned
        res = cli("watch", "list", "--store", e.store)
        assert res.exit_code == 0 and "crates_search" in res.output, res.output


def test_watch_add_takes_a_compiled_api_json_file():
    with Env() as e:
        path = e.compiled("crates_search")
        item = e.st.add(path, "serde", "1d")
        assert item["workflow"] == WA.API_WORKFLOW and item["tool_name"] == "crates_search", item
        assert item["tool_source"] == os.path.abspath(path), item
        # the same tool and input again is the same item
        try:
            e.st.add("crates_search", "serde", "1d", storage=e.storage)
            raise AssertionError("the same tool and input were added twice")
        except ValueError as exc:
            assert "already watched" in str(exc), exc


def test_watch_add_takes_a_saved_browser_tool_by_its_file_or_its_name():
    with Env() as e:
        path = e.saved_web("www.shop.test__product_lookup")
        item = e.st.add(path, "Line 6 POD Express Guitar", "1d")
        assert item["workflow"] == "product_lookup" and e.st.tool_of(item) == PRODUCT_TOOL, item
        other = WA.WatchStore(os.path.join(e.dir, "watch2"))
        item2 = other.add("www.shop.test__product_lookup", "Line 6 POD Express Guitar", "1d", storage=e.storage)
        assert item2["id"] == item["id"] and other.tool_of(item2) == PRODUCT_TOOL, item2
        # a bare compiled demonstration tool file works as before
        bare = WA.WatchStore(os.path.join(e.dir, "watch3")).add(e.tool_file(PRODUCT_TOOL), "Line 6", "1d")
        assert bare["workflow"] == "product_lookup" and "tool_name" not in bare, bare


def test_a_wrong_tool_says_what_tool_can_be():
    with Env() as e:
        e.compiled("crates_search")
        e.saved_web("www.shop.test__product_lookup")
        res = cli("watch", "add", "crates", "serde", "--store", e.store)
        assert res.exit_code == 1 and "does not exist" not in res.output, res.output
        for part in ("no tool called 'crates'", "anyuse workflow list", "compiled_api.json", "compiled_demo.json",
                     "crates_search", "www.shop.test__product_lookup"):
            assert part in res.output, (part, res.output)
        res = cli("watch", "add", os.path.join(e.dir, "missing.json"), "serde", "--store", e.store)
        assert res.exit_code == 1 and "no such file" in res.output and "anyuse workflow list" in res.output, \
            res.output
        # a JSON file that is no tool
        res = cli("watch", "add", e.tool_file({"hello": 1}), "serde", "--store", e.store)
        assert res.exit_code == 1 and "not a compiled demonstration tool" in res.output \
            and "anyuse workflow list" in res.output, res.output
    with Env() as e:
        res = cli("watch", "add", "crates", "serde", "--store", e.store)
        assert res.exit_code == 1 and "QUICKSTART_COMPILE.md" in res.output, res.output


def test_a_workflow_folder_is_read_for_its_tool_file():
    with Env() as e:
        path = e.compiled("crates_search")
        item = e.st.add(os.path.dirname(path), "serde", "1d")
        assert item["tool_name"] == "crates_search" and item["tool_source"] == os.path.abspath(path), item
        empty = os.path.join(e.dir, "empty")
        os.makedirs(empty)
        try:
            e.st.add(empty, "serde", "1d")
            raise AssertionError("an empty folder was accepted")
        except ValueError as exc:
            assert "holds no compiled_api.json or compiled_demo.json" in str(exc), exc


def test_a_folder_named_like_a_tool_does_not_hide_the_tool():
    with Env() as e:
        e.compiled("crates_search")
        cwd = os.path.join(e.dir, "cwd")
        os.makedirs(os.path.join(cwd, "crates_search"))
        here = os.getcwd()
        os.chdir(cwd)
        try:
            item = e.st.add("crates_search", "serde", "1d", storage=e.storage)
        finally:
            os.chdir(here)
        assert item["tool_name"] == "crates_search", item


def test_tools_that_can_never_run_are_refused_at_add():
    with Env() as e:
        e.compiled("unproved", passed=False)
        e.compiled("writes", method="POST")
        e.saved_web("www.shop.test__draft", passed=False)
        stale = e.compiled("stale")
        with open(stale) as fh:
            record = json.load(fh)
        record["template"]["query"]["per_page"] = "50"
        with open(stale, "w") as fh:
            json.dump(record, fh)
        changed = e.saved_web("www.shop.test__changed")
        with open(changed) as fh:
            saved = json.load(fh)
        saved["tool"]["site"] = "www.other.test"
        with open(changed, "w") as fh:
            json.dump(saved, fh)
        for name, part in (("unproved", "proof did not pass"), ("writes", "read-only"),
                           ("www.shop.test__draft", "draft"), ("stale", "changed after the proof"),
                           ("www.shop.test__changed", "does not match its digest")):
            try:
                e.st.add(name, "serde", "1d", storage=e.storage)
                raise AssertionError(f"{name} was accepted")
            except ValueError as exc:
                assert part in str(exc), (name, exc)
        assert e.st.items() == {}


def test_a_tool_with_more_inputs_takes_them_with_param():
    with Env() as e:
        e.compiled("paged", tmpl=template(params=("q", "page"), page="{page}"))
        try:
            e.st.add("paged", "serde", "1d", storage=e.storage)
            raise AssertionError("a missing input was accepted")
        except ValueError as exc:
            assert "--param page=VALUE" in str(exc), exc
        try:
            e.st.add("paged", "serde", "1d", storage=e.storage, params={"size": "5"})
            raise AssertionError("an unknown input was accepted")
        except ValueError as exc:
            assert "no input size" in str(exc), exc
        try:
            e.st.add("paged", "serde", "1d", storage=e.storage, params={"q": "tokio", "page": "2"})
            raise AssertionError("the INPUT was accepted twice")
        except ValueError as exc:
            assert "q is the INPUT argument" in str(exc), exc
        item = e.st.add("paged", "serde", "1d", storage=e.storage, params={"page": "2"})
        assert item["param"] == "q" and item["params"] == {"page": "2"}, item
        res = cli("watch", "add", "paged", "serde", "--param", "page=3", "--store", e.store)
        assert res.exit_code == 0, res.output
        try:
            WA.WatchStore(os.path.join(e.dir, "w2")).add(e.tool_file(PRODUCT_TOOL), "x", "1d", params={"a": "b"})
            raise AssertionError("--param was accepted for a browser tool")
        except ValueError as exc:
            assert "--param" in str(exc), exc


# ---------------------------------------------------------------------------
# watching a compiled web tool
# ---------------------------------------------------------------------------

def test_a_watched_compiled_web_tool_is_replayed_diffed_and_never_rewritten():
    with Env() as e:
        path = e.compiled("crates_search")
        acknowledge_host("crates.io", path=e.hosts)
        item = e.st.add("crates_search", "serde", "1h", storage=e.storage)
        with open(path, "rb") as fh:
            before = fh.read()
        tr = FakeTransport([crates("serde", "serde_json", "serde_yaml"), crates("serde", "serde_json", "serde_yaml"),
                            crates("serde", "serde_yaml", "serde_derive"), (200, '{"packages": []}'),
                            crates("serde", "serde_derive", "serde_yaml")])
        rp = WA.make_api_replay(executor=ApiStepExecutor(transport=tr), hosts_path=e.hosts)
        kinds = []
        for h in range(5):
            r = WA.run_watch(e.st, rp, now=hours(h))
            assert r["model_calls"]["replay"] == 0, r
            kinds.append(r["entries"][0]["kind"])
        assert kinds == ["baseline", "unchanged", "change", "drift", "change"], kinds
        assert all("q=serde" in u for u in tr.sent) and len(tr.sent) == 5, tr.sent
        log = e.st.changelog()
        change = [x for x in log if x["kind"] == "change"]
        assert change[0]["changes"] == [
            {"kind": "removed", "rank": 2, "title": "serde_json", "url": None, "list": "items"},
            {"kind": "added", "rank": 3, "title": "serde_derive", "url": None, "list": "items"},
            {"kind": "moved", "before_rank": 3, "after_rank": 2, "title": "serde_yaml", "url": None,
             "list": "items"}], change[0]
        # the drift kept the baseline: the next good run is diffed against the last good snapshot
        assert [c["kind"] for c in change[1]["changes"]] == ["moved", "moved"], change[1]
        # the site answered, but not as it did when the request was proved: a changed site
        drift = [x for x in log if x["kind"] == "drift"][0]
        assert drift["action"] == "recompile" and drift["drift"]["status"] == "failed", drift
        md = open(os.path.join(e.store, "CHANGELOG.md")).read()
        assert "items: removed #2: serde_json" in md and "record the search again and recompile" in md, md
        assert f"anyuse watch remove {item['id']}" in md, md
        with open(path, "rb") as fh:
            assert fh.read() == before, "the watch wrote into the compiled tool"
        assert e.st.items()[item["id"]]["last_status"] == "ok"


def test_a_site_that_did_not_answer_is_an_outage_not_drift():
    # a server error and a refused connection say nothing about the request: no recompile, the baseline stays
    with Env() as e:
        e.compiled("crates_search")
        acknowledge_host("crates.io", path=e.hosts)
        item = e.st.add("crates_search", "serde", "1h", storage=e.storage)
        tr = FakeTransport([crates("serde", "serde_json"), (503, "busy"),
                            ConnectionRefusedError(61, "Connection refused"), crates("serde", "serde_json")])
        rp = WA.make_api_replay(executor=ApiStepExecutor(transport=tr), hosts_path=e.hosts)
        runs = [WA.run_watch(e.st, rp, now=hours(h)) for h in range(3)]
        assert [r["entries"][0]["kind"] for r in runs] == ["baseline", "outage", "outage"], runs
        down = runs[1]["entries"][0]
        assert down["action"] == "" and down["outage"]["status"] == "unavailable" and "503" in down["outage"]["why"], down
        assert "drift" not in down and runs[1]["items"][0]["drift"] is None, down
        assert runs[1]["items"][0]["requests"] == 1, runs[1]["items"]
        it = e.st.items()[item["id"]]
        assert it["drift_since"] is None and it["last_good"].startswith("20261001T080000Z"), it
        assert it["last_status"] == "unavailable", it
        # the next good run is diffed against the snapshot before the outage
        assert WA.run_watch(e.st, rp, now=hours(3))["entries"][0]["kind"] == "unchanged"
        md = open(os.path.join(e.store, "CHANGELOG.md")).read()
        assert "SITE DID NOT ANSWER" in md and "Connection refused" in md, md
        assert "record the search again" not in md and "**DRIFT" not in md and "recompile " not in md, md
        hook = WA.hook_text(e.st, since="2026-10-01T00:00:00Z")
        assert "0 drift(s), 2 replay(s) the site did not answer" in hook and "nothing to recompile" in hook, hook
        # while the site is down, the item is not drifting
        res = cli("watch", "list", "--store", e.store)
        assert "drift since" not in res.output, res.output


def test_watch_run_counts_the_replays_the_site_did_not_answer():
    with Env() as e:
        e.compiled("crates_search")
        acknowledge_host("crates.io", path=e.hosts)
        assert cli("watch", "add", "crates_search", "serde", "--store", e.store).exit_code == 0
        make = WA.make_api_replay
        tr = FakeTransport([(503, "busy")])
        WA.make_api_replay = lambda **kw: make(executor=ApiStepExecutor(transport=tr), hosts_path=e.hosts, **kw)
        try:
            res = cli("watch", "run", "--store", e.store)
        finally:
            WA.make_api_replay = make
        assert res.exit_code == 0 and len(tr.sent) == 1, (res.output, tr.sent)
        assert "1 replayed, 0 skipped, 1 SITE DID NOT ANSWER (not drift);" in res.output, res.output


def test_a_policy_refusal_says_nothing_was_sent():
    with Env() as e:
        e.compiled("crates_search")
        acknowledge_host("crates.io", path=e.hosts)
        e.st.add("crates_search", "serde", "1h", storage=e.storage)
        refused = PermissionError("pre-send policy refused request: nonpublic destination '10.0.0.7' is refused "
                                  "by default")
        tr = FakeTransport([refused])
        r = WA.run_watch(e.st, WA.make_api_replay(executor=ApiStepExecutor(transport=tr), hosts_path=e.hosts),
                         now=hours(0))
        e1 = r["entries"][0]
        assert e1["kind"] == "drift" and e1["action"] == "", e1
        assert e1["drift"]["status"] == "refused" and e1["drift"]["policy_refused"] is True, e1
        assert "nonpublic destination" in e1["drift"]["why"], e1
        assert tr.sent == [] and r["items"][0]["requests"] == 0, (tr.sent, r["items"])
        md = WA.render_entry(e1)
        assert "REFUSED (nothing sent)" in md and "nonpublic destination" in md, md
        assert "record the search again" not in md and "recompile" not in md.replace("Nothing needs recompiling", ""), md


def test_a_watched_tool_that_was_recompiled_says_to_watch_it_again():
    with Env() as e:
        e.compiled("crates_search")
        acknowledge_host("crates.io", path=e.hosts)
        e.st.add("crates_search", "serde", "1h", storage=e.storage)
        e.compiled("crates_search", tmpl=template(per_page="50"))
        tr = FakeTransport([(404, "gone")])
        rp = WA.make_api_replay(executor=ApiStepExecutor(transport=tr), hosts_path=e.hosts, storage=e.storage)
        r = WA.run_watch(e.st, rp, now=hours(0))
        e1 = r["entries"][0]
        assert e1["kind"] == "drift" and "recompiled since this item was added" in e1["drift"]["why"], e1
        # the pinned request ran, not the new one
        assert tr.sent and "per_page" not in tr.sent[0], tr.sent


def test_run_compiled_never_writes_a_pinned_record_back():
    # a pinned copy runs with the stored tool's checks, but its outcome is never saved over the stored tool,
    # even when the caller asks for the outcome to be recorded
    from anyuse.mcpserver.compiled_tools import run_compiled
    with Env() as e:
        path = e.compiled("crates_search")
        acknowledge_host("crates.io", path=e.hosts)
        pinned = WA.resolve_tool("crates_search", e.storage, e.hosts)["record"]
        with open(path, "rb") as fh:
            before = fh.read()
        for answer in (crates("serde"), (503, "busy")):
            out = run_compiled("crates_search", {"q": "serde"}, storage=e.storage, hosts_path=e.hosts,
                               executor=ApiStepExecutor(transport=FakeTransport([answer])), record=pinned,
                               record_outcome=True, cascade=False, post_card=False)
            assert out["ok"] == (answer[0] == 200), out
        with open(path, "rb") as fh:
            assert fh.read() == before, "the pinned copy's outcome was written into the stored tool"
        assert "health" not in pinned, pinned


def test_an_unacknowledged_host_is_refused_before_any_request():
    with Env() as e:
        e.compiled("crates_search")
        e.st.add("crates_search", "serde", "1h", storage=e.storage)
        tr = FakeTransport([crates("serde")])
        r = WA.run_watch(e.st, WA.make_api_replay(executor=ApiStepExecutor(transport=tr), hosts_path=e.hosts),
                         now=hours(0))
        e1 = r["entries"][0]
        assert e1["kind"] == "drift" and e1["drift"]["status"] == "refused", e1
        assert "acknowledge-host" in e1["drift"]["why"] and tr.sent == [], (e1, tr.sent)
        assert r["items"][0]["requests"] == 0, r


def test_watch_run_needs_no_browser_for_compiled_web_tools():
    with Env() as e:
        e.compiled("crates_search")
        assert cli("watch", "add", "crates_search", "serde", "--store", e.store).exit_code == 0
        # the host is not acknowledged: the compiled request is refused before it is sent, so nothing leaves
        res = cli("watch", "run", "--store", e.store)
        assert res.exit_code == 0 and "1 replayed" in res.output and "--browser" not in res.output, res.output
        assert "acknowledge-host" in res.output, res.output
        # a watched browser tool still needs the browser
        e.saved_web("www.shop.test__product_lookup")
        assert cli("watch", "add", "www.shop.test__product_lookup", "Line 6", "--store", e.store).exit_code == 0
        res = cli("watch", "run", "--store", e.store)
        assert res.exit_code != 0 and "--browser" in res.output, res.output


def test_api_diff_of_fields_lists_and_rows():
    b = {"api": {"items": ["a", "b", "c"], "total": 3,
                 "rows": [{"name": "x", "url": "https://s.test/x", "price": 1.0}, {"name": "y", "price": 2.0}]}}
    a = {"api": {"items": ["a", "c", "d"], "total": 4,
                 "rows": [{"name": "x", "url": "https://s.test/x", "price": 1.5}, {"name": "y", "price": 2.0}]}}
    ch = WA.diff({"content": b}, {"content": a, "workflow": WA.API_WORKFLOW})
    kinds = [(c["kind"], c.get("list") or c.get("field")) for c in ch]
    assert kinds == [("removed", "items"), ("added", "items"), ("moved", "items"), ("item_field", "rows"),
                     ("field", "total")], ch
    row = [c for c in ch if c["kind"] == "item_field"][0]
    assert row["field"] == "price" and row["before"] == 1.0 and row["after"] == 1.5, row
    assert WA.change_line(row) == "rows: #1 x: price 1.0 -> 1.5", WA.change_line(row)
    assert WA.diff({"content": b}, {"content": b, "workflow": WA.API_WORKFLOW}) == []
    # listed <-> no results
    ch = WA.diff({"content": {"no_results": True}}, {"content": a, "workflow": WA.API_WORKFLOW})
    assert ch[0] == {"kind": "presence", "before": "no results", "after": "listed"}, ch


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok")
