const API = "/api/v1";
const state = {
  token: localStorage.getItem("ss_token") || "",
  user: null,
  projects: [],
  project: null,
  pages: [],
  page: null,
  /** @type {"all"|"open"} */
  thumbFilter: localStorage.getItem("ss_thumb_filter") === "open" ? "open" : "all",
  proofSuggestions: [],
  draftQuery: "",
  draftHits: [],
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function toast(msg, err = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", err);
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) {
    logout(false);
    throw new Error("Требуется вход");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || JSON.stringify(j);
    } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res;
}

function showView(name) {
  $$(".view").forEach((v) => v.classList.remove("active"));
  const el = document.querySelector(`#view-${name}`);
  if (el) el.classList.add("active");
  $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
}

function logout(notify = true) {
  state.token = "";
  state.user = null;
  localStorage.removeItem("ss_token");
  $("#top-user").textContent = "";
  $("#nav-authed").hidden = true;
  const cabinet = $("#nav-account");
  if (cabinet) cabinet.hidden = true;
  showView("login");
  if (notify) toast("Выход");
}

async function bootstrap() {
  if (!state.token) {
    showView("login");
    return;
  }
  try {
    state.user = await api("/auth/me");
    afterLogin();
    await loadProjects();
    showView("projects");
  } catch {
    logout(false);
  }
}

function afterLogin() {
  $("#nav-authed").hidden = false;
  $("#top-user").textContent = `${state.user.display_name} · ${state.user.role}`;
  $("#nav-admin").hidden = state.user.role !== "admin";
  const cabinet = $("#nav-account");
  if (cabinet) cabinet.hidden = !["admin", "expert", "scholar"].includes(state.user.role);
  $("#upload-card").hidden = state.user.role !== "admin";
}

async function login(ev) {
  ev.preventDefault();
  const email = $("#login-email").value.trim();
  const password = $("#login-password").value;
  try {
    const data = await api("/auth/login", { method: "POST", json: { email, password } });
    state.token = data.access_token;
    localStorage.setItem("ss_token", state.token);
    state.user = { display_name: data.display_name, role: data.role, email };
    // refresh full profile
    state.user = await api("/auth/me");
    afterLogin();
    await loadProjects();
    showView("projects");
    toast("Вход выполнен");
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadProjects() {
  state.projects = await api("/projects");
  const box = $("#project-list");
  if (!state.projects.length) {
    box.innerHTML = `<p class="muted">Пока нет проектов. Admin загружает PDF-скан.</p>`;
    return;
  }
  box.innerHTML = state.projects
    .map((p) => {
      const task = p.task || p.settings?.task || "digitize";
      const pill = task === "translate" ? "перевод" : "оцифровка";
      const pillClass = task === "translate" ? "task-pill ru" : "task-pill";
      return `
    <article class="project-card" data-id="${p.id}">
      <div class="${pillClass}">${pill}</div>
      <h3>${escapeHtml(p.title)}</h3>
      <div class="meta sa">${escapeHtml(p.title_sa || "")}</div>
      <div class="meta">${task === "translate" ? "стр." : "PDF"} ${p.pdf_pages ?? p.page_count ?? "?"} · согласовано ${p.accepted ?? 0} · на правке ${p.draft_ready ?? 0}</div>
      <div class="meta">${pipelineLabel(p)} · ${escapeHtml(p.slug)}</div>
    </article>`;
    })
    .join("");
  box.querySelectorAll(".project-card").forEach((card) => {
    card.onclick = () => openProject(card.dataset.id);
  });
}

function setUploadStatus(text) {
  const el = $("#upload-status");
  if (!el) return;
  if (!text) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.textContent = text;
}

function uploadProjectXhr(fd, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API + "/projects");
    xhr.setRequestHeader("Authorization", `Bearer ${state.token}`);
    xhr.timeout = 15 * 60 * 1000;
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) {
        onProgress("Отправка файла…");
        return;
      }
      const pct = Math.round((e.loaded / e.total) * 100);
      const mb = (e.loaded / (1024 * 1024)).toFixed(1);
      const totalMb = (e.total / (1024 * 1024)).toFixed(1);
      onProgress(`Отправка PDF: ${pct}% (${mb} / ${totalMb} МБ)`);
    };
    xhr.upload.onload = () => onProgress("Файл на сервере — считаем страницы (до ~1 мин)…");
    xhr.onload = () => {
      let body = null;
      try {
        body = JSON.parse(xhr.responseText || "null");
      } catch (_) {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body);
        return;
      }
      const detail = body?.detail;
      const msg = Array.isArray(detail)
        ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
        : detail || xhr.statusText || `Ошибка ${xhr.status}`;
      reject(new Error(msg));
    };
    xhr.onerror = () => reject(new Error("Сеть: не удалось отправить файл"));
    xhr.ontimeout = () => reject(new Error("Таймаут загрузки (15 мин) — попробуйте ещё раз"));
    xhr.send(fd);
  });
}

async function createProject(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const btn = $("#btn-upload");
  const label = btn.textContent;
  try {
    btn.disabled = true;
    btn.textContent = "Загрузка…";
    setUploadStatus("Готовим отправку…");
    const project = await uploadProjectXhr(fd, (msg) => {
      setUploadStatus(msg);
      btn.textContent = "Загрузка…";
    });
    form.reset();
    await loadProjects();
    setUploadStatus("");
    if (project.confirm_required) {
      state.pendingConfirmProject = project;
      const kind =
        project.source_kind === "text"
          ? "текстовый PDF (без LLM, но все страницы)"
          : "скан (LLM на каждую страницу)";
      $("#large-book-text").textContent =
        `«${project.title}» — ${project.page_count} страниц (>100). Тип: ${kind}.`;
      $("#large-book-modal").hidden = false;
      return;
    }
    const mode =
      project.source_kind === "text"
        ? "текстовый PDF — LLM не используется"
        : "скан — LLM по всей книге";
    toast(`Вся книга в очереди (${project.page_count} стр.): ${mode}`);
    await openProject(project.id);
  } catch (e) {
    toast(e.message, true);
    setUploadStatus(e.message || "Ошибка загрузки");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function confirmWholeBook() {
  const project = state.pendingConfirmProject;
  if (!project) return;
  try {
    $("#btn-confirm-whole-book").disabled = true;
    state.project = await api(`/projects/${project.id}/pipeline`, { method: "POST" });
    $("#large-book-modal").hidden = true;
    state.pendingConfirmProject = null;
    toast(`Перевод всей книги запущен (${state.project.page_count} стр.)`);
    await loadProjects();
    await openProject(state.project.id);
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("#btn-confirm-whole-book").disabled = false;
  }
}

function cancelWholeBook() {
  $("#large-book-modal").hidden = true;
  const project = state.pendingConfirmProject;
  state.pendingConfirmProject = null;
  toast("Книга загружена без перевода — можно запустить позже");
  if (project) openProject(project.id);
}

function sourceKindLabel(p) {
  if (p.source_kind === "text") return "текстовый PDF (без LLM)";
  return "скан → LLM";
}

function pipelineLabel(p) {
  const task = p.task || p.settings?.task || "digitize";
  const total = prTotal(p);
  const pipe = p.pipeline;
  if (task === "translate") {
    const tr = p.translation || p.settings?.translation || {};
    const agreed = tr.agreed ? "шаблон согласован" : "шаблон не согласован";
    if (pipe && (pipe.status === "running" || pipe.status === "queued")) {
      const pr = pipe.progress || {};
      const scope = pr.open_only ? "несогласованные" : "все";
      if (pr.scope === "translate_proofread") {
        return `смысловая проверка перевода · ${scope}: ${pr.done ?? 0}/${pr.total ?? total} (сейчас стр. ${pr.current_page ?? "…"})`;
      }
      return `перевод на русский · ${scope}: ${pr.done ?? 0}/${pr.total ?? total} (сейчас стр. ${pr.current_page ?? "…"})`;
    }
    if (pipe?.status === "failed") {
      const err = pipe.progress?.last_error || pipe.error || "";
      const kind = pipe.progress?.scope === "translate_proofread" ? "смысловая проверка" : "перевод на русский";
      return `${kind} · ошибка — ${String(err).slice(0, 80)}`;
    }
    if (pipe?.status === "done") {
      const pr = pipe.progress || {};
      if (pr.scope === "translate_proofread") {
        const flags = pr.flagged != null ? ` · замечаний ${pr.flagged}` : "";
        const high = pr.applied_high != null ? ` · грубых правок ${pr.applied_high}` : "";
        return `смысловая проверка перевода · готово ${pr.done ?? 0}/${pr.total ?? total}${high}${flags}`;
      }
      return `перевод на русский · ${agreed} · готово ${pr.done ?? total}/${pr.total ?? total}`;
    }
    return `перевод на русский · ${agreed} · ${total} стр.`;
  }
  const kind = sourceKindLabel(p);
  if (p.manual_pages && !pipe) {
    return `${kind} · ${total} стр. · постранично (автоперевод всей книги не запускался)`;
  }
  if (p.confirm_required || p.status === "awaiting_confirm") {
    return `${kind} · ${total} стр. (>100) — нужно подтверждение перевода всей книги`;
  }
  if (!pipe) return `${kind} · вся книга (${total} стр.) — конвейер не запущен`;
  const pr = pipe.progress || {};
  if (pipe.status === "running" || pipe.status === "queued") {
    const mode = (pr.source_kind || p.source_kind) === "text" ? "текст всей книги" : "перевод всей книги";
    const scope = pr.open_only ? "несогласованные" : mode;
    return `${kind} · ${scope}: ${pr.done ?? 0}/${pr.total ?? total} (сейчас стр. ${pr.current_page ?? "…"})`;
  }
  if (pipe.status === "done") return `${kind} · вся книга готова (${pr.total ?? total} стр.)`;
  if (pipe.status === "failed") {
    const err = pr.last_error || pipe.error || "";
    if (String(err).includes("llm_quota") || String(err).includes("402")) {
      return `${kind} · стоп: недостаточно средств ProxyAPI (стр. ${pr.current_page ?? "?"}/${pr.total ?? total})`;
    }
    return `${kind} · ошибка на стр. ${pr.current_page ?? "?"} — ${String(err).slice(0, 80)}`;
  }
  return `${kind} · ${pipe.status}`;
}

function prTotal(p) {
  return p.pipeline?.progress?.total || p.pdf_pages || p.page_count || "?";
}

function isTranslate() {
  return (state.project?.task || state.project?.settings?.task) === "translate";
}

function translationCfg() {
  return state.project?.translation || state.project?.settings?.translation || {};
}

function canAgreeStyle() {
  return ["admin", "expert"].includes(state.user?.role);
}

function childTranslation() {
  const id = state.project?.id;
  if (!id) return null;
  return (state.projects || []).find(
    (p) => String(p.source_project_id || "") === String(id) && (p.task || p.settings?.task) === "translate"
  );
}

function syncExportButtons() {
  const tr = isTranslate();
  const pdf = $("#btn-export-pdf");
  const pdfI = $("#btn-export-interleave");
  const docx = $("#btn-export-docx");
  const docxI = $("#btn-export-docx-interleave");
  const trPdf = $("#btn-export-tr-pdf");
  const trDocx = $("#btn-export-tr-docx");
  if (pdf) pdf.hidden = false;
  if (docx) docx.hidden = false;
  if (pdfI) pdfI.hidden = tr;
  if (docxI) docxI.hidden = tr;
  if (trPdf) trPdf.hidden = true;
  if (trDocx) trDocx.hidden = true;
}

function syncTaskUi() {
  const tr = isTranslate();
  const p = state.project;
  const cfg = translationCfg();
  const agreed = Boolean(cfg.agreed);
  const leftTitle = $("#left-pane-title");
  const leftSub = $("#left-pane-sub");
  const rightSub = $("#right-pane-sub");
  if (leftTitle) leftTitle.textContent = tr ? "Санскрит" : "Скан";
  if (leftSub) leftSub.textContent = tr ? "выверенный текст" : "как в PDF";
  if (rightSub) {
    rightSub.textContent = tr ? "русский" : "देवनागरी";
    rightSub.classList.toggle("sa", !tr);
  }
  const scanWrap = $("#left-scan-wrap");
  const sourceBox = $("#left-source-html");
  if (scanWrap) scanWrap.hidden = tr;
  if (sourceBox) sourceBox.hidden = !tr;

  const styleBar = $("#style-bar");
  if (styleBar) styleBar.hidden = !tr;
  if (tr) {
    const sel = $("#style-select");
    const eng = $("#style-english");
    const notes = $("#style-notes");
    if (sel && cfg.style) sel.value = cfg.style;
    if (eng && cfg.english_comments) eng.value = cfg.english_comments;
    if (notes) notes.value = cfg.notes || "";
    const lock = agreed || !canAgreeStyle();
    if (sel) sel.disabled = lock;
    if (eng) eng.disabled = lock;
    if (notes) notes.disabled = lock;
    const agreeBtn = $("#btn-agree-style");
    const revokeBtn = $("#btn-revoke-style");
    const mark = $("#style-agreed-mark");
    if (agreeBtn) agreeBtn.hidden = !canAgreeStyle() || agreed;
    if (revokeBtn) revokeBtn.hidden = !canAgreeStyle() || !agreed;
    if (mark) mark.textContent = agreed ? "согласован" : "не согласован — LLM не запустится";
  }

  const dir = $("#directive-input");
  if (dir) {
    dir.placeholder = tr
      ? "Замечание к переводу — или нажмите «Перевести страницу»."
      : "С чем не согласны — или: пересмотри страницу.";
  }
  const proof = $("#btn-proofread");
  if (proof) {
    proof.hidden = false;
    proof.title = tr
      ? "Смысловая проверка перевода: обрывы, стык страниц, санскрит, смысл"
      : "Второй проход: смысловая проверка со сканом";
  }
  const review = $("#btn-review-again");
  if (review) review.hidden = tr;
  const trPage = $("#btn-translate-page");
  if (trPage) {
    trPage.hidden = !tr;
    trPage.disabled = tr && !agreed;
    trPage.title = agreed ? "LLM переводит эту страницу по согласованному шаблону" : "Сначала согласуйте шаблон";
  }
  syncTranslateAllButtons();
  syncProofreadAllButtons();

  const openTr = $("#btn-open-translate");
  const back = $("#btn-back-source");
  if (openTr) {
    if (tr) {
      openTr.hidden = true;
    } else {
      const child = childTranslation();
      const canSpawn = canAgreeStyle();
      openTr.hidden = !child && !canSpawn;
      openTr.textContent = child ? "Открыть перевод" : "Перевод на русский";
      openTr.classList.toggle("primary", !child);
    }
  }
  if (back) {
    back.hidden = !tr || !p?.source_project_id;
  }
  syncExportButtons();
}

function canRunTranslateAll() {
  return ["admin", "expert", "scholar"].includes(state.user?.role);
}

function pipelineBusy() {
  return (
    !!state.project?.pipeline &&
    ["queued", "running"].includes(state.project.pipeline.status)
  );
}

function isProofreadJob() {
  return state.project?.pipeline?.progress?.scope === "translate_proofread";
}

function syncTranslateAllButtons() {
  const tr = isTranslate();
  const agreed = Boolean(translationCfg().agreed);
  const can = canRunTranslateAll();
  const busy = pipelineBusy();
  const proofBusy = busy && isProofreadJob();
  const openOnly = state.thumbFilter === "open";
  const label = busy && !proofBusy
    ? "Идёт перевод…"
    : openOnly
      ? "Перевести несогласованные"
      : "Перевести все";
  const title = !agreed
    ? "Сначала согласуйте шаблон перевода"
    : openOnly
      ? "Перевести страницы без согласия (фильтр включён)"
      : "Перевести все страницы книги";
  const ids = ["btn-translate-all"];
  for (const id of ids) {
    const el = $(`#${id}`);
    if (!el) continue;
    el.hidden = !tr || !can;
    el.disabled = !agreed || busy;
    el.textContent = label;
    el.title = title;
  }
}

function syncProofreadAllButtons() {
  const tr = isTranslate();
  const busy = pipelineBusy();
  const proofBusy = busy && isProofreadJob();
  const openOnly = state.thumbFilter === "open";
  const label = proofBusy
    ? "Идёт проверка…"
    : openOnly
      ? "Проверить несогласованные"
      : "Смысловая проверка всех";
  const title = openOnly
    ? "Смысловая проверка страниц без согласия (фильтр включён)"
    : "Пройти весь перевод: грубые обрывы исправить, тонкие — пометить";
  for (const id of ["btn-proofread-all"]) {
    const el = $(`#${id}`);
    if (!el) continue;
    el.hidden = !tr;
    el.disabled = busy;
    el.textContent = label;
    el.title = title;
  }
}

function updatePipelineBar() {
  const p = state.project;
  if (!p) return;
  $("#pipeline-info").textContent = pipelineLabel(p);
  const btn = $("#btn-start-pipeline");
  const tr = isTranslate();
  const busy = p.pipeline && ["queued", "running"].includes(p.pipeline.status);

  if (tr) {
    if (btn) btn.hidden = true;
    syncTranslateAllButtons();
    syncProofreadAllButtons();
    syncTaskUi();
    return;
  }

  if (state.user?.role !== "admin") {
    btn.hidden = true;
    syncTaskUi();
    return;
  }
  btn.hidden = false;
  btn.disabled = !!busy;
  if (p.confirm_required || p.status === "awaiting_confirm") {
    btn.disabled = false;
    btn.textContent = "Подтвердить перевод всей книги";
    syncTaskUi();
    return;
  }
  btn.textContent = busy ? "Идёт перевод всей книги…" : "Перевести всю книгу заново";
  syncTaskUi();
}

function formatTokens(n) {
  if (n == null) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function usageLabel(u, llm) {
  if (!u || !u.totals) return "Расход LLM: —";
  const t = u.totals;
  const nets = (u.by_network || [])
    .map((n) => `${n.network}: ${formatTokens(n.total_tokens)} (${n.calls})`)
    .join(" · ");
  const usd =
    u.est_usd_total != null ? ` · ≈ $${Number(u.est_usd_total).toFixed(4)}` : "";
  const live = llm?.route_label
    ? `Сейчас: ${llm.key_source === "personal" ? "свой ключ · " : "бэкофис · "}${llm.route_label}${llm.route_model ? " (" + llm.route_model + ")" : ""}. `
    : llm?.message
      ? `Сейчас: ${llm.message}. `
      : "";
  return (
    live +
    `Расход проекта: ${formatTokens(t.total_tokens)} ток. / ${t.calls} вызов.` +
    (nets ? ` · ${nets}` : "") +
    usd
  );
}

async function loadUsage() {
  const bar = $("#usage-bar");
  const info = $("#usage-info");
  if (!state.project || !bar || !info) return;
  const role = state.user?.role;
  if (!["admin", "expert", "scholar"].includes(role)) {
    bar.hidden = true;
    return;
  }
  try {
    const [usage, llm] = await Promise.all([
      api(`/projects/${state.project.id}/usage`),
      api("/system/llm-status").catch(() => null),
    ]);
    state.usage = usage;
    info.textContent = usageLabel(state.usage, llm);
    bar.hidden = false;
  } catch (_) {
    info.textContent = "Расход LLM: не удалось загрузить";
    bar.hidden = false;
  }
}

async function openProject(id) {
  try {
    state.projects = await api("/projects");
  } catch (_) {}
  state.project = await api(`/projects/${id}`);
  state.pages = await api(`/projects/${id}/pages`);
  $("#proj-title").textContent = state.project.title;
  $("#proj-meta").textContent = `${state.project.slug} · согласовано ${state.project.accepted ?? 0} · на правке ${state.project.draft_ready ?? 0}`;
  updatePipelineBar();
  await loadUsage();
  $("#page-total").textContent = String(state.pages.length || state.project.pdf_pages || 0);
  $("#page-jump").max = String(state.pages.length || 1);
  renderThumbList();
  clearDraftSearchUi(false);
  showView("editor");
  const pipeBusy =
    state.project.pipeline &&
    ["queued", "running"].includes(state.project.pipeline.status);
  if (pipeBusy || !isTranslate()) {
    startPipelinePoll();
  } else if (pipelineTimer) {
    clearInterval(pipelineTimer);
    pipelineTimer = null;
  }
  if (state.pages.length) {
    const keep =
      state.page && state.pages.some((p) => p.id === state.page.id)
        ? state.page.id
        : state.pages[0].id;
    await loadPage(keep);
  } else {
    setDraftHtml("");
    $("#scan-img").removeAttribute("src");
  }
}

const thumbBlobCache = new Map();
let thumbObserver = null;

function statusBadgeClass(status) {
  if (status === "expert_done") return "ok";
  if (status === "expert_review") return "edit";
  return "wait";
}

/** Pages visible in the thumb rail (and for ‹ › when filter is on). */
function visiblePages() {
  if (state.thumbFilter !== "open") return state.pages;
  return state.pages.filter((p) => p.status !== "expert_done");
}

function renderThumbList() {
  const list = $("#thumb-list");
  const shown = visiblePages();
  const total = state.pages.length;
  const countEl = $("#thumb-count");
  if (state.thumbFilter === "open" && shown.length !== total) {
    countEl.textContent = `${shown.length} / ${total}`;
  } else {
    countEl.textContent = String(total);
  }
  const filterCb = $("#thumb-filter-open");
  if (filterCb) filterCb.checked = state.thumbFilter === "open";

  if (!shown.length) {
    list.innerHTML =
      state.thumbFilter === "open" && total
        ? `<p class="thumb-list-empty">Все страницы согласованы.<br>Снимите фильтр, чтобы видеть все.</p>`
        : `<p class="thumb-list-empty">Нет страниц</p>`;
    if (thumbObserver) thumbObserver.disconnect();
    return;
  }

  const hitIds = new Set((state.draftHits || []).map((h) => String(h.page_id)));
  list.innerHTML = shown
    .map(
      (p) => `
    <button type="button" class="thumb-item${p.proof_n ? " has-proof" : ""}${
      hitIds.has(String(p.id)) ? " search-hit" : ""
    }" data-id="${p.id}" data-no="${p.page_no}" title="${
        p.proof_n
          ? `Стр. ${p.page_no} · замечаний смысловой проверки: ${p.proof_n}`
          : `Стр. ${p.page_no}`
      }">
      <div class="thumb-frame ${p.has_scan ? "" : "pending"}">
        ${
          p.has_scan
            ? `<img alt="" data-scan-page="${p.id}" />`
            : `<span class="ph">стр. ${p.page_no}<br>${
                p.has_source_html ? "санскрит" : p.has_html ? "текст" : "в очереди"
              }</span>`
        }
      </div>
      <div class="thumb-meta">
        <span>${p.page_no}${p.proof_n ? ` · ${p.proof_n}` : ""}</span>
        <span class="thumb-badge ${statusBadgeClass(p.status)}" title="${p.status}"></span>
      </div>
    </button>`
    )
    .join("");
  list.querySelectorAll(".thumb-item").forEach((el) => {
    el.onclick = () => loadPage(el.dataset.id);
  });
  if (thumbObserver) thumbObserver.disconnect();
  thumbObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const img = entry.target;
        const pageId = img.dataset.scanPage;
        if (!pageId || img.dataset.loaded) return;
        img.dataset.loaded = "1";
        loadThumb(pageId, img);
      });
    },
    { root: list, rootMargin: "120px 0px" }
  );
  list.querySelectorAll("img[data-scan-page]").forEach((img) => thumbObserver.observe(img));
  highlightThumb(state.page?.id);
}

function setThumbFilter(openOnly) {
  state.thumbFilter = openOnly ? "open" : "all";
  localStorage.setItem("ss_thumb_filter", state.thumbFilter);
  renderThumbList();
}

function indexOfFold(hay, needle, from) {
  if (!hay || !needle) return -1;
  const exact = hay.indexOf(needle, from);
  if (exact >= 0) return exact;
  const hl = hay.toLocaleLowerCase();
  const nl = needle.toLocaleLowerCase();
  if (hl.length === hay.length && nl.length === needle.length) {
    return hl.indexOf(nl, from);
  }
  return -1;
}

function unwrapDraftMarks(root) {
  if (!root) return;
  root.querySelectorAll("mark.draft-hl").forEach((m) => {
    const parent = m.parentNode;
    if (!parent) return;
    while (m.firstChild) parent.insertBefore(m.firstChild, m);
    parent.removeChild(m);
    parent.normalize();
  });
}

function highlightDraftQuery(root) {
  unwrapDraftMarks(root);
  const query = (state.draftQuery || "").trim();
  if (!root || !query) return 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue) return NodeFilter.FILTER_REJECT;
      if (node.parentElement?.closest("mark.draft-hl, script, style")) {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  let n = 0;
  for (const node of nodes) {
    const text = node.nodeValue;
    let from = 0;
    const pieces = [];
    while (true) {
      const i = indexOfFold(text, query, from);
      if (i < 0) break;
      if (i > from) pieces.push(document.createTextNode(text.slice(from, i)));
      const mark = document.createElement("mark");
      mark.className = n === 0 ? "draft-hl draft-hl-current" : "draft-hl";
      mark.textContent = text.slice(i, i + query.length);
      pieces.push(mark);
      n += 1;
      from = i + Math.max(query.length, 1);
    }
    if (!pieces.length) continue;
    if (from < text.length) pieces.push(document.createTextNode(text.slice(from)));
    const parent = node.parentNode;
    if (!parent) continue;
    for (const p of pieces) parent.insertBefore(p, node);
    parent.removeChild(node);
  }
  const current = root.querySelector("mark.draft-hl-current");
  if (current) current.scrollIntoView({ block: "nearest", inline: "nearest" });
  return n;
}

function clearDraftSearchUi(clearInput = true) {
  state.draftQuery = "";
  state.draftHits = [];
  const hits = $("#draft-search-hits");
  const meta = $("#draft-search-meta");
  if (hits) {
    hits.hidden = true;
    hits.innerHTML = "";
  }
  if (meta) meta.textContent = "";
  if (clearInput && $("#draft-search")) $("#draft-search").value = "";
  renderThumbList();
  highlightDraftQuery($("#html-preview"));
  highlightDraftQuery($("#left-source-html"));
}

function renderDraftSearchHits(data) {
  const box = $("#draft-search-hits");
  const meta = $("#draft-search-meta");
  if (!box) return;
  const hits = data.hits || [];
  state.draftHits = hits;
  if (meta) {
    meta.textContent = hits.length
      ? `${data.page_hits} стр. · ${data.total_matches} совп.${data.truncated ? "…" : ""}`
      : data.query
        ? "нет"
        : "";
  }
  if (!hits.length) {
    box.hidden = true;
    box.innerHTML = data.query ? `<p class="thumb-list-empty">В черновике не найдено</p>` : "";
    box.hidden = !data.query;
    return;
  }
  box.hidden = false;
  box.innerHTML = hits
    .map((h) => {
      const sn = (h.snippets || []).slice(0, 2).map((s) => `<span class="sn">${escapeHtml(s)}</span>`).join("");
      const active = state.page && String(state.page.id) === String(h.page_id) ? " active" : "";
      return `<button type="button" class="draft-search-hit${active}" data-id="${escapeHtml(h.page_id)}">
        <strong>стр. ${h.page_no}</strong> · ${h.count}
        ${sn}
      </button>`;
    })
    .join("");
  box.querySelectorAll(".draft-search-hit").forEach((el) => {
    el.onclick = () => loadPage(el.dataset.id);
  });
}

let draftSearchTimer = 0;
async function runDraftSearch(q) {
  if (!state.project) return;
  const query = String(q || "").trim();
  if (!query) {
    clearDraftSearchUi(false);
    return;
  }
  state.draftQuery = query;
  try {
    const data = await api(
      `/projects/${state.project.id}/search?q=${encodeURIComponent(query)}`
    );
    if (state.draftQuery !== query) return;
    renderDraftSearchHits(data);
    renderThumbList();
    highlightDraftQuery($("#html-preview"));
    highlightDraftQuery($("#left-source-html"));
  } catch (e) {
    const meta = $("#draft-search-meta");
    if (meta) meta.textContent = e.message || "ошибка поиска";
  }
}

function scheduleDraftSearch() {
  const q = $("#draft-search")?.value || "";
  window.clearTimeout(draftSearchTimer);
  draftSearchTimer = window.setTimeout(() => runDraftSearch(q), 280);
}

async function loadThumb(pageId, imgEl) {
  try {
    if (thumbBlobCache.has(pageId)) {
      imgEl.src = thumbBlobCache.get(pageId);
      return;
    }
    const res = await fetch(`${API}/pages/${pageId}/scan`, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (!res.ok) return;
    const url = URL.createObjectURL(await res.blob());
    thumbBlobCache.set(pageId, url);
    imgEl.src = url;
  } catch (_) {}
}

function highlightThumb(pageId) {
  $$(".thumb-item").forEach((el) => {
    const on = el.dataset.id === pageId;
    el.classList.toggle("active", on);
    if (on) el.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
}

function updateEditMode() {
  const hasHtml = Boolean((state.page?.current_html || "").trim());
  const hasSource = Boolean((state.page?.source_html || "").trim());
  const accepted = state.page?.status === "expert_done" && hasHtml;
  const pendingDraft = isTranslate()
    ? !hasSource && !hasHtml
    : !hasHtml &&
      ["pending", "extracting", "llm_draft", "ocr"].includes(state.page?.status || "");
  $("#accepted-box").hidden = !accepted;
  $("#edit-tools").hidden = accepted || pendingDraft;
  const srcTab = document.querySelector('.tab[data-tab="source"]');
  if (srcTab) srcTab.hidden = accepted;
  const wyTab = document.querySelector('.tab[data-tab="wysiwyg"]');
  if (wyTab) wyTab.hidden = accepted;
  if (accepted) switchTab("preview");
  $("#html-editor").readOnly = accepted || pendingDraft;
  const wyBox = $("#html-wysiwyg");
  if (wyBox && !wyBox.querySelector(".wy-empty")) {
    markWysiwygEditable(wyBox, !(accepted || pendingDraft));
  }
  const agreed = Boolean(translationCfg().agreed);
  const proofBtn = $("#btn-proofread");
  if (proofBtn) {
    proofBtn.disabled = accepted || pendingDraft || !hasHtml;
  }
  if (isTranslate()) {
    const trPage = $("#btn-translate-page");
    if (trPage) trPage.disabled = !agreed || accepted;
    const revise = $("#btn-revise");
    if (revise) revise.disabled = !agreed || accepted;
  } else {
    const revise = $("#btn-revise");
    if (revise) revise.disabled = accepted || pendingDraft;
  }
}

let pipelineTimer = null;
function startPipelinePoll() {
  if (pipelineTimer) clearInterval(pipelineTimer);
  pipelineTimer = setInterval(async () => {
    if (!state.project) return;
    try {
      const prevDone = state.project.pipeline?.progress?.done;
      const prevPage = state.project.pipeline?.progress?.current_page;
      const prevStatus = state.project.pipeline?.status;
      state.project = await api(`/projects/${state.project.id}`);
      updatePipelineBar();
      const curDone = state.project.pipeline?.progress?.done;
      const curPage = state.project.pipeline?.progress?.current_page;
      const curStatus = state.project.pipeline?.status;
      const changed =
        curDone !== prevDone || curPage !== prevPage || curStatus !== prevStatus;
      if (changed || ["queued", "running"].includes(curStatus)) {
        await loadUsage();
        const pageId = state.page?.id;
        const pageNo = state.page?.page_no;
        state.pages = await api(`/projects/${state.project.id}/pages`);
        $("#page-total").textContent = String(state.pages.length || state.project.pdf_pages || 0);
        renderThumbList();
        if (pageId) {
          const fresh = state.pages.find((p) => p.id === pageId);
          const proofScope = state.project.pipeline?.progress?.scope === "translate_proofread";
          const proofTouch = proofScope && (curPage === pageNo || curStatus === "done");
          if (fresh && (fresh.status !== state.page.status || (proofTouch && changed))) {
            await loadPage(pageId);
          }
        }
      }
      if (curStatus === "done" || curStatus === "failed") {
        clearInterval(pipelineTimer);
        pipelineTimer = null;
      }
    } catch (_) {}
  }, 8000);
}

function renderPreview(html) {
  const box = $("#html-preview");
  const src = (html || "").trim();
  if (!src) {
    box.innerHTML = isTranslate()
      ? `<p class="muted">Русский черновик ещё не готов. Согласуйте шаблон и нажмите «Перевести страницу».</p>`
      : `<p class="muted">Черновик ещё готовится (автоперевод) или пуст.</p>`;
    return;
  }
  // Draft HTML is trusted content from our pipeline / LLM, not arbitrary user HTML from the open web.
  box.innerHTML = src;
  hydratePreviewFigures(box);
  if (state.proofSuggestions?.length) highlightProofSuggestions(box, state.proofSuggestions);
  highlightDraftQuery(box);
}

/** /api/.../figures/* require Bearer — plain <img src> gets 401 and shows only alt text. */
async function hydratePreviewFigures(box) {
  if (!box || !state.token) return;
  const imgs = [...box.querySelectorAll('img[src*="/figures/"]')];
  await Promise.all(
    imgs.map(async (img) => {
      const url = img.getAttribute("src");
      if (!url || url.startsWith("blob:")) return;
      try {
        const res = await fetch(url, {
          headers: { Authorization: `Bearer ${state.token}` },
        });
        if (!res.ok) {
          img.alt = `illustration (${res.status})`;
          return;
        }
        const blob = await res.blob();
        if (!img.getAttribute("data-orig-src")) {
          img.setAttribute("data-orig-src", url);
        }
        img.src = URL.createObjectURL(blob);
      } catch (_) {
        /* leave broken img / alt */
      }
    })
  );
}

function setDraftHtml(html) {
  $("#html-editor").value = html || "";
  renderPreview(html || "");
  if ($("#tab-wysiwyg")?.classList.contains("active")) renderWysiwyg(html || "");
}

function draftTab() {
  // Digitize: stay on preview vs scan. Wysiwyg flush of figures can empty the editor
  // and then «Сохранить и согласовать» looks like it does nothing.
  return isTranslate() ? "wysiwyg" : "preview";
}

function isWysiwygActive() {
  return Boolean($("#tab-wysiwyg")?.classList.contains("active"));
}

function serializeWysiwyg(box) {
  const clone = box.cloneNode(true);
  clone.querySelectorAll("[contenteditable]").forEach((el) => el.removeAttribute("contenteditable"));
  clone.querySelectorAll(".wy-edit, .wy-lock, .wy-sa, .wy-ru").forEach((el) => {
    el.classList.remove("wy-edit", "wy-lock", "wy-sa", "wy-ru");
    if (!el.getAttribute("class")?.trim()) el.removeAttribute("class");
  });
  clone.querySelectorAll("span[style], font").forEach((el) => {
    if (el.closest(".sa")) return;
    const parent = el.parentNode;
    if (!parent) return;
    while (el.firstChild) parent.insertBefore(el.firstChild, el);
    parent.removeChild(el);
  });
  // Never persist blob: URLs from hydratePreviewFigures — restore API paths.
  clone.querySelectorAll("img").forEach((img) => {
    const orig = img.getAttribute("data-orig-src");
    const src = img.getAttribute("src") || "";
    if (orig) {
      img.setAttribute("src", orig);
      img.removeAttribute("data-orig-src");
    } else if (src.startsWith("blob:")) {
      img.removeAttribute("src");
    }
  });
  return clone.innerHTML;
}

function flushWysiwyg() {
  const box = $("#html-wysiwyg");
  if (!box || box.querySelector(".wy-empty")) return;
  if (!box.querySelector(".wy-edit, article, .page-style")) return;
  $("#html-editor").value = serializeWysiwyg(box);
}

function currentDraftHtml() {
  if (isWysiwygActive()) flushWysiwyg();
  return $("#html-editor").value;
}

function looksRussian(el) {
  return /[А-Яа-яЁёІіѢѣѲѳѴѵ]/.test(el.textContent || "");
}

function isSaLine(el) {
  if (el.classList.contains("ru") || el.classList.contains("tr") || el.classList.contains("note")) {
    return false;
  }
  if ((el.getAttribute("lang") || "").toLowerCase() === "ru") return false;
  if (looksRussian(el) && !el.classList.contains("sa")) return false;
  return el.classList.contains("sa") || (el.getAttribute("lang") || "").toLowerCase() === "sa";
}

function wysiwygBlocks(root) {
  return [
    ...new Set([
      ...root.querySelectorAll(
        "p, h1, h2, h3, h4, li, td, th, blockquote, figcaption, dt, dd, .running-head, .page-num, .footer"
      ),
    ]),
  ];
}

function setWysiwygEditable(el, enabled) {
  if (!enabled) {
    el.removeAttribute("contenteditable");
    el.classList.remove("wy-edit");
    return;
  }
  el.classList.add("wy-edit");
  el.spellcheck = true;
  try {
    el.contentEditable = "plaintext-only";
  } catch (_) {
    el.contentEditable = "true";
  }
  if (el.contentEditable !== "plaintext-only") el.contentEditable = "true";
}

function markWysiwygEditable(root, enabled) {
  root.querySelectorAll("[contenteditable]").forEach((el) => el.removeAttribute("contenteditable"));
  root.querySelectorAll(".wy-edit, .wy-lock, .wy-sa, .wy-ru").forEach((el) => {
    el.classList.remove("wy-edit", "wy-lock", "wy-sa", "wy-ru");
  });
  const blocks = wysiwygBlocks(root);
  blocks.forEach((el) => {
    if (isSaLine(el)) el.classList.add("wy-sa");
    else if (
      el.classList.contains("ru") ||
      el.classList.contains("tr") ||
      el.classList.contains("note") ||
      (el.getAttribute("lang") || "").toLowerCase() === "ru" ||
      looksRussian(el)
    ) {
      el.classList.add("wy-ru");
    }
    setWysiwygEditable(el, enabled);
  });
}

function seedRuAfterSa(html) {
  const wrap = document.createElement("div");
  wrap.innerHTML = html || "";
  const blocks = wysiwygBlocks(wrap);
  for (const el of blocks) {
    if (el.classList.contains("ru") || el.classList.contains("tr")) continue;
    const next = el.nextElementSibling;
    if (next && (next.classList.contains("ru") || next.classList.contains("tr"))) continue;
    const ru = document.createElement("p");
    ru.className = "ru tr";
    ru.setAttribute("lang", "ru");
    ru.appendChild(document.createElement("br"));
    el.insertAdjacentElement("afterend", ru);
  }
  return wrap.innerHTML;
}

function renderWysiwyg(html) {
  const box = $("#html-wysiwyg");
  const hint = $("#wysiwyg-hint");
  if (!box) return;
  if (hint) {
    hint.textContent = isTranslate()
      ? "Можно править и санскрит, и русский (разный цвет рамки). Теги не показываются."
      : "Правите санскрит прямо в строках, как в книге. Скан слева, теги не показываются.";
  }
  let src = (html || "").trim();
  if (!src && isTranslate() && (state.page?.source_html || "").trim()) {
    src = seedRuAfterSa(state.page.source_html);
    $("#html-editor").value = src;
  }
  if (!src) {
    box.innerHTML = `<p class="muted wy-empty">${
      isTranslate()
        ? "Черновика ещё нет — согласуйте шаблон и нажмите «Перевести страницу»."
        : "Черновик ещё готовится или пуст."
    }</p>`;
    return;
  }
  box.innerHTML = src;
  hydratePreviewFigures(box);
  markWysiwygEditable(box, !$("#html-editor")?.readOnly);
}

function switchTab(name) {
  if (isWysiwygActive() && name !== "wysiwyg") flushWysiwyg();
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "preview") renderPreview($("#html-editor").value);
  if (name === "wysiwyg") renderWysiwyg($("#html-editor").value);
  if (name === "chart") renderSaChart();
}

function insertPlainText(text) {
  if (document.execCommand("insertText", false, text)) return;
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return;
  sel.deleteContents();
  sel.getRangeAt(0).insertNode(document.createTextNode(text));
  sel.collapseToEnd();
}

function wyEditFromEvent(ev) {
  const el = ev.target?.nodeType === 1 ? ev.target : ev.target?.parentElement;
  return el?.closest?.(".wy-edit") || null;
}

function onWysiwygInput() {
  flushWysiwyg();
}

function onWysiwygPaste(ev) {
  if (!wyEditFromEvent(ev)) return;
  ev.preventDefault();
  insertPlainText(ev.clipboardData?.getData("text/plain") || "");
  flushWysiwyg();
}

function onWysiwygKeydown(ev) {
  if (ev.key !== "Enter") return;
  if (!wyEditFromEvent(ev)) return;
  ev.preventDefault();
  document.execCommand("insertLineBreak");
  flushWysiwyg();
}

/** Click Devanagari → copy to clipboard (for HTML / задания). */
const SA_CHART = {
  vowels: [
    ["अ", "a"], ["आ", "ā"], ["इ", "i"], ["ई", "ī"], ["उ", "u"], ["ऊ", "ū"],
    ["ऋ", "ṛ"], ["ॠ", "ṝ"], ["ऌ", "ḷ"], ["ए", "e"], ["ऐ", "ai"], ["ओ", "o"], ["औ", "au"],
    ["अं", "aṃ"], ["अः", "aḥ"],
  ],
  matras: [
    ["ा", "ā"], ["ि", "i"], ["ी", "ī"], ["ु", "u"], ["ू", "ū"], ["ृ", "ṛ"], ["ॄ", "ṝ"],
    ["ॢ", "ḷ"], ["े", "e"], ["ै", "ai"], ["ो", "o"], ["ौ", "au"],
    ["ं", "ṃ anusvāra"], ["ँ", "̃ candrabindu"], ["ꣳ", "Vedic ṃ"], ["ः", "ḥ"], ["्", "virāma"],
  ],
  /** Vedic tone marks — append after the marked syllable (य + ॒ → य॒). */
  svara: [
    ["॑", "U+0951 выше (svarita/udātta)"],
    ["॒", "U+0952 ниже (anudātta)"],
    ["य॑", "ya + ॑"],
    ["य॒", "ya + ॒"],
    ["त॑", "ta + ॑"],
    ["त॒", "ta + ॒"],
    ["वि॒", "vi + ॒"],
    ["रे॑", "re + ॑"],
    ["ण्यं॒", "ṇyaṃ + ॒"],
    ["गो॑", "go + ॑"],
    ["य॒ज्ञेन॑", "пример yajñena"],
  ],
  withAnusvara: [
    ["कं", "kaṃ"], ["खं", "khaṃ"], ["गं", "gaṃ"], ["घं", "ghaṃ"], ["ङं", "ṅaṃ"],
    ["चं", "caṃ"], ["जं", "jaṃ"], ["ञं", "ñaṃ"],
    ["टं", "ṭaṃ"], ["डं", "ḍaṃ"], ["णं", "ṇaṃ"],
    ["तं", "taṃ"], ["दं", "daṃ"], ["नं", "naṃ"],
    ["पं", "paṃ"], ["बं", "baṃ"], ["मं", "maṃ"],
    ["यं", "yaṃ"], ["रं", "raṃ"], ["लं", "laṃ"], ["वं", "vaṃ"],
    ["शं", "śaṃ"], ["षं", "ṣaṃ"], ["सं", "saṃ"], ["हं", "haṃ"],
    ["हꣳ", "haṃ Vedic"], ["गꣳ", "gaṃ Vedic"],
  ],
  consonants: [    ["Заднеязычные", [["क", "ka"], ["ख", "kha"], ["ग", "ga"], ["घ", "gha"], ["ङ", "ṅa"]]],
    ["Нёбные", [["च", "ca"], ["छ", "cha"], ["ज", "ja"], ["झ", "jha"], ["ञ", "ña"]]],
    ["Ретрофлексные", [["ट", "ṭa"], ["ठ", "ṭha"], ["ड", "ḍa"], ["ढ", "ḍha"], ["ण", "ṇa"]]],
    ["Зубные", [["त", "ta"], ["थ", "tha"], ["द", "da"], ["ध", "dha"], ["न", "na"]]],
    ["Губные", [["प", "pa"], ["फ", "pha"], ["ब", "ba"], ["भ", "bha"], ["म", "ma"]]],
    ["Полугласные", [["य", "ya"], ["र", "ra"], ["ल", "la"], ["व", "va"]]],
    ["Шипящие / h", [["श", "śa"], ["ष", "ṣa"], ["स", "sa"], ["ह", "ha"]]],
  ],
  ligatures: [
    ["ङ्ग", "ṅga", "часто путают с ज्ञ"],
    ["ज्ञ", "jña", "не ङ्ग"],
    ["ङ्क", "ṅka", ""],
    ["ङ्ख", "ṅkha", ""],
    ["ङ्घ", "ṅgha", ""],
    ["ञ्ज", "ñja", ""],
    ["ञ्च", "ñca", ""],
    ["ट्ट", "ṭṭa", ""],
    ["द्ध", "ddha", ""],
    ["त्त", "tta", ""],
    ["त्र", "tra", ""],
    ["त्व", "tva", ""],
    ["द्य", "dya", ""],
    ["द्व", "dva", ""],
    ["न्न", "nna", ""],
    ["प्र", "pra", ""],
    ["ब्र", "bra", ""],
    ["क्र", "kra", ""],
    ["ग्र", "gra", ""],
    ["श्र", "śra", ""],
    ["क्ष", "kṣa", ""],
    ["क्त", "kta", ""],
    ["प्त", "pta", ""],
    ["श्च", "śca", ""],
    ["ष्ठ", "ṣṭha", ""],
    ["स्त", "sta", ""],
    ["स्व", "sva", ""],
    ["ह्म", "hma", ""],
    ["ह्य", "hya", ""],
    ["हं", "haṃ", "не हंग"],
    ["हंस", "haṃsa", "лебедь; не हंगस"],
    ["ऽ", "avagraha", "не размножать ऽऽऽ"],
    ["ॐ", "oṃ", ""],
    ["।", "daṇḍa", ""],
    ["॥", "double daṇḍa", ""],
  ],
};

function copyTextFallback(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } finally {
    ta.remove();
  }
  return ok;
}

async function copySaChar(ch, iast) {
  if (!ch) {
    toast("Нечего копировать", true);
    return;
  }
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(ch);
      ok = true;
    }
  } catch (_) {
    ok = false;
  }
  if (!ok) ok = copyTextFallback(ch);
  if (ok) toast(`Скопировано: ${ch}${iast ? ` (${iast})` : ""}`);
  else toast("Не удалось скопировать", true);
}

function saPairButtons(pairs) {
  return pairs
    .map(
      ([sa, iast]) =>
        `<button type="button" class="sa-chip" data-sa="${escapeHtml(sa)}" data-iast="${escapeHtml(iast)}" title="${escapeHtml(iast)}">` +
        `<span class="sa">${escapeHtml(sa)}</span><span class="iast">${escapeHtml(iast)}</span></button>`
    )
    .join("");
}

function renderSaChart() {
  const box = $("#sa-chart");
  if (!box || box.dataset.ready === "1") return;
  let html = `<p class="muted sa-chart-hint">Клик по ячейке — копирует <strong>деванагари</strong> (для HTML и заданий). IAST — подсказка.</p>`;
  html += `<section><h3>Гласные (самостоятельные)</h3><div class="sa-chip-row">${saPairButtons(SA_CHART.vowels)}</div></section>`;
  html += `<section><h3>Матрā (знаки гласных / вирама / анусвара)</h3><div class="sa-chip-row">${saPairButtons(SA_CHART.matras)}</div></section>`;
  html += `<section><h3>Ведийские тоны (svara)</h3>`;
  html += `<p class="muted sa-chart-hint">Вставлять <strong>после</strong> слога. ॑ — чёрточка сверху, ॒ — снизу. Не путать с подчёркиванием слова.</p>`;
  html += `<div class="sa-chip-row">${saPairButtons(SA_CHART.svara)}</div></section>`;
  html += `<section><h3>Согласная + анусвара (клик = копировать слог)</h3><div class="sa-chip-row">${saPairButtons(SA_CHART.withAnusvara)}</div></section>`;
  html += `<section><h3>Согласные (знак · IAST)</h3>`;
  for (const [group, pairs] of SA_CHART.consonants) {
    html += `<h4>${escapeHtml(group)}</h4><div class="sa-chip-row">${saPairButtons(pairs)}</div>`;
  }
  html += `</section>`;
  html += `<section><h3>Частые лигатуры / знаки</h3><div class="sa-chip-row">`;
  html += SA_CHART.ligatures
    .map(
      ([sa, iast, note]) =>
        `<button type="button" class="sa-chip" data-sa="${escapeHtml(sa)}" data-iast="${escapeHtml(iast)}" title="${escapeHtml(note || iast)}">` +
        `<span class="sa">${escapeHtml(sa)}</span><span class="iast">${escapeHtml(iast)}</span>` +
        (note ? `<span class="note">${escapeHtml(note)}</span>` : "") +
        `</button>`
    )
    .join("");
  html += `</div></section>`;
  box.innerHTML = html;
  box.onclick = (ev) => {
    const btn = ev.target.closest(".sa-chip");
    if (!btn) return;
    copySaChar(btn.dataset.sa || "", btn.dataset.iast || "");
  };
  box.dataset.ready = "1";
}

async function loadPage(pageId) {
  clearProofread();
  state.page = await api(`/pages/${pageId}`);
  const st =
    state.page.status === "expert_done"
      ? "согласовано"
      : state.page.status === "expert_review"
        ? "на правке"
        : state.page.status;
  $("#page-status").textContent = st;
  $("#page-jump").value = String(state.page.page_no);
  $("#page-total").textContent = String(state.pages.length || state.project?.pdf_pages || 0);
  highlightThumb(pageId);
  $$(".draft-search-hit").forEach((el) => {
    el.classList.toggle("active", el.dataset.id === String(pageId));
  });
  setDraftHtml(state.page.current_html || "");
  const accepted =
    state.page.status === "expert_done" && Boolean((state.page.current_html || "").trim());
  switchTab(accepted ? "preview" : draftTab());
  updateEditMode();
  await renderLeftPane();
  const pendingNote = $("#proof-pending-note");
  const stored = state.page.proofread;
  if (accepted) {
    if (pendingNote) {
      const n = stored?.suggestions?.length || 0;
      pendingNote.hidden = !n;
      pendingNote.textContent = n
        ? `Есть замечания смысловой проверки (${n}). Отзовите согласие, чтобы просмотреть и применить.`
        : "";
    }
  } else if (stored?.suggestions?.length) {
    if (pendingNote) pendingNote.hidden = true;
    renderProofreadPanel(stored);
  } else if (pendingNote) {
    pendingNote.hidden = true;
  }
}

async function renderLeftPane() {
  const img = $("#scan-img");
  const empty = $("#scan-empty");
  const sourceBox = $("#left-source-html");
  if (isTranslate()) {
    if (img) {
      img.removeAttribute("src");
      img.hidden = true;
    }
    if (empty) empty.hidden = true;
    if (!sourceBox) return;
    const src = (state.page?.source_html || "").trim();
    sourceBox.innerHTML = src
      ? src
      : `<p class="muted">На этой странице нет выверенного санскрита.</p>`;
    if (src) await hydratePreviewFigures(sourceBox);
    highlightDraftQuery(sourceBox);
    return;
  }
  if (sourceBox) sourceBox.innerHTML = "";
  if (state.page.scan_url) {
    if (empty) empty.hidden = true;
    img.hidden = false;
    const blobRes = await fetch(state.page.scan_url, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (blobRes.ok) {
      const blob = await blobRes.blob();
      img.src = URL.createObjectURL(blob);
    }
  } else {
    img.removeAttribute("src");
    img.hidden = true;
    if (empty) {
      empty.hidden = false;
      empty.textContent =
        state.page.status === "pending" || state.page.status === "extracting"
          ? "Скан ещё не готов — конвейер дойдёт до этой страницы"
          : "Нет изображения скана";
    }
  }
}

function jumpToPageNo() {
  const n = Number($("#page-jump").value);
  if (!n) return;
  const page = state.pages.find((p) => p.page_no === n);
  if (!page) {
    toast(`Страницы ${n} нет`, true);
    if (state.page) $("#page-jump").value = String(state.page.page_no);
    return;
  }
  loadPage(page.id);
}

async function saveHtml() {
  if (!state.page) return;
  try {
    state.page = await api(`/pages/${state.page.id}`, {
      method: "PATCH",
      json: { html: currentDraftHtml(), note: "manual edit" },
    });
    setDraftHtml(state.page.current_html || $("#html-editor").value);
    $("#page-status").textContent = state.page.status;
    toast("Сохранено");
    // refresh page list statuses
    state.pages = await api(`/projects/${state.project.id}/pages`);
    renderThumbList();
  } catch (e) {
    toast(e.message, true);
  }
}

async function acceptPage() {
  if (!state.page) return;
  try {
    const saved = state.page.current_html || "";
    let html = saved;
    try {
      html = currentDraftHtml();
    } catch (_) {
      html = saved;
    }
    if (!(html || "").trim()) html = saved;
    if (!(html || "").trim()) {
      toast("Нет черновика — нечего согласовать", true);
      return;
    }
    if (html !== saved) {
      state.page = await api(`/pages/${state.page.id}`, {
        method: "PATCH",
        json: { html, note: "edit before accept" },
      });
      setDraftHtml(state.page.current_html || html);
    }
    const acceptedId = state.page.id;
    const openAfter = visiblePages().filter((p) => p.id !== acceptedId);
    const nextOpen = openAfter.find((p) => p.page_no > state.page.page_no) || openAfter[0] || null;
    state.page = await api(`/pages/${state.page.id}/accept`, { method: "POST" });
    $("#page-status").textContent = "согласовано";
    toast("Страница сохранена и принята");
    await openProject(state.project.id);
    if (state.thumbFilter === "open" && nextOpen) {
      await loadPage(nextOpen.id);
    }
  } catch (e) {
    toast(e.message, true);
  }
}

async function revokePage() {
  if (!state.page) return;
  try {
    state.page = await api(`/pages/${state.page.id}/revoke`, { method: "POST" });
    updateEditMode();
    $("#page-status").textContent =
      state.page.status === "expert_review" ? "на правке" : state.page.status;
    const filterNote =
      state.thumbFilter === "open"
        ? " В списке слева только несогласованные — остальные скрыты фильтром, не отозваны."
        : "";
    toast("Согласие отозвано — можно править заданием." + filterNote);
    if (state.project?.id) await openProject(state.project.id);
  } catch (e) {
    toast(e.message, true);
  }
}

async function runRevision(directive) {
  if (!state.page) return;
  const st = $("#revise-status");
  $("#btn-revise").disabled = true;
  $("#btn-review-again").disabled = true;
  const proofBtn = $("#btn-proofread");
  if (proofBtn) proofBtn.disabled = true;
  const trBtn = $("#btn-translate-page");
  if (trBtn) trBtn.disabled = true;
  st.textContent = isTranslate() ? "LLM переводит страницу… до 1–2 мин" : "LLM смотрит скан… до 1–2 мин";
  try {
    state.page = await api(`/pages/${state.page.id}/revise`, {
      method: "POST",
      json: { directive },
    });
    clearProofread();
    setDraftHtml(state.page.current_html || "");
    switchTab(draftTab());
    $("#page-status").textContent = state.page.status;
    toast("Черновик обновлён");
    st.textContent = "готово";
  } catch (e) {
    toast(e.message, true);
    st.textContent = "";
  } finally {
    $("#btn-revise").disabled = false;
    $("#btn-review-again").disabled = false;
    if (proofBtn) proofBtn.disabled = false;
    updateEditMode();
  }
}

async function revisePage() {
  const directive = $("#directive-input").value.trim();
  if (directive.length < 3) {
    toast(
      isTranslate()
        ? "Опишите правку или нажмите «Перевести страницу»"
        : "Опишите, с чем не согласны, или нажмите «Пересмотри страницу»",
      true
    );
    return;
  }
  await runRevision(directive);
}

async function reviewAgain() {
  const note = $("#directive-input").value.trim();
  if (note.length >= 3) {
    await runRevision(note);
    return;
  }
  const st = $("#revise-status");
  $("#btn-revise").disabled = true;
  $("#btn-review-again").disabled = true;
  st.textContent = "Пересмотр страницы…";
  try {
    state.page = await api(`/pages/${state.page.id}/review-again`, {
      method: "POST",
      json: {},
    });
    setDraftHtml(state.page.current_html || "");
    switchTab(draftTab());
    $("#page-status").textContent = state.page.status;
    toast("Страница пересмотрена");
    st.textContent = "готово";
  } catch (e) {
    toast(e.message, true);
    st.textContent = "";
  } finally {
    $("#btn-revise").disabled = false;
    $("#btn-review-again").disabled = false;
    updateEditMode();
  }
}

async function translatePage() {
  if (!state.page) return;
  if (!translationCfg().agreed) {
    toast("Сначала согласуйте шаблон перевода", true);
    return;
  }
  const st = $("#revise-status");
  const trBtn = $("#btn-translate-page");
  const reviseBtn = $("#btn-revise");
  if (trBtn) trBtn.disabled = true;
  if (reviseBtn) reviseBtn.disabled = true;
  st.textContent = "LLM переводит страницу… до 1–2 мин";
  try {
    const directive = $("#directive-input").value.trim();
    state.page = await api(`/pages/${state.page.id}/translate`, {
      method: "POST",
      json: { directive: directive.length >= 3 ? directive : null },
    });
    setDraftHtml(state.page.current_html || "");
    switchTab("wysiwyg");
    $("#page-status").textContent = state.page.status;
    toast("Черновик перевода готов");
    st.textContent = "готово";
    if (state.project?.id) {
      state.pages = await api(`/projects/${state.project.id}/pages`);
      renderThumbList();
    }
  } catch (e) {
    toast(e.message, true);
    st.textContent = "";
  } finally {
    updateEditMode();
  }
}

function showTranslateModal(show) {
  const modal = $("#translate-modal");
  if (modal) modal.hidden = !show;
}

function formField(form, name) {
  return form.elements.namedItem(name);
}

function openTranslateModal() {
  const p = state.project;
  if (!p) return;
  const form = $("#translate-form");
  if (!form) {
    toast("Форма перевода не найдена", true);
    return;
  }
  const slug = formField(form, "slug");
  const title = formField(form, "title");
  const style = formField(form, "style");
  const english = formField(form, "english_comments");
  const notes = formField(form, "notes");
  if (slug) slug.value = `${p.slug}-ru`.slice(0, 128);
  if (title) title.value = `${p.title} · перевод`;
  if (style) style.value = "interlinear";
  if (english) english.value = "replace";
  if (notes) notes.value = "";
  showTranslateModal(true);
}

async function onOpenTranslate() {
  const child = childTranslation();
  if (child) {
    await openProject(child.id);
    return;
  }
  openTranslateModal();
}

async function spawnTranslation(ev) {
  ev.preventDefault();
  if (!state.project) return;
  const form = ev.target;
  const btn = $("#btn-spawn-translate");
  const body = {
    slug: String(formField(form, "slug")?.value || "").trim().toLowerCase(),
    title: String(formField(form, "title")?.value || "").trim(),
    style: String(formField(form, "style")?.value || "interlinear"),
    english_comments: String(formField(form, "english_comments")?.value || "replace"),
    notes: String(formField(form, "notes")?.value || "").trim(),
  };
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Копируем санскрит…";
  }
  toast("Создаём проект перевода — для большой книги это может занять минуту");
  try {
    const dest = await api(`/projects/${state.project.id}/spawn-translation`, {
      method: "POST",
      json: body,
    });
    showTranslateModal(false);
    toast("Проект перевода создан — согласуйте шаблон");
    await openProject(dest.id);
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Создать проект перевода";
    }
  }
}

async function patchTranslationStyle(agree) {
  if (!state.project) return;
  try {
    state.project = await api(`/projects/${state.project.id}/translation-style`, {
      method: "PATCH",
      json: {
        style: $("#style-select").value,
        english_comments: $("#style-english").value,
        notes: $("#style-notes").value,
        agree,
      },
    });
    updatePipelineBar();
    updateEditMode();
    toast(agree ? "Шаблон согласован — можно переводить страницы" : "Согласование отозвано");
  } catch (e) {
    toast(e.message, true);
  }
}

function clearProofread() {
  state.proofSuggestions = [];
  const box = $("#proofread-box");
  if (box) box.hidden = true;
  const list = $("#proofread-list");
  if (list) list.innerHTML = "";
  const meta = $("#proofread-meta");
  if (meta) meta.textContent = "";
  const note = $("#proofread-note");
  if (note) note.textContent = "";
}

function selectedProofSuggestions() {
  return state.proofSuggestions.filter((s) => {
    const cb = document.querySelector(`#proofread-list input[data-id="${CSS.escape(s.id)}"]`);
    return cb && cb.checked;
  });
}

function syncProofHighlightClasses() {
  const selected = new Set(selectedProofSuggestions().map((s) => s.id));
  $$("#html-preview mark.proof-hit").forEach((mark) => {
    mark.classList.toggle("is-on", selected.has(mark.dataset.proofId));
  });
}

function highlightProofSuggestions(root, suggestions) {
  if (!root) return;
  // Unwrap previous marks
  root.querySelectorAll("mark.proof-hit").forEach((mark) => {
    const parent = mark.parentNode;
    while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
    parent.removeChild(mark);
  });
  root.normalize();
  const selected = new Set(
    suggestions
      .filter((s) => {
        const cb = document.querySelector(`#proofread-list input[data-id="${CSS.escape(s.id)}"]`);
        return !cb || cb.checked;
      })
      .map((s) => s.id)
  );
  // Longer strings first
  const ordered = [...suggestions].sort((a, b) => b.wrong.length - a.wrong.length);
  for (const s of ordered) {
    if (!s.wrong) continue;
    highlightFirstTextMatch(root, s.wrong, s.id, selected.has(s.id), s.reason || "");
  }
}

function highlightFirstTextMatch(root, needle, id, isOn, title) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !node.nodeValue.includes(needle)) return NodeFilter.FILTER_REJECT;
      if (node.parentElement?.closest("mark.proof-hit")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const node = walker.nextNode();
  if (!node) return false;
  const text = node.nodeValue;
  const idx = text.indexOf(needle);
  if (idx < 0) return false;
  const before = text.slice(0, idx);
  const match = text.slice(idx, idx + needle.length);
  const after = text.slice(idx + needle.length);
  const mark = document.createElement("mark");
  mark.className = "proof-hit" + (isOn ? " is-on" : "");
  mark.dataset.proofId = id;
  mark.title = title || `${needle}`;
  mark.textContent = match;
  const parent = node.parentNode;
  const frag = document.createDocumentFragment();
  if (before) frag.appendChild(document.createTextNode(before));
  frag.appendChild(mark);
  if (after) frag.appendChild(document.createTextNode(after));
  parent.replaceChild(frag, node);
  return true;
}

function proofKindLabel(kind) {
  return (
    {
      incomplete: "обрыв",
      join: "стык стр.",
      sanskrit: "санскрит",
      sense: "смысл",
    }[kind] || ""
  );
}

function proofTargetLabel(target) {
  return (
    {
      source: "слева",
      both: "оба",
    }[target] || ""
  );
}

function renderProofreadPanel(result) {
  const box = $("#proofread-box");
  const list = $("#proofread-list");
  if (!box || !list) return;
  state.proofSuggestions = result.suggestions || [];
  $("#proofread-meta").textContent = result.model ? `модель: ${result.model}` : "";
  $("#proofread-note").textContent = result.note || "";
  if (!state.proofSuggestions.length) {
    list.innerHTML = `<p class="muted" style="margin:0">Нечего применять — можно закрыть.</p>`;
    box.hidden = false;
    renderPreview($("#html-editor").value);
    return;
  }
  list.innerHTML = state.proofSuggestions
    .map((s) => {
      const kind = proofKindLabel(s.kind);
      const tgt = proofTargetLabel(s.target);
      const tags = [kind, tgt].filter(Boolean)
        .map((t) => `<span class="kind">${escapeHtml(t)}</span>`)
        .join("");
      return `<label class="proof-item">
      <input type="checkbox" checked data-id="${escapeHtml(s.id)}" />
      <span>
        <span class="sev">${escapeHtml(s.severity || "medium")}</span>${tags}
        <div class="sa-pair"><span class="sa">${escapeHtml(s.wrong)}</span> → <span class="sa">${escapeHtml(s.right)}</span></div>
        <div class="reason">${escapeHtml(s.reason || "")}</div>
      </span>
    </label>`;
    })
    .join("");
  list.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.onchange = () => syncProofHighlightClasses();
  });
  box.hidden = false;
  switchTab("preview");
  renderPreview($("#html-editor").value);
  const left = $("#left-source-html");
  if (left && isTranslate()) {
    const srcItems = state.proofSuggestions.filter(
      (s) => s.target === "source" || s.target === "both"
    );
    if (srcItems.length) highlightProofSuggestions(left, srcItems);
  }
}

async function runProofread() {
  if (!state.page) return;
  const st = $("#revise-status");
  const btn = $("#btn-proofread");
  $("#btn-revise").disabled = true;
  $("#btn-review-again").disabled = true;
  if (btn) btn.disabled = true;
  st.textContent = isTranslate()
    ? "Смысловая проверка перевода… до 1–2 мин"
    : "Смысловая проверка… до 1–2 мин";
  try {
    const result = await api(`/pages/${state.page.id}/proofread`, { method: "POST" });
    renderProofreadPanel(result);
    toast(
      result.suggestions?.length
        ? `Предложений: ${result.suggestions.length} — отметьте и примените`
        : "Подозрительных мест не найдено"
    );
    st.textContent = "проверка готова";
    if (state.project?.id) {
      state.pages = await api(`/projects/${state.project.id}/pages`);
      renderThumbList();
    }
  } catch (e) {
    toast(e.message, true);
    st.textContent = "";
  } finally {
    $("#btn-revise").disabled = false;
    $("#btn-review-again").disabled = false;
    if (btn) btn.disabled = false;
  }
}

async function applySelectedProofs() {
  if (!state.page) return;
  const accepted = selectedProofSuggestions();
  if (!accepted.length) {
    toast("Отметьте хотя бы одно предложение", true);
    return;
  }
  try {
    state.page = await api(`/pages/${state.page.id}/proofread/apply`, {
      method: "POST",
      json: { accepted },
    });
    clearProofread();
    setDraftHtml(state.page.current_html || "");
    if (isTranslate()) await renderLeftPane();
    switchTab("preview");
    toast(`Применено правок: ${accepted.length}`);
    if (state.page.proofread?.suggestions?.length) {
      renderProofreadPanel(state.page.proofread);
    }
    if (state.project?.id) {
      state.pages = await api(`/projects/${state.project.id}/pages`);
      renderThumbList();
    }
  } catch (e) {
    toast(e.message, true);
  }
}

function setProofSelection(all) {
  $$("#proofread-list input[type=checkbox]").forEach((cb) => {
    cb.checked = all;
  });
  syncProofHighlightClasses();
}

async function startPipeline() {
  if (!state.project) return;
  const openOnly = state.thumbFilter === "open";
  const openCount = state.pages.filter((p) => p.status !== "expert_done").length;
  const total = state.pages.length;
  const n = openOnly ? openCount : total;
  if (!n) {
    toast(openOnly ? "Нет несогласованных страниц" : "Нет страниц", true);
    return;
  }
  const tr = isTranslate();
  let msg;
  if (tr) {
    msg = openOnly
      ? `Перевести ${n} несогласованных страниц?`
      : `Перевести все ${n} страниц, включая согласованные? Черновики будут перезаписаны.`;
  } else if (state.project.confirm_required || state.project.status === "awaiting_confirm") {
    msg = `Подтвердить перевод всей книги (${n} стр.)?`;
  } else {
    msg = openOnly
      ? `Перезапустить конвейер для ${n} несогласованных страниц?`
      : `Перезапустить конвейер для всех ${n} страниц, включая согласованные? Черновики будут перезаписаны.`;
  }
  if (!confirm(msg)) return;

  const params = new URLSearchParams();
  params.set("open_only", openOnly ? "true" : "false");
  if (!openOnly) params.set("force", "true");
  try {
    state.project = await api(`/projects/${state.project.id}/pipeline?${params}`, {
      method: "POST",
    });
    toast(tr ? "Запущен перевод страниц" : "Запущен перевод всей книги");
    updatePipelineBar();
    startPipelinePoll();
  } catch (e) {
    toast(e.message, true);
  }
}

async function startProofreadAll() {
  if (!state.project || !isTranslate()) return;
  const openOnly = state.thumbFilter === "open";
  const n = openOnly
    ? state.pages.filter((p) => p.status !== "expert_done" && p.has_html).length
    : state.pages.filter((p) => p.has_html).length;
  if (!n) {
    toast(openOnly ? "Нет несогласованных страниц с переводом" : "Нет страниц с переводом", true);
    return;
  }
  const msg = openOnly
    ? `Смысловая проверка ${n} несогласованных страниц?\nГрубые обрывы перевода будут исправлены; тонкие замечания появятся в списке на странице.`
    : `Проверить все ${n} страниц (включая согласованные)?\nНа согласованных правки не применятся сами — только пометки. Грубые обрывы на несогласованных исправятся.`;
  if (!confirm(msg)) return;
  const params = new URLSearchParams();
  params.set("proofread", "true");
  params.set("open_only", openOnly ? "true" : "false");
  try {
    state.project = await api(`/projects/${state.project.id}/pipeline?${params}`, {
      method: "POST",
    });
    toast("Запущена смысловая проверка перевода");
    updatePipelineBar();
    startPipelinePoll();
  } catch (e) {
    toast(e.message, true);
  }
}

async function exportDocument(fmt = "pdf", mode = "text", { rebuild = false } = {}) {
  if (!state.project) return;
  const ext = fmt === "docx" ? "docx" : "pdf";
  const kind = fmt === "docx" ? "DOCX" : "PDF";
  try {
    const label =
      mode === "interleave" ? `${kind} (скан‖текст)` : kind;
    toast(
      rebuild
        ? `Собираем ${label} (может занять 1–2 мин)…`
        : `Скачиваем ${label}…`
    );
    let askRebuild = rebuild;
    let res;
    for (let i = 0; i < 180; i++) {
      const params = new URLSearchParams();
      if (mode === "interleave") params.set("mode", "interleave");
      if (askRebuild) params.set("rebuild", "1");
      askRebuild = false;
      const q = params.toString() ? `?${params}` : "";
      res = await fetch(`${API}/projects/${state.project.id}/export.${ext}${q}`, {
        headers: { Authorization: `Bearer ${state.token}` },
      });
      if (res.status === 202) {
        toast(`Собираем ${label}…`);
        await new Promise((r) => setTimeout(r, 2000));
        continue;
      }
      break;
    }
    if (!res) throw new Error(`Не удалось начать сборку ${kind}`);
    if (res.status === 202) {
      throw new Error(`${kind} всё ещё собирается — нажмите ещё раз через минуту`);
    }
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      const detail = j.detail;
      throw new Error(
        typeof detail === "string" ? detail : detail?.message || res.statusText
      );
    }
    const blob = await res.blob();
    if (!blob.size) throw new Error(`Пустой ${kind} — пересоберите (Shift+клик)`);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download =
      mode === "interleave"
        ? `${state.project.slug}-interleave.${ext}`
        : `${state.project.slug}.${ext}`;
    a.click();
    URL.revokeObjectURL(url);
    toast(rebuild ? `${kind} собран и скачан` : `${kind} скачан`);
  } catch (e) {
    const raw = String(e.message || e);
    const msg =
      raw === "Failed to fetch"
        ? "Связь оборвалась при сборке/скачивании. Подождите и нажмите снова (готовый файл скачается быстро). Пересборка: Shift+клик."
        : raw;
    toast(msg, true);
  }
}

async function exportPdf(mode = "text", opts = {}) {
  return exportDocument("pdf", mode, opts);
}

async function exportDocx(mode = "text", opts = {}) {
  return exportDocument("docx", mode, opts);
}

async function loadAdmin() {
  if (state.user?.role !== "admin") return;
  const users = await api("/admin/users");
  $("#users-table").innerHTML = users
    .map(
      (u) => `<tr>
      <td>${escapeHtml(u.email)}</td>
      <td>${escapeHtml(u.login || "")}</td>
      <td>${escapeHtml(u.display_name)}</td>
      <td>${u.role}</td>
      <td>${u.is_active ? "да" : "нет"}</td>
      <td><label class="check-row"><input type="checkbox" class="allow-default-llm" data-user="${u.id}"${u.allow_default_llm ? " checked" : ""} /></label></td>
      <td>${u.has_openrouter_key || u.has_proxyapi_key ? "да" : "нет"}</td>
    </tr>`
    )
    .join("");
  $$(".allow-default-llm").forEach((el) => {
    el.onchange = async () => {
      try {
        await api(`/admin/users/${el.dataset.user}`, {
          method: "PATCH",
          json: { allow_default_llm: el.checked },
        });
        toast(el.checked ? "Токены бэкофиса разрешены" : "Только свои ключи");
      } catch (e) {
        toast(e.message, true);
        el.checked = !el.checked;
      }
    };
  });
  const cat = await api("/admin/llm-catalog");
  $("#llm-catalog").innerHTML = cat.models
    .map((m) => `<li><code>${m.provider}</code> · <strong>${escapeHtml(m.model)}</strong> — ${escapeHtml(m.label)}</li>`)
    .join("");
  $("#llm-note").textContent = cat.note;
  await loadLlmRoute();
  await loadAdminUsage();
}

function taskLabel(task) {
  return task === "translate" ? "перевод" : "оцифровка";
}

function networkLabel(net) {
  const id = String(net || "");
  const names = {
    openrouter: "OpenRouter",
    gemini: "Gemini",
    anthropic: "Anthropic",
    openai: "OpenAI",
  };
  return names[id] || id || "—";
}

async function loadAdminUsage() {
  const body = $("#usage-table");
  const foot = $("#usage-tfoot");
  if (!body) return;
  try {
    const data = await api("/admin/usage");
    const rows = [];
    for (const p of data.projects || []) {
      const nets = (p.by_network || []).filter((n) => n.calls);
      if (!nets.length) continue;
      for (const n of nets) {
        rows.push(`<tr>
          <td><strong>${escapeHtml(p.slug)}</strong><div class="muted">${escapeHtml(p.title || "")}</div></td>
          <td>${taskLabel(p.task)}</td>
          <td>${escapeHtml(networkLabel(n.network))}</td>
          <td class="num">${formatTokens(n.prompt_tokens)}</td>
          <td class="num">${formatTokens(n.completion_tokens)}</td>
          <td class="num">${formatTokens(n.total_tokens)}</td>
          <td class="num">${n.calls || 0}</td>
        </tr>`);
      }
    }
    body.innerHTML = rows.join("") || `<tr><td colspan="7" class="muted">Пока нет вызовов LLM</td></tr>`;
    const footRows = [];
    for (const n of data.by_network || []) {
      footRows.push(`<tr>
        <td colspan="2">Итого</td>
        <td>${escapeHtml(networkLabel(n.network))}</td>
        <td class="num">${formatTokens(n.prompt_tokens)}</td>
        <td class="num">${formatTokens(n.completion_tokens)}</td>
        <td class="num">${formatTokens(n.total_tokens)}</td>
        <td class="num">${n.calls || 0}</td>
      </tr>`);
    }
    const t = data.totals || {};
    footRows.push(`<tr>
      <td colspan="3">Все сети</td>
      <td class="num">${formatTokens(t.prompt_tokens)}</td>
      <td class="num">${formatTokens(t.completion_tokens)}</td>
      <td class="num">${formatTokens(t.total_tokens)}</td>
      <td class="num">${t.calls || 0}</td>
    </tr>`);
    foot.innerHTML = footRows.join("");
    renderUsageByUser($("#usage-by-user-table"), data.by_user || []);
  } catch (e) {
    body.innerHTML = `<tr><td colspan="7" class="muted">${escapeHtml(e.message || "не удалось загрузить")}</td></tr>`;
  }
}

async function loadLlmRoute() {
  const box = $("#llm-route-box");
  const status = $("#llm-route-status");
  if (!box || !status) return;
  try {
    const route = await api("/admin/llm-route");
    const options = route.options || [];
    box.innerHTML = options
      .map((opt) => {
        const primary = opt.primary || {};
        const detail = primary.provider
          ? `${primary.provider}:${primary.model} — ${opt.hint || ""}`
          : opt.hint || "";
        return `<label class="llm-route-opt">
          <input type="radio" name="llm-route" value="${escapeHtml(opt.id)}"${opt.id === route.route ? " checked" : ""} />
          <span>
            <strong>${escapeHtml(opt.label || opt.id)}</strong>
            <span class="muted llm-route-detail">${escapeHtml(detail)}</span>
          </span>
        </label>`;
      })
      .join("");
    box.querySelectorAll('input[name="llm-route"]').forEach((input) => {
      input.checked = input.value === route.route;
      input.onchange = async () => {
        if (!input.checked) return;
        try {
          const updated = await api("/admin/llm-route", {
            method: "PUT",
            json: { route: input.value },
          });
          toast(`Маршрут LLM: ${updated.label}`);
          status.textContent = `Активно: ${updated.label} · ${updated.primary.provider}:${updated.primary.model}`;
          const cat = await api("/admin/llm-catalog");
          $("#llm-note").textContent = cat.note;
        } catch (e) {
          toast(e.message, true);
          await loadLlmRoute();
        }
      };
    });
    status.textContent = `Активно: ${route.label} · ${route.primary.provider}:${route.primary.model}`;
  } catch (e) {
    status.textContent = e.message || "Не удалось загрузить маршрут";
  }
}

async function createUser(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = {
    email: String(fd.get("email") || "").trim(),
    display_name: String(fd.get("display_name") || "").trim(),
    login: String(fd.get("login") || "").trim() || undefined,
    password: String(fd.get("password") || ""),
    role: String(fd.get("role") || "expert"),
    allow_default_llm: Boolean(ev.target.allow_default_llm?.checked),
  };
  try {
    await api("/admin/users", { method: "POST", json: body });
    toast("Пользователь создан");
    ev.target.reset();
    if (ev.target.allow_default_llm) ev.target.allow_default_llm.checked = true;
    await loadAdmin();
  } catch (e) {
    toast(e.message, true);
  }
}

function keySourceLabel(source, hint) {
  if (source === "personal") {
    return hint ? `свой ключ · …${escapeHtml(hint)}` : "свой ключ";
  }
  return hint ? `бэкофис · …${escapeHtml(hint)}` : "бэкофис";
}

function renderUsageByUser(body, rows) {
  if (!body) return;
  const out = [];
  for (const u of rows || []) {
    const who = `${escapeHtml(u.display_name || "—")}${u.login ? `<div class="muted">${escapeHtml(u.login)}</div>` : ""}`;
    const nets = (u.by_network || []).filter((n) => n.calls);
    if (!nets.length) {
      out.push(`<tr>
        <td>${who}</td>
        <td>${keySourceLabel(u.key_source, u.key_hint)}</td>
        <td>—</td>
        <td class="num">${formatTokens(u.prompt_tokens)}</td>
        <td class="num">${formatTokens(u.completion_tokens)}</td>
        <td class="num">${formatTokens(u.total_tokens)}</td>
        <td class="num">${u.calls || 0}</td>
      </tr>`);
      continue;
    }
    for (const n of nets) {
      out.push(`<tr>
        <td>${who}</td>
        <td>${keySourceLabel(u.key_source, u.key_hint)}</td>
        <td>${escapeHtml(networkLabel(n.network))}</td>
        <td class="num">${formatTokens(n.prompt_tokens)}</td>
        <td class="num">${formatTokens(n.completion_tokens)}</td>
        <td class="num">${formatTokens(n.total_tokens)}</td>
        <td class="num">${n.calls || 0}</td>
      </tr>`);
    }
  }
  body.innerHTML = out.join("") || `<tr><td colspan="7" class="muted">Пока нет вызовов с учётом ключа</td></tr>`;
}

async function loadAccount() {
  const form = $("#account-profile-form");
  if (!form || !state.user) return;
  const me = await api("/auth/me");
  state.user = me;
  afterLogin();
  form.login.value = me.login || "";
  form.email.value = me.email || "";
  form.display_name.value = me.display_name || "";
  form.current_password.value = "";
  form.password.value = "";
  await loadAccountLlm();
  await loadAccountUsage();
}

async function loadAccountLlm() {
  const llm = await api("/auth/me/llm");
  const status = $("#account-llm-status");
  const hint = $("#account-default-hint");
  const useDef = $("#account-use-default");
  const resetBtn = $("#btn-llm-reset");
  if (status) {
    status.textContent = llm.use_default_llm
      ? `Сейчас: токены бэкофиса · ${llm.effective_label}`
      : `Сейчас: свои ключи · ${llm.effective_label}`;
  }
  if (hint) {
    hint.textContent = llm.allow_default_llm
      ? `Бэкофис: ${llm.default_label}. OpenRouter ${llm.default_openrouter_key ? "задан" : "не задан"}, ProxyAPI ${llm.default_proxyapi_key ? "задан" : "не задан"}.`
      : "Администратор не назначил вам токены сервера — нужен свой ключ OpenRouter или ProxyAPI.";
  }
  if (useDef) {
    useDef.checked = Boolean(llm.use_default_llm);
    useDef.disabled = !llm.allow_default_llm;
  }
  if (resetBtn) resetBtn.hidden = !llm.allow_default_llm;
  const orHint = $("#account-or-hint");
  const pxHint = $("#account-px-hint");
  if (orHint) orHint.textContent = llm.has_openrouter_key ? `Сохранён OpenRouter …${llm.openrouter_hint || ""}` : "Свой OpenRouter не задан";
  if (pxHint) pxHint.textContent = llm.has_proxyapi_key ? `Сохранён ProxyAPI …${llm.proxyapi_hint || ""}` : "Свой ProxyAPI не задан";
  const box = $("#account-llm-route-box");
  if (box) {
    const selected = llm.use_default_llm ? llm.default_route : (llm.llm_route || llm.effective_route);
    box.innerHTML = (llm.options || [])
      .map((opt) => {
        const primary = opt.primary || {};
        const detail = primary.provider
          ? `${primary.provider}:${primary.model} — ${opt.hint || ""}`
          : opt.hint || "";
        return `<label class="llm-route-opt">
          <input type="radio" name="account-llm-route" value="${escapeHtml(opt.id)}"${opt.id === selected ? " checked" : ""}${llm.use_default_llm ? " disabled" : ""} />
          <span>
            <strong>${escapeHtml(opt.label || opt.id)}</strong>
            <span class="muted llm-route-detail">${escapeHtml(detail)}</span>
          </span>
        </label>`;
      })
      .join("");
  }
}

async function loadAccountUsage() {
  const body = $("#account-usage-table");
  if (!body) return;
  try {
    const data = await api("/auth/me/usage");
    renderUsageByUser(body, data.by_user || []);
  } catch (e) {
    body.innerHTML = `<tr><td colspan="6" class="muted">${escapeHtml(e.message || "не удалось загрузить")}</td></tr>`;
  }
}

async function saveAccountProfile(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = {
    login: String(fd.get("login") || "").trim(),
    email: String(fd.get("email") || "").trim(),
    display_name: String(fd.get("display_name") || "").trim(),
  };
  const current = String(fd.get("current_password") || "");
  const next = String(fd.get("password") || "");
  if (current) body.current_password = current;
  if (next) {
    body.password = next;
    body.current_password = current;
  }
  try {
    state.user = await api("/auth/me", { method: "PATCH", json: body });
    afterLogin();
    toast("Профиль сохранён");
    ev.target.current_password.value = "";
    ev.target.password.value = "";
  } catch (e) {
    toast(e.message, true);
  }
}

async function saveAccountLlm(ev) {
  ev.preventDefault();
  const useDefault = Boolean($("#account-use-default")?.checked);
  const body = { use_default_llm: useDefault };
  const orKey = String(ev.target.openrouter_api_key.value || "").trim();
  const pxKey = String(ev.target.proxyapi_key.value || "").trim();
  if (orKey) body.openrouter_api_key = orKey;
  if (pxKey) body.proxyapi_key = pxKey;
  const picked = document.querySelector('input[name="account-llm-route"]:checked');
  if (picked && !useDefault) body.llm_route = picked.value;
  try {
    await api("/auth/me/llm", { method: "PATCH", json: body });
    ev.target.openrouter_api_key.value = "";
    ev.target.proxyapi_key.value = "";
    toast("Ключи сохранены");
    await loadAccountLlm();
  } catch (e) {
    toast(e.message, true);
  }
}

async function resetAccountLlm() {
  try {
    await api("/auth/me/llm/reset", { method: "POST", json: {} });
    toast("Снова токены бэкофиса");
    await loadAccountLlm();
  } catch (e) {
    toast(e.message, true);
  }
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function wire() {
  $("#login-form").onsubmit = login;
  $("#logout-btn").onclick = () => logout();
  $("#upload-form").onsubmit = createProject;
  $("#btn-confirm-whole-book").onclick = confirmWholeBook;
  $("#btn-cancel-whole-book").onclick = cancelWholeBook;
  $("#btn-save").onclick = saveHtml;
  $("#btn-accept").onclick = acceptPage;
  $("#btn-revoke").onclick = revokePage;
  $("#btn-revise").onclick = revisePage;
  $("#btn-review-again").onclick = reviewAgain;
  const btnProof = $("#btn-proofread");
  if (btnProof) btnProof.onclick = runProofread;
  const btnProofApply = $("#btn-proof-apply");
  if (btnProofApply) btnProofApply.onclick = applySelectedProofs;
  const btnProofAll = $("#btn-proof-all");
  if (btnProofAll) btnProofAll.onclick = () => setProofSelection(true);
  const btnProofNone = $("#btn-proof-none");
  if (btnProofNone) btnProofNone.onclick = () => setProofSelection(false);
  const btnProofDismiss = $("#btn-proof-dismiss");
  if (btnProofDismiss) {
    btnProofDismiss.onclick = () => {
      clearProofread();
      renderPreview($("#html-editor").value);
    };
  }
  $("#btn-start-pipeline").onclick = startPipeline;
  const btnTrAll = $("#btn-translate-all");
  if (btnTrAll) btnTrAll.onclick = startPipeline;
  const btnProofreadAll = $("#btn-proofread-all");
  if (btnProofreadAll) btnProofreadAll.onclick = startProofreadAll;
  const btnOpenTr = $("#btn-open-translate");
  if (btnOpenTr) btnOpenTr.onclick = () => onOpenTranslate();
  const btnBackSrc = $("#btn-back-source");
  if (btnBackSrc) {
    btnBackSrc.onclick = () => {
      if (state.project?.source_project_id) openProject(state.project.source_project_id);
    };
  }
  const btnAgree = $("#btn-agree-style");
  if (btnAgree) btnAgree.onclick = () => patchTranslationStyle(true);
  const btnRevokeStyle = $("#btn-revoke-style");
  if (btnRevokeStyle) btnRevokeStyle.onclick = () => patchTranslationStyle(false);
  const btnTrPage = $("#btn-translate-page");
  if (btnTrPage) btnTrPage.onclick = translatePage;
  const trForm = $("#translate-form");
  if (trForm) trForm.onsubmit = spawnTranslation;
  const btnCancelTr = $("#btn-cancel-translate");
  if (btnCancelTr) btnCancelTr.onclick = () => showTranslateModal(false);
  $("#btn-export-pdf").onclick = (e) => exportPdf("text", { rebuild: e.shiftKey });
  $("#btn-export-interleave").onclick = (e) =>
    exportPdf("interleave", { rebuild: e.shiftKey });
  $("#btn-export-interleave").title =
    "Скан и текст чередуются. Shift+клик — пересобрать заново.";
  $("#btn-export-docx").onclick = (e) => exportDocx("text", { rebuild: e.shiftKey });
  $("#btn-export-docx-interleave").onclick = (e) =>
    exportDocx("interleave", { rebuild: e.shiftKey });
  $("#btn-export-docx-interleave").title =
    "DOCX: скан и текст чередуются. Shift+клик — пересобрать заново.";
  $("#btn-export-docx").title = "DOCX из HTML. Shift+клик — пересобрать заново.";
  const btnTrPdf = $("#btn-export-tr-pdf");
  if (btnTrPdf) {
    btnTrPdf.onclick = (e) => exportPdf("text", { rebuild: e.shiftKey });
    btnTrPdf.title = "PDF русской версии. Первый клик собирает файл (дождитесь скачивания). Shift+клик — пересобрать.";
  }
  const btnTrDocx = $("#btn-export-tr-docx");
  if (btnTrDocx) {
    btnTrDocx.onclick = (e) => exportDocx("text", { rebuild: e.shiftKey });
    btnTrDocx.title = "Word (.docx): санскрит + русский. Shift+клик — пересобрать.";
  }
  const thumbFilter = $("#thumb-filter-open");
  if (thumbFilter) {
    thumbFilter.checked = state.thumbFilter === "open";
    thumbFilter.onchange = () => setThumbFilter(thumbFilter.checked);
  }
  const searchForm = $("#draft-search-form");
  const searchInput = $("#draft-search");
  if (searchForm) {
    searchForm.onsubmit = (ev) => {
      ev.preventDefault();
      window.clearTimeout(draftSearchTimer);
      runDraftSearch(searchInput?.value || "");
    };
  }
  if (searchInput) {
    searchInput.addEventListener("input", scheduleDraftSearch);
    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        clearDraftSearchUi(true);
      }
    });
  }
  $("#html-editor").addEventListener("input", () => renderPreview($("#html-editor").value));
  const wyBox = $("#html-wysiwyg");
  if (wyBox) {
    wyBox.addEventListener("input", onWysiwygInput);
    wyBox.addEventListener("paste", onWysiwygPaste);
    wyBox.addEventListener("keydown", onWysiwygKeydown);
  }
  $$(".tab").forEach((t) => {
    t.onclick = () => switchTab(t.dataset.tab);
  });
  $("#btn-prev").onclick = () => shiftPage(-1);
  $("#btn-next").onclick = () => shiftPage(1);
  $("#page-jump").addEventListener("change", jumpToPageNo);
  $("#page-jump").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      jumpToPageNo();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (!$("#view-editor").classList.contains("active")) return;
    if ((e.ctrlKey || e.metaKey) && (e.key === "f" || e.key === "F")) {
      const box = $("#draft-search");
      if (box) {
        e.preventDefault();
        box.focus();
        box.select();
      }
      return;
    }
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "TEXTAREA" || tag === "INPUT") return;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      shiftPage(-1);
    }
    if (e.key === "ArrowRight") {
      e.preventDefault();
      shiftPage(1);
    }
  });
  $("#user-form").onsubmit = createUser;
  const accForm = $("#account-profile-form");
  if (accForm) accForm.onsubmit = saveAccountProfile;
  const accLlm = $("#account-llm-form");
  if (accLlm) accLlm.onsubmit = saveAccountLlm;
  const resetLlm = $("#btn-llm-reset");
  if (resetLlm) resetLlm.onclick = resetAccountLlm;
  const useDef = $("#account-use-default");
  if (useDef) {
    useDef.onchange = () => {
      $$('input[name="account-llm-route"]').forEach((el) => {
        el.disabled = useDef.checked;
      });
    };
  }

  $$(".nav-btn").forEach((btn) => {
    btn.onclick = async () => {
      const view = btn.dataset.view;
      if (view === "projects") await loadProjects();
      if (view === "admin") await loadAdmin();
      if (view === "account") {
        try {
          await loadAccount();
        } catch (e) {
          toast(e.message, true);
          return;
        }
      }
      if (view === "editor" && !state.project) {
        toast("Сначала откройте проект", true);
        return;
      }
      showView(view);
    };
  });
}

function shiftPage(delta) {
  const pages = visiblePages();
  if (!pages.length || !state.page) return;
  let idx = pages.findIndex((p) => p.id === state.page.id);
  if (idx < 0) {
    // Current page hidden by filter — jump to nearest open page.
    const allIdx = state.pages.findIndex((p) => p.id === state.page.id);
    if (delta > 0) {
      const next = pages.find((p) => state.pages.findIndex((x) => x.id === p.id) > allIdx);
      if (next) loadPage(next.id);
      else if (pages[0]) loadPage(pages[0].id);
    } else {
      const prev = [...pages].reverse().find((p) => state.pages.findIndex((x) => x.id === p.id) < allIdx);
      if (prev) loadPage(prev.id);
      else if (pages[pages.length - 1]) loadPage(pages[pages.length - 1].id);
    }
    return;
  }
  const next = pages[idx + delta];
  if (!next) return;
  loadPage(next.id);
}

wire();
bootstrap();
