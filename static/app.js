const state = {
  status: null,
  selected: null,
  searchTimer: null,
  searchSeq: 0,
  bulkSignature: "",
};

const $ = (id) => document.getElementById(id);

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.add("hidden"), 3600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const contentType = response.headers.get("Content-Type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    throw new Error(data.error || data || "请求失败");
  }
  return data;
}

function post(path, payload) {
  return api(path, { method: "POST", body: JSON.stringify(payload || {}) });
}

function fillSelect(select, items, counts = {}) {
  const previous = select.value;
  select.innerHTML = "";
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.id;
    const count = counts[item.id];
    option.textContent = count && item.id !== "all" ? `${item.label} (${count})` : item.label;
    select.appendChild(option);
  }
  if ([...select.options].some((option) => option.value === previous)) {
    select.value = previous;
  }
}

function refreshFilterCounts(stats = {}) {
  fillSelect($("categorySelect"), state.status.categories || [], stats.categoryCounts || {});
  fillSelect($("sinnerSelect"), state.status.sinners || [], stats.sinnerCounts || {});
}

function updateStatusLine() {
  const status = state.status;
  const line = $("statusLine");
  if (!status?.bound) {
    line.textContent = "未绑定";
    return;
  }
  const stats = status.stats || {};
  const suffix = stats.files ? ` · ${stats.files} 文件 · ${stats.strings} 文本` : "";
  line.textContent = `${status.path}${suffix}`;
}

async function loadStatus() {
  state.status = await api("/api/status");
  const pathInput = $("bindPath");
  if (!pathInput.value) {
    pathInput.value = state.status.path || state.status.defaultPath || "";
  }
  refreshFilterCounts(state.status.stats);
  updateStatusLine();
}

function selectedPayload() {
  return {
    query: $("queryInput").value,
    category: $("categorySelect").value,
    sinner: $("sinnerSelect").value,
    fileQuery: $("fileInput").value,
    fieldQuery: $("fieldInput").value,
    modifiedOnly: $("modifiedOnly").checked,
    limit: 300,
  };
}

function hasSearchCriteria(payload) {
  return Boolean(
    payload.query.trim() ||
      payload.category !== "all" ||
      payload.sinner !== "all" ||
      payload.fileQuery.trim() ||
      payload.fieldQuery.trim() ||
      payload.modifiedOnly,
  );
}

function clearSearchDisplay() {
  state.selected = null;
  $("resultList").innerHTML = "";
  $("resultCount").textContent = "0";
  $("editorBody").classList.add("hidden");
  $("emptyEditor").classList.remove("hidden");
  $("selectionTag").textContent = "未选择";
}

function bulkScopePayload() {
  if (!$("bulkUseScope").checked) {
    return {};
  }
  return {
    category: $("categorySelect").value,
    sinner: $("sinnerSelect").value,
    fileQuery: $("fileInput").value,
    fieldQuery: $("fieldInput").value,
    modifiedOnly: $("modifiedOnly").checked,
  };
}

function bulkPayload(forceSafety = false) {
  const payload = {
    oldText: $("bulkOldInput").value,
    newText: $("bulkNewInput").value,
    matchCase: $("bulkMatchCase").checked,
    scope: bulkScopePayload(),
  };
  if (forceSafety) {
    payload.forceSafety = true;
  }
  return payload;
}

function bulkSignature() {
  return JSON.stringify(bulkPayload());
}

function invalidateBulkPreview() {
  state.bulkSignature = "";
  $("bulkApplyButton").disabled = true;
}

function scheduleSearch() {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(runSearch, 220);
  invalidateBulkPreview();
}

async function runSearch() {
  if (!state.status?.bound) {
    clearSearchDisplay();
    return;
  }
  const payload = selectedPayload();
  if (!hasSearchCriteria(payload)) {
    ++state.searchSeq;
    clearSearchDisplay();
    return;
  }
  const seq = ++state.searchSeq;
  try {
    const data = await post("/api/search", payload);
    if (seq !== state.searchSeq) return;
    renderResults(data.results || [], data.total || 0, data.limited);
    if (data.stats) {
      state.status.stats = data.stats;
      refreshFilterCounts(data.stats);
      updateStatusLine();
    }
  } catch (error) {
    if (seq !== state.searchSeq) return;
    showToast(error.message, true);
  }
}

function renderResults(results, total, limited) {
  const list = $("resultList");
  list.innerHTML = "";
  $("resultCount").textContent = limited ? `${results.length} / ${total}` : String(total);
  if (!results.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = `
      <div>
        <p>没有匹配项。</p>
        <button id="emptyClearFiltersButton" type="button">清空筛选</button>
      </div>
    `;
    list.appendChild(empty);
    $("emptyClearFiltersButton").addEventListener("click", resetFilters);
    return;
  }
  for (const item of results) {
    const button = document.createElement("button");
    button.className = "result-item";
    if (state.selected?.key === item.key) {
      button.classList.add("active");
    }
    button.dataset.key = item.key;
    button.innerHTML = `
      <div class="result-title">
        <strong>${escapeHtml(item.label || item.entryId || item.field || item.file)}</strong>
        ${item.hasOverride ? '<span class="badge modified">已修正</span>' : ""}
      </div>
      <div class="badge-row">
        <span class="badge">${escapeHtml(item.categoryLabel || item.category)}</span>
        ${item.sinnerLabel ? `<span class="badge">${escapeHtml(item.sinnerLabel)}</span>` : ""}
        <span class="badge">${escapeHtml(item.field)}</span>
      </div>
      <div class="preview">${escapeHtml(item.preview)}</div>
      <div class="fileline">${escapeHtml(item.file)} · ${escapeHtml(item.pathDisplay)}</div>
    `;
    button.addEventListener("click", () => selectResult(item));
    list.appendChild(button);
  }
}

function selectResult(item) {
  state.selected = item;
  $("emptyEditor").classList.add("hidden");
  $("editorBody").classList.remove("hidden");
  $("selectionTag").textContent = item.hasOverride ? "已修正" : "当前文件";
  $("metaFile").textContent = item.file;
  $("metaField").textContent = item.field;
  $("metaCategory").textContent = [item.categoryLabel, item.sinnerLabel].filter(Boolean).join(" / ");
  $("metaPath").textContent = item.pathDisplay;
  $("editText").value = item.value;
  $("noteInput").value = "";
  renderResultsActiveOnly();
}

function renderResultsActiveOnly() {
  document.querySelectorAll(".result-item").forEach((node) => node.classList.remove("active"));
  document.querySelector(`.result-item[data-key="${CSS.escape(state.selected?.key || "")}"]`)?.classList.add("active");
}

function resetFilters() {
  $("queryInput").value = "";
  $("fileInput").value = "";
  $("fieldInput").value = "";
  $("modifiedOnly").checked = false;
  if ([...$("categorySelect").options].some((option) => option.value === "all")) {
    $("categorySelect").value = "all";
  }
  if ([...$("sinnerSelect").options].some((option) => option.value === "all")) {
    $("sinnerSelect").value = "all";
  }
  invalidateBulkPreview();
  runSearch();
}

async function saveSelected(forceSafety = false) {
  if (!state.selected) return;
  try {
    const linkedReference = state.selected.linkedReference;
    const endpoint = linkedReference ? "/api/update-reference" : "/api/update";
    const payload = linkedReference
      ? {
          token: linkedReference.token,
          expectedValue: linkedReference.expectedValue,
          newValue: $("editText").value,
          note: $("noteInput").value,
          forceSafety,
        }
      : {
          file: state.selected.file,
          path: state.selected.path,
          newValue: $("editText").value,
          note: $("noteInput").value,
          entryId: state.selected.entryId,
          label: state.selected.label,
          forceSafety,
        };
    const data = await post(endpoint, payload);
    if (data.blockedBySafety) {
      if (confirmSafetyOverride(data.warnings || [], "这次保存可能破坏游戏文本的格式标记。")) {
        await saveSelected(true);
      } else {
        showToast("已取消保存，未写入文件。", true);
      }
      return;
    }
    if (data.changed) {
      showToast(
        linkedReference
          ? `已同步修改 ${data.fields || linkedReference.copies} 个关联词条，原文件已备份。`
          : "已保存，原文件已备份。",
      );
      state.selected.value = $("editText").value;
      if (linkedReference) linkedReference.expectedValue = $("editText").value;
      await Promise.all([runSearch(), loadHistory()]);
    } else {
      showToast(data.message || "文本没有变化。");
    }
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadHistory() {
  try {
    const data = await api("/api/history?limit=80");
    renderHistory(data.history || []);
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderHistory(items) {
  const list = $("historyList");
  list.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "暂无历史。";
    list.appendChild(empty);
    return;
  }
  for (const item of items) {
    const node = document.createElement("div");
    node.className = "history-item";
    const action = {
      update: "保存",
      reapply: "重放",
      backup: "备份",
      bulk_replace: "批量替换",
      undo: "撤销",
    }[item.action] || item.action;
    node.innerHTML = `
      <strong>${escapeHtml(action)} · ${escapeHtml(item.file || item.note || "")}</strong>
      <p>${escapeHtml(formatTime(item.time))}${item.field ? ` · ${escapeHtml(item.field)}` : ""}</p>
      ${item.newValue ? `<p>${escapeHtml(shorten(item.newValue, 120))}</p>` : ""}
    `;
    list.appendChild(node);
  }
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function shorten(value, limit) {
  value = String(value).replace(/\r?\n/g, "\\n");
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safetyWarningLines(warnings = []) {
  const lines = [];
  for (const item of warnings) {
    const location = [item.file, item.field, item.pathDisplay].filter(Boolean).join(" · ");
    lines.push(location || "未知位置");
    for (const warning of item.warnings || []) {
      lines.push(`- ${warning.message}`);
    }
  }
  return lines;
}

function confirmSafetyOverride(warnings, title) {
  const lines = safetyWarningLines(warnings).slice(0, 28);
  const more = safetyWarningLines(warnings).length > lines.length ? "\n\n还有更多风险项未显示。" : "";
  return window.confirm(`${title}\n\n${lines.join("\n")}${more}\n\n仍要写入吗？`);
}

function renderSafetyWarnings(warnings = []) {
  return warnings
    .map(
      (item) => `
        <div class="safety-warning">
          <strong>${escapeHtml(item.file)} · ${escapeHtml(item.field || "")}</strong>
          <div>${escapeHtml(item.pathDisplay || "")}</div>
          <ul>
            ${(item.warnings || []).map((warning) => `<li>${escapeHtml(warning.message)}</li>`).join("")}
          </ul>
        </div>
      `,
    )
    .join("");
}

function showReapplyConflicts(items = []) {
  const conflicts = items.filter((item) => item.status === "conflict");
  if (!conflicts.length) return;
  const lines = conflicts.slice(0, 8).map((item) => {
    const location = [item.file, item.field].filter(Boolean).join(" · ");
    return `${location}\n当前：${item.current || ""}\n历史目标：${item.desired || ""}`;
  });
  const more = conflicts.length > lines.length ? `\n\n还有 ${conflicts.length - lines.length} 条冲突未显示。` : "";
  window.alert(`有 ${conflicts.length} 条历史修改因源文本变化被跳过。\n\n${lines.join("\n\n")}${more}`);
}

async function bindPath() {
  $("bindButton").disabled = true;
  try {
    state.status = await post("/api/bind", { path: $("bindPath").value });
    refreshFilterCounts(state.status.stats);
    updateStatusLine();
    showToast("绑定完成，索引已建立。");
    await runSearch();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    $("bindButton").disabled = false;
  }
}

async function pickFolder() {
  $("pickFolderButton").disabled = true;
  try {
    const data = await post("/api/pick-folder", {});
    if (!data.path) {
      showToast("已取消选择。");
      return;
    }
    $("bindPath").value = data.path;
    await bindPath();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    $("pickFolderButton").disabled = false;
  }
}

async function reindex() {
  try {
    state.status = await post("/api/reindex", {});
    refreshFilterCounts(state.status.stats);
    updateStatusLine();
    showToast("索引已刷新。");
    await runSearch();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function reapply() {
  if (!window.confirm("将把历史修正重新写回当前绑定目录。继续？")) return;
  try {
    const data = await post("/api/reapply", {});
    showToast(
      `重放完成：写回 ${data.changed} 条，跳过 ${data.skipped} 条，冲突 ${data.conflicts || 0} 条，缺失 ${data.missing} 条。`,
      Boolean(data.conflicts),
    );
    showReapplyConflicts(data.items || []);
    await Promise.all([runSearch(), loadHistory()]);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function backup() {
  try {
    const data = await post("/api/backup", {});
    showToast(`已备份 ${data.files} 个 JSON：${data.archive}`);
    await loadHistory();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function undoLast() {
  if (!window.confirm("确认撤销最近一次写入操作？\n\n工具会先备份当前 JSON，再把最近一次保存、批量替换或重放历史回退。")) {
    return;
  }
  $("undoLastButton").disabled = true;
  try {
    const data = await post("/api/undo-last", {});
    showToast(`撤销完成：回退 ${data.files} 个文件、${data.changed} 个字段；冲突 ${data.conflicts} 个，缺失 ${data.missing} 个。`);
    await Promise.all([runSearch(), loadHistory()]);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    $("undoLastButton").disabled = false;
  }
}

async function previewBulkReplace() {
  try {
    const payload = bulkPayload();
    const data = await post("/api/bulk-preview", payload);
    renderBulkPreview(data);
    state.bulkSignature = bulkSignature();
    $("bulkApplyButton").disabled = data.fields <= 0;
  } catch (error) {
    invalidateBulkPreview();
    showToast(error.message, true);
  }
}

function renderBulkPreview(data) {
  const summary = $("bulkSummary");
  const examples = $("bulkExamples");
  examples.innerHTML = "";
  if (!data.fields) {
    summary.textContent = "没有找到需要替换的文本。";
    return;
  }
  summary.textContent = `将影响 ${data.files} 个文件、${data.fields} 个文本字段，共 ${data.occurrences} 处。`;
  if (data.unsafeFields) {
    summary.textContent += ` 其中 ${data.unsafeFields} 个字段存在格式风险，执行时会要求二次确认。`;
    examples.insertAdjacentHTML("beforeend", renderSafetyWarnings(data.safetyWarnings || []));
  }
  for (const item of data.examples || []) {
    const node = document.createElement("div");
    node.className = "bulk-example";
    node.innerHTML = `
      <strong>${escapeHtml(item.file)}</strong>
      <div>${escapeHtml(item.field)} · ${escapeHtml(item.categoryLabel || "")}${item.sinnerLabel ? ` · ${escapeHtml(item.sinnerLabel)}` : ""}</div>
      <div>${escapeHtml(item.before)}</div>
      <div>→ ${escapeHtml(item.after)}</div>
    `;
    examples.appendChild(node);
  }
}

async function applyBulkReplace(forceSafety = false) {
  const signature = bulkSignature();
  if (signature !== state.bulkSignature) {
    showToast("筛选或替换内容已变化，请先重新预览。", true);
    $("bulkApplyButton").disabled = true;
    return;
  }
  const oldText = $("bulkOldInput").value;
  const newText = $("bulkNewInput").value;
  if (
    !forceSafety &&
    !window.confirm(`确认把所有命中的“${oldText}”替换为“${newText}”？\n\n工具会先备份涉及的 JSON 文件。`)
  ) {
    return;
  }
  $("bulkApplyButton").disabled = true;
  try {
    const data = await post("/api/bulk-replace", bulkPayload(forceSafety));
    if (data.blockedBySafety) {
      if (confirmSafetyOverride(data.warnings || [], `批量替换中有 ${data.unsafeFields || 0} 个字段可能破坏格式标记。`)) {
        await applyBulkReplace(true);
      } else {
        showToast("已取消批量替换，未写入文件。", true);
        $("bulkApplyButton").disabled = false;
      }
      return;
    }
    showToast(`批量替换完成：${data.files} 个文件、${data.changed} 个字段、${data.occurrences} 处。`);
    invalidateBulkPreview();
    $("bulkSummary").textContent = "已完成。再次替换前请重新预览。";
    $("bulkExamples").innerHTML = "";
    await Promise.all([runSearch(), loadHistory()]);
  } catch (error) {
    showToast(error.message, true);
    $("bulkApplyButton").disabled = false;
  }
}

async function shutdownTool() {
  if (!window.confirm("确定退出工具？")) return;
  try {
    await post("/api/shutdown", {});
    document.body.innerHTML = '<div class="shutdown-screen"><h1>工具已退出</h1><p>现在可以关闭这个页面。</p></div>';
  } catch (error) {
    showToast(error.message, true);
  }
}

function initEvents() {
  $("pickFolderButton").addEventListener("click", pickFolder);
  $("bindButton").addEventListener("click", bindPath);
  $("reindexButton").addEventListener("click", reindex);
  $("shutdownButton").addEventListener("click", shutdownTool);
  $("clearFiltersButton").addEventListener("click", resetFilters);
  $("saveButton").addEventListener("click", saveSelected);
  $("resetButton").addEventListener("click", () => {
    if (state.selected) $("editText").value = state.selected.value;
  });
  $("refreshHistoryButton").addEventListener("click", loadHistory);
  $("reapplyButton").addEventListener("click", reapply);
  $("backupButton").addEventListener("click", backup);
  $("undoLastButton").addEventListener("click", undoLast);
  $("bulkPreviewButton").addEventListener("click", previewBulkReplace);
  $("bulkApplyButton").addEventListener("click", applyBulkReplace);
  $("exportButton").addEventListener("click", () => {
    window.open("/api/export-overrides", "_blank");
  });

  for (const id of ["queryInput", "categorySelect", "sinnerSelect", "fileInput", "fieldInput", "modifiedOnly"]) {
    $(id).addEventListener("input", scheduleSearch);
    $(id).addEventListener("change", scheduleSearch);
  }
  for (const id of ["bulkOldInput", "bulkNewInput", "bulkUseScope", "bulkMatchCase"]) {
    $(id).addEventListener("input", invalidateBulkPreview);
    $(id).addEventListener("change", invalidateBulkPreview);
  }
}

async function boot() {
  initEvents();
  await loadStatus();
  await Promise.all([runSearch(), loadHistory()]);
}

boot().catch((error) => showToast(error.message, true));
