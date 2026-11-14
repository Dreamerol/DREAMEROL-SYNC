from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from requests.exceptions import HTTPError, RequestException

from api.auth import (
    SESSION_COOKIE,
    TERMINAL_STATES,
    UnknownSession,
    WebAuthError,
    WebAuthManager,
)
from fork_sync.github import GitHubClient
from fork_sync.synchronizer import ForkSynchronizer

router = APIRouter(prefix="/api", tags=["sync"])


class SyncRequest(BaseModel):
    dry_run: bool = True
    confirmation: str | None = None
    repositories: list[str] | None = None


class SyncResult(BaseModel):
    repository: str
    status: str
    message: str | None = None
    behind_by: int | None = None


class SyncResponse(BaseModel):
    account: str
    total: int
    results: list[SyncResult]


class AuthStartResponse(BaseModel):
    authorization_url: str


class AuthStatusResponse(BaseModel):
    state: str
    account: str | None = None


class LogoutResponse(BaseModel):
    state: str


def get_web_auth_manager(request: Request) -> WebAuthManager:
    return request.app.state.auth_manager


def _set_session_cookie(
    response: Response,
    request: Request,
    manager: WebAuthManager,
    session_id: str,
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=max(1, int(manager.ttl_seconds)),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def _resolve_github_token(
    request: Request,
    authorization: str | None,
    manager: WebAuthManager,
) -> str:
    if authorization is not None:
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            raise HTTPException(
                status_code=401,
                detail="Provide a GitHub token using the Bearer authorization scheme.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return token.strip()

    token = manager.token(request.cookies.get(SESSION_COOKIE))
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Sign in through the Auth Broker or provide a GitHub token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


@router.post("/auth/start", response_model=AuthStartResponse)
def start_web_authentication(
    request: Request,
    response: Response,
    manager: WebAuthManager = Depends(get_web_auth_manager),
):
    """Start a server-side Broker login for the browser session."""
    try:
        session_id, authorization_url = manager.start()
    except WebAuthError as error:
        raise HTTPException(status_code=502, detail=str(error)) from None

    _set_session_cookie(response, request, manager, session_id)
    return AuthStartResponse(authorization_url=authorization_url)


@router.get("/auth/status", response_model=AuthStatusResponse)
def web_authentication_status(
    request: Request,
    response: Response,
    manager: WebAuthManager = Depends(get_web_auth_manager),
):
    """Report whether the browser session has completed Broker login."""
    try:
        result = manager.status(request.cookies.get(SESSION_COOKIE))
    except UnknownSession:
        error_response = JSONResponse(
            status_code=401,
            content={"detail": "Sign in through the Auth Broker first."},
        )
        error_response.delete_cookie(SESSION_COOKIE, path="/")
        return error_response
    except WebAuthError as error:
        raise HTTPException(status_code=502, detail=str(error)) from None

    if result["state"] in TERMINAL_STATES:
        response.delete_cookie(SESSION_COOKIE, path="/")
    return AuthStatusResponse(**result)


@router.post("/auth/logout", response_model=LogoutResponse)
def logout_web_authentication(
    request: Request,
    response: Response,
    manager: WebAuthManager = Depends(get_web_auth_manager),
):
    """Discard the server-side browser credential."""
    manager.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return LogoutResponse(state="SIGNED_OUT")


@router.post("/sync", response_model=SyncResponse)
def sync_repositories(
    request: SyncRequest,
    http_request: Request,
    authorization: str | None = Header(default=None),
    manager: WebAuthManager = Depends(get_web_auth_manager),
):
    """Preview fork status or sync only the explicitly confirmed, lagging forks."""
    token = _resolve_github_token(http_request, authorization, manager)

    if not request.dry_run:
        if request.confirmation != "SYNC":
            raise HTTPException(
                status_code=400,
                detail='Live synchronization requires confirmation="SYNC".',
            )
        if not request.repositories:
            raise HTTPException(
                status_code=400,
                detail="Select at least one repository from the preview before syncing.",
            )
        if len(request.repositories) > 5000:
            raise HTTPException(status_code=400, detail="Too many repositories were selected.")
        if len(set(request.repositories)) != len(request.repositories):
            raise HTTPException(status_code=400, detail="Duplicate repositories were selected.")

    client = GitHubClient(token.strip())
    try:
        try:
            user = client.get_authenticated_user()
        except HTTPError as error:
            status_code = error.response.status_code if error.response is not None else None
            if status_code == 401:
                raise HTTPException(status_code=401, detail="The GitHub token is invalid.") from error
            if status_code == 403:
                raise HTTPException(
                    status_code=403,
                    detail="The GitHub token does not have sufficient permissions.",
                ) from error
            raise HTTPException(status_code=502, detail="GitHub authentication failed.") from error
        except RequestException as error:
            raise HTTPException(status_code=502, detail="Could not reach GitHub.") from error

        account = user.get("login") if isinstance(user, dict) else None
        if not isinstance(account, str) or not account.strip():
            raise HTTPException(status_code=502, detail="GitHub returned an invalid account profile.")

        synchronizer = ForkSynchronizer(client)
        if request.dry_run:
            # This performs read-only comparisons; it never submits a merge request.
            results = synchronizer.preview_all()
        else:
            try:
                current_forks = synchronizer.get_forks()
            except RequestException as error:
                raise HTTPException(status_code=502, detail="Could not load forks from GitHub.") from error

            available = {
                fork.get("full_name")
                for fork in current_forks
                if isinstance(fork, dict) and isinstance(fork.get("full_name"), str)
            }
            if any(repository not in available for repository in request.repositories):
                raise HTTPException(
                    status_code=400,
                    detail="A selected repository is no longer an available fork for this account. Preview again.",
                )

            selected_names = set(request.repositories)
            selected_forks = [
                fork for fork in current_forks
                if fork.get("full_name") in selected_names
            ]

            # Recheck status on the server so stale or edited browser requests cannot
            # cause an up-to-date fork to be sent to the merge-upstream endpoint.
            preview_by_name = {
                result["repository"]: result
                for result in (
                    synchronizer.preview_fork(fork) for fork in selected_forks
                )
            }
            behind_forks = [
                fork for fork in selected_forks
                if preview_by_name[fork["full_name"]]["status"] == "behind"
            ]
            sync_results = synchronizer.sync_all(forks=behind_forks)
            sync_by_name = {result["repository"]: result for result in sync_results}

            results = []
            for repository in request.repositories:
                preview = preview_by_name[repository]
                if preview["status"] == "behind":
                    # If the second comparison changes state, sync_fork reports the
                    # final status; otherwise this is the per-repository sync result.
                    results.append(sync_by_name.get(repository, preview))
                else:
                    results.append(preview)

        return SyncResponse(account=account, total=len(results), results=results)
    finally:
        client.session.close()
