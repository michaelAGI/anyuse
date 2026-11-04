# Model adapter and configuration

Compiled replays need no model. Agent tasks, planning, visual checks and the LLM
judge do, and all their text and screenshot calls go through
`execution/model_adapter.py`. Providers are OpenAI-compatible presets, not
Python SDK integrations: the adapter posts chat completions over `httpx` and
returns the text or parsed JSON, token usage, latency, the call count and an
explicit `provider_unreachable` classification.

Any OpenAI-compatible endpoint works, for example Anthropic's or OpenAI's
OpenAI-compatible APIs or a local server: set `ANYUSE_MODEL_BASE_URL`,
`ANYUSE_MODEL_NAME` and `ANYUSE_MODEL_API_KEY_ENV`. No key at all is needed with
`ANYUSE_MODEL_PROVIDER=claude-cli` or `codex-cli`: every call then runs the
Claude Code or Codex CLI you are already logged into (see the preset table). The
general agent harness (`anyuse agent run`, MCP `agent_run`) uses the same
configured model by default (policy `model`); `--policy claude` forces the Claude
CLI (`claude -p` with no tools and none of your own Claude Code configuration).
The verifier and the fallback of saved web tools use the configured model too.

For `anyuse learn`, one setup is to install the `claude` CLI, log in, then run
`export ANYUSE_MODEL_PROVIDER=claude-cli`. It also needs the `cdp` extra and
`ANYUSE_LEARN_ENDPOINT` set to a container browser's host-reachable IP; see
[the learn setup](QUICKSTART_COMPILE.md#or-let-an-agent-find-the-page-once).

## Spend limits

- **Per run:** `ANYUSE_RUN_BUDGET_USD` (default `1.0`, about 30 Sonnet-class
  decisions). Each request reserves
  its maximum completion tokens at list price before it is sent, and every retry
  reserves again. A successful call settles to its actual usage; a sent call
  without usable accounting keeps the maximum. `unlimited` lifts the per-run
  budget.
- **Per day:** `ANYUSE_SPEND_DAILY_USD` (default US$5 per UTC day), kept in a
  spend ledger by the `anyuse` CLI and the MCP server. See
  [SETTINGS.md](SETTINGS.md), "Spend caps".

## Configuration and precedence

| variable | meaning |
|---|---|
| `ANYUSE_MODEL_PROVIDER` | `openai`, `anthropic`, `gemini`, `openrouter`, `cerebras`, `local`, `claude-cli` or `codex-cli` |
| `ANYUSE_MODEL_BASE_URL` | replaces the preset OpenAI-compatible base URL |
| `ANYUSE_MODEL_NAME` | replaces the preset model name |
| `ANYUSE_MODEL_API_KEY_ENV` | name of the environment variable containing the credential |
| `ANYUSE_MODEL_ENV_FILE` | optional path to a `NAME=value` credential file |
| `ANYUSE_MODEL_STRUCTURED_OUTPUT_MODE` | HTTP structured output: `json_schema` (default) or explicit `json_object` compatibility mode |

Each role can override these with `ANYUSE_<ROLE>_MODEL_PROVIDER`,
`ANYUSE_<ROLE>_MODEL_BASE_URL`, `ANYUSE_<ROLE>_MODEL_NAME` and
`ANYUSE_<ROLE>_MODEL_API_KEY_ENV`, where the role is `PLANNER`, `VISUAL`,
`AGENT` or `JUDGE`.

Precedence: an explicit `ModelConfig` passed in code, then the role variable,
then the global variable, then the role's default. Without any configuration
the planner and visual roles default to Gemini model names and the agent and
judge roles to Cerebras. Setting one global provider makes all four roles use
it unless a role overrides it.

For an HTTP server whose grammar compiler rejects a schema, set
`ANYUSE_AGENT_MODEL_STRUCTURED_OUTPUT_MODE=json_object` for that agent role,
or pass `ModelConfig(structured_output_mode="json_object", ...)` in code.
This requests JSON without provider schema enforcement. The REA driver still
puts its exact schema in the prompt and validates every returned decision
before dispatch; its native, evidence and proof gates still apply. The default
continues to request strict JSON Schema. There is no automatic mode change or
retry when a provider rejects a request. Other adapter callers using this
explicit mode must retain their own semantic validation.

For a local HTTP endpoint that documents a reasoning control, code can opt in
with `ModelAdapter.for_role("agent", provider="local", reasoning_effort="none")`
or `ModelConfig(reasoning_effort="none", ...)`. The default is Python `None`,
which omits the field; the string `"none"` is sent explicitly. Provider-defined
names are passed through unchanged (1–64 characters, without surrounding
whitespace or control characters), so use a value supported by that endpoint.
This option does not select a provider, change token/spend limits, relax decision
validation or enable hosted fallback. CLI providers reject an explicit value.

| preset | base URL | default credential name |
|---|---|---|
| Cerebras | `https://api.cerebras.ai/v1` | `CEREBRAS_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| local | `http://127.0.0.1:8000/v1` | none |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `GEMINI_API_KEY` |
| OpenAI | `https://api.openai.com/v1` (no default model: set `ANYUSE_MODEL_NAME`) | `OPENAI_API_KEY` |
| Anthropic | `https://api.anthropic.com/v1/` (default model `claude-sonnet-5-5`) | `ANTHROPIC_API_KEY` |
| claude-cli | none: runs `claude -p` (default model `sonnet`) | none, uses your Claude Code login |
| codex-cli | none: runs `codex exec` read-only (no default model: set `ANYUSE_MODEL_NAME`) | none, uses your Codex login |

The two CLI presets start the CLI from an empty temporary directory with the
prompt on stdin, no tools, and none of your own configuration (Claude Code runs
with `--safe-mode`, Codex with `--ignore-user-config`), and count one call per
process. `claude-cli` accepts screenshots and reports its cost; `codex-cli` is
text only, so set `ANYUSE_VISUAL_MODEL_PROVIDER` to another preset if you use it
for everything else. With `--ignore-user-config`, Codex does not read your
default model, so set `ANYUSE_MODEL_NAME` to one your Codex account offers. One
`claude -p` call costs about US$0.01–0.05 on your plan; the per-run default of
US$1.00 fits a multi-step run, and the daily cap still applies.

### Agent escalation levels

With `ANYUSE_AGENT_ESCALATION=on` the agent tier escalates through two more
roles. L1 is the plain `agent` role.

| level | role | default provider / model | completion budget | override |
|---|---|---|---|---|
| L1 | `agent` | Cerebras `gpt-oss-120b` | 1 000 tokens | `ANYUSE_AGENT_MODEL_*` |
| L2 | `agent_l2` | OpenRouter `anthropic/claude-sonnet-5` | 600 tokens | `ANYUSE_AGENT_L2_MODEL_*` |
| L3 | `agent_l3` | OpenRouter `google/gemini-3.8-flash` (with screenshots) | 2 000 tokens | `ANYUSE_AGENT_L3_MODEL_*` |

A reasoning model counts its hidden reasoning against the completion budget, so
a budget that is too small can return an empty answer. The adapter marks a
reply without answer text as `ModelResult.empty_answer` with its
`finish_reason`. The agent tier then retries once in the same step with twice
the budget; a second empty answer is `provider_empty`, which stops the loop like
`provider_unreachable` and is never a verdict.

All enabled levels draw from the same run budget. A level whose credential is
missing is skipped before the run starts and named in one INFO log entry. On
OpenRouter the adapter asks for `usage.cost` and reports it as
`TokenUsage.cost_usd`; a missing cost falls back to the level's list price.

## Retries

HTTP 408, 429 and 5xx responses are retried before a call is reported as
`provider_unreachable`: up to `ANYUSE_MODEL_RETRIES` extra attempts (default 2,
at most 5; per role `ANYUSE_<ROLE>_MODEL_RETRIES`). The adapter waits for a
numeric `Retry-After` (capped at 8 s), otherwise 1 s doubling to 8 s. HTTP 402,
other 4xx, timeouts and connection failures are not retried. Every attempt
counts: `ModelResult.llm_calls` and the agent tier's `model_calls` report
attempts, not answers, and every retry needs a fresh budget reservation. `0`
turns retries off.

## Small-model checks

Some checks can ask a small model when the site's own evidence leaves them open,
for example when several lines of a page could be the one that answers the
check. The check asks one yes/no question per candidate line, all within one
deadline (`ANYUSE_MODEL_CHECK_TIMEOUT_S`, default 10 s). A yes counts only for a
line the check then finds on the page. A no counts only when every candidate was
answered no. An error, a timeout or an unclear answer counts as no opinion, and
the check goes on as it would without a model.

Where a check asks today:

* **A chain's fitting input** (compile, `learn`, `attach`, HAR): which field of a
  search result is its name, when the compile fits a search-then-open chain on
  another result's name and the site's own answer does not show which field that
  is. The candidates are the result's own fields; without an answer, the first
  field whose values all read as names is taken
  ([COMPILE.md](COMPILE.md#chains-pages-and-path-parameters)). The automatic
  recompile does not ask.
* **An item page's values** (compile, `learn`, `attach`, HAR): which line of a proof
  case's page shows the value of a field, when neither the case's recorded response
  nor the labels on its page name it. The candidates are the page's lines that hold
  one of the case's values; a yes counts only for a line with the field's label on it
  or just above it. Without an answer, a case with no recorded response is named by
  its order on the page ([COMPILE.md](COMPILE.md#compiling-from-a-har)). The automatic
  recompile does not ask.
* **A typed search that did nothing** (a demonstration or the learn agent): which of
  several controls next to the search box submits it, and which of several search
  addresses the page shows is the site's own search. The candidates are the controls'
  labels or the page's own addresses; the pick is checked on the page before it is
  used. Without an answer, a control named like a search, or the address the page ranks
  first, is taken. The check asks only where a model check may ask: `learn` opens one
  around its agent's own run (never around the navigations, the proof or the replay it
  runs after), and a demonstration asks during its steps (the compile's demonstrator,
  and an `agent run` that a model drives). A replay or a proof never asks: it uses the
  control the demonstration recorded ([KNOWN_ISSUES.md](KNOWN_ISSUES.md#web)).
* **A drop-down option a demonstration names** in other words than the page: which of
  the drop-down's options the demonstrator meant, when no option is its text and no
  single option holds its words as whole words. The candidates are the options; without
  an answer, the first option holding its words is taken. It asks only during a
  demonstration's steps.
* **Which tool an agent run compiles to** (`agent run --compile`): whether the task
  asks for one item's fact, such as its price, rather than a list, when the run's
  answer does not show which. The candidate is the task text; without an answer, the
  task's words decide. It asks when the run is compiled, and during a run a model
  drives (an answer that lists no rows); never during a replay.
* **An empty answer** from a compiled web tool that the site's own empty page does
  not settle: whether a line that reads as an error report says the site failed to
  answer, then which of the page's lines that name your input says the search found
  nothing ([COMPILE.md](COMPILE.md#when-an-empty-result-counts)). It asks on a tool
  call, and in a heal about the page of its made-up search; never in a proof, a
  recompile or a watch run.
* **A saved web tool's result page** (a tool saved by the current version): which of the page's lines
  that name your input says the search found nothing; whether a line holding an error
  phrase says the page could not be shown, when the line is neither a result's title
  nor a line the site's empty and result pages share; which of several results that
  match your input equally closely is the product itself, not an accessory; and whether
  a short state line of the product's buy box supports its structured availability,
  when the site's own learned line does not settle it. It asks on the tool's call over
  MCP, and the call's `model_calls` and `cost_usd` count each question; never in a
  proof, a watch run, or a re-proof or a check of another input that the repair runs
  during a call. Without an answer the tool decides by the site's own evidence, then
  the word lists.
* **A heal, and a request out of service that answers again**: whether the lines of a
  result page read now (or of the request's answer) are the results of the search,
  when the list changed since the tool's last good answer and the page itself does
  not settle it. A yes counts only for a line of the page's own list, with a no for
  each other line asked about; the new request is still proved on held-out inputs
  without a model ([COMPILE.md](COMPILE.md#healing-without-a-browser)).
* **Your input inside a longer word** in Chinese, Japanese, Korean or Thai: a one- or
  two-result answer, or a "no results" line, that holds your input at the end of or
  inside a longer word (大学 in 東京大学), on a tool whose proof did not see the site
  match that way. The candidates are your input and the longer words around it at
  that place; the answer counts only for a word the text holds there
  ([COMPILE.md](COMPILE.md#the-fresh-input-proof)). Without an answer the input must
  stand on its own or start a word. It asks only on a tool call, like the empty answer.

Nothing is asked unless you configured one of these models.
`ANYUSE_MODEL_CHECKS` picks it:

| value | who answers |
|---|---|
| `auto` (default) | the first of the four below that you set up, in this order; nobody if none is set up |
| `openjev` | the [OpenJev](https://github.com/razorback16/openjev) server at `ANYUSE_OPENJEV_URL` |
| `laya` | the local [Laya](https://huggingface.co/convaiinnovations/laya) model (`pip install laya`, Apache-2.0, by Nandakishor M / Convai Innovations; [source](https://github.com/NandhaKishorM/laya)) in `ANYUSE_CONFIRMER_LAYA_DIR`, in the confirmer's container. In the MCP server the first check starts the container in the background and has no opinion; later checks use the running container. The container lives only as long as the AnyUse process, so a one-off command does not start it, and its checks have no opinion |
| `jev` | the Jev decision model on OpenRouter (optional; not publicly available; needs `OPENROUTER_API_KEY`); `auto` picks it only when `ANYUSE_JUDGE=jev` |
| `llm` | the judge role's model (`ANYUSE_JUDGE_MODEL_*`), booked in the spend ledger as `model_check`; `auto` picks it only when `ANYUSE_JUDGE=llm`. A judge role on `claude-cli` or `codex-cli` is not asked: those take longer than a check's deadline |
| `none` | nobody |

**Getting Laya.** Laya is not bundled with AnyUse (each checkpoint is about
840 MB), but one command downloads exactly the files AnyUse was tested with, each
checked against a pinned SHA-256:

```sh
python tools/confirmer/fetch_laya.py            # into ~/.anyuse/laya (about 2.7 GB)
export ANYUSE_CONFIRMER_LAYA_DIR=~/.anyuse/laya
```

It fetches the `laya`, `laya-typed-decisions` and `laya-multilingual` checkpoints
from [Hugging Face](https://huggingface.co/convaiinnovations/laya) at pinned
revisions, and Laya 0.3.20 with its dependencies (Linux arm64, Python 3.12,
CPU-only PyTorch) for the container; `--checkpoint en` limits it to one checkpoint.
AnyUse runs Laya in Apple's `container` (macOS 26 on Apple silicon); pull
`python:3.12-slim` and create a container network named `laya-offline` first. On
any other system, run Laya behind an [OpenJev](https://github.com/razorback16/openjev)
server instead (`OPENJEV_BACKEND=laya`, model `laya-1.0`) and set
`ANYUSE_OPENJEV_URL` and `ANYUSE_OPENJEV_MODEL=laya-1.0`.

A judge model that merely has a key is not enough: `auto` asks Jev or the judge
model only when `ANYUSE_JUDGE` names it. Checks ask only during a tool call, an
agent task, a compile or a heal; anywhere else, such as a watch run, they ask
nothing. Each question sends a fixed wording that names what is checked, such as
your search input (cut to 80 characters), and at most 1,200 characters of the
page. A challenge page is never sent. Each check that asks writes one line to
`model_check/log.jsonl` in the data directory: the backend, the site's host, the
scores, the answer, the calls, the time and the cost. It never holds page text
or your input.

## Credential handling

The adapter reads the named credential at call time: first from the process
environment, then from the file named by `ANYUSE_MODEL_ENV_FILE`, or
`model.env` in the AnyUse secrets directory (`~/.anyuse` unless
`ANYUSE_SECRETS_DIR` is set; `anyuse doctor` prints it). It never copies a
credential into a subprocess or container environment, puts one on a command
line, or includes one in a result, log or error. A required credential that is unset refuses before any request and counts
zero model calls. `anyuse doctor` prints provider, model and
`set`/`unset`/`not required` per role, never the value.

## Provider-specific parts

- The optional typed decision judge (`execution/judge.py`) uses OpenRouter's
  `/api/alpha/decisions` endpoint, which is not a chat-completions API. The
  judge's `llm` tier uses the shared adapter.

## Tested and untested providers

Cerebras and OpenRouter have been used live. The local and Gemini presets have
only been tested against an in-process fake server. The `claude-cli` preset
was checked with one live call; `codex-cli` only against a fake runner. Other OpenAI-compatible
endpoints work through the base URL, with three known differences that show up
as behaviour, not as errors:

- Anthropic's OpenAI-compatible layer ignores `response_format`, so structured
  output is not enforced there. The adapter still checks the answer as JSON, so
  a malformed answer fails visibly.
- OpenAI reasoning models require `max_completion_tokens`; the adapter sends
  `max_tokens`.
- Ollama prefers no explicit output cap.
