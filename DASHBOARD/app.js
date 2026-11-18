import {
    requestJson,
    effectiveCountClass,
    formatLastSync,
    computeDashboardCounts,
    isSyncableRepository,
    isCheckError,
    computeProgress,
    finalizePreviewCounters,
    getPreviewProgressState,
    getSyncProgressState,
    dedupeRepositoriesByName,
    mergePreviewResult,
    mergePreviewError,
    getUnexpectedStates,
    getForkFlagErrors,
    getUpstreamLabel,
    summarizeSyncResults,
} from "./utils.js";

const WELCOME_URL = `${window.location.origin}/?switch=1`;
const PREVIEW_CONCURRENCY = 3;
const el = (id) => document.getElementById(id);
const userName = el("user-name");
const userAvatar = el("user-avatar");
const connectionStatus = el("connection-status");
const signoutButton = el("signout-button");
const switchAccountButton = el("switch-account-button");
const syncButton = el("sync-button");
const syncAllButton = el("sync-all-button");
const syncEligibleCount = el("sync-eligible-count");
const searchInput = el("repo-search");
const filterSelect = el("status-filter");
const statusMessage = el("status-message");
const repoList = el("repo-list");
const historyList = el("sync-history");
const previewPanel = el("preview-progress");
const previewLabel = el("preview-progress-label");
const previewCount = el("preview-progress-count");
const previewSpinner = el("preview-spinner");
const syncPanel = el("sync-progress");
const syncLabel = el("sync-progress-label");
const syncCount = el("sync-progress-count");
const syncSpinner = el("sync-spinner");
const syncCurrent = el("sync-current-info");
const statsEls = {
    total: el("stat-total"),
    upToDate: el("stat-up-to-date"),
    toBeSynced: el("stat-to-be-synced"),
    unsyncable: el("stat-unsyncable"),
    aheadNote: el("ahead-note"),
};
const summaryEls = {
    status: el("summary-status"),
    total: el("summary-total"),
    upToDate: el("summary-up-to-date"),
    toBeSynced: el("summary-to-be-synced"),
    unsyncable: el("summary-unsyncable"),
    attention: el("summary-attention"),
};
const syncSummary = el("sync-summary");
const syncSummaryEls = {
    success: el("sync-success"),
    skipped: el("sync-skipped"),
    failed: el("sync-failed"),
    manual: el("sync-manual"),
};

let repositories = [];
let selected = new Set();
let history = [];
let initialized = false;
let sessionGeneration = 0;
let activePreviewControllers = new Set();
let previewInFlight = false;
let syncInFlight = false;
let checkingSet = new Set();
let checkedCount = 0;
let previewTotal = 0;
let forkFlagErrors = [];

function isCurrent(gen) { return gen === sessionGeneration; }

function setConnectionState(text, tone = "") {
    if (!connectionStatus) return;
    connectionStatus.textContent = text;
    connectionStatus.classList.remove("is-online", "is-error");
    if (tone) connectionStatus.classList.add(tone);
}

function setCounterText(node, value, loading = false) {
    if (!node) return;
    node.textContent = loading ? "CHECKING" : String(value);
    node.classList.toggle("is-loading", !!loading);
}

function renderFinalSummary(counts, statusText) {
    setCounterText(summaryEls.total, counts.total, false);
    setCounterText(summaryEls.upToDate, counts.up_to_date, !counts.complete);
    setCounterText(summaryEls.toBeSynced, counts.to_be_synced, !counts.complete);
    setCounterText(summaryEls.unsyncable, counts.unsyncable, !counts.complete);
    if (summaryEls.status) summaryEls.status.textContent = statusText;
    const unexpected = getUnexpectedStates(repositories);
    const attention = [];
    if (forkFlagErrors.length > 0) attention.push(`${forkFlagErrors.length} item(s) with missing/invalid fork flag excluded as data errors: ${forkFlagErrors.slice(0, 5).join(", ")}.`);
    if (counts.invalid_repositories > 0) attention.push(`${counts.invalid_repositories} fork row(s) with invalid repository name excluded.`);
    if (counts.duplicates > 0) attention.push(`${counts.duplicates} duplicate fork row(s) ignored.`);
    if (counts.check_errors > 0) attention.push(`${counts.check_errors} check(s) failed or unverified; final counters are withheld.`);
    if (counts.pending > 0) attention.push(`${counts.pending} fork(s) still pending.`);
    if (unexpected.length > 0) attention.push(`Unexpected or unclassified state(s): ${unexpected.join(", ")}. Excluded as unverified.`);
    if (summaryEls.attention) {
        summaryEls.attention.hidden = attention.length === 0;
        summaryEls.attention.textContent = attention.join(" ");
    }
}

function renderSyncSummary(summary) {
    if (!syncSummary) return;
    syncSummary.hidden = false;
    if (syncSummaryEls.success) syncSummaryEls.success.textContent = String(summary.success);
    if (syncSummaryEls.skipped) syncSummaryEls.skipped.textContent = String(summary.skipped);
    if (syncSummaryEls.failed) syncSummaryEls.failed.textContent = String(summary.failed);
    if (syncSummaryEls.manual) syncSummaryEls.manual.textContent = String(summary.manual);
}

function repoRowClass(repo, effective, checking) {
    if (checking) return "is-checking";
    if (repo && isCheckError(repo)) return "is-unverified";
    if (effective === "synced") return "is-up-to-date";
    if (effective === "behind") return "is-behind";
    if (effective === "ahead") return "is-ahead";
    if (effective === "diverged") return "is-diverged";
    if (effective === "pending") return "is-pending";
    return "is-unverified";
}

function abortActivePreview() {
    for (const c of activePreviewControllers) {
        try { c.abort(); } catch {}
    }
    activePreviewControllers.clear();
}

function invalidatePendingOperations() {
    sessionGeneration += 1;
    abortActivePreview();
    previewInFlight = false;
    syncInFlight = false;
    checkingSet = new Set();
    if (typeof renderPreviewProgress === "function") {
        try { renderPreviewProgress(false); } catch {}
    }
}

function setStatus(message, tone = "") {
    statusMessage.textContent = message;
    statusMessage.classList.remove("is-success", "is-warning", "is-error", "is-loading");
    if (tone === "success") statusMessage.classList.add("is-success");
    else if (tone === "warning") statusMessage.classList.add("is-warning");
    else if (tone === "error") statusMessage.classList.add("is-error");
    else if (tone === "loading") statusMessage.classList.add("is-loading");
}

function renderPreviewProgress(active) {
    const running = !!active && previewInFlight && isCurrent(sessionGeneration);
    if (previewPanel) {
        previewPanel.hidden = !running;
        previewPanel.classList.toggle("is-active", running);
    }
    if (previewSpinner) previewSpinner.classList.toggle("is-stopped", !running);
    const loadingList = previewTotal === 0;
    const pendingCount = Math.max(0, previewTotal - checkedCount);
    if (previewLabel) previewLabel.textContent = loadingList ? "Loading fork list" : "Checking forks";
    if (previewCount) previewCount.textContent = loadingList ? "Loading fork list..." : `${checkedCount} / ${previewTotal} checked, ${pendingCount} pending`;
}

function finalizePreviewUi(gen) {
    if (!isCurrent(gen)) return;
    previewInFlight = false;
    checkingSet = new Set();
    const finalized = finalizePreviewCounters({ checked: checkedCount, total: previewTotal });
    checkedCount = finalized.checked;
    setBusyUI();
    renderPreviewProgress(false);
    renderRepos();
    updateStats();
    updateSelectionUI();
}

function renderSyncProgress({ total = 0, processed = 0, active = false, current = "" } = {}) {
    const state = getSyncProgressState({ total, processed, active });
    if (syncPanel) {
        syncPanel.hidden = !(active || total > 0);
        syncPanel.classList.toggle("is-active", !!active);
    }
    if (syncSpinner) syncSpinner.classList.toggle("is-stopped", !active);
    if (syncLabel) syncLabel.textContent = state.label;
    if (syncCount) syncCount.textContent = `${state.processed} / ${state.total}`;
    if (syncCurrent) syncCurrent.textContent = current || (active ? "Processing selected repositories..." : (total > 0 ? "Sync processing completed." : ""));
}

function setBusyUI() {
    const busy = previewInFlight || syncInFlight;
    if (switchAccountButton) switchAccountButton.disabled = syncInFlight;
    signoutButton.disabled = syncInFlight;
    if (busy) {
        syncButton.disabled = true;
        syncAllButton.disabled = true;
    }
}

function renderHistory() {
    renderResults(history.slice(0, 1).map((h) => ({ repository: h.time, status: h.summary, message: "" })), historyList);
}

function syncableRepositories() {
    return repositories.filter((r) => isSyncableRepository(r));
}

function updateSelectionUI() {
    const syncable = syncableRepositories();
    const selectedSyncable = syncable.filter((r) => selected.has(r.repository)).length;
    const idle = !previewInFlight && !syncInFlight;
    syncButton.disabled = !idle || selectedSyncable === 0;
    syncAllButton.disabled = !idle || syncable.length === 0;
    if (syncEligibleCount) {
        syncEligibleCount.textContent = syncable.length === 0
            ? "No READY TO SYNC forks"
            : `${syncable.length} READY TO SYNC fork${syncable.length === 1 ? "" : "s"}`;
    }
    syncButton.setAttribute("aria-label", `SYNC CHOSEN ${selectedSyncable} selected fork(s)`);
    syncAllButton.setAttribute("aria-label", `SYNC ALL ${syncable.length} eligible fork(s)`);
}

function resetDashboardState() {
    repositories = [];
    selected = new Set();
    history = [];
    checkedCount = 0;
    previewTotal = 0;
    checkingSet = new Set();
    forkFlagErrors = [];
    userName.textContent = "Loading...";
    userAvatar.hidden = true;
    userAvatar.removeAttribute("src");
    setConnectionState("Connecting...");
    if (syncSummary) syncSummary.hidden = true;
    renderPreviewProgress(false);
    renderSyncProgress({ total: 0, processed: 0, active: false, current: "" });
    if (syncPanel) syncPanel.hidden = true;
    updateStats();
    renderRepos();
    renderHistory();
    setBusyUI();
    updateSelectionUI();
}

function updateStats() {
    const counts = computeDashboardCounts(repositories);
    const showFinal = !previewInFlight && counts.complete;
    setCounterText(statsEls.total, counts.total, false);
    setCounterText(statsEls.upToDate, counts.up_to_date, !showFinal);
    setCounterText(statsEls.toBeSynced, counts.to_be_synced, !showFinal);
    setCounterText(statsEls.unsyncable, counts.unsyncable, !showFinal);
    if (statsEls.aheadNote) {
        statsEls.aheadNote.hidden = counts.ahead === 0;
        statsEls.aheadNote.textContent = counts.ahead > 0
            ? `${counts.ahead} AHEAD-only fork(s) included in UNSYNCABLE; not auto-synced.`
            : "";
    }
    let summaryText = "Checking starts automatically after sign-in.";
    if (previewInFlight && previewTotal > 0) {
        const pendingCount = Math.max(0, previewTotal - checkedCount);
        summaryText = `Checking forks: ${checkedCount} of ${previewTotal} complete, ${pendingCount} pending.`;
        setStatus(`Checking forks: ${checkedCount} of ${previewTotal} complete, ${pendingCount} pending — no repositories were modified.`, "loading");
    } else if (previewInFlight) {
        summaryText = "Loading fork list — no repositories will be modified.";
    } else if (repositories.length) {
        const unverified = counts.pending + counts.check_errors;
        summaryText = counts.complete
            ? `${counts.total} forks checked; no GitHub repositories were modified.`
            : `${counts.total} forks listed, ${unverified} check(s) unverified; final counters withheld. No GitHub repositories were modified.`;
    }
    renderFinalSummary(counts, summaryText);
}

function filteredRepos() {
    const q = (searchInput.value || "").toLowerCase();
    const f = filterSelect.value;
    return repositories.filter((r) => {
        const matchesQ = !q || (r.repository || "").toLowerCase().includes(q);
        const c = effectiveCountClass(r);
        let matchesF = false;
        if (f === "all") matchesF = true;
        else if (f === "up-to-date") matchesF = c === "synced";
        else if (f === "to-be-synced") matchesF = c === "behind";
        else if (f === "unsyncable") matchesF = c === "diverged" || c === "ahead";
        else if (f === "ahead-only") matchesF = c === "ahead";
        return matchesQ && matchesF;
    });
}

function renderRepos() {
    repoList.replaceChildren();
    const statusLabels = {
        "behind": "READY TO SYNC",
        "up-to-date": "UP TO DATE",
        "synced": "UP TO DATE",
        "ahead": "UNSYNCABLE (AHEAD-ONLY)",
        "diverged": "UNSYNCABLE",
        "error": "UNVERIFIED",
        "conflict": "UNVERIFIED",
        "unknown": "UNVERIFIED",
        "pending": "PENDING",
        "skipped": "UNVERIFIED",
    };
    for (const repo of filteredRepos()) {
        const li = document.createElement("li");
        const effective = effectiveCountClass(repo);
        const isChecking = previewInFlight && checkingSet.has(repo.repository);
        const checkFailed = !isChecking && isCheckError(repo);
        const lastKnown = checkFailed && !!repo.preview_error && effective !== "error" && effective !== "unknown";
        const confirmedLabel = statusLabels[effective] || statusLabels[repo.status] || "PENDING";
        li.className = `repo-row ${repoRowClass(repo, effective, isChecking)}`;
        const selectCell = document.createElement("span");
        selectCell.className = "repo-select";
        if (isSyncableRepository(repo)) {
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.checked = selected.has(repo.repository);
            cb.setAttribute("aria-label", `Select ${repo.repository} for SYNC`);
            cb.addEventListener("change", () => {
                if (cb.checked) selected.add(repo.repository); else selected.delete(repo.repository);
                updateSelectionUI();
            });
            selectCell.appendChild(cb);
        }
        li.appendChild(selectCell);
        const name = document.createElement("span");
        name.className = "repo-name";
        name.textContent = repo.repository || "unknown";
        const status = document.createElement("span");
        status.className = "repo-status";
        if (isChecking) status.textContent = "CHECKING";
        else if (lastKnown) status.textContent = `${confirmedLabel} (LAST KNOWN)`;
        else status.textContent = confirmedLabel;
        if (effective === "ahead") status.title = "AHEAD-ONLY is included in UNSYNCABLE and is never auto-synced.";
        else if (effective === "diverged") status.title = "DIVERGED forks are included in UNSYNCABLE and are never auto-synced.";
        else if (checkFailed) status.title = lastKnown
            ? "Check failed; showing last known classification. Final counters are incomplete."
            : "Check failed or unverified; excluded from confirmed counters.";
        const meta = document.createElement("span");
        meta.className = "repo-meta";
        const deltas = [];
        if (Number.isInteger(repo.ahead_by)) deltas.push(`${repo.ahead_by} ahead`);
        if (Number.isInteger(repo.behind_by)) deltas.push(`${repo.behind_by} behind`);
        const messageText = !isChecking ? (checkFailed ? repo.preview_error : (repo.message || "")) : "";
        meta.textContent = `${deltas.length ? `(${deltas.join(", ")})` : "—"}${messageText ? ` - ${messageText}` : ""}`;
        const upstream = document.createElement("span");
        upstream.className = "repo-meta repo-upstream";
        upstream.textContent = getUpstreamLabel(repo) || "—";
        const syncInfo = document.createElement("span");
        syncInfo.className = "repo-meta repo-last-sync";
        syncInfo.textContent = formatLastSync(repo.last_successful_sync);
        li.append(name, status, meta, upstream, syncInfo);
        repoList.appendChild(li);
    }
    updateSelectionUI();
}

function renderResults(results, target) {
    target.replaceChildren();
    for (const r of results || []) {
        const li = document.createElement("li");
        li.textContent = `${r.repository}: ${r.status}${r.message ? ` - ${r.message}` : ""}`;
        target.appendChild(li);
    }
}

function addHistory(entry) {
    history.unshift({ time: new Date().toLocaleTimeString(), ...entry });
    renderHistory();
}

async function ensureAuth(gen = sessionGeneration) {
    try {
        const data = await requestJson("/api/auth/status", { cache: "no-store" }, "Not signed in.");
        if (!isCurrent(gen)) return null;
        if (data.state !== "AUTHENTICATED") throw new Error("Not authenticated");
        userName.textContent = data.account || "unknown";
        setConnectionState("GitHub Connected", "is-online");
        if (data.avatar_url) {
            userAvatar.src = data.avatar_url;
            userAvatar.hidden = false;
        } else {
            userAvatar.hidden = true;
            userAvatar.removeAttribute("src");
        }
        return data;
    } catch {
        setConnectionState("GitHub Signed out", "is-error");
        if (isCurrent(gen)) window.location.assign(WELCOME_URL);
        return null;
    }
}

async function preview({ auto = false } = {}) {
    if (syncInFlight) return;
    if (previewInFlight) return;
    const gen = sessionGeneration;
    previewInFlight = true;
    abortActivePreview(); // abort only leftover/stale controllers; clears set
    checkedCount = 0;
    previewTotal = 0;
    checkingSet = new Set();
    forkFlagErrors = [];
    selected = new Set();
    renderSyncProgress({ total: 0, processed: 0, active: false, current: "" });
    if (syncPanel) syncPanel.hidden = true;
    setBusyUI();
    renderPreviewProgress(true);
    setStatus("Loading fork list — no repositories will be modified.", "loading");
    try {
        const listController = new AbortController();
        activePreviewControllers.add(listController);
        const listData = await requestJson(
            "/api/sync",
            { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dry_run: true, mode: "list" }), signal: listController.signal },
            "Check failed.",
        );
        activePreviewControllers.delete(listController);
        if (!isCurrent(gen)) return;

        const listRows = Array.isArray(listData.results) ? listData.results : [];
        forkFlagErrors = getForkFlagErrors(listRows);
        repositories = dedupeRepositoriesByName(
            listRows
                .filter((r) => r && r.fork === true && typeof r.repository === "string" && r.repository.includes("/"))
                .map((r) => ({
                    repository: r.repository,
                    status: "pending",
                    fork: r.fork,
                    ahead_by: r.ahead_by ?? null,
                    behind_by: r.behind_by ?? null,
                    message: r.message ?? null,
                    parent: r.parent ?? null,
                    upstream: r.upstream ?? null,
                    upstream_full_name: r.upstream_full_name ?? null,
                    last_successful_sync: r.last_successful_sync ?? null,
                }))
        );
        previewTotal = repositories.length;
        checkedCount = 0;
        checkingSet = new Set();
        if (listData.account) userName.textContent = listData.account;
        updateStats();
        renderRepos();
        renderPreviewProgress(true);
        updateSelectionUI();
        setStatus(`Checking ${previewTotal} forks — no repositories will be modified.`, "loading");

        const queue = repositories.map((_, i) => i);

        async function checkOne(index) {
            if (!isCurrent(gen)) return;
            const repo = repositories[index];
            if (!repo || !repo.repository) return;
            const targetName = repo.repository;
            checkingSet.add(targetName);
            if (isCurrent(gen)) renderRepos();
            const controller = new AbortController();
            activePreviewControllers.add(controller);
            let completedForCurrent = false;
            try {
                const data = await requestJson(
                    "/api/sync",
                    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dry_run: true, repositories: [targetName] }), signal: controller.signal },
                    "Check failed.",
                );
                if (!isCurrent(gen)) return;
                // guard against stale/mismatched responses by repository name
                if (!repositories[index] || repositories[index].repository !== targetName) {
                    repositories[index] = mergePreviewError(repositories[index], "Unexpected check response for this repository.", targetName);
                    completedForCurrent = true;
                    return;
                }
                const res = (data.results && data.results[0]) ? data.results[0] : null;
                repositories[index] = mergePreviewResult(repositories[index], res, targetName);
                completedForCurrent = true;
            } catch (e) {
                if (e && e.name === "AbortError") return;
                if (!isCurrent(gen)) return;
                if (e && e.status === 401) { window.location.assign(WELCOME_URL); return; }
                if (!repositories[index] || repositories[index].repository !== targetName) {
                    repositories[index] = mergePreviewError(repositories[index], "Unexpected check response for this repository.", targetName);
                    completedForCurrent = true;
                    return;
                }
                repositories[index] = mergePreviewError(repositories[index], (e && e.message) || "Check failed.", targetName);
                completedForCurrent = true;
            } finally {
                activePreviewControllers.delete(controller);
                if (!isCurrent(gen)) return;
                checkingSet.delete(targetName);
                if (completedForCurrent) checkedCount += 1;
                updateStats();
                renderPreviewProgress(true);
                renderRepos();
            }
        }

        async function worker() {
            while (isCurrent(gen)) {
                const idx = queue.shift();
                if (idx === undefined) break;
                await checkOne(idx);
            }
        }

        const workers = Array.from({ length: Math.min(PREVIEW_CONCURRENCY, Math.max(1, queue.length)) }, () => worker());
        await Promise.all(workers);

        if (!isCurrent(gen)) return;
        const counts = computeDashboardCounts(repositories);
        const unverified = counts.pending + counts.check_errors;
        const dataErrors = counts.invalid_fork_flags + counts.invalid_repositories + counts.duplicates + forkFlagErrors.length;
        const needsAttention = counts.unsyncable > 0 || unverified > 0 || dataErrors > 0;
        const checkingIncomplete = unverified > 0 || checkedCount < previewTotal;
        const countSummary = checkingIncomplete
            ? "final counters withheld"
            : `${counts.up_to_date} up to date, ${counts.to_be_synced} READY TO SYNC, ${counts.unsyncable} unsyncable`;
        setStatus(
            checkingIncomplete
                ? `Check incomplete: ${checkedCount} of ${previewTotal} complete, ${counts.pending} pending, ${counts.check_errors} check error(s); final counters withheld. No repositories were modified.`
                : `Check complete: ${counts.total} forks checked. No repositories were modified.${dataErrors > 0 ? ` ${dataErrors} data error(s) reported in the summary.` : ""}`,
            checkingIncomplete || needsAttention ? "warning" : ""
        );
        addHistory({
            summary: `${checkingIncomplete ? "Check incomplete" : (auto ? "Automatic check" : "Check")}: ${checkedCount}/${previewTotal} complete, ${counts.total} forks, ${countSummary}${dataErrors > 0 ? `, ${dataErrors} data error(s)` : ""}`,
        });
    } catch (e) {
        if (e && e.name === "AbortError") {
            // cancellation/interruption (e.g. account switch/new preview): stop spinner/state for this gen only
            finalizePreviewUi(gen);
            return;
        }
        if (!isCurrent(gen)) return;
        if (e && e.status === 401) { window.location.assign(WELCOME_URL); return; }
        repositories = [];
        selected = new Set();
        previewTotal = 0;
        checkedCount = 0;
        checkingSet = new Set();
        updateStats();
        renderRepos();
        setStatus((e && e.message) || "Check failed.", "error");
        addHistory({ summary: `Check failed: ${(e && e.message) || "error"}` });
    } finally {
        finalizePreviewUi(gen);
    }
}

async function runSync(mode) {
    if (syncInFlight || previewInFlight) return;
    const syncable = syncableRepositories();
    const repos = mode === "all"
        ? syncable.map((r) => r.repository)
        : Array.from(selected).filter((name) => syncable.some((r) => r.repository === name));
    if (repos.length === 0) {
        setStatus(mode === "all"
            ? "No READY TO SYNC forks are available. UNSYNCABLE forks cannot be auto-synced."
            : "Select at least one READY TO SYNC fork. UNSYNCABLE forks cannot be auto-synced.", "warning");
        return;
    }
    const confirmMessage = mode === "all"
        ? `Synchronize ALL ${repos.length} eligible READY TO SYNC fork(s)? This affects all eligible repositories. Only READY TO SYNC forks can be auto-synced.`
        : `Synchronize ${repos.length} selected READY TO SYNC fork(s)? Only READY TO SYNC forks can be auto-synced.`;
    if (!window.confirm(confirmMessage)) return;
    const gen = sessionGeneration;
    syncInFlight = true;
    setBusyUI();
    updateSelectionUI();
    renderSyncProgress({ total: repos.length, processed: 0, active: true, current: mode === "all"
        ? `Processing all eligible forks (0 of ${repos.length})...`
        : `Processing selected forks (0 of ${repos.length})...` });
    setStatus(mode === "all"
        ? `SYNC ALL in progress: ${repos.length} eligible fork(s).`
        : `SYNC CHOSEN in progress: ${repos.length} selected fork(s).`, "loading");
    try {
        const data = await requestJson("/api/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dry_run: false, confirmation: "SYNC", repositories: repos }) }, "Sync failed.");
        if (!isCurrent(gen)) return;
        const results = data.results || [];
        const processed = results.length;
        const selectedCount = repos.length;
        const summary = summarizeSyncResults(results);
        renderSyncSummary(summary);
        renderSyncProgress({ total: selectedCount, processed, active: false, current: `Completed: ${summary.success} synchronized, ${summary.failed} failed.` });
        const action = mode === "all" ? "SYNC ALL" : "SYNC CHOSEN";
        const historySummary = `${action}: ${summary.success} synchronized, ${summary.skipped} skipped, ${summary.failed} failed, ${summary.manual} need attention`;
        addHistory({ summary: historySummary });
        setStatus(`${historySummary}. Rechecking forks.`, summary.failed > 0 ? "error" : "success");
        await preview();
    } catch (e) {
        if (!isCurrent(gen)) return;
        if (e && e.status === 401) { window.location.assign(WELCOME_URL); return; }
        renderSyncProgress({ total: repos.length, processed: 0, active: false, current: "Sync failed." });
        setStatus(`Sync failed: ${(e && e.message) || "error"}. No UNSYNCABLE forks were auto-synced.`, "error");
        addHistory({ summary: `Sync failed: ${(e && e.message) || "error"}` });
    } finally {
        if (isCurrent(gen)) {
            syncInFlight = false;
            setBusyUI();
            updateSelectionUI();
        }
    }
}

async function initDashboard({ force = false } = {}) {
    if (previewInFlight || syncInFlight) {
        if (!force) return;
        invalidatePendingOperations();
    }
    if (initialized && !force) return;
    initialized = true;
    const gen = sessionGeneration;
    resetDashboardState();
    setConnectionState("Connecting...");
    setStatus("Restoring session...", "loading");
    const auth = await ensureAuth(gen);
    if (!auth || !isCurrent(gen)) {
        if (isCurrent(gen)) {
            renderPreviewProgress(false);
            setBusyUI();
            updateSelectionUI();
        }
        return;
    }
    await preview({ auto: true });
}

function toWelcome() {
    invalidatePendingOperations();
    initialized = false;
    resetDashboardState();
    window.location.assign(WELCOME_URL);
}

signoutButton.addEventListener("click", async () => {
    invalidatePendingOperations();
    try { await requestJson("/api/auth/logout", { method: "POST" }, "Sign out failed."); } catch {}
    toWelcome();
});

if (switchAccountButton) {
    switchAccountButton.addEventListener("click", async () => {
        switchAccountButton.disabled = true;
        setStatus("Switching account...", "loading");
        invalidatePendingOperations();
        initialized = false;
        resetDashboardState();
        try { await requestJson("/api/auth/switch", { method: "POST" }, "Switch account failed."); } catch {}
        toWelcome();
    });
}

syncButton.addEventListener("click", () => runSync("chosen"));
syncAllButton.addEventListener("click", () => runSync("all"));
searchInput.addEventListener("input", renderRepos);
filterSelect.addEventListener("change", renderRepos);
window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
        invalidatePendingOperations();
        initDashboard({ force: true });
    }
});

initDashboard();
