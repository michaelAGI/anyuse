"""Parameter inference: deterministic trace-diff + LLM-over-candidates proposal."""
from execution.params import generalize_from_traces, infer_kind, propose_params


def test_infer_kind():
    assert infer_kind("invoice_final.pdf") == "file"
    assert infer_kind("report.docx") == "file"
    assert infer_kind("report.2024") == "text"     # numeric "extension" is not a file
    assert infer_kind("42") == "number"
    assert infer_kind("3.50") == "number"          # NOT a ".50" file
    assert infer_kind("a@b.com") == "email"
    assert infer_kind("https://x.com/y") == "url"
    assert infer_kind("my secret password") == "credential"
    assert infer_kind("Save") == "text"


def test_generalize_from_traces_finds_changed_value():
    slots = generalize_from_traces(["invoice.pdf", "Q3 report", "Save"],
                                   ["final.pdf", "Q3 report", "Save"])
    assert len(slots) == 1
    assert slots[0].example == "invoice.pdf" and slots[0].kind == "file" and slots[0].confidence == 1.0


def test_generalize_identical_has_no_params():
    assert generalize_from_traces(["x", "y"], ["x", "y"]) == []


def test_generalize_multiple_changes():
    slots = generalize_from_traces(["a@b.com", "100"], ["c@d.com", "250"])
    assert [s.kind for s in slots] == ["email", "number"]


def test_propose_params_parses_llm_output():
    fake = lambda p: '{"params":[{"name":"file","example":"invoice.pdf","kind":"file","confidence":0.9}]}'
    slots = propose_params("rename a file", ["invoice.pdf", "Save"], llm_fn=fake)
    assert len(slots) == 1 and slots[0].name == "file" and slots[0].kind == "file" and slots[0].confidence == 0.9


def test_propose_params_empty_values():
    assert propose_params("t", [], llm_fn=lambda p: "{}") == []


def test_propose_params_tolerant_to_bad_llm():
    assert propose_params("t", ["x"], llm_fn=lambda p: "not json at all") == []


def test_propose_params_tolerant_to_fenced_json():
    fake = lambda p: '```json\n{"params":[{"name":"n","example":"v"}]}\n```'
    slots = propose_params("t", ["v"], llm_fn=fake)
    assert len(slots) == 1 and slots[0].example == "v"


if __name__ == "__main__":
    import inspect, sys
    mod = sys.modules[__name__]
    p = f = 0
    for n, fn in [(n, f) for n, f in vars(mod).items() if n.startswith("test_") and callable(f)]:
        if len(inspect.signature(fn).parameters) != 0:
            raise SystemExit(f"{fn.__name__} takes arguments; this runner cannot supply fixtures")
        try:
            fn(); p += 1; print("OK  ", n)
        except Exception as e:
            f += 1; print("FAIL", n, "::", repr(e)[:200])
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
