# Changelog

## 1.0.1 (2026-11-08)

- Failed logins are logged with the reason: no key, an unknown key, or a key used from an address it isn't allowed from. Replies are unchanged.
- `log_output: true` writes a command's stdout and stderr to the log.
- `key generate` prints a new key and its hash without changing the config, for configs managed by another tool.

## 1.0.0 (2026-11-08)

Rewrite with an HTTP listener and a new config format.

- HTTP listener for webhooks and other clients: `POST /run/<cluster>/<command>`, with Bearer auth or an `auth_key` field in the body.
- Commands take values from the message with `${field.name}`, each validated, in JSON or `KEY=VALUE` format.
- Keys are stored as hashes, and can be limited to chosen addresses.
- Background commands, with overlapping runs merged.
- `when` conditions, which skip a request that isn't relevant to a command.
- `on_failure` command, run when a background command fails.
- `cluster add`, `key add`, `key rotate` and `key remove` commands, and config reload without a restart.
- `test` command, showing what a message would run without running it.
- HTTP request headers and optional fields with defaults, usable in commands.
- `secrets.env` for values commands need in their environment.
- Limits on connections, message size, send time, command time, output and concurrency. Commands run in an empty private directory by default, and are stopped together with everything they started on a timeout.
- A run ID in replies and in the log.

### Upgrading from 0.x

- `auth.conf` and `relays.conf` are replaced by one `config.yml`, see `config.sample.yml`. Listen addresses moved there from `--host` and `--port`, and the default address is now `127.0.0.1`.
- Create new keys with `uscmdrelay key add` rather than carrying over the old ones, which were stored in plain text.
- `--shell-exec` is gone. Move pipes and redirects into a script.
- Clients can no longer append arbitrary arguments. A command has to name what it receives: `arguments` from existing TCP clients is available as `${arguments.0}`, `${arguments.1}` and so on.
- Commands no longer run in the service's working directory. Set `cwd:` on any that depend on one.
- The TCP message format and the `status`, `message` and `code` response fields are unchanged.
