"""DREAMEROL FORK SYNC - Main entry point."""

import argparse
import getpass
import os
import sys
import time

from dotenv import load_dotenv
from requests.exceptions import RequestException

from auth_broker.client import (
    DEFAULT_BROKER_URL,
    BrokerAuthClient,
    BrokerAuthError,
)
from auth_broker.security import generate_polling_secret, hash_secret
from fork_sync.accounts import AccountManager
from fork_sync.github import GitHubClient
from fork_sync.synchronizer import ForkSynchronizer


AUTH_POLL_INTERVAL_SECONDS = 2.0
AUTH_TIMEOUT_SECONDS = 600.0


def build_parser():
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Safely synchronize GitHub forks with their upstream repositories."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--add-account",
        action="store_true",
        help="Add an account; its token is requested securely.",
    )
    actions.add_argument(
        "--list-accounts",
        action="store_true",
        help="List accounts saved in the system keyring.",
    )
    actions.add_argument(
        "--login",
        action="store_true",
        help="Authenticate through the auth Broker and save the credential.",
    )
    parser.add_argument(
        "--account",
        help="Use a saved account by GitHub username.",
    )
    parser.add_argument(
        "--broker-url",
        metavar="URL",
        help="Auth Broker base URL (default: BROKER_URL or http://127.0.0.1:8000).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show selected forks without changing them or prompting for confirmation.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Only select this fork (repeat to select multiple repositories).",
    )
    return parser


def filter_repositories(forks, requested_repositories):
    """Select forks by exact owner/repository name; reject unknown selections."""
    if not requested_repositories:
        return forks

    requested = {name.strip() for name in requested_repositories if name.strip()}
    available = {
        repo.get("full_name")
        for repo in forks
        if isinstance(repo, dict) and isinstance(repo.get("full_name"), str)
    }
    missing = sorted(requested - available)
    if missing:
        raise ValueError(
            "Not found among this account's forks: " + ", ".join(missing)
        )

    return [
        repo for repo in forks
        if isinstance(repo, dict) and repo.get("full_name") in requested
    ]


def confirm_synchronization(forks, input_fn=input, output=print):
    """Require an explicit typed confirmation before changing any fork."""
    if not forks:
        output("No fork repositories selected.")
        return False

    output("The following default branches may be changed:")
    for repo in forks:
        output(f" - {repo.get('full_name', 'unknown')}")
    try:
        response = input_fn("Type SYNC to continue, or anything else to cancel: ")
    except (EOFError, OSError):
        return False
    return response.strip() == "SYNC"


def _get_github_client(args, accounts):
    """Resolve the requested credential source and return a GitHub client."""
    if args.account:
        return accounts.get_client(args.account)

    token = os.getenv("GITHUB_TOKEN")
    if token:
        return GitHubClient(token)

    saved_accounts = accounts.list_accounts()
    if len(saved_accounts) == 1:
        return accounts.get_client(saved_accounts[0])
    if len(saved_accounts) > 1:
        raise ValueError(
            "More than one account is saved; select one with --account USERNAME. "
            "Saved accounts: " + ", ".join(saved_accounts)
        )
    raise ValueError(
        "No token configured. Set GITHUB_TOKEN or add an account with "
        "--login or --add-account."
    )


def _run_broker_login(
    args,
    accounts,
    *,
    auth_client=None,
    output=print,
    error=None,
    input_fn=input,
    sleep=time.sleep,
    clock=time.monotonic,
):
    """Run the Broker authorization flow, retrying Keyring storage safely."""
    if error is None:
        def error(message):
            print(message, file=sys.stderr)

    client = auth_client
    try:
        if client is None:
            broker_url = getattr(args, "broker_url", None) or os.getenv(
                "BROKER_URL",
                DEFAULT_BROKER_URL,
            )
            client = BrokerAuthClient(broker_url)

        polling_secret = generate_polling_secret()
        start = client.start(hash_secret(polling_secret))
        output("Open this URL in your browser to authorize GitHub access:")
        output(start.authorization_url)
        output("Waiting for authorization...")

        deadline = clock() + AUTH_TIMEOUT_SECONDS
        while True:
            if clock() >= deadline:
                error("Authentication timed out. Please try again.")
                return 1

            try:
                result = client.poll(start.transaction_id, polling_secret)
            except BrokerAuthError as exc:
                error(f"Authentication failed: {exc}")
                return 1

            if result.state in {"PENDING", "PROCESSING", "TOKEN_READY"}:
                sleep(AUTH_POLL_INTERVAL_SECONDS)
                continue

            if result.state == "DELIVERY_AUTHORIZED":
                if not result.access_token:
                    error(
                        "Authentication failed: the Broker did not return a credential."
                    )
                    return 1
                while True:
                    try:
                        username = accounts.add_account(result.access_token)
                        break
                    except Exception as exc:
                        error(
                            "Could not validate or store the credential in the "
                            f"system keyring: {exc}"
                        )
                        try:
                            response = input_fn(
                                "Fix the problem (for example, unlock the system "
                                "keyring), then press Enter to retry, or type "
                                "anything else to cancel: "
                            )
                        except (EOFError, OSError):
                            response = "cancel"
                        if response.strip():
                            error(
                                "The credential was not acknowledged and will expire automatically."
                            )
                            return 1

                output(f"Account '{username}' added to the system keyring.")
                try:
                    client.ack(start.transaction_id, polling_secret)
                except BrokerAuthError as exc:
                    error(
                        "Credential stored, but the Broker could not be notified: "
                        f"{exc}"
                    )
                    return 0
                return 0

            if result.state == "EXPIRED":
                error("Authentication expired. Please try again.")
                return 1
            if result.state == "FAILED":
                error("Authentication failed. Please try again.")
                return 1
            if result.state == "CANCELLED":
                error("Authentication was cancelled.")
                return 1
            if result.state == "ACKNOWLEDGED":
                error("Authentication transaction is already acknowledged.")
                return 1

            error(f"Authentication failed: unexpected state '{result.state}'.")
            return 1
    except BrokerAuthError as exc:
        error(f"Authentication failed: {exc}")
        return 1
    finally:
        if client is not None:
            client.close()


def main(argv=None):
    """Run account management or the fork synchronization workflow."""
    args = build_parser().parse_args(argv)
    load_dotenv()
    accounts = AccountManager()

    if args.login:
        return _run_broker_login(args, accounts)

    if args.add_account:
        token = getpass.getpass("GitHub token (input hidden): ").strip()
        if not token:
            print("Error: token cannot be empty.", file=sys.stderr)
            return 1
        try:
            username = accounts.add_account(token)
            print(f"Account '{username}' added to the system keyring.")
            return 0
        except Exception as error:
            print(f"Could not add account: {error}", file=sys.stderr)
            return 1

    if args.list_accounts:
        try:
            saved_accounts = accounts.list_accounts()
        except Exception as error:
            print(f"Could not read saved accounts: {error}", file=sys.stderr)
            return 1
        if not saved_accounts:
            print("No saved accounts. Add one with: python main.py --login")
        else:
            print("Saved accounts:")
            for username in saved_accounts:
                print(f" - {username}")
        return 0

    github = None
    try:
        github = _get_github_client(args, accounts)
        user = github.get_authenticated_user()
        if not isinstance(user, dict) or not user.get("login"):
            raise ValueError("GitHub returned invalid authenticated-user data.")
        print(f"\nGitHub account: {user['login']}")

        synchronizer = ForkSynchronizer(github)
        forks = filter_repositories(synchronizer.get_forks(), args.repo)
        print(f"Selected {len(forks)} fork repository/repositories.\n")

        if args.dry_run:
            results = synchronizer.preview_all(forks=forks)
        else:
            if not confirm_synchronization(forks):
                print("Synchronization cancelled.")
                return 0
            results = synchronizer.sync_all(forks=forks)

        for result in results:
            print(f"{result['repository']}: {result['status']}")
            if result.get("message"):
                print(f"  {result['message']}")
        return 1 if any(result.get("status") in {"error", "conflict"} for result in results) else 0

    except (RequestException, ValueError, KeyError, TypeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Unexpected error: {error}", file=sys.stderr)
        return 1
    finally:
        if github is not None:
            github.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
