// 「📂 開啟」的端對端測試：只指定 .skel，圖集與貼圖從它所在的資料夾讀。
//
//   node tests/test_open.js <骨架檔路徑>      （同資料夾要有它的 .atlas 與貼圖）
//
// 會把骨架、圖集、貼圖複製到暫存資料夾再開（要改檔測自動更新，不能動到原檔），
// 「最近開啟」也寫到暫存檔（SPINEVIEW_RECENT），不碰使用者自己的清單。
// 開檔視窗（tkinter）會真的跳在桌面上，不在這裡測；它和「貼上路徑」走同一個 add()，
// 視窗本身的邏輯由 tests/test_opened.py 用假的 tkinter 測。
const fs = require("fs");
const os = require("os");
const path = require("path");
const { launch, makeChecker, sleep, PAINTED_EXPR } = require("./_browser");

const skel = process.argv[2];
if (!skel) { console.error("用法: node tests/test_open.js <骨架檔路徑>"); process.exit(2); }
const src = path.dirname(path.resolve(skel));
const base = path.basename(skel).replace(/\.(skel|json)$/i, "");
const atlasSrc = path.join(src, base + ".atlas");
const pageNames = fs.readFileSync(atlasSrc, "utf8").split(/\r?\n/).map(l => l.trim())
  .filter(l => /\.(png|jpe?g|webp)$/i.test(l));

const work = fs.mkdtempSync(path.join(os.tmpdir(), "spineview-open-"));
const folder = path.join(work, "某個 資料夾");          // 含空白與中文的路徑
fs.mkdirSync(folder);
for (const name of [path.basename(skel), base + ".atlas", ...pageNames]) {
  fs.copyFileSync(path.join(src, name), path.join(folder, name));
}
const skelCopy = path.join(folder, path.basename(skel));
process.env.SPINEVIEW_RECENT = path.join(work, "recent.json");
const { check, failures } = makeChecker();

async function openByPath(evaluate, p) {
  await evaluate(`(() => { el('search').value = ${JSON.stringify(p)};
    el('search').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); })()`);
}

(async () => {
  let b = await launch(["--upload-only"]);
  if (!b) { console.log("SKIP 找不到 Chrome／Edge"); return; }
  let { send, evaluate, waitFor } = b;
  await send("Page.navigate", { url: b.base });
  await waitFor("document.readyState === 'complete' && typeof finishOpen === 'function'");

  // 貼上 .skel 的完整路徑按 Enter
  await openByPath(evaluate, skelCopy);
  check(await waitFor("state.info && state.info.opened && state.frames.size > 0 && state.viewport", 30000),
        "只給 .skel 的路徑就能打開");
  check(await evaluate("!!state.info.atlas && state.info.pages.every(p => p.exists) && state.info.missingRegions.length === 0"),
        "圖集與貼圖從 .skel 所在的資料夾讀到", await evaluate("state.info.atlas"));
  await evaluate("setFrame(Math.floor(state.last / 2))");
  await waitFor("state.frames.has(state.frame)");
  await evaluate("redraw()");
  check(await evaluate(PAINTED_EXPR) > 50, "畫面確實畫出來");
  check(await evaluate("el('search').value === ''"), "開啟後篩選框清空");
  check(await evaluate(`[...document.querySelectorAll('#files .file')].some(r => r.title === ${JSON.stringify(skelCopy)})`),
        "「最近開啟」列表的提示是完整路徑");

  // 讀的是原檔：改檔會自動更新
  const stampBefore = await evaluate("state.info.stamp");
  const now = new Date();
  fs.utimesSync(skelCopy, now, new Date(now.getTime() + 5000));
  check(await waitFor(`state.info.stamp !== ${JSON.stringify(stampBefore)}`, 15000),
        "原檔被改動後畫面自動重新載入");

  // 連續快速改檔（像 agent 一直寫、美術重新匯出）：最後顯示的一定要是最新版本
  const atlasCopy = path.join(folder, base + ".atlas");
  for (let k = 1; k <= 6; k++) {
    const t = new Date(Date.now() + 10000 * k);
    fs.utimesSync(k % 2 ? skelCopy : atlasCopy, t, t);
    await sleep(120);
  }
  const settled = await waitFor(`(async () => {
    const stat = await fetch("/api/stat?file=" + encodeURIComponent(state.file)).then(r => r.json());
    return state.info && stat.stamp === state.info.stamp && state.frames.size > 0 && !reloading;
  })()`, 30000);
  check(settled, "連續快速改檔之後，畫面停在最新版本（網頁的版本＝檔案實際的版本）");

  // 檔案壞掉（例如用錯版本匯出）→ 顯示錯誤、控制項停用；修好後自動恢復，不用按重新整理
  const goodBytes = fs.readFileSync(skelCopy);
  const animBefore = await evaluate("state.anim");
  fs.writeFileSync(skelCopy, "not a skeleton");
  check(await waitFor("el('error').style.display === 'block' && el('errorText').textContent.includes('讀不進來') && !state.info", 20000),
        "檔案壞掉時顯示「讀不進來」");
  check(await evaluate("['play', 'timeline', 'frameNum', 'prevFrame', 'nextFrame', 'savePng', 'anim'].every(id => el(id).disabled)"),
        "讀不進來時播放、時間軸、逐格、存圖都停用");
  check(await evaluate("el('slotList').children.length === 0 && el('boneList').children.length === 0"),
        "插槽與骨骼列表清空（不留著舊檔的內容）");
  const errorsBefore = b.consoleErrors.length;
  await evaluate("(() => { el('nextFrame').click(); el('timeline').value = 3; el('timeline').dispatchEvent(new Event('input')); })()");
  await sleep(300);
  check(b.consoleErrors.length === errorsBefore, "讀不進來時亂點控制項不會出錯");
  await sleep(2500);
  const infoCalls = await evaluate(`(async () => { window.__infoCalls = 0; const f = window.fetch;
    window.fetch = (u, ...a) => { if (String(u).startsWith('/api/info')) window.__infoCalls++; return f(u, ...a); };
    await new Promise(r => setTimeout(r, 4000)); return window.__infoCalls; })()`);
  check(infoCalls === 0, "檔案沒修好之前不會一直重新載入", `4 秒內要了 ${infoCalls} 次 info`);
  fs.writeFileSync(skelCopy, goodBytes);
  const tFix = new Date(Date.now() + 20000);
  fs.utimesSync(skelCopy, tFix, tFix);
  check(await waitFor("state.info && state.frames.size > 0 && el('error').style.display === 'none'", 20000),
        "檔案修好後自動恢復（不用按重新整理）");
  check(await evaluate(`state.anim === ${JSON.stringify(animBefore)} && !el('play').disabled === (state.last > 0)`),
        "恢復後接回原本的動畫，控制項重新啟用");

  // 重新載入時檔案正被別的程式鎖住（匯出工具、防毒）：讀不到、也沒有指紋可比；鎖放開後要自己恢復
  if (process.platform === "win32") {
    const { spawn } = require("child_process");
    const locker = spawn("python", ["-c", [
      "import msvcrt, os, sys, time",
      "f = open(sys.argv[1], 'r+b')",
      "msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, os.path.getsize(sys.argv[1]))",
      "print('locked', flush=True)",
      "time.sleep(60)"].join("\n"), skelCopy], { stdio: ["ignore", "pipe", "inherit"] });
    let lockOut = "";
    locker.stdout.on("data", d => lockOut += d);
    for (let i = 0; i < 50 && !lockOut.includes("locked"); i++) await sleep(100);
    const tLock = new Date(Date.now() + 40000);
    fs.utimesSync(skelCopy, tLock, tLock);
    check(await waitFor("el('error').style.display === 'block' && !state.info && state.failed && !state.failed.stamp", 20000),
          "檔案被鎖住時顯示讀不進來（沒有指紋）", lockOut.trim());
    locker.kill();
    check(await waitFor("state.info && state.frames.size > 0 && el('error').style.display === 'none'", 15000),
          "鎖放開後自動恢復（以前要手動按重新整理）");
  }

  // 美術重新匯出時插槽／骨骼增減：隱藏與選取依名稱接回去，不會指到別的插槽或超出範圍
  const otherSkel = fs.readdirSync(src).find(n => /\.skel$/i.test(n) && n.toLowerCase() !== path.basename(skel).toLowerCase());
  if (otherSkel) {
    const before = await evaluate(`(() => {
      const i = state.info.slots.length - 1;
      state.hidden.add(i); refreshVisibility(false);
      state.selBone = state.info.bones.length - 1; markSelectedBone(); redraw();
      return { hidden: [...state.hidden].map(k => state.info.slots[k].name), bone: state.info.bones[state.selBone].name,
               stamp: state.info.stamp, slots: state.info.slots.length };
    })()`);
    const errorsAt = b.consoleErrors.length;
    fs.copyFileSync(path.join(src, otherSkel), skelCopy);
    const tSwap = new Date(Date.now() + 60000);
    fs.utimesSync(skelCopy, tSwap, tSwap);
    check(await waitFor(`state.info && state.info.stamp !== ${JSON.stringify(before.stamp)} && state.frames.size > 0`, 20000),
          "換成另一份骨架後自動重新載入", otherSkel);
    const after = await evaluate(`({ hidden: [...state.hidden].map(k => state.info.slots[k] ? state.info.slots[k].name : null),
      bone: state.selBone < 0 ? null : (state.info.bones[state.selBone] || {}).name, slots: state.info.slots.length })`);
    check(after.hidden.every(n => n !== null && before.hidden.includes(n)) && (after.bone === null || after.bone === before.bone),
          "隱藏與選取的插槽、骨骼依名稱接回去（新檔沒有的就放掉）", JSON.stringify({ before, after }));
    await sleep(500);
    check(b.consoleErrors.length === errorsAt, "插槽數變了也沒有出錯", b.consoleErrors.slice(errorsAt, errorsAt + 2).join(" | "));
    fs.writeFileSync(skelCopy, goodBytes);
    const tBack = new Date(Date.now() + 80000);
    fs.utimesSync(skelCopy, tBack, tBack);
    check(await waitFor(`state.info && state.info.slots.length === ${before.slots} && state.frames.size > 0`, 20000),
          "換回原本的骨架後恢復");
  }

  // 給同資料夾的 .atlas 也能找到同名骨架檔
  // 開的是同一份骨架，state.info.path 本來就對——要等這次開啟真的跑完（狀態列會換成骨架資訊），
  // 不然它跑完時清掉錯誤訊息，會讓下一步的檢查誤判
  await evaluate("el('status').textContent = '(等待開啟)'");
  await openByPath(evaluate, path.join(folder, base + ".atlas"));
  check(await waitFor(`el('status').textContent.includes('Spine ') && state.info.path === ${JSON.stringify(skelCopy)}`),
        "給 .atlas 路徑也會找到同名的骨架檔");

  // 錯誤路徑要講清楚
  await openByPath(evaluate, folder);
  check(await waitFor("el('error').style.display === 'block' && el('error').textContent.includes('資料夾')"),
        "給資料夾路徑時說明要選 .skel");
  await openByPath(evaluate, path.join(folder, "不存在.skel"));
  check(await waitFor("el('error').textContent.includes('找不到檔案')"), "檔案不存在時明講");
  check(b.consoleErrors.length === 0, "console 沒有錯誤", b.consoleErrors.slice(0, 5).join(" | "));
  b.cleanup();
  await sleep(800);

  // 伺服器重開後「最近開啟」還在，網址也還有效
  b = await launch(["--upload-only"]);
  ({ send, evaluate, waitFor } = b);
  await send("Page.navigate", { url: b.base });
  check(await waitFor(`[...document.querySelectorAll('#files .file')].some(r => r.title === ${JSON.stringify(skelCopy)})`),
        "伺服器重開後「最近開啟」還在");
  await evaluate(`[...document.querySelectorAll('#files .file')].find(r => r.title === ${JSON.stringify(skelCopy)}).click()`);
  check(await waitFor("state.info && state.info.opened && state.frames.size > 0", 30000), "從「最近開啟」點了就能開");
  await evaluate(`[...document.querySelectorAll('#files .file')].find(r => r.title === ${JSON.stringify(skelCopy)}).querySelector('.rm-row').click()`);
  check(await waitFor(`![...document.querySelectorAll('#files .file')].some(r => r.title === ${JSON.stringify(skelCopy)})`),
        "✕ 可以從「最近開啟」移除");
  check(await waitFor("state.file === null && !state.info && !el('empty').hidden && el('play').disabled"),
        "移除的正是畫面上的檔案：畫面關掉（伺服器已經找不到它，留著只會讓之後的操作都失敗）");
  check(fs.existsSync(skelCopy), "移除只是從清單拿掉，檔案還在");
  check(b.consoleErrors.length === 0, "console 沒有錯誤（第二次）", b.consoleErrors.slice(0, 5).join(" | "));
  b.cleanup();
  setTimeout(() => { try { fs.rmSync(work, { recursive: true, force: true }); } catch {} }, 800);
  console.log(failures() ? `${failures()} 項失敗` : "全部通過");
  process.exitCode = failures() ? 1 : 0;
})().catch(e => { console.error("FAIL", e); process.exit(1); });
