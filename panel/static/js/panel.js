/**
 * 3세대 음방시스템 컨트롤 패널
 */
(function () {
  "use strict";

  let socket = null;
  let csrfToken = "";
  let playlist = [];
  let currentIndex = -1;
  let playbackStatus = "stopped";
  let selectedDisplay = 0;
  let sortable = null;
  let socketConnected = false;
  let broadcastAllowed = false;

  const $ = (sel) => document.querySelector(sel);

  function isLocalAppView() {
    const h = location.hostname;
    return h === "127.0.0.1" || h === "localhost";
  }

  /** 네이티브 앱 패널(로그인 후 index) — 소켓 대기 없이 방송 UI 허용 */
  function isNativePanelSession() {
    return isLocalAppView();
  }

  function canUseBroadcastControls() {
    return socketConnected;
  }

  let lanPrimaryUrlFull = "";
  let websitePrimaryUrlFull = "";
  let displaysCache = null;
  let displaysLoading = null;
  let playbackCurrentSec = 0;
  let playbackDurationSec = 0;
  let isScrubbingProgress = false;

  let ytdlpBatchRunning = false;
  let ytdlpPrepareModalDismissed = false;

  function resetBroadcastPrepUi() {
    ytdlpBatchRunning = false;
    ytdlpPrepareModalDismissed = false;
    const modal = $("#ytdlpPrepareModal");
    if (modal) modal.hidden = true;
    const displayModal = $("#displayModal");
    if (displayModal) displayModal.hidden = true;
    const confirm = $("#btnDisplayConfirm");
    if (confirm) confirm.disabled = false;
    const wrap = $("#ytdlpScanProgressWrap");
    const fill = $("#ytdlpScanProgressFill");
    const text = $("#ytdlpScanProgressText");
    const hint = $("#ytdlpScanHint");
    if (wrap) wrap.hidden = true;
    if (fill) fill.style.width = "0%";
    if (text) text.textContent = "";
    if (hint && !hint.dataset.busy) {
      hint.textContent = "방송 시작 시 방송 화면에서 임베드 검사 후, 필요한 곡만 고화질로 받습니다.";
    }
    hint?.removeAttribute("data-busy");
  }

  function updateYtdlpScanProgress(data) {
    const wrap = $("#ytdlpScanProgressWrap");
    const fill = $("#ytdlpScanProgressFill");
    const text = $("#ytdlpScanProgressText");
    const hint = $("#ytdlpScanHint");
    const modal = $("#ytdlpPrepareModal");
    const modalFill = $("#ytdlpPrepareModalFill");
    const modalStatus = $("#ytdlpPrepareModalStatus");
    const modalTitle = $("#ytdlpPrepareModalTitle");
    const done = Number((data && data.done) || 0);
    const total = Number((data && data.total) || 0);
    const percent =
      typeof data?.percent === "number"
        ? Math.max(0, Math.min(100, data.percent))
        : total > 0
          ? Math.max(0, Math.min(100, Math.round((done / total) * 100)))
          : 0;
    const running = !!(data && data.running);
    ytdlpBatchRunning = running;
    if (modal) {
      if (running || (data && data.phase === "방송 시작 전")) {
        if (!ytdlpPrepareModalDismissed) {
          modal.hidden = false;
        }
        if (modalTitle) {
          modalTitle.textContent =
            (data && data.phase) === "방송준비중"
              ? "방송준비중입니다"
              : (data && data.phase) === "방송 시작 전"
                ? "방송 시작 준비 중"
                : "방송 준비 중";
        }
        if (modalFill) modalFill.style.width = `${percent}%`;
        if (modalStatus) {
          modalStatus.textContent =
            (data && data.status) || `준비 중… ${done}/${total}`;
        }
      } else if (!running) {
        ytdlpPrepareModalDismissed = false;
        if (modalFill) modalFill.style.width = `${percent}%`;
        if (modalStatus) modalStatus.textContent = (data && data.status) || "준비 완료";
        if (modal) modal.hidden = true;
      }
    }
    if (wrap && fill && text) {
      wrap.hidden = !running && percent <= 0;
      fill.style.width = `${percent}%`;
      text.textContent = (data && data.status) || "";
    }
    if (running && hint && text) {
      hint.dataset.busy = "1";
      hint.textContent = text.textContent || `검사 중... ${done}/${total}`;
    } else if (hint && text) {
      hint.dataset.busy = "";
      if (text.textContent) hint.textContent = text.textContent;
      setTimeout(() => {
        if (wrap) wrap.hidden = true;
      }, 1500);
    }
    const startBtn = $("#btnDisplayConfirm");
    if (startBtn) startBtn.disabled = running;
  }

  async function fetchCsrf() {
    const res = await fetch("/api/csrf-token", { credentials: "same-origin" });
    const data = await res.json();
    csrfToken = data.csrf_token;
  }

  function initSocket() {
    socket = io({ withCredentials: true });

    socket.on("playlist_update", (data) => {
      playlist = data.playlist || [];
      renderPlaylist();
    });

    socket.on("state_sync", (data) => {
      if (!data) return;
      playlist = data.playlist || playlist;
      if (typeof data.current_index === "number") {
        currentIndex = data.current_index;
      }
      if (data.playback_status) {
        playbackStatus = data.playback_status;
      }
      if (playbackStatus === "stopped" || playbackStatus === "ended") {
        resetBroadcastPrepUi();
      }
      renderPlaylist();
      updatePauseButton();
    });
    socket.on("ytdlp_scan_progress", (data) => {
      if (!data) return;
      if (data.running || data.phase) {
        updateYtdlpScanProgress(data);
      }
    });
    socket.on("ytdlp_download_error", (data) => {
      const msg = (data && data.message) || "다운로드 실패";
      const title = (data && data.title) || "YouTube";
      showAppAlert(`${title}\n\n${msg}`, { title: "yt-dlp 오류" });
    });

    socket.on("now_playing", (data) => {
      currentIndex = data.index;
      const title = data.title || "재생 중인 곡 없음";
      $("#nowTitle").textContent = title;
      playbackCurrentSec = 0;
      if (!isScrubbingProgress) updateProgressBar(0, playbackDurationSec);
      renderPlaylist();
    });

    socket.on("playback_status", (data) => {
      playbackStatus = data.status;
      updatePauseButton();
      if (data.status === "stopped" || data.status === "ended") {
        resetBroadcastPrepUi();
      }
    });

    socket.on("broadcast_prep_reset", () => {
      resetBroadcastPrepUi();
    });

    socket.on("search_progress", (data) => {
      const wrap = $("#searchProgressWrap");
      wrap.hidden = false;
      const bar = $("#searchProgressBar");
      bar.style.setProperty("--pct", `${data.progress}%`);
      bar.style.width = `${data.progress}%`;
      bar.style.height = "6px";
      bar.style.background = "var(--primary)";
      bar.style.borderRadius = "99px";
      $("#searchProgressStatus").textContent = data.status || "";
      if (data.progress >= 100) {
        setTimeout(() => {
          wrap.hidden = true;
        }, 600);
      }
    });

    socket.on("playback_progress", (data) => {
      if (!data || !data.duration) return;
      if (typeof data.index === "number" && data.index >= 0) {
        currentIndex = data.index;
      }
      playbackCurrentSec = Number(data.current) || 0;
      playbackDurationSec = Number(data.duration) || 0;
      if (!isScrubbingProgress) {
        updateProgressBar(playbackCurrentSec, playbackDurationSec);
      }
    });

    socket.on("connect", () => {
      socketConnected = true;
      broadcastAllowed = true;
      setControlsEnabled(true);
      socket.emit("get_state", {});
      updateServerStatus();
    });

    socket.on("disconnect", () => {
      socketConnected = false;
      broadcastAllowed = false;
      setControlsEnabled(false);
      updateServerStatus();
    });

    socket.on("connect_error", () => {
      socketConnected = false;
      broadcastAllowed = false;
      setControlsEnabled(false);
      updateServerStatus();
    });

    socket.on("control_denied", (data) => {
      showAppAlert((data && data.message) || "방송을 제어할 수 없습니다. 로그인 상태를 확인해 주세요.");
      setControlsEnabled(false);
    });

    socket.on("session_status", (data) => {
      if (data && typeof data.broadcast_allowed === "boolean") {
        broadcastAllowed = data.broadcast_allowed;
      } else if (socketConnected) {
        broadcastAllowed = true;
      }
      setControlsEnabled(socketConnected);
      updateServerStatusFromData(data);
    });

    window.panelSocket = socket;
    if (window.PlaybackRecoveryUI) window.PlaybackRecoveryUI.bindSocket(socket);
  }

  function updateProgressBar(currentSec, durationSec) {
    const fill = $("#progressFill");
    const track = $("#progressTrack");
    if (!fill) return;
    const dur = Math.max(0, Number(durationSec) || 0);
    const cur = Math.max(0, Math.min(dur, Number(currentSec) || 0));
    const pct = dur > 0 ? Math.min(100, Math.max(0, (cur / dur) * 100)) : 0;
    fill.style.width = `${pct}%`;
    if (track) track.setAttribute("aria-valuenow", String(Math.round(pct)));
  }

  function seekSecondsFromPointer(track, clientX) {
    const rect = track.getBoundingClientRect();
    if (!rect.width) return 0;
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    return ratio * playbackDurationSec;
  }

  function initProgressScrub() {
    const track = $("#progressTrack");
    if (!track || track.dataset.scrubReady) return;
    track.dataset.scrubReady = "1";

    const endScrub = (e) => {
      if (!isScrubbingProgress) return;
      isScrubbingProgress = false;
      track.classList.remove("scrubbing");
      try {
        track.releasePointerCapture(e.pointerId);
      } catch (_) {
        /* ignore */
      }
      const sec = seekSecondsFromPointer(track, e.clientX);
      updateProgressBar(sec, playbackDurationSec);
      if (socket && socket.connected && playbackDurationSec > 0) {
        socket.emit("control", { action: "seek", seconds: sec });
      }
    };

    track.addEventListener("pointerdown", (e) => {
      if (!playbackDurationSec || playbackDurationSec <= 0) return;
      if (e.button !== 0 && e.pointerType === "mouse") return;
      isScrubbingProgress = true;
      track.classList.add("scrubbing");
      try {
        track.setPointerCapture(e.pointerId);
      } catch (_) {
        /* ignore */
      }
      const sec = seekSecondsFromPointer(track, e.clientX);
      updateProgressBar(sec, playbackDurationSec);
      e.preventDefault();
    });

    track.addEventListener("pointermove", (e) => {
      if (!isScrubbingProgress) return;
      const sec = seekSecondsFromPointer(track, e.clientX);
      updateProgressBar(sec, playbackDurationSec);
    });

    track.addEventListener("pointerup", endScrub);
    track.addEventListener("pointercancel", endScrub);
  }

  function setControlsEnabled(enabled) {
    const allowTransport = enabled && socketConnected;
    const startBtn = document.getElementById("btnBroadcastStart");
    if (startBtn) startBtn.disabled = false;
    ["btnPrev", "btnPause", "btnNext", "btnStop"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.disabled = !allowTransport;
    });
  }

  function renderPlaylist() {
    const list = $("#playlistList");
    const empty = $("#playlistEmpty");
    const scanHint = $("#ytdlpScanHint");
    list.innerHTML = "";
    empty.hidden = playlist.length > 0;
    const youtubeCount = playlist.filter(
      (item) => item && item.type === "youtube"
    ).length;
    if (scanHint && !scanHint.dataset.busy) {
      if (!playlist.length) scanHint.textContent = "";
      else if (youtubeCount > 0) {
        const ytdlpN = playlist.filter(
          (i) => i && i.type === "youtube" && i.ytdlp_checked && i.ytdlp_required
        ).length;
        if (ytdlpN > 0) {
          scanHint.textContent = `YouTube ${youtubeCount}곡 · yt-dlp ${ytdlpN}곡 / 퍼가기 ${youtubeCount - ytdlpN}곡`;
        } else {
          scanHint.textContent = `YouTube ${youtubeCount}곡 · 재생 중 yt-dlp 자동 감지`;
        }
      } else scanHint.textContent = "";
    }

    playlist.forEach((item, idx) => {
      const li = document.createElement("li");
      li.className = "playlist-item" + (idx === currentIndex ? " playing" : "");
      li.dataset.index = String(idx);

      const displayTitle =
        item.type === "local"
          ? item.title || item.id || "로컬 파일"
          : item.title || "제목 없음";

      let thumb;
      if (item.type === "youtube" && item.thumbnail) {
        thumb = `<img src="${escapeHtml(item.thumbnail)}" alt="" />`;
      } else if (item.type === "youtube" && item.id) {
        thumb = `<img src="https://i.ytimg.com/vi/${escapeHtml(item.id)}/hqdefault.jpg" alt="" />`;
      } else {
        thumb = `<span class="thumb-icon">📁</span>`;
      }

      li.innerHTML = `
        <span class="drag-handle" title="드래그하여 순서 변경" aria-hidden="true">⋮⋮</span>
        ${thumb}
        <div class="title-wrap">
          <span class="title" title="${escapeHtml(displayTitle)}">${escapeHtml(displayTitle)}</span>
          <div class="meta-row">
            ${item.ytdlp_checked && item.ytdlp_required ? '<span class="playlist-badge ytdlp">YT-DLP</span>' : ""}
          </div>
        </div>
        <button type="button" class="btn-delete" data-index="${idx}" aria-label="삭제">✕</button>
      `;
      list.appendChild(li);
    });

    list.querySelectorAll(".btn-delete").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const index = parseInt(btn.dataset.index, 10);
        socket.emit("remove_song", { index });
      });
    });

    if (sortable) sortable.destroy();
    if (!list || playlist.length === 0) return;
    sortable = new Sortable(list, {
      animation: 200,
      handle: ".drag-handle",
      filter: ".btn-delete",
      preventOnFilter: true,
      forceFallback: true,
      fallbackOnBody: true,
      fallbackTolerance: 4,
      ghostClass: "sortable-ghost",
      delay: 80,
      delayOnTouchOnly: false,
      onEnd(evt) {
        if (evt.oldIndex === evt.newIndex) return;
        if (!socketConnected) return;
        socket.emit("reorder", { from_idx: evt.oldIndex, to_idx: evt.newIndex });
      },
    });
  }

  function endImagePublicUrl(path) {
    if (!path) return "";
    if (path.startsWith("http://") || path.startsWith("https://")) return path;
    if (path.startsWith("/")) return path;
    return "/" + path.replace(/^\/+/, "");
  }

  function updateImagePreview(path, wrapId, imgId) {
    const wrap = $(wrapId);
    const img = $(imgId);
    if (!wrap || !img) return;
    const url = endImagePublicUrl(path);
    if (!url) {
      wrap.hidden = true;
      img.removeAttribute("src");
      return;
    }
    img.onerror = () => {
      wrap.hidden = true;
    };
    img.onload = () => {
      wrap.hidden = false;
    };
    img.src = url + (url.includes("?") ? "&" : "?") + "t=" + Date.now();
  }

  function updateEndImagePreview(path) {
    updateImagePreview(path, "#endImagePreviewWrap", "#endImagePreview");
  }

  const DEFAULT_NEXT_ALERT_LOGO_URL = "/assets/bundled/njbs-logo.png";

  function getSelectedRadio(name, fallback = "light") {
    const el = document.querySelector(`input[name="${name}"]:checked`);
    return el ? el.value : fallback;
  }

  function setSelectedRadio(name, value) {
    const el = document.querySelector(`input[name="${name}"][value="${value}"]`);
    if (el) el.checked = true;
  }

  function syncAlertLogoPreviewWrapTheme() {
    const wrap = $("#nextAlertLogoPreviewWrap");
    if (!wrap) return;
    const theme = getSelectedRadio("nextAlertTheme", "light");
    wrap.classList.remove("preview-on-dark", "preview-on-light");
    wrap.classList.add(theme === "light" ? "preview-on-light" : "preview-on-dark");
  }

  function updateNextAlertLogoPreview(path) {
    const url = path || DEFAULT_NEXT_ALERT_LOGO_URL;
    updateImagePreview(url, "#nextAlertLogoPreviewWrap", "#nextAlertLogoPreview");
    syncAlertLogoPreviewWrapTheme();
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function emitAddSong(payload) {
    return new Promise((resolve) => {
      if (!socket || !socket.connected) {
        showAppAlert("서버에 연결되어 있지 않습니다.", { title: "추가 실패" });
        resolve(null);
        return;
      }
      socket.emit("add_song", payload, (res) => {
        if (res && res.ok) {
          const name = payload.title || "곡";
          showAppAlert(`「${name}」이(가) 플레이리스트에 추가되었습니다.`, { title: "추가 완료" });
        } else {
          showAppAlert("플레이리스트에 추가하지 못했습니다.", { title: "추가 실패" });
        }
        resolve(res);
      });
    });
  }

  function updatePauseButton() {
    const btn = $("#btnPause");
    btn.textContent = playbackStatus === "playing" ? "⏸" : "▶";
  }

  const ONBOARDING_SLIDE_COUNT = 4;
  let onboardingSlide = 0;

  function initTabs() {
    document.querySelectorAll(".sidebar-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".sidebar-tab").forEach((t) => t.classList.remove("active"));
        document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
        tab.classList.add("active");
        $("#panel-" + tab.dataset.tab).classList.add("active");
      });
    });
  }

  async function syncNetwork() {
    const btn = $("#btnSyncNetwork");
    if (btn) btn.disabled = true;
    try {
      const res = await fetch("/api/network", { credentials: "same-origin" });
      if (res.ok) {
        const data = await res.json();
        showLanUrls(data);
      }
      await loadPublicConfig();
      await updateServerStatus();
      const hint = document.querySelector(".server-sync-hint");
      if (hint) {
        const orig = hint.textContent;
        hint.textContent = "동기화 완료";
        setTimeout(() => {
          hint.textContent = orig;
        }, 2000);
      }
    } catch (err) {
      showAppAlert("동기화 실패: " + err.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function openBroadcastWarnModal() {
    const modal = $("#broadcastWarnModal");
    const ack = $("#broadcastWarnAck");
    const confirmBtn = $("#btnBroadcastWarnConfirm");
    if (ack) ack.checked = false;
    if (confirmBtn) confirmBtn.disabled = true;
    if (modal) modal.hidden = false;
    preloadDisplays();
    requestAnimationFrame(() => {
      if (ack) ack.focus();
    });
  }

  function preloadDisplays() {
    if (displaysCache || displaysLoading) return;
    displaysLoading = fetch("/api/displays", { credentials: "same-origin" })
      .then((res) => res.json())
      .then((data) => {
        displaysCache = data.displays || [{ index: 0, name: "기본 모니터" }];
        return displaysCache;
      })
      .catch(() => {
        displaysCache = [{ index: 0, name: "기본 모니터" }];
        return displaysCache;
      })
      .finally(() => {
        displaysLoading = null;
      });
  }

  function updateOnboardingUI() {
    const track = $("#onboardingTrack");
    const viewport = $("#onboardingViewport");
    if (track && viewport) {
      const w = viewport.getBoundingClientRect().width;
      track.style.transform = w > 0 ? `translateX(-${onboardingSlide * w}px)` : "";
    }
    document.querySelectorAll(".onboarding-dot").forEach((d, i) => {
      d.classList.toggle("active", i === onboardingSlide);
    });
    const prev = $("#btnOnboardingPrev");
    if (prev) prev.hidden = onboardingSlide === 0;
    const nextBtn = $("#btnOnboardingNext");
    if (nextBtn) {
      nextBtn.textContent =
        onboardingSlide >= ONBOARDING_SLIDE_COUNT - 1 ? "동의하고 시작" : "다음";
    }
    const err = $("#onboardingErr");
    if (err) err.hidden = true;
  }

  function initOnboardingControls() {
    const dots = $("#onboardingDots");
    if (!dots || dots.dataset.ready) return;
    dots.dataset.ready = "1";
    dots.innerHTML = "";
    for (let i = 0; i < ONBOARDING_SLIDE_COUNT; i++) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "onboarding-dot" + (i === 0 ? " active" : "");
      b.setAttribute("aria-label", i + 1 + "번째 안내");
      b.addEventListener("click", () => {
        onboardingSlide = i;
        updateOnboardingUI();
      });
      dots.appendChild(b);
    }
    $("#btnOnboardingPrev").addEventListener("click", () => {
      if (onboardingSlide > 0) {
        onboardingSlide--;
        updateOnboardingUI();
      }
    });
    $("#btnOnboardingNext").addEventListener("click", async () => {
      if (onboardingSlide < ONBOARDING_SLIDE_COUNT - 1) {
        onboardingSlide++;
        updateOnboardingUI();
        return;
      }
      if (!$("#onboardingTerms").checked) {
        $("#onboardingErr").hidden = false;
        return;
      }
      const res = await fetch("/api/settings/onboarding", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ agree_terms: true }),
      });
      const data = await res.json();
      if (!res.ok) {
        showAppAlert(data.error || "저장 실패");
        return;
      }
      $("#onboardingOverlay").hidden = true;
    });
  }

  async function maybeShowOnboarding() {
    try {
      const res = await fetch("/api/settings/onboarding", { credentials: "same-origin" });
      if (!res.ok) return;
      const data = await res.json();
      if (data.complete) return;
      onboardingSlide = 0;
      initOnboardingControls();
      $("#onboardingOverlay").hidden = false;
      requestAnimationFrame(() => updateOnboardingUI());
    } catch (e) {
      /* ignore */
    }
  }

  async function addYoutubeByUrl() {
    const raw = $("#youtubeUrlInput").value.trim();
    const hint = $("#youtubeUrlHint");
    if (!raw) {
      if (hint) hint.textContent = "YouTube 링크를 입력해 주세요.";
      return;
    }
    const btn = $("#btnAddYoutubeUrl");
    if (btn) btn.disabled = true;
    if (hint) hint.textContent = "영상 정보를 가져오는 중…";
    try {
      const res = await fetch("/api/youtube/from-url", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ url: raw }),
      });
      const data = await res.json();
      if (!res.ok) {
        if (hint) hint.textContent = data.error || "추가 실패";
        showAppAlert(data.error || "추가 실패");
        return;
      }
      await emitAddSong({
        type: "youtube",
        id: data.id,
        title: data.title,
        thumbnail: data.thumbnail,
        duration: data.duration || 0,
      });
      $("#youtubeUrlInput").value = "";
      if (hint) hint.textContent = "";
    } catch (err) {
      if (hint) hint.textContent = "오류: " + err.message;
      showAppAlert("링크 추가 중 오류: " + err.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function doSearch() {
    const query = $("#searchInput").value.trim();
    if (!query) return;
    const resultsEl = $("#searchResults");
    resultsEl.innerHTML = "";
    $("#searchProgressWrap").hidden = false;

    try {
      const res = await fetch("/api/search", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ query }),
      });
      const data = await res.json();
      if (!res.ok) {
        showAppAlert(data.error || "검색 실패");
        return;
      }
      (data.results || []).forEach((item) => {
        const li = document.createElement("li");
        li.innerHTML = `
          <img src="${escapeHtml(item.thumbnail)}" alt="" />
          <span class="title">${escapeHtml(item.title)}</span>
          <button type="button" class="btn-primary btn-sm">추가</button>
        `;
        li.querySelector("button").addEventListener("click", () => {
          emitAddSong({
            type: "youtube",
            id: item.id,
            title: item.title,
            thumbnail: item.thumbnail,
            duration: item.duration || 0,
          });
        });
        resultsEl.appendChild(li);
      });
    } catch (err) {
      showAppAlert("검색 중 오류: " + err.message);
    }
  }

  async function uploadLocal(file) {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch("/api/upload/local", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken },
      body: fd,
    });
    const data = await res.json();
    if (!res.ok) {
      showAppAlert(data.error || "업로드 실패");
      return;
    }
    await emitAddSong(data);
  }

  function renderDisplayList(displays) {
    const list = $("#displayList");
    if (!list) return;
    list.innerHTML = "";
    (displays || []).forEach((d) => {
      const li = document.createElement("li");
      li.textContent = d.name;
      li.dataset.index = String(d.index);
      if (d.index === selectedDisplay) li.classList.add("selected");
      li.addEventListener("click", () => {
        list.querySelectorAll("li").forEach((x) => x.classList.remove("selected"));
        li.classList.add("selected");
        selectedDisplay = d.index;
      });
      list.appendChild(li);
    });
  }

  async function loadDisplays() {
    if (displaysCache) {
      renderDisplayList(displaysCache);
      return;
    }
    if (displaysLoading) {
      await displaysLoading;
      renderDisplayList(displaysCache);
      return;
    }
    preloadDisplays();
    await displaysLoading;
    renderDisplayList(displaysCache);
  }

  function openDisplayModal() {
    $("#displayModal").hidden = false;
    void loadDisplays();
  }

  function initControls() {
    $("#btnSearch").addEventListener("click", doSearch);
    $("#searchInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") doSearch();
    });

    $("#btnAddYoutubeUrl").addEventListener("click", addYoutubeByUrl);
    $("#youtubeUrlInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") addYoutubeByUrl();
    });

    $("#btnPickLocal").addEventListener("click", () => $("#localFileInput").click());
    $("#localFileInput").addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (file) uploadLocal(file);
      e.target.value = "";
    });
    $("#btnBroadcastStart").addEventListener("click", () => {
      if (!playlist.length) {
        showAppAlert("플레이리스트에 곡을 추가해 주세요.");
        return;
      }
      openBroadcastWarnModal();
    });

    $("#broadcastWarnAck").addEventListener("change", () => {
      $("#btnBroadcastWarnConfirm").disabled = !$("#broadcastWarnAck").checked;
    });
    $("#btnBroadcastWarnCancel").addEventListener("click", () => {
      $("#broadcastWarnModal").hidden = true;
    });
    $("#btnBroadcastWarnConfirm").addEventListener("click", () => {
      if (!$("#broadcastWarnAck").checked) return;
      $("#broadcastWarnModal").hidden = true;
      openDisplayModal();
    });

    $("#btnDisplayCancel").addEventListener("click", () => {
      $("#displayModal").hidden = true;
    });
    $("#btnYtdlpPrepareClose")?.addEventListener("click", () => {
      ytdlpPrepareModalDismissed = true;
      const modal = $("#ytdlpPrepareModal");
      if (modal) modal.hidden = true;
    });
    $("#btnDisplayConfirm").addEventListener("click", () => {
      $("#displayModal").hidden = true;
      if (!canUseBroadcastControls()) {
        showAppAlert("앱에 로그인되어 있어야 방송을 시작할 수 있습니다.");
        return;
      }
      if (!playlist.length) {
        showAppAlert("플레이리스트에 곡을 추가해 주세요.");
        return;
      }
      if (!socket || !socket.connected) {
        showAppAlert("서버 연결 중입니다. 잠시 후 다시 시도해 주세요.");
        return;
      }
      ytdlpPrepareModalDismissed = false;
      const youtubeN = playlist.filter((i) => i && i.type === "youtube").length;
      updateYtdlpScanProgress({
        done: 0,
        total: Math.max(youtubeN, 1),
        percent: 0,
        phase: "방송 시작 전",
        status: "방송 화면에서 임베드 검사 및 다운로드 준비…",
        running: true,
      });
      socket.emit("control", { action: "start", display_index: selectedDisplay });
    });

    function copyUrlToClipboard(full, label) {
      if (!full) return;
      const done = () =>
        showAppAlert(`${label} 접속 주소를 복사했습니다.`, { title: "복사" });
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(full).then(done).catch(() => {
          showAppAlert(full, { title: label });
        });
      } else {
        showAppAlert(full, { title: label });
      }
    }

    $("#btnCopyLan")?.addEventListener("click", () => {
      copyUrlToClipboard(lanPrimaryUrlFull, "컨트롤 패널");
    });

    $("#btnCopyWebsiteLan")?.addEventListener("click", () => {
      copyUrlToClipboard(websitePrimaryUrlFull, "관리자 웹");
    });

    $("#btnPause").addEventListener("click", () => {
      const action = playbackStatus === "playing" ? "pause" : "play";
      socket.emit("control", { action });
    });
    $("#btnPrev").addEventListener("click", () => socket.emit("control", { action: "prev" }));
    $("#btnNext").addEventListener("click", () => socket.emit("control", { action: "next" }));
    $("#btnStop").addEventListener("click", async () => {
      const ok = await showAppConfirm("방송을 종료할까요?", { title: "방송 종료" });
      if (ok) {
        resetBroadcastPrepUi();
        socket.emit("control", { action: "stop" });
      }
    });

    $("#btnLogout").addEventListener("click", async () => {
      if (socket && socket.connected) {
        socket.disconnect();
      }
      socketConnected = false;
      broadcastAllowed = false;
      setControlsEnabled(false);
      await fetch("/api/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrfToken },
      });
      window.location.href = "/login";
    });

    $("#btnEndImage").addEventListener("click", () => $("#endImageInput").click());
    $("#endImageInput").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const localPreview = URL.createObjectURL(file);
      const previewImg = $("#endImagePreview");
      const previewWrap = $("#endImagePreviewWrap");
      if (previewImg && previewWrap) {
        previewImg.src = localPreview;
        previewWrap.hidden = false;
      }
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch("/api/settings/end-image", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrfToken },
        body: fd,
      });
      const data = await res.json();
      if (res.ok) {
        const path = data.path || "";
        $("#endImageHint").textContent = path ? "커스텀 종료 이미지 적용 중" : "업로드 완료";
        if (localPreview) URL.revokeObjectURL(localPreview);
        updateEndImagePreview(path);
      } else {
        if (localPreview) URL.revokeObjectURL(localPreview);
        updateEndImagePreview("");
        showAppAlert(data.error);
      }
      e.target.value = "";
    });

    $("#btnEndImageClear").addEventListener("click", async () => {
      await fetch("/api/settings/end-image", {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrfToken },
      });
      $("#endImageHint").textContent = "기본 종료 화면 사용 중";
      updateEndImagePreview("");
      const fileInput = $("#endImageInput");
      if (fileInput) fileInput.value = "";
    });

    $("#btnNextAlertLogo").addEventListener("click", () => $("#nextAlertLogoInput").click());
    $("#nextAlertLogoInput").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const localPreview = URL.createObjectURL(file);
      const previewImg = $("#nextAlertLogoPreview");
      const previewWrap = $("#nextAlertLogoPreviewWrap");
      if (previewImg && previewWrap) {
        previewImg.src = localPreview;
        previewWrap.hidden = false;
      }
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch("/api/settings/next-alert-logo", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrfToken },
        body: fd,
      });
      const data = await res.json();
      if (res.ok) {
        const path = data.path || "";
        $("#nextAlertLogoHint").textContent = path ? "로고 적용 중" : "업로드 완료";
        if (localPreview) URL.revokeObjectURL(localPreview);
        updateNextAlertLogoPreview(path);
      } else {
        if (localPreview) URL.revokeObjectURL(localPreview);
        updateNextAlertLogoPreview("");
        showAppAlert(data.error);
      }
      e.target.value = "";
    });

    $("#btnNextAlertLogoClear").addEventListener("click", async () => {
      await fetch("/api/settings/next-alert-logo", {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrfToken },
      });
      $("#nextAlertLogoHint").textContent = "기본 로고 사용 중";
      updateNextAlertLogoPreview(DEFAULT_NEXT_ALERT_LOGO_URL);
      const fileInput = $("#nextAlertLogoInput");
      if (fileInput) fileInput.value = "";
    });

    document.querySelectorAll('input[name="nextAlertTheme"]').forEach((el) => {
      el.addEventListener("change", syncAlertLogoPreviewWrapTheme);
    });

    $("#btnAlertThemesSave").addEventListener("click", async () => {
      const res = await fetch("/api/settings/alert-themes", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({
          next_alert_theme: getSelectedRadio("nextAlertTheme", "light"),
          now_playing_theme: getSelectedRadio("nowPlayingTheme", "light"),
        }),
      });
      const data = await res.json();
      if (res.ok) {
        setSelectedRadio("nextAlertTheme", data.next_alert_theme || "light");
        setSelectedRadio("nowPlayingTheme", data.now_playing_theme || "light");
        syncAlertLogoPreviewWrapTheme();
        showAppAlert("안내 테마가 저장되었습니다.", { title: "완료" });
      } else {
        showAppAlert(data.error || "저장 실패", { title: "오류" });
      }
    });

    $("#btnNextAlertTextSave").addEventListener("click", async () => {
      const text = ($("#nextAlertTextInput").value || "").trim();
      const res = await fetch("/api/settings/next-alert-text", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ text }),
      });
      const data = await res.json();
      if (res.ok) {
        $("#nextAlertTextInput").value = data.text || "";
        showAppAlert("안내 문구가 저장되었습니다.", { title: "완료" });
      } else {
        showAppAlert(data.error || "저장 실패", { title: "오류" });
      }
    });

    $("#btnChangePassword").addEventListener("click", async () => {
      const res = await fetch("/api/password", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({
          current_password: $("#currentPassword").value,
          new_password: $("#newPassword").value,
        }),
      });
      const data = await res.json();
      showAppAlert(res.ok ? "비밀번호가 변경되었습니다." : data.error || "실패", {
        title: res.ok ? "완료" : "오류",
      });
    });

    $("#btnResetAccount").addEventListener("click", async () => {
      const ok = await showAppConfirm(
        "로컬 계정을 admin / 1234 로 초기화할까요?",
        { title: "DB 계정 초기화", okText: "초기화", cancelText: "취소" }
      );
      if (!ok) return;
      const res = await fetch("/api/settings/reset-account", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (res.ok) {
        window.location.href = "/login";
        return;
      }
      showAppAlert(data.error || "초기화 실패");
    });

    document.querySelectorAll('input[name="broadcastBrowser"]').forEach((radio) => {
      radio.addEventListener("change", async () => {
        if (!radio.checked) return;
        const res = await fetch("/api/settings/broadcast-browser", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken,
          },
          body: JSON.stringify({ broadcast_browser: radio.value }),
        });
        const data = await res.json();
        if (!res.ok) showAppAlert(data.error || "저장 실패");
        else updateBrowserHint(data.available);
      });
    });

    $("#autostartToggle").addEventListener("change", async (e) => {
      await fetch("/api/settings/autostart", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ enabled: e.target.checked }),
      });
    });

    async function loadYoutubeCookiesStatus() {
      const el = $("#youtubeCookiesStatus");
      const help = $("#youtubeCookiesHelp");
      if (!el) return;
      try {
        const res = await fetch("/api/youtube/cookies/status", {
          credentials: "same-origin",
        });
        const data = await res.json();
        if (help && (data.guide || data.help)) {
          help.textContent = data.guide || data.help;
        }
        if (!res.ok) {
          el.textContent = "상태 확인 실패";
          return;
        }
        if (data.ok) {
          el.textContent =
            "저장됨 · " +
            (data.path || "") +
            (data.size ? " (" + data.size + " bytes)" : "");
          el.className = "hint status-ok";
        } else {
          const blocking = (data.blocking_browsers || []).join(", ");
          el.textContent =
            "쿠키 없음" +
            (blocking ? " — 실행 중: " + blocking : "") +
            " · 위 「확장 프로그램 설치」 안내를 따라 주세요.";
          el.className = "hint status-warn";
        }
      } catch (err) {
        el.textContent = "상태 확인 오류: " + (err.message || err);
      }
    }

    const btnCookies = $("#btnYoutubeCookiesRefresh");
    const cookiesFileInput = $("#youtubeCookiesFileInput");
    async function importYoutubeCookiesFile(file) {
      if (!file) return;
      btnCookies.disabled = true;
      btnCookies.textContent = "가져오는 중…";
      try {
        const fd = new FormData();
        fd.append("file", file);
        const res = await fetch("/api/youtube/cookies/import", {
          method: "POST",
          credentials: "same-origin",
          headers: { "X-CSRFToken": csrfToken },
          body: fd,
        });
        const data = await res.json();
        await loadYoutubeCookiesStatus();
        if (!res.ok) {
          throw new Error(data.error || "가져오기 실패");
        }
        showAppAlert(
          data.ok
            ? `「${file.name}」을(를) 저장했습니다.`
            : "YouTube 쿠키가 포함된 txt 파일인지 확인해 주세요.",
          { title: data.ok ? "완료" : "쿠키 파일 필요" }
        );
      } catch (err) {
        showAppAlert(String(err.message || err), { title: "오류" });
      } finally {
        btnCookies.disabled = false;
        btnCookies.textContent = "YouTube 쿠키 파일 가져오기";
        if (cookiesFileInput) cookiesFileInput.value = "";
      }
    }
    if (btnCookies && cookiesFileInput) {
      btnCookies.addEventListener("click", () => {
        cookiesFileInput.click();
      });
      cookiesFileInput.addEventListener("change", () => {
        const file = cookiesFileInput.files && cookiesFileInput.files[0];
        if (file) importYoutubeCookiesFile(file);
      });
    }
    loadYoutubeCookiesStatus();
  }

  async function loadYoutubePlaybackSettings() {
    try {
      const res = await fetch("/api/settings/youtube-playback", {
        credentials: "same-origin",
      });
      if (!res.ok) return;
      const data = await res.json();
      const embedToggle = document.getElementById("youtubeEmbedOnlyToggle");
      if (embedToggle) embedToggle.checked = data.youtube_embed_only !== false;
      const quality = document.getElementById("youtubeIframeQuality");
      if (quality && data.youtube_iframe_quality) {
        quality.value = data.youtube_iframe_quality;
      }
      const scanHint = $("#ytdlpScanHint");
      if (scanHint && data.youtube_embed_only !== false) {
        scanHint.textContent =
          "YouTube 퍼가기(최고 화질) — 방송 시 yt-dlp 다운로드 없이 재생합니다.";
      }
    } catch (_) {}
  }

  function bindYoutubePlaybackSettings() {
    const btn = document.getElementById("btnSaveYoutubePlayback");
    const hint = document.getElementById("youtubePlaybackSaveHint");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const embedToggle = document.getElementById("youtubeEmbedOnlyToggle");
      const quality = document.getElementById("youtubeIframeQuality");
      try {
        const res = await fetch("/api/settings/youtube-playback", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken,
          },
          body: JSON.stringify({
            youtube_embed_only: !!embedToggle?.checked,
            youtube_iframe_quality: quality?.value || "highres",
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "저장 실패");
        const scanHint = $("#ytdlpScanHint");
        if (scanHint) {
          scanHint.textContent = data.youtube_embed_only
            ? "YouTube 퍼가기(최고 화질) — 방송 시 yt-dlp 다운로드 없이 재생합니다."
            : "방송 시작 시 방송 화면에서 임베드 검사 후, 필요한 곡만 고화질로 받습니다.";
        }
        if (hint) {
          hint.textContent = "저장되었습니다. 다음 방송부터 적용됩니다.";
          setTimeout(() => {
            hint.textContent = "";
          }, 2500);
        }
      } catch (err) {
        showAppAlert(String(err.message || err));
      }
    });
  }

  function updateServerStatusFromData(data) {
    const el = $("#serverStatus");
    if (!el || !data) return;
    if (data.viewer_is_local || isLocalAppView()) {
      el.textContent = "서버 켜짐";
      el.className = "server-status";
    } else if (data.panel_online && data.broadcast_allowed) {
      el.textContent = "앱과 연결됨";
      el.className = "server-status status-remote-ok";
    } else if (data.panel_online) {
      el.textContent = "앱 로그인 대기";
      el.className = "server-status status-wait";
    } else {
      el.textContent = "앱 로그인 대기";
      el.className = "server-status status-wait";
    }
  }

  async function updateServerStatus() {
    const el = $("#serverStatus");
    if (!el) return;
    try {
      const res = await fetch("/api/session/status", { credentials: "same-origin" });
      if (!res.ok) return;
      const data = await res.json();
      if (typeof data.broadcast_allowed === "boolean") {
        broadcastAllowed = data.broadcast_allowed;
        setControlsEnabled(socketConnected && broadcastAllowed);
      }
      if (isLocalAppView()) {
        el.textContent = "서버 켜짐";
        el.className = "server-status";
      } else if (data.panel_online && data.authenticated && data.broadcast_allowed) {
        el.textContent = "앱과 연결됨";
        el.className = "server-status status-remote-ok";
      } else if (data.panel_online) {
        el.textContent = "앱 로그인 대기";
        el.className = "server-status status-wait";
      } else {
        el.textContent = "앱 로그인 대기";
        el.className = "server-status status-wait";
      }
    } catch (e) {
      el.textContent = isLocalAppView() ? "서버 켜짐" : "연결 확인 중…";
    }
  }

  function formatLanHostPort(url) {
    if (!url) return "";
    try {
      const u = new URL(url);
      const port = u.port || (u.protocol === "https:" ? "443" : "80");
      return `${u.hostname}:${port}`;
    } catch (_) {
      return String(url).replace(/^https?:\/\//, "").replace(/\/$/, "");
    }
  }

  function pickPrimaryUrl(data, lanKey, primaryKey, localKey, fallbackLanKey) {
    const lanList = Array.isArray(data[lanKey])
      ? data[lanKey]
      : fallbackLanKey && Array.isArray(data[fallbackLanKey])
        ? data[fallbackLanKey]
        : [];
    return (
      data[primaryKey] ||
      (lanList.length ? lanList[0] : "") ||
      data[localKey] ||
      ""
    );
  }

  function applyLanUrl(el, full) {
    if (!el) return;
    const label = formatLanHostPort(full);
    el.textContent = label || "LAN 주소 없음";
    el.title = full || "";
  }

  function showLanUrls(data) {
    if (!data) return;
    lanPrimaryUrlFull = pickPrimaryUrl(
      data,
      "panel_lan",
      "panel_primary_lan",
      "panel_local",
      "lan"
    );
    websitePrimaryUrlFull = pickPrimaryUrl(
      data,
      "website_lan",
      "website_primary_lan",
      "website_local"
    );
    applyLanUrl($("#lanPrimaryUrl"), lanPrimaryUrlFull);
    applyLanUrl($("#websitePrimaryUrl"), websitePrimaryUrlFull);
  }

  function updateBrowserHint(available) {
    const el = $("#browserAvailHint");
    if (!available) return;
    const parts = [];
    if (available.edge) parts.push("Edge 설치됨");
    else parts.push("Edge 없음");
    if (available.chrome) parts.push("Chrome 설치됨");
    else parts.push("Chrome 없음");
    el.textContent = parts.join(" · ");
    document
      .querySelector('input[name="broadcastBrowser"][value="edge"]')
      .closest("label")
      .style.opacity = available.edge ? "1" : "0.45";
    document
      .querySelector('input[name="broadcastBrowser"][value="chrome"]')
      .closest("label")
      .style.opacity = available.chrome ? "1" : "0.45";
  }

  async function loadBrowserSetting() {
    const res = await fetch("/api/settings/broadcast-browser", {
      credentials: "same-origin",
    });
    if (!res.ok) return;
    const data = await res.json();
    const val = data.broadcast_browser || "auto";
    const radio = document.querySelector(
      'input[name="broadcastBrowser"][value="' + val + '"]'
    );
    if (radio) radio.checked = true;
    updateBrowserHint(data.available);
  }

  async function loadPublicConfig() {
    const res = await fetch("/api/config/public", { credentials: "same-origin" });
    const data = await res.json();
    $("#autostartToggle").checked = !!data.autostart;
    const endPath = data.end_broadcast_image || "";
    $("#endImageHint").textContent = endPath
      ? "커스텀 종료 이미지 적용 중"
      : "기본 종료 화면 사용 중";
    updateEndImagePreview(endPath);
    const logoPath = data.next_alert_logo || DEFAULT_NEXT_ALERT_LOGO_URL;
    $("#nextAlertLogoHint").textContent = "로고 적용 중";
    updateNextAlertLogoPreview(logoPath);
    setSelectedRadio("nextAlertTheme", data.next_alert_theme || "light");
    setSelectedRadio("nowPlayingTheme", data.now_playing_theme || "light");
    syncAlertLogoPreviewWrapTheme();
    const textInput = $("#nextAlertTextInput");
    if (textInput) textInput.value = data.next_alert_text || "";
    showLanUrls(data);
    const br = data.broadcast_browser || "auto";
    const radio = document.querySelector(
      'input[name="broadcastBrowser"][value="' + br + '"]'
    );
    if (radio) radio.checked = true;
    loadBrowserSetting();
  }

  function updateMobileControlBarInset() {
    if (!window.matchMedia("(max-width: 768px)").matches) return;
    const bar = document.querySelector(".control-bar");
    if (!bar) return;
    const space = bar.getBoundingClientRect().height + 16;
    document.documentElement.style.setProperty("--mobile-control-bar-space", `${space}px`);
  }

  window.addEventListener("resize", () => {
    const overlay = $("#onboardingOverlay");
    if (overlay && !overlay.hidden) updateOnboardingUI();
    updateMobileControlBarInset();
  });

  async function loadPlaybackRecoverySettings() {
    try {
      const res = await fetch("/api/settings/playback-recovery", {
        credentials: "same-origin",
      });
      if (!res.ok) return;
      const data = await res.json();
      const stall = document.getElementById("playbackStallSeconds");
      if (stall) stall.value = data.playback_error_stall_seconds ?? 10;
      const mode = data.playback_error_recover_mode || "manual";
      document
        .querySelectorAll('input[name="playbackRecoverMode"]')
        .forEach((el) => {
          el.checked = el.value === mode;
        });
    } catch (_) {}
  }

  function bindPlaybackRecoverySettings() {
    const btn = document.getElementById("btnSavePlaybackRecovery");
    const hint = document.getElementById("playbackRecoverySaveHint");
    if (!btn) return;
    btn.addEventListener("click", async () => {
      const stall = document.getElementById("playbackStallSeconds");
      const modeEl = document.querySelector(
        'input[name="playbackRecoverMode"]:checked'
      );
      try {
        const res = await fetch("/api/settings/playback-recovery", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken,
          },
          body: JSON.stringify({
            playback_error_stall_seconds: Number(stall?.value) || 10,
            playback_error_recover_mode: modeEl?.value || "manual",
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "저장 실패");
        if (hint) {
          hint.textContent = "저장되었습니다.";
          setTimeout(() => {
            hint.textContent = "";
          }, 2500);
        }
      } catch (err) {
        showAppAlert(String(err.message || err));
      }
    });
  }

  async function maybeShowYoutubeCookiesWarning() {
    try {
      const pbRes = await fetch("/api/settings/youtube-playback", {
        credentials: "same-origin",
      });
      if (pbRes.ok) {
        const pb = await pbRes.json();
        if (pb.youtube_embed_only !== false) return;
      }
      const res = await fetch("/api/youtube/cookies/status", {
        credentials: "same-origin",
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data.ok) return;
      const settingsTab = document.querySelector('.sidebar-tab[data-tab="settings"]');
      if (settingsTab) settingsTab.click();
      const card = document.getElementById("youtubeCookiesCard");
      if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
      showAppAlert(
        "YouTube 고화질 다운로드용 쿠키 파일이 없습니다.\n\n" +
          "설정 탭 → 「YouTube 쿠키」에서\n" +
          "「확장 프로그램 설치 · Export 방법」을 따라\n" +
          "파일을 저장한 뒤\n" +
          "「YouTube 쿠키 파일 가져오기」로 txt 파일을 선택해 주세요.\n\n" +
          "(쿠키 파일은 다른 사람에게 보내지 마세요.)",
        { title: "YouTube 쿠키 필요", okText: "확인" }
      );
    } catch (_) {}
  }

  async function init() {
    initTabs();
    initSocket();
    initControls();
    loadYoutubePlaybackSettings();
    bindYoutubePlaybackSettings();
    initProgressScrub();
    setControlsEnabled(false);
    const startBtn = document.getElementById("btnBroadcastStart");
    if (startBtn) startBtn.disabled = false;
    preloadDisplays();
    const tasks = [fetchCsrf(), loadPublicConfig()];
    if (!isNativePanelSession()) tasks.push(updateServerStatus());
    await Promise.all(tasks);
    await maybeShowOnboarding();
    await maybeShowYoutubeCookiesWarning();
    if (!isNativePanelSession()) {
      setInterval(updateServerStatus, 12000);
    }
    updatePauseButton();
    updateMobileControlBarInset();
    requestAnimationFrame(updateMobileControlBarInset);
    initCfSync();
    bindPlaybackRecoverySettings();
    loadPlaybackRecoverySettings();
  }

  document.addEventListener("DOMContentLoaded", init);
})();

/* ── Cloudflare 동기화 (브라우저 → Worker 직접 호출) ───────────────────── */
(function () {
  const WORKER = 'https://auto-music-player-backend.rukkit.workers.dev';
  let _jwt = null;

  function setCfStatus(msg, type = '') {
    const el = document.getElementById('cfSyncStatus');
    if (!el) return;
    el.textContent = msg;
    el.className = 'cf-sync-status ' + type;
  }

  // Worker에 로그인해서 JWT 획득
  async function cfLogin() {
    const res = await fetch(`${WORKER}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'admin', password: '1234' }),
    });
    const data = await res.json();
    if (!data.token) throw new Error('Worker 로그인 실패: ' + (data.error || `HTTP ${res.status}`));
    _jwt = data.token;
  }

  // Worker API 호출 (자동 로그인 + 401 시 재시도)
  async function workerFetch(method, path, body, retry = true) {
    if (!_jwt) await cfLogin();
    const res = await fetch(`${WORKER}${path}`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${_jwt}`,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (res.status === 401 && retry) {
      _jwt = null;
      return workerFetch(method, path, body, false);
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Worker HTTP ${res.status}`);
    return data;
  }

  // 로컬 Flask 호출
  async function localFetch(method, path, body) {
    const res = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `로컬 HTTP ${res.status}`);
    return data;
  }

  // 📤 앱 동기화: 로컬 상태 읽기 → Worker에 푸시
  async function doPush() {
    const local = await localFetch('GET', '/api/local/state');
    await workerFetch('POST', '/api/sync/push', {
      playlist: local.playlist,
      settings: local.settings,
    });
    return local.playlist.length;
  }

  // 📥 데이터베이스 동기화: Worker에서 당기기 → 로컬에 적용
  async function doPull() {
    const remote = await workerFetch('GET', '/api/sync/pull');
    const result = await localFetch('POST', '/api/local/apply', {
      playlist: remote.playlist || [],
      settings: remote.settings || {},
    });
    return result.songs ?? 0;
  }

  async function loadCfConfig() {
    try {
      const cfgData = await localFetch('GET', '/api/cf/config');
      const pullEl = document.getElementById('cfAutoPull');
      if (pullEl) pullEl.checked = cfgData.cf_auto_pull_on_start !== false;
      setCfStatus('준비됨', '');
    } catch (_) {
      setCfStatus('준비됨', '');  // 연결 실패해도 버튼은 활성화
    }
  }

  window.initCfSync = function () {
    loadCfConfig();

    // 📤 앱 동기화
    const btnPush = document.getElementById('btnCfPush');
    if (btnPush) {
      btnPush.addEventListener('click', async () => {
        btnPush.disabled = true;
        setCfStatus('📤 DB에 저장 중…', 'busy');
        try {
          const count = await doPush();
          setCfStatus(`✅ ${new Date().toLocaleTimeString('ko-KR')} 저장됨 (${count}곡)`, 'ok');
        } catch (e) {
          setCfStatus('❌ ' + e.message, 'err');
          console.error('[CF push]', e);
        } finally {
          btnPush.disabled = false;
        }
      });
    }

    // 📥 데이터베이스 동기화
    const btnPull = document.getElementById('btnCfPull');
    if (btnPull) {
      btnPull.addEventListener('click', async () => {
        btnPull.disabled = true;
        setCfStatus('📥 DB에서 불러오는 중…', 'busy');
        try {
          const count = await doPull();
          setCfStatus(`✅ ${new Date().toLocaleTimeString('ko-KR')} (${count}곡)`, 'ok');
        } catch (e) {
          setCfStatus('❌ ' + e.message, 'err');
          console.error('[CF pull]', e);
        } finally {
          btnPull.disabled = false;
        }
      });
    }

    // 설정 저장 (자동 동기화 토글)
    const btnSave = document.getElementById('btnSaveCfConfig');
    if (btnSave) {
      btnSave.addEventListener('click', async () => {
        const hint = document.getElementById('cfSaveHint');
        try {
          await localFetch('POST', '/api/cf/config', {
            cf_auto_pull_on_start: document.getElementById('cfAutoPull')?.checked ?? true,
          });
          if (hint) { hint.textContent = '✅ 저장됨'; hint.style.color = '#22a35a'; }
        } catch (e) {
          if (hint) { hint.textContent = '❌ ' + e.message; hint.style.color = '#c0392b'; }
        }
        if (hint) setTimeout(() => { hint.textContent = ''; }, 3000);
      });
    }
  };

})();
