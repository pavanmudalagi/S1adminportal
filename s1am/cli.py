import argparse
import json
import sys

from .client import SentinelOneClient
from .config import Config
from .exceptions import SentinelOneError


def _print_json(data):
    print(json.dumps(data, indent=2, sort_keys=True))


def _parse_params(params_list):
    params = {}
    for item in params_list or []:
        if "=" not in item:
            raise ValueError(f"Invalid param '{item}'. Use key=value.")
        key, value = item.split("=", 1)
        params[key] = value
    return params


def build_client():
    config = Config()
    config.validate()
    return SentinelOneClient(
        base_url=config.base_url,
        api_token=config.api_token,
        api_path=config.api_path,
        timeout=config.timeout,
        verify=config.verify_ssl,
        auth_prefix=config.auth_prefix,
    )


def cmd_list_accounts(args):
    client = build_client()
    params = _parse_params(args.param)
    if args.limit is not None:
        params["limit"] = args.limit
    if args.cursor:
        params["cursor"] = args.cursor
    if args.all:
        pages = client.paginate("accounts", params=params)
        _print_json(pages)
    else:
        data = client.request("GET", "accounts", params=params)
        _print_json(data)


def cmd_get_account(args):
    client = build_client()
    data = client.request("GET", f"accounts/{args.account_id}")
    _print_json(data)


def cmd_account_policy(args):
    client = build_client()
    path = f"accounts/{args.account_id}/policy"
    if args.action == "get":
        data = client.request("GET", path)
    else:
        payload = SentinelOneClient.load_json_payload(args.payload)
        if payload is None:
            raise ValueError("--payload is required for policy update")
        data = client.request("PUT", path, payload=payload)
    _print_json(data)


def cmd_list_sites(args):
    client = build_client()
    params = _parse_params(args.param)
    if args.limit is not None:
        params["limit"] = args.limit
    if args.cursor:
        params["cursor"] = args.cursor
    if args.all:
        pages = client.paginate("sites", params=params)
        _print_json(pages)
    else:
        data = client.request("GET", "sites", params=params)
        _print_json(data)


def cmd_list_agents(args):
    client = build_client()
    params = _parse_params(args.param)
    if args.limit is not None:
        params["limit"] = args.limit
    if args.cursor:
        params["cursor"] = args.cursor
    if args.all:
        pages = client.paginate("agents", params=params)
        _print_json(pages)
    else:
        data = client.request("GET", "agents", params=params)
        _print_json(data)


def cmd_raw(args):
    client = build_client()
    params = _parse_params(args.param)
    payload = SentinelOneClient.load_json_payload(args.payload)
    data = client.request(args.method, args.path, params=params, payload=payload)
    _print_json(data)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="s1am",
        description="SentinelOne Account Management helper CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_accounts = sub.add_parser("list-accounts", help="List accounts")
    list_accounts.add_argument("--limit", type=int, default=None)
    list_accounts.add_argument("--cursor", type=str, default=None)
    list_accounts.add_argument("--param", action="append", help="Extra query params key=value")
    list_accounts.add_argument("--all", action="store_true", help="Fetch all pages if cursor is available")
    list_accounts.set_defaults(func=cmd_list_accounts)

    get_account = sub.add_parser("get-account", help="Get account by ID")
    get_account.add_argument("account_id")
    get_account.set_defaults(func=cmd_get_account)

    account_policy = sub.add_parser("account-policy", help="Get or update account policy")
    account_policy.add_argument("account_id")
    account_policy.add_argument("action", choices=["get", "update"])
    account_policy.add_argument("--payload", help="JSON payload for update")
    account_policy.set_defaults(func=cmd_account_policy)

    list_sites = sub.add_parser("list-sites", help="List sites")
    list_sites.add_argument("--limit", type=int, default=None)
    list_sites.add_argument("--cursor", type=str, default=None)
    list_sites.add_argument("--param", action="append", help="Extra query params key=value")
    list_sites.add_argument("--all", action="store_true", help="Fetch all pages if cursor is available")
    list_sites.set_defaults(func=cmd_list_sites)

    list_agents = sub.add_parser("list-agents", help="List agents")
    list_agents.add_argument("--limit", type=int, default=None)
    list_agents.add_argument("--cursor", type=str, default=None)
    list_agents.add_argument("--param", action="append", help="Extra query params key=value")
    list_agents.add_argument("--all", action="store_true", help="Fetch all pages if cursor is available")
    list_agents.set_defaults(func=cmd_list_agents)

    raw = sub.add_parser("raw", help="Call any SentinelOne API endpoint")
    raw.add_argument("method", help="HTTP method, e.g. GET, POST")
    raw.add_argument("path", help="Relative path, e.g. sites or accounts/123")
    raw.add_argument("--param", action="append", help="Query param key=value")
    raw.add_argument("--payload", help="JSON payload string")
    raw.set_defaults(func=cmd_raw)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ValueError, SentinelOneError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
