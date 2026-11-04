# Running the desktop path in a macOS VM

AnyUse's desktop actions click and type for real. Run them in a throwaway macOS
guest, not on the Mac you work on. This page collects what it takes to keep such a
guest usable, using [Tart](https://tart.run) as the example; any macOS VM works the
same way.

## 1. Start the guest detached

Start the VM so it does not die with the shell that started it:

```sh
nohup tart run automation-vm --no-graphics >> ~/tart-automation-vm.log 2>&1 < /dev/null &
```

Check that it was re-parented (`ps -o ppid= -p <pid>` prints `1`). Start it from an
unsandboxed terminal: from a sandboxed shell `tart run` fails silently (no network,
nothing in the log, while `tart list` still says "running").

## 2. Keep it awake (every boot)

A headless guest sleeps about a minute after boot, drops its network interface, and
SSH times out from then on. In the first SSH session after each boot:

```sh
sudo pmset -a sleep 0 displaysleep 0 disksleep 0 standby 0 autopoweroff 0 powernap 0
pgrep -x caffeinate >/dev/null || (nohup caffeinate -dimsu >/dev/null 2>&1 &)
```

`pmset` persists across reboots; `caffeinate` does not.

## 3. Grant the permissions once

Desktop actions need **Accessibility** and **Screen Recording** for the process that
runs them. Only a person can grant them, on the guest's screen. Grant them once,
then keep a snapshot of the guest with the grants (`tart clone`), so a reset does not
lose them. If a run reports the grants as missing, stop: without them the results are
meaningless.

The AppleScript route (Apple Events to Finder, Notes, TextEdit) needs the
**Automation** permission per app as well. macOS asks once per app ("Terminal wants
access to control Finder"); until someone clicks OK on the guest's screen, every
event is refused with error -1744 and the route falls back to Accessibility. A
person answers that prompt, not an agent. To revoke it inside the guest:
`tccutil reset AppleEvents com.apple.Terminal`.

## 4. Run jobs inside the GUI session

Code started over SSH has no window server and none of the grants. Use SSH for files
and installs only. Run desktop jobs from a process inside the guest's logged-in GUI
session (for example a Terminal window started by a LaunchAgent at login that picks
up job scripts from a folder), so the jobs inherit its permissions.

Compiled desktop tools are served by the guest job agent. Start it in that GUI
session, with a session name the host also uses (`ANYUSE_DESKTOP_GUEST_SESSION`):

```sh
python -P -m anyuse.execution.guest.desktop_e2e_job serve \
  --session "$HOME/anyuse-guest/e2e/<session>"
```

## 5. Install inside the guest

Do not import AnyUse from a shared folder: the VM's file sharing can keep serving a
stale, truncated copy of an edited file, which then fails with a `SyntaxError` that
looks like a bug. Clone (or copy) the repository into the guest and install it there,
with the guest's own Python 3.11+ (an SSH shell may find an older `python3` first):

```sh
git clone https://github.com/michaelAGI/anyuse "$HOME/anyuse" && cd "$HOME/anyuse"
python3.13 -m venv .venv && . .venv/bin/activate && pip install -e '.[desktop]'
```

Pull again after a change, never while a job is running.

## 6. Guest environment

```sh
export PYTHONDONTWRITEBYTECODE=1
export ANYUSE_DATA_DIR="$HOME/anyuse-guest/data"
export ANYUSE_SECRETS_DIR="$HOME/anyuse-guest/data/secrets"
export ANYUSE_VAULT_KEYFILE="$HOME/anyuse-guest/data/secrets/entity_vault.keys"
export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
export ANYUSE_DRIVE_ENABLED=1     # guest only: the switch that lets actions fire at all
```

## 7. Typing: turn text substitution off once

macOS autocorrect also changes typed input ("Watcher" became "Witcher"). AnyUse's
read-back catches it, but the run then fails for nothing. In the guest, once:

```sh
defaults write -g NSAutomaticSpellingCorrectionEnabled -bool false
defaults write -g NSAutomaticTextCompletionEnabled -bool false
defaults write -g NSAutomaticCapitalizationEnabled -bool false
defaults write -g NSAutomaticPeriodSubstitutionEnabled -bool false
```

Relaunch the app afterwards. Avoid `defaults write com.apple.<sandboxed app>` in the
GUI session; it can hang for minutes.

## 8. The foreground interlock

Every input AnyUse sends asks `execution/foreground_interlock.py` first. In a guest it
refuses while a screen-sharing client is connected, while the screen is locked, when
the process has no GUI session of its own (an SSH process is refused), or when any of
that cannot be read. To see the verdict without sending anything:

```sh
python -m anyuse.execution.foreground_interlock
```

On a normal Mac (not a VM) it refuses in every state. The CLI's
`anyuse credential use`, which types a vault credential for the person at the
keyboard, is not behind the interlock.

## 9. Reset

Keep a clean snapshot and never run it directly; reset by cloning it:

```sh
tart stop automation-vm
tart delete automation-vm
tart clone automation-vm-granted automation-vm
```

A clone gets a new MAC address and usually a new IP; it keeps the same SSH host key,
so check the fingerprint rather than trusting a new one blindly.

## 10. When something behaves oddly

- **`tart run` is only the controller.** The guest runs in a separate
  Virtualization process that holds the disk image. Killing the controller does not
  stop the guest; starting another one then runs two guests on one disk. Stop the VM
  with `tart stop` and wait until nothing holds the disk image
  (`lsof | grep <vm>/disk.img`). The reliable liveness test is whether the guest
  answers SSH.
- **SSH worked once, then timed out**: the guest went to sleep (see step 2).
- **The IP moved**: the host may report a stale DHCP lease; check `arp -an` against
  the VM's MAC address.
- **A queued job ran twice**: a job still queued at a reboot is run again from the
  start; treat it as not run.
- **The guest clock is off**: measure durations inside the process, not from guest
  timestamps.
- **Quit apps with `NSRunningApplication.terminate()`, not `killall`**: a killed app
  restores its windows on the next launch.
- **Search, find and address fields ignore an `AXValue` write**; they act on typed
  text. AnyUse types there and verifies the effect.
- **A posted shortcut can leave a modifier key held**; `context/keyboard.py`'s
  `release_modifiers()` clears it at the start of a job.
