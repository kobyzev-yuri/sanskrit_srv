const API = "/api/v1";
const state = {
  token: localStorage.getItem("ss_token") || "",
  user: null,
  projects: [],
  project: null,
  pages: [],
  page: null,
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
    .map(
      (p) => `
    <article class="project-card" data-id="${p.id}">
      <h3>${escapeHtml(p.title)}</h3>
      <div class="meta sa">${escapeHtml(p.title_sa || "")}</div>
      <div class="meta">PDF ${p.pdf_pages ?? "?"} · согласовано ${p.accepted ?? 0} · на правке ${p.draft_ready ?? 0}</div>
      <div class="meta">${pipelineLabel(p)} · ${escapeHtml(p.slug)}</div>
    </article>`
    )
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
  const kind = sourceKindLabel(p);
  const pipe = p.pipeline;
  const total = prTotal(p);
  if (p.confirm_required || p.status === "awaiting_confirm") {
    return `${kind} · ${total} стр. (>100) — нужно подтверждение перевода всей книги`;
  }
  if (!pipe) return `${kind} · вся книга (${total} стр.) — конвейер не запущен`;
  const pr = pipe.progress || {};
  if (pipe.status === "running" || pipe.status === "queued") {
    const mode = (pr.source_kind || p.source_kind) === "text" ? "текст всей книги" : "перевод всей книги";
    return `${kind} · ${mode}: ${pr.done ?? 0}/${pr.total ?? total} (сейчас стр. ${pr.current_page ?? "…"})`;
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

function updatePipelineBar() {
  const p = state.project;
  if (!p) return;
  $("#pipeline-info").textContent = pipelineLabel(p);
  const btn = $("#btn-start-pipeline");
  const busy = p.pipeline && ["queued", "running"].includes(p.pipeline.status);
  btn.hidden = state.user?.role !== "admin";
  btn.disabled = !!busy;
  if (p.confirm_required || p.status === "awaiting_confirm") {
    btn.hidden = state.user?.role !== "admin";
    btn.disabled = false;
    btn.textContent = "Подтвердить перевод всей книги";
    return;
  }
  btn.textContent = busy ? "Идёт перевод всей книги…" : "Перевести всю книгу заново";
}

function formatTokens(n) {
  if (n == null) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function usageLabel(u) {
  if (!u || !u.totals) return "Расход LLM: —";
  const t = u.totals;
  const nets = (u.by_network || [])
    .map((n) => `${n.network}: ${formatTokens(n.total_tokens)} (${n.calls})`)
    .join(" · ");
  const usd =
    u.est_usd_total != null ? ` · ≈ $${Number(u.est_usd_total).toFixed(4)}` : "";
  return `Расход LLM: ${formatTokens(t.total_tokens)} ток. / ${t.calls} вызов.` +
    (nets ? ` · ${nets}` : "") +
    usd;
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
    state.usage = await api(`/projects/${state.project.id}/usage`);
    info.textContent = usageLabel(state.usage);
    bar.hidden = false;
  } catch (_) {
    info.textContent = "Расход LLM: не удалось загрузить";
    bar.hidden = false;
  }
}

async function openProject(id) {
  state.project = await api(`/projects/${id}`);
  state.pages = await api(`/projects/${id}/pages`);
  $("#proj-title").textContent = state.project.title;
  $("#proj-meta").textContent = `${state.project.slug} · согласовано ${state.project.accepted ?? 0} · на правке ${state.project.draft_ready ?? 0}`;
  updatePipelineBar();
  await loadUsage();
  $("#page-total").textContent = String(state.pages.length || state.project.pdf_pages || 0);
  $("#page-jump").max = String(state.pages.length || 1);
  renderThumbList();
  showView("editor");
  startPipelinePoll();
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

function renderThumbList() {
  const list = $("#thumb-list");
  $("#thumb-count").textContent = String(state.pages.length);
  list.innerHTML = state.pages
    .map(
      (p) => `
    <button type="button" class="thumb-item" data-id="${p.id}" data-no="${p.page_no}" title="Стр. ${p.page_no}">
      <div class="thumb-frame ${p.has_scan ? "" : "pending"}">
        ${
          p.has_scan
            ? `<img alt="" data-scan-page="${p.id}" />`
            : `<span class="ph">стр. ${p.page_no}<br>в очереди</span>`
        }
      </div>
      <div class="thumb-meta">
        <span>${p.page_no}</span>
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
  const accepted = state.page?.status === "expert_done" && hasHtml;
  const pendingDraft =
    !hasHtml &&
    ["pending", "extracting", "llm_draft", "ocr"].includes(state.page?.status || "");
  $("#accepted-box").hidden = !accepted;
  $("#edit-tools").hidden = accepted || pendingDraft;
  const srcTab = document.querySelector('.tab[data-tab="source"]');
  if (srcTab) srcTab.hidden = accepted;
  if (accepted) switchTab("preview");
  $("#html-editor").readOnly = accepted || pendingDraft;
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
      if (curDone !== prevDone) await loadUsage();
      const changed =
        curDone !== prevDone || curPage !== prevPage || curStatus !== prevStatus;
      if (changed || ["queued", "running"].includes(curStatus)) {
        const pageId = state.page?.id;
        const pageNo = state.page?.page_no;
        state.pages = await api(`/projects/${state.project.id}/pages`);
        $("#page-total").textContent = String(state.pages.length || state.project.pdf_pages || 0);
        renderThumbList();
        if (pageId) {
          const fresh = state.pages.find((p) => p.id === pageId);
          // Reload open page when its status/scan catches up.
          if (
            changed ||
            (fresh &&
              (fresh.status !== state.page.status ||
                fresh.has_scan !== Boolean(state.page.scan_url) ||
                curPage === pageNo))
          ) {
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
    box.innerHTML = `<p class="muted">Черновик ещё готовится (автоперевод) или пуст.</p>`;
    return;
  }
  // Draft HTML is trusted content from our pipeline / LLM, not arbitrary user HTML from the open web.
  box.innerHTML = src;
  hydratePreviewFigures(box);
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
}

function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "preview") renderPreview($("#html-editor").value);
}

async function loadPage(pageId) {
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
  setDraftHtml(state.page.current_html || "");
  switchTab("preview");
  updateEditMode();
  const img = $("#scan-img");
  const empty = $("#scan-empty");
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
      json: { html: $("#html-editor").value, note: "manual edit" },
    });
    setDraftHtml(state.page.current_html || $("#html-editor").value);
    $("#page-status").textContent = state.page.status;
    toast("Сохранено");
    // refresh page list statuses
    state.pages = await api(`/projects/${state.project.id}/pages`);
  } catch (e) {
    toast(e.message, true);
  }
}

async function acceptPage() {
  if (!state.page) return;
  try {
    // Direct HTML edit is first-class: persist textarea before acceptance.
    const html = $("#html-editor").value;
    if (html !== (state.page.current_html || "")) {
      state.page = await api(`/pages/${state.page.id}`, {
        method: "PATCH",
        json: { html, note: "edit before accept" },
      });
      setDraftHtml(state.page.current_html || html);
    }
    state.page = await api(`/pages/${state.page.id}/accept`, { method: "POST" });
    $("#page-status").textContent = state.page.status;
    toast("Страница сохранена и принята");
    await openProject(state.project.id);
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
    toast("Согласие отозвано — можно править заданием");
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
  st.textContent = "LLM смотрит скан… до 1–2 мин";
  try {
    state.page = await api(`/pages/${state.page.id}/revise`, {
      method: "POST",
      json: { directive },
    });
    setDraftHtml(state.page.current_html || "");
    switchTab("preview");
    $("#page-status").textContent = state.page.status;
    toast("Черновик обновлён");
    st.textContent = "готово";
  } catch (e) {
    toast(e.message, true);
    st.textContent = "";
  } finally {
    $("#btn-revise").disabled = false;
    $("#btn-review-again").disabled = false;
  }
}

async function revisePage() {
  const directive = $("#directive-input").value.trim();
  if (directive.length < 3) {
    toast("Опишите, с чем не согласны, или нажмите «Пересмотри страницу»", true);
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
    switchTab("preview");
    $("#page-status").textContent = state.page.status;
    toast("Страница пересмотрена");
    st.textContent = "готово";
  } catch (e) {
    toast(e.message, true);
    st.textContent = "";
  } finally {
    $("#btn-revise").disabled = false;
    $("#btn-review-again").disabled = false;
  }
}

async function startPipeline() {
  if (!state.project) return;
  try {
    state.project = await api(`/projects/${state.project.id}/pipeline`, { method: "POST" });
    toast("Запущен перевод всей книги");
    updatePipelineBar();
    startPipelinePoll();
  } catch (e) {
    toast(e.message, true);
  }
}

async function exportPdf(mode = "text") {
  if (!state.project) return;
  try {
    toast(mode === "interleave" ? "Собираем PDF (скан‖текст)…" : "Собираем PDF…");
    const q = mode === "interleave" ? "?mode=interleave" : "";
    const res = await fetch(`${API}/projects/${state.project.id}/export.pdf${q}`, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.detail || res.statusText);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download =
      mode === "interleave"
        ? `${state.project.slug}-interleave.pdf`
        : `${state.project.slug}.pdf`;
    a.click();
    URL.revokeObjectURL(url);
    toast("PDF скачан");
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadAdmin() {
  if (state.user?.role !== "admin") return;
  const users = await api("/admin/users");
  $("#users-table").innerHTML = users
    .map(
      (u) => `<tr>
      <td>${escapeHtml(u.email)}</td>
      <td>${escapeHtml(u.display_name)}</td>
      <td>${u.role}</td>
      <td>${u.is_active ? "да" : "нет"}</td>
    </tr>`
    )
    .join("");
  const cat = await api("/admin/llm-catalog");
  $("#llm-catalog").innerHTML = cat.models
    .map((m) => `<li><code>${m.provider}</code> · <strong>${escapeHtml(m.model)}</strong> — ${escapeHtml(m.label)}</li>`)
    .join("");
  $("#llm-note").textContent = cat.note;
}

async function createUser(ev) {
  ev.preventDefault();
  const body = Object.fromEntries(new FormData(ev.target).entries());
  try {
    await api("/admin/users", { method: "POST", json: body });
    toast("Пользователь создан");
    ev.target.reset();
    await loadAdmin();
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
  $("#btn-start-pipeline").onclick = startPipeline;
  $("#btn-export-pdf").onclick = () => exportPdf("text");
  $("#btn-export-interleave").onclick = () => exportPdf("interleave");
  $("#html-editor").addEventListener("input", () => renderPreview($("#html-editor").value));
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

  $$(".nav-btn").forEach((btn) => {
    btn.onclick = async () => {
      const view = btn.dataset.view;
      if (view === "projects") await loadProjects();
      if (view === "admin") await loadAdmin();
      if (view === "editor" && !state.project) {
        toast("Сначала откройте проект", true);
        return;
      }
      showView(view);
    };
  });
}

function shiftPage(delta) {
  if (!state.pages.length || !state.page) return;
  const idx = state.pages.findIndex((p) => p.id === state.page.id);
  const next = state.pages[idx + delta];
  if (!next) return;
  loadPage(next.id);
}

wire();
bootstrap();
