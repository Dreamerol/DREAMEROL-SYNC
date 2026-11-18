const KNOWN_STATUS_CLASSES = {
    "behind": "behind",
    "up-to-date": "synced",
    "synced": "synced",
    "diverged": "diverged",
    "ahead": "ahead",
    "error": "error",
    "conflict": "error",
    "ambiguous": "error",
    "unknown": "unknown",
    "pending": "pending",
    "skipped": "unknown",
};

export async function requestJson(url, options = {}, fallbackMessage = "The request failed.") {
    let response;
    try {
        response = await fetch(url, { credentials: "same-origin", ...options });
    } catch {
        throw new Error("Could not reach the server.");
    }
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (!response.ok) {
        const detail = data && typeof data.detail === "string" ? data.detail : fallbackMessage;
        const error = new Error(detail);
        error.status = response.status;
        throw error;
    }
    if (data === null) throw new Error("The server returned an invalid response.");
    return data;
}

export function classifyRepo(result) {
    const status = result && result.status;
    if (typeof status !== "string") return "unknown";
    if (status === "error" && result && result.preview_error) return "unknown";
    return KNOWN_STATUS_CLASSES[status] || "unknown";
}

export function getCompareClass(repo) {
    const ahead = repo && repo.ahead_by;
    const behind = repo && repo.behind_by;
    if (!Number.isInteger(ahead) || !Number.isInteger(behind) || ahead < 0 || behind < 0) return "";
    if (ahead === 0 && behind === 0) return "synced";
    if (ahead === 0 && behind > 0) return "behind";
    if (ahead > 0 && behind === 0) return "ahead";
    return "diverged";
}

export function effectiveCountClass(repo) {
    const r = repo || {};
    const base = classifyRepo(r);
    if (base === "error" || base === "pending") return base;
    if (r.preview_error && base !== "unknown") return base;
    return getCompareClass(r) || base;
}

export function isCheckError(repo) {
    const r = repo || {};
    const c = effectiveCountClass(r);
    return !!r.preview_error || c === "error" || c === "unknown";
}

export function hasKnownCountableStatus(repo) {
    const c = effectiveCountClass(repo || {});
    return c === "synced" || c === "behind" || c === "diverged" || c === "ahead";
}

export function getForkFlagErrors(repos = []) {
    const labels = [];
    for (const repo of Array.isArray(repos) ? repos : []) {
        if (repo && typeof repo === "object" && (repo.fork === true || repo.fork === false)) continue;
        const name = repo && typeof repo.repository === "string" && repo.repository.trim() ? repo.repository.trim() : "unknown";
        if (!labels.includes(name)) labels.push(name);
    }
    return labels;
}

export function getUnexpectedStates(repos = []) {
    const states = [];
    const forks = Array.isArray(repos) ? repos.filter(isForkRepository) : [];
    for (const repo of forks) {
        if (getCompareClass(repo)) continue;
        const raw = repo && repo.status;
        const known = typeof raw === "string" && Object.prototype.hasOwnProperty.call(KNOWN_STATUS_CLASSES, raw);
        if (known) continue;
        const label = typeof raw === "string" && raw.trim() ? raw.trim() : "unclassified";
        if (!states.includes(label)) states.push(label);
    }
    return states;
}

export function getUpstreamLabel(repo) {
    const parent = repo && repo.parent;
    if (typeof parent === "string" && parent.trim()) return parent.trim();
    if (parent && typeof parent.full_name === "string" && parent.full_name.trim()) return parent.full_name.trim();
    if (parent && typeof parent.html_url === "string" && parent.html_url.trim()) return parent.html_url.trim();
    const upstream = repo && repo.upstream;
    if (typeof upstream === "string" && upstream.trim()) return upstream.trim();
    if (upstream && typeof upstream.full_name === "string" && upstream.full_name.trim()) return upstream.full_name.trim();
    if (repo && typeof repo.upstream_full_name === "string" && repo.upstream_full_name.trim()) return repo.upstream_full_name.trim();
    return "";
}

export function isForkRepository(repo) {
    return !!(repo && typeof repo === "object" && repo.fork === true);
}

export function isSyncableRepository(repo) {
    return isForkRepository(repo) && getCompareClass(repo) === "behind" && effectiveCountClass(repo) === "behind";
}

export function isUnresolvedRepository(repo) {
    const c = effectiveCountClass(repo || {});
    return isCheckError(repo) || c === "pending";
}

export function computeDashboardCounts(repos = []) {
    let up_to_date = 0, to_be_synced = 0, unsyncable = 0;
    let ahead = 0, diverged = 0, errors = 0, pending = 0;
    let invalid_fork_flags = 0, invalid_repositories = 0, duplicates = 0, check_errors = 0;
    const seen = new Set();
    const forks = [];
    for (const r of Array.isArray(repos) ? repos : []) {
        if (!r || typeof r !== "object") {
            invalid_fork_flags += 1;
            continue;
        }
        if (r.fork === false) continue;
        if (r.fork !== true) {
            invalid_fork_flags += 1;
            continue;
        }
        const name = r.repository;
        if (typeof name !== "string" || !name.trim() || !name.includes("/")) {
            invalid_repositories += 1;
            continue;
        }
        if (seen.has(name)) {
            duplicates += 1;
            continue;
        }
        seen.add(name);
        forks.push(r);
    }
    for (const r of forks) {
        const countableCheckError = !!r.preview_error && hasKnownCountableStatus(r);
        if (isCheckError(r) && !countableCheckError) {
            errors += 1;
            check_errors += 1;
            continue;
        }
        const c = effectiveCountClass(r);
        if (c === "synced") {
            up_to_date += 1;
        } else if (c === "behind") {
            to_be_synced += 1;
        } else if (c === "ahead") {
            unsyncable += 1;
            ahead += 1;
        } else if (c === "diverged") {
            unsyncable += 1;
            diverged += 1;
        } else if (c === "pending") {
            pending += 1;
        } else {
            errors += 1;
            check_errors += 1;
        }
        if (countableCheckError) {
            errors += 1;
            check_errors += 1;
        }
    }
    return {
        total: forks.length,
        up_to_date,
        to_be_synced,
        unsyncable,
        ahead,
        diverged,
        errors,
        pending,
        check_errors,
        invalid_fork_flags,
        invalid_repositories,
        duplicates,
        compare_errors: check_errors,
        data_errors: invalid_fork_flags + invalid_repositories + check_errors,
        complete: pending === 0 && check_errors === 0,
    };
}

export function computeStatsCounts(repos = []) {
    return computeDashboardCounts(repos);
}

export function isValidRepositoryResult(result) {
    return !!(result && typeof result === "object" && typeof result.repository === "string" && result.repository.includes("/"));
}

export function mergePreviewResult(previousRepo, result, targetName) {
    if (isValidRepositoryResult(result) && result.repository === targetName) {
        const merged = { ...result, repository: targetName };
        if (previousRepo && typeof previousRepo === "object") {
            for (const key of ["fork", "parent", "upstream", "upstream_full_name", "last_successful_sync"]) {
                if (!(key in merged) && key in previousRepo) merged[key] = previousRepo[key];
            }
        }
        delete merged.preview_error;
        return merged;
    }
    return mergePreviewError(previousRepo, "No matching result returned.", targetName);
}

export function mergePreviewError(previousRepo, errorMessage, targetName) {
    const prev = previousRepo || {};
    const msg = errorMessage || "Check failed.";
    if (hasKnownCountableStatus(prev)) {
        return { ...prev, repository: targetName, preview_error: msg };
    }
    return { ...prev, repository: targetName, status: "unknown", preview_error: msg, message: msg };
}

export function computeProgress(checked, total) {
    const safeTotal = Number.isFinite(total) && total > 0 ? total : 0;
    const safeChecked = Math.max(0, Math.min(Number.isFinite(checked) ? checked : 0, safeTotal || (Number.isFinite(checked) ? checked : 0)));
    const percent = safeTotal > 0 ? Math.round((safeChecked / safeTotal) * 100) : 0;
    return { checked: safeChecked, total: safeTotal, percent };
}

export function getPreviewProgressState({ total = 0, checked = 0, checking = [], active = false } = {}) {
    const p = computeProgress(checked, total);
    return {
        mode: "preview",
        label: "Checking repositories:",
        active: !!active,
        ...p,
        current: Array.isArray(checking) ? checking : [],
    };
}

export function getPreviewSpinnerUiState({ previewInFlight = false, isCurrent = true } = {}) {
    const visible = !!previewInFlight && !!isCurrent;
    return {
        visible,
        spinnerStopped: !visible,
        label: "Checking repositories:",
    };
}

export function canApplyPreviewUpdate({ updateGen, currentGen, targetName = null, currentName = null } = {}) {
    if (updateGen !== currentGen) return false;
    if (targetName != null && currentName != null && targetName !== currentName) return false;
    return true;
}

export function finalizePreviewCounters({ checked = 0, total = 0 } = {}) {
    const safeTotal = Number.isFinite(total) && total >= 0 ? total : 0;
    const safeChecked = Math.max(0, Math.min(Number.isFinite(checked) ? checked : 0, safeTotal));
    return { checked: safeChecked, total: safeTotal, complete: safeTotal === 0 ? true : safeChecked >= safeTotal };
}

export function getSyncProgressState({ total = 0, processed = 0, active = false } = {}) {
    const p = computeProgress(processed, total);
    return {
        mode: "sync",
        label: "SYNC — processing selected forks",
        active: !!active,
        processed: p.checked,
        total: p.total,
        percent: p.percent,
    };
}

export function summarizeSyncResults(results = []) {
    let success = 0, skipped = 0, failed = 0, manual = 0;
    for (const result of Array.isArray(results) ? results : []) {
        const status = result && result.status;
        if (status === "synced") success += 1;
        else if (status === "up-to-date" || status === "skipped") skipped += 1;
        else if (status === "error" || status === "conflict" || status === "ambiguous") { failed += 1; manual += 1; }
        else if (status === "diverged" || status === "ahead") { skipped += 1; manual += 1; }
        else manual += 1;
    }
    return { success, skipped, failed, manual };
}

export function formatLastSync(value, now = Date.now()) {
    if (!value) return "Never synced";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "Never synced";
    const absolute = date.toLocaleString(undefined, {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
    });
    const diffMs = (typeof now === "number" ? now : Date.now()) - date.getTime();
    const diffSec = Math.max(0, Math.floor(diffMs / 1000));
    let relative;
    if (diffSec < 60) relative = `${diffSec} seconds ago`;
    else if (diffSec < 3600) {
        const m = Math.floor(diffSec / 60);
        relative = m === 1 ? "1 minute ago" : `${m} minutes ago`;
    } else if (diffSec < 86400) {
        const h = Math.floor(diffSec / 3600);
        relative = h === 1 ? "1 hour ago" : `${h} hours ago`;
    } else {
        const d = Math.floor(diffSec / 86400);
        relative = d === 1 ? "1 day ago" : `${d} days ago`;
    }
    return `Last successful sync: ${absolute} (${relative})`;
}

export function dedupeRepositoriesByName(repos = []) {
    const seen = new Set();
    const unique = [];
    for (const repo of repos) {
        if (!repo || typeof repo !== "object") continue;
        const name = repo.repository;
        if (typeof name !== "string" || !name.includes("/") || seen.has(name)) continue;
        seen.add(name);
        unique.push(repo);
    }
    return unique;
}