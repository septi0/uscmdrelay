import sys
import shlex
import argparse
from uscmdrelay.manager import UsCmdRelayManager
from uscmdrelay.exceptions import ConditionNotMet, RelayError, UsCmdRelayRuntimeError
from uscmdrelay.info import __app_name__, __version__, __description__

def print_key(token: str) -> None:
    print(f'Key (shown only once, give this to the client):\n  {token}')

def print_reload_status(reloaded: bool) -> None:
    if reloaded:
        print('Running service reloaded')
    else:
        print('Could not reach a running service. If one is running, apply the change with: systemctl reload uscmdrelay')

def run_admin_command(manager: UsCmdRelayManager, args: argparse.Namespace) -> None:
    admin = manager.admin
    token = None

    if args.command == 'cluster':
        token = admin.add_cluster(args.name, key_name=args.key, allowed_ips=args.allowed_ip)
        print(f'Added cluster "{args.name}"')
    elif args.action == 'add':
        token = admin.add_key(args.name, args.clusters, args.allowed_ip)
        print(f'Added key "{args.name}"')
    elif args.action == 'rotate':
        token = admin.rotate_key(args.name)
        print(f'Replaced key "{args.name}", the previous one no longer works')
    else:
        admin.remove_key(args.name)
        print(f'Removed key "{args.name}"')

    if token:
        print_key(token)

    print_reload_status(admin.reload_service())

def read_test_data(path: str | None) -> bytes:
    if path is None:
        return b''

    if path == '-':
        return sys.stdin.buffer.read()

    with open(path, 'rb') as f:
        return f.read()

def run_test_command(manager: UsCmdRelayManager, args: argparse.Namespace) -> None:
    headers = [tuple(part.strip() for part in header.split(':', 1)) for header in args.headers]

    try:
        argv = manager.preview(args.target, read_test_data(args.data), headers)
    except ConditionNotMet as e:
        print(f'Would skip: {e}')
        return
    except RelayError as e:
        print(f'Would be refused: {e}', file=sys.stderr)
        sys.exit(1)

    print(f'Would run: {shlex.join(argv)}')

def main():
    # get args from command line
    parser = argparse.ArgumentParser(description=__description__)

    parser.add_argument('--config', dest='config_file', help='Alternative config file')
    parser.add_argument('--log', dest='log_file', help='Log file where to write logs')
    parser.add_argument('--log-level', dest='log_level', help='Log level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'])
    parser.add_argument('--version', action='version', version=f'{__app_name__} {__version__}')

    subparsers = parser.add_subparsers(title="Commands", dest="command")

    subparsers.add_parser('configtest', help='Test the configuration file')
    subparsers.add_parser('serve', help='Start the server')

    test_parser = subparsers.add_parser('test', help='Show what a message would run, without running it')
    test_parser.add_argument('target', help='The command: name for the default cluster, or cluster::name')
    test_parser.add_argument('--data', dest='data', help='File holding the message, or - for stdin')
    test_parser.add_argument('--header', dest='headers', action='append', default=[], help='HTTP header as "Name: value". Can be used multiple times')

    cluster_parser = subparsers.add_parser('cluster', help='Manage clusters')
    cluster_actions = cluster_parser.add_subparsers(title="Actions", dest="action", required=True)

    cluster_add_parser = cluster_actions.add_parser('add', help='Add an empty cluster, optionally with a key for it')
    cluster_add_parser.add_argument('name', help='Name of the cluster')
    cluster_add_parser.add_argument('--key', dest='key', help='Also create a key with this name, granted to the new cluster')
    cluster_add_parser.add_argument('--allowed-ip', dest='allowed_ip', action='append', help='IP or CIDR range the new key can be used from. Can be used multiple times')

    key_parser = subparsers.add_parser('key', help='Manage keys')
    key_actions = key_parser.add_subparsers(title="Actions", dest="action", required=True)

    key_add_parser = key_actions.add_parser('add', help='Create a new key')
    key_add_parser.add_argument('name', help='Name of the key, shown in logs')
    key_add_parser.add_argument('--clusters', dest='clusters', nargs='+', required=True, help='Clusters the key can use, or "*" for all')
    key_add_parser.add_argument('--allowed-ip', dest='allowed_ip', action='append', help='IP or CIDR range the key can be used from. Can be used multiple times')

    key_rotate_parser = key_actions.add_parser('rotate', help='Replace a key, keeping its clusters and allowed addresses')
    key_rotate_parser.add_argument('name', help='Name of the key')

    key_remove_parser = key_actions.add_parser('remove', help='Revoke a key')
    key_remove_parser.add_argument('name', help='Name of the key')

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit()

    if args.command == 'cluster' and args.allowed_ip and not args.key:
        cluster_add_parser.error('--allowed-ip requires --key')

    if args.command == 'test' and not all(':' in header for header in args.headers):
        test_parser.error('--header must have the format "Name: value"')

    # the result of a dry run is its output, not the log
    if args.command == 'test' and not args.log_level:
        args.log_level = 'CRITICAL'

    admin = args.command in ('cluster', 'key')

    # startup errors like a missing or invalid config are reported without a traceback
    try:
        manager = UsCmdRelayManager(log_file=args.log_file, log_level=args.log_level, config_file=args.config_file, admin=admin)

        if admin:
            run_admin_command(manager, args)
        elif args.command == 'configtest':
            print('Configuration file is valid')
        elif args.command == 'test':
            run_test_command(manager, args)
        elif not manager.run_forever():
            sys.exit(1)
    except (UsCmdRelayRuntimeError, OSError) as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(2)

    sys.exit(0)
