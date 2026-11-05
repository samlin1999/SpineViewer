// 網頁介面測試共用：開一個 spineview serve 和無頭 Chrome，透過 DevTools Protocol 操作。
// 需要 Node 22+（內建 WebSocket）與 Chrome／Edge；找不到瀏覽器時回傳 null（呼叫端應跳過）。
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const BROWSERS = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
];
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function makeChecker() {
  let failures = 0;
  const check = (ok, label, extra) => {
    console.log(`${ok ? "PASS" : "FAIL"}  ${label}${extra !== undefined && extra !== "" ? "  " + extra : ""}`);
    if (!ok) failures++;
  };
  return { check, failures: () => failures };
}

async function launch(serveArgs) {
  const browser = BROWSERS.find(p => fs.existsSync(p));
  if (!browser) return null;
  const root = path.join(__dirname, "..");
  const port = 8900 + Math.floor(Math.random() * 80);
  const debug = port + 100;
  // PYTHONUNBUFFERED：stdout 接到管線時 Python 會整塊緩衝，不加就讀不到「已啟動」那一行
  const server = spawn("python", ["spineview.py", "serve", ...serveArgs, "--port", String(port), "--no-browser"],
                       { cwd: root, stdio: ["ignore", "pipe", "pipe"],
                         env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" } });
  let serverOut = "";
  server.stdout.on("data", d => serverOut += d);
  server.stderr.on("data", d => serverOut += d);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "spineview-ui-"));
  const chrome = spawn(browser, ["--headless=new", "--enable-unsafe-swiftshader", "--use-angle=swiftshader",
                                 `--remote-debugging-port=${debug}`, `--user-data-dir=${profile}`,
                                 "--window-size=1600,900", "about:blank"], { stdio: "ignore" });
  const cleanup = () => {
    try { chrome.kill(); } catch {}
    try { server.kill(); } catch {}
    setTimeout(() => { try { fs.rmSync(profile, { recursive: true, force: true }); } catch {} }, 500);
  };
  process.on("exit", cleanup);

  let actualPort = null;
  for (let i = 0; i < 60 && !actualPort; i++) {
    const m = serverOut.match(/127\.0\.0\.1:(\d+)/);
    if (m) actualPort = +m[1]; else await sleep(200);
  }
  if (!actualPort) { cleanup(); throw new Error("伺服器沒有啟動：" + serverOut); }
  let target;
  for (let i = 0; i < 60 && !target; i++) {
    try {
      const list = await fetch(`http://127.0.0.1:${debug}/json`).then(r => r.json());
      target = list.find(t => t.type === "page");
    } catch { await sleep(200); }
  }
  if (!target) { cleanup(); throw new Error("連不上瀏覽器"); }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener("open", r, { once: true }));
  let seq = 0;
  const waiting = new Map();
  const consoleErrors = [];
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && waiting.has(msg.id)) { waiting.get(msg.id)(msg); waiting.delete(msg.id); }
    if (msg.method === "Runtime.exceptionThrown") {
      consoleErrors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
    }
    if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
      consoleErrors.push(msg.params.args.map(a => a.value ?? a.description).join(" "));
    }
  });
  const send = (method, params = {}) => new Promise(resolve => {
    const id = ++seq;
    waiting.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expr) => {
    const res = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    if (res.result.exceptionDetails) throw new Error(res.result.exceptionDetails.exception?.description || expr);
    return res.result.result.value;
  };
  const waitFor = async (expr, ms = 20000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      try { if (await evaluate(expr)) return true; } catch {}
      await sleep(150);
    }
    return false;
  };
  await send("Runtime.enable");
  await send("Page.enable");
  await send("DOM.enable");
  const base = `http://127.0.0.1:${actualPort}/`;
  return { base, send, evaluate, waitFor, consoleErrors, cleanup: () => { ws.close(); cleanup(); } };
}

// WebGL 畫布上有沒有畫出彩色內容（背景格子是灰的，有顏色就代表畫到東西）
const PAINTED_EXPR = `(() => {
  const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;
  const px = new Uint8Array(w * h * 4);
  gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
  let colored = 0;
  for (let i = 0; i < px.length; i += 16) {
    const r = px[i], g = px[i + 1], b = px[i + 2];
    if (Math.abs(r - g) > 12 || Math.abs(g - b) > 12) colored++;
  }
  return colored;
})()`;

module.exports = { launch, makeChecker, sleep, PAINTED_EXPR };
