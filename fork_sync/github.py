"""GitHub API client for repository management."""

import time

import requests


class GitHubClient:
    """Handles authenticated communication with the GitHub API."""

    BASE_URL = "https://api.github.com"
    GET_MAX_ATTEMPTS = 3
    GET_RETRY_BASE_DELAY = 0.5
    GET_RETRY_MAX_DELAY = 5.0

    def __init__(self, token: str):
        if not token:
            raise ValueError("A GitHub token is required.")

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    @staticmethod
    def _retryable_get_error(error):
        if isinstance(error, requests.exceptions.Timeout):
            return True
        if not isinstance(error, requests.exceptions.HTTPError):
            return False
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        return isinstance(status, int) and (status == 429 or status >= 500)

    @classmethod
    def _retry_delay(cls, error, attempt):
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if status == 429:
            headers = getattr(response, "headers", None) or {}
            retry_after = headers.get("Retry-After")
            try:
                return min(max(float(retry_after), 0), cls.GET_RETRY_MAX_DELAY)
            except (TypeError, ValueError):
                pass
        return min(cls.GET_RETRY_BASE_DELAY * (2 ** attempt), cls.GET_RETRY_MAX_DELAY)

    def _request(self, method: str, endpoint: str, **kwargs):
        """Send an authenticated request to GitHub."""
        is_get = method.upper() == "GET"
        attempts = self.GET_MAX_ATTEMPTS if is_get else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    f"{self.BASE_URL}{endpoint}",
                    timeout=30,
                    **kwargs,
                )
                response.raise_for_status()
            except (requests.exceptions.Timeout, requests.exceptions.HTTPError) as error:
                can_retry = is_get and self._retryable_get_error(error)
                if not can_retry or attempt == attempts - 1:
                    raise
                time.sleep(self._retry_delay(error, attempt))
                continue

            if response.status_code == 204 or not response.content:
                return None

            return response.json()

    def get_authenticated_user(self):
        """Return the authenticated GitHub account."""
        return self._request("GET", "/user")

    def get_repository(self, full_name: str):
        """Return complete repository metadata, including fork parent details."""
        if not isinstance(full_name, str) or full_name.count("/") != 1:
            raise ValueError("Repository full_name must be OWNER/REPO.")
        owner, name = full_name.split("/", 1)
        if not owner or not name:
            raise ValueError("Repository full_name must be OWNER/REPO.")
        return self._request("GET", f"/repos/{owner}/{name}")

    def list_repositories(self):
        """Return all repositories owned by the authenticated account."""
        repositories = []
        page = 1

        while True:
            batch = self._request(
                "GET",
                "/user/repos",
                params={
                    "type": "owner",
                    "sort": "updated",
                    "per_page": 100,
                    "page": page,
                },
            )

            repositories.extend(batch)

            if len(batch) < 100:
                break

            page += 1

        return repositories

    def compare_upstream(
        self,
        fork_full_name: str,
        upstream_full_name: str,
        upstream_branch: str,
        fork_branch: str,
    ):
        """Compare a fork branch with its upstream branch.

        GitHub compare API expects the `basehead` form:
        "UPSTREAM_OWNER:BRANCH...FORK_OWNER:BRANCH"
        when comparing a fork against its parent repository.
        """
        if not isinstance(fork_full_name, str) or not fork_full_name.strip():
            raise ValueError("Fork full_name is required.")
        if not isinstance(upstream_full_name, str) or not upstream_full_name.strip():
            raise ValueError("Upstream full_name is required.")
        if not isinstance(upstream_branch, str) or not upstream_branch.strip():
            raise ValueError("Upstream branch is required.")
        if not isinstance(fork_branch, str) or not fork_branch.strip():
            raise ValueError("Fork branch is required.")

        fork_owner, fork_name = fork_full_name.split("/", 1)
        upstream_owner, _ = upstream_full_name.split("/", 1)

        basehead = (
            f"{upstream_owner}:{upstream_branch.strip()}..."
            f"{fork_owner}:{fork_branch.strip()}"
        )

        return self._request(
            "GET",
            f"/repos/{fork_owner}/{fork_name}/compare/{basehead}",
        )

    def merge_upstream(self, full_name: str, branch: str):
        """Merge the upstream branch into a fork."""
        owner, name = full_name.split("/", 1)

        return self._request(
            "POST",
            f"/repos/{owner}/{name}/merge-upstream",
            json={"branch": branch},
        )
