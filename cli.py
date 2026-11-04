"""
AnyUse Command Line Interface

Usage:
    anyuse "Open Safari"
    anyuse "Search for Python" -v
    anyuse workflow list
    anyuse credential set github_pass
"""
import glob
import os
import sys
import json
from pathlib import Path

# Add parent for imports during development
sys.path.insert(0, str(Path(__file__).parent.parent))

import click

from anyuse.core.result import AgentResult
from anyuse.execution.model_adapter import MODEL_SETUP_HINT as _MODEL_SETUP_HINT


# ─────────────────────────────────────────────────────────
# Main CLI Group
# ─────────────────────────────────────────────────────────

class TaskOrSubcommandGroup(click.Group):
    """Top-level group that accepts EITHER a free-form task OR a subcommand.

    ``@click.argument('task', required=False)`` on a Group makes click consume
    the first positional argument — which, for ``anyuse workflow list``, is the
    group name ``workflow`` — as the task. Everything after it is then resolved
    as a subcommand of the TOP-LEVEL group, so every grouped command failed with
    ``No such command 'list'``: ``workflow``, ``credential``, ``account``,
    ``guide`` and ``watcher`` were all unreachable, including the usages in this
    module's own docstring.

    Fix: when the first positional argument names a registered subcommand, parse
    without the ``task`` parameter so click routes normally. A real task
    (``anyuse "open Safari"``) is unaffected — a task string is not a command
    name — and ``anyuse -v workflow list`` still works because option values are
    skipped when looking for that first positional.
    """

    def _first_positional(self, args):
        """Index of the first argument that is neither an option nor its value."""
        takes_value = set()
        for param in self.params:
            if getattr(param, "is_flag", False):
                continue
            takes_value.update(getattr(param, "opts", []) or [])
            takes_value.update(getattr(param, "secondary_opts", []) or [])
        skip = False
        for i, arg in enumerate(args):
            if skip:
                skip = False
                continue
            if arg.startswith("-"):
                if "=" not in arg and arg in takes_value:
                    skip = True
                continue
            return i
        return None

    def _near_command(self, args, idx):
        """The visible command a mistyped first word most likely meant, when what follows makes a
        task unlikely (`anyuse wokflow list`: the next word is one of workflow's own subcommands)."""
        import difflib
        word = args[idx]
        if not word.replace("-", "").isalnum() or word.lower() != word:
            return None
        rest = [a for a in args[idx + 1:] if not a.startswith("-")]
        if not rest:
            return None                     # one word: the callback's own refusal handles it
        visible = sorted(n for n, c in self.commands.items() if not getattr(c, "hidden", False))
        close = difflib.get_close_matches(word, visible, n=1, cutoff=0.75)
        if not close:
            return None
        match = self.commands[close[0]]
        if isinstance(match, click.Group):
            return close[0] if rest[0] in match.commands else None
        return close[0]

    def collect_usage_pieces(self, ctx):
        # the task argument shows in this command's own usage only, never in a subcommand's
        pieces = super().collect_usage_pieces(ctx)
        if "[TASK]" not in pieces:
            pieces.insert(1 if pieces and pieces[0] == self.options_metavar else 0, "[TASK]")
        return pieces

    def parse_args(self, ctx, args):
        idx = self._first_positional(args)
        if idx is not None and args[idx] not in self.commands:
            meant = self._near_command(args, idx)
            if meant:
                click.echo(f"Error: no such command '{args[idx]}'. Did you mean: {meant}?", err=True)
                ctx.exit(2)
        if idx is not None and args[idx] in self.commands:
            saved = self.params
            self.params = [p for p in saved if getattr(p, "name", "") != "task"]
            try:
                rest = super().parse_args(ctx, args)
            finally:
                self.params = saved
            ctx.params.setdefault("task", None)
            return rest
        return super().parse_args(ctx, args)


class _TaskArgument(click.Argument):
    """The root's free-form task: listed once in the root's usage (TaskOrSubcommandGroup), never in
    a subcommand's usage line (`anyuse workflow play ...`, not `anyuse [TASK] workflow play ...`)."""

    def get_usage_pieces(self, ctx):
        return []


def _anyuse_version() -> str:
    """The installed distribution's version, else the package's own string (a clone run without installing)."""
    import importlib.metadata
    try:
        return importlib.metadata.version("anyuse")
    except importlib.metadata.PackageNotFoundError:
        from anyuse import __version__
        return __version__


def _show_version(ctx, _param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"anyuse, version {_anyuse_version()}")
    ctx.exit()


@click.group(cls=TaskOrSubcommandGroup, invoke_without_command=True)
@click.option('--version', is_flag=True, expose_value=False, is_eager=True, callback=_show_version,
              help='Show the version and exit.')
@click.argument('task', required=False, cls=_TaskArgument)
@click.option('--max-turns', '-m', default=20, help='Maximum turns in agent loop')
@click.option('--verbose', '-v', is_flag=True, help='Verbose output')
@click.option('--auto-confirm', '-y', is_flag=True, help='Skip safety confirmations')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--api-key', envvar='GEMINI_API_KEY', hidden=True,
              help='Deprecated; configure models with ANYUSE_MODEL_* (docs/MODEL_CONFIGURATION.md)')
# Feature toggles
@click.option('--no-plan', is_flag=True, help='Disable task planning')
@click.option('--no-memory', is_flag=True, help='Disable workflow memory/learning')
@click.option('--no-credentials', is_flag=True, help='Disable credential vault')
@click.option('--no-learn', is_flag=True, help='Disable auto-learning from executions')
@click.option('--no-replay', is_flag=True, help='Disable auto-replay of learned workflows')
@click.option('--dry-run', is_flag=True, hidden=True, help='Not available in this release')
# Verification options
@click.option('--no-verify', is_flag=True, help='Disable all verification (faster but less safe)')
@click.option('--verify-every', type=int, default=0, help='Verify every N steps (0=use smart checkpoints)')
@click.option('--no-verify-smart', is_flag=True, help='Disable smart checkpoints (use --verify-every instead)')
# Credential mode options
@click.option('--agent-mode', is_flag=True, hidden=True, help='Agent mode: create own accounts with secure passwords')
@click.option('--preferred-sso', type=click.Choice(['google', 'microsoft', 'apple', 'github']), hidden=True,
              help='Preferred SSO provider (if available)')
@click.option('--sso-account', hidden=True, help='Email/account for SSO login')
@click.option('--sso-password-alias', hidden=True, help='Keychain alias for SSO password')
@click.option('--sso-2fa', type=click.Choice(['totp', 'sms', 'push']), hidden=True, help='2FA method for SSO')
@click.option('--sso-totp-alias', hidden=True, help='Keychain alias for SSO TOTP secret')
@click.pass_context
def cli(ctx, task, max_turns, verbose, auto_confirm, json_output, api_key,
        no_plan, no_memory, no_credentials, no_learn, no_replay, dry_run,
        no_verify, verify_every, no_verify_smart,
        agent_mode, preferred_sso, sso_account, sso_password_alias, sso_2fa, sso_totp_alias):
    """
    AnyUse: turn a task done once into a tool that runs again without a model.

    \b
    Compile and replay (web, no desktop access):
        anyuse doctor
        anyuse workflow compile NAME --from-har one.har --har-param q=V1 \\
            --har-expect 'a|b|c' --expect V2='d|e|f' --expect V3='g|h|i'
        anyuse workflow play NAME --param q=VALUE
        anyuse learn "search this site for {q}" --url https://example.org --values a,b,c,d
        python -P -m anyuse.mcpserver         # serve proved tools over MCP

    \b
    Desktop (macOS; a VM is recommended):
        anyuse "Open TextEdit and create a note"     # a natural-language task
        anyuse workflow play NAME

    \b
    Models: compiled replays need none. Agent tasks need no API key with
    ANYUSE_MODEL_PROVIDER=claude-cli (or codex-cli), which uses the CLI you
    are logged into; any OpenAI-compatible endpoint works too
    (docs/MODEL_CONFIGURATION.md).
    """
    # a product process: model requests are checked against the global daily
    # cap and booked in the spend ledger (execution/spend.py)
    from anyuse.execution import spend as _spend
    _spend.arm()
    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose
    ctx.obj['json_output'] = json_output
    ctx.obj['api_key'] = api_key
    ctx.obj['max_turns'] = max_turns
    ctx.obj['auto_confirm'] = auto_confirm
    # Feature toggles
    ctx.obj['no_plan'] = no_plan
    ctx.obj['no_memory'] = no_memory
    ctx.obj['no_credentials'] = no_credentials
    ctx.obj['no_learn'] = no_learn
    ctx.obj['no_replay'] = no_replay
    ctx.obj['dry_run'] = dry_run
    ctx.obj['no_verify'] = no_verify
    ctx.obj['verify_every'] = verify_every
    ctx.obj['no_verify_smart'] = no_verify_smart
    # Credential mode
    ctx.obj['agent_mode'] = agent_mode
    ctx.obj['preferred_sso'] = preferred_sso
    ctx.obj['sso_account'] = sso_account
    ctx.obj['sso_password_alias'] = sso_password_alias
    ctx.obj['sso_2fa'] = sso_2fa
    ctx.obj['sso_totp_alias'] = sso_totp_alias
    
    if ctx.invoked_subcommand is None:
        if not task:
            click.echo(ctx.get_help())
            return
        if len(task.split()) == 1:
            _refuse_unknown_command(ctx, task)
        _run_task(ctx, task)


def _refuse_unknown_command(ctx, word: str):
    """A single word is a mistyped command, never a desktop task."""
    import difflib
    close = difflib.get_close_matches(word, sorted(ctx.command.commands), n=3, cutoff=0.6)
    click.echo(f"Error: no such command '{word}'." + (f" Did you mean: {', '.join(close)}?" if close else ""), err=True)
    click.echo('A desktop task is a sentence in quotes, e.g. anyuse "Open TextEdit and create a note".', err=True)
    click.echo("See anyuse --help.", err=True)
    sys.exit(2)


def _model_configured() -> bool:
    """A provider or base URL is set globally or for a role, in the environment or in settings.toml
    (or the legacy GEMINI_API_KEY default is present)."""
    if os.environ.get("GEMINI_API_KEY"):
        return True
    from anyuse.execution.model_adapter import provider_configured
    try:
        return any(provider_configured(role) for role in ("planner", "visual", "agent", "judge"))
    except Exception:  # noqa: BLE001 - an unreadable settings file configures nothing
        return False


def _run_task(ctx, task: str):
    """Run a task with the agent."""
    api_key = ctx.obj.get('api_key')
    if ctx.obj.get('dry_run'):
        click.echo("Error: --dry-run is not available in this release.", err=True)
        sys.exit(2)
    if not _model_configured():
        click.echo(_MODEL_SETUP_HINT, err=True)
        sys.exit(1)
    
    from anyuse.core.agent import Agent
    
    # Build agent with all options (matching Agent.__init__ exactly)
    credential_mode = 'agent' if ctx.obj.get('agent_mode') else 'user'
    
    agent = Agent(
        api_key=api_key,
        max_turns=ctx.obj.get('max_turns', 20),
        auto_confirm=ctx.obj.get('auto_confirm', False),
        verbose=ctx.obj.get('verbose', False),
        # Feature toggles
        enable_planning=not ctx.obj.get('no_plan', False),
        enable_memory=not ctx.obj.get('no_memory', False),
        enable_credentials=not ctx.obj.get('no_credentials', False),
        auto_learn=not ctx.obj.get('no_learn', False),
        auto_replay=not ctx.obj.get('no_replay', False),
        # Verification settings
        enable_verification=not ctx.obj.get('no_verify', False),
        verify_every_n_steps=ctx.obj.get('verify_every', 0),
        verify_smart=not ctx.obj.get('no_verify_smart', False),
        # Credential mode
        credential_mode=credential_mode,
        # SSO configuration
        preferred_sso=ctx.obj.get('preferred_sso'),
        sso_account=ctx.obj.get('sso_account'),
        sso_credential_alias=ctx.obj.get('sso_password_alias'),
        sso_2fa_method=ctx.obj.get('sso_2fa'),
        sso_totp_alias=ctx.obj.get('sso_totp_alias'),
    )
    
    # Show mode info
    if ctx.obj.get('verbose'):
        mode_info = "🤖 Agent mode" if ctx.obj.get('agent_mode') else "👤 User mode"
        click.echo(f"{mode_info}")
        if ctx.obj.get('preferred_sso'):
            click.echo(f"   SSO: {ctx.obj.get('preferred_sso')} ({ctx.obj.get('sso_account')})")
        click.echo()
    
    result = agent.run(task)
    
    if ctx.obj.get('json_output'):
        click.echo(result.to_json())
    else:
        _print_result(result, ctx.obj.get('verbose', False))
    
    sys.exit(0 if result.success else 1)


@cli.group()
def workflow():
    """Workflow management commands."""
    pass


@workflow.command('list')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.pass_context
def workflow_list(ctx, json_output):
    """List saved workflows: compiled web tools and recorded desktop workflows."""
    from anyuse.memory.storage import WorkflowStorage
    from anyuse.mcpserver.compiled_tools import list_compiled

    storage = WorkflowStorage()
    workflows = storage.list_all()
    try:
        web_tools = list_compiled(storage)
    except Exception:  # noqa: BLE001 - an unreadable store lists nothing, it does not crash the listing
        web_tools = []
    # saved web tools (agent_run with compile): a web tool, not a desktop workflow with no steps
    saved_web = {}
    from anyuse.execution.harness_compile import load_web_tool
    for wf in workflows:
        try:
            rec = load_web_tool(storage, wf.name)
        except Exception:  # noqa: BLE001 - an unreadable file is listed as a plain workflow
            rec = None
        if rec:
            saved_web[wf.name] = rec
    workflows = [wf for wf in workflows if wf.name not in saved_web]
    json_output = json_output or bool((ctx.obj or {}).get('json_output'))

    if not workflows and not web_tools and not saved_web and not json_output:
        click.echo("No workflows saved.")
        click.echo("\nMake a web tool (docs/QUICKSTART_COMPILE.md):")
        click.echo("  record one search as a HAR, then: anyuse workflow compile NAME --from-har one.har "
                   "--har-param q=VALUE --har-expect 'a|b|c' --expect OTHER='d|e|f' --expect THIRD='g|h|i'")
        click.echo("  or watch a browser you drive: anyuse attach --help")
        click.echo("or let an agent find the page once: anyuse learn --help")
        return

    if json_output:
        data = [{"name": t["workflow"], "kind": "web", "host": t.get("host"),
                 "proof": (t.get("proof") or {}).get("status"),
                 "proof_passed": (t.get("proof") or {}).get("passed"),
                 "host_acknowledged": t.get("host_acknowledged"),
                 "runnable_without_human": t.get("runnable_without_human"),
                 "available_via": t.get("available_via"),
                 **({"degraded": t["degraded"]} if t.get("degraded") else {}),
                 **({"recompile": t["recompile"]} if t.get("recompile") else {}),
                 **({"needs_browser": True, "note": t.get("note")} if t.get("needs_browser") else {})}
                for t in web_tools]
        data += [{"name": name, "kind": "saved_web_tool", "site": rec.get("site"),
                  "status": rec.get("status"), "input": rec.get("param") or "value"}
                 for name, rec in saved_web.items()]
        for wf in workflows:
            data.append({
                "name": wf.name,
                "kind": "desktop",
                "description": wf.description,
                "steps": len(wf.steps),
                "runs": wf.run_count,
                "success_rate": wf.success_rate,
                "verified": wf.verified,
                "robustness_score": wf.robustness_score,
                "issues": len(wf.verification_issues),
            })
        click.echo(json.dumps(data, indent=2))
        return

    if web_tools:
        click.echo("Compiled web tools:\n")
        for t in web_tools:
            proof = t.get("proof") or {}
            state = proof.get("status") if proof.get("passed") else f"not proved ({proof.get('status') or 'no proof'})"
            ack = "acknowledged" if t.get("host_acknowledged") else "host not acknowledged"
            click.echo(f"  {t['workflow']}  {t.get('host') or '?'}  {state}, {ack}")
            click.echo(f"      run: anyuse workflow play {t['workflow']} "
                       + " ".join(f"--param {p}=VALUE" for p in (t.get("parameters") or ["q"])))
            recompile = t.get("recompile") or {}
            if t.get("needs_browser"):
                click.echo(f"      {t.get('note')}"
                           + ("" if t.get("available_via") else "; none is configured now"))
            elif not (t.get("health") or {}).get("preferred", True):
                health = t.get("health") or {}
                via = t.get("available_via")
                when = health.get("next_check_after")
                again = f"AnyUse tries it again after {when}" if when else "AnyUse tries it again later"
                # a request out of service is checked again by itself (mcpserver/compiled_tools.run_compiled)
                if via == "api":
                    click.echo("      api out of service; the next call checks whether it answers again")
                elif via:
                    click.echo(f"      api out of service (served by the {via.replace('_', ' ')}); {again}")
                elif health.get("out_of_service_cause") in ("outage", "rate_limit"):
                    what = ("the site asked AnyUse to wait (rate limit)"
                            if health.get("out_of_service_cause") == "rate_limit" else
                            "the site did not answer")
                    click.echo(f"      api out of service: {what}. {again}; "
                               f"nothing needs recording again")
                else:
                    click.echo(f"      api out of service. {again}; if the site changed for good, "
                               f"record the search again and recompile")
                last = recompile.get("last_attempt") or {}
                nxt = recompile.get("next_attempt_after")
                if last.get("at"):
                    click.echo(f"      automatic recompile {last['at']}: {last.get('reason') or 'failed'}")
                if nxt == "never":
                    click.echo("      no automatic attempts left: record the search again and recompile")
                elif nxt:
                    click.echo(f"      next automatic recompile after {nxt}")
            elif (recompile.get("recompiled") or {}).get("at"):
                done = recompile["recompiled"]
                click.echo(f"      recompiled automatically {done['at']} from {done.get('from') or '?'}")
        click.echo()
    if saved_web:
        click.echo("Saved web tools (from agent runs):\n")
        for name, rec in saved_web.items():
            click.echo(f"  {name}  {rec.get('site') or '?'}  {rec.get('status')}, input "
                       f"{rec.get('param') or 'value'}")
        click.echo("\n  agents call them over MCP; `anyuse watch run` replays them too\n")
    if not workflows:
        return
    click.echo("Saved workflows:\n")
    for wf in workflows:
        compiled = _compiled_recording_summary(storage, wf)
        if compiled:
            # a recording compiled by from-draft: its steps live in the compiled record
            click.echo(f"  ▶ {wf.name}  (compiled recording)")
            click.echo(f"    {compiled}")
            click.echo()
            continue
        tried = (wf.success_count + wf.failure_count) > 0 or wf.run_count > 0
        success = f"{int(wf.success_rate * 100)}%" if tried else "not run yet"
        
        # Status: a recorded verification when there is one, else what its replays did
        if wf.verified:
            verify_icon = "✅"
            verify_info = f" | Robustness: {wf.robustness_score:.0%}"
        elif wf.verification_issues:
            verify_icon = "⚠️"
            verify_info = f" | {len(wf.verification_issues)} issue(s)"
        elif tried:
            verify_icon = "✅" if wf.success_rate >= 1.0 else "⚠️"
            verify_info = ""
        else:
            verify_icon = "❓"
            verify_info = f" | replay it: ANYUSE_DRIVE_ENABLED=on anyuse workflow play {wf.name}"
        
        click.echo(f"  {verify_icon} {wf.name}")
        click.echo(f"    Steps: {len(wf.steps)} | Runs: {wf.run_count} | Success: {success}{verify_info}")
        if wf.description:
            click.echo(f"    {wf.description}")
        click.echo()


@workflow.command('play')
@click.argument('name')
@click.option('--param', 'params', multiple=True, metavar='KEY=VALUE',
              help='Bind a parameter of a compiled web tool (repeatable): runs the proved '
                   'request; if it fails, the browser replay and then the agent answer '
                   '(when configured), never on your desktop')
@click.option('--no-fallback', is_flag=True,
              help='Run only the compiled request (a tool with no compiled request: only its '
                   'browser replay): no agent, no desktop path')
@click.option('--verify', is_flag=True, hidden=True, help='Not available in this release')
@click.pass_context
def workflow_play(ctx, name, params, no_fallback, verify):
    """Play a saved workflow.

    \b
    A compiled web tool (--param) runs the cheapest rung that answers:
      1. the proved request (milliseconds, no agent model);
      2. the saved browser replay, if a browser is configured
         (ANYUSE_CDP_ENDPOINT or ANYUSE_LEARN_ENDPOINT; seconds, no agent
         model);
      3. the agent with the tool's task, if a model is configured
         (model calls, counted and capped; logins, payments and sent forms
         stop and come back to you);
      4. you, with the reason (a CAPTCHA, a sign-in wall or a rate limit goes
         straight here).
    Before step 4, when the site changed, AnyUse heals the request from plain
    reads of its result page if it can (ANYUSE_AUTO_HEAL; no browser). Steps 1
    and 2 and a heal may ask a configured check model (ANYUSE_MODEL_CHECKS)
    where the page alone leaves a check open; those calls are counted.
    The answer names the rung that served it. --no-fallback keeps it to step 1.
    A recorded desktop workflow replays its steps instead (a VM is recommended);
    one compiled from a recording takes its values with --param, like a web tool.
    """
    as_json = bool(ctx.obj.get('json_output'))

    def stop(code, reason, text=None, **extra):
        # with --json every outcome is one JSON object, so a script or an agent can parse it
        if as_json:
            click.echo(json.dumps({"ok": False, "workflow": name, "reason": reason, **extra},
                                  indent=2, default=str))
        else:
            click.echo(text or f"✗ {reason}", err=True)
        sys.exit(code)

    if verify:
        stop(2, "--verify is not available in this release",
             "Error: --verify is not available in this release.")
    bound = {}
    for item in params:
        if '=' not in item:
            stop(2, f"--param expects KEY=VALUE, got {item!r}")
        k, v = item.split('=', 1)
        bound[k.strip()] = v
    if _is_compiled_recording(name):
        # a desktop recording compiled by from-draft replays through `workflow replay`
        return ctx.invoke(workflow_replay, name=name, params=params, json_output=as_json)
    desktop = _has_desktop_recording(name)
    if not params and not desktop:
        web = _web_tool_parameters(name)
        if web is not None:
            example = " ".join(f"--param {p}=VALUE" for p in (web or ["q"]))
            stop(2, f"{name} is a compiled web tool: run it with {example}",
                 parameters=web, next=f"anyuse workflow play {name} {example}")
        stop(1, f"no workflow named {name!r}",
             f"✗ Workflow '{name}' not found (anyuse workflow list shows the names).",
             next="anyuse workflow list shows the names")
    compiled_rung = None
    if params:
        from anyuse.mcpserver.compiled_tools import run_compiled
        out = run_compiled(name, bound, cascade=not no_fallback)
        next_lines = [] if out.get('ok') else _play_next_lines(name, out, desktop)
        # a search that listed no result of the asked-for name is an answer: the UI path would open another one
        finished = (out.get('ok') or no_fallback or out.get('action_required') == 'human' or not desktop
                    or out.get('not_found'))
        if as_json:
            if finished:
                if not out.get('ok'):
                    out = {**out, "reason": out.get('reason') or out.get('error') or "the compiled request failed"}
                    if next_lines:
                        out["next"] = next_lines[0].strip().removeprefix("Next: ")
                click.echo(json.dumps(out, indent=2, default=str))
                sys.exit(0 if out.get('ok') else 1)
            compiled_rung = {"ok": False, "reason": out.get('error')}
            click.echo("Compiled rung failed; continuing with the recorded UI path.", err=True)
        else:
            tier = out.get('ladder_tier', '')
            if out.get('ok'):
                served = str(out.get('served_by') or '')
                cost = float(out.get('cost_usd') or 0.0)
                click.echo(f"✓ {name}: served by {served.replace('_', ' ')}"
                           + (f" ({tier})" if served == 'api' else "")
                           + f", {out.get('latency_ms') or 0:.0f} ms, "
                           f"{out.get('model_calls', 0)} model calls"
                           + (f", US${cost:.4f}" if cost else ""))
                _echo_rungs(out)
                items = (out.get('result') or {})
                for key, val in items.items():
                    shown = val[:10] if isinstance(val, list) else val
                    click.echo(f"  {key}: {shown}")
                if out.get('next') and served != 'api':
                    click.echo(f"  Note: {out['next']}")
                recompiled = out.get('recompiled') or {}
                if recompiled.get('swapped'):
                    click.echo(f"  Recompiled: a new request was proved from "
                               f"{recompiled.get('from') or 'fresh evidence'}; the next call uses it")
                healed = out.get('healed') or {}
                if healed.get('swapped'):
                    click.echo(f"  Healed: {_heal_line(healed)}")
                if out.get('restored'):
                    click.echo("  Restored: the compiled request was out of service; it answered again, "
                               "passed its checks and is back in service")
            else:
                click.echo(f"✗ {name}: {out.get('error')}")
                _echo_rungs(out)
                heal = out.get('heal') or {}
                if heal.get('reason'):
                    after = str(heal.get('next_attempt_after') or "")
                    click.echo(f"  Not healed: {heal['reason']}"
                               + (f" (next attempt after {after})"
                                  if after and after not in heal['reason'] else ""))
                for line in next_lines:
                    click.echo(f"  {line}")
            if finished:
                sys.exit(0 if out.get('ok') else 1)
            click.echo("\nCompiled rung failed; continuing with the recorded UI path.")

    import contextlib
    from anyuse.core.agent import Agent

    try:
        # with --json the replay's own progress output goes to stderr; stdout carries the one JSON object
        with contextlib.redirect_stdout(sys.stderr) if as_json else contextlib.nullcontext():
            agent = Agent(
                api_key=ctx.obj.get('api_key'),
                verbose=ctx.obj.get('verbose', False),
                auto_confirm=ctx.obj.get('auto_confirm', False),
            )
            result = agent.play(name)
    except Exception as exc:  # noqa: BLE001 - with --json a crash is reported as JSON too
        if not as_json:
            raise
        stop(1, f"the desktop replay could not run: {type(exc).__name__}: {exc}",
             **({"compiled_rung": compiled_rung} if compiled_rung else {}))
    
    if verify and result.success:
        click.echo("\n🔍 Verifying replay result...")
        try:
            from agent.workflow_verifier import WorkflowVerifier
            from anyuse.memory.storage import WorkflowStorage
            from anyuse.context.screenshot import ScreenshotCapture
            
            storage = WorkflowStorage()
            workflow = storage.get(name) or storage.find(name)
            
            if workflow:
                verifier = WorkflowVerifier(storage=storage, verbose=ctx.obj.get('verbose', False))
                screenshot = ScreenshotCapture()
                final_screenshot = screenshot.capture()
                
                verify_result = verifier.verify_replay(workflow, final_screenshot)
                
                if verify_result.passed:
                    click.echo(f"✅ Verification passed (confidence: {verify_result.robustness_score:.0%})")
                else:
                    click.echo(f"⚠️ Verification issues:")
                    for issue in verify_result.issues:
                        click.echo(f"   - {issue.description}")
        except Exception as e:
            click.echo(f"⚠️ Verification failed: {e}")
    
    if as_json:
        data = result.to_dict()
        data["ok"] = bool(result.success)
        if not result.success:
            data["reason"] = result.error or result.message or "the replay failed"
        if compiled_rung:
            data["compiled_rung"] = compiled_rung
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        _print_result(result, ctx.obj.get('verbose', False))

    sys.exit(0 if result.success else 1)


@workflow.command('record')
@click.argument('name')
@click.option('--task', '-t', required=True, help='Task to record')
@click.option('--description', '-d', default='', help='Workflow description')
@click.pass_context
def workflow_record(ctx, name, task, description):
    """Record a new workflow."""
    from anyuse.core.agent import Agent
    
    api_key = ctx.obj.get('api_key')
    if not _model_configured():
        click.echo(_MODEL_SETUP_HINT, err=True)
        sys.exit(1)
    
    agent = Agent(
        api_key=api_key,
        verbose=True,
        auto_confirm=ctx.obj.get('auto_confirm', False),
    )
    
    click.echo(f"🔴 Recording workflow: {name}")
    click.echo(f"   Task: {task}\n")
    
    with agent.record(name):
        result = agent.run(task)
    
    if result.success:
        click.echo(f"\n✓ Workflow '{name}' recorded successfully")
    else:
        click.echo(f"\n⚠️ Task had issues but workflow was recorded")
        click.echo(f"   Error: {result.error}")


# ─────────────────────────────────────────────────────────
# The question before the proof replays against a new host
# ─────────────────────────────────────────────────────────

def _terminal_attached() -> bool:
    """The question can be shown and answered: it is written to stderr and read from stdin, so both
    must be a terminal (``> out.txt 2>&1`` at a terminal leaves stdin one, but nobody sees it)."""
    try:
        return bool(sys.stdin.isatty() and sys.stderr.isatty())
    except (AttributeError, ValueError):
        return False


def _robots_phrase(question) -> str:
    """What robots.txt said of the question's URL, in a few words."""
    import re as _re
    import urllib.parse as _up
    path = _up.urlsplit(question.url).path or "/"
    if question.robots_allowed is True:
        return f"robots.txt allows {path}"
    if question.robots_allowed is False:
        return f"robots.txt disallows {path}; your override applies"
    note = question.robots_note or ""
    if not note or note.startswith(("no robots.txt", "no usable robots.txt")):
        return "the host serves no robots.txt"
    status = _re.search(r"HTTP (\d{3})", note)
    if note.startswith("robots_unavailable_4xx") and status:
        return f"its robots.txt answered HTTP {status.group(1)}, so no rules apply"
    return note[:120]


def _host_question_text(question) -> str:
    from anyuse.execution.api_permission import PURPOSE_SAVE
    host, robots = question.host, _robots_phrase(question)
    if question.purpose == PURPOSE_SAVE:
        return f"Save this tool for {host}? Each call sends a request to {host} ({robots})."
    fewest, most = (tuple(question.requests or ()) + (0, 0))[:2]
    if most:
        count = f"{fewest}" if fewest == most else f"{fewest} to {most}"
        sends = f"This sends {count} request{'' if most == 1 else 's'} to {host}"
    else:
        sends = f"This sends the proof's requests to {host}"
    return f"Prove against {host} now? {sends} ({robots})."


class _NewHostQuestion:
    """The asker a proving command installs (``api_permission.asking_hosts``): asks once per host,
    after robots.txt is read and before the proof replays the compiled request against a host that is
    not acknowledged yet. ``--yes`` (or the root ``--auto-confirm``) answers yes; without a terminal
    and without either, the answer is no, with the two ways to proceed. ``records`` is False under
    ``--dry-run`` / ``--no-persist``: a yes then holds for this command only."""

    def __init__(self, yes: bool = False, flag: str = "--yes", records: bool = True):
        self.yes, self.flag, self.records = bool(yes), flag, bool(records)
        self.answers = {}

    def __call__(self, question):
        if question.host in self.answers:
            return self.answers[question.host]
        text = _host_question_text(question)
        if self.yes:
            click.echo(f"{text} yes ({self.flag})", err=True)
            answer = (True, "")
        elif not _terminal_attached():
            answer = (False, f"{question.host} is a new host; re-run with --yes, or run "
                             f"`anyuse workflow acknowledge-host {question.host}` first")
        else:
            try:
                ok = click.confirm(text, default=True, err=True)
            except click.Abort:
                ok = False
            answer = (True, "") if ok else (False, f"you answered no for {question.host}")
        self.answers[question.host] = answer
        return answer

    def declined(self, host) -> bool:
        return host in self.answers and not self.answers[host][0]

    def acknowledged(self):
        return [host for host, (ok, _why) in self.answers.items() if ok]


def _asks_new_hosts(command):
    """Adds ``--yes/-y`` and runs ``command`` with the new-host question installed. The root
    ``anyuse -y`` (``--auto-confirm``) answers it too. Under the command's own ``--dry-run`` or
    ``--no-persist`` a yes holds for that command only and api_hosts.json is not written."""
    import functools

    @functools.wraps(command)
    def run(*args, yes=False, **kwargs):
        from anyuse.execution.api_permission import asking_hosts
        ctx = click.get_current_context(silent=True)
        root_yes = bool(ctx is not None and isinstance(ctx.obj, dict) and ctx.obj.get('auto_confirm'))
        records = not (kwargs.get('dry_run') or kwargs.get('no_persist'))
        asker = _NewHostQuestion(yes or root_yes, flag='--yes' if yes else '--auto-confirm',
                                 records=records)
        with asking_hosts(asker, record=records):
            return command(*args, **kwargs)
    return click.option('--yes', '-y', 'yes', is_flag=True,
                        help='Answer yes when AnyUse asks before the proof replays the '
                             'compiled request against a new host (for scripts)')(run)


def _echo_rungs(out):
    """The runtime cascade, one line: each rung tried, in order, and how it went."""
    tried = out.get("rungs_tried") or []
    if len(tried) < 2 and not any(t.get("rung") != "api" for t in tried):
        return
    parts = []
    for t in tried:
        label = str(t.get("rung") or "").replace("_", " ")
        if t.get("ok"):
            parts.append(f"{label} ✓" + (f" {t['ms']:.0f} ms" if isinstance(t.get("ms"), (int, float)) else ""))
        elif t.get("skipped"):
            parts.append(f"{label} skipped ({t.get('why')})")
        else:
            parts.append(f"{label} ✗ ({t.get('why')})")
    click.echo("  rungs: " + " → ".join(parts))


def _heal_line(healed):
    """What a heal did, in one sentence (execution/step_heal.py)."""
    what = ("re-learned the " + ", ".join(healed.get('relearned') or ['broken step'])
            if healed.get('rung') == 'step_repair' else "recompiled the request")
    return (f"the site changed; AnyUse {what} from {healed.get('from') or 'the result page'}, "
            f"proved it on held-out inputs ({healed.get('requests', 0)} requests, "
            f"{healed.get('model_calls', 0)} model calls, no browser) and this call used it")


def _play_next_lines(name, out, desktop):
    """What to do after a failed compiled play, as lines to print: the fix this failure calls for (a
    parameter, a host acknowledgement, a person, a browser, a site that is down), and a new recording
    only where a changed site is the likely cause."""
    import shlex
    ran = 'served_by' in out
    if out.get('action_required') == 'human':
        # a CAPTCHA, a sign-in wall or rate limiting: a person, not a recompile
        lines = [f"Next: {out.get('fallback') or 'a person has to clear this in their browser'}"]
        card = (out.get('needs_human') or {}).get('card')
        return lines + ([f"(anyuse inbox shows it as card {card})"] if card else [])
    if "no compiled workflow" in str(out.get('error') or ""):
        return ["Next: anyuse workflow list shows the tools you have."]
    if out.get('unacknowledged_hosts'):
        cmds = "; ".join(f"anyuse workflow acknowledge-host {shlex.quote(str(h))}"
                         for h in out['unacknowledged_hosts'])
        return [f"Next: {cmds}, then play it again."]
    if 'parameters' in out and not ran:
        example = " ".join(f"--param {p}=VALUE" for p in out.get('parameters') or [])
        return [f"Next: anyuse workflow play {name} {example}".rstrip()]
    if 'risk_class' in out and not ran:
        return []
    if out.get('no_browser'):
        # a browser-only tool: a browser endpoint, not a recompile
        return ["Next: point ANYUSE_CDP_ENDPOINT (or ANYUSE_LEARN_ENDPOINT) at a "
                "container browser's DevTools endpoint and play it again "
                "(docs/QUICKSTART_COMPILE.md, \"Deterministic browser replay\")."]
    if out.get('needs_browser'):
        return ["Next: if the site changed, run the same `anyuse learn` again (docs/QUICKSTART_COMPILE.md)."]
    if out.get('policy_refused'):
        # nothing was sent; the reason names the setting or the address that stopped it
        return []
    if out.get('not_found'):
        # the search answered and listed no result of that name: a new value, not a recompile
        return [f"Next: {out.get('fallback')}."]
    if out.get('transient'):
        wait = out.get('retry_after')
        return ["Next: the site did not answer (a refused connection, a timeout or a server error); "
                + (f"AnyUse sends the request again after {wait}; play it again then."
                   if wait else "play it again later.")]
    oos = out.get('out_of_service') or {}
    if oos and not ran and (not desktop or oos.get('cause') in ('outage', 'rate_limit')):
        # when AnyUse tries the request again; after a site that did not answer or asked AnyUse to
        # wait, no new recording
        return [f"Next: {out.get('fallback')}."]
    if desktop and out.get('fallback'):
        return [out['fallback']]
    if not desktop:
        return ["Next: if the site changed, record the search again and recompile (docs/QUICKSTART_COMPILE.md)."]
    return []


def _echo_har_report(har_report):
    """The HAR files read, the typed proof cases, and what was refused."""
    hars = [src for src in har_report["source"] if src.get("kind") != "typed"]
    typed = [src for src in har_report["source"] if src.get("kind") == "typed"]
    click.echo(f"Imported from HAR: {len(hars)} file(s)"
               + (f", {len(typed)} typed proof case(s)" if typed else "")
               + f", origins {', '.join(har_report['origins'][:4])}")
    for src in hars:
        click.echo(f"  {src['source']}: {src['entries']} entries, "
                   f"{src['skipped']} skipped, "
                   f"{src['credential_bound']} credential-bound (refused)"
                   + (f", {src['with_cookies']} sent with cookies (the proof sends no cookies)"
                      if src.get('with_cookies') else ""))
    for src in typed:
        click.echo(f"  typed: {src['value']} (what the page showed; no HAR)")
    for path in har_report.get("fitting") or []:
        click.echo(f"  {os.path.basename(path)}: the chain's fitting input (the search opens "
                   f"a result); not a proof case")
    generated = har_report.get("generated_fitting") or {}
    if generated.get("value"):
        click.echo(f"  {generated['value']!r}: the chain's fitting input, taken from the first "
                   f"HAR's search results (the search opens a result); not a proof case")
    for item in har_report["credential_bound_requests"][:5]:
        click.echo(f"    REFUSED {item['url'][:90]} — {item['why']}")
    for item in har_report["bodies_missing"][:3]:
        click.echo(f"    no body  {item['url'][:90]} — {item['why']}")


@workflow.command('compile')
@click.argument('name')
@click.option('--recording', '-r', 'recording_dir', default=None,
              type=click.Path(exists=True),
              help='Directory (or file) holding the network recording: '
                   '<name>.rec.json plus <name>.fresh*.json for the proof')
@click.option('--from-har', 'har_files', multiple=True,
              type=click.Path(exists=True),
              help='A HAR exported from your browser\'s DevTools. The FIRST is compiled '
                   'from; the fresh-input proof needs two more cases: more HARs, or '
                   '--expect for values you only searched (record once). A search that '
                   'opens a result needs three HARs, and a fourth when the site\'s search '
                   'does not answer another result\'s name: then the second fits it.')
@click.option('--har-param', 'har_params', multiple=True,
              help='The value you typed in the matching --from-har, as '
                   'name=value (the name may be given once)')
@click.option('--har-expect', 'har_expects', multiple=True,
              help='What the page SHOWED for the matching --from-har: the '
                   'result labels, or the values an item\'s page showed, separated '
                   'by | . A HAR records traffic, not pixels, so this is the only '
                   'possible ground truth.')
@click.option('--expect', 'typed_expects', multiple=True, metavar="VALUE='A|B|C'",
              help='A proof case without a HAR: another input and what the page showed for it '
                   '(at least three labels, in page order, separated by |). For an item\'s page, '
                   'give its values in the order the page shows them. With one --from-har, '
                   'two --expect make the proof: record once, type the rest.')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--no-persist', is_flag=True,
              help='Extract and verify, but do not write compiled_api.json')
@click.option('--transition', 'transition_spec', default='',
              help='Also add the api rung to a state graph, as "from:to:intent"')
@click.option('--no-permission-check', is_flag=True,
              help='Deprecated and refused: proof traffic always requires permission')
@click.option('--paginate', is_flag=True,
              help='Also derive how to ask for page 2 of the proved listing')
@click.option('--explain', is_flag=True,
              help='Print why this request was chosen and the others were not')
@click.option('--robots-policy', 'robots_policy_flag',
              type=click.Choice(['respect', 'ignore']), default=None,
              help="'respect' (default) never compiles a robots-disallowed path; "
                   "'ignore' is YOUR responsibility and is never implied")
@_asks_new_hosts
def workflow_compile(name, recording_dir, har_files, har_params, har_expects, typed_expects,
                     json_output, no_persist, transition_spec,
                     no_permission_check, paginate, explain, robots_policy_flag):
    """Compile a recorded workflow down to an API call, with proof.

    Deterministically extracts the request the UI rendered from, then REPLAYS it
    with parameter values that were not in the recording and compares the result
    with what the UI showed for those same values. Only a template that passes
    on >= 2 fresh values is persisted, together with the evidence. AnyUse reads
    a new host's robots.txt, then asks before the proof replays the request
    against it; --yes answers for scripts. Exits 1 when nothing was saved.

    \b
    Record once: one HAR, plus what the page showed for two other values
      anyuse workflow compile pkg_search --explain \\
        --from-har serde.har --har-param q=serde --har-expect 'serde|serde_json|serde_yaml' \\
        --expect regex='regex|regex-syntax|regex-automata' \\
        --expect tokio='tokio|tokio-util|tokio-stream'
    \b
    Or three HARs, one per value
      anyuse workflow compile pkg_search --explain \\
        --from-har serde.har --har-param q=serde --har-expect 'serde|serde_json|serde_yaml' \\
        --from-har regex.har --har-param q=regex --har-expect 'regex|regex-syntax|regex-automata' \\
        --from-har tokio.har --har-param q=tokio --har-expect 'tokio|tokio-util|tokio-stream'
    """
    import glob as _glob
    import os as _os

    from anyuse.execution.api_compile import (
        compiled_record, load_compiled, persist_compiled, add_api_implementation)
    from anyuse.execution.api_verify import compile_and_verify, permission_preflight
    if no_permission_check:
        raise click.UsageError("--no-permission-check is no longer permitted: origin, "
                               "robots and host approval are checked before proof traffic")
    from anyuse.memory.storage import WorkflowStorage

    har_report = None
    if har_files:
        # --- the HAR path -------------------------------------------------
        # A user exports a HAR from DevTools; no AnyUse recorder is involved.
        # The proof is the SAME one, on inputs that were not compiled from.
        from anyuse.execution.har_import import HarCase, HarError, compile_from_har
        if recording_dir:
            click.echo("Use either --recording or --from-har, not both.")
            raise SystemExit(2)
        if len(har_params) != len(har_files) or len(har_expects) != len(har_files):
            click.echo(f"--from-har was given {len(har_files)} time(s) but "
                       f"--har-param {len(har_params)} and --har-expect "
                       f"{len(har_expects)}. They are matched by position, so "
                       f"there must be exactly one of each per HAR.")
            raise SystemExit(2)
        param_name = 'q'
        cases = []
        for path, raw_param, raw_expect in zip(har_files, har_params, har_expects):
            if '=' in raw_param:
                param_name, value = raw_param.split('=', 1)
            else:
                value = raw_param
            expected = [x.strip() for x in raw_expect.split('|') if x.strip()]
            cases.append(HarCase(path=path, param_value=value, expected=expected))
        for raw in typed_expects:
            value, sep, labels = raw.partition('=')
            if not sep or not value.strip():
                click.echo(f"--expect takes VALUE='label|label|label', got {raw!r}")
                raise SystemExit(2)
            cases.append(HarCase(path='', param_value=value.strip(),
                                 expected=[x.strip() for x in labels.split('|') if x.strip()]))
        kept = {}
        try:
            compiled, proof, har_report = compile_from_har(
                cases, param_name=param_name, capture_ua_for=_browser_identity_predicate(),
                keep=kept)
        except HarError as exc:
            click.echo(f"HAR import refused: {exc}")
            raise SystemExit(2)
        rec_files = [cases[0].path]
        fit_files = list(har_report.get("fitting") or [])
        fresh_files = [c.path for c in cases[1:] if not (c.path and c.path in fit_files)]
        recording = {"site": har_report["origins"][0] if har_report["origins"] else ""}
        return _finish_compile(
            name, compiled, proof, recording, rec_files, fit_files, fresh_files,
            json_output, no_persist, transition_spec, no_permission_check,
            paginate, explain, robots_policy_flag, har_report, evidence=kept)

    if typed_expects:
        click.echo("--expect adds proof cases to a HAR compile: give the one recording with --from-har FILE.")
        raise SystemExit(2)
    if not recording_dir:
        click.echo("Give either --recording DIR or --from-har FILE.")
        raise SystemExit(2)

    if _os.path.isdir(recording_dir):
        rec_files = sorted(_glob.glob(_os.path.join(recording_dir, f"{name}.rec.json")))
        fit_files = sorted(_glob.glob(_os.path.join(recording_dir, f"{name}.fit*.json")))
        sel_files = sorted(_glob.glob(_os.path.join(recording_dir, f"{name}.sel*.json")))
        fresh_files = sorted(_glob.glob(_os.path.join(recording_dir, f"{name}.fresh*.json")))
    else:
        rec_files, fit_files, sel_files, fresh_files = [recording_dir], [], [], []
    if not rec_files:
        click.echo(f"No recording '{name}.rec.json' found in {recording_dir}")
        raise SystemExit(2)

    with open(rec_files[0]) as fh:
        recording = json.load(fh)
    fresh = []
    for path in fresh_files:
        with open(path) as fh:
            fresh.append(json.load(fh))
    fitting = []
    for path in fit_files:
        with open(path) as fh:
            fitting.append(json.load(fh))
    selection = []
    for path in sel_files:
        with open(path) as fh:
            selection.append(json.load(fh))

    from anyuse.execution.api_permission import check_permission
    def proof_preflight(compiled):
        return permission_preflight(
            compiled, recording,
            checker=lambda url, hosts_path=None: check_permission(
                url, hosts_path=hosts_path, policy=robots_policy_flag))
    # roles by file name: .rec (compile), .fit* (fitting), .sel* (selection among
    # runner-ups), .fresh* (validation, untouched until the final check) —
    # execution/proof_split.py settles the status from them
    if fitting:
        compiled, proof = compile_and_verify(recording, fresh, preflight=proof_preflight,
                                             fitting_recordings=fitting,
                                             selection_recordings=selection,
                                             model_scope="compile")
    else:
        # a chain with no NAME.fit1.json is fitted on another result's name from the
        # recording's own search (execution/chain_fit.py); anything else compiles as before
        from anyuse.execution.api_verify import compile_and_verify_with_fit
        compiled, proof, _fit = compile_and_verify_with_fit(
            recording, fresh, [], preflight=proof_preflight, selection_recordings=selection,
            model_scope="compile")
        if compiled.chain_needs_fitting:
            hint = ((f". A value taken from the recording's own search results did not fit "
                     f"it ({compiled.chain_fit_note})" if compiled.chain_fit_note else "")
                    + f". Fit it with {name}.fit1.json: a recording of another input that "
                      f"opens a result the same way")
            compiled.reason += hint
            proof.reason += hint
    return _finish_compile(
        name, compiled, proof, recording, rec_files, fit_files, fresh_files,
        json_output, no_persist, transition_spec, no_permission_check,
        paginate, explain, robots_policy_flag, None, sel_files=sel_files,
        evidence={"recording": recording, "fitting": fitting, "selection": selection,
                  "fresh": fresh})


def _finish_compile(name, compiled, proof, recording, rec_files, fit_files, fresh_files,
                    json_output, no_persist, transition_spec, no_permission_check,
                    paginate, explain, robots_policy_flag, har_report,
                    attach_report=None, confirm=None, sel_files=None, evidence=None):
    """The half of `workflow compile` that does not care where the recording
    came from: pagination, the courtesy gate, persistence and the report.
    ``evidence``: the recordings by role (``recording``, ``fitting``, ``selection``,
    ``fresh``), kept in the tool's folder for an automatic recompile
    (execution/evidence_store.py).

    Shared by the recorder path and the `--from-har` path on purpose. The
    proof, the permission gate and the persistence rule are the same for
    both — a HAR is a different way to obtain a recording, not a different
    standard of evidence.
    """
    import os as _os

    from anyuse.execution.api_compile import (
        compiled_record, persist_compiled, add_api_implementation)
    from anyuse.memory.storage import WorkflowStorage

    # the user's own --no-persist; the permission gate and the confirmation may set it later
    asked_no_persist = bool(no_persist)
    report = {
        "workflow": name,
        "extraction": compiled.to_dict(),
        "equivalence": proof.to_dict(),
        # Which on-screen labels were treated as page chrome and therefore NOT
        # used as ground truth. Listed rather than merely counted: the filter
        # decides what "the result list" means, so a human has to be able to
        # check that it did not eat the answer.
        "chrome_filtered": sorted(compiled.chrome_dropped),
        "har": har_report,
        "persisted": None,
    }
    if attach_report is not None:
        report["attach"] = attach_report

    # Page 2 of a proved listing. Derived from evidence the first response
    # carries, reported as a CANDIDATE: like every rung it is only real once it
    # has been proved on a page the compiler never saw.
    if paginate and compiled.template is not None and getattr(compiled.template, "shape", "") == "list":
        from anyuse.execution.pagination import compile_next_page
        first_body = None
        for req in (recording.get("network") or []):
            if req.get("body") and "json" in (req.get("mime") or "").lower():
                try:
                    first_body = json.loads(req["body"])
                except ValueError:
                    continue
                if compiled.template.url.split("?")[0] in req.get("url", ""):
                    break
        nxt, pag, why = compile_next_page(compiled.template, first_body,
                                          (recording.get("network") or [{}])[0].get("url", ""))
        report["pagination"] = ({"derived": pag.to_dict(), "describe": pag.describe(),
                                 "template": nxt.to_dict(), "proved": False}
                                if pag is not None else {"derived": None, "reason": why})

    # Courtesy gate (robots.txt + a per-host acknowledgement). It never blocks
    # compiling or reporting — only whether the rung becomes the PREFERRED way
    # to run the workflow. See execution/api_permission.py.
    permission = None
    if compiled.template is not None and not no_permission_check:
        from anyuse.execution.api_permission import check_permission
        steps = getattr(compiled.template, "steps", None)
        urls = ([step.template.url for step in steps] if steps
                else [compiled.template.url])
        permissions = [check_permission(url, policy=robots_policy_flag) for url in urls]
        permission = permissions[0]
        report["permission"] = {"all_allowed": all(p.may_prefer for p in permissions),
                                "steps": [p.to_dict() for p in permissions]}
        if not all(p.may_prefer for p in permissions):
            no_persist = True
    from anyuse.execution.api_permission import host_asker
    asker = host_asker()
    if asker is not None and getattr(asker, "acknowledged", None) and asker.acknowledged():
        # under --no-persist the yes was not written: it held for this command only
        key = ("acknowledged_at_compile" if getattr(asker, "records", True)
               else "acknowledged_for_this_command")
        report[key] = asker.acknowledged()

    if (compiled.template is not None and proof.passed and not no_persist
            and confirm is not None):
        # A person confirms what was proved before anything is persisted.
        ok, why = confirm(compiled, proof)
        report["confirmation"] = {"confirmed": bool(ok), "how": why}
        if not ok:
            no_persist = True
    if compiled.template is not None and proof.passed and not no_persist:
        storage = WorkflowStorage()
        wf = storage.get(name)
        workflow_id = wf.id if wf else name
        record = compiled_record(
            compiled.template, proof.to_dict(), workflow=name,
            chrome_filtered=list(compiled.chrome_dropped),
            permission=report.get("permission"),
            source={"recording": _os.path.basename(rec_files[0]),
                    "fitting": [_os.path.basename(p) for p in fit_files],
                    "selection": [_os.path.basename(p) for p in (sel_files or [])],
                    "fresh": [_os.path.basename(p) for p in fresh_files],
                    # where they are, so an automatic recompile can read them again
                    # (execution/auto_recompile.py); a HAR has no recording to re-read
                    **({} if har_report else {"paths": {
                        "recording": _os.path.abspath(rec_files[0]),
                        "fitting": [_os.path.abspath(p) for p in fit_files],
                        "selection": [_os.path.abspath(p) for p in (sel_files or [])],
                        "fresh": [_os.path.abspath(p) for p in fresh_files]}}),
                    "site": recording.get("site", ""),
                    **({"origin": "attach",
                        "ground_truth": (attach_report.get("ground_truth_source")
                                         or "captured result-page text (docs/ATTACH.md)")}
                       if attach_report is not None else {})})
        # proof.to_dict() already carries status, split and scope
        # (execution/proof_split.py); every compile path persists the same way.
        from anyuse.execution.api_compile import with_runtime_rungs
        with_runtime_rungs(record, recording, origin=("attach" if attach_report is not None
                                                      else "har" if har_report else "recording"))
        if evidence:
            from anyuse.execution.evidence_store import keep_compile_evidence
            keep_compile_evidence(storage, workflow_id, record, **evidence)
        report["persisted"] = persist_compiled(storage, workflow_id, record)
        if transition_spec:
            frm, to, intent = (transition_spec.split(":", 2) + ["", ""])[:3]
            guide_name = recording.get("site", "")
            from anyuse.guides.storage import GuideStorage
            guide = GuideStorage().get(guide_name) if guide_name else None
            if guide is None:
                report["transition"] = f"no app guide named {guide_name!r}; api rung not added to a graph"
            else:
                graph = guide.get_state_graph()
                add_api_implementation(graph, frm, to, intent, compiled.template)
                guide.set_state_graph(graph)
                GuideStorage().save(guide)
                report["transition"] = f"api implementation added to {guide_name}: {frm}->{to} ({intent})"

    if json_output:
        click.echo(json.dumps(report, indent=2, default=str))
        _compile_exit(report, compiled, proof, asked_no_persist)
        return

    click.echo(f"Workflow: {name}   (recording: {_os.path.basename(rec_files[0])})")
    if har_report:
        _echo_har_report(har_report)
    click.echo(f"Extraction: {compiled.status} — {compiled.reason or 'winning request explains the screen'}")
    if compiled.template is not None:
        # a chain (RequestPlan) prints each of its requests; what it returns is its last one's
        steps = [step.template for step in getattr(compiled.template, "steps", None) or []]
        for request in steps or [compiled.template]:
            q = "&".join(f"{k}={v}" for k, v in request.query.items())
            click.echo(f"  {request.method} {request.url}{('?' + q) if q else ''}")
        t = steps[-1] if steps else compiled.template
        kind = getattr(t, "extract_kind", "json")
        if kind == "embedded_json":
            from anyuse.execution.embedded_json import describe_locator
            click.echo(f"  rung:    embedded JSON — {describe_locator(t.embedded)}")
        elif kind == "html":
            click.echo("  rung:    HTML selector (bound to markup — the most brittle rung)")
        click.echo(f"  extract: {t.extract}")
        click.echo(f"  returns: {_describe_returns(t)}")
        if explain:
            click.echo(f"  expect:  {t.expect}")
    if compiled.chrome_dropped:
        labels = sorted(compiled.chrome_dropped)
        click.echo(f"Page chrome filtered out of the ground truth "
                   f"({len(labels)} label(s) that appear for EVERY recorded query):")
        for label in labels[:12]:
            click.echo(f"    - {label}")
        if len(labels) > 12:
            click.echo(f"    … and {len(labels) - 12} more (use --json for all)")
        click.echo("  If one of those is actually a result, the ground truth is "
                   "wrong and so is anything proved against it.")
    if compiled.typed_decision_would_help:
        click.echo("  note: candidates were equally plausible — a typed decision layer "
                   "would choose here (not wired); verification decided instead.")
    if explain:
        click.echo("Why this request:")
        chosen = getattr(compiled.template, "url", "") if compiled.template else ""
        for c in compiled.candidates[:6]:
            s = c.summary()
            # host AND path: a site_metadata call on the same host is not the
            # request that was chosen, and marking it as such would make the
            # explanation lie about the very thing it exists to explain.
            mark = "->" if chosen and chosen.split("?")[0].endswith(
                s["host"] + s["path"]) else "  "
            click.echo(f"  {mark} {s['host']}{s['path']}  coverage={s['coverage']:.2f} "
                       f"bytes={s['bytes']} path={s['best_path'] or '-'}"
                       + (f"  REFUSED: {s['ineligible']}" if s["ineligible"] else ""))
        if not compiled.candidates:
            click.echo("  (no JSON response was eligible; a document rung was tried instead)")
    if "pagination" in report:
        pg = report["pagination"]
        if pg.get("derived"):
            click.echo(f"Pagination: {pg['describe']} — CANDIDATE, not proved "
                       "(supply page-2 recordings to prove it)")
        else:
            click.echo(f"Pagination: not derived — {pg['reason']}")
    click.echo(f"Fresh-input proof: {'PASSED' if proof.passed else 'NOT PASSED'} "
               f"({proof.passed_count}/{len(proof.cases)} cases) — {proof.reason}")
    click.echo(f"Status: {proof.effective_status}")
    if getattr(proof, "chain_fit", None):
        fit = proof.chain_fit
        how = "the browser replay" if fit.get("via") == "browser" else "plain HTTP"
        click.echo(f"  chain fitted on {fit.get('value')!r}: a name from the first recording's "
                   f"search results, sent to the site over {how}; not a proof input")
    if proof.cases:
        click.echo(_describe_proof(compiled, proof))
    if explain and proof.scope:
        click.echo(f"  scope: {proof.scope}")
    if explain and proof.split:
        sp = proof.split
        extra = "".join(f", +{sp[k]} {k}" for k in ("fitting", "selection") if sp.get(k))
        verb = "proved on" if proof.passed else ("checked on" if proof.cases else "to be proved on")
        click.echo(f"  built from {sp.get('compile', 1)} recording{extra}; {verb} "
                   f"{sp.get('validation', 0)} other value(s)"
                   + ("" if sp.get('held_out') else
                      " (NOT held out: those values also helped choose the request)")
                   + (f" ({sp['why']})" if sp.get("why") else ""))
    for case in proof.cases:
        mark = "ok  " if case.passed else "FAIL"
        click.echo(f"  [{mark}] {case.value!r}: coverage={case.coverage:.2f} "
                   f"order_ok={case.order_ok} ui={case.ui_count} api={case.api_count} "
                   f"{case.reason}")
    if permission is not None:
        # the new-host answer (no, or no terminal) is the proof's own reason line
        declined = bool(asker is not None and getattr(asker, "declined", None)
                        and asker.declined(permission.host))
        already_said = (not proof.cases and not permission.may_prefer and permission.reason
                        and (permission.reason in (proof.reason or "") or declined))
        click.echo(f"Permission: {'may prefer' if permission.may_prefer else 'NOT preferred'} — "
                   + ("see the reason above" if already_said else f"{permission.reason}"))
        click.echo(f"  robots policy: {permission.policy}")
        if permission.robots_overridden:
            click.echo(f"  !! ROBOTS DISALLOW OVERRIDDEN — {permission.robots_overridden}")
    if report["persisted"]:
        click.echo(f"✓ Persisted as the preferred implementation: {report['persisted']}")
    elif compiled.template is not None and not proof.passed and not proof.cases:
        # No proof request was sent (e.g. the host is not acknowledged yet):
        # saying "did not reproduce the UI" here would report a failure that
        # never happened.
        click.echo("✗ Not persisted: the fresh-input proof was not run (reason above).")
    elif compiled.template is not None and not proof.passed:
        click.echo("✗ Not persisted: the template did not reproduce the UI on fresh input.")
        if har_report and har_report.get("needs_sign_in"):
            click.echo(f"  {har_report['needs_sign_in']}")
    elif report.get("confirmation") and not report["confirmation"]["confirmed"]:
        click.echo(f"✗ Not persisted: not confirmed ({report['confirmation']['how']}).")
    elif no_persist:
        click.echo("(--no-persist: nothing written)")
    _compile_exit(report, compiled, proof, asked_no_persist)


def _compiled_recording_summary(storage, wf):
    """One line for a from-draft compiled recording (steps, parameters, how to replay), or ''."""
    try:
        from anyuse.execution import draft_compile as DC
        path = os.path.join(str(storage.get_workflow_dir(wf.id)), DC.RECORD_FILE)
        if not os.path.isfile(path):
            return ""
        with open(path) as fh:
            rec = json.load(fh)
    except Exception:  # noqa: BLE001 - an unreadable record lists like any other workflow
        return ""
    steps = len(rec.get("steps") or [])
    names = [str(p.get("name")) for p in (rec.get("parameters") or []) if isinstance(p, dict) and p.get("name")]
    args = " ".join(f"--param {n}=VALUE" for n in names)
    status = str(rec.get("status") or "draft")
    head = f"{status}, {steps} step(s), {DC.approval_level(rec)} approvals"
    if status != "ready":
        why = next((str(r) for r in rec.get("status_reasons") or []), "")
        return f"{head}; not replayable yet" + (f": {why[:160]}" if why else "")
    return f"{head}; replay: ANYUSE_DRIVE_ENABLED=on anyuse workflow play {wf.name} {args}".rstrip()


def _is_compiled_recording(name):
    """A desktop recording compiled by `workflow from-draft` (it replays with --param values)."""
    try:
        from anyuse.execution import draft_compile as DC
        from anyuse.memory.storage import WorkflowStorage
        storage = WorkflowStorage()
        return DC.play_refusal(storage, storage.get(name)) is not None
    except Exception:  # noqa: BLE001 - an unreadable store holds no compiled recording
        return False


def _has_desktop_recording(name):
    """A recorded desktop path exists for this workflow (a web tool alone has none)."""
    try:
        from anyuse.memory.storage import WorkflowStorage
        return WorkflowStorage().get(name) is not None
    except Exception:  # noqa: BLE001 - an unreadable store has no recording to fall back to
        return False


def _web_tool_parameters(name):
    """The parameter names of a compiled web tool called NAME, or None if there is no such tool."""
    try:
        from anyuse.mcpserver.compiled_tools import list_compiled
        for tool in list_compiled():
            if tool.get("workflow") == name:
                return list(tool.get("parameters") or [])
    except Exception:  # noqa: BLE001
        return None
    return None


def _compile_exit(report, compiled, proof, asked_no_persist):
    """Exit 1 when nothing was saved, unless the user asked not to save and the proof passed."""
    if report.get("persisted"):
        return
    if asked_no_persist and compiled.template is not None and proof.passed:
        return
    raise SystemExit(1)


def _describe_returns(template):
    """What a compiled tool returns, in one plain line (the full contract is in --json)."""
    extract = template.extract if isinstance(template.extract, dict) else {"result": template.extract}
    paths = ", ".join(f"{k} from {v}" for k, v in extract.items())
    if getattr(template, "shape", "list") == "list":
        least = (template.expect or {}).get("min_items")
        return f"a list ({paths})" + (f", at least {least} item(s) per call" if least else "")
    return f"named fields ({paths})"


def _describe_proof(compiled, proof):
    """The proof's claim in one plain sentence (the exact scope text is in --explain and --json)."""
    count = len(proof.cases)
    held = (proof.split or {}).get("held_out", True)
    others = "values it was not built from" if held else "values that also helped choose the request"
    shape = getattr(compiled.template, "shape", "list") if compiled.template is not None else "list"
    if shape == "list":
        claim = (f"for each of {count} {others}, at least 80% of the first 10 labels the page "
                 f"showed came back in the same order")
    else:
        claim = f"for each of {count} {others}, the named fields matched what the page showed"
    return f"What was checked: {claim}. It does not promise every input behaves the same way."


# ─────────────────────────────────────────────────────────
# Draft -> workflow
# ─────────────────────────────────────────────────────────

@workflow.command('drafts')
@click.option('--inbox', default='', help='Draft files from this directory (default ~/.anyuse/drafts)')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def workflow_drafts(inbox, json_output):
    """List the recorded drafts `from-draft` can compile: watcher sessions and the inbox."""
    from anyuse.execution import draft_compile as DC
    rows = DC.list_drafts(inbox=inbox or None)
    if json_output:
        click.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        click.echo("No drafts (watcher sessions with a workflow, or files in "
                   f"{inbox or DC.drafts_inbox()}).")
        return
    for r in rows:
        if r.get("error"):
            click.echo(f"  {r['id']}: unreadable ({r['error']})")
            continue
        flag = "" if r.get("complete") else "  (partial)"
        click.echo(f"  {r['id']}  [{r['source']}]  {r.get('steps', 0)} step(s)  "
                   f"{', '.join(r.get('apps') or [])}{flag}")


def _draft_overrides(params):
    from anyuse.execution.draft_params import parse_override
    out = []
    for spec in params:
        try:
            name, ptype, ref = parse_override(spec)
        except ValueError as exc:
            raise click.UsageError(str(exc))
        out.append({"name": name, "type": ptype, "ref": ref})
    return out


@workflow.command('from-draft')
@click.argument('draft')
@click.option('--param', 'params', multiple=True, metavar='NAME[:TYPE]=CANDIDATE',
              help='Make a candidate a parameter (by id, e.g. @c2, or by its recorded value). '
                   'TYPE: text, number, date, file_name, choice. Wins over --propose.')
@click.option('--fixed', 'fixed', multiple=True, metavar='CANDIDATE',
              help='Keep a candidate fixed even if the model proposes it')
@click.option('--propose', is_flag=True,
              help='Ask the parameter model once (the candidate values go to the configured '
                   'provider; the price is printed by a run without this flag)')
@click.option('--fresh-json', default='',
              help='The proof\'s fresh value sets as JSON, e.g. \'[{"text": "a"}, {"text": "b"}]\' '
                   '(default: generated values that were not recorded)')
@click.option('--name', default='', help='Save under this workflow name')
@click.option('--web-name', default='', help='Which NAME.rec.json in a recording directory')
@click.option('--dry-run', is_flag=True, help='Compile and prove; write nothing')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@_asks_new_hosts
def workflow_from_draft(draft, params, fixed, propose, fresh_json, name, web_name, dry_run,
                        json_output):
    """Turn a recorded draft into a saved, parameterized workflow.

    DRAFT is SESSION_ID/WORKFLOW (see `anyuse workflow drafts`), a draft file, the guest
    drive harness's workflow.json, or a web recording directory. The workflow is saved as
    `ready` only after it replayed correctly with values that were not in the recording;
    otherwise as `draft`, with the reasons. Nothing here sends input.
    """
    from anyuse.execution import draft_compile as DC
    from anyuse.execution import draft_params as DP
    fresh = None
    if fresh_json:
        try:
            fresh = json.loads(fresh_json)
            assert isinstance(fresh, list) and all(isinstance(f, dict) for f in fresh)
        except (ValueError, AssertionError):
            raise click.UsageError("--fresh-json must be a JSON list of {name: value} objects")
    try:
        report = DC.run_sync(DC.draft_to_workflow(
            draft, overrides=_draft_overrides(params), fixed=list(fixed), propose=propose,
            fresh=fresh, name=name, save=not dry_run, web_name=web_name))
    except DC.DraftError as exc:
        click.echo(f"✗ {exc}")
        sys.exit(2)
    if json_output:
        click.echo(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        sys.exit(0 if report["status"] == "ready" else 1)
    click.echo(f"Draft: {draft}  →  workflow '{report['workflow']}'")
    att = report["attribution"]
    click.echo(f"  taught by {att.get('taught_by')} at {att.get('taught_at') or '?'}"
               f" ({att.get('draft_kind')}, digest {str(att.get('draft_digest'))[:12]})")
    if report["candidates"]:
        click.echo("Candidates:")
        for c in report["candidates"]:
            val = c["value"] if len(c["value"]) <= 60 else c["value"][:57] + "..."
            click.echo(f"  {c['id']:4} {c['source']:7} {val!r}  (steps {c['steps']})")
    for u in report["unreplayable"]:
        click.echo(f"  ! step {u['draft_index']}: {u['reason']}")
    for s in report.get("shortcuts") or []:
        click.echo(f"  ~ step {s['draft_index']}: {s['note']}")
    choice = report["parameter_choice"]
    prop = choice.get("proposal") or {}
    if prop.get("model"):
        m = prop["model"]
        click.echo(f"Model: {m.get('provider')}/{m.get('model')}, {m.get('calls', 0)} call(s), "
                   f"at most US${(prop.get('estimate') or {}).get('max_usd', 0):.4f}")
    if report["parameters"]:
        click.echo("Parameters:")
        for p in report["parameters"]:
            click.echo(f"  {p['name']} ({p['type']}) = {p['candidate'] or '-'} [{p['chosen_by']}]")
    elif report["candidates"] and not propose and not params:
        try:
            target = DP.model_target()
            click.echo(f"No parameters chosen. Choose with --param NAME=@cN, or ask "
                       f"{target['provider']}/{target['model']} once with --propose "
                       f"(US${target['price_in']}/{target['price_out']} per million tokens in/out; "
                       "the candidate values above are sent).")
        except Exception as exc:  # noqa: BLE001
            click.echo(f"No parameters chosen (the parameter model is not configured: {exc}).")
    proof = report["proof"]
    kind = "Replay proof" if proof.get("fixed_routine") else "Fresh-input proof"
    click.echo(f"{kind} ({proof.get('harness')}): "
               f"{'PASSED' if proof.get('passed') else 'NOT PASSED'} — {proof.get('reason')}")
    for i, case in enumerate(proof.get("cases") or []):
        mark = "ok  " if case.get("passed") else "FAIL"
        click.echo(f"  [{mark}] {json.dumps(case.get('values'), ensure_ascii=False)[:120]}  "
                   f"holds={case.get('holds')} drift={case.get('drift_fired')}")
    from anyuse.execution import draft_compile as _DC
    _gated = len(report['approvals']['gated_steps'])
    click.echo(f"Replay approvals: level {_DC.APPROVAL_DEFAULT} (starting a replay with its values is the "
               f"approval; {_gated} step(s) would ask a person after `anyuse workflow approvals NAME --level all`); "
               f"drift baseline {report['drift_profile'].get('baseline_steps', '-')}/"
               f"{report['drift_profile'].get('steps', '-')}")
    for r in report["rungs"]:
        click.echo(f"  rung {r['rung']}: {r['status']}" + (f" — {r['reason']}" if r.get("reason") else ""))
    v = report.get("verdict") or {}
    if v.get("source") == "verdict store":
        click.echo(f"Verdict store: {v.get('reason') or v.get('verdict')}")
    click.echo(f"Status: {report['status'].upper()}")
    for r in report["reasons"]:
        click.echo(f"  - {r}")
    if report.get("saved"):
        click.echo(f"✓ Saved: {report['saved']['record']}")
    elif dry_run:
        click.echo("(--dry-run: nothing written)")
    if report["status"] == "needs_fill":
        click.echo(f"Next: `anyuse workflow fill {report['workflow']} --param NAME=VALUE ...` in the "
                   "machine that drives the app (the AI agent completes the gaps once, then it is proved)")
    sys.exit(0 if report["status"] == "ready" else 1)


def _pid_tree(app):
    """The AX tree of the ONE running process named ``app`` (nothing is launched). A
    ``draft_compile.BundleId`` (a multi-app replay names its apps by bundle id) is looked up among
    the running apps instead (``pgrep -x`` misses a localized name); any other name - "zoom.us"
    included - goes to ``pgrep -x`` exactly as before."""
    import subprocess
    from anyuse.execution.draft_compile import BundleId
    if isinstance(app, BundleId):
        from anyuse.context.accessibility import AccessibilityTree
        pid = AccessibilityTree.running_pid(app)
        return AccessibilityTree(pid=pid) if pid is not None else None
    try:
        out = subprocess.run(["/usr/bin/pgrep", "-x", app], capture_output=True, text=True,
                             timeout=10).stdout.split()
    except Exception:  # noqa: BLE001
        return None
    if len(out) != 1:
        return None
    from anyuse.context.accessibility import AccessibilityTree
    return AccessibilityTree(pid=int(out[0]))


APPROVAL_VALUE_INLINE = 400


def _approval_value(value):
    """(what the approval line shows, the full text to print above it or None). Up to
    APPROVAL_VALUE_INLINE characters the value is shown whole; a longer one is printed whole
    first and the line says it is cut, where, and how long the value is."""
    import hashlib
    v = str(value or "")
    if len(v) <= APPROVAL_VALUE_INLINE:
        return v, None
    h = hashlib.sha256(v.encode()).hexdigest()[:12]
    return (f"{v[:300]}… [CUT at 300 of {len(v)} characters; the whole value is printed above; "
            f"sha256 {h}]"), v


def _tty_step_approver(name):
    """Each held step is shown on the terminal and needs the typed-back code (human_gate.TtyGate)."""
    import hashlib
    from anyuse.execution.human_gate import TtyGate
    gate = TtyGate()

    async def approve(ctx):
        args = ctx.get("args") or {}
        shown, full = _approval_value(args.get("input_data"))
        if full is not None:
            click.echo(f"\nThe value this step types ({len(full)} characters, in full):\n{full}\n")
        # TtyGate prints a fixed set of keys: the target and any key code ride in "step"/"action"
        summary = {"purpose": "approve this step", "manifest": name,
                   "step": (f"{str(args.get('intent') or '')[:160]} | target: "
                            f"{str(args.get('target') or '')[:120]}"),
                   "action": " ".join(str(x) for x in (ctx.get("action"), args.get("keycode") or "") if x),
                   "effects": ctx.get("step_class"),
                   "bound_inputs": shown}
        # the typed-back code binds the WHOLE value, not only what the line shows
        digest = hashlib.sha256((json.dumps(summary, sort_keys=True)
                                 + str(args.get("input_data") or "")).encode()).hexdigest()
        why = gate(summary, digest)
        if why:
            click.echo(why)
        return why is None
    return approve


@workflow.command('verdict')
@click.argument('name')
@click.option('--verdict', 'verdict', required=True,
              type=click.Choice(['wrong', 'correct', 'incomplete', 'undeterminable']),
              help='Your audit of the compile (or, with --param, of one replay)')
@click.option('--why', default='', help='What was wrong (kept with the verdict)')
@click.option('--ref', default='', help='Where the evidence is (a run, a ticket)')
@click.option('--param', 'params', multiple=True, metavar='NAME=VALUE',
              help='The values of the replay this verdict is about (none: the compile)')
def workflow_verdict(name, verdict, why, ref, params):
    """Record an audit verdict on a draft-compiled workflow or on one of its replays.

    A compile judged wrong stays a draft when it is compiled again; a replay judged wrong (or
    any replay of a compile judged wrong) is refused - never repeated silently.
    """
    from anyuse.execution import draft_compile as DC
    from anyuse.execution import draft_verdicts as DV
    wf, rec = DC.find_saved(name)
    if rec is None:
        click.echo(f"✗ no draft-compiled workflow named {name!r}")
        sys.exit(2)
    values = {}
    for item in params:
        if '=' not in item:
            raise click.UsageError(f"--param expects NAME=VALUE, got {item!r}")
        k, v = item.split('=', 1)
        values[k.strip()] = v
    problem = DV.values_problem(rec, values) if values else ""
    if problem:
        raise click.UsageError(problem)
    row = DV.record(rec, verdict, source="audit", why=why, ref=ref, values=values or None)
    scope = "this replay" if values else "the compile"
    click.echo(f"✓ {verdict} recorded on {scope} of {name} ({DV.store_path()})" if row
               else f"(already recorded: {verdict} on {scope} of {name})")


@workflow.command('replay')
@click.argument('name')
@click.option('--param', 'params', multiple=True, metavar='NAME=VALUE',
              help='A value for each parameter of the saved workflow')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def workflow_replay(name, params, json_output):
    """Replay a workflow saved by `from-draft` with new values.

    Refuses before anything is built unless ANYUSE_DRIVE_ENABLED is on and the foreground
    interlock allows input (so it refuses on a desktop someone is using). Every held step
    asks a person at this terminal; the target app must already be running.
    """
    from anyuse.execution import draft_compile as DC
    values = {}
    for item in params:
        if '=' not in item:
            raise click.UsageError(f"--param expects NAME=VALUE, got {item!r}")
        k, v = item.split('=', 1)
        values[k.strip()] = v
    out = DC.run_sync(DC.replay_saved(name, values, approver=_tty_step_approver(name),
                                      tree_factory=_pid_tree, supervised=True))
    if json_output:
        if not out.get("ok"):
            out = {**out, "reason": out.get("reason") or out.get("stopped") or "the replay stopped"}
        click.echo(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    else:
        for s in out.get("steps") or []:
            click.echo(f"  [{'ok  ' if s['success'] else 'FAIL'}] {s['intent']}"
                       + (f" — {s['error']}" if s.get("error") else ""))
        click.echo(f"✓ {name}: replayed" if out.get("ok") else f"✗ {out.get('stopped')}")
    sys.exit(0 if out.get("ok") else 1)


@workflow.command('fill')
@click.argument('name')
@click.option('--param', 'params', multiple=True, metavar='NAME=VALUE',
              help='A value for each parameter of the saved workflow')
@click.option('--show-values', is_flag=True, default=None,
              help='The model also sees the fields\' values (never a password field\'s)')
@click.option('--free-text', is_flag=True, default=None,
              help='The model may type its own text (saved in the step, typed on every later run)')
@click.option('--sensitive', is_flag=True, default=None,
              help='Fill password, payment and terminal steps; passwords come from the credential vault')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def workflow_fill(name, params, show_values, free_text, sensitive, json_output):
    """Fill the gaps of a `needs_fill` workflow once with the AI agent, then prove it.

    A live run like `workflow replay` (the drive gate and the foreground interlock first; the
    target app must be running). At each recorded step the engine cannot repeat, the model
    picks one listed element per action; every step, the model's included, asks a person at
    this terminal. Afterwards the workflow replays without a model.
    """
    from anyuse.execution import draft_compile as DC
    from anyuse.execution import draft_fill as DF
    values = {}
    for item in params:
        if '=' not in item:
            raise click.UsageError(f"--param expects NAME=VALUE, got {item!r}")
        k, v = item.split('=', 1)
        values[k.strip()] = v
    # a flag turns its switch on for this run; without it the setting decides
    out = DC.run_sync(DF.fill_saved(name, values, approver=_tty_step_approver(name),
                                    tree_factory=_pid_tree, show_values=show_values or None,
                                    free_text=free_text or None, sensitive=sensitive or None))
    if json_output:
        click.echo(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    else:
        for s in out.get("steps") or []:
            mark = " (AI)" if s.get("filled_gap") is not None else ""
            click.echo(f"  [{'ok  ' if s['success'] else 'FAIL'}]{mark} {s['intent']}"
                       + (f" — {s['error']}" if s.get("error") else ""))
        m = out.get("model") or {}
        if m:
            click.echo(f"  model calls: {m.get('calls', 0)}, US${m.get('usd', 0):.4f}")
        if out.get("ok"):
            click.echo(f"✓ {name}: filled, saved as {out.get('status')}"
                       + ("" if out.get("status") == "ready" else f" ({'; '.join(out.get('reasons') or [])})"))
        else:
            click.echo(f"✗ {out.get('stopped')}")
    sys.exit(0 if out.get("ok") else 1)


@workflow.command('from-map')
@click.argument('app')
@click.argument('task')
@click.option('--map', 'map_files', multiple=True, metavar='FILE',
              help='An app map file (merged in order); default: the built-in probed effect map and '
                   'discovery results for APP')
@click.option('--routine', 'routines', multiple=True, metavar='FILE',
              help='A recorded success (a run record or a compiled list) to add to the map')
@click.option('--workflow-record', 'records', multiple=True, metavar='FILE',
              help='A saved draft_workflow.json (a proven workflow) to add to the map')
@click.option('--param', 'params', multiple=True, metavar='NAME=VALUE',
              help='A value for a parameter the task text does not name (a content reference)')
@click.option('--model', 'use_model', is_flag=True,
              help='The configured planner model proposes the plan from the map\'s transitions; the plan '
                   'is validated against the map and never invents an identity')
@click.option('--name', default='', help='Save under this workflow name')
@click.option('--no-review', is_flag=True,
              help='Skip the independent reviewer (ANYUSE_MAP_REVIEWER): the workflow can then never be '
                   'saved ready - at best a draft with a needs_review reason')
@click.option('--reviewer', default='', metavar='CLI:MODEL',
              help='The reviewer for this run as CLI:MODEL (e.g. claude:sonnet or codex:<model>); a '
                   'non-default reviewer is a security-relevant setting (ANYUSE_MAP_REVIEWER)')
@click.option('--dry-run', is_flag=True, help='Plan, compile and prove; write nothing')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def workflow_from_map(app, task, map_files, routines, records, params, use_model, name, no_review, reviewer, dry_run,
                      json_output):
    """Compile a NEW task for a known app from its map, without a demonstration.

    The map merges the probed effect map, discovered commands and the steps of proven workflows and
    recorded routines. The planner composes known transitions; anything the
    map cannot provide is a gap for the agent bridge, a reference the text does not name is a
    parameter. Then the unchanged draft path: fill (gaps only), the fresh-value proof, the saved
    workflow, a replay with 0 model calls. Every chosen step cites its map source (map_plan.json).
    A workflow is saved READY only after the coverage check found every instruction of the task
    consumed AND an independent reviewer model (ANYUSE_MAP_REVIEWER, default claude:sonnet) said
    the plan covers the task; otherwise it is a draft with a needs_review reason, never ready.
    """
    from anyuse.execution import draft_compile as DC
    from anyuse.execution import map_compile as M
    from anyuse.execution import map_review as MR
    amap = M.AppMap(app=app)
    if map_files:
        for f in map_files:
            amap.merge(M.load_map(f))
    else:
        amap.merge(M.from_effect_map(app=app))
        for p in sorted(glob.glob(os.path.join(M.DISCOVERY_DIR, "dump_*.json.gz"))):
            m = M.from_discovery(M.load_discovery_dump(p))
            if m.app == app:
                amap.merge(m)
    for f in routines:
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        compiled = data.get("compiled") if isinstance(data, dict) else data
        amap.merge(M.from_routine(compiled or [], str((data if isinstance(data, dict) else {}).get("goal") or ""),
                                  os.path.basename(f), apps=[app]))
    for f in records:
        with open(f, encoding="utf-8") as fh:
            amap.merge(M.from_workflow_record(json.load(fh), os.path.basename(f)))
    values = {}
    for item in params:
        if '=' not in item:
            raise click.UsageError(f"--param expects NAME=VALUE, got {item!r}")
        k, v = item.split('=', 1)
        values[k.strip()] = v
    rev = None
    if not no_review:
        try:
            rev = MR.reviewer_from_settings(reviewer or None)
        except ValueError as exc:
            raise click.UsageError(str(exc))
    # the model planner is arm P (plan_by_model): its own model function (schema map_plan_p), never arm M's
    report = DC.run_sync(M.from_map(app, task, amap, name=name, llm=M.default_llm_p if use_model else None,
                                    save=not dry_run, values=values, reviewer=rev,
                                    planner="model" if use_model else "lexicon"))
    if json_output:
        click.echo(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        sys.exit(0 if report["status"] == "ready" else 1)
    plan = report["plan"]
    click.echo(f"Map: {len(amap.transitions)} transition(s) from {len(amap.sources)} source(s); planner "
               f"{plan['how']} in {plan['ms']} ms, model calls {plan['model'].get('calls', 0)} "
               f"(US${float(plan['model'].get('cost_usd') or 0):.4f})")
    click.echo("Plan:")
    for i, s in enumerate(plan["steps"]):
        cites = ", ".join(f"{c.get('kind')}:{str(c.get('ref'))[:30]}" for c in s["cites"][:2])
        click.echo(f"  {i:2} {s['effect']:14} {s['app']:10} {json.dumps(s['bindings'], ensure_ascii=False)[:60]}  [{cites}]")
    for g in plan["gaps"]:
        click.echo(f"  ! gap: {g['intent']} ({g['reason']})")
    for u in plan["unbound"]:
        click.echo(f"  ? unbound parameter {u['param']!r}: {u.get('reference') or u.get('reason')} (give it with --param {u['param']}=VALUE)")
    proof = report["proof"]
    kind = "Replay proof" if proof.get("fixed_routine") else "Fresh-input proof"
    click.echo(f"{kind} ({proof.get('harness')}): {'PASSED' if proof.get('passed') else 'NOT PASSED'} — {proof.get('reason')}")
    rv = report.get("review") or {}
    v = rv.get("verdict") or {}
    if not plan["complete"]:
        click.echo("Gate R: not asked (the plan has gaps or unbound references)")
    elif v:
        click.echo(f"Gate R ({v.get('cli', '?')}:{v.get('model', '?')}): {'COVERS' if rv.get('covers') else 'DOES NOT COVER'}"
                   + (f" — {v.get('missing')}" if v.get('missing') else "") + f" ({MR.COST_NOTE})")
    else:
        click.echo("Gate R: not run (no reviewer) — the workflow cannot be ready")
    click.echo(f"Status: {report['status'].upper()}")
    for r in report["reasons"]:
        click.echo(f"  - {r}")
    if report.get("saved"):
        click.echo(f"✓ Saved: {report['saved']['record']} (plan and citations: {report['saved'].get('map_plan')})")
    elif dry_run:
        click.echo("(--dry-run: nothing written)")
    if report["status"] == "needs_fill":
        click.echo(f"Next: `anyuse workflow fill {report['workflow']} --param NAME=VALUE ...` (the agent bridge fills "
                   "the gaps once in the machine that drives the app)")
    sys.exit(0 if report["status"] == "ready" else 1)


@workflow.command('approvals')
@click.argument('name')
@click.option('--level', type=click.Choice(['auto', 'all', 'destructive', 'off']), default=None,
              help='auto (default): a started replay asks nothing; all / destructive: opt-in holds')
def workflow_approvals(name, level):
    """Show or set which steps of a saved workflow ask before they run.

    By default (`auto`) a replay started with its values asks nothing: starting it is the
    approval, and a replay that deviates from the recording stops. `all` and `destructive` hold
    steps for a person. Lowering the holds again needs the typed-code confirmation. The same
    levels apply to draft-compiled workflows and to recorded ones (their template).
    """
    from anyuse.execution import draft_compile as DC
    _wf, rec = DC.find_saved(name)
    if rec is None and _wf is not None:
        # a recorded (non-draft) workflow keeps its level on its template
        from anyuse.memory.storage import WorkflowStorage
        tpl = WorkflowStorage().get_template(_wf.id)
        if tpl is not None:
            rec = {"workflow": _wf.name, "approval": dict(tpl.approval or {})}
    if rec is None:
        raise click.ClickException(f"no replayable workflow named {name!r}")
    if level is None:
        appr = rec.get("approval") or {}
        click.echo(f"{name}: level {DC.approval_level(rec)}, replays run with "
                   f"{DC.effective_hil_mode(rec)!r} (supervised runs: {int(appr.get('supervised_runs') or 0)})")
        return
    if DC.lowers_holds(rec, level):
        import hashlib
        from anyuse.execution.human_gate import TtyGate
        summary = {"purpose": "change-approvals", "action": f"approvals {level} for {name}",
                   "effects": {"value": level, "security_relevant": True,
                               "warning": ("no step of this workflow asks before it runs, including send, "
                                           "delete and pay steps" if level == "off" else
                                           "only steps whose labels are destructive ask before they run")}}
        digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
        why = TtyGate()(summary, digest)
        if why:
            raise click.ClickException(why)
    try:
        out = DC.set_approval_level(name, level)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"✓ {out['workflow']}: approvals {out['level']} (replays run with {out['mode']!r})")


@workflow.command('acknowledge-host')
@click.argument('host')
@click.option('--note', default='', help='Why this host is acceptable to replay against')
@click.option('--ignore-robots', is_flag=True,
              help='ALSO record that robots.txt should not stop replays against '
                   'THIS host (your own site, or written permission)')
@click.option('--reason', default='',
              help='Required with --ignore-robots: why you may do this')
@click.option('--readonly-post-endpoint', default='',
              help='Exact https endpoint to approve for a read-only POST')
@click.option('--readonly-post-operation', default='',
              help='Exact operation identity shown by compile (for example query:Search)')
def workflow_acknowledge_host(host, note, ignore_robots, reason,
                              readonly_post_endpoint, readonly_post_operation):
    """Record that AnyUse may send requests to HOST.

    Commands that run a proof (compile, learn) ask this inline before the proof
    replays the compiled request against a new host and record the same entry;
    this command records it ahead of time, for example before an agent calls a
    tool over MCP. Entries live in api_hosts.json in your data directory.

    HOST is a host with an optional port (crates.io, 127.0.0.1:8080); a URL
    records its host and port.
    """
    from anyuse.execution.api_permission import acknowledge_host, host_key, hosts_file
    typed = host
    try:
        host = host_key(typed)
        entry = acknowledge_host(host, note=note, ignore_robots=ignore_robots,
                                 robots_reason=reason,
                                 readonly_post_endpoint=readonly_post_endpoint,
                                 readonly_post_operation=readonly_post_operation)
    except ValueError as exc:
        click.echo(f"✗ {exc}")
        raise SystemExit(2)
    click.echo(f"✓ {host} acknowledged at {entry['at']} ({hosts_file()})")
    if host != typed.strip():
        import urllib.parse
        has_port = urllib.parse.urlsplit("//" + host).port is not None
        click.echo(f"  (the host{' and port' if has_port else ''} of {typed.strip()})")
    if ignore_robots:
        click.echo(f"  !! robots.txt will NOT stop replays against {host}: {reason}")
        click.echo("     This applies to this host only and is shown in every proof.")


@workflow.command('delete')
@click.argument('name')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
def workflow_delete(name, yes):
    """Delete a saved workflow or compiled web tool."""
    import shutil
    from anyuse.memory.storage import WorkflowStorage
    from anyuse.mcpserver.compiled_tools import find_compiled

    storage = WorkflowStorage()
    wf = storage.get(name)
    compiled = find_compiled(name, storage)
    if not wf and not compiled:
        click.echo(f"Workflow '{name}' not found (anyuse workflow list shows the names).", err=True)
        sys.exit(1)

    if not yes:
        if not click.confirm(f"Delete workflow '{name}'?"):
            click.echo("Cancelled.")
            return

    if wf:
        storage.delete(name)
    if compiled:
        folder = os.path.dirname(compiled[0])
        if os.path.isfile(os.path.join(folder, "workflow.json")):
            # the folder belongs to another saved workflow: only this tool's compile files go
            from anyuse.execution.api_recompile import LOCK_FILE, PREVIOUS_FILE
            for file_name in (os.path.basename(compiled[0]), PREVIOUS_FILE, LOCK_FILE):
                try:
                    os.remove(os.path.join(folder, file_name))
                except FileNotFoundError:
                    pass
        elif os.path.isdir(folder):
            shutil.rmtree(folder)
    click.echo(f"✓ Workflow '{name}' deleted")


def _compiled_only(storage, name):
    """``(path, record)`` of a compiled web tool NAME that has no saved workflow of its own, or None."""
    from anyuse.mcpserver.compiled_tools import find_compiled
    if storage.get(name):
        return None
    return find_compiled(name, storage)


@workflow.command('rename')
@click.argument('name')
@click.argument('new_name')
def workflow_rename(name, new_name):
    """Rename a saved workflow (a learned run is named after its task)."""
    import re
    from anyuse.memory.storage import WorkflowStorage

    storage = WorkflowStorage()
    wf = storage.get(name)
    compiled = _compiled_only(storage, name)
    if not wf and not compiled:
        raise click.ClickException(f"workflow '{name}' not found (anyuse workflow list shows the names)")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", new_name):
        raise click.ClickException("a workflow name is 1 to 64 letters, digits, _ or -")
    if compiled:
        _rename_compiled(storage, name, new_name, *compiled)
        click.echo(f"✓ Renamed '{name}' to '{new_name}'")
        return
    if _compiled_recording_summary(storage, wf):
        raise click.ClickException(
            f"'{name}' is a compiled recording; its name is set when it is compiled: "
            f"anyuse workflow from-draft DRAFT --name {new_name}")
    if storage.get(new_name):
        raise click.ClickException(f"a workflow named '{new_name}' exists already")
    storage.rename(name, new_name)
    click.echo(f"✓ Renamed '{name}' to '{new_name}'")


def _rename_compiled(storage, name, new_name, path, record):
    """Move a compiled web tool's folder to workflow_<new_name> and rename it in its record: calls find the
    record by its name and save their outcome in the folder of that name."""
    from anyuse.execution.api_compile import persist_compiled
    from anyuse.mcpserver.compiled_tools import find_compiled
    folder = os.path.dirname(path)
    target = str(storage.get_workflow_dir(new_name))
    if storage.get(new_name) or find_compiled(new_name, storage) or os.path.exists(target):
        raise click.ClickException(f"a workflow named '{new_name}' exists already")
    if os.path.isfile(os.path.join(folder, "workflow.json")):
        raise click.ClickException(
            f"'{name}' is kept with another saved workflow in {folder}; compile it again under the new name")
    os.rename(folder, target)
    record["workflow"] = new_name
    persist_compiled(storage, new_name, record)


@workflow.command('export')
@click.argument('name')
@click.argument('filepath')
def workflow_export(name, filepath):
    """Export a workflow, or a compiled web tool's record, to a JSON file."""
    from anyuse.memory.storage import WorkflowStorage

    storage = WorkflowStorage()
    workflow = storage.get(name)
    compiled = None if workflow else _compiled_only(storage, name)
    if not workflow and not compiled:
        click.echo(f"Workflow '{name}' not found (anyuse workflow list shows the names).", err=True)
        sys.exit(1)

    if compiled:
        with open(filepath, "w", encoding="utf-8") as fh:
            json.dump(compiled[1], fh, indent=2)
        click.echo(f"✓ Exported the compiled web tool '{name}' to {filepath}")
        return
    storage.export(name, filepath)
    click.echo(f"✓ Exported to {filepath}")


@workflow.command('import')
@click.argument('filepath')
def workflow_import(filepath):
    """Import a workflow from JSON file."""
    from anyuse.memory.storage import WorkflowStorage
    
    if not os.path.exists(filepath):
        click.echo(f"File not found: {filepath}")
        return
    
    storage = WorkflowStorage()
    workflow = storage.import_from(filepath)
    
    if workflow:
        click.echo(f"✓ Imported workflow: {workflow.name}")
    else:
        click.echo("Failed to import workflow.")


@workflow.command('verify', hidden=True)
@click.argument('name', required=False)
@click.option('--all', 'verify_all', is_flag=True, help='Verify all workflows')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.pass_context
def workflow_verify(ctx, name, verify_all, json_output):
    """
    Verify workflow robustness.
    
    Analyzes workflows for issues that could cause replay failures:
    - Window-specific coordinates
    - Hardcoded positions
    - Redundant steps
    - Missing checkpoints
    
    \b
    Examples:
        anyuse workflow verify my_workflow
        anyuse workflow verify --all
    """
    click.echo("Error: this command is not available in this release.", err=True)
    sys.exit(2)
    from anyuse.memory.storage import WorkflowStorage
    from agent.workflow_verifier import WorkflowVerifier, VerificationResult
    
    storage = WorkflowStorage()
    verifier = WorkflowVerifier(storage=storage, verbose=not json_output)
    
    if verify_all:
        workflows = storage.list_all()
        if not workflows:
            click.echo("No workflows to verify.")
            return
        
        results = {}
        for wf in workflows:
            click.echo(f"\n{'─' * 50}")
            result = verifier.verify_recording(wf)
            results[wf.name] = {
                "passed": result.passed,
                "robustness_score": result.robustness_score,
                "issues": len(result.issues),
            }
            
            # Update workflow
            wf.verified = result.passed
            wf.verification_issues = result.issues
            wf.robustness_score = result.robustness_score
            storage.save(wf)
        
        if json_output:
            click.echo(json.dumps(results, indent=2))
        else:
            click.echo(f"\n{'═' * 50}")
            click.echo("VERIFICATION SUMMARY")
            click.echo(f"{'═' * 50}")
            passed = sum(1 for r in results.values() if r["passed"])
            click.echo(f"  Passed: {passed}/{len(results)}")
            for name, r in results.items():
                icon = "✅" if r["passed"] else "⚠️"
                click.echo(f"  {icon} {name} - {r['robustness_score']:.0%} robustness, {r['issues']} issue(s)")
    
    elif name:
        workflow = storage.get(name) or storage.find(name)
        if not workflow:
            click.echo(f"Workflow '{name}' not found.")
            return
        
        result = verifier.verify_recording(workflow)
        
        # Update workflow
        workflow.verified = result.passed
        workflow.verification_issues = result.issues
        workflow.robustness_score = result.robustness_score
        storage.save(workflow)
        
        if json_output:
            output = {
                "workflow": workflow.name,
                "passed": result.passed,
                "robustness_score": result.robustness_score,
                "goal_achieved": result.goal_achieved,
                "issues": [i.to_dict() for i in result.issues],
                "suggestions": result.suggestions,
            }
            click.echo(json.dumps(output, indent=2))
        else:
            click.echo(f"\n{'═' * 50}")
            click.echo(f"VERIFICATION: {workflow.name}")
            click.echo(f"{'═' * 50}")
            
            icon = "✅ PASSED" if result.passed else "⚠️ ISSUES FOUND"
            click.echo(f"\nResult: {icon}")
            click.echo(f"Robustness Score: {result.robustness_score:.0%}")
            click.echo(f"Goal Achieved: {'Yes' if result.goal_achieved else 'No'}")
            
            if result.issues:
                click.echo(f"\nIssues ({len(result.issues)}):")
                for issue in result.issues:
                    severity_icon = "❌" if issue.severity == "error" else "⚠️" if issue.severity == "warning" else "ℹ️"
                    step_info = f"Step {issue.step_index + 1}" if issue.step_index >= 0 else "Workflow"
                    click.echo(f"  {severity_icon} [{step_info}] {issue.issue_type}")
                    click.echo(f"      {issue.description}")
                    if issue.suggested_fix:
                        click.echo(f"      Fix: {issue.suggested_fix}")
            
            if result.suggestions:
                click.echo("\nSuggestions:")
                for s in result.suggestions:
                    click.echo(f"  • {s}")
            
            # Clean up screenshots if passed
            if result.passed:
                verifier.cleanup_screenshots(workflow)
                click.echo("\n✓ Verification screenshots cleaned up")
    else:
        click.echo("Error: Specify a workflow name or use --all")
        click.echo("\nUsage:")
        click.echo("  anyuse workflow verify my_workflow")
        click.echo("  anyuse workflow verify --all")


@workflow.command('improve', hidden=True)
@click.argument('name')
@click.option('--max-attempts', default=3, help='Maximum improvement attempts')
@click.pass_context
def workflow_improve(ctx, name, max_attempts):
    """
    Self-improve a workflow by re-recording broken steps.
    
    Uses AI to identify issues and re-record problematic steps
    with more robust methods.
    
    \b
    Example:
        anyuse workflow improve my_workflow
    """
    click.echo("Error: this command is not available in this release.", err=True)
    sys.exit(2)
    from anyuse.memory.storage import WorkflowStorage
    from agent.workflow_verifier import WorkflowVerifier
    
    api_key = ctx.obj.get('api_key')
    if not api_key:
        click.echo("Error: GEMINI_API_KEY required for self-improvement.", err=True)
        sys.exit(1)
    
    storage = WorkflowStorage()
    workflow = storage.get(name) or storage.find(name)
    
    if not workflow:
        click.echo(f"Workflow '{name}' not found.")
        return
    
    verifier = WorkflowVerifier(storage=storage, verbose=True)
    
    # First verify to find issues
    click.echo(f"\n🔍 Analyzing workflow: {workflow.name}")
    result = verifier.verify_recording(workflow)
    
    if result.passed:
        click.echo("✅ Workflow already passes verification!")
        click.echo(f"   Robustness score: {result.robustness_score:.0%}")
        return
    
    click.echo(f"\n⚠️ Found {len(result.issues)} issue(s)")
    for issue in result.issues:
        severity_icon = "❌" if issue.severity == "error" else "⚠️"
        click.echo(f"  {severity_icon} Step {issue.step_index + 1}: {issue.description}")
    
    # Check if there are errors to fix
    error_issues = [i for i in result.issues if i.severity == "error"]
    if not error_issues:
        click.echo("\n✓ No critical errors to fix (only warnings)")
        click.echo("  Consider manually reviewing the warnings.")
        return
    
    # Attempt self-improvement
    if workflow.self_improvement_attempts >= max_attempts:
        click.echo(f"\n⚠️ Maximum improvement attempts ({max_attempts}) reached")
        click.echo("  Manual intervention required.")
        return
    
    click.echo(f"\n🔧 Attempting self-improvement (attempt {workflow.self_improvement_attempts + 1}/{max_attempts})")
    click.echo("   This will replay the workflow up to the broken step,")
    click.echo("   then use AI to re-record the remaining steps.\n")
    
    if not click.confirm("Continue?"):
        click.echo("Cancelled.")
        return
    
    try:
        # Try to get agent and executor for full self-improvement
        from anyuse.core.agent import Agent
        
        agent = Agent(
            api_key=api_key,
            verbose=True,
            auto_confirm=True,
            auto_learn=True,
        )
        
        improved = verifier.self_improve(
            workflow,
            result.issues,
            agent=agent,
            executor=agent.executor if hasattr(agent, 'executor') else None,
        )
        
        if improved:
            click.echo("\n✓ Self-improvement completed")
            click.echo("  Re-verify with: anyuse workflow verify " + name)
        else:
            click.echo("\n⚠️ Self-improvement could not complete")
            click.echo("  Manual intervention may be required.")
            
    except Exception as e:
        click.echo(f"\n❌ Self-improvement error: {e}")
        click.echo("  Try manually re-recording the workflow.")


# ─────────────────────────────────────────────────────────
# Credential Commands
# ─────────────────────────────────────────────────────────

@cli.group()
def selfheal():
    """Element-matcher tuning on YOUR OWN recorded AX snapshots."""
    pass


def _load_pairs(path):
    import json as _json
    with open(path) as fh:
        data = _json.load(fh)
    pairs = data.get("pairs") if isinstance(data, dict) else data
    if not isinstance(pairs, list) or not pairs:
        raise SystemExit(f"{path} holds no pairs; expected a list of "
                         '{"target": {...}, "candidates": [...], "correct": <index|-1>}')
    return pairs


@selfheal.command('eval')
@click.argument('pairs_file', type=click.Path(exists=True))
@click.option('--weights', type=click.Path(exists=True),
              help='Weights JSON from `selfheal fit` (default: the web-fitted prior)')
@click.option('--json', 'json_output', is_flag=True)
def selfheal_eval(pairs_file, weights, json_output):
    """Compare the exact cascade against the weighted scorer on labelled pairs.

    PAIRS_FILE is JSON: a list of {"target", "candidates", "correct"}, where
    `correct` is the index of the right candidate or -1 when the element is
    gone (in which case abstaining IS the right answer).
    """
    import json as _json
    from anyuse.context.element_scorer import evaluate

    pairs = _load_pairs(pairs_file)
    w = _json.load(open(weights)) if weights else None
    reports = [evaluate(pairs, "cascade"), evaluate(pairs, "weighted", w)]
    if json_output:
        click.echo(_json.dumps([r.to_dict() for r in reports], indent=2))
        return
    click.echo(f"{len(pairs)} labelled pair(s) from {pairs_file}")
    for r in reports:
        click.echo("  " + r.line())
    click.echo("\nabstaining on a gone element counts as correct; wrong-match is what hurts.")
    if not weights:
        click.echo("weights: the WEB-FITTED PRIOR — fitted on web DOM, where elements "
                   "have classes and AX elements do not. Refit before trusting it.")


@selfheal.command('fit')
@click.argument('pairs_file', type=click.Path(exists=True))
@click.option('--out', 'out_path', default='', help='Write the fitted weights here')
@click.option('--rounds', default=3, show_default=True)
def selfheal_fit(pairs_file, out_path, rounds):
    """Fit matcher weights on YOUR labelled AX pairs and report what changed."""
    import json as _json
    from anyuse.context.element_scorer import WEB_FITTED_WEIGHTS, evaluate, fit

    pairs = _load_pairs(pairs_file)
    before = evaluate(pairs, "weighted", WEB_FITTED_WEIGHTS)
    weights, after = fit(pairs, rounds=rounds)
    click.echo(f"{len(pairs)} labelled pair(s)")
    click.echo("  prior  " + before.line())
    click.echo("  fitted " + after.line())
    click.echo("\nfitted weights:")
    for k, v in sorted(weights.items(), key=lambda kv: -kv[1]):
        click.echo(f"  {k:<6s} {v:.3f}   (prior {WEB_FITTED_WEIGHTS[k]:.3f})")
    if out_path:
        with open(out_path, "w") as fh:
            _json.dump(weights, fh, indent=2)
        click.echo(f"\n✓ written to {out_path}")
        click.echo("  enable the weighted matcher with ANYUSE_ELEMENT_MATCHER=weighted")
    if after.top1 <= before.top1:
        click.echo("\nNote: fitting did not improve top-1 on these pairs. With few "
                   "pairs that is expected; record more before switching matcher.")


@cli.group()
def credential():
    """Credential management commands."""
    pass


@credential.command('set')
@click.argument('alias')
@click.option('--persist', '-p', is_flag=True, help='Persist to disk/keychain')
def credential_set(alias, persist):
    """Set a credential (will prompt for value)."""
    from anyuse.credentials.vault import prompt_for_credential
    
    success = prompt_for_credential(alias, persist=persist)
    sys.exit(0 if success else 1)


@credential.command('list')
def credential_list():
    """List credential aliases (not values)."""
    from anyuse.credentials.vault import CredentialVault
    
    vault = CredentialVault()
    aliases = vault.list_aliases()
    
    if not aliases:
        click.echo("No credentials stored.")
        click.echo("\nAdd one with:")
        click.echo("  anyuse credential set my_password")
        return
    
    click.echo("Stored credentials:")
    for alias in aliases:
        click.echo(f"  • {alias}")


@credential.command('delete')
@click.argument('alias')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
def credential_delete(alias, yes):
    """Delete a credential."""
    from anyuse.credentials.vault import CredentialVault
    
    vault = CredentialVault()
    
    if alias not in vault.list_aliases():
        click.echo(f"Credential '{alias}' not found.")
        return
    
    if not yes:
        if not click.confirm(f"Delete credential '{alias}'?"):
            click.echo("Cancelled.")
            return
    
    vault.remove_credential(alias)
    click.echo(f"✓ Credential '{alias}' deleted")


@credential.command('use')
@click.argument('alias')
@click.option('--enter', '-e', is_flag=True, help='Press Enter after typing')
def credential_use(alias, enter):
    """Type a credential at current cursor position."""
    import time
    from anyuse.credentials.vault import CredentialVault
    
    vault = CredentialVault()
    
    if alias not in vault.list_aliases():
        click.echo(f"Credential '{alias}' not found.")
        sys.exit(1)

    import shlex
    from anyuse.execution.actions import drive_enabled
    if not drive_enabled():
        click.echo("✗ Desktop input is off, so nothing was typed.", err=True)
        click.echo(f"  To type it, turn desktop input on for this command: "
                   f"ANYUSE_DRIVE_ENABLED=on anyuse credential use {shlex.quote(alias)}"
                   + (" --enter" if enter else ""), err=True)
        sys.exit(1)

    click.echo(f"Typing credential '{alias}' in 3 seconds...")
    click.echo("Position your cursor in the target field.")
    time.sleep(3)
    
    result = vault.use_credential(alias, press_enter=enter)
    
    if result.get("success"):
        click.echo("✓ Credential typed")
    else:
        click.echo(f"✗ Failed: {result.get('error')}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────
# Account Commands (Agent Mode)
# ─────────────────────────────────────────────────────────

@cli.group()
def account():
    """Account management for agent mode."""
    pass


@account.command('list')
@click.option('--type', 'cred_type', type=click.Choice(['website', 'app', 'cli', 'api', 'system']),
              help='Filter by credential type')
@click.option('--created-by', type=click.Choice(['user', 'agent']), help='Filter by creator')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def account_list(cred_type, created_by, json_output):
    """List registered accounts."""
    from anyuse.credentials.accounts import AccountRegistry, CredentialType
    
    registry = AccountRegistry()
    accounts = registry.list_all()
    
    # Apply filters
    if cred_type:
        accounts = [a for a in accounts if a.credential_type == CredentialType(cred_type)]
    if created_by:
        accounts = [a for a in accounts if a.created_by == created_by]
    
    if not accounts:
        click.echo("No accounts registered.")
        click.echo("\nAccounts are created when the agent signs up for services in --agent-mode.")
        return
    
    if json_output:
        import json as json_lib
        data = [a.to_dict() for a in accounts]
        click.echo(json_lib.dumps(data, indent=2))
        return
    
    click.echo("Registered accounts:\n")
    for acc in accounts:
        icon = "🤖" if acc.created_by == "agent" else "👤"
        sso_info = f" (via {acc.sso_provider.value})" if acc.created_via_sso else ""
        tfa_info = " 🔐" if acc.has_2fa else ""
        
        click.echo(f"{icon} {acc.service}{sso_info}{tfa_info}")
        click.echo(f"   Type: {acc.credential_type.value}")
        if acc.username:
            click.echo(f"   User: {acc.username}")
        if acc.email:
            click.echo(f"   Email: {acc.email}")
        if acc.password_alias:
            click.echo(f"   Password: stored as '{acc.password_alias}'")
        click.echo()


@account.command('show')
@click.argument('service')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def account_show(service, json_output):
    """Show details for a specific account."""
    from anyuse.credentials.accounts import AccountRegistry
    
    registry = AccountRegistry()
    acc = registry.get(service)
    
    if not acc:
        click.echo(f"Account '{service}' not found.")
        click.echo("\nAvailable accounts:")
        for name in registry.list_services():
            click.echo(f"  • {name}")
        return
    
    if json_output:
        import json as json_lib
        click.echo(json_lib.dumps(acc.to_dict(), indent=2))
        return
    
    click.echo(f"\nAccount: {acc.service}")
    click.echo("-" * 40)
    click.echo(f"  Type: {acc.credential_type.value}")
    click.echo(f"  Created by: {acc.created_by}")
    click.echo(f"  Created at: {acc.created_at}")
    
    if acc.username:
        click.echo(f"  Username: {acc.username}")
    if acc.email:
        click.echo(f"  Email: {acc.email}")
    if acc.url:
        click.echo(f"  URL: {acc.url}")
    if acc.app_bundle_id:
        click.echo(f"  Bundle ID: {acc.app_bundle_id}")
    
    click.echo()
    click.echo("Authentication:")
    if acc.password_alias:
        click.echo(f"  Password alias: {acc.password_alias}")
    if acc.created_via_sso:
        click.echo(f"  SSO: {acc.sso_provider.value} ({acc.sso_account})")
    if acc.has_2fa:
        click.echo(f"  2FA: {acc.two_factor_method.value}")
        if acc.totp_secret_alias:
            click.echo(f"  TOTP alias: {acc.totp_secret_alias}")
    
    if acc.last_used:
        click.echo(f"\nLast used: {acc.last_used}")
        click.echo(f"Use count: {acc.use_count}")


@account.command('delete')
@click.argument('service')
@click.option('--yes', '-y', is_flag=True, help='Skip confirmation')
def account_delete(service, yes):
    """Delete an account from the registry."""
    from anyuse.credentials.accounts import AccountRegistry
    from anyuse.credentials.vault import CredentialVault
    
    registry = AccountRegistry()
    acc = registry.get(service)
    
    if not acc:
        click.echo(f"Account '{service}' not found.")
        return
    
    if not yes:
        click.echo(f"Account: {acc.service}")
        click.echo(f"  Username: {acc.username}")
        click.echo(f"  Created by: {acc.created_by}")
        if not click.confirm("\nDelete this account?"):
            click.echo("Cancelled.")
            return
    
    # Also remove associated credentials
    vault = CredentialVault(use_keychain=True)
    removed_creds = []
    if acc.password_alias:
        vault.remove_credential(acc.password_alias)
        removed_creds.append(acc.password_alias)
    if acc.totp_secret_alias:
        vault.remove_credential(acc.totp_secret_alias)
        removed_creds.append(acc.totp_secret_alias)
    
    registry.delete(service)
    
    click.echo(f"✓ Account '{service}' deleted")
    if removed_creds:
        click.echo(f"  Also removed credentials: {', '.join(removed_creds)}")


@account.command('stats')
def account_stats():
    """Show account registry statistics."""
    from anyuse.credentials.accounts import AccountRegistry
    
    registry = AccountRegistry()
    stats = registry.get_statistics()
    
    click.echo("\nAccount Registry Statistics")
    click.echo("-" * 40)
    click.echo(f"  Total accounts: {stats['total_accounts']}")
    click.echo()
    click.echo("  By type:")
    for t, count in stats['by_type'].items():
        if count > 0:
            click.echo(f"    {t}: {count}")
    click.echo()
    click.echo("  By creator:")
    click.echo(f"    User: {stats['by_creator']['user']}")
    click.echo(f"    Agent: {stats['by_creator']['agent']}")
    click.echo()
    click.echo(f"  SSO accounts: {stats['sso_accounts']}")
    click.echo(f"  With 2FA: {stats['with_2fa']}")


@account.command('set-2fa')
@click.argument('service')
@click.option('--method', type=click.Choice(['totp', 'sms', 'push']), required=True,
              help='2FA method')
@click.option('--totp-secret', help='TOTP secret (base32 encoded)')
@click.option('--sms-sender', help='Expected SMS sender')
def account_set_2fa(service, method, totp_secret, sms_sender):
    """Configure 2FA for an account."""
    from anyuse.credentials.accounts import AccountRegistry
    from anyuse.credentials.two_factor import TwoFactorMethod
    from anyuse.credentials.vault import CredentialVault
    import getpass
    
    registry = AccountRegistry()
    acc = registry.get(service)
    
    if not acc:
        click.echo(f"Account '{service}' not found.")
        return
    
    vault = CredentialVault(use_keychain=True)
    
    acc.has_2fa = True
    acc.two_factor_method = TwoFactorMethod(method)
    
    if method == 'totp':
        if not totp_secret:
            totp_secret = getpass.getpass("Enter TOTP secret (base32): ")
        
        totp_alias = f"{service}-totp"
        vault.set_credential(totp_alias, totp_secret, persist=True)
        acc.totp_secret_alias = totp_alias
        click.echo(f"✓ TOTP secret stored as '{totp_alias}'")
    
    elif method == 'sms':
        acc.sms_sender = sms_sender
        click.echo(f"✓ SMS 2FA configured (sender: {sms_sender or 'any'})")
    
    elif method == 'push':
        click.echo("✓ Push 2FA configured")
    
    registry.register(acc)
    click.echo(f"✓ 2FA configured for '{service}'")


# ─────────────────────────────────────────────────────────
# 2FA Commands
# ─────────────────────────────────────────────────────────

@cli.group('2fa')
def twofa():
    """Two-factor authentication commands."""
    pass


@twofa.command('code')
@click.argument('alias')
def twofa_code(alias):
    """Get current TOTP code for an account."""
    from anyuse.credentials.vault import CredentialVault
    
    vault = CredentialVault(use_keychain=True)
    
    try:
        code = vault.get_2fa_code(alias)
        if code:
            # Get time remaining
            from anyuse.credentials.two_factor import TOTPGenerator
            totp = TOTPGenerator(get_secret_func=vault.get_credential)
            remaining = totp.get_time_remaining(vault.get_credential(f"{alias}-totp"))
            
            click.echo(f"Code: {code}")
            click.echo(f"Expires in: {remaining}s")
        else:
            click.echo(f"No TOTP secret found for '{alias}'")
            click.echo("\nSet one with:")
            click.echo(f"  anyuse account set-2fa {alias} --method totp")
    except Exception as e:
        click.echo(f"Error: {e}")


@twofa.command('sms')
@click.option('--sender', help='Filter by sender')
@click.option('--wait', '-w', is_flag=True, help='Wait for new SMS')
@click.option('--timeout', '-t', default=120, help='Timeout in seconds')
def twofa_sms(sender, wait, timeout):
    """Read verification code from SMS (Messages app)."""
    from anyuse.credentials.two_factor import SMSCodeReader
    
    reader = SMSCodeReader(verbose=True)
    
    if wait:
        click.echo(f"Waiting for SMS code (timeout: {timeout}s)...")
        code = reader.wait_for_code(sender=sender, timeout=timeout)
    else:
        code = reader.get_latest_code(sender=sender)
    
    if code:
        click.echo(f"Code: {code}")
    else:
        click.echo("No verification code found.")


# ─────────────────────────────────────────────────────────
# Other Commands
# ─────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def interactive(ctx):
    """Interactive mode for multiple tasks."""
    from anyuse.core.agent import Agent
    
    api_key = ctx.obj.get('api_key')
    if not _model_configured():
        click.echo(_MODEL_SETUP_HINT, err=True)
        sys.exit(1)
    
    agent = Agent(
        api_key=api_key,
        verbose=True,
        auto_confirm=ctx.obj.get('auto_confirm', False),
    )
    
    click.echo("\n" + "=" * 50)
    click.echo("AnyUse Interactive Mode")
    click.echo("=" * 50)
    click.echo("Commands: quit, help, context, workflows, credentials")
    click.echo()
    
    while True:
        try:
            task = click.prompt("anyuse", prompt_suffix="> ").strip()
            
            if not task:
                continue
            
            if task.lower() in ('quit', 'exit', 'q'):
                click.echo("Goodbye!")
                break
            
            if task.lower() == 'help':
                click.echo("Commands:")
                click.echo("  quit        - Exit interactive mode")
                click.echo("  help        - Show this help")
                click.echo("  context     - Show macOS context")
                click.echo("  workflows   - List saved workflows")
                click.echo("  credentials - List credential aliases")
                click.echo("  <task>      - Run a task")
                continue
            
            if task.lower() == 'context':
                ctx_data = agent.get_context()
                click.echo(json.dumps(ctx_data, indent=2, default=str))
                continue
            
            if task.lower() == 'workflows':
                for name in agent.list_workflows():
                    click.echo(f"  • {name}")
                continue
            
            if task.lower() == 'credentials':
                for alias in agent.list_credentials():
                    click.echo(f"  • {alias}")
                continue
            
            # Run the task
            result = agent.run(task)
            _print_result(result, verbose=True)
            
        except (KeyboardInterrupt, EOFError):
            click.echo("\nGoodbye!")
            break


# ─────────────────────────────────────────────────────────
# Guide Commands
# ─────────────────────────────────────────────────────────

@cli.group()
def guide():
    """Application/website guide management.
    
    Guides provide "how-to" documentation for applications and websites
    that help the AI agent understand how to navigate and use them.
    """
    pass


@guide.command('list')
@click.option('--type', 'guide_type', type=click.Choice(['application', 'website']),
              help='Filter by guide type')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def guide_list(guide_type, json_output):
    """List all application/website guides."""
    from anyuse.guides.storage import GuideStorage
    from anyuse.guides.models import GuideType
    
    storage = GuideStorage()
    guides = storage.list_all()
    
    # Filter by type if specified
    if guide_type:
        filter_type = GuideType(guide_type)
        guides = [g for g in guides if g.guide_type == filter_type]
    
    if json_output:
        data = [g.to_dict() for g in guides]
        click.echo(json.dumps(data, indent=2))
    else:
        if not guides:
            click.echo("No guides found.")
            click.echo("Use 'anyuse guide add <name>' to create one.")
            return
        
        click.echo(f"\nApplication/Website Guides ({len(guides)}):")
        click.echo("-" * 50)
        
        for guide in sorted(guides, key=lambda g: g.name):
            type_icon = "🌐" if guide.is_website else "📱"
            workflows = f" [{len(guide.linked_workflows)} workflow(s)]" if guide.linked_workflows else ""
            click.echo(f"  {type_icon} {guide.name}{workflows}")
            if guide.short_description:
                click.echo(f"     {guide.short_description}")


@guide.command('show')
@click.argument('name')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def guide_show(name, json_output):
    """Show details of a specific guide."""
    from anyuse.guides.storage import GuideStorage
    
    storage = GuideStorage()
    guide = storage.get(name)
    
    if not guide:
        # Try finding by partial match
        guide = storage.get_by_app(name) or storage.get_by_domain(name)
    
    if not guide:
        click.echo(f"Guide '{name}' not found.")
        click.echo(f"Available guides: {', '.join(storage.list_names())}")
        return
    
    if json_output:
        click.echo(json.dumps(guide.to_dict(), indent=2))
    else:
        click.echo(guide.get_full_context())
        
        if guide.linked_workflows:
            click.echo(f"\nLinked Workflows: {', '.join(guide.linked_workflows)}")


@guide.command('add')
@click.argument('name')
@click.option('--type', 'guide_type', type=click.Choice(['application', 'website']),
              default='application', help='Guide type')
@click.option('--domain', help='Domain for website guides (e.g., github.com)')
@click.option('--description', '-d', help='Short description')
@click.option('--use-case', '-u', multiple=True, help='Use case (can specify multiple)')
def guide_add(name, guide_type, domain, description, use_case):
    """Add a new application/website guide."""
    from anyuse.guides.storage import GuideStorage
    from anyuse.guides.models import AppGuide, GuideType
    
    storage = GuideStorage()
    
    # Check if guide already exists
    if storage.get(name):
        click.echo(f"Guide '{name}' already exists. Use 'anyuse guide edit {name}' to modify.")
        return
    
    # Create guide
    if guide_type == 'website':
        if not domain:
            domain = name.lower()  # Use name as domain if not specified
        guide = AppGuide.create_website_guide(
            name=name,
            domain=domain,
            short_description=description or "",
            use_cases=list(use_case),
        )
    else:
        guide = AppGuide.create_app_guide(
            name=name,
            short_description=description or "",
            use_cases=list(use_case),
        )
    
    storage.save(guide)
    
    click.echo(f"✓ Created guide: {name}")
    click.echo(f"  Type: {guide_type}")
    if description:
        click.echo(f"  Description: {description}")
    if use_case:
        click.echo(f"  Use cases: {', '.join(use_case)}")
    click.echo()
    click.echo(f"Edit the full guide with: anyuse guide edit {name}")


@guide.command('edit')
@click.argument('name')
@click.option('--description', '-d', help='Update short description')
@click.option('--add-use-case', '-u', multiple=True, help='Add use case')
@click.option('--add-landmark', '-l', multiple=True, help='Add UI landmark')
@click.option('--add-shortcut', '-s', nargs=2, multiple=True, help='Add shortcut (action key)')
@click.option('--add-tip', '-t', multiple=True, help='Add navigation tip')
@click.option('--guide-text', '-g', help='Set full guide text (markdown)')
@click.option('--guide-file', type=click.Path(exists=True), help='Load full guide from file')
def guide_edit(name, description, add_use_case, add_landmark, add_shortcut, add_tip, guide_text, guide_file):
    """Edit an existing guide."""
    from anyuse.guides.storage import GuideStorage
    
    storage = GuideStorage()
    guide = storage.get(name)
    
    if not guide:
        guide = storage.get_by_app(name) or storage.get_by_domain(name)
    
    if not guide:
        click.echo(f"Guide '{name}' not found.")
        return
    
    # Track changes
    changes = []
    
    if description:
        guide.short_description = description
        changes.append("description")
    
    if add_use_case:
        guide.use_cases.extend(add_use_case)
        changes.append(f"{len(add_use_case)} use case(s)")
    
    if add_landmark:
        guide.ui_landmarks.extend(add_landmark)
        changes.append(f"{len(add_landmark)} UI landmark(s)")
    
    if add_shortcut:
        for action, key in add_shortcut:
            guide.common_shortcuts[action] = key
        changes.append(f"{len(add_shortcut)} shortcut(s)")
    
    if add_tip:
        guide.navigation_tips.extend(add_tip)
        changes.append(f"{len(add_tip)} tip(s)")
    
    if guide_file:
        with open(guide_file) as f:
            guide.full_guide = f.read()
        changes.append("full guide (from file)")
    elif guide_text:
        guide.full_guide = guide_text
        changes.append("full guide")
    
    if changes:
        storage.save(guide)
        click.echo(f"✓ Updated {guide.name}: {', '.join(changes)}")
    else:
        click.echo("No changes specified. Use options like --description, --add-use-case, etc.")


@guide.command('delete')
@click.argument('name')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
def guide_delete(name, force):
    """Delete a guide."""
    from anyuse.guides.storage import GuideStorage
    
    storage = GuideStorage()
    guide = storage.get(name)
    
    if not guide:
        guide = storage.get_by_app(name) or storage.get_by_domain(name)
    
    if not guide:
        click.echo(f"Guide '{name}' not found.")
        return
    
    if not force:
        if not click.confirm(f"Delete guide '{guide.name}'?"):
            click.echo("Cancelled.")
            return
    
    storage.delete(guide.name)
    click.echo(f"✓ Deleted guide: {guide.name}")


@guide.command('link')
@click.argument('guide_name')
@click.argument('workflow_name')
def guide_link(guide_name, workflow_name):
    """Link a workflow to a guide.
    
    This creates a reference so users can see related workflows
    when viewing a guide's documentation.
    """
    from anyuse.guides.storage import GuideStorage
    from anyuse.memory.storage import WorkflowStorage
    
    guide_storage = GuideStorage()
    workflow_storage = WorkflowStorage()
    
    # Find guide
    guide = guide_storage.get(guide_name)
    if not guide:
        guide = guide_storage.get_by_app(guide_name) or guide_storage.get_by_domain(guide_name)
    
    if not guide:
        click.echo(f"Guide '{guide_name}' not found.")
        return
    
    # Verify workflow exists
    workflow = workflow_storage.get(workflow_name) or workflow_storage.find(workflow_name)
    if not workflow:
        click.echo(f"Workflow '{workflow_name}' not found.")
        return
    
    # Link workflow
    workflow_id = workflow.id if hasattr(workflow, 'id') else workflow_name
    guide_storage.link_workflow_to_guide(guide.name, workflow_id)
    
    click.echo(f"✓ Linked workflow '{workflow_name}' to guide '{guide.name}'")


@guide.command('unlink')
@click.argument('guide_name')
@click.argument('workflow_name')
def guide_unlink(guide_name, workflow_name):
    """Unlink a workflow from a guide."""
    from anyuse.guides.storage import GuideStorage
    
    guide_storage = GuideStorage()
    
    guide = guide_storage.get(guide_name)
    if not guide:
        guide = guide_storage.get_by_app(guide_name) or guide_storage.get_by_domain(guide_name)
    
    if not guide:
        click.echo(f"Guide '{guide_name}' not found.")
        return
    
    guide_storage.unlink_workflow_from_guide(guide.name, workflow_name)
    click.echo(f"✓ Unlinked workflow '{workflow_name}' from guide '{guide.name}'")


@guide.command('import')
@click.argument('filepath', type=click.Path(exists=True))
def guide_import(filepath):
    """Import a guide from a JSON file."""
    from anyuse.guides.storage import GuideStorage
    
    storage = GuideStorage()
    guide = storage.import_guide(filepath)
    
    if guide:
        click.echo(f"✓ Imported guide: {guide.name}")
    else:
        click.echo("Failed to import guide.")


@guide.command('export')
@click.argument('name')
@click.argument('filepath', type=click.Path())
def guide_export(name, filepath):
    """Export a guide to a JSON file."""
    from anyuse.guides.storage import GuideStorage
    
    storage = GuideStorage()
    guide = storage.get(name)
    
    if not guide:
        click.echo(f"Guide '{name}' not found.")
        return
    
    storage.export_guide(name, filepath)
    click.echo(f"✓ Exported guide '{name}' to {filepath}")


# ─────────────────────────────────────────────────────────
# Guide Auto-Generation Commands
# ─────────────────────────────────────────────────────────

@guide.command('observations')
@click.argument('app_or_url')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def guide_observations(app_or_url, json_output):
    """Show what's been learned about an app/website.
    
    Displays observations accumulated from watching sessions,
    which will be used to auto-generate guides.
    
    \b
    Example:
        anyuse guide observations github.com
        anyuse guide observations "Google Chrome"
    """
    from anyuse.guides.generator import GuideGenerator
    from anyuse.guides.storage import GuideStorage
    
    storage = GuideStorage()
    generator = GuideGenerator(guide_storage=storage)
    
    obs = generator.get_observations(app_or_url)
    
    if not obs:
        click.echo(f"No observations found for '{app_or_url}'.")
        click.echo("\nLearn observations from a saved workflow or an agent execution.")
        return
    
    if json_output:
        click.echo(json.dumps(obs.to_dict(), indent=2))
        return
    
    click.echo(f"\n📊 Observations for: {app_or_url}")
    click.echo("=" * 50)
    
    click.echo(f"\n📈 Statistics:")
    click.echo(f"   Total observations: {obs.total_observations}")
    click.echo(f"   Sessions: {obs.total_sessions}")
    click.echo(f"   First seen: {obs.first_seen.strftime('%Y-%m-%d %H:%M')}")
    click.echo(f"   Last seen: {obs.last_seen.strftime('%Y-%m-%d %H:%M')}")
    
    if obs.use_cases:
        click.echo(f"\n🎯 Use Cases ({len(obs.use_cases)}):")
        for case, count in sorted(obs.use_cases.items(), key=lambda x: x[1], reverse=True)[:10]:
            click.echo(f"   • {case} ({count}x)")
    
    if obs.landmarks:
        click.echo(f"\n🗺️ UI Landmarks ({len(obs.landmarks)}):")
        for landmark, count in sorted(obs.landmarks.items(), key=lambda x: x[1], reverse=True)[:10]:
            click.echo(f"   • {landmark} ({count}x)")
    
    if obs.shortcuts:
        click.echo(f"\n⌨️ Keyboard Shortcuts ({len(obs.shortcuts)}):")
        for action, (keys, count) in sorted(obs.shortcuts.items(), key=lambda x: x[1][1], reverse=True)[:10]:
            click.echo(f"   • {action}: {keys} ({count}x)")
    
    if obs.navigation_tips:
        click.echo(f"\n🧭 Navigation Tips ({len(obs.navigation_tips)}):")
        for tip, count in sorted(obs.navigation_tips.items(), key=lambda x: x[1], reverse=True)[:5]:
            click.echo(f"   • {tip} ({count}x)")
    
    if obs.linked_workflows:
        click.echo(f"\n🔗 Linked Workflows ({len(obs.linked_workflows)}):")
        for wf in list(obs.linked_workflows)[:10]:
            click.echo(f"   • {wf}")


@guide.command('generate')
@click.option('--app', '-a', help='Generate guide for specific app/website')
@click.option('--force', '-f', is_flag=True, help='Generate even if below observation threshold')
@click.option('--min-observations', '-m', default=3, help='Minimum observations required')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.pass_context
def guide_generate(ctx, app, force, min_observations, json_output):
    """Generate guides from watcher observations.
    
    Analyzes accumulated observations from watching sessions
    and generates/updates guides automatically.
    
    \b
    Examples:
        anyuse guide generate                    # Generate all pending
        anyuse guide generate --app github.com   # Generate for specific app
        anyuse guide generate --force            # Generate even with few observations
    """
    from anyuse.guides.generator import GuideGenerator
    from anyuse.guides.storage import GuideStorage
    
    api_key = ctx.obj.get('api_key')
    storage = GuideStorage()
    generator = GuideGenerator(
        guide_storage=storage,
        api_key=api_key,
        min_observations=min_observations,
        verbose=not json_output,
    )
    
    if app:
        obs = generator.get_observations(app)
        if not obs:
            click.echo(f"No observations found for '{app}'.")
            return
        
        if not force and obs.total_observations < min_observations:
            click.echo(f"Not enough observations for '{app}' ({obs.total_observations}/{min_observations}).")
            click.echo("Use --force to generate anyway.")
            return
        
        click.echo(f"Generating guide for: {app}")
        guides = generator.generate_guides(force=True)
        guides = [g for g in guides if g.name.lower() == app.lower()]
    else:
        stats = generator.get_stats()
        click.echo(f"Apps observed: {stats['apps_observed']}")
        click.echo(f"Pending generation: {stats['pending_generation']}")
        
        guides = generator.generate_guides(force=force)
    
    if json_output:
        data = [g.to_dict() for g in guides]
        click.echo(json.dumps(data, indent=2))
        return
    
    if not guides:
        click.echo("\nNo guides generated.")
        if not force:
            click.echo(f"Guides need at least {min_observations} observations.")
        return
    
    click.echo(f"\n✓ Generated/updated {len(guides)} guide(s):")
    for g in guides:
        type_icon = "🌐" if g.is_website else "📱"
        click.echo(f"\n  {type_icon} {g.name}")
        click.echo(f"     {g.short_description[:60]}..." if len(g.short_description) > 60 else f"     {g.short_description}")


@guide.command('stats')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def guide_stats(json_output):
    """Show guide generation statistics."""
    from anyuse.guides.generator import GuideGenerator
    from anyuse.guides.storage import GuideStorage
    
    storage = GuideStorage()
    generator = GuideGenerator(guide_storage=storage)
    
    stats = generator.get_stats()
    observed = generator.list_observed_apps()
    
    if json_output:
        stats['observed_apps'] = [{'name': name, 'observations': count} for name, count in observed]
        click.echo(json.dumps(stats, indent=2))
        return
    
    click.echo("\n📊 Guide Generation Statistics")
    click.echo("=" * 50)
    
    click.echo(f"\n📈 Overview:")
    click.echo(f"   Apps/websites observed: {stats['apps_observed']}")
    click.echo(f"   Total observations: {stats['total_observations']}")
    click.echo(f"   Guides generated: {stats['guides_generated']}")
    click.echo(f"   Pending generation: {stats['pending_generation']}")
    
    if observed:
        click.echo(f"\n📋 Observed Apps (top 15):")
        for name, count in observed[:15]:
            guide = storage.get(name) or storage.get_by_app(name)
            status = "✅" if guide else "⏳" if count >= 3 else "📊"
            click.echo(f"   {status} {name}: {count} observations")


@guide.command('learn-from-session')
@click.argument('session_id')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.pass_context
def guide_learn_from_session(ctx, session_id, json_output):
    """Learn guide observations from a historical watcher session.

    Structured steps are not learned from yet."""
    from anyuse.watcher.storage import SessionStorage
    from anyuse.guides.generator import GuideGenerator
    from anyuse.guides.storage import GuideStorage
    
    session_storage = SessionStorage()
    session = session_storage.load(session_id)
    
    if not session:
        click.echo(f"Session '{session_id}' not found.", err=True)
        sys.exit(1)
    
    storage = GuideStorage()
    generator = GuideGenerator(
        guide_storage=storage,
        api_key=ctx.obj.get('api_key'),
        auto_generate=False,
        verbose=not json_output,
    )
    
    click.echo(f"📚 Learning from session: {session_id}")
    click.echo(f"   Events: {len(session.events)}")
    click.echo(f"   Apps used: {', '.join(session.apps_used)}")
    
    generator.learn_from_session(session)
    
    stats = generator.get_stats()
    
    if json_output:
        click.echo(json.dumps(stats, indent=2))
        return
    
    click.echo(f"\n✓ Observations recorded")
    click.echo(f"   Apps observed: {stats['apps_observed']}")
    click.echo(f"   Total observations: {stats['total_observations']}")


# ─────────────────────────────────────────────────────────
# Watcher Commands (Structured Step Recording)
# ─────────────────────────────────────────────────────────

@cli.group()
def watcher():
    """Record Mac app workflows through Accessibility steps.

    \b
    Record a workflow in a Mac app (Accessibility steps; no model, no screen frames):
        anyuse watcher start --steps            # do the task, then Ctrl+C
        anyuse workflow drafts                  # the recording, as SESSION/steps_<app>_N
        anyuse workflow from-draft SESSION/steps_textedit_1 --name new_note   # lists the candidates
        anyuse workflow from-draft SESSION/steps_textedit_1 --name new_note --param text=@cN
        ANYUSE_DRIVE_ENABLED=on anyuse workflow play new_note --param text="Hello"
    See docs/DESKTOP_TOOLS.md. Run desktop recording and replay in a macOS VM.

    """
    pass


@cli.group('watch', invoke_without_command=True)
@click.pass_context
def watch(ctx):
    """Watch pages for changes with compiled tools (docs/WATCH.md).

    \b
      anyuse watch add TOOL INPUT [--every 1d]   watch INPUT with a compiled tool
      anyuse watch run [--dry-run] [--explain]   replay due items, diff and log
      anyuse watch log                          the changelog
      anyuse watch install-hook --print         print the advisory agent hook
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _watch_store(store):
    from anyuse.execution.watch import WatchStore
    return WatchStore(store or None)


_store_opt = click.option('--store', 'store', default='', envvar='ANYUSE_WATCH_DIR',
                          help='The watch store directory (default: <data dir>/watch)')


@watch.command('add')
@click.argument('tool')
@click.argument('value', metavar='INPUT')
@click.option('--every', 'every', default='1d', show_default=True,
              help='How often the item is due (>= 1h: 1h, 6h, 1d, 1w)')
@click.option('--param', 'params', multiple=True, metavar='KEY=VALUE',
              help="A compiled web tool's other inputs, when it takes more than one (repeatable)")
@_store_opt
def watch_add(tool, value, every, params, store):
    """Watch INPUT with the compiled tool TOOL (a pinned copy is kept).

    \b
    TOOL is one of:
      a name from `anyuse workflow list` (a compiled web tool)
      the path to its compiled_api.json or compiled_demo.json
    INPUT is the tool's input (q for most tools).
    """
    bound = {}
    for p in params:
        if '=' not in p:
            raise click.ClickException(f"--param expects KEY=VALUE, got {p!r}")
        k, v = p.split('=', 1)
        bound[k.strip()] = v
    try:
        item = _watch_store(store).add(tool, value, every, params=bound)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    from anyuse.execution import watch as WA
    name = item.get('tool_name') or item['workflow']
    click.echo(f"watching {item['id']}: {item['site']} {name} {value!r} every {item['every_s'] // 3600} h")
    if WA.is_api_item(item):
        from anyuse.execution.api_permission import host_acknowledged
        for host in item.get('hosts') or [item['site']]:
            if not host_acknowledged(host):
                click.echo(f"Note: {host} is not acknowledged yet. Run `anyuse workflow acknowledge-host {host}` "
                           "first, or every run is refused.")
    if WA.is_api_item(item) and not item.get('needs_browser'):
        click.echo("Next: anyuse watch run")
    else:
        click.echo("Next: anyuse watch run --browser http://127.0.0.1:PORT "
                   "(a headless browser's DevTools endpoint, docs/WATCH.md)")


@watch.command('list')
@_store_opt
@click.option('--json', 'json_output', is_flag=True)
def watch_list(store, json_output):
    """The watched items and their last state."""
    from anyuse.execution import watch as WA
    items = _watch_store(store).items()
    if json_output:
        click.echo(json.dumps(items, indent=1, ensure_ascii=False))
        return
    for iid, it in sorted(items.items()):
        state = it.get('last_status') or 'never run'
        redo = "anyuse watch log says why" if WA.is_api_item(it) else "re-demonstrate"
        drift = f", drift since {it['drift_since']} ({redo})" if it.get('drift_since') else ''
        click.echo(f"{iid}  {it.get('tool_name') or it['workflow']}  every {it['every_s'] // 3600} h  "
                   f"last {it.get('last_run_at') or '-'} {state}{drift}")


@watch.command('remove')
@click.argument('item')
@_store_opt
def watch_remove(item, store):
    """Stop watching ITEM (its snapshots and changelog stay)."""
    if not _watch_store(store).remove(item):
        raise click.ClickException(f"no such item: {item}")
    click.echo(f"removed {item}")


@watch.command('run')
@click.option('--dry-run', is_flag=True, help='Replay and print the diff; write nothing to the store '
                                              '(the guard still books the contact)')
@click.option('--explain', is_flag=True, help='Off by default: a model says what changed, only for a non-empty diff')
@click.option('--explain-backend', type=click.Choice(['openjev', 'cerebras']), default='openjev', show_default=True,
              help='openjev: a self-hosted OpenJev server at ANYUSE_OPENJEV_URL; '
                   'cerebras: Cerebras through the model adapter (needs CEREBRAS_API_KEY)')
@click.option('--browser', 'browser', default='', envvar='ANYUSE_WATCH_BROWSER',
              help='CDP endpoint of the headless browser (http://127.0.0.1:PORT)')
@click.option('--item', 'only', multiple=True, help='Only this item (repeatable)')
@_store_opt
@click.option('--json', 'json_output', is_flag=True)
def watch_run(dry_run, explain, explain_backend, browser, only, store, json_output):
    """Replay every DUE item (>= 1 h apart), snapshot, diff against the last good snapshot, log.

    A compiled web tool replays its proved request and needs no browser; a browser tool
    (compiled_demo.json) needs --browser.
    """
    from anyuse.execution import watch as WA
    st = _watch_store(store)
    selected = [it for iid, it in st.items().items() if not only or iid in only]
    if not browser and any(not WA.is_api_item(it) or it.get('needs_browser') for it in selected):
        raise click.ClickException('--browser (or ANYUSE_WATCH_BROWSER) is required for a watched browser tool: '
                                   'the guarded browser endpoint')
    explainer = None
    if explain:
        try:
            explainer = WA.CerebrasExplainer() if explain_backend == 'cerebras' else WA.OpenJevExplainer()
        except ValueError as exc:
            raise click.ClickException(str(exc))
    from anyuse.execution import watch_live as WL
    browser_replay = {}
    api_replay = WA.make_api_replay(browser=browser)

    def replay(item, tool, cap):
        if WA.is_api_item(item):
            return api_replay(item, tool, cap)
        if 'run' not in browser_replay:
            browser_replay['run'] = WL.make_replay(browser, str(st.cache_dir), log=lambda m: click.echo(m, err=True))
        return browser_replay['run'](item, tool, cap)
    # the web tools' verdict store (the one the MCP route checks): a known wrong answer is marked, never silent
    from anyuse.execution.verdict_store import VerdictStore
    from anyuse.execution.web_tool_call import verdict_store_path
    rep = WA.run_watch(st, replay, dry_run=dry_run, explainer=explainer, only=only,
                       log=lambda m: click.echo(m, err=True), verdicts=VerdictStore(verdict_store_path()))
    if json_output:
        click.echo(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
        return
    for e in rep.get('entries') or []:
        click.echo(WA.render_entry(e).rstrip() + "\n")
    n_review = sum(1 for e in rep.get('entries') or [] if e.get('kind') == 'needs_review')
    n_wrong = sum(1 for e in rep.get('entries') or [] if (e.get('drift') or {}).get('status') == 'known_wrong')
    n_down = sum(1 for e in rep.get('entries') or [] if e.get('kind') == 'outage')
    click.echo(f"{'DRY RUN (nothing written) ' if dry_run else ''}{len(rep.get('entries') or [])} replayed, "
               f"{sum(1 for i in rep['items'] if i.get('skipped'))} skipped"
               f"{f', {n_review} NEED REVIEW (not accepted)' if n_review else ''}"
               f"{f', {n_down} SITE DID NOT ANSWER (not drift)' if n_down else ''}"
               f"{f', {n_wrong} KNOWN WRONG (not accepted)' if n_wrong else ''}; model calls: replay "
               f"{rep['model_calls']['replay']}, explain {(rep['model_calls'].get('explain') or {}).get('calls', 0)}")
    if rep.get('hard_stop'):
        raise click.ClickException(f"hard stop: {rep['hard_stop']}")


@watch.command('log')
@_store_opt
@click.option('--item', 'item', default='', help='Only this item')
@click.option('-n', 'n', type=int, default=20, show_default=True, help='The last N entries')
@click.option('--runs', is_flag=True, help='The run log (every replay, unchanged ones included)')
@click.option('--json', 'json_output', is_flag=True)
def watch_log(store, item, n, runs, json_output):
    """The changelog (changes, drift, baselines), newest last."""
    from anyuse.execution import watch as WA
    st = _watch_store(store)
    if runs:
        rows = []
        p = st.root / 'runs.jsonl'
        if p.exists():
            rows = [json.loads(ln) for ln in p.read_text(encoding='utf-8').splitlines() if ln.strip()]
        click.echo(json.dumps(rows[-n:], indent=1, ensure_ascii=False))
        return
    rows = [e for e in st.changelog() if not item or e.get('item') == item][-n:]
    if json_output:
        click.echo(json.dumps(rows, indent=1, ensure_ascii=False))
        return
    for e in rows:
        click.echo(WA.render_entry(e).rstrip() + "\n")


@watch.command('install-hook')
@click.option('--print', 'print_only', is_flag=True, help='Print the hook blocks (the only thing this does)')
def watch_install_hook(print_only):
    """The advisory agent hook for Claude Code / Codex - PRINTED, never installed."""
    from anyuse.execution import watch as WA
    if not print_only:
        raise click.ClickException("anyuse installs no hook: run `anyuse watch install-hook --print` and paste it")
    click.echo(WA.install_hook_text())


@watch.command('hook-run', hidden=True)
@_store_opt
def watch_hook_run(store):
    """The hook command itself: one advisory line from the local changelog; always exits 0."""
    try:
        from anyuse.execution import watch as WA
        out = WA.hook_output(_watch_store(store))
    except Exception:                                  # noqa: BLE001 - a hook never fails the agent
        out = ''
    if out:
        click.echo(out)


def _record_steps(duration: float, watcher=None):
    """``anyuse watcher start --steps``: a steps-only recording, then the next commands."""
    import asyncio
    # the structured step capture's gate (watcher/step_capture.py), for this process only
    os.environ["ANYUSE_WATCH_STRUCTURED_STEPS"] = "1"
    from anyuse.watcher.batch_watcher import BatchWatcher, BatchWatcherConfig
    watcher = watcher or BatchWatcher(BatchWatcherConfig(steps_only=True))
    holder = {}

    async def run():
        await watcher.start()
        click.echo("Recording. Do the task in the app, then press Ctrl+C."
                   + (f" (stops after {duration:g} s)" if duration > 0 else ""))
        try:
            if duration > 0:
                await asyncio.sleep(duration)
            else:
                while True:
                    await asyncio.sleep(3600)
        finally:
            holder["session"] = await watcher.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    session = holder.get("session")
    names = list(getattr(session, "structured_workflow_names", None) or []) if session else []
    if not session or not names:
        click.echo("Nothing was recorded. Check that your terminal has the Accessibility permission "
                   "(System Settings > Privacy & Security > Accessibility).")
        sys.exit(1)
    typed = any(e.workflow_name == names[0] and isinstance(e.data, dict) and e.data.get("value_ref")
                for e in getattr(session, "step_events", None) or [])
    click.echo("\nNext:")
    if not typed:
        click.echo(f"  anyuse workflow from-draft {session.id}/{names[0]} --name NAME"
                   "      # nothing was typed: a fixed routine, proved by one replay")
        click.echo("  ANYUSE_DRIVE_ENABLED=on anyuse workflow play NAME   # in a VM")
        return
    click.echo(f"  anyuse workflow from-draft {session.id}/{names[0]} --name NAME"
               "      # lists the values you can turn into --param")
    click.echo(f"  anyuse workflow from-draft {session.id}/{names[0]} --name NAME --param text=@cN"
               "   # cN = the candidate that holds what you typed (menu titles are candidates too)")
    click.echo("  ANYUSE_DRIVE_ENABLED=on anyuse workflow play NAME --param text=...   # in a VM")


@watcher.command('start')
@click.option('--steps', 'steps_only', is_flag=True,
              help='Compatibility flag: step recording is now the default.')
@click.option('--duration', type=float, default=0.0,
              help='Stop after this many seconds (default: until Ctrl+C)')
def watcher_start(steps_only, duration):
    """Record Mac app steps through the Accessibility tree.

    Records AX identity per action and keeps typed values in the local vault.
    Stop with Ctrl+C or --duration, then compile with `anyuse workflow from-draft`.
    Needs the Accessibility permission for your terminal.
    """
    _record_steps(duration)


@watcher.command('stop')
def watcher_stop():
    """Stop the current watching session."""
    click.echo("Note: If watcher is running in foreground, use Ctrl+C to stop it.")


@watcher.command('status')
def watcher_status():
    """Show current watcher status."""
    from anyuse.watcher.storage import SessionStorage
    
    storage = SessionStorage()
    stats = storage.get_storage_stats()
    recent = storage.list_sessions()[:5]
    
    click.echo("\n📊 Watcher Status")
    click.echo("-" * 40)
    click.echo(f"  Total sessions: {stats['session_count']}")
    click.echo(f"  Total anchors: {stats['anchor_count']}")
    click.echo(f"  Storage size: {stats['total_size_mb']} MB")
    
    if recent:
        click.echo("\n📋 Recent Sessions:")
        for sess in recent:
            status_icon = "🔴" if sess['status'] == 'recording' else "✓"
            click.echo(f"  {status_icon} {sess['id']}")
            click.echo(f"     Started: {sess['started_at']}")


@watcher.command('sessions')
@click.option('--limit', '-n', default=10, help='Number of sessions to show')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def watcher_sessions(limit, json_output):
    """List past watching sessions."""
    from anyuse.watcher.storage import SessionStorage
    
    storage = SessionStorage()
    sessions = storage.list_sessions()[:limit]
    
    if json_output:
        click.echo(json.dumps(sessions, indent=2))
        return
    
    if not sessions:
        click.echo("No watching sessions found.")
        click.echo("Start one with: anyuse watcher start")
        return
    
    click.echo(f"\n📋 Watching Sessions ({len(sessions)} shown):")
    click.echo("-" * 60)
    
    for sess in sessions:
        status_icon = {'recording': '🔴', 'stopped': '⏹', 'composed': '✅'}.get(sess['status'], '❓')
        click.echo(f"\n{status_icon} {sess['id']}")
        click.echo(f"   {sess['started_at']}")
        click.echo(f"   Events: {sess['events_count']} | Clicks: {sess['clicks_count']}")


@watcher.command('delete')
@click.argument('session_id')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
def watcher_delete(session_id, force):
    """Delete a watching session and its anchors."""
    from anyuse.watcher.storage import SessionStorage
    
    storage = SessionStorage()
    
    if not force:
        session = storage.load(session_id)
        if session:
            click.echo(f"Session: {session_id}")
            click.echo(f"  Events: {len(session.events)}")
        
        if not click.confirm("Delete this session?"):
            click.echo("Cancelled.")
            return
    
    if storage.delete(session_id):
        click.echo(f"✓ Session {session_id} deleted")
    else:
        click.echo(f"Session {session_id} not found.")


@watcher.command('cleanup')
@click.option('--days', '-d', default=30, help='Delete sessions older than N days')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
def watcher_cleanup(days, force):
    """Clean up old watching sessions."""
    from anyuse.watcher.storage import SessionStorage
    
    storage = SessionStorage()
    stats_before = storage.get_storage_stats()
    
    click.echo(f"Current storage: {stats_before['session_count']} sessions, {stats_before['total_size_mb']} MB")
    
    if not force:
        if not click.confirm(f"Delete sessions older than {days} days?"):
            click.echo("Cancelled.")
            return
    
    storage.cleanup_old_sessions(max_age_days=days)
    
    stats_after = storage.get_storage_stats()
    deleted = stats_before['session_count'] - stats_after['session_count']
    freed = stats_before['total_size_mb'] - stats_after['total_size_mb']
    
    click.echo(f"✓ Cleaned up {deleted} session(s), freed {freed:.2f} MB")


# ─────────────────────────────────────────────────────────
# Context Commands
# ─────────────────────────────────────────────────────────

def _learn_allow_url(robots_policy):
    """The robots gate for the learn loop's browser: consulted before every
    top-level navigation the agent or the navigator causes. ``respect`` is
    the default and the only setting our own traffic ever runs under."""
    import urllib.parse as _up

    from anyuse.execution.api_permission import (ROBOTS_IGNORE, host_override,
                                                 robots_allows)
    from anyuse.execution.api_permission import robots_policy as _policy

    def allow(url: str) -> bool:
        if _policy(robots_policy) == ROBOTS_IGNORE:
            return True
        allowed, _note = robots_allows(url)
        if allowed is False and not host_override(_up.urlsplit(url).netloc):
            return False
        return True

    return allow


def _print_candidate(cand) -> None:
    """The preview a person confirms. The same fields `--json` and MCP carry."""
    s = cand.summary()
    click.echo("Candidate — the page the agent reached ONCE:")
    click.echo(f"  URL:    {s['final_url'][:140]}")
    click.echo(f"  title:  {s['title'][:120]!r}")
    click.echo(f"  value:  {s['value']!r} — bound as {s['value_bound_as']}")
    if s["url_template"]:
        click.echo(f"  pattern: {s['url_template'][:140]}")
    if s["preview"]:
        click.echo(f"  preview ({s['preview_note']}):")
        for item in s["preview"]:
            click.echo(f"     - {str(item)[:100]}")
    else:
        click.echo(f"  preview: {s['preview_note']}")
    if s["blocked"]:
        click.echo(f"  BLOCKED: {s['blocked']}")
    if s["error"]:
        click.echo(f"  ERROR: {s['error']}")
    click.echo(f"  cost so far: {s['model_calls']} model call(s), ${s['cost_usd']:.4f}, "
               f"{s['agent_seconds']:.1f}s")


def _print_learn_result(result, dry_run: bool) -> None:
    for run in result.runs:
        kind = "agent" if run.get("model_calls") else "navigate"
        made = " (generated)" if run.get("generated") else ""
        click.echo(f"  {kind:8s} {run['value']!r}{made}: {run.get('model_calls', 0)} model call(s), "
                   f"{run['seconds']:.1f}s -> {run['final_url'][:76]}")
    for bad in result.discarded:
        click.echo(f"  DISCARDED {bad['value']!r}: {bad['why'][:140]}")
    if result.generated:
        where = {"result_page": "from the first result page", "model": "from the model"}
        click.echo("Generated values (fetched and proved like yours): "
                   + ", ".join(f"{g['value']!r} ({where.get(g['from'], g['from'])})"
                               for g in result.generated))
    if result.generate_note:
        click.echo(f"Generating values: {result.generate_note}")
    click.echo()
    if result.ground_truth_selector:
        click.echo(f"Ground truth (derived across the values): {result.ground_truth_selector!r}")
        click.echo(f"  first items: {result.ground_truth_sample[:3]}")
        if result.chrome_filtered:
            click.echo(f"  page chrome filtered out: {result.chrome_filtered[:4]}")
    import textwrap
    click.echo("\n" + textwrap.fill(f"Outcome: {result.outcome} — {result.reason}", width=100,
                                    subsequent_indent="  ", break_long_words=False))
    if result.learned_url:
        click.echo(f"Learned from: {result.learned_url[:110]}  (confirmed by {result.confirmed_by})")
    click.echo(f"Cost of learning: {result.agent_runs} agent run(s), "
               f"{result.navigations} substituted navigation(s), "
               f"{result.model_calls} model call(s), ${result.cost_usd:.4f}, "
               f"{result.learn_seconds:.0f}s")
    if result.first_run_ms:
        later = (f"{result.later_run_ms:.0f} ms, {result.later_run_model_calls} model calls"
                 if result.later_run_ms else "n/a")
        click.echo(f"First run (agent): {result.first_run_ms:.0f} ms  ->  "
                   f"later runs ({result.outcome}): {later}")
    if result.persisted:
        click.echo(f"✓ Persisted: {result.persisted}")
        if result.outcome == "tier2":
            click.echo("  Served by the browser replay (no compiled request): each call needs a "
                       "browser (ANYUSE_CDP_ENDPOINT or ANYUSE_LEARN_ENDPOINT).")
    elif not dry_run and result.outcome in ("tier1", "tier2_nonpersistent"):
        click.echo("✗ Proved but NOT persisted — see the reason above.")
    elif dry_run:
        click.echo("(--dry-run: nothing written)")


@cli.command()
@click.argument('task')
@click.option('--url', required=True, help='Where the agent starts (the site front page)')
@click.option('--values', required=True,
              help='Comma-separated inputs: the first is what the agent looks for, the '
                   'rest are substituted. Four values reach "proved" (one picks the '
                   'request, two are held out); with fewer, learn proposes the missing '
                   'ones (see --no-generate)')
@click.option('--param', default='q', show_default=True,
              help='The parameter name the {placeholder} in TASK binds to')
@click.option('--detail', 'cls', flag_value='detail', default=False,
              help='The task opens ONE item and reads its fields (default: a list)')
@click.option('--endpoint', default='',
              help='DevTools endpoint of the container browser '
                   '(default: $ANYUSE_LEARN_ENDPOINT). A loopback address is refused.')
@click.option('--cdp', default='',
              help='DevTools endpoint for the tier-2 fall-back (default: the same one)')
@click.option('--confirm', 'ask', is_flag=True,
              help='Show the candidate page and ask y/n before anything is learned')
@click.option('--confirm-url', default='',
              help='Non-interactive confirmation: the candidate URL, restated exactly')
@click.option('--budget-usd', type=float, default=0.25, show_default=True,
              help='Stop the agent when learning has cost this much')
@click.option('--dry-run', is_flag=True,
              help='Decide and report, persist nothing')
@click.option('--save-recordings', default='',
              help='Also write the recordings to this directory')
@click.option('--robots-policy', default=None,
              type=click.Choice(['respect', 'per-host', 'ignore']),
              help='Overrides the stored setting for THIS run')
@click.option('--no-generate', is_flag=True,
              help='Do not propose missing values: use only --values (three give "checked")')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@_asks_new_hosts
def learn(task, url, values, param, cls, endpoint, cdp, ask, confirm_url, budget_usd,
          dry_run, save_recordings, robots_policy, no_generate, json_output):
    """The agent finds the page once; you confirm it once; AnyUse proves it.

    \b
      anyuse learn "search this site for {q}" \\
          --url https://crates.io --values serde,regex,tokio,clap --confirm

    The agent runs the task ONCE, for the first value, in a headless browser
    inside a container, and is recorded. You are shown the candidate — the
    URL, the title, a preview of the list, where the value sits in the URL —
    and asked for ONE confirmation. Without a confirmation nothing is learned
    and nothing is persisted: the candidate is kept under
    $ANYUSE_DATA_DIR/learn_candidates/ so `anyuse learn-confirm <id>` can
    continue later without running the agent again.

    After confirmation the other values are bound into the confirmed URL and
    simply opened (no model call); the agent is used only where the value is
    not in the URL. Every further page must match the confirmed page's URL
    pattern and title — never merely mention the value. Then the ordinary
    path runs unchanged: ground truth across the values, the chrome filter,
    the compiler, the fresh-input proof. Persisting still needs the proof AND
    an acknowledged host: AnyUse asks before the compiled request is replayed
    against a new host (--yes answers for scripts).

    With fewer than four values, learn proposes the missing ones: other
    results on the first value's page, else one call to the configured model.
    They are only ever selection or held-out values, fetched and proved like
    yours, and listed in the output and in the tool's proof. --no-generate
    turns this off. A page only the browser replay could prove is saved as a
    tool served by that replay; each of its calls needs a browser. It never
    replaces a proved compiled request already saved under the same name.

    This command drives a browser in a container and only there. It refuses a
    loopback endpoint and a browser with pages open.
    """
    from anyuse.execution.attach import AttachError, require_cdp_client
    try:
        require_cdp_client()
    except AttachError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)
    from anyuse.execution.agent_browser import (Navigator, make_agent,
                                                NotAContainerError,
                                                assert_container_endpoint,
                                                learn_endpoint)
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.cdp_replay import ReplayExecutor
    from anyuse.execution.learn_loop import (continue_learning,
                                             explicit_confirmation,
                                             make_persister, propose,
                                             save_candidate)

    ep = learn_endpoint(endpoint)
    try:
        info = assert_container_endpoint(ep)
    except NotAContainerError as exc:
        click.echo(f"✗ {exc}")
        raise SystemExit(2)
    except Exception as exc:
        click.echo(f"✗ could not reach {ep}: {type(exc).__name__}: {exc}")
        raise SystemExit(2)

    vals = [v.strip() for v in values.split(',') if v.strip()]
    if not vals or (no_generate and len(vals) < 3):
        click.echo("✗ give at least three values: one to confirm, two to prove on"
                   if no_generate else "✗ give at least one value: the one the agent looks for")
        raise SystemExit(2)
    allow = _learn_allow_url(robots_policy)
    agent = make_agent(ep, allow_url=allow)   # escalation iff ANYUSE_AGENT_ESCALATION=on
    navigator = Navigator(ep, allow_url=allow)
    replay = ReplayExecutor(endpoint=(cdp or ep))
    persist = None if dry_run else make_persister(save_recordings, robots_policy)

    click.echo(f"Browser: {info.get('Browser')} at {ep}")
    click.echo(f"Values: {', '.join(vals)}  (budget ${budget_usd:.2f})\n")

    cand = propose(task, url, vals[0], param=param, cls=(cls or 'list'), agent=agent)
    if not json_output:
        _print_candidate(cand)
    if not cand.ok:
        if json_output:
            click.echo(json.dumps({"candidate": cand.summary(), "outcome": "nothing"},
                                  indent=2, default=str))
        else:
            click.echo("\nOutcome: nothing — the agent's one run produced no usable candidate.")
        raise SystemExit(1)

    confirmed_by = ""
    if confirm_url:
        ok, why = explicit_confirmation(cand, confirm_url)
        if not ok:
            click.echo(f"\n✗ not confirmed: {why}")
            raise SystemExit(1)
        confirmed_by = "cli --confirm-url"
    elif ask:
        if click.confirm("\nIs this the page you meant, with the list you meant?",
                         default=False):
            confirmed_by = "cli prompt"
    if not confirmed_by:
        path = save_candidate(cand)
        if json_output:
            click.echo(json.dumps({"candidate": cand.summary(), "outcome": "unconfirmed",
                                   "saved": path}, indent=2, default=str))
        else:
            click.echo("\nOutcome: unconfirmed — nothing learned, nothing persisted.")
            click.echo(f"Candidate kept at {path}")
            click.echo(f"To continue: anyuse learn-confirm {cand.candidate_id} "
                       + (f"--values {','.join(vals[1:])} " if vals[1:] else "")
                       + f"--url {cand.final_url}")
        return

    result = continue_learning(cand, vals[1:], agent=agent, navigator=navigator,
                               executor=ApiStepExecutor(), replay=replay,
                               persist=persist, confirmed_by=confirmed_by,
                               budget_usd=budget_usd, generate=not no_generate)
    if save_recordings and dry_run:
        make_persister(save_recordings, robots_policy)(None, None, [], result)
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, default=str))
        return
    click.echo()
    _print_learn_result(result, dry_run)


@cli.command('learn-confirm')
@click.argument('candidate_id')
@click.option('--values', default='',
              help='Comma-separated further inputs to substitute: three reach "proved" '
                   '(one picks the request, two are held out); with fewer, the missing '
                   'ones are proposed (see --no-generate)')
@click.option('--url', 'confirm_url', default='',
              help='Non-interactive confirmation: the candidate URL, restated exactly')
@click.option('--endpoint', default='',
              help='DevTools endpoint of the container browser '
                   '(default: $ANYUSE_LEARN_ENDPOINT). A loopback address is refused.')
@click.option('--cdp', default='', help='DevTools endpoint for the tier-2 fall-back')
@click.option('--budget-usd', type=float, default=0.25, show_default=True)
@click.option('--dry-run', is_flag=True, help='Decide and report, persist nothing')
@click.option('--save-recordings', default='')
@click.option('--robots-policy', default=None,
              type=click.Choice(['respect', 'per-host', 'ignore']))
@click.option('--no-generate', is_flag=True,
              help='Do not propose missing values: use only --values (two give "checked")')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@_asks_new_hosts
def learn_confirm(candidate_id, values, confirm_url, endpoint, cdp, budget_usd, dry_run,
                  save_recordings, robots_policy, no_generate, json_output):
    """Confirm a candidate `anyuse learn` proposed, then substitute, prove, persist.

    Shows the SAME preview again. The confirmation is either the y/n prompt or
    `--url` restating the candidate's URL exactly; a confirmation that names
    nothing is not accepted.
    """
    from anyuse.execution.agent_browser import (Navigator, make_agent,
                                                NotAContainerError,
                                                assert_container_endpoint,
                                                learn_endpoint)
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.cdp_replay import ReplayExecutor
    from anyuse.execution.learn_loop import (continue_learning, discard_candidate,
                                             explicit_confirmation, load_candidate,
                                             make_persister)

    cand = load_candidate(candidate_id)
    if cand is None:
        click.echo(f"✗ no candidate {candidate_id!r} under learn_candidates/")
        raise SystemExit(2)
    if not json_output:
        _print_candidate(cand)
    vals = [v.strip() for v in values.split(',') if v.strip()]
    vals = [v for v in vals if v != cand.value]
    if no_generate and len(vals) < 2:
        click.echo("✗ give at least two further values the compiler has not seen")
        raise SystemExit(2)

    confirmed_by = ""
    if confirm_url:
        ok, why = explicit_confirmation(cand, confirm_url)
        if not ok:
            click.echo(f"\n✗ not confirmed: {why}")
            raise SystemExit(1)
        confirmed_by = "cli learn-confirm --url"
    elif click.confirm("\nIs this the page you meant, with the list you meant?",
                       default=False):
        confirmed_by = "cli learn-confirm prompt"
    if not confirmed_by:
        click.echo("\nOutcome: unconfirmed — nothing learned, nothing persisted; "
                   "the candidate is kept.")
        return

    ep = learn_endpoint(endpoint)
    try:
        assert_container_endpoint(ep)
    except NotAContainerError as exc:
        click.echo(f"✗ {exc}")
        raise SystemExit(2)
    except Exception as exc:
        click.echo(f"✗ could not reach {ep}: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
    allow = _learn_allow_url(robots_policy)
    agent = make_agent(ep, allow_url=allow)   # escalation iff ANYUSE_AGENT_ESCALATION=on
    navigator = Navigator(ep, allow_url=allow)
    replay = ReplayExecutor(endpoint=(cdp or ep))
    persist = None if dry_run else make_persister(save_recordings, robots_policy)
    result = continue_learning(cand, vals, agent=agent, navigator=navigator,
                               executor=ApiStepExecutor(), replay=replay,
                               persist=persist, confirmed_by=confirmed_by,
                               budget_usd=budget_usd, generate=not no_generate)
    if result.persisted:
        discard_candidate(candidate_id)
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, default=str))
        return
    click.echo()
    _print_learn_result(result, dry_run)


@cli.command()
@click.pass_context
def context(ctx):
    """Show current macOS context."""
    from anyuse.context.macos import MacOSContext
    
    macos_ctx = MacOSContext()
    data = macos_ctx.get_full_context()
    
    if ctx.obj.get('json_output'):
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        click.echo("\nCurrent macOS Context:")
        click.echo(f"  Screen: {data['screen_info']['width']}x{data['screen_info']['height']}")
        click.echo(f"  Active App: {data['frontmost_app']}")
        click.echo(f"  Running Apps: {len(data['running_apps'])}")
        
        if data['running_apps']:
            click.echo("    " + ", ".join(data['running_apps'][:10]))


def _desktop_profile() -> dict:
    """The active desktop AX-matcher / drift-gate profile, read from the SAME
    resolvers the desktop code itself calls — never re-derived from env vars
    here, so this can never drift from what a replay actually does.

    The tuned combination is: weighted matcher, weights fitted on
    testdata/ax/ax_weights_corpus2.json, drift tolerance 0.02 and the menu bar
    excluded from anchors. Every one of those is opt-in; this only reports what
    is actually active.
    """
    from anyuse.context import accessibility as _acc
    from anyuse.context import element_scorer as _es
    from anyuse.execution import drift_gate as _dg

    matcher = _es.matcher_mode()
    weights_path = os.environ.get("ANYUSE_ELEMENT_WEIGHTS", "").strip()
    active_weights = _es.ax_weights()
    weights_is_builtin = active_weights == _es.AX_FITTED_WEIGHTS
    if not weights_path:
        weights_source = "built-in AX_FITTED_WEIGHTS"
    elif weights_is_builtin:
        weights_source = (f"built-in AX_FITTED_WEIGHTS (ANYUSE_ELEMENT_WEIGHTS="
                          f"{weights_path!r} was set but unreadable — fell back)")
    else:
        weights_source = f"file: {weights_path}"

    tolerance = _dg._env_tolerance()
    exclude_menubar = _acc.drift_excludes_menubar()

    # Is the active weight vector the tuned one (testdata/ax/ax_weights_corpus2.json)?
    # Compared by VALUE, not by path, so a
    # copy of the same file elsewhere still counts.
    is_corpus2_weights = False
    corpus2_path = Path(__file__).resolve().parent / "testdata" / "ax" / "ax_weights_corpus2.json"
    if corpus2_path.exists():
        try:
            corpus2 = json.loads(corpus2_path.read_text())
            is_corpus2_weights = active_weights == {
                k: float(corpus2.get(k, 0.0)) for k in active_weights
            }
        except Exception:
            is_corpus2_weights = False

    is_r2 = (matcher == "weighted" and is_corpus2_weights
             and abs(tolerance - 0.02) < 1e-9 and exclude_menubar)

    return {
        "matcher": matcher,
        "weights_source": weights_source,
        "drift_missing_tolerance": tolerance,
        "drift_excludes_menubar": exclude_menubar,
        "is_documented_r2_profile": is_r2,
    }


@cli.command()
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--install', 'install_only', is_flag=True,
              help='Only the MCP server install self-check: Python, imports, data dir, MCP entry, '
                   'settings (docs/INSTALL_FOR_AGENTS.md)')
def doctor(json_output, install_only):
    """Check that the compile path can actually work on this machine.

    Read-only and desktop-free: it reads files and asks the runtime what it has.
    It never drives the screen, never opens a browser and never contacts a site.
    Every check says what it found, why it matters, and what to do about it.

    With --install it runs only the five install checks in anyuse/selfcheck.py,
    each PASS or FAIL, and exits 1 if any fails.
    """
    if install_only:
        from anyuse import selfcheck as _selfcheck
        rows = _selfcheck.install_checks()
        if json_output:
            click.echo(json.dumps({"checks": rows}, indent=2))
        else:
            click.echo(_selfcheck.render(rows))
        if not all(row["ok"] for row in rows):
            raise SystemExit(1)
        return

    import shutil as _shutil

    checks = []

    def add(name, ok, found, why, fix="", needed_for=""):
        # needed_for: an optional capability; a miss is reported but does not fail compile + replay
        checks.append({"check": name, "ok": bool(ok), "found": found,
                       "why": why, "fix": fix, "optional": bool(needed_for), "needed_for": needed_for})

    # --- where data lives ---------------------------------------------------
    from anyuse import paths as _paths
    info = _paths.describe()
    add("data directory", True, info["data_dir"],
        "where compiled workflows, guides and the host registries are kept",
        "" if info["data_dir_exists"] else
        "it does not exist yet; it is created on first write")
    add("data dir overridden", True,
        "yes (ANYUSE_DATA_DIR)" if info["data_dir_overridden"] else "no (default)",
        "a redirected data dir is what makes a test run safe")
    add("secrets directory", True, info["secret_dir"],
        "key material does NOT follow ANYUSE_DATA_DIR, on purpose — "
        "see anyuse/paths.py")

    # --- the container runtime the recorder needs ---------------------------
    # Apple container on macOS; Docker or Podman on Linux
    import platform as _platform
    if _platform.system() == "Darwin":
        apple_ctr = (_shutil.which("container") or
                     ("/opt/homebrew/bin/container" if _os_path_exists("/opt/homebrew/bin/container")
                      else None))
        have_ctr = bool(apple_ctr and _os_path_exists(apple_ctr))
        if have_ctr:
            found = apple_ctr
        else:
            other_ctr = _shutil.which("docker") or _shutil.which("podman")
            found = (f"{other_ctr} (Apple's `container` CLI not found; "
                     "Docker/Podman on macOS only expose loopback)" if other_ctr else "not found")
    else:
        ctr = _shutil.which("container") or _shutil.which("docker") or _shutil.which("podman")
        have_ctr = _os_path_exists(ctr)
        found = ctr if have_ctr else "not found"
    add("container runtime", have_ctr, found,
        "recording a public workflow happens in a headless browser INSIDE a "
        "container, so nothing touches your desktop or your cookies",
        "" if have_ctr else (
            "install Apple's `container` (brew install container; Apple silicon, macOS 26), "
            "or record with your own browser instead (docs/QUICKSTART_COMPILE.md, docs/ATTACH.md)"
            if _platform.system() == "Darwin" else
            "use a Docker-compatible runtime (docs/QUICKSTART_COMPILE.md), or record with your own "
            "browser instead (docs/ATTACH.md)"),
        needed_for="recording a site with `anyuse learn`")

    # --- HTTP transport -----------------------------------------------------
    try:
        import httpx  # noqa: F401
        add("httpx", True, "importable",
            "the compiled rung replays over httpx, IPv4-forced, cookies off")
    except Exception as exc:
        add("httpx", False, f"missing ({exc})",
            "without it a compiled rung cannot be replayed",
            "python -m pip install -e . (from the AnyUse checkout)")

    # --- one model adapter, four independently overridable roles ----------
    from anyuse.execution.model_adapter import CLI_PROVIDERS, credential_status, resolve_model_config
    _model_needed = "agent tasks (`anyuse \"task\"`, `anyuse learn`); compiled replays need no model"
    _no_key_fix = ("no key needed: export ANYUSE_MODEL_PROVIDER=claude-cli (or codex-cli) to use the CLI "
                   "you are logged into; any OpenAI-compatible endpoint works too (docs/MODEL_CONFIGURATION.md)")
    for _role in ("planner", "visual", "agent", "judge"):
        try:
            _model_cfg = resolve_model_config(_role)
            if _model_cfg.provider in CLI_PROVIDERS:
                _binary = "claude" if _model_cfg.provider == "claude-cli" else "codex"
                _on_path = _shutil.which(_binary) is not None
                # codex-cli runs with --ignore-user-config, so it needs the model named here
                _named = bool(_model_cfg.model) or _model_cfg.provider != "codex-cli"
                _ready = _on_path and _named
                _found = (f"provider={_model_cfg.provider} model={_model_cfg.model or 'not set'} "
                          f"{_binary}={'found' if _on_path else 'not on PATH'}")
                _fix = ("" if _ready else
                        f"install and log in to the `{_binary}` CLI, or pick another provider" if not _on_path else
                        "set ANYUSE_MODEL_NAME to a model your Codex account offers (codex-cli does not read "
                        "your Codex config's default)")
            else:
                _credential = credential_status(_model_cfg)
                _ready = _credential != "unset"
                _found = (f"provider={_model_cfg.provider} model={_model_cfg.model} "
                          f"credential={_credential}")
                _fix = "" if _ready else f"{_no_key_fix}; or set {_model_cfg.api_key_env}"
            add(f"model role {_role}", _ready, _found,
                ("resolved model configuration; credential values are never printed or "
                 "retained by doctor"), _fix, needed_for=_model_needed)
        except Exception as exc:
            add(f"model role {_role}", False, f"invalid ({exc})",
                "each model role must resolve to a valid adapter configuration",
                "check docs/MODEL_CONFIGURATION.md", needed_for=_model_needed)

    # --- the global spend cap (read-only; the ledger is never created here) --
    from anyuse.execution import spend as _spend
    _spend_ok, _spend_found = _spend.doctor_line()
    add("spend cap", _spend_ok, _spend_found,
        "every model request of the anyuse CLI or the MCP server is checked "
        "against one daily cap and booked in the spend ledger",
        "" if _spend_ok else "fix or move the ledger file, or correct ANYUSE_SPEND_DAILY_USD; "
        "`anyuse settings spend` shows the details")

    # --- the api modality switch -------------------------------------------
    from anyuse.execution.api_step import api_modality_enabled
    on = api_modality_enabled()
    add("api modality", on, "on" if on else "off (ANYUSE_API_MODALITY=off)",
        "when off, no compiled rung is ever preferred and replay always uses "
        "the recorded UI path",
        "" if on else "unset ANYUSE_API_MODALITY to re-enable")

    # --- host acknowledgements ---------------------------------------------
    from anyuse.execution.api_permission import hosts_file, load_hosts
    hosts = load_hosts()
    add("acknowledged hosts", True,
        f"{len(hosts)} in {hosts_file()}",
        "a compiled rung is reported either way, but only becomes the PREFERRED "
        "implementation for a host you have acknowledged",
        "" if hosts else "anyuse workflow acknowledge-host <host> --note '...'")

    # --- blocked-host registry ---------------------------------------------
    from anyuse.execution.blocked import blocked_hosts_file
    bpath = blocked_hosts_file()
    blocked_n = 0
    if _os_path_exists(bpath):
        try:
            with open(bpath) as fh:
                blocked_n = len(json.load(fh) or {})
        except Exception:
            blocked_n = -1
    add("blocked hosts", True,
        f"{blocked_n if blocked_n >= 0 else 'unreadable'} in {bpath}",
        "hosts that answered with a challenge; they are handed to a human and "
        "never auto-retried")

    # --- browser-only hosts -------------------------------------------------
    from anyuse.execution.blocked import browser_only_hosts, browser_only_hosts_file
    try:
        bo = browser_only_hosts()
    except Exception:
        bo = {}
    add("browser-only hosts", True,
        (", ".join(sorted(bo)[:4]) + (f" (+{len(bo) - 4} more)" if len(bo) > 4 else "")
         if bo else f"none in {browser_only_hosts_file()}"),
        "hosts that serve the container browser and refuse AnyUse's named HTTP "
        "client. The compiled-request rung is never attempted there; the "
        "deterministic browser replay rung is. AnyUse does not change its "
        "User-Agent to look like a browser, and this is what that costs",
        "`anyuse workflow clear-browser-only <host>` if you believe it changed"
        if bo else "")

    # --- the learn loop -----------------------------------------------------
    from anyuse.execution.agent_browser import ENDPOINT_ENV as _LEARN_ENV
    from anyuse.execution.agent_browser import learn_endpoint, validate_learn_endpoint_url
    ep = learn_endpoint()
    problem = validate_learn_endpoint_url(ep)
    if not ep:
        found = f"{_LEARN_ENV} is unset"
        fix = (f"start a headless browser in a container and set {_LEARN_ENV} to its "
               "DevTools endpoint (a loopback address is refused)")
    elif problem == "loopback":
        found = f"{ep} (refused: loopback address)"
        fix = (f"point {_LEARN_ENV} at the container's own IP, not loopback "
               "(see docs/QUICKSTART_COMPILE.md)")
    elif problem:
        found = f"{ep} ({problem})"
        fix = f"set {_LEARN_ENV} to a valid http://<container-ip>:9222 DevTools URL"
    else:
        found = f"{ep} (syntax ok; reachability not checked offline)"
        fix = ""
    add("learn loop", problem is None, found,
        "`anyuse learn` has a step agent find the page ONCE in a CONTAINER "
        "browser, shows you the candidate, and learns only what you confirm. Over "
        "MCP the same two halves exist as propose_learn_candidate (persists "
        "nothing) and confirm_learn_candidate (must restate the URL). Both refuse "
        "a loopback endpoint and a browser with pages open; neither drives the "
        "desktop",
        fix,
        needed_for="`anyuse learn`")
    # --- robots cache -------------------------------------------------------
    from anyuse.execution import api_permission as _ap
    add("robots cache", True, f"{len(_ap._ROBOTS_CACHE)} host(s) this process",
        "robots.txt is fetched once per host per process, before a rung is "
        "preferred")

    # --- compiled workflows on disk ----------------------------------------
    from anyuse.paths import anyuse_data_path
    wf_dir = anyuse_data_path("workflows")
    compiled = list(wf_dir.glob("*/compiled_api.json")) if wf_dir.exists() else []
    add("compiled workflows", True, f"{len(compiled)} found under {wf_dir}",
        "each carries its own proof; `anyuse workflow list` shows them")

    # --- desktop AX matcher / drift-gate profile (macOS only) ----------------
    if _platform.system() == "Darwin":
        profile = _desktop_profile()
        add("desktop profile", True,
            (f"matcher={profile['matcher']}, weights={profile['weights_source']}, "
             f"drift_missing_tolerance={profile['drift_missing_tolerance']}, "
             f"drift_excludes_menubar={profile['drift_excludes_menubar']}, "
             f"tuned combination={'yes' if profile['is_documented_r2_profile'] else 'no'}"),
            "the AX element matcher and drift gate settings actually in effect for "
            "this process (the tuned combination is opt-in, not a default)",
            "" if profile["is_documented_r2_profile"] else
            "for the tuned combination set ANYUSE_ELEMENT_MATCHER=weighted, "
            "ANYUSE_ELEMENT_WEIGHTS=<repo>/testdata/ax/ax_weights_corpus2.json, "
            "ANYUSE_DRIFT_MISSING_TOLERANCE=0.02 and ANYUSE_DRIFT_EXCLUDE_MENUBAR=1")

    # --- attach and learn talk to a browser over DevTools --------------------
    import importlib.util as _ilu
    _ws = _ilu.find_spec("websockets") is not None
    add("browser client (cdp extra)", _ws, "importable" if _ws else "not installed",
        "attach and learn read a browser over the DevTools protocol",
        "" if _ws else "pip install -e '.[cdp]'", needed_for="attach and learn")

    # --- desktop tasks (macOS): read-only, never raises a permission prompt ------
    if _platform.system() == "Darwin":
        desk = _desktop_probe()
        needed = "desktop tasks (recording and replaying Mac apps; run them in a macOS VM)"
        state = {True: "granted", False: "not granted", None: "unknown (pyobjc missing)"}
        if os.environ.get("SSH_CONNECTION"):
            # grants belong to the GUI app that starts anyuse (Terminal), never to an SSH login
            state[False] = ("not granted to this SSH session (macOS grants belong to the app on the "
                            "screen; run anyuse doctor in that Mac's own Terminal)")
        add("pyobjc", desk["pyobjc"], "importable" if desk["pyobjc"] else "not installed",
            "macOS control and the Accessibility tree go through pyobjc",
            "" if desk["pyobjc"] else "pip install -e '.[desktop]'", needed_for=needed)
        add("accessibility permission", desk["accessibility"] is True, state[desk["accessibility"]],
            "recording and replay read and press controls through the Accessibility tree",
            "" if desk["accessibility"] is True else
            "System Settings > Privacy & Security > Accessibility: allow the terminal you run anyuse in",
            needed_for=needed)
        add("screen recording permission", desk["screen_recording"] is True, state[desk["screen_recording"]],
            "computer use and its goal check look at the screen",
            "" if desk["screen_recording"] is True else
            "System Settings > Privacy & Security > Screen & System Audio Recording: allow that terminal",
            needed_for=needed)
        from anyuse.execution.actions import drive_enabled as _drive_enabled
        drive_on = _drive_enabled()
        add("desktop input", drive_on,
            "on (ANYUSE_DRIVE_ENABLED)" if drive_on else "off (ANYUSE_DRIVE_ENABLED)",
            "a desktop replay or task sends real clicks and keys only when this is on",
            "" if drive_on else "set ANYUSE_DRIVE_ENABLED=on for a desktop replay, in a VM", needed_for=needed)

    bad = [c for c in checks if not c["ok"] and not c["optional"]]
    if json_output:
        click.echo(json.dumps({"checks": checks}, indent=2))
        if bad:
            raise SystemExit(1)
        return

    optional_missing = [c for c in checks if not c["ok"] and c["optional"]]
    click.echo("anyuse doctor — the compile path\n")
    for c in checks:
        mark = "ok  " if c["ok"] else ("opt " if c["optional"] else "FAIL")
        click.echo(f"[{mark}] {c['check']}: {c['found']}")
        click.echo(f"        {c['why']}")
        if not c["ok"] and c["optional"]:
            click.echo(f"        optional: only needed for {c['needed_for']}")
        if c["fix"]:
            click.echo(f"        -> {c['fix']}")
    click.echo()
    if bad:
        click.echo(f"{len(bad)} check(s) need attention. "
                   "Nothing here was fixed for you.")
        raise SystemExit(1)
    click.echo("Compile and replay are ready. Nothing on your desktop was touched.")
    if optional_missing:
        click.echo(f"{len(optional_missing)} optional check(s) apply only to recording or agent tasks "
                   "(marked opt above).")


def _desktop_probe() -> dict:
    """Read-only desktop readiness: pyobjc importable, Accessibility trusted, Screen Recording
    preflight. AXIsProcessTrusted and CGPreflightScreenCaptureAccess never show a prompt."""
    out = {"pyobjc": False, "accessibility": None, "screen_recording": None}
    try:
        import ApplicationServices as _AS
        out["pyobjc"] = True
        out["accessibility"] = bool(_AS.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001 - not installed, or not macOS: reported as missing
        return out
    try:
        import Quartz as _Q
        out["screen_recording"] = bool(_Q.CGPreflightScreenCaptureAccess())
    except Exception:  # noqa: BLE001
        pass
    return out


def _os_path_exists(path) -> bool:
    return os.path.exists(str(path))


@cli.command('attach')
@click.option('--cdp', 'cdp', default='',
              help='The DevTools endpoint of a Chromium someone else is driving: '
                   'http://127.0.0.1:PORT or http://[::1]:PORT, nothing else')
@click.option('--target', 'target_id', default=None,
              help='The page target to pin (required when more than one page is open)')
@click.option('--task', 'task', default='', help='Name for this task; sessions of the '
              'same task with different inputs are compiled together')
@click.option('--input', 'input_value', default='',
              help='The input the agent will use, as NAME=VALUE (optional; inferred '
                   'from the result URL when omitted)')
@click.option('--max-seconds', type=float, default=600.0,
              help='Stop observing after this many seconds')
@click.option('--forget', 'forget_flag', is_flag=True,
              help='Delete stored attach sessions (all, or the one named by --session)')
@click.option('--session', 'session_id', default='', help='With --forget: one session id')
@click.option('--list', 'list_flag', is_flag=True, help='List stored attach sessions')
@click.option('--anonymous-browser', 'anonymous_browser', is_flag=True,
              help='You state the observed browser holds no sign-in (its cookie jar was '
                   'empty when this session started): cookies the site sets during the '
                   'session are then anonymous visitor cookies, not a sign-in')
@click.option('--origin', 'origin', default='',
              type=click.Choice(['', 'user_task', 'scheduled', 'bulk', 'bench']),
              help='The run origin. With ANYUSE_CHALLENGE_POLICY=human_solves_live and an '
                   'origin in ANYUSE_LIVE_SOLVE_ORIGINS, a human check on the page asks you '
                   'to solve it in your own browser while this keeps watching. Omitted: '
                   'no live solve. bench (an automated test run): refused with that policy.')
@click.option('--login-replay', 'login_replay_host', default='',
              help='Capture logged-in requests for this site (its registrable domain), '
                   'keeping their session cookie NAMES (never values) so the workflow can '
                   'compile for site-mode login=replay. The site must already be set '
                   'login=replay (`anyuse sites set <host> --login replay`).')
def attach_cmd(cdp, target_id, task, input_value, max_seconds, forget_flag,
               session_id, list_flag, anonymous_browser=False, origin='',
               login_replay_host=''):
    """Observe a browser another agent drives — read-only — and record the session.

    Nothing is clicked, typed or navigated by AnyUse. Secrets are filtered before
    anything is written; the session file is mode 0600 under the AnyUse data dir.
    Compile several sessions of one task with `anyuse compile last`.
    See docs/ATTACH.md.
    """
    from anyuse.execution import attach as _attach
    if forget_flag:
        n = _attach.forget(session_id)
        click.echo(f"deleted {n} attach session(s)")
        return
    if list_flag:
        for s in _attach.list_sessions():
            click.echo(f"{s['id']}  task={_attach.task_name([s])}  "
                       f"requests={len(s.get('network') or [])}  "
                       f"snapshots={len(s.get('snapshots') or [])}  "
                       f"stop={s.get('stop_reason', '')}")
        return
    if not cdp:
        raise click.UsageError("--cdp http://127.0.0.1:PORT is required")
    login_replay_site = ""
    if login_replay_host:
        from anyuse.execution import site_modes
        try:
            site = site_modes.validate_host(login_replay_host)
        except site_modes.SiteModeError as exc:
            click.echo(f"attach refused: {exc}")
            raise SystemExit(2)
        modes = site_modes.stored_modes(site)
        if modes is None or modes.login != "replay":
            click.echo(f"attach refused: {site} is not set login=replay; run "
                       f"`anyuse sites set {site} --login replay` first")
            raise SystemExit(2)
        login_replay_site = site
    live_solver = None
    if origin:
        from anyuse.execution import human_inbox as _hi
        from anyuse.execution import live_solve as _ls
        from anyuse.execution.human_gate import TtyGate
        try:
            _hi.refuse_bench_live_solve(origin)
        except _hi.LiveSolveRefused as exc:
            click.echo(f"attach refused: {exc}")
            raise SystemExit(2)
        live_solver = _ls.default_solver(_hi.Inbox(), gate=TtyGate())
    try:
        path, summary = _attach.attach(cdp, target_id=target_id, task=task,
                                       input_value=input_value, max_seconds=max_seconds,
                                       echo=lambda m: click.echo(m),
                                       anonymous_browser=anonymous_browser,
                                       live_solver=live_solver, origin=origin or None,
                                       login_replay_site=login_replay_site,
                                       capture_ua_for=_browser_identity_predicate())
    except _attach.AttachError as exc:
        click.echo(f"attach refused: {exc}")
        raise SystemExit(2)
    click.echo(f"session {summary['id']} saved ({summary['requests']} requests, "
               f"{summary['credential_bound']} credential-bound, {summary['snapshots']} "
               f"snapshots; stopped: {summary['stop_reason']}) -> {path}")


@cli.command('compile')
@click.argument('which', type=click.Choice(['last']))
@click.option('--n', 'n', type=int, default=0,
              help='Use only the N most recent sessions of the task (default: all)')
@click.option('--task', 'task', default='', help='Task name (default: the latest session\'s)')
@click.option('--name', 'name', default='', help='Workflow name to persist under')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--no-persist', is_flag=True, help='Compile and prove, but do not persist')
@click.option('--robots-policy', 'robots_policy_flag',
              type=click.Choice(['respect', 'ignore']), default=None)
@click.option('--confirm', 'confirm_url', default='',
              help='Non-interactive confirmation: restate the proved request URL '
                   '(scheme://host/path) exactly; without it you are asked')
@click.option('--explain', is_flag=True,
              help='Print why this request was chosen and what the status means')
@click.option('--shape', 'shape', type=click.Choice(['auto', 'list', 'detail']),
              default='auto', help='list: result-list proof; detail: item-page proof '
                                   '(auto: detail when the input is the item URL\'s path)')
@_asks_new_hosts
def compile_cmd(which, n, task, name, json_output, no_persist, robots_policy_flag,
                confirm_url, explain, shape):
    """Compile the latest attached sessions of one task, with the fresh-input proof.

    The first session is compiled from; the others are inputs it never saw.
    Ground truth is each session's captured result page (docs/ATTACH.md).
    AnyUse reads a new host's robots.txt, then asks before the proof replays
    the request against it; --yes answers for scripts.
    """
    from anyuse.execution import attach as _attach
    try:
        sessions = _attach.select_sessions(_attach.list_sessions(), task=task, n=n)
        compiled, proof, report, recs = _attach.compile_sessions(
            sessions, robots_policy=robots_policy_flag, shape=shape)
    except _attach.AttachError as exc:
        click.echo(f"attach compile refused: {exc}")
        raise SystemExit(2)
    wf_name = name or _attach.task_name(sessions)
    if report.get("proof_kind") == "detail_fields":
        # Detail proofs produce a normal, read-only RequestTemplate whose
        # extractor uses the same bounded field vocabulary as the proof.  The
        # record below is therefore listable/runnable through the ordinary MCP
        # compiled-tool surface; its status is copied from proof_split and is
        # never promoted here.
        import os as _os
        from anyuse.execution.api_compile import compiled_record, persist_compiled
        from anyuse.memory.storage import WorkflowStorage

        persisted = None
        confirmation = None
        if proof.passed and not no_persist:
            url = getattr(compiled.template, "url", "") or ""
            if confirm_url:
                ok = confirm_url.strip() == url
                why = ("restated URL (--confirm)" if ok
                       else f"--confirm did not restate the proved URL {url}")
            else:
                click.echo(f"Status {report['status']!r} on {proof.passed_count} validation "
                           f"input(s): {url} (field {report.get('field') or '-'}). "
                           "The ground truth was the agent's captured item page.", err=True)
                try:
                    ok = click.confirm("Persist this as a tool?", default=False, err=True)
                except click.Abort:
                    ok = False
                why = "interactive prompt"
            confirmation = {"confirmed": bool(ok), "how": why}
            if ok:
                storage = WorkflowStorage()
                wf = storage.get(wf_name)
                workflow_id = wf.id if wf else wf_name
                first_path = str(sessions[0].get("_path") or sessions[0].get("id") or "")
                record = compiled_record(
                    compiled.template, proof.to_dict(), workflow=wf_name,
                    source={"recording": _os.path.basename(first_path),
                            "sessions": [s.get("id") for s in sessions],
                            "site": recs[0].get("site", ""), "origin": "attach",
                            "shape": "detail",
                            "ground_truth": "captured item-page field (docs/ATTACH.md)"})
                from anyuse.execution.evidence_store import keep_compile_evidence
                keep_compile_evidence(storage, workflow_id, record, sessions=sessions)
                persisted = persist_compiled(storage, workflow_id, record)
        if json_output:
            click.echo(json.dumps({"workflow": wf_name, "report": report,
                                   "proof": proof.to_dict(),
                                   "confirmation": confirmation,
                                   "persisted": persisted},
                                  indent=2, default=str))
        else:
            click.echo(f"Attached sessions: {len(sessions)} of task {wf_name!r}; item pages "
                       f"({report['param']!r} = {', '.join(report['values'])}, "
                       f"{report['param_source']})")
            click.echo(f"Detail proof: template {report['template_url']}, field "
                       f"{report.get('field') or '-'}")
            for c in proof.cases:
                click.echo(f"  {c.value}: {'PASSED' if c.passed else 'FAILED'} - {c.reason}")
            click.echo(f"Status: {report['status']}")
            click.echo(f"Proof scope: {report['scope']}")
            if persisted:
                click.echo(f"✓ Persisted as the preferred implementation: {persisted}")
            elif confirmation and not confirmation["confirmed"]:
                click.echo(f"✗ Not persisted: not confirmed ({confirmation['how']}).")
            elif no_persist:
                click.echo("(--no-persist: nothing written)")
            elif not proof.passed:
                click.echo("✗ Not persisted: the detail template did not reproduce the "
                           "captured item field on fresh input.")
        raise SystemExit(0 if proof.passed else 1)
    if not json_output:
        click.echo(f"Attached sessions: {len(sessions)} of task {wf_name!r}; input "
                   f"{report['param']!r} = {', '.join(report['values'])} "
                   f"({report['param_source']})")
        click.echo(f"Ground truth: {report['ground_truth']} "
                   f"(selector {report['ground_truth_selector']!r}); sample: "
                   f"{'; '.join(report['ground_truth_sample'][:4])}")
        if report.get("repeat_ms_median") is not None:
            click.echo(f"Repeat latency (proof replays): median "
                       f"{report['repeat_ms_median']:.0f} ms")
        click.echo(f"Status: {report['status']}")
        if explain or not report["selection_held_out"]:
            click.echo(_attach_status_explanation(report, len(sessions)))
    paths = [s["_path"] for s in sessions]

    def confirm(compiled, proof):
        url = getattr(compiled.template, "url", "") or ""
        if confirm_url:
            if confirm_url.strip() == url:
                return True, "restated URL (--confirm)"
            return False, f"--confirm did not restate the proved URL {url}"
        click.echo(f"Status {report['status']!r} on {proof.passed_count} validation "
                   f"input(s): {url} (parameter {report['param']!r}). The ground truth was the "
                   f"agent's result page, not a person's list: "
                   f"{'; '.join(report['ground_truth_sample'][:5])}", err=True)
        try:
            ok = click.confirm("Persist this as a tool?", default=False, err=True)
        except click.Abort:
            ok = False
        return ok, "interactive prompt"

    # the sessions are ordered compile, selection, held-out (attach.compile_sessions): record them so
    n_sel = int(report.get("n_selection") or 0)
    return _finish_compile(
        wf_name, compiled, proof, recs[0], paths[:1], [], paths[1 + n_sel:],
        json_output, no_persist, "", False, False, explain, robots_policy_flag, None,
        attach_report=report, confirm=confirm, sel_files=paths[1:1 + n_sel],
        evidence={"recording": sessions[0], "selection": sessions[1:1 + n_sel],
                  "fresh": sessions[1 + n_sel:]})


def _attach_status_explanation(report, n_sessions):
    from anyuse.execution.attach import MIN_SESSIONS_PROVED
    if report["selection_held_out"]:
        return (f"  why: the result-list rule and the request were fitted on sessions "
                f"1-2 only; {n_sessions - 2} validation session(s) were never seen by "
                f"fitting or selection.")
    return (f"  why NOT proved: with {n_sessions} session(s) the result-list rule had to "
            f"be derived from every result page, including the validation ones, so "
            f"the check is not independent. Record >= {MIN_SESSIONS_PROVED} sessions "
            f"(1 compile, 1 selection, >= 2 validation) for 'proved'.")


@cli.command()
def version():
    """Show version information."""
    from anyuse import __version__
    click.echo(f"anyuse {__version__}")


# ─────────────────────────────────────────────────────────
# Persistent behavior settings
# ─────────────────────────────────────────────────────────

@cli.group('settings', invoke_without_command=True)
@click.pass_context
def settings_group(ctx):
    """Show or change persistent behavior settings."""
    if ctx.invoked_subcommand is not None:
        return
    from anyuse import settings as _settings
    try:
        _settings.validate_file()
        rows = [_settings.resolve(name) for name in _settings.SETTINGS]
    except _settings.SettingError as exc:
        raise click.ClickException(str(exc))
    name_width = max(len("NAME"), *(len(row.setting.name) for row in rows))
    value_width = max(len("VALUE"), *(len(_settings.display_value(row.value)) for row in rows))
    click.echo(f"{'NAME':<{name_width}}  {'VALUE':<{value_width}}  SOURCE   SECURITY  DESCRIPTION")
    for row in rows:
        security = "*" if row.setting.security_relevant else ""
        click.echo(
            f"{row.setting.name:<{name_width}}  "
            f"{_settings.display_value(row.value):<{value_width}}  "
            f"{row.source:<8} {security:<8}  {row.setting.description}"
        )
    for row in rows:
        warning = _settings.security_warning(row.setting.name, row.value)
        if warning:
            click.echo(f"\n{row.setting.name}: {warning}")
    from anyuse.execution.consent import load_decisions
    choices = load_decisions()
    if choices:
        click.echo("\nREMEMBERED CONSENT CHOICES")
        for host, entry in sorted(choices.items()):
            click.echo(f"  {host} = {entry['choice']}")
        click.echo("Reset with: anyuse settings reset-consent [HOST]")
    click.echo("\n* security-relevant; enabling or relaxing it requires terminal confirmation")


@settings_group.command('get')
@click.argument('name')
def settings_get(name):
    """Print the effective value of NAME."""
    from anyuse import settings as _settings
    try:
        resolved = _settings.resolve(name)
    except _settings.SettingError as exc:
        raise click.ClickException(str(exc))
    click.echo(_settings.display_value(resolved.value))


def _confirm_setting_change(spec, value) -> None:
    """Use the same typed-code terminal gate as routine approval."""
    if not spec.needs_confirmation(value):
        return
    import hashlib
    from anyuse.execution.human_gate import TtyGate
    summary = {
        "purpose": "change-setting",
        "action": f"set {spec.name}",
        "effects": {
            "value": value,
            "description": spec.description,
            "security_relevant": True,
        },
    }
    from anyuse import settings as _settings
    warning = _settings.security_warning(spec.name, value)
    if warning:
        summary["effects"]["warning"] = warning
    digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
    why = TtyGate()(summary, digest)
    if why:
        raise click.ClickException(why)


@settings_group.command('set')
@click.argument('name')
@click.argument('value')
def settings_set(name, value):
    """Validate and persist NAME as VALUE."""
    from anyuse import settings as _settings
    try:
        canonical, parsed = _settings.parse_for_storage(name, value)
        _confirm_setting_change(_settings.SETTINGS[canonical], parsed)
        path, parsed = _settings.set_value(canonical, parsed)
    except _settings.SettingError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{canonical} = {_settings.display_value(parsed)} ({path})")


@settings_group.command('reset')
@click.argument('name')
def settings_reset(name):
    """Remove NAME from settings.toml (environment variables may still win)."""
    from anyuse import settings as _settings
    try:
        canonical = _settings.normalize_name(name)
        values = _settings.validate_file()
        current = _settings.resolve(canonical).value
        if canonical in os.environ:
            after = _settings.SETTINGS[canonical].parse(
                os.environ[canonical], strict=False)
        else:
            after = _settings.SETTINGS[canonical].default
        if canonical in values and current != after:
            _confirm_setting_change(_settings.SETTINGS[canonical], after)
        path, removed = _settings.reset_value(canonical)
    except _settings.SettingError as exc:
        raise click.ClickException(str(exc))
    state = "reset" if removed else "already absent"
    click.echo(f"{canonical}: {state} ({path})")


@settings_group.command('spend')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def settings_spend(json_output):
    """Show the spend caps in effect and today's (UTC) spend per feature.

    Read-only: it reads settings and the spend ledger and creates no file."""
    from anyuse.execution import spend as _spend
    info = _spend.summary()
    if json_output:
        click.echo(json.dumps(info, indent=2, sort_keys=True))
        return

    def amount(entry, per=""):
        if entry.get("problem"):
            return "REFUSES ALL (" + entry["problem"] + ")"
        if entry.get("unlimited"):
            return "unlimited"
        return f"US${entry['usd']:.4f}{per}"

    caps = info["caps"]
    rows = [
        ("global, per UTC day", amount(caps["global_daily"]), caps["global_daily"]),
        ("per run", amount(caps["per_run"]), caps["per_run"]),
        ("parameter proposals, per day", amount(caps["draft_propose_daily"]),
         caps["draft_propose_daily"]),
        ("gap fills, per day", amount(caps["draft_fill_daily"]), caps["draft_fill_daily"]),
    ]
    click.echo("SPEND CAPS")
    width = max(len(r[0]) for r in rows)
    for label, text, entry in rows:
        click.echo(f"  {label:<{width}}  {text}  ({entry['setting']}, {entry['source']})")
    for key, label in (("draft_propose_daily", "parameter proposals"),
                       ("draft_fill_daily", "gap fills")):
        entry = caps[key]
        if "log" in entry:
            spent = entry.get("spent_today_usd")
            click.echo(f"  {label} today in their own log: "
                       + (f"US${spent:.4f}" if spent is not None else "unreadable")
                       + f" ({entry['log']})")
    click.echo(f"\nLEDGER {info['ledger']}")
    today = info["today"]
    if today is None:
        click.echo("  this process is not armed: the ledger was not read")
        return
    if today["problem"]:
        click.echo(f"  {today['problem']}")
        return
    click.echo(f"  today ({today['day']} UTC)")
    if not today["by_feature"]:
        click.echo("  no model request booked today")
    feature_width = max([len("total")] + [len(k) for k in today["by_feature"]])
    for feature, row in today["by_feature"].items():
        click.echo(f"  {feature:<{feature_width}}  US${row['usd']:.4f}  {row['calls']} request(s)")
    total = f"  {'total':<{feature_width}}  US${today['total_usd']:.4f}"
    if info.get("remaining_usd") is not None:
        total += f"  (US${info['remaining_usd']:.4f} left under the global cap)"
    click.echo(total)


@settings_group.command('reset-consent')
@click.argument('host', required=False, default='')
def settings_reset_consent(host):
    """Forget the remembered cookie choice for HOST, or all hosts."""
    from anyuse.execution.consent import decisions_path, reset_decisions
    removed = reset_decisions(host)
    subject = host or "all hosts"
    click.echo(f"consent choices for {subject}: removed {removed} ({decisions_path()})")


# ─────────────────────────────────────────────────────────
# Per-site web-tier modes (docs/SITE_MODES.md)
# ─────────────────────────────────────────────────────────

def _browser_identity_predicate():
    """The product recorders' User-Agent capture predicate: a request's UA is
    kept only for a site set identity=browser, read through the one accessor."""
    from anyuse.execution.site_modes import browser_identity_for
    return browser_identity_for


def _confirm_site_modes(host: str, effects: dict) -> None:
    """The SAME typed-code terminal gate security settings and routine approval
    use (execution/human_gate.TtyGate). Any non-default mode goes through it, so
    it is impossible to enable one through a pipe or an unattended shell."""
    import hashlib
    from anyuse.execution.human_gate import TtyGate
    summary = {
        "purpose": "set-site-modes",
        "action": f"set web modes for {host}",
        "effects": effects,
    }
    digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
    why = TtyGate()(summary, digest)
    if why:
        raise click.ClickException(why)


@cli.group('sites', invoke_without_command=True)
@click.pass_context
def sites_group(ctx):
    """Show or change per-site web-tier modes (identity/robots/login/route).

    Every mode is opt-in and per registrable domain; a site with no entry runs
    today's safe defaults (honest UA, robots respected, no credential replay,
    UI route). See docs/SITE_MODES.md.
    """
    if ctx.invoked_subcommand is not None:
        return
    ctx.invoke(sites_list)


@sites_group.command('list')
def sites_list():
    """List every site that has a non-default mode."""
    from anyuse.execution import site_modes
    rows = site_modes.load()
    if not rows:
        click.echo("no per-site modes set — every site runs the safe defaults "
                   f"({site_modes.site_modes_path()})")
        return
    for host in sorted(rows):
        modes = site_modes.SiteModes.from_table(host, rows[host])
        click.echo(f"{host}: identity={modes.identity} robots={modes.robots} "
                   f"login={modes.login} route={modes.route}"
                   + (" [IGNORED: " + site_modes.host_block_reason(host) + "]"
                      if site_modes.host_block_reason(host) else ""))


@sites_group.command('show')
@click.argument('host')
def sites_show(host):
    """Show the stored modes for HOST (by its registrable domain)."""
    from anyuse.execution import site_modes
    modes = site_modes.stored_modes(host)
    if modes is None:
        click.echo(f"{host}: no modes set — runs the safe defaults")
        return
    click.echo(f"{modes.host}: identity={modes.identity} robots={modes.robots} "
               f"login={modes.login} route={modes.route}")
    if modes.session:
        click.echo(f"  session: {modes.session.get('browser')} / "
                   f"{modes.session.get('profile')} / host {modes.session.get('host')}; "
                   f"cookie names {sorted(modes.session.get('cookie_names') or [])} "
                   f"(NAMES only — no value is stored)")
    block = site_modes.host_block_reason(host)
    if block:
        click.echo(f"  NOTE: a mode here is IGNORED at use time: {block}")


@sites_group.command('set')
@click.argument('host')
@click.option('--identity', type=click.Choice(['honest', 'browser']), default=None)
@click.option('--robots', type=click.Choice(['respect', 'ignore']), default=None)
@click.option('--login', type=click.Choice(['none', 'replay']), default=None)
@click.option('--route', type=click.Choice(['ui', 'replay']), default=None)
def sites_set(host, identity, robots, login, route):
    """Set one or more modes for HOST. Any non-default value needs the typed-code
    terminal confirmation (as `anyuse routine approve`)."""
    from anyuse.execution import site_modes
    try:
        reg = site_modes.validate_host(host)
        current = site_modes.stored_modes(reg)
        proposed = {
            "identity": identity or (current.identity if current else site_modes.DEFAULTS["identity"]),
            "robots": robots or (current.robots if current else site_modes.DEFAULTS["robots"]),
            "login": login or (current.login if current else site_modes.DEFAULTS["login"]),
            "route": route or (current.route if current else site_modes.DEFAULTS["route"]),
        }
        non_default = {k: v for k, v in proposed.items() if v != site_modes.DEFAULTS[k]}
        if non_default:
            _confirm_site_modes(reg, {"non_default": non_default, "registrable_domain": reg})
        modes = site_modes.set_modes(reg, identity=identity, robots=robots,
                                     login=login, route=route)
    except site_modes.SiteModeError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{modes.host}: identity={modes.identity} robots={modes.robots} "
               f"login={modes.login} route={modes.route} "
               f"({site_modes.site_modes_path()})")
    if modes.login == "replay" and not modes.session:
        click.echo(f"next: import the session with `anyuse sites login {modes.host}`")


@sites_group.command('reset')
@click.argument('host')
def sites_reset(host):
    """Remove every mode for HOST (back to the safe defaults)."""
    from anyuse.execution import site_modes
    key = site_modes.site_key(host) or host.strip().lower()
    try:
        removed = site_modes.reset_modes(host)
    except site_modes.SiteModeError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{key}: {'reset' if removed else 'already absent'} "
               f"({site_modes.site_modes_path()})")


@sites_group.command('login')
@click.argument('host')
@click.option('--browser', required=True,
              help='chrome, arc, brave, edge, chromium or firefox')
@click.option('--profile', default='', help='the profile directory name')
@click.option('--db', 'db_path', required=True,
              help='path to the browser profile cookie database to read NAMES from')
@click.option('--cookie-host', default='',
              help='the cookie host to match (defaults to the site)')
def sites_login(host, browser, profile, db_path, cookie_host):
    """Record the imported session REFERENCE for a login=replay site.

    Reads only the cookie NAMES the host has in the chosen profile (plaintext;
    no decryption, no Keychain). Nothing but the browser, profile name, cookie
    host and those names is stored; a cookie VALUE is only ever decrypted into
    memory at replay time. Enabling login=replay needs the typed-code gate."""
    from anyuse.execution import site_modes, session_import
    for label, text in (("--db", db_path), ("--profile", profile),
                        ("--browser", browser), ("--cookie-host", cookie_host)):
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in str(text or "")):
            raise click.ClickException(f"{label} contains a control character — refused")
    try:
        reg = site_modes.validate_host(host)
        ck_host = (cookie_host or reg).split("/")[0].split(":")[0].strip().lower().rstrip(".")
        # The cookie host must belong to this site (never import one site's
        # cookies under another) and must not be a host site_modes refuses (the
        # never-contact list; government hosts with ANYUSE_REFUSE_GOV_HOSTS on).
        if site_modes.site_key(ck_host) != reg:
            raise click.ClickException(
                f"--cookie-host {ck_host!r} is not under the site {reg} — refused")
        block = site_modes.host_block_reason(ck_host)
        if block:
            raise click.ClickException(f"refusing to import cookies for {ck_host}: {block}")
        names = session_import.cookie_names(browser, db_path, ck_host)
        if not names:
            raise click.ClickException(
                f"no cookies for {ck_host} in that profile — sign in to the site in "
                f"{browser} first, then re-run `anyuse sites login`")
        session_names = [n for n in names]
        import os as _os
        db_abs = _os.path.abspath(db_path)
        _confirm_site_modes(reg, {"non_default": {"login": "replay"},
                                  "session": {"browser": browser, "profile": profile,
                                              "host": ck_host, "db_path": db_abs,
                                              "cookie_names": sorted(session_names)}})
        modes = site_modes.set_modes(reg, login="replay", session={
            "browser": browser, "profile": profile, "host": ck_host,
            "db_path": db_abs, "cookie_names": session_names})
    except (site_modes.SiteModeError, session_import.SessionImportError) as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{modes.host}: login=replay session recorded "
               f"({browser}/{profile}, {len(modes.session['cookie_names'])} cookie name(s); "
               f"no value stored)")
    if modes.route != "replay":
        click.echo(f"next: `anyuse sites set {modes.host} --route replay` to replay "
                   f"logged-in work over HTTP (the UI route stays the default otherwise)")


# ─────────────────────────────────────────────────────────
# Skills: compiled tools as Agent Skills folders (docs/INSTALL_FOR_AGENTS.md)
# ─────────────────────────────────────────────────────────

@cli.group('skills')
def skills_group():
    """Write your proved compiled tools as Agent Skills (SKILL.md folders)."""
    pass


@skills_group.command('export')
@click.argument('directory', metavar='DIR')
@click.option('--tool', 'tools', multiple=True, metavar='NAME',
              help='Export only this tool (repeatable; anyuse workflow list shows the names)')
@click.option('--force', is_flag=True,
              help='Write skills this command wrote before even when nothing changed. '
                   'Folders it did not write are never touched.')
@click.option('--profile', type=click.Choice(['default', 'meta', 'full']), default=None,
              help='The MCP profile your agent runs the AnyUse server with; the skills name its tool. '
                   'Default: ANYUSE_MCP_PROFILE as set here.')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.pass_context
def skills_export(ctx, directory, tools, force, profile, json_output):
    """Write DIR/anyuse-<tool>/SKILL.md for each proved tool MCP serves.

    \b
    DIR is the skills folder your agent reads; nothing is written anywhere else:
      Claude Code   ~/.claude/skills/   (a project: .claude/skills/)
      Codex         ~/.agents/skills/   (a project: .agents/skills/)
      OpenClaw      ~/.agents/skills/ or ~/.openclaw/skills/
      Hermes Agent  ~/.hermes/skills/
    Each skill tells the agent to call the compiled tool over MCP, or with
    `anyuse --json workflow play`, instead of browsing the site again. Under
    --profile meta (the server started with ANYUSE_MCP_PROFILE=meta) the skill
    names call_operation; under default (and full), the typed tool. A tool that is not
    proved, not read-only or not servable is skipped with the reason. A folder
    this command did not write is left alone.
    """
    from anyuse.execution.skill_export import export_skills, text

    as_json = json_output or bool((ctx.obj or {}).get('json_output'))
    report = export_skills(directory, tools=tools, force=force, profile=profile)
    code = 0 if report['ok'] else (1 if report['written'] or report['skipped'] else 2)
    if as_json:
        click.echo(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        sys.exit(code)
    for item in report['written']:
        click.echo(f"wrote    {item['path']}  ({text(item['tool'], 80)} on {text(item['host'], 80)})")
    for item in report['skipped']:
        where = f" on {text(item['host'], 80)}" if item.get('host') else ""
        click.echo(f"skipped  {text(item['tool'], 80)}{where}: {text(item['reason'], 400)}")
    if report['reason'] and not (report['written'] or report['skipped']):
        click.echo(f"✗ {report['reason']}", err=True)
    elif not (report['written'] or report['skipped']):
        click.echo("No compiled tools to export. Compile one first (docs/QUICKSTART_COMPILE.md).")
    else:
        click.echo(f"{len(report['written'])} written, {len(report['skipped'])} skipped, in {report['dir']}, "
                   f"for the {report['profile']} MCP profile."
                   + (f" {report['reason']}" if report['reason'] else
                      " Your agent reads them from its skills folder (docs/INSTALL_FOR_AGENTS.md)."))
    sys.exit(code)


# ─────────────────────────────────────────────────────────
# Routines: the out-of-band human channel (docs/ROUTINES.md)
# ─────────────────────────────────────────────────────────

@cli.group('routine')
def routine_group():
    """Approve or reconcile a skill/routine run as a person, outside MCP."""
    pass


def _routine_echo(result):
    click.echo(json.dumps(result, indent=2, sort_keys=True, default=str))
    if result.get('status') == 'refused':
        raise SystemExit(1)


@routine_group.command('show')
@click.argument('run_id')
@click.option('--skills-dir', default=None, help='Skills directory (default <ANYUSE_DATA_DIR>/skills)')
def routine_show(run_id, skills_dir):
    """Show the run's pending hand-off, including the digest to confirm."""
    from anyuse.mcpserver import skill_tools
    _routine_echo(skill_tools.show_run(run_id, directory=skills_dir))


@routine_group.command('approve')
@click.argument('run_id')
@click.argument('step_id')
@click.option('--confirm', 'confirm', required=True,
              help='The exact approval_digest shown by the hand-off')
@click.option('--skills-dir', default=None, help='Skills directory (default <ANYUSE_DATA_DIR>/skills)')
def routine_approve(run_id, step_id, confirm, skills_dir):
    """Approve the pending step. Bound to run, step, inputs, effects and version.

    Needs a terminal on stdin and stdout; shows the effect and asks you to type
    back a code on /dev/tty. --confirm alone is not sufficient.
    """
    from anyuse.mcpserver import skill_tools
    from anyuse.execution.human_gate import TtyGate
    _routine_echo(skill_tools.human_approve(run_id, step_id, confirm, gate=TtyGate(),
                                            directory=skills_dir))


@routine_group.command('reconcile')
@click.argument('run_id')
@click.argument('step_id')
@click.option('--outcome', type=click.Choice(['completed', 'not_done']), required=True)
@click.option('--outputs', 'outputs_json', default=None,
              help='JSON object with the step outputs (outcome completed)')
@click.option('--confirm', 'confirm', required=True,
              help='The exact reconcile_digest shown by the hand-off')
@click.option('--skills-dir', default=None, help='Skills directory (default <ANYUSE_DATA_DIR>/skills)')
def routine_reconcile(run_id, step_id, outcome, outputs_json, confirm, skills_dir):
    """Say what happened to an uncertain step (completed with outputs, or not done)."""
    from anyuse.mcpserver import skill_tools
    try:
        outputs = json.loads(outputs_json) if outputs_json else None
    except ValueError as e:
        raise click.ClickException(f'--outputs is not JSON: {e}')
    from anyuse.execution.human_gate import TtyGate
    _routine_echo(skill_tools.human_reconcile(run_id, step_id, outcome, confirm, outputs,
                                              gate=TtyGate(), directory=skills_dir))


@routine_group.command('allow-agent-terminals')
@click.option('--revoke', is_flag=True, help='Remove the opt-in file')
def routine_allow_agent_terminals(revoke):
    """Per-machine opt-in: allow approve/reconcile in terminals with agent markers.

    Must be run from your own terminal (no agent markers) and asks you to type
    back a code. This raises the bar; it is not authentication.
    """
    from anyuse.execution.human_gate import TtyGate
    gate = TtyGate()
    if revoke:
        try:
            os.remove(gate.optin_file)
        except FileNotFoundError:
            pass
        _routine_echo({'status': 'revoked', 'file': gate.optin_file})
        return
    _routine_echo(gate.write_optin())


# ─────────────────────────────────────────────────────────
# Human inbox (docs/HUMAN_INBOX.md): things only a person can do
# ─────────────────────────────────────────────────────────

@cli.group('inbox', invoke_without_command=True)
@click.option('--all', 'include_all', is_flag=True, help='Include done/closed cards')
@click.option('--json', 'as_json', is_flag=True, help='Print JSON')
@click.option('--skills-dir', default=None, help='Skills directory (default <ANYUSE_DATA_DIR>/skills)')
@click.pass_context
def inbox_group(ctx, include_all, as_json, skills_dir):
    """Pending cards across runs: kind, host, what is needed, what continues after."""
    ctx.ensure_object(dict)
    ctx.obj['inbox_skills_dir'] = skills_dir
    if ctx.invoked_subcommand is not None:
        return
    from anyuse.mcpserver import skill_tools
    listing = skill_tools.inbox_list(directory=skills_dir, include_all=include_all)
    if as_json:
        click.echo(json.dumps(listing, indent=2, sort_keys=True, default=str))
        return
    cards = listing['cards']
    if not cards:
        click.echo('Inbox empty: nothing needs you.')
        return
    click.echo(f"{listing['pending']} card(s) need you:")
    for c in cards:
        click.echo(f"\n  {c['id']}  {c['kind']:<13} {c['host'] or '-'}  [{c['status']}]")
        click.echo(f"    needed: {c['needed']}")
        click.echo(f"    after:  {c['continues_after']}")
        click.echo(f"    run:    {c['run_id']} ({c['manifest']}, step {c['step']})")
    click.echo(f"\n{listing['how_to_complete']}")


def _inbox_dir_opt(ctx, skills_dir):
    return skills_dir or (ctx.obj or {}).get('inbox_skills_dir')


@inbox_group.command('do')
@click.argument('card_id')
@click.option('--skills-dir', default=None)
@click.pass_context
def inbox_do(ctx, card_id, skills_dir):
    """Do what the card asks, then that run resumes (fresh checks first).

    Needs a terminal on stdin and stdout. Shows the card on /dev/tty, reads the
    result you type there (a value card: typing is not shown), and asks you to
    type back a code.
    """
    from anyuse.mcpserver import skill_tools
    from anyuse.execution.human_gate import TtyGate
    _routine_echo(skill_tools.inbox_do(card_id, gate=TtyGate(),
                                       directory=_inbox_dir_opt(ctx, skills_dir)))


@inbox_group.command('live')
@click.argument('card_id')
@click.option('--skills-dir', default=None)
@click.pass_context
def inbox_live(ctx, card_id, skills_dir):
    """Open a waiting live solve: shows the private 127.0.0.1 view link on your
    terminal only (typed-back code; never printed to stdout or logged).

    You solve the site's check yourself in that view; AnyUse only passes your
    clicks and keys to that one page, and the waiting run continues after.
    """
    from anyuse.mcpserver import skill_tools
    from anyuse.execution.human_gate import TtyGate
    _routine_echo(skill_tools.inbox_live(card_id, gate=TtyGate(),
                                         directory=_inbox_dir_opt(ctx, skills_dir)))


@inbox_group.command('skip')
@click.argument('card_id')
@click.option('--skills-dir', default=None)
@click.pass_context
def inbox_skip(ctx, card_id, skills_dir):
    """Put a card aside; its run keeps waiting and `inbox do` still works."""
    from anyuse.mcpserver import skill_tools
    from anyuse.execution.human_gate import TtyGate
    _routine_echo(skill_tools.inbox_skip(card_id, gate=TtyGate(),
                                         directory=_inbox_dir_opt(ctx, skills_dir)))


@inbox_group.command('cancel')
@click.argument('card_id')
@click.option('--skills-dir', default=None)
@click.pass_context
def inbox_cancel(ctx, card_id, skills_dir):
    """Cancel the card's run for good (typed-back code)."""
    from anyuse.mcpserver import skill_tools
    from anyuse.execution.human_gate import TtyGate
    _routine_echo(skill_tools.inbox_cancel(card_id, gate=TtyGate(),
                                           directory=_inbox_dir_opt(ctx, skills_dir)))


# ─────────────────────────────────────────────────────────
# Map registry — opt-in, nothing sent without a URL
# ─────────────────────────────────────────────────────────

@cli.group('map')
def map_registry():
    """App interfaces: the surfaces an app declares, and the REA harness (anyuse map rea)."""
    pass


def _map_client(registry_opt):
    from anyuse.registry.client import RegistryClient, RegistryConfigError, registry_url
    try:
        return RegistryClient(registry_url(registry_opt))
    except RegistryConfigError as e:
        raise click.ClickException(str(e))


def _map_echo(obj):
    click.echo(json.dumps(obj, indent=2, sort_keys=True))


@map_registry.command('keygen')
def map_keygen():
    """Create the local Ed25519 contributor key; print only the public key."""
    from anyuse.registry.client import RegistryConfigError, generate_private_key_file
    try:
        path, public_key = generate_private_key_file()
    except RegistryConfigError as e:
        raise click.ClickException(str(e))
    click.echo(f'Private key created at {path} (mode 0600).')
    click.echo(f'Public key: {public_key}')


@map_registry.command('export')
@click.argument('discovery_json', type=click.Path(exists=True, dir_okay=False))
@click.option('--host', 'host', required=True, help='Host inside the discovery result file')
@click.option('--date', default=None, help='Proof day (YYYY-MM-DD); default today (UTC)')
@click.option('--output', '-o', type=click.Path(dir_okay=False), default=None)
def map_export(discovery_json, host, date, output):
    """Build a sanitised map entry from a local discovery result. Sends nothing."""
    from anyuse.registry.client import export_web
    with open(discovery_json, encoding='utf-8') as fh:
        data = json.load(fh)
    rec = (data.get('hosts') or {}).get(host) if isinstance(data, dict) else None
    if rec is None and isinstance(data, dict) and data.get('host') == host:
        rec = data
    if rec is None:
        raise click.ClickException('host not found in that file')
    entry, report = export_web(rec, date=date)
    click.echo(json.dumps({'report': report}, indent=2), err=True)
    if entry is None:
        raise SystemExit(1)
    text = json.dumps(entry, indent=2, sort_keys=True)
    if output:
        with open(output, 'w', encoding='utf-8') as fh:
            fh.write(text + '\n')
    else:
        click.echo(text)


@map_registry.command('push')
@click.argument('entry_json', type=click.Path(exists=True, dir_okay=False))
@click.option('--registry', 'registry_opt', default=None, help='Registry URL (no default)')
@click.option('--yes', is_flag=True, help='Send without the confirmation prompt')
def map_push(entry_json, registry_opt, yes):
    """Send one entry. Shows exactly what will be sent first."""
    from anyuse.registry import schema as S
    with open(entry_json, encoding='utf-8') as fh:
        entry = json.load(fh)
    errs = S.entry_errors(entry)
    if errs:
        raise click.ClickException('entry refused locally: ' + '; '.join(errs[:5]))
    client = _map_client(registry_opt)
    body = S.canonical_json(entry)
    click.echo(f'Will send {len(body)} bytes to {client.url}/v0/maps:', err=True)
    click.echo(body.decode('ascii'), err=True)
    if not yes and not click.confirm('Send?', default=False):
        raise SystemExit(1)
    status, out = client.push(entry)
    _map_echo({'status': status, 'response': out})
    if status >= 400:
        raise SystemExit(1)


@map_registry.command('pull')
@click.option('--target', default=None, help='https://host or app:<name>')
@click.option('--id', 'map_id', default=None, help='One map id')
@click.option('--registry', 'registry_opt', default=None, help='Registry URL (no default)')
def map_pull(target, map_id, registry_opt):
    """Fetch maps and land them locally as UNPROVED candidates."""
    from anyuse.registry.client import CandidateStore
    if bool(target) == bool(map_id):
        raise click.ClickException('pass exactly one of --target or --id')
    client = _map_client(registry_opt)
    ids = [map_id] if map_id else None
    if ids is None:
        status, out = client.by_target(target)
        if status != 200:
            raise click.ClickException(f'registry answered {status}: {out}')
        ids = [m.get('id') for m in (out.get('maps') or [])][:10]
    store = CandidateStore()
    landed = []
    for mid in ids:
        status, out = client.get(mid)
        if status != 200:
            landed.append({'id': mid, 'error': status})
            continue
        rec = store.land(out, source=client.url)
        landed.append({'id': rec['id'], 'local': rec['local']})
    _map_echo({'landed': landed})


@map_registry.command('list')
def map_list():
    """Local candidates and their LOCAL proof status."""
    from anyuse.registry.client import CandidateStore
    _map_echo([{'id': r['id'], 'target': r['entry']['target'], 'local': r['local']}
               for r in CandidateStore().list()])


@map_registry.command('prove')
@click.argument('map_id')
@click.option('--cap', 'cap_name', default=None, help='Capability name (default: all web ones)')
@click.option('--values', default=None, help='Comma-separated inputs from the site itself')
@click.option('--report', is_flag=True, help='Report the proof summary to the registry')
@click.option('--registry', 'registry_opt', default=None, help='Registry URL for --report')
@_asks_new_hosts
def map_prove(map_id, cap_name, values, report, registry_opt):
    """Run the unchanged local fresh-input proof against the REAL site."""
    from anyuse.execution import discover as D
    from anyuse.execution.api_permission import robots_allows
    from anyuse.execution.api_step import ApiStepExecutor
    from anyuse.execution.api_verify import permission_preflight
    from anyuse.registry.client import CandidateStore, proof_report, prove_candidate
    store = CandidateStore()
    rec = store.load(map_id)
    if rec is None:
        raise click.ClickException('no such local candidate; pull it first')
    names = [cap_name] if cap_name else [c['name'] for c in rec['entry']['capabilities']
                                         if 'request' in c]
    fetch = D.HttpFetcher(allow_url=lambda u: robots_allows(u)[0] is not False)
    executor = ApiStepExecutor(transport=D.PacedTransport(fetch))
    gate = lambda compiled, recording: permission_preflight(compiled, recording)  # noqa: E731
    vals = [v.strip() for v in values.split(',')] if values else None
    results = {}
    for name in names:
        results[name] = prove_candidate(store, map_id, name, fetch, values=vals,
                                        executor=executor, proof_preflight=gate,
                                        transport=D.PacedTransport(fetch))
    out = {'local': results}
    if report:
        client = _map_client(registry_opt)
        out['reported'] = [client.report_proof(map_id, proof_report(results[n], n))[0]
                           for n in names]
    _map_echo(out)


@map_registry.command('surfaces')
@click.argument('target')
@click.option('--bundle', 'bundle_path', type=click.Path(exists=True, file_okay=False), default=None,
              help='An app bundle path (default: found by name in /Applications, /System/Applications, CoreServices)')
@click.option('--artifacts', 'artifacts_dir', type=click.Path(exists=True, file_okay=False), default=None,
              help='Site mode: a folder of saved artifacts (*.html, *.link, *.json, *.js); nothing is fetched')
@click.option('--knowledge', 'knowledge_file', type=click.Path(exists=True, dir_okay=False), default=None,
              help='An anyuse.surface-knowledge/1 file (URL templates etc.; unverified entries stay hypotheses)')
@click.option('--limit', default=40, show_default=True, help='Surfaces to print (0 = all)')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def map_surfaces(target, bundle_path, artifacts_dir, knowledge_file, limit, json_output):
    """Rank an app's or site's automation surfaces, best first, with citations.

    Read-only and offline: an app's bundle files are read, never launched or scripted; a site's saved
    artifacts are read, nothing is fetched."""
    import os as _os
    from anyuse.execution import surface_discovery as SD
    if artifacts_dir or (SD.looks_like_site(target) and not bundle_path):
        arts = {}
        if artifacts_dir:
            for name in sorted(_os.listdir(artifacts_dir)):
                p = _os.path.join(artifacts_dir, name)
                if _os.path.isfile(p) and _os.path.getsize(p) <= SD.MAX_READ:
                    with open(p, encoding='utf-8', errors='replace') as fh:
                        arts[name] = fh.read()
        rep = SD.discover_site(target, arts)
    else:
        path = bundle_path or SD.find_bundle(target)
        if not path:
            raise click.ClickException(f'no app bundle named {target!r} (give --bundle PATH)')
        b = SD.discover_bundle(path)
        surfaces = b.surfaces
        if knowledge_file:
            with open(knowledge_file, encoding='utf-8') as fh:
                surfaces = SD.apply_knowledge(surfaces, SD.load_knowledge(json.load(fh)))
        rep = SD.ladder_report(surfaces, target=f'{b.app} ({b.bundle_id})', notes=b.notes)
        rep['bundle'] = {'path': b.path, 'version': b.version, 'scripting': b.scripting, 'files_read': b.files_read}
    if json_output:
        click.echo(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
        return
    if rep.get('refused'):
        raise click.ClickException(rep['refused'])
    click.echo(f"Surfaces of {rep['target']}: " + ', '.join(f'{k} {n}' for k, n in sorted(
        rep['counts'].items(), key=lambda kv: SD.KIND_ORDER.get(kv[0], 99))))
    for line in rep.get('steer') or []:
        click.echo(f'  steer: {line}')
    shown = rep['surfaces'] if not limit else rep['surfaces'][:limit]
    for s in shown:
        eff = (s.get('effect') or {}).get('effect') or '-'
        cite = s.get('cite') or {}
        where = cite.get('file', '') + (f"#{cite['pointer']}" if cite.get('pointer') else '')
        click.echo(f"  [{s['tier']}] {s['kind']:<12} {s['label'][:44]:<44} effect={eff:<13} {s['interface']:<8} {where}")
    if limit and len(rep['surfaces']) > limit:
        click.echo(f"  ... {len(rep['surfaces']) - limit} more (--limit 0 for all)")
    if rep.get('best_by_effect'):
        click.echo('  best per map effect: ' + '; '.join(f"{e}: {v['kind']} {v['key']}"
                                                      for e, v in sorted(rep['best_by_effect'].items())))
    if rep.get('deferred_probes'):
        click.echo(f"  not fetched (offline): {', '.join(p['url'] for p in rep['deferred_probes'][:4])} ...")


from anyuse.execution.rea_cli import rea_commands as _rea_commands
map_registry.add_command(_rea_commands)


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _print_result(result: AgentResult, verbose: bool = False):
    """Print result in human-readable format."""
    icon = "✓" if result.success else "✗"
    
    click.echo()
    click.echo(f"{icon} {result.message}")
    
    if verbose or not result.success:
        if result.actions:
            click.echo(f"  Actions: {len(result.actions)}")
        click.echo(f"  Duration: {result.duration_seconds:.1f}s")
        
        if result.workflow_matched:
            click.echo(f"  Workflow: {result.workflow_matched}")
    
    if result.error and not result.success:
        click.echo(f"  Error: {result.error}")
    _print_rea_escalation(getattr(result, "rea_escalation", None))


def _print_rea_escalation(status):
    if status and status.get("escalate_to_rea"):
        click.echo(f"  REA mapping recommended after {status.get('vision_fallbacks', 0)} computer-use runs; "
                   "start a proof workflow with `anyuse map rea workflow init` and `anyuse map rea workflow begin`.")


def main():
    """Entry point for anyuse command."""
    cli(obj={})


if __name__ == '__main__':
    main()


# ---------------------------------------------------------------------------
# the agent harness: one open-ended run
# ---------------------------------------------------------------------------
@cli.group()
def agent():
    """The general agent harness: one open-ended task with the default tools."""
    pass


@agent.group('system-prompt')
def agent_system_prompt():
    """Inspect a system prompt or attempt one configured permission/trust choice."""


@agent_system_prompt.command('inspect')
@click.option('--json', 'json_output', is_flag=True, help='Print the read-only prompt snapshot and fingerprint.')
def agent_system_prompt_inspect(json_output):
    """Read the current prompt without pressing a button or changing permissions."""
    from anyuse.execution.system_prompt_policy import inspect_current_prompt
    result = inspect_current_prompt()
    if json_output:
        click.echo(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        click.echo(f"system prompt: {result['status']}; {result['reason']}")
        if result.get('fingerprint_sha256'):
            click.echo(f"fingerprint_sha256: {result['fingerprint_sha256']}")


@agent_system_prompt.command('resolve')
@click.option('--json', 'json_output', is_flag=True, help='Print the resolution and required next action.')
@click.option('--task', default='', help='The originating task for llm decisions; never include passwords or codes.')
def agent_system_prompt_resolve(json_output, task):
    """Attempt one configured choice; never resume a pending desktop action."""
    from anyuse.execution.system_prompt_policy import resolve_configured
    result = resolve_configured('cli.agent.system-prompt.resolve', task_context=task)
    if json_output:
        click.echo(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        click.echo(f"system prompt: {result['status']}; {result['reason']}")
        if result.get('attempted') or result.get('reobserve_required'):
            click.echo('Discard earlier targets. Observe the screen again before choosing a new action.')
    if result.get('next_action') == 'stop':
        raise SystemExit(1)


@agent.command('run')
@click.argument('task')
@click.option('--site', default='', help='A website (host or URL) to work on in the guarded container browser.')
@click.option('--app', default='', help='A desktop app (name or bundle id) to work in through the guest job agent.')
@click.option('--policy', type=click.Choice(['model', 'cerebras', 'claude', 'scripted']), default='model',
              help='The model policy: the configured model (ANYUSE_MODEL_PROVIDER, any preset; booked under the '
                   'daily cap; cerebras is its older name), the Claude CLI (no tools, an empty directory), or a '
                   'scripted JSON list of actions (offline checks).')
@click.option('--script', default='', help='The JSON action list for --policy scripted.')
@click.option('--value', 'values', multiple=True, metavar='NAME=VALUE', help='A named value for the task (repeatable).')
@click.option('--max-steps', type=int, default=0, help='Override ANYUSE_AGENT_MAX_STEPS for this run.')
@click.option('--compile', 'do_compile', is_flag=True, help='Compile a finished run into a saved tool.')
@click.option('--fresh', default='', help='Extra fresh values for a web compile\'s proof, comma-separated; tried only '
                                         'when the site\'s own pages show them (the proof takes its values from the '
                                         'site\'s own vocabulary).')
@click.option('--fresh-set', 'fresh_sets', multiple=True, metavar='NAME=VALUE,...',
              help='One fresh value set for a flow compile\'s proof (repeat >= 2 times), e.g. product=Teapot,max_total=40 EUR.')
@click.option('--json', 'json_output', is_flag=True, help='Print the report as JSON.')
def agent_run(task, site, app, policy, script, values, max_steps, do_compile, fresh, fresh_sets, json_output):
    """Run TASK through the agent harness. Open tasks: a step the task asks for runs
    (initiation = approval: a login, a form sent, a purchase under the task's own cap "at most 30 EUR"); a deviation
    stops; a step the task did not ask for is asked here at the terminal (no terminal, or
    ANYUSE_AGENT_UNASKED_IRREVERSIBLE=stop: the run stops). A password is given only as --value password=${secret:NAME}
    (the Keychain item NAME of service anyuse). The daily spend cap applies; the browser is the guarded container page
    only; the desktop goes through the guest."""
    from anyuse.execution import harness_product as HP
    vals = {}
    for item in values:
        if '=' not in item:
            raise click.UsageError(f'--value expects NAME=VALUE, got {item!r}')
        k, v = item.split('=', 1)
        vals[k.strip()] = v
    scripted = None
    if policy == 'scripted':
        if not script:
            raise click.UsageError('--policy scripted needs --script FILE')
        scripted = HP.load_script(script)
    fresh_values = [x.strip() for x in fresh.split(',') if x.strip()]
    sets = []
    for item in fresh_sets:
        one = {}
        for pair in item.split(','):
            if '=' not in pair:
                raise click.UsageError(f'--fresh-set expects NAME=VALUE,..., got {item!r}')
            k, v = pair.split('=', 1)
            one[k.strip()] = v.strip()
        sets.append(one)
    from anyuse.execution.api_permission import user_initiated
    try:
        # ANYUSE_ROBOTS=user_initiated: a run / compile the user started at the terminal
        with user_initiated("cli"):
            out = HP.run_agent_task(task, values=vals, site=site, app=app, policy=policy, script=scripted,
                                    max_steps=max_steps or None, compile=do_compile, fresh=fresh_values,
                                    fresh_sets=sets or None, asker=HP.terminal_asker(), surface="cli")
    except ValueError as exc:
        click.echo(f'✗ {exc}')
        raise SystemExit(2)
    if json_output:
        click.echo(json.dumps(out, indent=2, default=str))
    elif (out.get('refusal') or {}).get('kind') == 'no_model':
        click.echo(out.get('reason'), err=True)
    else:
        icon = '✓' if out.get('ok') else '✗'
        click.echo(f"{icon} {out.get('outcome')}: {out.get('reason')}")
        click.echo(f"  steps {out.get('steps')}  model calls {out.get('model_calls')}  cost US${out.get('cost_usd', 0):.4f}")
        if out.get('result') is not None:
            click.echo('  result: ' + json.dumps(out['result'], ensure_ascii=False, default=str)[:800])
        comp = out.get('compiled')
        if comp:
            click.echo(f"  compiled: {comp.get('name')} ({comp.get('status')})"
                       + (': ' + '; '.join(comp.get('reasons') or []) if comp.get('reasons') else ''))
        _print_rea_escalation(out.get('rea_escalation'))
    raise SystemExit(0 if out.get('ok') else 1)


@agent.command('replay')
@click.argument('name')
@click.option('--value', 'values', multiple=True, metavar='NAME=VALUE', help='A value for the flow tool (repeatable).')
@click.option('--json', 'json_output', is_flag=True, help='Print the report as JSON.')
def agent_replay(name, values, json_output):
    """Replay the saved flow tool NAME with new values (0 model calls). Started with values, it is covered by
    initiation and asks nothing but a one-time code; a deviation (another target, a higher amount than the cap
    value, a page the recording does not have) stops."""
    from anyuse.execution import harness_product as HP
    vals = {}
    for item in values:
        if '=' not in item:
            raise click.UsageError(f'--value expects NAME=VALUE, got {item!r}')
        k, v = item.split('=', 1)
        vals[k.strip()] = v
    try:
        from anyuse.execution.api_permission import user_initiated
        with user_initiated("cli"):
            out = HP.replay_flow_tool(name, vals, asker=HP.terminal_asker())
    except ValueError as exc:
        click.echo(f'✗ {exc}')
        raise SystemExit(2)
    if json_output:
        click.echo(json.dumps(out, indent=2, default=str))
    else:
        click.echo(f"{'✓' if out.get('ok') else '✗'} {out.get('outcome')}: {out.get('reason')}")
        click.echo(f"  steps {out.get('steps')}  model calls {out.get('model_calls')}")
        if out.get('result') is not None:
            click.echo('  result: ' + json.dumps(out['result'], ensure_ascii=False, default=str)[:800])
    raise SystemExit(0 if out.get('ok') else 1)


@agent.command('sandbox')
@click.option('--json', 'json_output', is_flag=True, help='Print as JSON.')
def agent_sandbox(json_output):
    """Whether a runtime-written tool could run in the container sandbox now (read-only)."""
    from anyuse.execution import harness_sandbox_runner as SR
    st = SR.status()
    if json_output:
        click.echo(json.dumps(st, indent=2))
    else:
        click.echo(f"container sandbox: {'enabled' if st['enabled'] else 'off'} ({st['setting']}); "
                   f"tart VM running: {st['tart_vm'] or 'none'}; vsock fix confirmed: {st['vsock_fix_confirmed']}; "
                   f"would start: {st['would_start']}")


# Legacy commands and ones without a public registry yet stay callable, but out of the help.
for _group, _names in ((cli, ("interactive", "guide", "account", "2fa", "context")),
                       (cli.commands.get("map"), ("push", "pull", "keygen", "export", "prove")),
                       (cli.commands.get("agent"), ("sandbox",))):
    for _name in _names:
        if _group is not None and _name in getattr(_group, "commands", {}):
            _group.commands[_name].hidden = True
