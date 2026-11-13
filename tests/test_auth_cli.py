from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from auth_broker.client import (
    DEFAULT_BROKER_URL,
    AuthPoll,
    AuthStart,
    BrokerAuthError,
)
from auth_broker.security import hash_secret


class FakeAuthClient:
    def __init__(
        self,
        *,
        start_result=None,
        polls=None,
        start_error=None,
        ack_error=None,
        events=None,
    ):
        self.start_result = start_result or AuthStart(
            "transaction-1",
            "https://github.com/login/oauth/authorize?client_id=abc&state=xyz",
        )
        self.polls = list(polls or [])
        self.start_error = start_error
        self.ack_error = ack_error
        self.events = events if events is not None else []
        self.start_calls = []
        self.poll_calls = []
        self.ack_calls = []
        self.closed = False

    def start(self, polling_secret_hash):
        self.start_calls.append(polling_secret_hash)
        if self.start_error is not None:
            raise self.start_error
        return self.start_result

    def poll(self, transaction_id, polling_secret):
        self.poll_calls.append((transaction_id, polling_secret))
        result = self.polls.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def ack(self, transaction_id, polling_secret):
        self.ack_calls.append((transaction_id, polling_secret))
        if self.ack_error is not None:
            raise self.ack_error
        self.events.append("ack")
        return "ACKNOWLEDGED"

    def close(self):
        self.closed = True


class FakeAccounts:
    def __init__(self, *, error=None, failures=0, events=None):
        self.error = error
        self.failures = failures
        self.events = events if events is not None else []
        self.tokens = []

    def add_account(self, token):
        self.tokens.append(token)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("keyring unavailable")
        if self.error is not None:
            raise self.error
        self.events.append("store")
        return "octocat"


def test_login_stores_credential_before_ack_and_keeps_secrets_private():
    events = []
    auth = FakeAuthClient(
        polls=[
            AuthPoll("PENDING"),
            AuthPoll("DELIVERY_AUTHORIZED", "gho_secret", "bearer"),
        ],
        events=events,
    )
    accounts = FakeAccounts(events=events)
    output = []
    errors = []

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        accounts,
        auth_client=auth,
        output=output.append,
        error=errors.append,
        sleep=lambda _: None,
        clock=Mock(side_effect=[0, 0, 0]),
    )

    polling_secret = auth.poll_calls[0][1]
    assert result == 0
    assert errors == []
    assert auth.start_calls == [hash_secret(polling_secret)]
    assert accounts.tokens == ["gho_secret"]
    assert auth.ack_calls == [("transaction-1", polling_secret)]
    assert events == ["store", "ack"]
    assert auth.closed is True
    combined_output = "\n".join(output + errors)
    assert auth.start_result.authorization_url in output
    assert "Account 'octocat' added" in combined_output
    assert "gho_secret" not in combined_output
    assert polling_secret not in combined_output


def test_login_does_not_ack_when_keyring_storage_fails():
    events = []
    auth = FakeAuthClient(
        polls=[AuthPoll("DELIVERY_AUTHORIZED", "gho_secret", "bearer")],
        events=events,
    )
    accounts = FakeAccounts(error=RuntimeError("keyring unavailable"), events=events)
    output = []
    errors = []

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        accounts,
        auth_client=auth,
        output=output.append,
        error=errors.append,
        input_fn=lambda _: "cancel",
        sleep=lambda _: None,
        clock=Mock(side_effect=[0, 0]),
    )

    assert result == 1
    assert accounts.tokens == ["gho_secret"]
    assert auth.ack_calls == []
    assert events == []
    assert any("keyring" in error for error in errors)
    assert any("not acknowledged" in error for error in errors)
    assert "gho_secret" not in "\n".join(output + errors)
    assert auth.closed is True


def test_login_retries_keyring_storage_and_acks_after_success():
    events = []
    auth = FakeAuthClient(
        polls=[AuthPoll("DELIVERY_AUTHORIZED", "gho_secret", "bearer")],
        events=events,
    )
    accounts = FakeAccounts(failures=1, events=events)
    output = []
    errors = []
    prompts = []

    def input_fn(prompt):
        prompts.append(prompt)
        return ""

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        accounts,
        auth_client=auth,
        output=output.append,
        error=errors.append,
        input_fn=input_fn,
        sleep=lambda _: None,
        clock=Mock(side_effect=[0, 0]),
    )

    polling_secret = auth.poll_calls[0][1]
    assert result == 0
    assert accounts.tokens == ["gho_secret", "gho_secret"]
    assert events == ["store", "ack"]
    assert auth.ack_calls == [("transaction-1", polling_secret)]
    assert len(prompts) == 1
    assert "retry" in prompts[0]
    combined_output = "\n".join(output + errors + prompts)
    assert "gho_secret" not in combined_output
    assert polling_secret not in combined_output


def test_login_cancels_keyring_retry_when_input_is_unavailable():
    auth = FakeAuthClient(
        polls=[AuthPoll("DELIVERY_AUTHORIZED", "gho_secret", "bearer")],
    )
    accounts = FakeAccounts(failures=1)
    errors = []

    def input_fn(_):
        raise EOFError

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        accounts,
        auth_client=auth,
        output=lambda _: None,
        error=errors.append,
        input_fn=input_fn,
        sleep=lambda _: None,
        clock=Mock(side_effect=[0, 0]),
    )

    assert result == 1
    assert accounts.tokens == ["gho_secret"]
    assert auth.ack_calls == []
    assert any("not acknowledged" in error for error in errors)


def test_login_keeps_stored_credential_when_ack_fails():
    events = []
    auth = FakeAuthClient(
        polls=[AuthPoll("DELIVERY_AUTHORIZED", "gho_secret", "bearer")],
        ack_error=BrokerAuthError("Broker unavailable"),
        events=events,
    )
    accounts = FakeAccounts(events=events)
    errors = []

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        accounts,
        auth_client=auth,
        output=lambda _: None,
        error=errors.append,
        sleep=lambda _: None,
        clock=Mock(side_effect=[0, 0]),
    )

    assert result == 0
    assert events == ["store"]
    assert auth.ack_calls
    assert any("could not be notified" in error for error in errors)


def test_login_times_out_without_ack():
    auth = FakeAuthClient(polls=[AuthPoll("PENDING")])
    accounts = FakeAccounts()
    errors = []
    sleep = Mock()

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        accounts,
        auth_client=auth,
        output=lambda _: None,
        error=errors.append,
        sleep=sleep,
        clock=Mock(side_effect=[0, 0, 601]),
    )

    assert result == 1
    assert sleep.call_count == 1
    assert auth.ack_calls == []
    assert any("timed out" in error for error in errors)
    assert auth.closed is True


@pytest.mark.parametrize(
    "state",
    ["EXPIRED", "FAILED", "CANCELLED", "ACKNOWLEDGED", "UNKNOWN"],
)
def test_login_rejects_terminal_states(state):
    auth = FakeAuthClient(polls=[AuthPoll(state)])
    accounts = FakeAccounts()
    errors = []

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        accounts,
        auth_client=auth,
        output=lambda _: None,
        error=errors.append,
        sleep=lambda _: None,
        clock=Mock(side_effect=[0, 0]),
    )

    assert result == 1
    assert auth.ack_calls == []
    assert errors
    assert auth.closed is True


def test_login_reports_start_error():
    auth = FakeAuthClient(start_error=BrokerAuthError("Broker unavailable"))
    errors = []

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        FakeAccounts(),
        auth_client=auth,
        output=lambda _: None,
        error=errors.append,
        clock=Mock(),
    )

    assert result == 1
    assert any("Broker unavailable" in error for error in errors)
    assert auth.closed is True


def test_login_reports_poll_error():
    auth = FakeAuthClient(polls=[BrokerAuthError("Broker unavailable")])
    errors = []

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        FakeAccounts(),
        auth_client=auth,
        output=lambda _: None,
        error=errors.append,
        sleep=lambda _: None,
        clock=Mock(side_effect=[0, 0]),
    )

    assert result == 1
    assert any("Broker unavailable" in error for error in errors)
    assert auth.closed is True


def test_login_uses_explicit_broker_url(monkeypatch):
    captured = []

    class FailingBrokerClient:
        def __init__(self, url):
            captured.append(url)
            raise BrokerAuthError("stop")

    monkeypatch.setattr(main, "BrokerAuthClient", FailingBrokerClient)
    errors = []

    result = main._run_broker_login(
        SimpleNamespace(broker_url="https://broker.example.com"),
        FakeAccounts(),
        error=errors.append,
    )

    assert result == 1
    assert captured == ["https://broker.example.com"]


def test_login_falls_back_to_default_broker_url(monkeypatch):
    captured = []

    class FailingBrokerClient:
        def __init__(self, url):
            captured.append(url)
            raise BrokerAuthError("stop")

    monkeypatch.delenv("BROKER_URL", raising=False)
    monkeypatch.setattr(main, "BrokerAuthClient", FailingBrokerClient)
    errors = []

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        FakeAccounts(),
        error=errors.append,
    )

    assert result == 1
    assert captured == [DEFAULT_BROKER_URL]


def test_login_uses_broker_url_environment(monkeypatch):
    captured = []

    class FailingBrokerClient:
        def __init__(self, url):
            captured.append(url)
            raise BrokerAuthError("stop")

    monkeypatch.setenv("BROKER_URL", "https://env-broker.example.com")
    monkeypatch.setattr(main, "BrokerAuthClient", FailingBrokerClient)
    errors = []

    result = main._run_broker_login(
        SimpleNamespace(broker_url=None),
        FakeAccounts(),
        error=errors.append,
    )

    assert result == 1
    assert captured == ["https://env-broker.example.com"]


def test_parser_accepts_login_and_broker_url():
    args = main.build_parser().parse_args(
        ["--login", "--broker-url", "https://broker.example.com"]
    )

    assert args.login is True
    assert args.broker_url == "https://broker.example.com"


@pytest.mark.parametrize("option", ["--add-account", "--list-accounts"])
def test_login_is_mutually_exclusive_with_account_actions(option):
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(["--login", option])


def test_main_dispatches_login(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "AccountManager", lambda: "accounts")
    monkeypatch.setattr(
        main,
        "_run_broker_login",
        lambda args, accounts: calls.append((args, accounts)) or 7,
    )

    assert main.main(["--login"]) == 7
    assert calls[0][0].login is True
    assert calls[0][1] == "accounts"
