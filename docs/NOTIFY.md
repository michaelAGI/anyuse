# Notifications

The inbox ([HUMAN_INBOX.md](HUMAN_INBOX.md)) asks through **MCP elicitation** first. If no
client with elicitation is attached (a scheduled run, or a client without the
capability), `execution/notify.py` sends you **one short notice** through the
configured channel, instead of letting the card wait silently until someone runs
`anyuse inbox`.

Code: `execution/notify.py`. It is wired into `mcpserver/skill_tools.ask_human`
(a routine card, no elicitor) and `mcpserver/server.ask_about_block` (a direct
compiled call hit a block page, no elicitor). When a client with elicitation
is attached, nothing is sent. Tests: `test_notify.py`, offline.

## Settings (registry, `anyuse settings`)

| Setting | Values | Default | |
|---|---|---|---|
| `ANYUSE_NOTIFY_CHANNEL` | `none`, `imessage`, `webhook` | `none` | Which channel is used. `none` sends nothing. |
| `ANYUSE_NOTIFY_IMESSAGE_TO` * | one phone number or one e-mail address | unset | The iMessage recipient. Anything else is refused by the CLI and read as unset from the environment. |
| `ANYUSE_NOTIFY_WEBHOOK_URL` * | an `https://` URL; `http://` only to a loopback host; no credentials in the URL | unset | Where the webhook POSTs. An invalid value means "no notice". |

\* security-relevant: setting it needs terminal confirmation.

## What a notice says

```
AnyUse: card 0123456789ab needs a value only you know. Answer in your MCP client, or run: anyuse inbox do 0123456789ab
```

It contains:

* the card id;
* the kind of help needed;
* how to answer.

It contains no host, instruction, value, error text or secret. The webhook body is `{"schema": "anyuse.notice/1", "card", "kind",
"text", "answer_with"}`. The channel does not answer the card: answers still
go through elicitation or `anyuse inbox do <id>` behind the terminal gate.

## De-duplication and rate limit

* A card is notified **at most once**. Sent ids are kept for 14 days in
  `<ANYUSE_DATA_DIR>/notify_state.json` (0600, written atomically).
* There is at least 60 s between notices and at most 10 per rolling hour. A
  card suppressed by the limit is not lost: the next notice adds "(N more
  waiting: anyuse inbox)".
* A failed send is not marked as sent, so the card is tried again later. A
  notice never fails a run.

## iMessage: outbound only

The AppleScript is a **constant string** (`IMESSAGE_SCRIPT`), with
`on run argv`. The recipient and the text are passed as separate argv items
(`/usr/bin/osascript - <recipient> <text>`, with the script on stdin), so no
card text can become AppleScript. The recipient is validated, so a value like
`-e` or a quote-breaking string is refused before anything runs.

The runner is injected. The default runner (`default_osascript_runner`)
calls `/usr/bin/osascript`; the tests use a fake runner. The first real send
makes macOS ask whether the terminal may control Messages.

**Inbound replies are not built.** Reading replies would need Full Disk
Access to `~/Library/Messages/chat.db`.

## Webhook

A POST with a JSON body. No proxies, **no redirects followed**, 8 s timeout.
Any 2xx counts as sent. Tests use a fake poster and one exact-loopback
fixture (`ANYUSE_LOCAL_FIXTURE_ORIGINS`).
