"""Tests for GitHub fork discovery and upstream synchronization."""

from unittest.mock import Mock

import pytest
import requests

from fork_sync.synchronizer import ForkSynchronizer


@pytest.fixture
def github():
    client = Mock()
    client.list_repositories.return_value = []
    client.compare_upstream.return_value = {
        "status": "behind",
        "ahead_by": 0,
        "behind_by": 3,
    }
    client.merge_upstream.return_value = None
    return client


@pytest.fixture
def synchronizer(github):
    return ForkSynchronizer(github)


@pytest.fixture
def fork():
    return {
        "full_name": "Dreamerol/AI-STUDIO",
        "fork": True,
        "default_branch": "main",
        "parent": {
            "full_name": "original/AI-STUDIO",
            "default_branch": "main",
        },
    }


@pytest.fixture
def differently_named_fork():
    return {
        "full_name": "octocat/demo-fork",
        "fork": True,
        "default_branch": "master",
        "parent": {
            "full_name": "octodemo/upstream-repo",
            "default_branch": "main",
        },
    }


def test_get_forks_returns_only_forks(synchronizer, github):
    github.list_repositories.return_value = [
        {
            "full_name": "user/fork",
            "fork": True,
            "parent": {
                "full_name": "original/fork",
                "default_branch": "main",
            },
        },
        {"full_name": "user/original", "fork": False},
        {"full_name": "user/another", "fork": True},
        {"full_name": "user/incomplete-parent", "fork": True, "parent": {}},
        "invalid",
    ]

    result = synchronizer.get_forks()

    assert len(result) == 1
    assert result[0]["full_name"] == "user/fork"


def test_get_forks_rejects_invalid_response(synchronizer, github):
    github.list_repositories.return_value = None

    with pytest.raises(ValueError):
        synchronizer.get_forks()


def test_get_forks_exposes_read_only_diagnostics(synchronizer, github):
    github.list_repositories.return_value = [
        {"full_name": "user/original", "fork": False},
        {
            "full_name": "user/fork",
            "fork": True,
            "parent": {
                "full_name": "original/fork",
                "default_branch": "main",
            },
        },
        {"full_name": "user/no-parent", "fork": True},
    ]
    github.get_repository.return_value = {
        "full_name": "user/no-parent",
        "fork": True,
        "parent": {
            "full_name": "original/no-parent",
            "default_branch": "main",
        },
    }

    result = synchronizer.get_forks()

    assert [repo["full_name"] for repo in result] == ["user/fork", "user/no-parent"]
    assert synchronizer.get_last_diagnostics() == {
        "owned_repositories": 3,
        "fork_candidates": 2,
        "valid_forks": 2,
        "unavailable_forks": 0,
    }


def test_get_forks_skips_stale_fork_with_404(synchronizer, github):
    github.list_repositories.return_value = [
        {"full_name": "user/stale", "fork": True},
        {
            "full_name": "user/fork",
            "fork": True,
            "parent": {
                "full_name": "original/fork",
                "default_branch": "main",
            },
        },
    ]
    response = Mock(status_code=404)
    github.get_repository.side_effect = requests.HTTPError("Not Found", response=response)

    result = synchronizer.get_forks()

    assert [repo["full_name"] for repo in result] == ["user/fork"]
    assert synchronizer.get_last_diagnostics()["unavailable_forks"] == 1


def test_sync_fork_when_up_to_date(synchronizer, github, fork):
    github.compare_upstream.return_value = {
        "ahead_by": 0,
        "behind_by": 0,
        "status": "identical",
    }

    result = synchronizer.sync_fork(fork)

    assert result["status"] == "up-to-date"
    assert result["repository"] == "Dreamerol/AI-STUDIO"
    github.compare_upstream.assert_called_once_with(
        fork_full_name="Dreamerol/AI-STUDIO",
        upstream_full_name="original/AI-STUDIO",
        upstream_branch="main",
        fork_branch="main",
    )
    github.merge_upstream.assert_not_called()


def test_sync_fork_when_behind_upstream_targets_fork_default_branch(
    synchronizer, github, differently_named_fork
):
    github.compare_upstream.return_value = {
        "ahead_by": 0,
        "behind_by": 3,
        "status": "behind",
    }

    result = synchronizer.sync_fork(differently_named_fork)

    assert result["status"] == "synced"
    assert result["repository"] == "octocat/demo-fork"
    assert "3 upstream commits" in result["message"]
    github.compare_upstream.assert_called_once_with(
        fork_full_name="octocat/demo-fork",
        upstream_full_name="octodemo/upstream-repo",
        upstream_branch="main",
        fork_branch="master",
    )
    github.merge_upstream.assert_called_once_with("octocat/demo-fork", "master")


def test_sync_fork_rechecks_comparison_after_preview(
    synchronizer, github, differently_named_fork
):
    github.compare_upstream.side_effect = [
        {"ahead_by": 0, "behind_by": 2, "status": "behind"},
        {"ahead_by": 1, "behind_by": 2, "status": "diverged"},
    ]

    preview = synchronizer.preview_fork(differently_named_fork)
    result = synchronizer.sync_fork(differently_named_fork)

    assert preview["status"] == "behind"
    assert result["status"] == "diverged"
    assert github.compare_upstream.call_count == 2
    github.merge_upstream.assert_not_called()


@pytest.mark.parametrize(
    "comparison, expected_status",
    [
        ({"ahead_by": 2, "behind_by": 0, "status": "ahead"}, "ahead"),
        ({"ahead_by": 2, "behind_by": 0, "status": "ahead"}, "ahead"),
        ({"ahead_by": 2, "behind_by": 3, "status": "diverged"}, "diverged"),
    ],
)
def test_sync_fork_does_not_merge_fork_with_local_commits(
    synchronizer, github, fork, comparison, expected_status
):
    github.compare_upstream.return_value = comparison

    result = synchronizer.sync_fork(fork)

    assert result["status"] == expected_status
    github.merge_upstream.assert_not_called()


@pytest.mark.parametrize(
    "comparison",
    [
        {},
        {"behind_by": 3},
        {"ahead_by": None, "behind_by": 3},
        {"ahead_by": "0", "behind_by": 3},
        {"ahead_by": -1, "behind_by": 3},
        {"ahead_by": True, "behind_by": 3},
        {"ahead_by": 0, "behind_by": None},
        {"ahead_by": 0, "behind_by": "3"},
        {"ahead_by": 0, "behind_by": -1},
        {"ahead_by": 0, "behind_by": False},
    ],
)
def test_sync_fork_rejects_invalid_comparison_counts(
    synchronizer, github, fork, comparison
):
    github.compare_upstream.return_value = comparison

    result = synchronizer.sync_fork(fork)

    assert result["status"] == "error"
    github.merge_upstream.assert_not_called()


def test_sync_fork_handles_conflict(synchronizer, github, fork):
    github.compare_upstream.return_value = {
        "ahead_by": 0,
        "behind_by": 2,
        "status": "behind",
    }
    response = Mock()
    response.status_code = 409
    error = requests.HTTPError("Merge conflict", response=response)
    github.merge_upstream.side_effect = error

    result = synchronizer.sync_fork(fork)

    assert result["status"] == "conflict"
    assert result["repository"] == "Dreamerol/AI-STUDIO"


def test_sync_fork_handles_github_api_error(synchronizer, github, fork):
    github.compare_upstream.side_effect = requests.ConnectionError("Connection failed")

    result = synchronizer.sync_fork(fork)

    assert result["status"] == "error"
    assert "Connection failed" in result["message"]
    github.merge_upstream.assert_not_called()


def test_preview_fork_reports_stale_404_without_syncing(synchronizer, github, fork):
    response = Mock(status_code=404)
    response.json.return_value = {"message": "Not Found"}
    response.text = "Not Found"
    github.compare_upstream.side_effect = requests.HTTPError(
        "Not Found", response=response
    )

    result = synchronizer.preview_fork(fork)

    assert result["status"] == "error"
    assert "HTTP 404" in result["message"]
    github.merge_upstream.assert_not_called()


def test_sync_all_continues_after_repository_error(synchronizer, github, fork):
    second_fork = {
        "full_name": "Dreamerol/PORTFOLIO",
        "fork": True,
        "default_branch": "main",
        "parent": {
            "full_name": "original/PORTFOLIO",
            "default_branch": "main",
        },
    }
    github.compare_upstream.side_effect = [
        RuntimeError("Unexpected failure"),
        {"behind_by": 2, "ahead_by": 0, "status": "behind"},
    ]

    results = synchronizer.sync_all(forks=[fork, second_fork])

    assert len(results) == 2
    assert results[0]["status"] == "error"
    assert results[1]["status"] == "synced"
    assert results[1]["repository"] == "Dreamerol/PORTFOLIO"
    github.merge_upstream.assert_called_once_with("Dreamerol/PORTFOLIO", "main")


def test_sync_all_handles_repository_discovery_error(synchronizer, github):
    github.list_repositories.side_effect = requests.ConnectionError("GitHub unavailable")

    results = synchronizer.sync_all()

    assert len(results) == 1
    assert results[0]["repository"] == "account"
    assert results[0]["status"] == "error"


def test_sync_all_dry_run_does_not_merge(synchronizer, github, fork):
    results = synchronizer.sync_all(forks=[fork], dry_run=True)

    assert results[0]["status"] == "dry-run"
    github.compare_upstream.assert_not_called()
    github.merge_upstream.assert_not_called()


def test_get_forks_ignores_truthy_non_boolean_fork_flags(synchronizer, github):
    parent = {"full_name": "original/repo", "default_branch": "main"}
    github.list_repositories.return_value = [
        {"full_name": "user/string", "fork": "true", "parent": parent},
        {"full_name": "user/integer", "fork": 1, "parent": parent},
        {"full_name": "user/valid", "fork": True, "parent": parent},
    ]

    forks = synchronizer.get_forks()

    assert [fork["full_name"] for fork in forks] == ["user/valid"]


def test_sync_fork_maps_merge_403_to_error(synchronizer, github, fork):
    github.compare_upstream.return_value = {
        "ahead_by": 0,
        "behind_by": 2,
        "status": "behind",
    }
    response = Mock()
    response.status_code = 403
    response.json.return_value = {"message": "Resource not accessible by integration"}
    response.text = "Resource not accessible by integration"
    github.merge_upstream.side_effect = requests.HTTPError("Forbidden", response=response)

    result = synchronizer.sync_fork(fork)

    assert result["status"] == "error"
    assert "HTTP 403" in result["message"]
    github.merge_upstream.assert_called_once()


def test_sync_fork_maps_merge_timeout_to_ambiguous(synchronizer, github, fork):
    merge_error = requests.Timeout("GitHub timed out")
    github.compare_upstream.return_value = {
        "ahead_by": 0,
        "behind_by": 2,
        "status": "behind",
    }
    github.merge_upstream.side_effect = merge_error

    result = synchronizer.sync_fork(fork)

    assert result["status"] == "ambiguous"
    assert "timed out" in result["message"]
    assert "Preview again before retrying" in result["message"]
    github.merge_upstream.assert_called_once()


def test_sync_fork_maps_merge_5xx_to_ambiguous(synchronizer, github, fork):
    github.compare_upstream.return_value = {
        "ahead_by": 0,
        "behind_by": 2,
        "status": "behind",
    }
    response = Mock()
    response.status_code = 503
    response.json.return_value = {"message": "Service Unavailable"}
    response.text = "Service Unavailable"
    github.merge_upstream.side_effect = requests.HTTPError("Bad Gateway", response=response)

    result = synchronizer.sync_fork(fork)

    assert result["status"] == "ambiguous"
    assert "HTTP 503" in result["message"]
    assert "Preview again before retrying" in result["message"]
    github.merge_upstream.assert_called_once()
