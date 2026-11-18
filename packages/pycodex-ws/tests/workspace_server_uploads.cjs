// Manual browser regression; uses local fixtures and never calls a model.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright");

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    let hold, status = 201, requests = 0;
    await page.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/w/test/") return route.fulfill({contentType: "text/html", body: `
        <form id="composer"><div class="composer-surface">
          <div id="prompt" contenteditable="true"></div><div id="contextBlockDetail"></div>
          <button id="send" type="submit">Send</button>
        </div></form>
        <script type="module">
          import {mountServerUploads} from "./server_uploads.mjs";
          let selected = "a", draft = {parts: [{type:"text",text:"existing draft"}]}, changed;
          window.fixture = {
            get draft() {return draft;},
            set draft(value) {draft = value;},
            submissions: 0, drops: 0,
            switch() {selected = selected === "a" ? "b" : "a"; changed();},
          };
          const host = {
            getDraft: () => draft, setDraft: value => {draft = value;},
            getActiveSessionId: () => selected,
            onSessionChanged: listener => {changed = listener;return () => {};},
            request: async route => {const reply=await fetch(route);return reply.json();},
          };
          mountServerUploads(host);
          document.getElementById("composer").addEventListener("submit", event => {
            event.preventDefault(); fixture.submissions++;
          });
          document.getElementById("prompt").addEventListener("drop", () => fixture.drops++);
        </script>
      `});
      if (url.pathname.endsWith("server_uploads.mjs")) return route.fulfill({
        contentType: "text/javascript",
        body: fs.readFileSync(path.join(__dirname,
          "../workspace_server/optional_features/file-attachments/source/server_uploads.mjs"), "utf8"),
      });
      if (url.pathname.endsWith("/api/uploads")) return route.fulfill({
        json: {max_file_bytes: 256 * 1024 * 1024},
      });
      if (url.pathname.endsWith("/uploads")) {
        requests++;
        if (hold) await hold;
        const name = decodeURIComponent(route.request().headers()["x-upload-filename"]);
        return route.fulfill({status, json: status === 201 ? {
          name, path: "/stored/" + name, bytes: route.request().postDataBuffer().length,
        } : {detail: "disk unavailable"}});
      }
      throw new Error("Unexpected request " + url);
    });
    await page.goto("http://workspace.test/w/test/");
    await page.locator("[data-server-uploads]").waitFor();
    let release;
    hold = new Promise(resolve => {release = resolve;});
    await page.locator('input[type="file"]').setInputFiles({
      name: "sample.txt", mimeType: "text/plain", buffer: Buffer.from("contents"),
    });
    await page.waitForFunction(() => document.querySelector(".server-uploads li")?.textContent.includes("上传中"));
    await page.locator("#send").click();
    assert.equal(await page.evaluate(() => fixture.submissions), 0);
    await page.evaluate(() => fixture.draft.parts.push({type: "text", text: "typed during upload"}));
    release(); hold = null;
    await page.waitForFunction(() => fixture.draft.parts.some(part => part.type === "context"));
    const draft = await page.evaluate(() => fixture.draft);
    assert.equal(draft.parts[0].text, "existing draft");
    assert.equal(draft.parts[1].text, "typed during upload");
    assert.ok(draft.parts[2].text.includes("/stored/sample.txt"));
    assert.ok(draft.parts[2].text.length < 300);
    assert.ok(!draft.parts[2].text.includes("contents"));
    await page.locator("#send").click();
    assert.equal(await page.evaluate(() => fixture.submissions), 1);
    await page.locator(".server-uploads li button").last().click();
    assert.equal(await page.evaluate(() => fixture.draft.parts.length), 2);
    // Drop capture keeps the built-in text-drop path from handling file bytes.
    await page.evaluate(() => {
      const transfer = new DataTransfer();
      transfer.items.add(new File(["drop"], "drop.txt"));
      document.getElementById("prompt").dispatchEvent(
        new DragEvent("drop", {bubbles: true, cancelable: true, dataTransfer: transfer}));
    });
    await page.waitForFunction(() => fixture.draft.parts.some(part => part.label === "drop.txt"));
    assert.equal(await page.evaluate(() => fixture.drops), 0);
    // A failed upload is retryable.
    status = 507;
    await page.locator('input[type="file"]').setInputFiles({
      name: "retry.txt", mimeType: "text/plain", buffer: Buffer.from("retry"),
    });
    await page.getByRole("button", {name: "重试", exact: true}).waitFor();
    status = 201;
    await page.getByRole("button", {name: "重试", exact: true}).click();
    await page.waitForFunction(() => fixture.draft.parts.some(part => part.label === "retry.txt"));
    // Oversized files never issue an upload request.
    const before = requests;
    await page.evaluate(() => {
      const file = new File(["small"], "oversized.bin");
      Object.defineProperty(file, "size", {value: 256 * 1024 * 1024 + 1});
      const transfer = new DataTransfer(); transfer.items.add(file);
      document.getElementById("prompt").dispatchEvent(
        new DragEvent("drop", {bubbles: true, cancelable: true, dataTransfer: transfer}));
    });
    await page.getByText("文件超过 256 MB 上限", {exact: true}).waitFor();
    assert.equal(requests, before);
    // Session switching cancels a pending request and cannot append to a new draft.
    hold = new Promise(resolve => {release = resolve;});
    await page.locator('input[type="file"]').setInputFiles({
      name: "late.txt", mimeType: "text/plain", buffer: Buffer.from("late"),
    });
    await page.waitForFunction(() => document.querySelector(".server-uploads").textContent.includes("上传中"));
    await page.evaluate(() => {fixture.switch(); fixture.draft = {parts: []};});
    release(); hold = null;
    await page.waitForTimeout(100);
    assert.deepEqual(await page.evaluate(() => fixture.draft.parts), []);
    assert.deepEqual(errors, []);
    console.log("Server uploads: select, drop, size guard, send guard, retry and session isolation passed.");
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
