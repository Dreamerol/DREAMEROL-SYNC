const form = document.getElementById("sync-form");
const tokenInput = document.getElementById("github-token");
const previewButton = document.getElementById("preview-button");
const syncButton = document.getElementById("sync-button");
const buttonText = document.getElementById("button-text");
const statusBox = document.getElementById("sync-status");
const resultsBox = document.getElementById("sync-results");
const brokerLoginButton = document.getElementById("broker-login-button");
const logoutButton = document.getElementById("logout-button");
const brokerLink = document.getElementById("broker-link");
const authStatus = document.getElementById("auth-status");

const PENDING_AUTH_STATES = new Set(["PENDING", "PROCESSING", "TOKEN_READY"]);
const TERMINAL_AUTH_STATES = new Set(["FAILED", "CANCELLED", "EXPIRED", "ACKNOWLEDGED"]);
const LOGIN_POLL_MS = 2000;
const LOGIN_TIMEOUT_MS = 5 * 60 * 1000;

let previewReady = false;
let previewRepositories = [];
let activeToken = "";
let sessionAuthenticated = false;
let loginTimer = null;
let loginStartedAt = 0;

function setAuthStatus(message, state = "") {
    authStatus.textContent = message;
    authStatus.dataset.state = state;
}

function stopLoginPolling() {
    if (loginTimer !== null) {
        window.clearTimeout(loginTimer);
        loginTimer = null;
    }
}

function setSignedIn(account) {
    stopLoginPolling();
    sessionAuthenticated = true;
    brokerLoginButton.hidden = true;
    logoutButton.hidden = false;
    brokerLink.hidden = true;
    setAuthStatus(account ? `Signed in as ${account}.` : "Signed in.", "success");
}

function setSignedOut(message = "Not signed in.") {
    stopLoginPolling();
    sessionAuthenticated = false;
    brokerLoginButton.hidden = false;
    logoutButton.hidden = true;
    brokerLink.hidden = true;
    setAuthStatus(message, "muted");
}

function resetPreview() {
    previewReady = false;
    previewRepositories = [];
    syncButton.hidden = true;
    resultsBox.replaceChildren();
}

function renderResults(results) {
    resultsBox.replaceChildren();
    results.forEach((result) => {
        const item = document.createElement("div");
        item.className = "result-item";
        item.dataset.status = result.status;
        item.textContent = `${result.repository}: ${result.status}` +
            (Number.isInteger(result.behind_by) ? ` (${result.behind_by} behind)` : "") +
            (result.message ? ` — ${result.message}` : "");
        resultsBox.appendChild(item);
    });
}

async function requestJson(url, options = {}, fallbackMessage = "The request failed.") {
    let response;
    try {
        response = await fetch(url, {
            credentials: "same-origin",
            ...options
        });
    } catch {
        throw new Error("Could not reach the server.");
    }

    let data = null;
    try {
        data = await response.json();
    } catch {
        data = null;
    }

    if (!response.ok) {
        const detail = data && typeof data.detail === "string"
            ? data.detail
            : fallbackMessage;
        const error = new Error(detail);
        error.status = response.status;
        throw error;
    }

    if (data === null) {
        throw new Error("The server returned an invalid response.");
    }

    return data;
}

function requestSync(payload) {
    const headers = {"Content-Type": "application/json"};
    if (activeToken) {
        headers.Authorization = `Bearer ${activeToken}`;
    }
    return requestJson("/api/sync", {
        method: "POST",
        headers,
        body: JSON.stringify(payload)
    }, "Synchronization failed.");
}

function fetchAuthStatus() {
    return requestJson("/api/auth/status", {
        cache: "no-store"
    }, "Could not check GitHub sign-in status.");
}

function setBusy(isBusy, label = "Preview forks") {
    previewButton.disabled = isBusy;
    syncButton.disabled = isBusy;
    buttonText.textContent = isBusy ? label : "Preview forks";
}

async function runPreview() {
    resetPreview();
    setBusy(true, "Checking GitHub…");
    statusBox.textContent = "Checking your GitHub access and comparing forks with upstream…";

    try {
        const data = await requestSync({dry_run: true});
        renderResults(data.results);

        const behindResults = data.results.filter((result) => result.status === "behind");
        const upToDateCount = data.results.filter((result) => result.status === "up-to-date").length;
        const errorCount = data.results.filter((result) => result.status === "error").length;
        previewRepositories = behindResults.map((result) => result.repository);
        previewReady = previewRepositories.length > 0;
        syncButton.hidden = !previewReady;

        statusBox.textContent = `GitHub account: ${data.account}. ${behindResults.length} behind, ${upToDateCount} up to date` +
            (errorCount ? `, ${errorCount} could not be checked` : "") + ". No repositories were changed.";
        if (data.total === 0) {
            statusBox.textContent = `No fork repositories found for ${data.account}.`;
        }
    } catch (error) {
        if (error.status === 401 && sessionAuthenticated && !activeToken) {
            setSignedOut("Your GitHub sign-in expired. Sign in again or enter a token.");
        }
        activeToken = "";
        statusBox.textContent = error.message || "Could not connect to the server.";
    } finally {
        setBusy(false);
    }
}

async function pollLoginStatus() {
    loginTimer = null;
    if (Date.now() - loginStartedAt > LOGIN_TIMEOUT_MS) {
        setSignedOut("GitHub sign-in timed out. Try again.");
        return;
    }

    try {
        const data = await fetchAuthStatus();
        if (data.state === "AUTHENTICATED") {
            setSignedIn(data.account);
            await runPreview();
            return;
        }
        if (TERMINAL_AUTH_STATES.has(data.state)) {
            setSignedOut("GitHub sign-in did not complete. Try again.");
            return;
        }
        setAuthStatus("Waiting for GitHub authorization…", "pending");
    } catch (error) {
        if (error.status === 401) {
            setSignedOut("GitHub sign-in session expired. Try again.");
            return;
        }
        setAuthStatus("Could not check GitHub sign-in yet. Retrying…", "error");
    }

    loginTimer = window.setTimeout(pollLoginStatus, LOGIN_POLL_MS);
}

function startLoginPolling() {
    stopLoginPolling();
    loginStartedAt = Date.now();
    setAuthStatus("Waiting for GitHub authorization…", "pending");
    loginTimer = window.setTimeout(pollLoginStatus, LOGIN_POLL_MS);
}

async function restoreSession() {
    try {
        const data = await fetchAuthStatus();
        if (data.state === "AUTHENTICATED") {
            setSignedIn(data.account);
            return;
        }
        if (PENDING_AUTH_STATES.has(data.state)) {
            startLoginPolling();
            return;
        }
        setSignedOut();
    } catch (error) {
        if (error.status === 401) {
            setSignedOut();
            return;
        }
        setAuthStatus(error.message || "Could not check GitHub sign-in status.", "error");
    }
}

brokerLoginButton.addEventListener("click", async () => {
    brokerLoginButton.disabled = true;
    brokerLink.hidden = true;
    activeToken = "";
    tokenInput.value = "";
    resetPreview();
    statusBox.textContent = "Complete GitHub sign-in to preview your forks.";

    try {
        const data = await requestJson("/api/auth/start", {
            method: "POST"
        }, "Could not start GitHub sign-in.");
        brokerLink.href = data.authorization_url;
        brokerLink.hidden = false;
        window.open(data.authorization_url, "_blank", "noopener,noreferrer");
        startLoginPolling();
    } catch (error) {
        setAuthStatus(error.message || "Could not start GitHub sign-in.", "error");
    } finally {
        brokerLoginButton.disabled = false;
    }
});

logoutButton.addEventListener("click", async () => {
    logoutButton.disabled = true;
    try {
        await requestJson("/api/auth/logout", {
            method: "POST"
        }, "Could not sign out.");
    } catch {
    } finally {
        logoutButton.disabled = false;
    }

    activeToken = "";
    tokenInput.value = "";
    resetPreview();
    setSignedOut("Signed out. Sign in with GitHub or enter a personal access token.");
    statusBox.textContent = "Signed out. No repositories were changed.";
});

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = tokenInput.value.trim();
    if (!token && !sessionAuthenticated) {
        statusBox.textContent = "Sign in with GitHub or enter a personal access token.";
        tokenInput.focus();
        return;
    }

    activeToken = token;
    tokenInput.value = "";
    await runPreview();
});

syncButton.addEventListener("click", async () => {
    if (!previewReady || previewRepositories.length === 0) return;

    const confirmation = window.prompt(
        `This will synchronize ${previewRepositories.length} fork(s) shown as behind. Type SYNC to continue:`
    );
    if (confirmation !== "SYNC") {
        statusBox.textContent = "Synchronization cancelled. No repositories were changed.";
        activeToken = "";
        previewReady = false;
        previewRepositories = [];
        syncButton.hidden = true;
        return;
    }

    previewReady = false;
    syncButton.hidden = true;
    setBusy(true, "Synchronizing…");
    statusBox.textContent = "Rechecking and synchronizing only the forks that are still behind…";

    try {
        const data = await requestSync({
            dry_run: false,
            confirmation: "SYNC",
            repositories: previewRepositories
        });
        renderResults(data.results);
        const synced = data.results.filter((result) => result.status === "synced").length;
        const upToDate = data.results.filter((result) => result.status === "up-to-date").length;
        const errors = data.results.filter(
            (result) => result.status === "error" || result.status === "conflict"
        ).length;
        statusBox.textContent = `Completed for ${data.account}: ${synced} synchronized, ${upToDate} already up to date` +
            (errors ? `, ${errors} failed or conflicted` : "") + ".";
    } catch (error) {
        if (error.status === 401 && sessionAuthenticated && !activeToken) {
            setSignedOut("Your GitHub sign-in expired. Sign in again or enter a token.");
        }
        statusBox.textContent = error.message || "Could not connect to the server.";
    } finally {
        activeToken = "";
        previewRepositories = [];
        setBusy(false);
    }
});

restoreSession();
