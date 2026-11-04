"""
Unit tests for anyuse.core.restriction — BOUNDARY enforcement.

Pure in-memory policy/enforcement tests. No live agent, no actuation, no
network. Run with:

    /usr/local/bin/python3.13 -m pytest test_restriction.py -q
    # or
    /usr/local/bin/python3.13 test_restriction.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)                       # flat imports
import pathlib as _pathlib  # noqa: E402
import tempfile as _tempfile  # noqa: E402
# `import anyuse` must resolve to THIS checkout, never a sibling directory named anyuse.
_LINK = _tempfile.TemporaryDirectory(prefix="anyuse-pkgroot-")
(_pathlib.Path(_LINK.name) / "anyuse").symlink_to(_HERE, target_is_directory=True)
sys.path.insert(0, _LINK.name)

from anyuse.core.restriction import (  # noqa: E402
    ComplianceChecker,
    RestrictionPolicy,
    domain_matches,
)


# ---------------------------------------------------------------------------
# domain_matches: exact, subdomain, wildcard, and suffix-bypass negatives
# ---------------------------------------------------------------------------

def test_domain_exact_match():
    assert domain_matches("example.com", "example.com")
    assert domain_matches("example.com", "EXAMPLE.COM")  # case-insensitive
    assert domain_matches("example.com", "example.com.")  # trailing dot trimmed


def test_domain_exact_matches_subdomains():
    # A bare domain in a boundary list covers its subdomains too.
    assert domain_matches("example.com", "sub.example.com")
    assert domain_matches("example.com", "a.b.example.com")


def test_domain_exact_does_not_match_sibling_or_suffix_tricks():
    assert not domain_matches("example.com", "notexample.com")
    assert not domain_matches("example.com", "example.com.attacker.com")
    assert not domain_matches("example.com", "evil-example.com")
    assert not domain_matches("example.com", "examplexcom")


def test_domain_wildcard_matches_subdomains():
    assert domain_matches("*.example.com", "sub.example.com")
    assert domain_matches("*.example.com", "a.b.example.com")
    assert domain_matches("*.EXAMPLE.com", "Deep.Sub.Example.com")


def test_domain_wildcard_does_not_match_apex():
    # Standard DNS wildcard semantics: *.example.com != example.com
    assert not domain_matches("*.example.com", "example.com")


def test_domain_wildcard_suffix_bypass_negative():
    # The classic bug: naive endswith("example.com") would WRONGLY allow these.
    assert not domain_matches("*.example.com", "evil-example.com.attacker.com")
    assert not domain_matches("*.example.com", "notexample.com")
    assert not domain_matches("*.example.com", "example.com.attacker.com")
    assert not domain_matches("*.example.com", "fooexample.com")


def test_domain_catchall_wildcard():
    assert domain_matches("*", "anything.com")
    assert domain_matches("*", "example.com")


def test_domain_partial_label_glob_rejected():
    # We deliberately do NOT support partial-label globs (bypass risk).
    assert not domain_matches("*example.com", "sub.example.com")
    assert not domain_matches("ex*.com", "example.com")


def test_domain_match_accepts_full_urls_as_host_input():
    assert domain_matches("example.com", "https://sub.example.com:443/path?q=1")
    assert domain_matches("*.example.com", "http://a.example.com/")


# ---------------------------------------------------------------------------
# RestrictionPolicy: blocklist mode
# ---------------------------------------------------------------------------

def test_blocklist_allows_by_default():
    pol = RestrictionPolicy(
        list_mode="blocklist",
        blocked_domains=["*.evil.com"],
    )
    ok, reason = pol.is_domain_allowed("https://good.com")
    assert ok, reason
    ok, reason = pol.is_domain_allowed("sub.evil.com")
    assert not ok
    assert "blocked" in reason.lower()


def test_blocklist_blocks_subdomain_of_blocked():
    pol = RestrictionPolicy(list_mode="blocklist", blocked_domains=["evil.com"])
    assert pol.is_domain_allowed("evil.com")[0] is False
    assert pol.is_domain_allowed("a.evil.com")[0] is False
    # Suffix trick must still be allowed (not actually evil.com).
    assert pol.is_domain_allowed("evil.com.good.com")[0] is True


# ---------------------------------------------------------------------------
# RestrictionPolicy: allowlist mode
# ---------------------------------------------------------------------------

def test_allowlist_only_permits_listed():
    pol = RestrictionPolicy(
        list_mode="allowlist",
        allowed_domains=["example.com", "*.corp.net"],
        default_deny=True,
    )
    assert pol.is_domain_allowed("example.com")[0] is True
    assert pol.is_domain_allowed("sub.example.com")[0] is True
    assert pol.is_domain_allowed("api.corp.net")[0] is True
    # Not on allowlist -> denied.
    ok, reason = pol.is_domain_allowed("random.com")
    assert ok is False
    assert "allowlist" in reason.lower()


def test_allowlist_blocklist_overrides_allow():
    # A domain on both lists is blocked — blocklist is an explicit deny.
    pol = RestrictionPolicy(
        list_mode="allowlist",
        allowed_domains=["*.example.com"],
        blocked_domains=["secret.example.com"],
        default_deny=True,
    )
    assert pol.is_domain_allowed("ok.example.com")[0] is True
    assert pol.is_domain_allowed("secret.example.com")[0] is False


# ---------------------------------------------------------------------------
# App matching
# ---------------------------------------------------------------------------

def test_app_blocklist():
    pol = RestrictionPolicy(list_mode="blocklist", blocked_apps=["Terminal"])
    assert pol.is_app_allowed("Safari")[0] is True
    assert pol.is_app_allowed("Terminal")[0] is False
    assert pol.is_app_allowed("Terminal.app")[0] is False  # .app suffix normalized
    assert pol.is_app_allowed("terminal")[0] is False       # case-insensitive


def test_app_allowlist():
    pol = RestrictionPolicy(
        list_mode="allowlist",
        allowed_apps=["Safari", "Mail.app"],
        default_deny=True,
    )
    assert pol.is_app_allowed("Safari")[0] is True
    assert pol.is_app_allowed("Mail")[0] is True
    ok, reason = pol.is_app_allowed("Terminal")
    assert ok is False
    assert "allowlist" in reason.lower()


# ---------------------------------------------------------------------------
# default_deny semantics
# ---------------------------------------------------------------------------

def test_default_deny_blocks_unparseable_target():
    pol = RestrictionPolicy(
        list_mode="allowlist",
        allowed_domains=["example.com"],
        default_deny=True,
    )
    ok, _ = pol.is_domain_allowed("")  # no host
    assert ok is False


def test_no_default_deny_allows_unconstrained_target():
    pol = RestrictionPolicy(
        list_mode="blocklist",
        blocked_domains=["evil.com"],
        default_deny=False,
    )
    ok, _ = pol.is_domain_allowed("")  # can't identify -> allow in blocklist
    assert ok is True


# ---------------------------------------------------------------------------
# from_boundary parsing
# ---------------------------------------------------------------------------

def test_from_boundary_parses_sample_dict():
    boundary = {
        "list_mode": "allowlist",
        "allowed_domains": ["*.example.com", "stripe.com"],
        "blocked_domains": ["*.evil.com"],
        "allowed_apps": ["Safari", "Mail"],
        "blocked_apps": ["Terminal"],
        "max_actions": 500,
        "action_rules": {"navigate": "guard"},  # handled elsewhere; ignored here
        "confirmation_behavior": "selective",
        "safety_instructions": "be careful",
    }
    pol = RestrictionPolicy.from_boundary(boundary)
    assert pol.active is True
    assert pol.list_mode == "allowlist"
    assert pol.default_deny is True  # inferred from allowlist
    assert "stripe.com" in pol.allowed_domains
    assert "*.evil.com" in pol.blocked_domains
    # Enforcement reflects the parsed policy.
    assert pol.is_domain_allowed("dashboard.stripe.com")[0] is True
    assert pol.is_domain_allowed("a.example.com")[0] is True
    assert pol.is_domain_allowed("malware.evil.com")[0] is False
    assert pol.is_domain_allowed("unknown.com")[0] is False  # not allowlisted
    assert pol.is_app_allowed("Safari")[0] is True
    assert pol.is_app_allowed("Terminal")[0] is False


def test_from_boundary_tolerant_of_scalars_and_missing_keys():
    boundary = {
        "list_mode": "BLOCKLIST",          # case
        "blocked_domains": "single.com",   # scalar instead of list
        # allowed_domains / apps absent
    }
    pol = RestrictionPolicy.from_boundary(boundary)
    assert pol.list_mode == "blocklist"
    assert pol.blocked_domains == ["single.com"]
    assert pol.is_domain_allowed("single.com")[0] is False
    assert pol.is_domain_allowed("other.com")[0] is True


def test_from_boundary_synonyms():
    assert RestrictionPolicy.from_boundary({"list_mode": "whitelist", "allowed_domains": ["x.com"]}).list_mode == "allowlist"
    assert RestrictionPolicy.from_boundary({"list_mode": "blacklist", "blocked_domains": ["x.com"]}).list_mode == "blocklist"


# ---------------------------------------------------------------------------
# No-policy => permissive (existing behavior preserved)
# ---------------------------------------------------------------------------

def test_empty_boundary_is_permissive():
    for empty in (None, {}, {"list_mode": "blocklist"}):
        pol = RestrictionPolicy.from_boundary(empty)
        assert pol.active is False
        assert pol.is_domain_allowed("anything.com")[0] is True
        assert pol.is_domain_allowed("evil.com")[0] is True
        assert pol.is_app_allowed("Terminal")[0] is True


def test_explicit_deny_everything_boundary_is_active():
    # allowlist + default_deny with no entries = deliberate "deny all".
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "allowlist", "default_deny": True}
    )
    assert pol.active is True
    assert pol.is_domain_allowed("anything.com")[0] is False


# ---------------------------------------------------------------------------
# ComplianceChecker.check_action
# ---------------------------------------------------------------------------

def test_checker_no_policy_is_passthrough():
    cc = ComplianceChecker()  # permissive default
    ok, reason = cc.check_action({"action": "navigate", "args": {"url": "https://evil.com"}})
    assert ok is True
    assert cc.actions_executed == 1
    assert cc.actions_blocked == 0


def test_checker_blocks_navigation_to_blocked_domain():
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "blocklist", "blocked_domains": ["*.evil.com"]}
    )
    cc = ComplianceChecker(pol)
    ok, reason = cc.check_action({"action": "navigate", "args": {"url": "https://login.evil.com/x"}})
    assert ok is False
    assert "blocked" in reason.lower()
    assert cc.actions_blocked == 1
    assert cc.actions_executed == 0


def test_checker_allows_navigation_to_allowed_domain():
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "allowlist", "allowed_domains": ["stripe.com"], "default_deny": True}
    )
    cc = ComplianceChecker(pol)
    ok, _ = cc.check_action({"action": "navigate", "args": {"url": "https://dashboard.stripe.com"}})
    assert ok is True
    assert cc.actions_executed == 1
    # And a non-allowlisted nav is blocked.
    ok, _ = cc.check_action({"action": "navigate", "args": {"url": "https://other.com"}})
    assert ok is False
    assert cc.actions_blocked == 1


def test_checker_blocks_app_launch():
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "blocklist", "blocked_apps": ["Terminal"]}
    )
    cc = ComplianceChecker(pol)
    ok, reason = cc.check_action({"action": "open_app", "args": {"app_name": "Terminal"}})
    assert ok is False
    assert "blocked" in reason.lower()
    ok, _ = cc.check_action({"action": "open_app", "args": {"app_name": "Safari"}})
    assert ok is True


def test_checker_allows_non_target_actions():
    # Clicks / typing have no domain or app target — always allowed even under
    # an active policy.
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "allowlist", "allowed_domains": ["x.com"], "default_deny": True}
    )
    cc = ComplianceChecker(pol)
    ok, _ = cc.check_action({"action": "click_at", "args": {"x": 10, "y": 20}})
    assert ok is True
    ok, _ = cc.check_action({"action": "type_text", "args": {"text": "hello"}})
    assert ok is True


def test_checker_flat_action_shape():
    # Some callers pass a flat dict (url at top level, no "args" wrapper).
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "blocklist", "blocked_domains": ["evil.com"]}
    )
    cc = ComplianceChecker(pol)
    ok, _ = cc.check_action({"action": "navigate", "url": "https://evil.com"})
    assert ok is False


def test_checker_url_on_any_action_is_evaluated():
    # A URL arg on a non-nav-named action is still checked (defense in depth).
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "blocklist", "blocked_domains": ["evil.com"]}
    )
    cc = ComplianceChecker(pol)
    ok, _ = cc.check_action({"action": "do_something", "args": {"url": "https://evil.com"}})
    assert ok is False


def test_checker_actions_executed_counter_used_by_mcp_layer():
    # The MCP server and plan_executor read .actions_executed for reporting.
    cc = ComplianceChecker(RestrictionPolicy.permissive())
    for _ in range(3):
        cc.check_action({"action": "click_at", "args": {"x": 1, "y": 2}})
    assert cc.actions_executed == 3
    cc.reset()
    assert cc.actions_executed == 0


# ===========================================================================
# Regression tests for confirmed security bypasses
# ===========================================================================

# ---------------------------------------------------------------------------
# (3) HIGH — parser-differential: backslash + userinfo in authority
# ---------------------------------------------------------------------------

def test_backslash_userinfo_parser_differential_bypass():
    # 'https://evil.com\@allowed.com' parses host=allowed.com under urlparse,
    # but a browser (WHATWG) normalizes the backslash and treats the part
    # before '@' as userinfo -> it navigates to evil.com. Must NOT be allowed.
    pol = RestrictionPolicy(
        list_mode="allowlist",
        allowed_domains=["allowed.com"],
        default_deny=True,
    )
    ok, reason = pol.is_domain_allowed("https://evil.com\\@allowed.com")
    assert ok is False, reason
    # And the blocklist direction: evil.com is the real host, so a block on it
    # must catch the disguised URL too.
    pol_b = RestrictionPolicy(list_mode="blocklist", blocked_domains=["evil.com"])
    assert pol_b.is_domain_allowed("https://evil.com\\@allowed.com")[0] is False


def test_userinfo_stripped_for_scheme_and_schemeless():
    from anyuse.core.restriction import _normalize_host
    assert _normalize_host("https://evil.com\\@allowed.com") == "evil.com"
    assert _normalize_host("https://user:pass@real.com/x") == "real.com"
    # scheme-less userinfo trick
    assert _normalize_host("evil.com@allowed.com/path") == "allowed.com"
    # forward-slash variant a browser also collapses
    assert _normalize_host("https://evil.com/@allowed.com") == "evil.com"


# ---------------------------------------------------------------------------
# (6) MEDIUM — IPv4 literal canonicalization (decimal / hex / octal)
# ---------------------------------------------------------------------------

def test_ipv4_decimal_and_hex_literal_canonicalized():
    # blocked 192.168.1.1 must also block its decimal/hex integer spellings.
    pol = RestrictionPolicy(list_mode="blocklist", blocked_domains=["192.168.1.1"])
    assert pol.is_domain_allowed("http://3232235777/")[0] is False     # decimal
    assert pol.is_domain_allowed("http://0xc0a80101/")[0] is False     # hex
    # dotted-quad itself still blocked
    assert pol.is_domain_allowed("http://192.168.1.1/")[0] is False
    # an unrelated IP is still allowed (no over-blocking)
    assert pol.is_domain_allowed("http://8.8.8.8/")[0] is True


def test_ipv4_octal_and_normalize_helper():
    from anyuse.core.restriction import _normalize_host
    assert _normalize_host("http://3232235777/") == "192.168.1.1"
    assert _normalize_host("http://0xc0a80101/") == "192.168.1.1"
    # C-style leading-zero octal dotted form
    assert _normalize_host("http://0300.0250.01.01/") == "192.168.1.1"
    # a bare decimal host (no scheme) canonicalizes too
    assert _normalize_host("3232235777") == "192.168.1.1"
    # a normal domain is untouched
    assert _normalize_host("http://example.com/") == "example.com"
    # an out-of-range integer is NOT treated as an IPv4 literal
    assert _normalize_host("99999999999") == "99999999999"


# ---------------------------------------------------------------------------
# (7) MEDIUM — IDNA / NFC normalization (Unicode IDN <-> punycode)
# ---------------------------------------------------------------------------

def test_idna_unicode_bypasses_punycode_blocklist():
    # Blocklist holds the punycode form; the equivalent Unicode IDN host must
    # still be blocked (and vice versa).
    pol = RestrictionPolicy(
        list_mode="blocklist", blocked_domains=["xn--bcher-kva.de"]
    )
    assert pol.is_domain_allowed("https://bücher.de/")[0] is False  # bücher.de
    # reverse: blocklist holds Unicode, punycode input must be blocked too
    pol_rev = RestrictionPolicy(
        list_mode="blocklist", blocked_domains=["bücher.de"]
    )
    assert pol_rev.is_domain_allowed("https://xn--bcher-kva.de/")[0] is False


def test_idna_wildcard_and_allowlist_consistency():
    # Wildcard pattern with a Unicode base must match a punycode subdomain.
    pol = RestrictionPolicy(
        list_mode="blocklist", blocked_domains=["*.bücher.de"]
    )
    assert pol.is_domain_allowed("https://shop.bücher.de/")[0] is False
    assert pol.is_domain_allowed("https://shop.xn--bcher-kva.de/")[0] is False
    # allowlist: Unicode entry must permit the equivalent punycode host
    pol_a = RestrictionPolicy(
        list_mode="allowlist",
        allowed_domains=["bücher.de"],
        default_deny=True,
    )
    assert pol_a.is_domain_allowed("https://xn--bcher-kva.de/")[0] is True
    assert pol_a.is_domain_allowed("https://other.de/")[0] is False


# ---------------------------------------------------------------------------
# (5) MEDIUM — embedded nav target in opaque script/exec payloads
# ---------------------------------------------------------------------------

def test_checker_blocks_javascript_embedded_disallowed_url():
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "blocklist", "blocked_domains": ["evil.com"]}
    )
    cc = ComplianceChecker(pol)
    ok, reason = cc.check_action(
        {"action": "javascript", "args": {"script": "location.href='https://evil.com'"}}
    )
    assert ok is False
    assert "evil.com" in reason
    assert cc.actions_blocked == 1


def test_checker_blocks_embedded_url_nested_in_payload():
    # URL hidden inside a nested list/dict arg is still extracted and checked.
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "blocklist", "blocked_domains": ["*.evil.com"]}
    )
    cc = ComplianceChecker(pol)
    ok, _ = cc.check_action(
        {"action": "eval", "args": {"parts": ["x", "open https://login.evil.com/x now"]}}
    )
    assert ok is False


def test_checker_scripting_action_fails_closed_when_target_unverifiable():
    # An allowlist (constraining) policy + a scripting action whose payload has
    # no extractable URL -> cannot vet -> fail closed.
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "allowlist", "allowed_domains": ["x.com"], "default_deny": True}
    )
    cc = ComplianceChecker(pol)
    ok, reason = cc.check_action(
        {"action": "javascript", "args": {"script": "return document.title"}}
    )
    assert ok is False
    assert "fail-closed" in reason.lower()


def test_checker_scripting_action_with_allowed_embedded_url_passes():
    # The fail-closed backstop must NOT fire when an embedded target was found
    # and is on the allowlist — the navigation is vetted.
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "allowlist", "allowed_domains": ["x.com"], "default_deny": True}
    )
    cc = ComplianceChecker(pol)
    ok, _ = cc.check_action(
        {"action": "javascript", "args": {"script": "location.href='https://api.x.com/'"}}
    )
    assert ok is True
    assert cc.actions_executed == 1


def test_checker_benign_action_with_urlish_text_still_allowed():
    # type/click etc. are benign no-target actions: a URL-looking substring in
    # typed text must NOT trip the embedded-URL scan (no over-blocking).
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "blocklist", "blocked_domains": ["evil.com"]}
    )
    cc = ComplianceChecker(pol)
    ok, _ = cc.check_action(
        {"action": "type_text", "args": {"text": "please visit https://evil.com later"}}
    )
    assert ok is True
    # And a non-scripting, non-benign action under a pure blocklist with no
    # extractable URL is not blanket-denied (blocklist is not constraining).
    cc2 = ComplianceChecker(pol)
    ok2, _ = cc2.check_action(
        {"action": "javascript", "args": {"script": "return document.title"}}
    )
    assert ok2 is True


# ---------------------------------------------------------------------------
# (8) LOW — from_boundary unrecognized list_mode must FAIL CLOSED
# ---------------------------------------------------------------------------

def test_from_boundary_unrecognized_mode_fails_closed():
    # 'allow-list' is not a recognized token. It must NOT degrade to a
    # permissive blocklist; an intended allowlist would otherwise leak.
    pol = RestrictionPolicy.from_boundary(
        {"list_mode": "allow-list", "allowed_domains": ["safe.com"]}
    )
    assert pol.active is True
    assert pol.list_mode == "allowlist"
    assert pol.default_deny is True
    assert pol.is_domain_allowed("safe.com")[0] is True
    assert pol.is_domain_allowed("random.com")[0] is False


def test_from_boundary_unrecognized_mode_ignores_permissive_default_deny_flag():
    # Even an explicit default_deny=False can't re-open a malformed boundary.
    pol = RestrictionPolicy.from_boundary(
        {
            "list_mode": "permit",          # unrecognized
            "allowed_domains": ["safe.com"],
            "default_deny": False,
        }
    )
    assert pol.default_deny is True
    assert pol.is_domain_allowed("random.com")[0] is False


def test_from_boundary_empty_mode_stays_permissive():
    # An absent/empty list_mode is the benign default path, not "unrecognized":
    # it must remain the safe-for-existing-flows blocklist, and with no rules,
    # inactive/permissive.
    pol = RestrictionPolicy.from_boundary({"list_mode": ""})
    assert pol.list_mode == "blocklist"
    assert pol.active is False
    assert pol.is_domain_allowed("anything.com")[0] is True


def test_ipv4_short_dword_2_and_3_part_forms_canonicalized():
    # re-audit: 2-part (a.b) and 3-part (a.b.c) inet_aton dword forms browsers
    # accept must also canonicalize, else a blocked 192.168.1.1 is reachable.
    from anyuse.core.restriction import _normalize_host
    assert _normalize_host("http://192.11010305/") == "192.168.1.1"   # 2-part
    assert _normalize_host("http://192.168.257/") == "192.168.1.1"    # 3-part
    pol = RestrictionPolicy(list_mode="blocklist", blocked_domains=["192.168.1.1"])
    assert pol.is_domain_allowed("192.11010305")[0] is False
    assert pol.is_domain_allowed("192.168.257")[0] is False


# ---------------------------------------------------------------------------
# Lightweight runner (works without pytest installed)
# ---------------------------------------------------------------------------

def test_boundary_rules_and_allowance_are_enforced_atomically():
    from concurrent.futures import ThreadPoolExecutor
    checker = ComplianceChecker(RestrictionPolicy.from_boundary({"max_actions": 1}))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: checker.check_action({"action": "click_at", "args": {}}), range(8)))
    assert sum(ok for ok, _ in results) == 1
    assert checker.actions_executed == 1 and checker.actions_blocked == 7
    for rule in ("exclude", "guard"):
        checker = ComplianceChecker(RestrictionPolicy.from_boundary({"action_rules": {"click": rule}}))
        assert checker.check_action({"action": "click_at", "args": {}})[0] is False
        assert checker.actions_executed == 0
    checker = ComplianceChecker(RestrictionPolicy.from_boundary({"action_rules": {"right_click": "exclude"}}))
    assert checker.check_action({"action": "click_at", "args": {"button": "left"}})[0] is True
    assert checker.check_action({"action": "click_at", "args": {"button": "right"}})[0] is False


def test_guard_requires_an_explicit_boolean_and_rechecks_deadline():
    from anyuse.core.restriction import ExecutionControl
    now = [0.0]
    control = ExecutionControl(2, clock=lambda: now[0])
    policy = RestrictionPolicy.from_boundary({"action_rules": {"click": "guard"}})
    for value in (None, "true", 1, False):
        checker = ComplianceChecker(policy, guard_evaluator=lambda _, v=value: v)
        assert checker.check_action({"action": "click"})[0] is False
    def expired(_):
        now[0] = 3
        return True
    checker = ComplianceChecker(policy, control=control, guard_evaluator=expired)
    assert checker.check_action({"action": "click"})[0] is False
    assert checker.actions_executed == 0


def test_malformed_action_boundaries_fail_closed():
    for boundary in ({"max_actions": True}, {"max_actions": -1}, {"max_actions": "1"},
                     {"action_rules": []}, {"action_rules": {"click": "sometimes"}}):
        try:
            RestrictionPolicy.from_boundary(boundary)
        except ValueError:
            pass
        else:
            raise AssertionError(boundary)


if __name__ == "__main__":
    import traceback

    fns = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = 0
    failed = 0
    for name, fn in fns:
        try:
            fn()
            passed += 1
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed of {len(fns)}")
    sys.exit(1 if failed else 0)
