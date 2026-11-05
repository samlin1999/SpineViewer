// 「上傳」功能的端對端測試：伺服器不開任何資料夾（--upload-only），
// 透過真正的檔案選擇框上傳一份 Spine，確認畫得出來；再試缺圖集的上傳、惡意檔名與刪除。
//
//   node tests/test_upload.js <骨架檔路徑>      （同資料夾要有它的 .atlas 與貼圖）
const fs = require("fs");
const os = require("os");
const path = require("path");
const { launch, makeChecker, PAINTED_EXPR } = require("./_browser");

const skel = process.argv[2];
if (!skel) { console.error("用法: node tests/test_upload.js <骨架檔路徑>"); process.exit(2); }
const folder = path.dirname(path.resolve(skel));
const base = path.basename(skel).replace(/\.(skel|json)$/i, "");
const atlas = path.join(folder, base + ".atlas");
const pages = fs.readFileSync(atlas, "utf8").split(/\r?\n/).map(l => l.trim())
  .filter(l => /\.(png|jpe?g|webp)$/i.test(l)).map(l => path.join(folder, l));
const { check, failures } = makeChecker();

(async () => {
  const b = await launch(["--upload-only"]);
  if (!b) { console.log("SKIP 找不到 Chrome／Edge"); return; }
  const { send, evaluate, waitFor } = b;
  await send("Page.navigate", { url: b.base });
  check(await waitFor("document.querySelectorAll('#files .file').length === 0 && el('status').textContent.includes('開啟')" +
                      " && el('status').textContent.includes('拖進來')"),
        "不開資料夾時檔案列表是空的，並提示可以開啟或拖進來");

  const setFiles = async (files) => {
    const doc = await send("DOM.getDocument", {});
    const q = await send("DOM.querySelector", { nodeId: doc.result.root.nodeId, selector: "#uploadInput" });
    await send("DOM.setFileInputFiles", { nodeId: q.result.nodeId, files });
  };

  // 完整上傳：骨架 + 圖集 + 貼圖
  await setFiles([path.resolve(skel), atlas, ...pages]);
  check(await waitFor("state.info && state.info.uploaded && state.frames.size > 0 && state.viewport", 40000),
        "上傳後自動打開並載入影格", path.basename(skel));
  check(await evaluate("[...document.querySelectorAll('#files .group')].some(g => g.textContent.includes('上傳'))"),
        "檔案列表出現「上傳」群組");
  await evaluate("setFrame(Math.floor(state.last / 2))");
  await waitFor("state.frames.has(state.frame)");
  await evaluate("redraw()");
  const painted = await evaluate(PAINTED_EXPR);
  check(painted > 50, "上傳的 Spine 確實畫在 WebGL 畫布上", painted);
  check(await evaluate("el('error').style.display !== 'block'"), "完整上傳沒有警告");
  check(await evaluate("state.info.missingRegions.length === 0 && !!state.info.atlas"), "圖集自動配對成功");

  // 只上傳骨架檔：要明確說缺什麼
  await setFiles([path.resolve(skel)]);
  check(await waitFor("el('error').style.display === 'block' && el('error').textContent.includes('少了')", 30000),
        "只上傳骨架檔時明確提示缺少圖集");
  check(await evaluate("el('error').textContent.includes('.atlas')"), "提示裡寫出缺的是 .atlas");
  check(await evaluate("el('error').textContent.includes('📂 開啟')"), "提示改用「開啟」只選 .skel");

  // 沒有骨架檔：不送出，直接說明
  await setFiles(pages.slice(0, 1));
  check(await waitFor("el('error').textContent.includes('沒有骨架檔')"), "只選貼圖時提示要一起選骨架檔");

  // 惡意檔名不能寫到上傳資料夾外面
  const id = (await fetch(b.base + "api/upload/new", { method: "POST" }).then(r => r.json())).id;
  for (const name of ["../escape.png", "..\\escape.png", "C:/escape.png", "a/../../escape.png"]) {
    const res = await fetch(`${b.base}api/upload/file?id=${id}&name=${encodeURIComponent(name)}`,
                            { method: "POST", body: "x" });
    check(res.status === 400, "拒絕不合法的檔名", name);
  }
  const odd = await fetch(`${b.base}api/upload/file?id=${id}&name=evil.exe`, { method: "POST", body: "x" })
    .then(r => r.json());
  check(odd.stored === false, "不是 Spine 會用到的副檔名不存", "evil.exe");
  const tmpEscape = path.join(os.tmpdir(), "escape.png");
  check(!fs.existsSync(tmpEscape), "暫存資料夾外面沒有被寫入檔案");

  // 檔案內容裡的文字一律當文字顯示：Spine JSON 的事件 int 寫成 HTML 也不能被執行
  const evilDir = fs.mkdtempSync(path.join(os.tmpdir(), "spineview-evil-"));
  const evil = path.join(evilDir, "evil.json");
  fs.writeFileSync(evil, JSON.stringify({
    skeleton: { spine: "3.8.99", width: 10, height: 10 },
    bones: [{ name: "root" }], slots: [], skins: [{ name: "default", attachments: {} }],
    events: { boom: {} },
    animations: { a: { events: [{ time: 0.1, name: "boom", int: '<img src=x onerror="window.__xss=1">' }] } },
  }));
  await setFiles([evil]);
  check(await waitFor("state.info && state.info.name === 'evil' && el('tab-events').textContent.includes('<img')", 30000),
        "事件的 int 是一段 HTML 時照原樣顯示成文字");
  check(await evaluate("window.__xss === undefined && !el('tab-events').querySelector('img')"), "那段 HTML 沒有被執行");
  try { fs.rmSync(evilDir, { recursive: true, force: true }); } catch {}

  // 刪除上傳
  const groups = await evaluate("document.querySelectorAll('#files .group .rm').length");
  await evaluate("document.querySelector('#files .group .rm').click()");
  check(await waitFor(`document.querySelectorAll('#files .group .rm').length === ${groups - 1}`),
        "按 ✕ 可以刪除一次上傳");
  check(await waitFor("state.file === null && !state.info && !el('empty').hidden && el('play').disabled"),
        "刪掉的正是畫面上的上傳：畫面關掉、控制項停用");

  check(b.consoleErrors.length === 0, "console 沒有錯誤", b.consoleErrors.slice(0, 5).join(" | "));
  b.cleanup();
  console.log(failures() ? `${failures()} 項失敗` : "全部通過");
  process.exitCode = failures() ? 1 : 0;
})().catch(e => { console.error("FAIL", e); process.exit(1); });
