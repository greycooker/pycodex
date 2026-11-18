// Server-staged attachments: the model receives paths, never file bytes.
export const MAX_FILE_BYTES = 256 * 1024 * 1024;

export function uploadPart(file) {
  return {
    type: "context", id: crypto.getRandomValues(new Uint32Array(4)).join("-"), label: file.name,
    text: "Uploaded file stored on this server. Read the path with file tools as needed.\n" +
      JSON.stringify({name: file.name, path: file.path, bytes: file.bytes}),
  };
}

export function mountServerUploads(host) {
  const form = document.getElementById("composer");
  const surface = form?.querySelector(".composer-surface");
  const prompt = document.getElementById("prompt");
  if (!surface || !prompt || surface.querySelector("[data-server-uploads]")) return;
  const node = (tag, text = "") => {
    const element = document.createElement(tag);
    element.textContent = text;
    return element;
  };
  const root = node("section");
  root.dataset.serverUploads = "";
  root.className = "server-uploads";
  const toolbar = node("div");
  const picker = node("button", "添加文件");
  picker.type = "button";
  picker.title = "选择文件，也可拖拽或粘贴文件到聊天框";
  const hint = node("span", "单文件最大 256 MB");
  const input = node("input");
  input.type = "file";
  input.multiple = true;
  input.hidden = true;
  input.setAttribute("aria-label", "选择上传文件");
  const tray = node("ul");
  tray.setAttribute("aria-label", "上传的文件");
  tray.setAttribute("aria-live", "polite");
  toolbar.append(picker, hint, input);
  root.append(toolbar, tray);
  surface.insertBefore(root, document.getElementById("contextBlockDetail"));
  const entries = [], listeners = [];
  let epoch = 0, disposed = false, working = false, active = null;
  let limit = MAX_FILE_BYTES, available = false;

  function listen(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    listeners.push(() => target.removeEventListener(type, handler, options));
  }
  function pending() {
    return entries.some(entry => ["queued", "uploading"].includes(entry.state));
  }
  function render() {
    const partIds = new Set(host.getDraft().parts.map(part => part.id));
    for (const entry of entries) {
      if (entry.state === "ready" && !partIds.has(entry.partId)) entry.hidden = true;
    }
    tray.replaceChildren();
    for (const entry of entries.filter(item => !item.hidden)) {
      const row = node("li");
      const name = node("span", entry.file.name);
      name.className = "server-upload-name";
      row.append(name);
      const status = entry.state === "ready" ? "已上传" :
        entry.state === "queued" ? "等待上传" :
        entry.state === "uploading" ? `上传中 ${entry.percent}%` : entry.error;
      row.append(node("span", status));
      if (entry.state === "error" && entry.file.size <= limit) {
        const retry = node("button", "重试");
        retry.type = "button";
        retry.addEventListener("click", () => {
          entry.state = "queued";
          entry.error = "";
          render();
          pump();
        });
        row.append(retry);
      }
      const remove = node("button", entry.state === "uploading" ? "取消" : "移除");
      remove.type = "button";
      remove.addEventListener("click", () => {
        entry.hidden = true;
        if (entry.state === "uploading") active?.abort();
        entry.state = "removed";
        if (entry.partId) {
          const draft = host.getDraft();
          host.setDraft({...draft, parts: draft.parts.filter(part => part.id !== entry.partId)});
        }
        render();
      });
      row.append(remove);
      tray.append(row);
    }
    tray.hidden = !tray.children.length;
  }
  async function checkAvailable() {
    if (available) return;
    const config = await host.request("api/uploads");
    limit = config.max_file_bytes;
    if (!Number.isSafeInteger(limit) || limit <= 0) throw new Error("上传接口配置无效");
    available = true;
  }
  function send(entry, sessionId) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      active = xhr;
      xhr.open("POST", new URL(
        `api/sessions/${encodeURIComponent(sessionId)}/uploads`, location.href));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("X-Upload-Filename", encodeURIComponent(entry.file.name));
      xhr.upload.onprogress = event => {
        if (event.lengthComputable && entry.state === "uploading") {
          entry.percent = Math.floor(event.loaded * 100 / event.total);
          render();
        }
      };
      xhr.onload = () => {
        let payload;
        try { payload = JSON.parse(xhr.responseText); } catch { payload = {}; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
        else reject(new Error(payload.detail || `上传失败 HTTP ${xhr.status}`));
      };
      xhr.onerror = () => reject(new Error("网络错误，点击重试"));
      xhr.onabort = () => reject(new Error("上传已取消"));
      xhr.send(entry.file);
    });
  }
  async function pump() {
    if (working || disposed) return;
    working = true;
    const current = epoch;
    try {
      let entry;
      while (!disposed && epoch === current &&
             (entry = entries.find(item => item.state === "queued"))) {
        entry.state = "uploading";
        entry.percent = 0;
        render();
        try {
          await checkAvailable();
          if (disposed || epoch !== current || entry.state === "removed") break;
          if (entry.file.size > limit) throw new Error("文件超过 256 MB 上限");
          const uploaded = await send(entry, host.getActiveSessionId());
          if (disposed || epoch !== current || entry.state === "removed") continue;
          if (typeof uploaded.path !== "string" || !uploaded.path ||
              uploaded.bytes !== entry.file.size) throw new Error("上传结果不完整，请重试");
          const draft = host.getDraft();
          const part = uploadPart(uploaded);
          host.setDraft({...draft, parts: [...draft.parts, part]});
          entry.partId = part.id;
          entry.state = "ready";
        } catch (error) {
          if (epoch !== current || disposed) break;
          if (entry.state !== "removed") {
            entry.state = "error";
            entry.error = error.message.includes("404") ?
              "上传接口尚未加载，请更新服务后重试" : error.message;
          }
        } finally {
          active = null;
          if (!disposed && epoch === current) render();
        }
      }
    } finally {
      working = false;
      if (!disposed && entries.some(item => item.state === "queued")) pump();
    }
  }
  function add(files) {
    const remaining = 32 - entries.filter(entry => !entry.hidden).length;
    const selected = Array.from(files);
    if (selected.length > remaining) hint.textContent = "一次最多保留 32 个文件，请分批添加";
    for (const file of selected.slice(0, Math.max(0, remaining))) {
      entries.push({file, state: file.size > limit ? "error" : "queued",
        error: file.size > limit ? "文件超过 256 MB 上限" : "", percent: 0});
    }
    render();
    pump();
  }
  listen(picker, "click", () => { input.value = ""; input.click(); });
  listen(input, "change", () => add(input.files));
  listen(surface, "dragover", event => {
    if (Array.from(event.dataTransfer?.types || []).includes("Files")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      event.dataTransfer.dropEffect = "copy";
    }
  }, true);
  listen(surface, "drop", event => {
    if (event.dataTransfer?.files.length) {
      event.preventDefault();
      event.stopImmediatePropagation();
      add(event.dataTransfer.files);
    }
  }, true);
  listen(prompt, "paste", event => {
    if (event.clipboardData?.files.length) {
      event.preventDefault();
      event.stopImmediatePropagation();
      add(event.clipboardData.files);
    }
  }, true);
  function block(event) {
    if (!pending()) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    hint.textContent = "请等待文件上传完成后再发送";
  }
  listen(form, "submit", block, true);
  listen(form, "keydown", event => {
    if (event.target === prompt && event.key === "Enter" && !event.shiftKey &&
        !event.isComposing && event.keyCode !== 229) block(event);
  }, true);
  const observer = new MutationObserver(() => {
    hint.textContent = pending() ? "上传完成后可发送" : "单文件最大 256 MB";
    render();
  });
  observer.observe(prompt, {childList: true, subtree: true, characterData: true});
  function reset() {
    epoch++;
    active?.abort();
    entries.length = 0;
    render();
  }
  const unsubscribe = host.onSessionChanged(reset);
  listen(window, "pagehide", reset);
  render();
  return () => {
    disposed = true;
    reset();
    unsubscribe();
    observer.disconnect();
    listeners.forEach(dispose => dispose());
    root.remove();
  };
}
