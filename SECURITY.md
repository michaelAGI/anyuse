# Security

AnyUse can drive a macOS desktop, replay web requests and, with REA, analyse and
instrument native apps. Please report security problems privately.

## Reporting

Use GitHub's **private vulnerability reporting** on this repository
(Security → Report a vulnerability). Please do not open a public issue for a
vulnerability. Include the version or commit, what you ran, and what happened.
We aim to acknowledge reports within a week.

## Scope notes

- The desktop path is meant to run in a throwaway macOS guest VM. REA refuses to
  start outside a VM unless `--allow-host` is passed for that invocation.
- Compiled web requests are read-only by default (`GET`/`HEAD`, no cookies or
  credential headers). The one exception, on by default: a search provider's public
  key that the site's own page states, read from that page on every call and never
  stored, sent with the provider's search as the page sends it (a read-only POST
  included). Logged-in replay is a separate, opt-in site mode.
- Bot walls (CAPTCHA, challenge pages, login walls, 403/429) are never bypassed;
  a report that AnyUse circumvents one is in scope.
- The design and its known limits are in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)
  and [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md).
