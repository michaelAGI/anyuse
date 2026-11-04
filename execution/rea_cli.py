"""Explicit VM commands (``--allow-host`` runs them on the host); registering them never loads or installs REA."""
import json
from functools import wraps

import click

from . import rea_mapping as R


def errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
    return wrapped


def _replay_succeeded(result):
    """Keep deterministic and supported model-judged outcomes distinct."""
    return isinstance(result, dict) and (
        result.get("verdict") == "verified" or
        result.get("verdict") == "model_judged" and
        result.get("verification_tier") == "model_judged" and
        result.get("backend") == "guest_model_judged_state_v1")


@click.group("rea")
@click.option("--allow-host", is_flag=True,
              help="Run on this Mac instead of a macOS VM (REA decompiles and instruments apps here).")
def rea_commands(allow_host):
    """Investigate apps with REA, verify recovered adapters, and replay inside a VM."""
    if allow_host:
        R.allow_host()


@rea_commands.group("registered")
def registered_commands():
    """Make an already-proved guest catalog available to agents and MCP."""


@registered_commands.command("add")
@click.argument("name")
@click.argument("catalog", type=click.Path(exists=True, dir_okay=False))
@click.option("--require-restoration", is_flag=True,
              help="Require a trusted fresh restoration declaration for every execution.")
@errors
def registered_add(name, catalog, require_restoration):
    from .rea_catalog import CatalogStore
    click.echo(json.dumps(CatalogStore().register(name, catalog, require_restoration=require_restoration), indent=2))


@registered_commands.command("remove")
@click.argument("name")
@errors
def registered_remove(name):
    from .rea_catalog import CatalogStore
    CatalogStore().unregister(name)
    click.echo(json.dumps({"removed": name}))


@registered_commands.command("list")
@errors
def registered_list():
    from .rea_catalog import CatalogStore
    click.echo(json.dumps(CatalogStore().list_tools(), indent=2))


@rea_commands.command("setup")
@click.option("--prefix", type=click.Path(file_okay=False),
              help="Install folder (default ~/anyuse-rea in a VM, ~/.anyuse/rea-tools with --allow-host).")
@click.option("--allow-host", "allow_host_here", is_flag=True,
              help="Install on this Mac instead of a VM (same as `anyuse map rea --allow-host setup`).")
@errors
def setup(prefix, allow_host_here):
    """Install Node, a JDK, Ghidra and REA at pinned, checksummed versions into one folder."""
    if allow_host_here:
        R.allow_host()
    R.assert_guest()
    from pathlib import Path
    from . import rea_setup as S
    on_host = R.host_allowed()
    receipt = S.setup(Path(prefix) if prefix else S.default_prefix(on_host), on_host=on_host,
                      say=lambda line: click.echo(line, err=True))
    click.echo(json.dumps(receipt, indent=2))
    if receipt["ghidra_ready"]:
        import shlex
        click.echo(f"\nReady. Load the tools with:  . {shlex.quote(receipt['env'])}", err=True)
    else:
        raise click.ClickException(f"REA is installed but its Ghidra check failed; see {receipt['doctor']}")


@rea_commands.group("agent")
def agent_commands():
    """Persistent investigation tools for a reasoning agent; VM only."""


@rea_commands.group("workflow")
def workflow_commands():
    """Generic discovery, public setup, independent trials and saved reuse."""


@rea_commands.command("qt-sources")
@click.option("--out", type=click.Path(), help="New reusable source cache (defaults to the supported Qt commit under AnyUse data).")
@errors
def qt_sources(out):
    """Retain seven hash-pinned public Qt source files without executing them."""
    from . import rea_qt_sources as sources
    click.echo(json.dumps(sources.setup(out), ensure_ascii=False, indent=2))


def _apply_cli_trial_overrides(target_dict, effort, trials_learn, trials_fresh, trials_heldout, require_restart):
    import copy
    from . import rea_generic_workflow as W
    from . import rea_visual_campaign as VC
    out = copy.deepcopy(target_dict)
    if effort is not None:
        out["effort"] = effort
    overrides = {}
    if trials_learn is not None:
        overrides["min_learning"] = trials_learn
    if trials_fresh is not None:
        overrides["min_verification"] = trials_fresh
    if trials_heldout is not None:
        overrides["min_heldout"] = trials_heldout
    if require_restart is not None:
        overrides["require_restart"] = require_restart
    if effort is not None or overrides:
        plan = out.get("plan")
        if effort is not None:
            # Explicit CLI effort replaces any earlier preset or custom base.
            base = VC.validate_trial_policy(effort)
        else:
            # Resolve aliases before canonical CLI keys replace their values.
            base_spec = W._creation_spec(out) if out.get("format") == W.FORMAT else out
            _, _, base = W._resolve_spec_modes(base_spec)
            if base is None and isinstance(plan, dict) and "trial_policy" in plan:
                base = VC.validate_trial_policy(plan["trial_policy"])
            base = base or VC.validate_trial_policy()
        effective = VC.validate_trial_policy({**base, **overrides})
        out["trial_policy"] = effective
        if isinstance(plan, dict):
            # Keep the declared rows; coverage validation rejects too few trials.
            plan["trial_policy"] = copy.deepcopy(effective)
    return out


def _workflow_observer_config(provider, base_url, model, api_key_env, price_in=None, price_out=None):
    """Resolve observer flags without reading credentials or contacting a model."""
    from .model_adapter import PROVIDER_PRESETS, ProviderPreset
    if provider == "local":
        # Preserve the initializer's existing Ollama defaults.
        preset = ProviderPreset("http://127.0.0.1:11434/v1", "llava", "")
    elif provider == "openai_compatible":
        # A custom service has no known endpoint, model, or credential name.
        preset = ProviderPreset("", "", "")
    else:
        preset = PROVIDER_PRESETS[provider]
    config = {
        "provider": provider,
        "base_url": preset.base_url if base_url is None else base_url,
        "model": preset.model if model is None else model,
        "api_key_env": preset.api_key_env if api_key_env is None else api_key_env,
    }
    for field, flag in (("base_url", "--observer-base-url"), ("model", "--observer-model")):
        if not config[field].strip():
            raise ValueError(f"{flag} is required for observer provider {provider!r}")
    if price_in is not None:
        config["price_in"] = price_in
    if price_out is not None:
        config["price_out"] = price_out
    # Validation below requires explicit prices for a hosted origin.
    from .rea_visual_effect import _model_config
    _model_config({"retries": 0, "timeout": 30, **config})
    return config


@workflow_commands.command("init")
@click.argument("out", type=click.Path(dir_okay=False))
@click.option("--id", "spec_id", default=None, help="Workflow identifier (defaults to a slug of --task).")
@click.option("--task", required=True, help="Desired operation in plain words.")
@click.option("--before", required=True, help="Visible starting state before the operation.")
@click.option("--after", required=True, help="Desired visible state after the operation.")
@click.option("--window-title", default=None, help="Optional expected window title.")
@click.option("--observer-provider", default="local", show_default=True,
              type=click.Choice(["local", "openai_compatible", "openai", "anthropic", "gemini"]),
              help="Vision observer model provider.")
@click.option("--observer-base-url", default=None,
              help="Observer API URL (provider preset; local: http://127.0.0.1:11434/v1; required for openai_compatible).")
@click.option("--observer-model", default=None,
              help="Observer model (provider preset; local: llava; required for openai and openai_compatible).")
@click.option("--observer-api-key-env", default=None,
              help="API-key environment variable (provider preset; empty for local/custom; pass an empty value for no key).")
@click.option("--observer-price-in", type=click.FloatRange(min=0), default=None,
              help="Input price in USD per million tokens (required for hosted observers).")
@click.option("--observer-price-out", type=click.FloatRange(min=0), default=None,
              help="Output price in USD per million tokens (required for hosted observers).")
@click.option("--effort", type=click.Choice(["default", "fast", "strict", "rea-fast", "rea-strict"]), default=None,
              help="Mapping effort preset (rea-fast enables REA with 1/1/1 trials; rea-strict uses 3/3/3).")
@click.option("--trials-learn", type=click.IntRange(1, 32), default=None, help="Override minimum learning trials.")
@click.option("--trials-fresh", type=click.IntRange(1, 32), default=None, help="Override minimum fresh verification trials.")
@click.option("--trials-heldout", type=click.IntRange(1, 32), default=None, help="Override minimum held-out trials.")
@click.option("--require-restart/--no-require-restart", default=None, help="Override whether held-out phase requires a restart.")
@errors
def workflow_init(out, spec_id, task, before, after, window_title, observer_provider, observer_base_url,
                  observer_model, observer_api_key_env, observer_price_in, observer_price_out, effort, trials_learn, trials_fresh, trials_heldout, require_restart):
    """Write a validated generic REA workflow task specification without hand-editing JSON."""
    from pathlib import Path
    from . import rea_generic_workflow as W
    dest = Path(out).resolve()
    if dest.exists():
        raise ValueError("workflow specification file already exists: " + str(dest))
    observer = _workflow_observer_config(observer_provider, observer_base_url, observer_model, observer_api_key_env,
                                         observer_price_in, observer_price_out)
    raw = W.build_spec(task, before, after, spec_id=spec_id, observer_model=observer,
                       effort=effort, window_title=window_title)
    spec = _apply_cli_trial_overrides(raw, effort, trials_learn, trials_fresh, trials_heldout, require_restart)
    W.task_contract({
        "bundle_id": "com.example.App",
        "version": "1.0",
        "build": "1",
        "files": 1,
        "bytes": 1,
        "sha256": "a" * 64,
    }, spec)
    R.write_json(dest, spec)
    click.echo(json.dumps(spec, ensure_ascii=False, indent=2))


@workflow_commands.command("begin")
@click.argument("bundle", type=click.Path(exists=True, file_okay=False))
@click.argument("spec", type=click.Path(exists=True, dir_okay=False))
@click.option("--rea", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--effort", type=click.Choice(["default", "fast", "strict", "rea-fast", "rea-strict"]), default=None,
              help="Mapping effort preset (rea-fast enables REA with 1/1/1 trials; rea-strict uses 3/3/3).")
@click.option("--trials-learn", type=click.IntRange(1, 32), default=None, help="Override minimum learning trials.")
@click.option("--trials-fresh", type=click.IntRange(1, 32), default=None, help="Override minimum fresh verification trials.")
@click.option("--trials-heldout", type=click.IntRange(1, 32), default=None, help="Override minimum held-out trials.")
@click.option("--require-restart/--no-require-restart", default=None, help="Override whether held-out phase requires a restart.")
@errors
def workflow_begin(bundle, spec, rea, out, effort, trials_learn, trials_fresh, trials_heldout, require_restart):
    """Freeze the intended effect and trial contract before model discovery."""
    from . import rea_generic_workflow as W
    loaded = _apply_cli_trial_overrides(R.load_json(spec), effort, trials_learn, trials_fresh, trials_heldout, require_restart)
    click.echo(json.dumps(W.begin(bundle, rea, loaded, out), ensure_ascii=False, indent=2))


@workflow_commands.command("context")
@click.argument("root", type=click.Path(exists=True, file_okay=False))
@errors
def workflow_context(root):
    from . import rea_generic_workflow as W
    click.echo(json.dumps(W.context(root), ensure_ascii=False, indent=2))


@workflow_commands.command("ui-trace")
@click.argument("trace", type=click.Path(exists=True, dir_okay=False))
@click.option("--bundle-id", default=None, help="Select historical actuation windows for this app.")
@errors
def workflow_ui_trace(trace, bundle_id):
    """Read redacted GUI windows for mapping; does not attach, invoke or prove."""
    from . import rea_generic_workflow as W
    click.echo(json.dumps(W.ui_trace_context(trace, bundle_id=bundle_id), ensure_ascii=False, indent=2))


@workflow_commands.command("advance")
@click.argument("root", type=click.Path(exists=True, file_okay=False))
@click.option("--continue-discovery", is_flag=True, help="Explicitly continue a healthy stopped/bounded investigation using retained memory.")
@errors
def workflow_advance(root, continue_discovery):
    """Use the configured reasoning model, then yield ordinary public setup."""
    from . import rea_generic_workflow as W
    click.echo(json.dumps(W.advance(root, continue_discovery=continue_discovery), ensure_ascii=False, indent=2))


@workflow_commands.command("public-start")
@click.argument("root", type=click.Path(exists=True, file_okay=False))
@errors
def workflow_public_start(root):
    """Journal a setup/restart request before the caller uses ordinary UI."""
    from . import rea_generic_workflow as W
    click.echo(json.dumps(W.public_start(root), ensure_ascii=False, indent=2))


@workflow_commands.command("public-finish")
@click.argument("root", type=click.Path(exists=True, file_okay=False))
@click.option("--evidence", multiple=True, required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--restart", type=click.Path(exists=True, dir_okay=False), help="Independently witnessed restart entry for a pending restart.")
@errors
def workflow_public_finish(root, evidence, restart):
    """Submit actual public observations; independent trial checks still run."""
    from pathlib import Path
    from . import rea_generic_workflow as W
    result = W.public_finish(root, [W._pin(Path(file).resolve(strict=True)) for file in evidence],
                             restart=R.load_json(restart) if restart else None)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@workflow_commands.command("restart")
@click.argument("root", type=click.Path(exists=True, file_okay=False))
@click.option("--timeout", type=click.IntRange(1, 60), default=30)
@errors
def workflow_restart(root, timeout):
    """Witness one previously authorized normal app restart; never force quit."""
    from . import rea_generic_workflow as W
    click.echo(json.dumps(W.public_restart(root, timeout=timeout), ensure_ascii=False, indent=2))


@agent_commands.command("begin")
@click.argument("bundle", type=click.Path(exists=True, file_okay=False))
@click.argument("spec", type=click.Path(exists=True, dir_okay=False))
@click.option("--rea", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--max-steps", type=click.IntRange(8, 64), default=24)
@click.option("--budget-seconds", type=click.IntRange(30, 7200), default=1800)
@click.option("--observer-profile", type=click.Path(exists=True, dir_okay=False),
              help="Freeze an independently authored observer declaration for the agent's proposals.")
@click.option("--native-contract-mode", type=click.Choice(["attributable", "declared"]), default="attributable",
              help="Explicitly opt into model-declared native ABI hypotheses for a model-judged visual campaign.")
@click.option("--native-path-review", type=click.Path(exists=True, dir_okay=False),
              help="Freeze a separate native receiver source-review model policy before discovery.")
@click.option("--native-receiver-mode", type=click.Choice(["binary_runtime_hypothesis"]), default=None,
              help="Opt into experimental source-free receiver hypotheses; requires declared native types and visual verification.")
@click.option("--trials-learn", type=click.IntRange(1, 32), default=None, help="Override minimum learning trials.")
@click.option("--trials-fresh", type=click.IntRange(1, 32), default=None, help="Override minimum fresh verification trials.")
@click.option("--trials-heldout", type=click.IntRange(1, 32), default=None, help="Override minimum held-out trials.")
@click.option("--require-restart/--no-require-restart", default=None, help="Override whether held-out phase requires a restart.")
@errors
def agent_begin(bundle, spec, rea, out, max_steps, budget_seconds, observer_profile, native_contract_mode, native_path_review,
                native_receiver_mode, trials_learn, trials_fresh, trials_heldout, require_restart):
    """Freeze task, typed inputs and evaluation cases before investigation."""
    R.assert_guest()
    from . import rea_agent as G
    value = R.load_json(spec)
    if not isinstance(value, dict) or set(value) != {"task", "params", "success_criteria", "plan"}:
        raise ValueError("spec needs task, params, success_criteria and plan")
    plan = value["plan"]
    if isinstance(plan, dict) and any(x is not None for x in (trials_learn, trials_fresh, trials_heldout, require_restart)):
        plan = _apply_cli_trial_overrides(plan, None, trials_learn, trials_fresh, trials_heldout, require_restart)
    result = G.begin(bundle, rea, value["task"], value["params"], value["success_criteria"], plan, out,
                     max_steps=max_steps, budget_seconds=budget_seconds,
                     observer_profile=R.load_json(observer_profile) if observer_profile else None,
                     native_contract_mode=native_contract_mode,
                     native_path_review=R.load_json(native_path_review) if native_path_review else None,
                     **({"native_receiver_mode": native_receiver_mode} if native_receiver_mode is not None else {}))
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@agent_commands.command("context")
@click.argument("session", type=click.Path(exists=True, file_okay=False))
@errors
def agent_context(session):
    """Return tools, evidence, frozen goal, history and remaining budget."""
    from . import rea_agent as G
    click.echo(json.dumps(G.context(session), ensure_ascii=False, indent=2))


@agent_commands.command("step")
@click.argument("session", type=click.Path(exists=True, file_okay=False))
@click.argument("request", type=click.Path(exists=True, dir_okay=False))
@errors
def agent_step(session, request):
    """Run one agent-selected tool and return observations or failure feedback."""
    R.assert_guest()
    from . import rea_agent as G
    click.echo(json.dumps(G.step(session, R.load_json(request)), ensure_ascii=False, indent=2))


@agent_commands.command("run")
@click.argument("session", type=click.Path(exists=True, file_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--max-decisions", type=click.IntRange(1, 64), default=12)
@click.option("--timeout-seconds", type=click.IntRange(1, 7200), default=300)
@click.option("--max-context-chars", type=click.IntRange(4096, 250000), default=32000)
@click.option("--model-max-tokens", type=click.IntRange(256, 32768), default=4096,
              help="Maximum output tokens for each reasoning call")
@click.option("--model-timeout-seconds", type=click.IntRange(1, 7200),
              help="Per-call timeout within the total driver budget (default at most 60 seconds)")
@click.option("--tool", "tools", multiple=True,
              help="Admit only these G tools (repeatable); omitted admits the session tool set.")
@click.option("--static-only", is_flag=True,
              help="Admit only native Ghidra analysis, symbols and evidence/resource reads; no GUI or runtime.")
@click.option("--decision-protocol", type=click.Choice(["request_json_v1", "tool_object_v1", "full_native_v1", "inspector_structured_v1"]),
              help="Select JSON text, general G tool objects, or specialized native/inspector objects.")
@click.option("--decision-corrections", type=click.IntRange(0, 3), default=0,
              help="Bounded new decisions after rejected object-protocol requests; no tool retry or budget extension.")
@click.option("--proposal-evidence", type=click.Choice(["native_objc_v1"]),
              help="Check cited raw Objective-C actor evidence before proposal; no native operation or task proof.")
@click.option("--discovery-playbook", type=click.Choice(["objc_appkit_v1", "inspector_v1"]),
              help="Optional AppKit guidance or explicit inspector-only discovery guidance; no automatic operations.")
@click.option("--discovery-routine", type=click.Choice(["objc_appkit_receiver_v1"]),
              help="Opt into measured AppKit receiver getter execution before model decisions.")
@click.option("--discovery-route", type=click.Choice(["main_window", "key_window"]),
              help="One receiver route; defaults to main_window only when the routine is selected. No fallback.")
@click.option("--discovery-class-query", multiple=True,
              help="Optional task-literal class query after the selected routine; at most four, 32 rows each.")
@click.option("--resume-from", type=click.Path(exists=True, file_okay=False),
              help="Restore verified investigation evidence from a prior driver output for this same session.")
@click.option("--until", type=click.Choice(["hypothesis", "step", "published"]), default="hypothesis")
@errors
def agent_run(session, out, max_decisions, timeout_seconds, max_context_chars, model_max_tokens, model_timeout_seconds,
              tools, static_only, decision_protocol, decision_corrections, proposal_evidence, discovery_playbook,
              discovery_routine, discovery_route, discovery_class_query, resume_from, until):
    """Drive an existing mapping session with the configured reasoning model.

    Uses only the session's tools; saves every decision and stops for public
    setup between trials. Typed choice models use choose-view instead.
    """
    from . import rea_agent_driver as D
    correction_options = {"max_decision_corrections": decision_corrections} if decision_corrections else {}
    if proposal_evidence is not None:
        correction_options["proposal_evidence"] = proposal_evidence
    if discovery_playbook is not None:
        correction_options["discovery_playbook"] = discovery_playbook
    if discovery_routine is not None or discovery_route is not None:
        correction_options.update(discovery_routine=discovery_routine, discovery_route=discovery_route)
    if discovery_class_query:
        correction_options["discovery_class_queries"] = list(discovery_class_query)
    result = D.run(session, out, max_decisions=max_decisions,
                   timeout_seconds=timeout_seconds, max_context_chars=max_context_chars,
                   allowed_tools=tools or None, static_only=static_only, until=until,
                   decision_protocol=decision_protocol, resume_from=resume_from,
                   model_max_tokens=model_max_tokens, model_timeout_seconds=model_timeout_seconds, **correction_options)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@agent_commands.command("coordinate")
@click.argument("session", type=click.Path(exists=True, file_okay=False))
@click.argument("configuration", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--resume", is_flag=True, help="Continue only a completed-action boundary; failed or uncertain actions never retry.")
@click.option("--actions-per-run", type=click.IntRange(1, 256), default=256,
              help="Bound deterministic caller actions; the default completes the frozen campaign.")
@click.option("--checkpoint", type=click.Path(exists=True, dir_okay=False),
              help="Consume one pinned public-setup or restart observation for a visual campaign request.")
@errors
def agent_coordinate(session, configuration, out, resume, actions_per_run, checkpoint):
    """Prepare, record, restart, verify and publish an approved fixed-state campaign.

    Runs after the reasoning agent has proposed its approved hypothesis. Uses
    only fixed normal owned-app/public-control setup; no model or JSON callback.
    Configuration contains private media pins, exact ownership and finite bounds.
    """
    from . import rea_campaign_coordinator as coordinator
    cfg = R.load_json(configuration)
    if cfg.get("format") == coordinator.VISUAL_CONFIG:
        result = coordinator.run_visual(session, cfg, out, resume=resume,
                                        actions_per_run=actions_per_run, checkpoint=checkpoint)
    else:
        if checkpoint is not None:
            raise ValueError("public checkpoint is only supported by the visual coordinator")
        result = coordinator.run(session, cfg, out, resume=resume, actions_per_run=actions_per_run)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] not in {"completed", "ready", "needs_setup", "needs_restart"}:
        raise click.ClickException("campaign stopped without retry; review the retained coordinator journal")


@rea_commands.group("inspector")
def inspector_commands():
    """Explicit VM app setup for a declared local renderer inspector."""


def _lifecycle_result(value, expected):
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))
    if value["status"] != expected:
        raise click.ClickException("inspector lifecycle incomplete; inspect the receipt before another action")


@inspector_commands.command("launch")
@click.argument("policy", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--previous", type=click.Path(exists=True, dir_okay=False))
@click.option("--timeout", type=click.IntRange(1, 120), default=60)
@errors
def inspector_launch(policy, out, previous, timeout):
    """Launch once from a pinned policy; reuse an explicitly closed prior launch."""
    R.assert_guest()
    from . import rea_inspector_lifecycle as L
    _lifecycle_result(L.launch(policy, out, previous=previous, timeout=timeout), "ready")


@inspector_commands.command("inspect")
@click.argument("receipt", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout", type=click.IntRange(1, 60), default=30)
@errors
def inspector_inspect(receipt, out, timeout):
    """Recheck owned process, listener, renderer and profile metadata."""
    R.assert_guest()
    from . import rea_inspector_lifecycle as L
    _lifecycle_result(L.inspect(receipt, out, timeout=timeout), "inspected")


@inspector_commands.command("close")
@click.argument("receipt", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout", type=click.IntRange(1, 60), default=15)
@errors
def inspector_close(receipt, out, timeout):
    """Request normal termination of the same owned app, without escalation."""
    R.assert_guest()
    from . import rea_inspector_lifecycle as L
    _lifecycle_result(L.close(receipt, out, timeout=timeout), "closed")


@inspector_commands.command("review-failed")
@click.argument("receipt", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout", type=click.IntRange(1, 60), default=30)
@errors
def inspector_review_failed(receipt, out, timeout):
    """Review persisted ownership after failed startup; the launch stays failed."""
    R.assert_guest()
    from . import rea_inspector_recovery as E
    _lifecycle_result(E.review_failed(receipt, out, timeout=timeout), "resource_reviewed")


@inspector_commands.command("close-failed")
@click.argument("receipt", type=click.Path(exists=True, dir_okay=False))
@click.option("--review", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout", type=click.IntRange(1, 60), default=15)
@errors
def inspector_close_failed(receipt, review, out, timeout):
    """Normally close the same reviewed failed startup, without task retry."""
    R.assert_guest()
    from . import rea_inspector_recovery as E
    _lifecycle_result(E.close_failed(receipt, review, out, timeout=timeout), "closed")


@rea_commands.command("runtime-doctor")
@click.option("--node", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--modules", required=True, type=click.Path(exists=True, file_okay=False),
              help="Explicit existing node_modules cache; never installs dependencies")
@click.option("--adapter", type=click.Path(exists=True, dir_okay=False),
              help="Also check an existing bundle and its adjacent source/build manifest")
@errors
def runtime_doctor(node, modules, adapter):
    """Read-only offline build check; does not load Frida or inspect an app."""
    from . import rea_runtime_build as B
    result = B.doctor(node, modules, adapter=adapter)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["build_ready"]:
        raise click.ClickException("offline runtime build prerequisites are unavailable; see the JSON error")


@rea_commands.command("runtime-setup")
@click.option("--node", required=True, type=click.Path(exists=True, dir_okay=False),
              help="Node installed by REA setup, with its retained authenticated archive")
@click.option("--out", required=True, type=click.Path(file_okay=False), help="New runtime dependency prefix")
@click.option("--timeout", type=click.IntRange(1, 3600), default=900)
@errors
def runtime_setup(node, out, timeout):
    """Install pinned native compiler dependencies into a fresh guest prefix."""
    R.assert_guest()
    from . import rea_runtime_setup as S
    result = S.setup(node=node, out=out, timeout=timeout)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "installed":
        raise click.ClickException("runtime dependency installation failed; inspect the retained setup.json")


@rea_commands.command("runtime-build")
@click.option("--node", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--modules", required=True, type=click.Path(exists=True, file_okay=False),
              help="Explicit existing node_modules cache; never installs dependencies")
@click.option("--out", required=True, type=click.Path(file_okay=False), help="New build directory")
@click.option("--timeout", type=click.IntRange(1, 600), default=120)
@errors
def runtime_build(node, modules, out, timeout):
    """Build only the fixed bundled adapter with the admitted offline tools."""
    from . import rea_runtime_build as B
    result = B.build(node, modules, out, timeout=timeout)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "built":
        raise click.ClickException("offline runtime build failed; inspect the retained build.json")


@rea_commands.command("runtime")
@click.argument("bundle", type=click.Path(exists=True, file_okay=False))
@click.argument("request", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout", type=click.IntRange(1, 60), default=15)
@errors
def runtime(bundle, request, out, timeout):
    """Inspect or invoke a typed live Objective-C boundary inside the guest."""
    R.assert_guest()
    from . import rea_runtime_bridge as B
    receipt = B.collect(bundle, out, R.load_json(request), timeout=timeout)
    click.echo(json.dumps(receipt, ensure_ascii=False, indent=2))
    if receipt["status"] not in {"observed", "executed"}:
        raise click.ClickException("runtime transaction incomplete; no retry or fallback")


@rea_commands.command("describe")
@click.argument("candidate", type=click.Path(exists=True, dir_okay=False))
@click.option("--proof", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--bundle", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--summary", required=True)
@click.option("--details", required=True)
@click.option("--out", required=True, type=click.Path())
@errors
def describe(candidate, proof, bundle, summary, details, out):
    """Bind an agent-authored action description to a measured local recipe."""
    from . import rea_actions as A
    click.echo(json.dumps(A.describe(R.load_json(candidate), R.load_json(proof), bundle,
                                    summary, details, out), ensure_ascii=False, indent=2))


@rea_commands.command("catalog")
@click.argument("entries", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@errors
def catalog(entries, out):
    """Expose descriptions only for recipes whose VM verification still matches."""
    from . import rea_actions as A
    click.echo(json.dumps(A.catalog(R.load_json(entries), out), ensure_ascii=False, indent=2))


@rea_commands.command("choose")
@click.argument("catalog", type=click.Path(exists=True, dir_okay=False))
@click.option("--task", required=True)
@click.option("--backend", type=click.Choice(["auto", "djev", "openjev", "laya", "jev", "reasoning"]), default=None)
@click.option("--fallback", type=click.Choice(["none", "reasoning"]), default="none")
@click.option("--presentation", type=click.Choice(["catalog_json_v1", "semantic_v2"]), default="catalog_json_v1")
@click.option("--out", required=True, type=click.Path())
@errors
def choose(catalog, task, backend, fallback, presentation, out):
    """Ask the configured typed model chooser to pick a verified action; does not execute."""
    from . import rea_actions as A
    _choose_view(A.prepare_selection(R.load_json(catalog), task), backend, out, presentation, fallback)


@rea_commands.command("choices")
@click.argument("catalog", type=click.Path(exists=True, dir_okay=False))
@click.option("--task", required=True)
@click.option("--out", required=True, type=click.Path())
@errors
def choices(catalog, task, out):
    """Check the guest catalog and export descriptions for a host model."""
    from pathlib import Path
    from . import rea_actions as A
    folder = Path(out).resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    view = A.prepare_selection(R.load_json(catalog), task)
    R.write_json(folder / "view.json", view)
    click.echo(json.dumps(view, ensure_ascii=False, indent=2))


def _choose_view(view, backend, out, presentation="catalog_json_v1", fallback="none"):
    from pathlib import Path
    from . import rea_actions as A
    from .rea_picker import configured_client
    from .model_adapter import RunBudget
    if fallback not in {"none", "reasoning"} or backend == "reasoning" and fallback == "reasoning":
        raise ValueError("fallback must name a distinct reasoning backend or none")
    view = A._choice_view(view)
    folder = Path(out).resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    R.write_json(folder / "view.json", view)
    client = fallback_client = None
    cleanup_records = []
    budget = RunBudget.from_env()
    try:
        client = configured_client(backend, budget=budget)
        if fallback != "none":
            fallback_client = configured_client(fallback, budget=budget)
        result = A.select_view(view, client=client, presentation=presentation, fallback_client=fallback_client)
    except Exception as exc:
        R.write_json(folder / "failure.json", {"status": "chooser_failed", "type": type(exc).__name__,
                                              "actuated": False})
        raise
    finally:
        for role, reader in (("primary", client), ("fallback", fallback_client)):
            if reader is None:
                continue
            close = getattr(reader, "close", None)
            try:
                if close is not None:
                    close()
                cleanup = getattr(reader, "cleanup", None)
            except Exception as exc:
                cleanup = {"attempted": True, "confirmed": False, "error_type": type(exc).__name__}
            if isinstance(cleanup, dict):
                cleanup_records.append({"role": role, "cleanup": cleanup})
        R.write_json(folder / "cleanup.json", cleanup_records)
    chosen_client = fallback_client if result.get("fallback_attempted") else client
    receipt = getattr(chosen_client, "last_receipt", None)
    if isinstance(receipt, dict):
        result["model_receipt"] = receipt
    for attempt in result.get("selection_attempts", []):
        reader = client if attempt["role"] == "primary" else fallback_client
        receipt = getattr(reader, "last_receipt", None)
        if isinstance(receipt, dict):
            attempt["selection"]["model_receipt"] = receipt
    result["chooser_cleanup"] = cleanup_records
    if any(row["cleanup"].get("attempted") and row["cleanup"].get("confirmed") is not True
           for row in cleanup_records):
        result.update(status="abstained", action=None, resource_review_required=True,
                      why="owned chooser runtime cleanup is unconfirmed")
    R.write_json(folder / "selection.json", result)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@rea_commands.command("choose-view")
@click.argument("view", type=click.Path(exists=True, dir_okay=False))
@click.option("--backend", type=click.Choice(["auto", "djev", "openjev", "laya", "jev", "reasoning"]), default=None)
@click.option("--fallback", type=click.Choice(["none", "reasoning"]), default="none")
@click.option("--presentation", type=click.Choice(["catalog_json_v1", "semantic_v2"]), default="catalog_json_v1")
@click.option("--out", required=True, type=click.Path())
@errors
def choose_view(view, backend, fallback, presentation, out):
    """Choose from exported descriptions on this machine; no app/VM access."""
    _choose_view(R.load_json(view), backend, out, presentation, fallback)


@rea_commands.command("execute")
@click.argument("catalog", type=click.Path(exists=True, dir_okay=False))
@click.argument("selection", type=click.Path(exists=True, dir_okay=False))
@click.option("--inputs", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout", type=click.IntRange(1, 300), default=300)
@errors
def execute(catalog, selection, inputs, out, timeout):
    """Execute one selected recipe with explicit inputs and normal proof/read-back."""
    from . import rea_actions as A
    result = A.execute(R.load_json(catalog), R.load_json(selection), R.load_json(inputs), out, timeout=timeout)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not _replay_succeeded(result):
        raise click.ClickException("selected replay not verified; no automatic retry")


@rea_commands.command("learn")
@click.argument("profile", type=click.Path(exists=True, dir_okay=False))
@click.argument("recordings", type=click.Path(exists=True, dir_okay=False))
@click.option("--bundle", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--out", required=True, type=click.Path())
@errors
def learn(profile, recordings, bundle, out):
    """Learn a data recipe from recorded invocations/AX trees; still unverified."""
    from . import rea_harness as H
    candidate = H.learn(R.load_json(profile), R.load_json(recordings), bundle, out)
    click.echo(json.dumps(candidate, ensure_ascii=False, indent=2))


@rea_commands.command("record")
@click.argument("profile", type=click.Path(exists=True, dir_okay=False))
@click.argument("invocation", type=click.Path(exists=True, dir_okay=False))
@click.option("--inputs", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--bundle", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout", type=click.IntRange(1, 300), default=30)
@errors
def record(profile, invocation, inputs, bundle, out, timeout):
    """Record one typed invocation with full independent output checks."""
    from . import rea_harness as H
    demo = H.record(R.load_json(profile), R.load_json(invocation), R.load_json(inputs), bundle, out, timeout)
    click.echo(json.dumps({"status": "observed", "proof_created": False, "inputs": demo["inputs"]}, indent=2))


@rea_commands.command("harness")
@click.argument("profile", type=click.Path(exists=True, dir_okay=False))
@click.argument("recordings", type=click.Path(exists=True, dir_okay=False))
@click.argument("cases", type=click.Path(exists=True, dir_okay=False))
@click.option("--heldout", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--bundle", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout", type=click.IntRange(1, 300), default=30)
@errors
def harness(profile, recordings, cases, heldout, bundle, out, timeout):
    """Learn, verify, freeze and check novel inputs through one shared driver."""
    from . import rea_harness as H
    result = H.run(R.load_json(profile), R.load_json(recordings), R.load_json(cases),
                   R.load_json(heldout), bundle, out, timeout)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@rea_commands.command("analyze")
@click.argument("bundle", type=click.Path(exists=True, file_okay=False))
@click.option("--rea", required=True, type=click.Path(exists=True, dir_okay=False),
              help="REA executable already installed inside the guest; never auto-installs")
@click.option("--out", required=True, type=click.Path(), help="New evidence directory inside the guest")
@click.option("--provider", type=click.Choice(["ghidra", "hopper", "ida"]), default="ghidra")
@click.option("--query", default="", help="A focused native search term")
@click.option("--search-kind", type=click.Choice(["strings", "procedures"]), default="strings",
              help="Search string data (default) or function names")
@click.option("--function", "function_name", default="",
              help="Decompile one function: Class::method:, -[Class method:] or an address")
@click.option("--javascript", default="", help="Bundle-relative JS directory or ASAR instead of native analysis")
@click.option("--timeout", type=click.IntRange(1, 900), default=300)
@errors
def analyze(bundle, rea, out, provider, query, search_kind, function_name, javascript, timeout):
    """Collect evidence; even successful decompilation does not verify an adapter."""
    report = R.investigate(bundle, rea, out, provider=provider, query=query, function=function_name,
                           javascript=javascript, timeout=timeout, search_kind=search_kind)
    click.echo(json.dumps(report, ensure_ascii=False, indent=2))


@rea_commands.command("inspect")
@click.argument("candidate", type=click.Path(exists=True, dir_okay=False))
@click.option("--proof", type=click.Path(exists=True, dir_okay=False))
@errors
def inspect(candidate, proof):
    """Inspect saved metadata offline; does not establish the current app fingerprint."""
    c = R.validate_candidate(R.load_json(candidate))
    if proof is None and c.get("backend") in R.HARNESS_BACKENDS:
        ready, why = False, "no matching verification receipt"
    else:
        ready, why = R.proof_status(c, R.load_json(proof) if proof else None, c["target"])
    out = {"id": c["id"], "receipt_matches_recorded_target": ready,
           "current_app_checked": False, "why": why}
    if c.get("backend") in {"guest_fixed_state_v1", "guest_model_judged_state_v1"}:
        from . import rea_actions as A
        out["contract"] = A._contract(c)
    click.echo(json.dumps(out, indent=2))


@rea_commands.command("verify")
@click.argument("candidate", type=click.Path(exists=True, dir_okay=False))
@click.argument("cases", type=click.Path(exists=True, dir_okay=False))
@click.option("--bundle", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout", type=click.IntRange(1, 300), default=30)
@errors
def verify(candidate, cases, bundle, out, timeout):
    """Run varied inputs and independently check file bytes or dictionary definitions."""
    result = R.verify(R.load_json(candidate), R.load_json(cases), bundle, out, timeout=timeout)
    click.echo(json.dumps(result, indent=2))


@rea_commands.command("replay")
@click.argument("candidate", type=click.Path(exists=True, dir_okay=False))
@click.option("--proof", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--inputs", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--bundle", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--out", required=True, type=click.Path())
@click.option("--timeout", type=click.IntRange(1, 300), default=300)
@errors
def replay(candidate, proof, inputs, bundle, out, timeout):
    """Rehash the app, invoke the measured adapter once, and verify the result."""
    result = R.replay(R.load_json(candidate), R.load_json(proof), R.load_json(inputs), bundle, out, timeout=timeout)
    click.echo(json.dumps(result, indent=2))
    if not _replay_succeeded(result):
        raise click.ClickException("replay not verified; no automatic retry")


@rea_commands.command("surfaces")
@click.argument("candidate", type=click.Path(exists=True, dir_okay=False))
@click.option("--proof", type=click.Path(exists=True, dir_okay=False))
@click.option("--bundle", required=True, type=click.Path(exists=True, file_okay=False))
@errors
def surfaces(candidate, proof, bundle):
    """Generate a surface record for the map, checking the actual guest bundle."""
    R.assert_guest()
    c = R.validate_candidate(R.load_json(candidate))
    surface = R.candidate_surface(c, R.load_json(proof) if proof else None, R.fingerprint(bundle),
                                  app=c["target"]["bundle_id"])
    from .surface_discovery import ladder_report
    click.echo(json.dumps(ladder_report([surface], target=c["target"]["bundle_id"]), indent=2))
