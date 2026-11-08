# UsCmdRelay

## Description

**UsCmdRelay** is a small server that runs pre-defined commands on a host on behalf of authenticated clients. Commands are written by the admin in a config file; clients can only choose which one to run and supply values the admin has explicitly allowed.

It listens on plain TCP and on HTTP (typically for webhooks), and both follow the same rules. It is a command executor, not a remote shell, and is meant for trusted networks such as a LAN or VPN: keys and data travel unencrypted.

## Features

- TCP and HTTP listeners, with JSON or `KEY=VALUE` messages
- Values from a message placed into a command with `${field.name}`, each one validated
- Keys stored only as hashes, each limited to chosen clusters and optionally to chosen addresses
- Commands never run through a shell
- Background commands for slow work, with overlapping runs merged
- A command to run when a background command fails, for alerts
- Limits on connections, message size, time, output and concurrency
- Keys and clusters can be changed without a restart
- A dry run that shows what a message would execute

## Software requirements

- Python 3.10 or newer
- Linux

## Installation

```
pip install --upgrade <git-repo>
```

or from a checkout, without installing:

```
git clone <git-repo>
cd <git-repo>
pip install -r requirements.txt
./run.py <parameters>
```

## Quick start

Copy `config.sample.yml` to one of the locations below, as `config.yml`, and adjust it. The first file found is used, unless `--config` is given:

- `/etc/uscmdrelay/config.yml`
- `/etc/opt/uscmdrelay/config.yml`
- `~/.config/uscmdrelay/config.yml`

Then:

```
uscmdrelay cluster add my-cluster --key my-client    # prints the key, only once
uscmdrelay configtest
uscmdrelay serve
```

Add commands to the cluster by editing the config, reload, then send a request:

```
curl -X POST -H 'Authorization: Bearer <key>' -d '{"name": "app"}' http://127.0.0.1:7778/run/my-cluster/my-command
```

## Command line

```
uscmdrelay [--config FILE] [--log FILE] [--log-level LEVEL] [--version] <command>

  configtest                                  Test the configuration file
  serve                                       Start the server
  test COMMAND [--data FILE] [--header "Name: value"]
                                              Show what a message would run, without running it
  cluster add NAME [--key NAME] [--allowed-ip IP]
                                              Add an empty cluster, optionally with a key for it
  key add NAME --clusters CLUSTER [CLUSTER...] [--allowed-ip IP]
                                              Create a new key
  key rotate NAME                             Replace a key, keeping its clusters and allowed addresses
  key remove NAME                             Revoke a key
```

The `cluster` and `key` commands update the config while keeping its comments and ordering, refuse any change that would make it invalid, and tell a running service to reload. `--allowed-ip` can be repeated and accepts IPs and CIDR ranges.

`test` is the quickest way to check a template and its regexes. `--data` is a file holding the message, or `-` for stdin:

```
$ uscmdrelay test deploy::docker-rebuild --data push.json
Would run: /opt/scripts/rebuild.sh --repo=myorg/app --commit=abc123
```

When a `when` condition isn't met, `test` prints `Would skip:` and the condition.

Exit codes: `0` on success or a clean stop, `1` when the service failed or `test` found the message would be refused, `2` for startup and config errors.

## Configuration

`config.sample.yml` documents every setting. Unknown settings are errors, so a typo never goes unnoticed.

The config decides what gets executed, so it and its directory must not be writable by group or others, and must be owned by root or the user running the service. The strongest setup is root-owned and read-only to the service, so neither the service nor its commands can grant themselves access.

### Defaults

| Setting | Default |
|---|---|
| Listen address | `127.0.0.1` |
| `limits.max_connections` | 256 |
| `limits.max_connections_per_ip` | 32 |
| `limits.max_message_size` | 2 MiB |
| `limits.max_field_size` | 4,096 characters |
| Time for a client to send its message or read the reply | 10 seconds (fixed) |
| Command `timeout` | 60 seconds |
| `limits.max_output_size` | 1 MiB |
| `limits.max_running` | 16 |
| `limits.max_queued` | 64 |
| `shutdown_timeout` | 30 seconds |

### Commands

```yaml
clusters:
  deploy:
    docker-rebuild:
      cmd: /opt/scripts/rebuild.sh --repo=${repository.full_name} --commit=${after}
      fields:
        repository.full_name: '^myorg/[a-z0-9._-]+$'
        ref: '^refs/heads/main$'
      background: true
      timeout: 1800
```

`${repository.full_name}` is replaced by that field of the message. Dots walk into objects and numbers index arrays (`${commits.0.id}`). A command only receives the fields it names. The full message, without the key, is also passed on stdin.

Rules:

- The command is split into arguments when the config loads and values are put in afterwards, so a value with spaces or `;` stays inside its argument.
- A missing field, an object or a list rejects the request and nothing runs.
- Each value must match the built-in safe pattern (letters, digits and `. _ / : @ + -`, starting with a letter, digit or underscore, no `..` or `:/`) unless `fields` gives it its own regex. Regexes must match the whole value. Avoid nested repeats such as `(a+)+`: a value crafted against one can freeze the whole server.
- Anything listed under `fields` must exist and match, even if `cmd` doesn't use it. That is how a command is limited to, for example, one branch. A request that doesn't match counts as a failure for the sender, so filter on the sender's side too if it can.
- A field can have a default, which also makes it optional: `branch: {pattern: '^[a-z]+$', default: main}`.
- HTTP request headers are fields too, with lowercase names: `${headers.x-gitea-event}`. `Authorization`, `Proxy-Authorization` and `Cookie` are never available, and a message body can't supply `headers`.
- Placeholders are refused in the program position, in shell commands such as `sh -c "... ${x}"`, and as the program started by wrappers such as `sudo ${x}`. Pipes and redirects belong in a script.
- A literal `${X}` is written `$${X}`. Plain `$X` and `$$` are left alone.
- Commands run with a minimal environment (`PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`, `TZ`) plus their `env`.
- Commands run in an empty private directory, so relative paths in values reach nothing. Use `cwd:` to run a command somewhere else.
- When a command exits, its exit code is what counts. Anything it left running keeps running.

Each run is logged with its key name, source address, full command, exit code and duration, so a command should not take secrets through its arguments.

### Conditions

`when` decides whether a request is for this command at all. If any condition isn't met, the reply is 200 with status `skipped`, nothing runs, and the sender sees a successful delivery. Use `fields` to reject bad requests, and `when` to ignore ones that aren't relevant.

```yaml
docker-release:
  cmd: /opt/scripts/release.sh ${ref}
  when:
    ref: 'refs/tags/.+'                   # regex, must match the whole value
    headers.x-gitea-event: 'push'         # headers are fields too
    release.draft: {exists: false}        # absent or null
    repository.full_name: {exists: true}  # present and not null
```

All conditions must be true. They are checked after authentication and before field validation, so a skipped request can't fail on a missing `${ref}`. A missing field makes a regex condition false. Objects, lists and values over `limits.max_field_size` never match a regex, and only `exists` works on objects and lists. For "or" within one field use the regex (`'push|create'`); anything more complex belongs in a script, which gets the message on stdin.

### Secrets

Anything sensitive a command needs goes in `secrets.env` next to the config, as `KEY=VALUE` lines with permissions 600, and is referenced in `env:` values only:

```yaml
env: {REGISTRY_TOKEN: "${REGISTRY_TOKEN}"}
```

### Background commands

With `background: true` the caller gets a reply right away and failures only appear in the log. Use it for anything slower than the sender will wait; webhook senders often give up within seconds.

For each command and set of arguments there is one run in progress and at most one waiting, and the newest request replaces the waiting one. Different arguments run in parallel.

To hear about failures, set `on_failure`, at the top of the config for every command or on one command to replace it (`on_failure: null` turns it off for that command). It runs after a non-zero exit, a timeout, or a command that couldn't start, with the command's stderr on stdin. It doesn't run for commands stopped by a shutdown, and its own failures are only logged.

```yaml
on_failure: /opt/scripts/alert.sh ${run.command} ${run.exit_code} ${run.id}
```

`${run.exit_code}` is `none` when the command never started.

### Applying changes

The `cluster` and `key` commands apply themselves. After editing the config by hand:

```
uscmdrelay configtest
systemctl reload uscmdrelay
```

Keys and clusters apply at once and running commands are untouched, so a background run a revoked key had already queued still executes. If the new config is invalid it is refused and the previous one stays active. Listener and limit changes need a restart. When the service and the admin commands run as different users, set `pid_file` in the config so they find each other.

## Client usage

### HTTP

`POST /run/<cluster>/<command>` with the message as the body.

The key goes in an `Authorization: Bearer <key>` header. Clients that can't set headers can put an `auth_key` field in the body instead. When the header is present it decides alone, and keys are never accepted in a URL.

Webhook example, for a sender that can set an authorization header (in Gitea, the Authorization Header field of the webhook, with content type `application/json`): point it at `http://<host>:7778/run/deploy/docker-rebuild` and set the header to `Bearer <key>`.

### TCP

One message per connection. The key and the command are fields of the message, and the command is `name` for the `default` cluster or `cluster::name`.

JSON is one line:

```
echo '{"auth_key": "<key>", "command": "deploy::my-command", "name": "app"}' | nc <host> 7777 | jq
```

Env format is `KEY=VALUE` lines ending at a blank line or when the connection closes. Lines split on the first `=`, `#` comments are ignored, and quotes are not processed:

```
printf 'auth_key=<key>\ncommand=deploy::my-command\nname=app\n\n' | nc <host> 7777
```

A message starting with `{` is JSON, anything else is env format, and it must match the command's `format`.

### Response

Both listeners return the same JSON. On TCP it is one line.

```
{"status": "ok", "message": "...", "code": 0, "exit_code": 0, "run_id": "9f3a1c07"}
```

`status` is `ok`, `accepted`, `skipped` or `error`, and `exit_code` is only present when a command ran to completion. `run_id` appears on any reply where a command ran or was accepted, and matches the `run=` value in the log. An accepted run that a newer request replaces never runs, so its ID won't appear there. `message` is the command's stdout on success, and its stderr on failure, or `exited with code N` when stderr is empty. With `hide_output: true` it only says whether the command succeeded.

| Outcome | `code` | HTTP |
|---|---|---|
| Success | 0 | 200 |
| Accepted for background run | 0 | 202 |
| Skipped by a `when` condition | 0 | 200 |
| Authentication failed | 1000 | 401 |
| Unknown cluster | 2000 | 404 |
| Unknown command | 2001 | 404 |
| Missing or invalid field | 2002 | 422 |
| Wrong format for this command | 2003 | 400 |
| Malformed or oversized message | 2099 | 400 or 413 |
| Command failed | 3001 | 500 |
| Command timed out | 3002 | 500 |
| Too many commands running or queued, or shutting down | 3003 | 503 |
| Server error | 9999 | 500 |

A wrong key, a key without access to the cluster and a blocked address all get the same answer, whether or not the command exists.

## Systemd service

Copy `uscmdrelay.sample.service` to `/etc/systemd/system/uscmdrelay.service`, adjusting `ExecStart` to the installation method and `User=` to the account commands should run as. Then:

```
systemctl daemon-reload
systemctl enable --now uscmdrelay.service
```

Keep `KillMode=mixed` from the sample: with systemd's default, a stop sends SIGTERM to running commands at once instead of letting them finish. `TimeoutStopSec` must be longer than `shutdown_timeout` plus 15 seconds, so running commands can be stopped cleanly.

## Upgrading from 0.x

See `CHANGELOG.md`.

## Disclaimer

This software is provided as is, without any warranty. Use at your own risk. The author is not responsible for any damage caused by this software.

## License

This software is licensed under the GNU GPL v3 license. See the LICENSE file for more information.
