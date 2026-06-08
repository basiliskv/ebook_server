(() => {
  const cfg = window.__VIEWER__;
  if (!cfg) return;

  const pages = cfg.pages;
  const kind = cfg.kind;
  const bookQ = cfg.bookQ;
  const library = cfg.library || "dojin";
  const indexUrl = cfg.indexUrl || "/";
  const nextBook = cfg.nextBook || "";
  const nextBookOpenUrl = cfg.nextBookOpenUrl || "";
  const nextBookCoverUrl = cfg.nextBookCoverUrl || "";
  const icons = cfg.icons || {};
  const TOTAL = pages.length;
  const pagePosKey = `viewer_idx:${library}:${bookQ}`;
  const videoSourceKey = "video_source_mode";

  const viewer = document.getElementById("viewer");
  const spreadWrap = document.getElementById("spreadWrap");
  const imgL = document.getElementById("imgL");
  const imgR = document.getElementById("imgR");
  const vidL = document.getElementById("vidL");
  const vidR = document.getElementById("vidR");
  const slider = document.getElementById("slider");
  const videoSeek = document.getElementById("videoSeek");
  const videoBufferWrap = document.getElementById("videoBufferWrap");
  const videoBufferBar = document.getElementById("videoBufferBar");
  const muteBtn = document.getElementById("muteBtn");
  const videoSourceWrap = document.getElementById("videoSourceWrap");
  const videoSourceBtn = document.getElementById("videoSourceBtn");
  const videoSourceMenu = document.getElementById("videoSourceMenu");
  const videoSourceOptions = [...document.querySelectorAll(".videoSourceOption")];
  const ui = document.getElementById("ui");
  const page = document.getElementById("page");
  const listBtn = document.getElementById("listBtn");
  const listCloseBtn = document.getElementById("listCloseBtn");
  const fileListPanel = document.getElementById("fileListPanel");
  const fileList = document.getElementById("fileList");
  const nextBookSuggest = document.getElementById("nextBookSuggest");
  const nextBookCover = document.getElementById("nextBookCover");
  const nextBookTitle = document.getElementById("nextBookTitle");

  const prevBtn = document.getElementById("prevBtn");
  const nextBtn = document.getElementById("nextBtn");
  const swapBtn = document.getElementById("swapBtn");
  const modeBtn = document.getElementById("modeBtn");

  let idx = +localStorage.getItem(pagePosKey) || 0;
  idx = Math.max(0, Math.min(idx, Math.max(0, TOTAL - 1)));
  let spread = localStorage.getItem("spread") === "1";
  let spreadOffset = 0;
  let activeVideo = null;
  let animTimer = null;
  let drag = null;
  const prefetched = new Set();
  let videoSourceMode =
    localStorage.getItem(videoSourceKey) === "original" ? "original" : "cache";

  viewer.focus();
  viewer.addEventListener("click", () => viewer.focus());

  viewer.addEventListener("click", (e) => {
    const vh = window.innerHeight || 0;
    if (vh > 0 && e.clientY >= vh * 0.8) {
      ui.classList.contains("show") ? hideUI() : showUI();
    }
  });

  document.addEventListener("click", (e) => {
    if (!ui.classList.contains("show")) return;
    if (ui.contains(e.target)) return;
    hideUI();
  });

  function pageUrl(i) {
    const suffix =
      pageType(i) === "video" && videoSourceMode === "original"
        ? "&original=1"
        : "";
    if (kind === "pdf") {
      return `/pdf_page?book=${bookQ}&page=${pages[i]}&library=${encodeURIComponent(library)}${suffix}`;
    }
    if (kind === "eagle") {
      return `/eagle_page?book=${bookQ}&page=${encodeURIComponent(pages[i])}&library=${encodeURIComponent(library)}${suffix}`;
    }
    return `/zip_page?book=${bookQ}&page=${encodeURIComponent(pages[i])}&library=${encodeURIComponent(library)}${suffix}`;
  }

  function pageType(i) {
    const p = String(pages[i] ?? "").toLowerCase();
    if (
      p.endsWith(".mp4") ||
      p.endsWith(".webm") ||
      p.endsWith(".mov") ||
      p.endsWith(".m4v")
    ) return "video";
    if (p.endsWith(".gif")) return "gif";
    return "image";
  }

  function prefetchUrl(i) {
    if (i < 0 || i >= TOTAL) return;
    const url = pageUrl(i);
    if (prefetched.has(url)) return;
    prefetched.add(url);

    const type = pageType(i);
    if (type === "video") {
      fetch(url, { cache: "force-cache" }).catch(() => {});
      return;
    }

    const img = new Image();
    img.decoding = "async";
    img.loading = "eager";
    img.src = url;
  }

  function prefetchAround(baseIdx) {
    const step = spread ? 2 : 1;
    prefetchUrl(baseIdx + step);
    prefetchUrl(baseIdx + step * 2);
    prefetchUrl(baseIdx - step);
  }

  function updateModeIcon() {
    modeBtn.src = spread ? icons.double : icons.single;
  }

  function updateNextSuggestion() {
    if (!nextBookSuggest || !nextBookCover || !nextBookTitle) return;
    if (!nextBook || !nextBookOpenUrl || !nextBookCoverUrl) {
      nextBookSuggest.classList.remove("show");
      return;
    }
    const rightVisible = spread ? idx + spreadOffset : idx;
    const isLast = rightVisible >= TOTAL - 1;
    if (!isLast) {
      nextBookSuggest.classList.remove("show");
      return;
    }
    nextBookSuggest.href = nextBookOpenUrl;
    nextBookCover.src = nextBookCoverUrl;
    nextBookTitle.textContent = nextBook.split("/").pop();
    nextBookSuggest.classList.add("show");
  }

  function displayName(p, i) {
    if (kind === "pdf") return `page-${String(i + 1).padStart(3, "0")}`;
    const parts = String(p || "").split("/");
    return parts[parts.length - 1] || String(p || "");
  }

  function renderFileList() {
    if (!fileList) return;
    const html = pages
      .map((p, i) => {
        const name = displayName(p, i);
        const active = i === idx ? "active" : "";
        return `<div class="fileItem ${active}" data-idx="${i}">
          <div class="name">${name}</div>
          <div class="idx">${i + 1}</div>
        </div>`;
      })
      .join("");
    fileList.innerHTML = html;
  }

  function openFileList() {
    renderFileList();
    fileListPanel.classList.add("show");
    fileListPanel.setAttribute("aria-hidden", "false");
    showUI();
  }

  function closeFileList() {
    fileListPanel.classList.remove("show");
    fileListPanel.setAttribute("aria-hidden", "true");
  }

  function setActiveVideo(vid) {
    activeVideo = vid;
    if (!activeVideo) {
      videoSeek.style.display = "none";
      videoBufferWrap.style.display = "none";
      videoBufferBar.style.width = "0%";
      muteBtn.style.display = "none";
      videoSourceWrap.style.display = "none";
      videoSourceMenu.classList.remove("show");
      return;
    }
    videoSeek.style.display = "block";
    videoBufferWrap.style.display = "block";
    muteBtn.style.display = "inline-block";
    videoSourceWrap.style.display = "block";
    videoSeek.max = activeVideo.duration || 0;
    videoSeek.value = activeVideo.currentTime || 0;
    muteBtn.src = activeVideo.muted ? icons.mute : icons.sound;
    updateVideoSourceUi();
    updateVideoBuffer(activeVideo);
  }

  function updateVideoSourceUi() {
    videoSourceBtn.textContent = videoSourceMode === "original" ? "orig" : "cache";
    videoSourceOptions.forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === videoSourceMode);
    });
  }

  function updateVideoBuffer(vid = activeVideo) {
    if (!vid || vid !== activeVideo) return;
    const duration = vid.duration || 0;
    if (!duration || !Number.isFinite(duration) || !vid.buffered.length) {
      videoBufferBar.style.width = "0%";
      return;
    }
    const end = vid.buffered.end(vid.buffered.length - 1);
    const percent = Math.max(0, Math.min(100, (end / duration) * 100));
    videoBufferBar.style.width = `${percent}%`;
  }

  function updateActiveVideo() {
    if (vidR.style.display === "block") {
      setActiveVideo(vidR);
      return;
    }
    if (vidL.style.display === "block") {
      setActiveVideo(vidL);
      return;
    }
    setActiveVideo(null);
  }

  function hideSlot(img, vid) {
    img.style.display = "none";
    vid.pause();
    vid.removeAttribute("src");
    vid.load();
    vid.style.display = "none";
  }

  function setSlot(img, vid, i, autoplayVideo = true) {
    if (!pages[i]) {
      hideSlot(img, vid);
      return;
    }
    const url = pageUrl(i);
    const type = pageType(i);
    if (type === "video") {
      img.style.display = "none";
      if (vid.src !== url) vid.src = url;
      vid.style.display = "block";
      vid.currentTime = 0;
      if (autoplayVideo) vid.play().catch(() => {});
      return;
    }
    vid.pause();
    vid.removeAttribute("src");
    vid.load();
    vid.style.display = "none";
    img.src = url;
    img.style.display = "block";
  }

  function setSlotOn(slot, i, autoplayVideo = true) {
    const img = slot.querySelector("img");
    const vid = slot.querySelector("video");
    setSlot(img, vid, i, autoplayVideo);
  }

  function createSpreadNode(centerIdx) {
    const node = document.createElement("div");
    node.className = "spread-clone";

    const slotR = document.createElement("div");
    slotR.className = "slot";
    slotR.innerHTML =
      '<img><video playsinline muted loop preload="metadata"></video>';

    const slotL = document.createElement("div");
    slotL.className = "slot";
    slotL.innerHTML =
      '<img><video playsinline muted loop preload="metadata"></video>';

    if (!spread) {
      node.classList.add("single");
      node.appendChild(slotR);
      setSlotOn(slotR, centerIdx, false);
      return node;
    }

    node.appendChild(slotR);
    node.appendChild(slotL);

    const base = centerIdx + spreadOffset;
    setSlotOn(slotR, base, false);
    setSlotOn(slotL, base - 1, false);
    return node;
  }

  function load() {
    if (!spread) {
      setSlot(imgR, vidR, idx);
      hideSlot(imgL, vidL);
      updateActiveVideo();
      return;
    }

    const base = idx + spreadOffset;
    setSlot(imgR, vidR, base);
    setSlot(imgL, vidL, base - 1);
    updateActiveVideo();
  }

  function render() {
    load();
    slider.value = idx;
    page.textContent = `${idx + 1} / ${TOTAL}`;
    localStorage.setItem(pagePosKey, idx);
    localStorage.setItem("spread", spread ? "1" : "0");
    updateModeIcon();
    updateNextSuggestion();
    if (fileListPanel.classList.contains("show")) renderFileList();
    prefetchAround(idx);
  }
  render();

  function makeSpreadClone() {
    const clone = spreadWrap.cloneNode(true);
    clone.removeAttribute("id");
    clone.classList.add("spread-clone");
    clone.querySelectorAll("video").forEach((v) => {
      v.pause();
      v.removeAttribute("autoplay");
      v.controls = false;
      v.muted = true;
    });
    return clone;
  }

  function animatePageTurn(dir, applyChange) {
    clearDragLayer();
    clearTimeout(animTimer);
    spreadWrap.classList.remove("enter-from-right", "enter-from-left");
    viewer.querySelectorAll(".slide-layer").forEach((el) => el.remove());

    const layer = document.createElement("div");
    layer.className = "slide-layer";
    const oldClone = makeSpreadClone();
    oldClone.classList.add(dir > 0 ? "exit-to-right" : "exit-to-left");
    layer.appendChild(oldClone);
    viewer.appendChild(layer);

    applyChange();
    render();

    void spreadWrap.offsetWidth;
    spreadWrap.classList.add(dir > 0 ? "enter-from-left" : "enter-from-right");

    animTimer = setTimeout(() => {
      spreadWrap.classList.remove("enter-from-right", "enter-from-left");
      layer.remove();
    }, 240);
  }

  function moveTo(nextIdx, dirHint) {
    const clamped = Math.max(0, Math.min(nextIdx, TOTAL - 1));
    if (clamped === idx) return;
    const dir = dirHint || (clamped > idx ? 1 : -1);
    animatePageTurn(dir, () => {
      idx = clamped;
    });
  }

  function clearDragLayer() {
    if (!drag) return;
    clearTimeout(drag.cleanupTimer);
    if (drag.layer) drag.layer.remove();
    spreadWrap.style.opacity = "";
    drag = null;
  }

  function startDrag(clientX) {
    const step = spread ? 2 : 1;
    drag = {
      startX: clientX,
      dx: 0,
      dir: 0,
      step,
      targetIdx: idx,
      width: Math.max(1, viewer.clientWidth),
      layer: null,
      track: null,
      cleanupTimer: null,
    };
  }

  function ensureDragLayer(dir) {
    if (!drag || drag.layer) return;
    const targetIdx = Math.max(
      0,
      Math.min(idx + (dir < 0 ? drag.step : -drag.step), TOTAL - 1)
    );
    if (targetIdx === idx) return;

    drag.dir = dir;
    drag.targetIdx = targetIdx;

    const layer = document.createElement("div");
    layer.className = "slide-layer dragging";
    const track = document.createElement("div");
    track.className = "drag-track";

    const a = document.createElement("div");
    const b = document.createElement("div");
    a.className = "drag-item";
    b.className = "drag-item";
    const rect = spreadWrap.getBoundingClientRect();

    if (dir > 0) {
      const currentNode = createSpreadNode(idx);
      const prevNode = createSpreadNode(targetIdx);
      currentNode.style.width = `${rect.width}px`;
      currentNode.style.height = `${rect.height}px`;
      prevNode.style.width = `${rect.width}px`;
      prevNode.style.height = `${rect.height}px`;
      a.appendChild(currentNode);
      b.appendChild(prevNode);
      track.style.transform = "translateX(0px)";
    } else {
      const nextNode = createSpreadNode(targetIdx);
      const currentNode = createSpreadNode(idx);
      nextNode.style.width = `${rect.width}px`;
      nextNode.style.height = `${rect.height}px`;
      currentNode.style.width = `${rect.width}px`;
      currentNode.style.height = `${rect.height}px`;
      a.appendChild(nextNode);
      b.appendChild(currentNode);
      track.style.transform = `translateX(${-drag.width}px)`;
    }

    track.appendChild(a);
    track.appendChild(b);
    layer.appendChild(track);
    viewer.appendChild(layer);
    spreadWrap.style.opacity = "0";

    drag.layer = layer;
    drag.track = track;
  }

  function updateDrag(clientX) {
    if (!drag) return;
    drag.dx = clientX - drag.startX;
    const abs = Math.abs(drag.dx);
    if (!drag.layer && abs > 6) {
      ensureDragLayer(drag.dx < 0 ? 1 : -1);
    }
    if (!drag.layer) return;

    let tx = 0;
    if (drag.dir > 0) {
      tx = Math.max(-drag.width, Math.min(0, drag.dx));
    } else {
      tx = Math.max(-drag.width, Math.min(0, -drag.width + drag.dx));
    }
    drag.track.style.transition = "none";
    drag.track.style.transform = `translateX(${tx}px)`;
  }

  function endDrag() {
    if (!drag) return;
    const info = drag;
    drag = null;

    if (!info.layer) {
      if (Math.abs(info.dx) < 10) {
        ui.classList.contains("show") ? hideUI() : showUI();
      }
      return;
    }

    const commit = Math.abs(info.dx) > info.width * 0.18;
    const finalTx =
      info.dir > 0
        ? (commit ? -info.width : 0)
        : (commit ? 0 : -info.width);

    info.track.style.transition = "transform .18s ease-out";
    info.track.style.transform = `translateX(${finalTx}px)`;

    info.cleanupTimer = setTimeout(() => {
      info.layer.remove();
      spreadWrap.style.opacity = "";
      if (commit) {
        idx = info.targetIdx;
        render();
      }
    }, 190);
  }

  let timer = null;
  function showUI() {
    ui.classList.add("show");
    document.body.classList.add("uiVisible");
    clearTimeout(timer);
    timer = setTimeout(() => {
      ui.classList.remove("show");
      document.body.classList.remove("uiVisible");
    }, 1800);
  }
  function hideUI() {
    ui.classList.remove("show");
    document.body.classList.remove("uiVisible");
    clearTimeout(timer);
    closeFileList();
  }

  prevBtn.onclick = () => {
    moveTo(idx - (spread ? 2 : 1), -1);
    showUI();
  };
  nextBtn.onclick = () => {
    moveTo(idx + (spread ? 2 : 1), 1);
    showUI();
  };
  modeBtn.onclick = () => {
    spread = !spread;
    spreadOffset = 0;
    render();
    showUI();
  };
  swapBtn.onclick = () => {
    if (spread) {
      spreadOffset = spreadOffset ? 0 : 1;
      render();
      showUI();
    }
  };
  slider.oninput = (e) => {
    moveTo(+e.target.value);
    showUI();
  };
  videoSeek.oninput = (e) => {
    if (activeVideo) activeVideo.currentTime = +e.target.value;
  };
  muteBtn.onclick = () => {
    if (activeVideo) {
      activeVideo.muted = !activeVideo.muted;
      muteBtn.src = activeVideo.muted ? icons.mute : icons.sound;
    }
  };
  videoSourceBtn.onclick = () => {
    videoSourceMenu.classList.toggle("show");
    videoSourceMenu.setAttribute(
      "aria-hidden",
      videoSourceMenu.classList.contains("show") ? "false" : "true"
    );
  };
  videoSourceOptions.forEach((btn) => {
    btn.onclick = () => {
      const nextMode = btn.dataset.mode === "original" ? "original" : "cache";
      if (nextMode !== videoSourceMode) {
        videoSourceMode = nextMode;
        localStorage.setItem(videoSourceKey, videoSourceMode);
        prefetched.clear();
        render();
      }
      updateVideoSourceUi();
      videoSourceMenu.classList.remove("show");
      videoSourceMenu.setAttribute("aria-hidden", "true");
    };
  });

  listBtn.onclick = () => {
    fileListPanel.classList.contains("show") ? closeFileList() : openFileList();
  };
  listCloseBtn.onclick = closeFileList;
  fileList.addEventListener("click", (e) => {
    const item = e.target.closest(".fileItem");
    if (!item) return;
    moveTo(+item.dataset.idx || 0);
  });
  document.addEventListener("click", (e) => {
    if (!videoSourceMenu.classList.contains("show")) return;
    if (videoSourceWrap.contains(e.target)) return;
    videoSourceMenu.classList.remove("show");
    videoSourceMenu.setAttribute("aria-hidden", "true");
  });

  function attachVideoEvents(vid) {
    vid.addEventListener("loadedmetadata", () => {
      if (vid === activeVideo) {
        videoSeek.max = vid.duration || 0;
        videoSeek.value = vid.currentTime || 0;
        muteBtn.src = vid.muted ? icons.mute : icons.sound;
        updateVideoBuffer(vid);
      }
    });
    vid.addEventListener("progress", () => updateVideoBuffer(vid));
    vid.addEventListener("loadeddata", () => updateVideoBuffer(vid));
    vid.addEventListener("canplay", () => updateVideoBuffer(vid));
    vid.addEventListener("stalled", () => updateVideoBuffer(vid));
    vid.addEventListener("waiting", () => updateVideoBuffer(vid));
    vid.addEventListener("emptied", () => {
      if (vid === activeVideo) videoBufferBar.style.width = "0%";
    });
    vid.addEventListener("timeupdate", () => {
      if (vid === activeVideo && !videoSeek.matches(":active")) {
        videoSeek.value = vid.currentTime || 0;
      }
    });
    vid.addEventListener("play", () => {
      if (vid === activeVideo) videoSeek.style.display = "block";
    });
    vid.addEventListener("pause", () => {
      if (vid === activeVideo) videoSeek.style.display = "block";
    });
  }
  attachVideoEvents(vidR);
  attachVideoEvents(vidL);
  updateVideoSourceUi();

  viewer.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) return;
    clearDragLayer();
    startDrag(e.touches[0].clientX);
  });
  viewer.addEventListener(
    "touchmove",
    (e) => {
      if (!drag || e.touches.length !== 1) return;
      updateDrag(e.touches[0].clientX);
      if (drag.layer) e.preventDefault();
    },
    { passive: false }
  );
  viewer.addEventListener("touchend", () => endDrag());
  viewer.addEventListener("touchcancel", () => endDrag());

  viewer.addEventListener("keydown", (e) => {
    let target = idx;

    const key = e.key.toLowerCase();
    if (key === "arrowright" || key === "arrowdown" || key === "d") {
      target = idx + (spread ? 2 : 1);
    }
    if (key === "arrowleft" || key === "arrowup" || key === "c") {
      target = idx - (spread ? 2 : 1);
    }
    if (e.key === "Escape") {
      closeViewer();
      return;
    }

    if (target !== idx) {
      e.preventDefault();
      moveTo(target);
    }
  });

  viewer.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      moveTo(idx + (e.deltaY > 0 ? (spread ? 2 : 1) : -(spread ? 2 : 1)));
      showUI();
    },
    { passive: false }
  );

  function closeViewer() {
    const url = new URL(indexUrl, location.origin);
    url.searchParams.set("back", "1");
    location.href = url.pathname + url.search;
  }

  window.closeViewer = closeViewer;
})();
