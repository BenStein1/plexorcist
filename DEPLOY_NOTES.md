# Deploy Notes — Production Host (FreeBSD)

Practical notes for deploying `plexorcist` to the production host after the
2026-07 AI-layer rewrite (see `REWRITE_PLAN.md` for the rewrite itself). Read
this before your next `pip install -e .` on the server if it's been a while.

## The two-step deploy

1. `bash deploy.local.sh` (locally) — rsyncs the repo to the server-mounted
   path per `.rsync-filter`. This does **not** touch dependencies or restart
   anything.
2. On the server, as root, in the deployed directory:
   - If `pyproject.toml` didn't change: just `supervisorctl restart plexorcist`.
   - If `pyproject.toml` *did* change (new/bumped dependency): run
     `./.venv/bin/pip install -e ".[dev]"` **first**, then restart. See
     "Server venv is separate" and "FreeBSD + Rust" below before doing this.

Nothing in step 1 restarts the live service — the old process keeps running
old code in memory until you restart it. That makes step 1 safe to run
anytime; step 2's `pip install` is also safe to run before restarting (it
doesn't affect the already-running old process), so there's no harm in
running both ahead of a planned restart.

## Server venv is separate from the repo

`.rsync-filter` excludes `.venv/` — the server has its own persistent virtual
environment that survives every deploy. Bumping a dependency in
`pyproject.toml` locally does **nothing** on the server until someone runs
`pip install -e .` there. Skipping this step is the #1 way a deploy looks
"successful" (rsync completes, no errors) but the restarted service immediately
crash-loops on `ImportError`.

**Use the venv's own pip explicitly**: `./.venv/bin/pip install -e ".[dev]"`,
not bare `pip`. On this box, bare `pip` in a root shell resolves to a
different Python install and will silently ignore the venv — the install
"succeeds" against the wrong interpreter and the app still won't boot.

## The server shell is csh/tcsh, not bash

Root's login shell there is `csh`. Bash syntax (`VAR=$(...)`, `[ -z "$x" ]`,
etc.) will fail with `Illegal variable name` or similar if typed directly at
the prompt. Wrap anything beyond a single plain command in `bash -c '...'`.
Also: multiline paste into that terminal is unreliable — collapse multi-step
commands into **one line** using `;` separators inside the `bash -c` string
rather than literal newlines.

## FreeBSD + Rust: the recurring build failure

The server's system Rust toolchain (`rust-1.95.0`) is broken and **cannot
build anything**. Any Python package with a compiled Rust extension (common
in the modern LLM/web-framework ecosystem: `jiter`, `pydantic-core`,
`watchfiles`, `cryptography`, `rpds-py`, and likely others in the future) will
fail during `pip install` with:

```
ld-elf.so.1: /usr/local/bin/../lib/librustc_driver-....so: Undefined symbol "_ZNSt3__122__libcpp_verbose_abortEPKcz"
error: can't find Rust compiler
```

**Root cause** (confirmed, don't re-diagnose): `librustc_driver` dynamically
links against FreeBSD's **base system** `/usr/lib/libc++.so.1` (this is
FreeBSD 13.1-RELEASE), which predates that libc++ symbol. Neither installed
LLVM port (`llvm15`, `llvm19`) ships its own `libc++.so` to redirect to via
`libmap.conf` — they both rely on the base system's libc++ too. `pkg install
-f llvm19 rust` does **not** fix it (tried; reinstalling the same broken pair
changes nothing). The actual fix would be upgrading the FreeBSD base system,
which was deliberately **not** attempted here — too invasive to do casually
on a live host. If this becomes a recurring drag, that's the real fix to
schedule deliberately.

**Working workaround** — FreeBSD's ports tree (`pkg`) ships prebuilt binary
packages for most of these (built on a working build farm, not this box).
Install the binary via `pkg`, then manually copy its files into the venv
(the venv doesn't see system `pkg`-installed packages since it isn't
`--system-site-packages`):

```sh
bash -c 'pkg install -y py311-<name> >/dev/null; VENV_SITE=$(find . -maxdepth 4 -path "*/.venv/lib/python3.11/site-packages" -type d | head -1); pkg info -l py311-<name> | grep site-packages | sed "s/^ *//" | while read f; do rel="${f#/usr/local/lib/python3.11/site-packages/}"; mkdir -p "$VENV_SITE"/$(dirname "$rel"); cp -a "$f" "$VENV_SITE"/"$rel"; done; echo COPY_DONE; ./.venv/bin/pip install -e ".[dev]"'
```

Steps, if you need to do this for a *new* package pip chokes on:

1. `pip install -e ".[dev]"` fails, ending with
   `ERROR: Failed to build '<name>' when installing build dependencies for <name>`.
2. `pkg search ^py311-<name>` — check a binary package exists.
3. Run the command above with `<name>` filled in.
4. **Check for an exact version pin.** Some packages (notably `pydantic`,
   which hard-pins its paired `pydantic-core==X.Y.Z` per release) require an
   *exact* match, and pkg's version can be one patch behind PyPI's latest. If
   step 3 still fails because pip wants a newer exact version than pkg has:
   - Download the wheel for the exact pip-requested top-level version
     (e.g. `pydantic==2.13.4`) with a **working** pip elsewhere (your local
     machine's venv works fine) and read `*.dist-info/METADATA` for the
     `Requires-Dist: <name>==...` line — don't trust PyPI's web JSON API for
     this via a summarizing web-fetch tool; it tends to mangle these into
     changelog noise. `pip download <pkg>==<version> --no-deps -d /tmp/x && unzip -p /tmp/x/*.whl "*.dist-info/METADATA" | grep Requires-Dist` is reliable.
   - Find which slightly-older top-level version pairs with the pkg-provided
     exact version, then explicitly `./.venv/bin/pip install "<pkg>==<older-version>"`
     before retrying `-e ".[dev]"` — pip won't upgrade an already-satisfying
     already-installed package on a normal install, so it'll stick.

**Already resolved in the current deployed venv** (as of the 2026-07 rewrite):
`jiter`, `watchfiles`, `pydantic-core` (pinned to `pydantic==2.13.3` /
`pydantic-core==2.46.3`, one patch behind PyPI latest), `cryptography`,
`rpds-py`. Adding or bumping a dependency later can easily reintroduce a new
instance of this same problem for a different package — same recipe applies.
