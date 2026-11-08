# PROJECT.md

UsCmdRelay is a small server that runs pre-defined commands on a host on behalf of authenticated clients, over plain TCP and HTTP. It is a command executor, not a remote shell, and is meant for trusted networks. How to configure and use it is in `README.md`; this file is about how the project is built and changed.

## Principles

A professional tool that is useful, intuitive, secure and light, without being over-engineered. These apply to anyone changing the project.

- **Useful, not a gimmick.** Every feature solves a real problem for someone running a relay. If its value isn't obvious, it doesn't ship.
- **General purpose.** Nothing is built around one sender or product: no per-provider parsing, headers or options.
- **Intuitive.** A config should read clearly without the docs. Sensible defaults, plain wording, and errors that say what to do next. `configtest` catches everything it can before the server ever starts.
- **Secure by default.** Client data is untrusted from the first byte. See [Security rules](#security-rules).
- **Light.** Few dependencies and a small amount of code. Waiting on a command never blocks other requests.
- **Not over-engineered.** The simplest design that is correct. No abstractions, options or layers for hypothetical needs, and no guards for situations a real deployment won't hit.
- **Reuse first.** Before writing a helper or test fixture, find the existing one. One rule lives in one place; the TCP and HTTP listeners never each carry their own copy of a rule.
- **Tidy and readable.** Clear names, small functions, no clever tricks. No dead or commented-out code, and no hacks: fix the cause, not the symptom. When a change leaves something unused, it goes in the same change.
- **Respect the layers.** See [Layers](#layers).
- **A live production app.** After 1.0, the config format and the client message format are compatibility promises. Refactors keep behaviour identical.
- **Match the surrounding code.** Same naming, idiom and formatting. A one-line comment only where the code needs it; design rationale belongs in this file. No em dashes in code, docs or commits.
- **Verify honestly.** The test suite plus a real server run against real requests, on a scratch config and scratch ports, never on real config. Failures are reported as they are.

## Commands

```
pip install -r requirements.txt pytest    # in a virtualenv
python -m pytest                          # run the tests
./run.py --config ./config.yml configtest # validate a config
./run.py --config ./config.yml serve      # run the server
```

`CHANGELOG.md` holds one short section per version, with user-visible changes and upgrade notes only.

## Layers

Dependencies go one way: `listeners` → `services` → `libraries`.

- **`listeners/`** handle transport only: reading a message within the limits, and turning a result into a TCP reply or an HTTP status.
- **`services/`** hold every rule, so behaviour can't drift between listeners.
- **`libraries/`** know nothing about keys, clusters or listeners.
- **`models/`** are the pydantic config schema, one file per model, and depend on nothing else. Anything checkable from the config alone is checked here, so it fails at `configtest` rather than on the first request.
- **`manager.py`** ties it together: config loading, logging, PID file, event loop and signals. **`__init__.py`** is the command line.

Runtime dependencies stay few and pinned in `requirements.txt`, and the standard library is used where it is enough.

## Security rules

- No shell, anywhere. Commands are split into arguments once, when the config loads, and values are substituted afterwards, once, so a value can never become another argument.
- Nothing runs that the admin didn't write. Clients choose a command and supply values, and every value is validated against a whole-value regex or the built-in safe pattern.
- Keys are random tokens stored as SHA-256 hashes and compared in constant time. A fast hash is correct here because a generated token has too much entropy to brute force. Keys never appear in URLs or logs and never reach a command.
- Every failed authentication gets the same answer, regardless of what exists, so an unauthenticated client can't discover what is configured.
- With a Bearer header, authentication happens before the body is read.
- Every resource has a limit: connections in total and per address, message and field size, send time, command time, output, concurrency and queue length.
- Client-facing errors never include internal paths or exceptions; details go to the log.
- Message contents stay out of the log, except the validated values a command actually uses, because knowing what ran matters for audit.
- The service never writes its own config. Admin commands run as the admin, validate the result, and replace the file atomically.
- Encryption is out of scope by design: the relay belongs on a trusted network and warns when listening on a non-local address.

## Deliberately left out

Each of these was considered and rejected on purpose, so a proposal to add one needs a new reason.

- **Shell execution.** Joining client values into a shell string is command injection.
- **Jinja or any template engine.** Looking up a field needs a few lines of code. An engine adds a dependency, expressions that need sandboxing, and makes it harder for `configtest` to know which fields a command uses.
- **An expression language for `when`.** Conditions are a flat list of "matches" and "exists" checks that must all be true. "Or" across fields and numeric comparisons belong in a script, which gets the message on stdin.
- **Per-sender handling**, such as HMAC signatures. Senders that can't set a header would be a new decision.
- **Built-in TLS and `trusted_proxies`.** The relay runs on trusted networks and is reached directly.
- **Run-as options.** Which user runs the service and its commands is a deployment choice.
- **A database for keys.** A database file isn't encrypted, and the service would need write access to it, so a compromised command could add its own key. Hashes in a config the service can only read are safer, and the `key` commands give the same convenience.
- **Managing commands from the CLI.** A command has a template, regexes and limits, which read better in a file than as flags.
- **Health endpoint.** Monitoring can check the port or run a harmless command with a key, and nothing extra is reachable without authentication.
- **Job history, web UI, rate limiter.** Nothing needs them.

## Traps

- **Killing only the direct child isn't enough.** A script's children survive it and can hold the output pipe open, hanging the request. Commands run in their own process group, and the group is what gets signalled.
- **A command's exit and its pipes closing are separate events.** `process.wait()` can wait for the pipes on newer Python versions, and a child left running may hold them open forever.
- **`configtest` can't see every re-interpreting command.** It refuses placeholders in shells and as the program of common wrappers, but `ssh host "cmd ${x}"` and `sudo -u y ${x}` pass. The default pattern is the protection there, so loosening a field's regex on such a command needs care.
- **Field regexes run on the event loop, and Python's `re` has no timeout.** A nested repeat like `(a+)+` lets a key holder freeze the server. They are admin-written, so this is documented rather than guarded.
- **Dropping root to signal needs the real user ID changed too.** With only `seteuid`, the real ID of 0 can still signal root processes.
- **Two aiohttp details are outside its public options:** `_LimitedProtocol` wraps its handler to enforce connection limits, and `web_response.SERVER_SOFTWARE` is overwritten to hide its version. Tests cover both, so re-check them when upgrading aiohttp.
- **Requests without a Bearer header are parsed before authentication.** That code handles untrusted input, including deeply nested JSON, which raises `RecursionError` rather than a decode error.
- **YAML.** `*` must be quoted (`['*']`), and regexes belong in single quotes, since double quotes make YAML process backslashes first. Only `ruamel.yaml` keeps comments when the config is written back; PyYAML loses them.
- **Regexes.** Field regexes use `fullmatch`, so an unanchored pattern can't pass by matching part of a value.
- **Command output isn't always text.** It is decoded with replacement characters, so binary output can't turn a successful command into a server error.
- **Python 3.10 is the floor and behaves differently.** A failed write to a child's stdin is logged as "never retrieved" unless the close is awaited. Run the tests there too, since newer versions hide it. A child left running at exit can print a harmless "Event loop is closed" there; it is cosmetic and only appears on shutdown.
- **Anything an unauthenticated client can trigger must not write a log line per event.** Use `ThrottledLog`, or a flood becomes a full disk.
- **aiohttp's idle limit.** Connections that never send a request are only bounded by `keepalive_timeout`, whose default is about an hour.
- **`TimeoutStopSec` in the systemd unit** must be longer than `shutdown_timeout` plus 15 seconds, or systemd kills the service before it finishes stopping commands.
