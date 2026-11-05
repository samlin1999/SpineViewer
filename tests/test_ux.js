// 介面操作的端對端測試：快捷鍵、焦點、篩選、只看單一插槽、側欄寬度、記住設定、存成 PNG。
// 全部用真的滑鼠／鍵盤事件（DevTools Protocol 的 Input.*）操作，不直接呼叫頁面裡的函式。
//
//   node tests/test_ux.js <含骨架檔的資料夾> <資料夾內的骨架檔相對路徑>
//
// 資料夾裡最好有兩個以上的骨架檔（測 ↑↓ 切換檔案）；骨架有兩段以上動畫時多測 [ ] 切換。
const fs = require("fs");
const os = require("os");
const path = require("path");
const { launch, makeChecker, sleep } = require("./_browser");

const folder = process.argv[2];
const file = process.argv[3];
if (!folder || !file) { console.error("用法: node tests/test_ux.js <資料夾> <骨架檔相對路徑>"); process.exit(2); }
const { check, failures } = makeChecker();

const KEYS = {
  ArrowLeft: 37, ArrowRight: 39, ArrowUp: 38, ArrowDown: 40, Home: 36, End: 35, Escape: 27, Space: 32,
  BracketLeft: 219, BracketRight: 221, KeyB: 66, KeyF: 70, Digit1: 49, Slash: 191,
};

(async () => {
  const b = await launch([folder]);
  if (!b) { console.log("SKIP 找不到 Chrome／Edge"); return; }
  const { send, evaluate, waitFor } = b;
  const MOD = { alt: 1, ctrl: 2, shift: 8 };
  const key = async (code, { key: k, shift = false, text } = {}) => {
    const name = k || (code.startsWith("Key") ? code.slice(3).toLowerCase() : code.startsWith("Digit") ? code.slice(5) : code);
    const base = { code, key: name, windowsVirtualKeyCode: KEYS[code], modifiers: shift ? MOD.shift : 0 };
    await send("Input.dispatchKeyEvent", { type: "keyDown", ...base, ...(text ? { text } : {}) });
    await send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
    await sleep(60);
  };
  const center = (sel) => evaluate(`(() => { const n = ${sel}; n.scrollIntoView({ block: "nearest" });
    const r = n.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })()`);
  const click = async (sel, { modifiers = 0, clickCount = 1 } = {}) => {
    const p = await center(sel);
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: p.x, y: p.y });
    for (let c = 1; c <= clickCount; c++) {
      await send("Input.dispatchMouseEvent", { type: "mousePressed", x: p.x, y: p.y, button: "left", clickCount: c, modifiers });
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: p.x, y: p.y, button: "left", clickCount: c, modifiers });
    }
    await sleep(80);
  };
  const loaded = "state.info && !state.loadingName && state.frames.has(state.frame) && state.viewport";

  await send("Page.navigate", { url: `${b.base}#file=${encodeURIComponent(file)}` });
  check(await waitFor(loaded, 30000), "載入骨架");
  check(await evaluate("!el('stage').classList.contains('loading') && !el('badge').textContent.includes('載入中')"),
        "載入完成後不再顯示「載入中」");

  // ---------------------------------------------------------------- 逐格與跳格
  await evaluate("(() => { stopPlay(); setFrame(Math.min(5, state.last)); })()");
  const f0 = await evaluate("state.frame"), last = await evaluate("state.last");
  if (last >= 20) {
    await key("ArrowRight");
    check(await evaluate(`state.frame === ${f0 + 1}`), "→ 下一格");
    await key("ArrowRight", { shift: true });
    check(await evaluate(`state.frame === ${f0 + 11}`), "Shift+→ 一次 10 格");
    await key("ArrowLeft");
    check(await evaluate(`state.frame === ${f0 + 10}`), "← 上一格");
    await click("el('nextFrame')");
    check(await evaluate(`state.frame === ${f0 + 11}`), "› 按鈕下一格");
    await click("el('prevFrame')");
    check(await evaluate(`state.frame === ${f0 + 10}`), "‹ 按鈕上一格");
    await key("End");
    check(await evaluate("state.frame === state.last"), "End 跳到最後一格");
    await key("Home");
    check(await evaluate("state.frame === 0"), "Home 跳到第一格");
    // 停在最後一格按播放：從第 0 格開始，第 0 格要真的畫出來（以前只改內部格數，畫面直接跳到第 1 格）
    await key("End");
    await evaluate("startPlay()");
    check(await evaluate("state.frame === 0 && +el('timeline').value === 0"), "停在最後一格按播放：從第 0 格開始");
    await sleep(400);
    await evaluate("stopPlay()");
    check(await evaluate("state.frame === 0 || new URLSearchParams(location.hash.slice(1)).get('frame') === String(state.frame)"),
          "暫停後網址記下目前這一格（分享的連結才會是同一格）");
  }

  // ---------------------------------------------------------------- 焦點：用滑鼠操作過的控制項不搶快捷鍵
  await click("el('play')");
  check(await evaluate("state.playing === true"), "滑鼠點播放開始播放");
  await key("Space", { key: " ", text: " " });
  check(await evaluate("state.playing === false"), "接著按空白鍵只暫停一次（不會又被按鈕吃掉）");
  // 用滑鼠選過下拉選單後：→ 要逐格，不能改掉選單的值（以前會直接把速度、動畫換掉）
  await evaluate("(() => { stopPlay(); setFrame(1); })()");
  // 下拉選單的選項是瀏覽器自己的視窗，CDP 點不到；直接送出「選好了」的 change（前一個按鍵不在選單上＝滑鼠選的）
  await evaluate("(() => { const s = el('speed'); s.value = '0.5'; s.dispatchEvent(new Event('change', { bubbles: true })); })()");
  check(await evaluate("document.activeElement !== el('speed')"), "用滑鼠選完下拉選單後焦點離開選單");
  await key("ArrowRight");
  check(await evaluate("el('speed').value === '0.5' && state.frame === 2"), "接著按 → 是逐格，選單的值沒有被改掉",
        await evaluate("JSON.stringify({ speed: el('speed').value, frame: state.frame })"));
  // 時間軸：用滑鼠點過之後按 ↑↓ 不該被滑桿當成改格數
  await click("el('timeline')");
  check(await evaluate("document.activeElement !== el('timeline')"), "用滑鼠點過時間軸後焦點離開滑桿");
  // 用鍵盤（Tab）移到下拉選單時保留焦點，方向鍵照常改選單
  await evaluate("el('speed').focus()");
  await key("ArrowDown");
  check(await evaluate("document.activeElement === el('speed')"), "用鍵盤操作下拉選單時焦點保留");
  await evaluate("(() => { el('speed').value = '1'; el('speed').dispatchEvent(new Event('change', { bubbles: true })); el('speed').blur(); })()");

  // ---------------------------------------------------------------- 縮放、骨骼、說明
  await key("Digit1");
  check(await evaluate("Math.abs(state.cam.scale - 1) < 1e-9 && el('zoom').value === '1'"), "1 縮放到 100%");
  await key("KeyF");
  check(await evaluate("state.cam.fit === true && el('zoom').value === 'fit'"), "F 縮放到適合");
  const bones0 = await evaluate("el('showBones').checked");
  await key("KeyB");
  check(await evaluate(`el('showBones').checked === ${!bones0}`), "B 開關骨骼");
  await key("KeyB");
  await key("Slash", { key: "?", shift: true, text: "?" });
  check(await evaluate("!el('help').hidden"), "? 打開快捷鍵說明");
  await key("Escape");
  check(await evaluate("el('help').hidden"), "Esc 關掉說明");
  await click("el('helpBtn')");
  check(await evaluate("!el('help').hidden"), "「⌨ 快捷鍵」按鈕打開說明");
  await click("el('stage')");
  check(await evaluate("el('help').hidden"), "點說明以外的地方會關掉");

  // ---------------------------------------------------------------- 錯誤訊息可以關
  // 錯誤訊息框在畫面裡：點它（選文字、按 ✕）不能同時變成「點畫面」——以前會把目前的選取清掉或選到底下的插槽
  const someSlot = await evaluate("currentFrame().draws[0].slot");
  await evaluate(`(() => { selectSlot(${someSlot}, false); showError('測試訊息：點這段文字不能變成點畫面'); })()`);
  const cam0 = await evaluate("JSON.stringify(state.cam)");
  await click("el('errorText')");
  check(await evaluate(`state.selSlot === ${someSlot} && JSON.stringify(state.cam) === ${JSON.stringify(cam0)}`),
        "點錯誤訊息框不會變成點畫面（選取、鏡頭都不變）");
  await click("el('errorClose')");
  check(await evaluate(`state.selSlot === ${someSlot}`), "按錯誤訊息的 ✕ 也不會清掉選取");
  await evaluate("selectSlot(-1, false)");
  await evaluate("showError('測試訊息')");
  await click("el('errorClose')");
  check(await evaluate("el('error').style.display === 'none'"), "錯誤訊息的 ✕ 可以關掉");
  await evaluate("showError('測試訊息')");
  await key("Escape");
  check(await evaluate("el('error').style.display === 'none'"), "Esc 也能關掉錯誤訊息");

  // ---------------------------------------------------------------- 伺服器渲染：同一格「開 → 關 → 開」
  const setPng = (on) => evaluate(`(() => { el('pngMode').checked = ${on}; el('pngMode').onchange(); })()`);
  await setPng(true);
  check(await waitFor("el('png').style.display === 'block' && el('png').naturalWidth > 0", 30000), "打開伺服器渲染顯示 PNG");
  await setPng(false);
  check(await evaluate("el('png').style.display === 'none'"), "關掉後 PNG 藏起來");
  await setPng(true);
  check(await waitFor("el('png').style.display === 'block'", 3000), "同一格再打開：PNG 馬上顯示（以前會一直空白）");
  await setPng(false);

  // ---------------------------------------------------------------- 循環播放的接縫
  // 引擎在動畫長度處繞回 0，「最後一格」（時間＝長度）不會出現；以前每圈多播這一格，接縫處同一個姿勢停兩格
  if (last >= 20) {
    check(await waitFor("state.frames.size === state.last + 1", 60000), "整段影格載入完（循環測試用）");
    await evaluate(`(() => { window.__seen = []; window.__origSetFrame = setFrame;
      setFrame = (f) => { window.__seen.push(Math.round(f)); return window.__origSetFrame(f); };
      el('loop').checked = true; el('speed').value = '0.25'; window.__origSetFrame(state.last - 3); startPlay(); })()`);
    const wrapped = await waitFor("window.__seen.some((f, i) => i > 0 && f < 3)", 6000);
    await evaluate("(() => { stopPlay(); setFrame = window.__origSetFrame; el('speed').value = '1'; })()");
    const seen = await evaluate("window.__seen");
    check(wrapped && !seen.includes(last), "循環播放時最後一格不會出現（照引擎在動畫長度處繞回）", JSON.stringify(seen.slice(0, 12)));
  }

  // ---------------------------------------------------------------- 插槽篩選
  await evaluate("(() => { stopPlay(); setFrame(Math.floor(state.last / 2)); })()");
  await waitFor("state.frames.has(state.frame)");
  await click("document.querySelector('.tab[data-tab=\"slots\"]')");
  const slotName = await evaluate("state.info.slots[currentFrame().draws[currentFrame().draws.length - 1].slot].name");
  await click("el('slotFilter')");
  await send("Input.insertText", { text: slotName });
  await sleep(100);
  const rows = await evaluate("[...el('slotList').querySelectorAll('[data-slot]')].map(r => r.textContent.toLowerCase())");
  check(rows.length > 0 && rows.every(t => t.includes(slotName.toLowerCase())), "插槽篩選只列出名稱或附件符合的",
        `「${slotName}」→ ${rows.length} 列`);
  await key("Space", { key: " ", text: " " });
  check(await evaluate("state.playing === false && el('slotFilter').value.endsWith(' ')"),
        "在篩選框裡打空白是打字，不會開始播放");
  await key("Escape");
  check(await evaluate("el('slotFilter').value === '' && document.activeElement === el('slotFilter')"), "篩選框按 Esc 先清空");
  await key("Escape");
  check(await evaluate("document.activeElement !== el('slotFilter')"), "再按一次 Esc 離開篩選框");
  await evaluate("(() => { const f = el('slotFilter'); f.value = 'zz_沒有這個名字_zz'; f.dispatchEvent(new Event('input')); })()");
  check(await evaluate("el('slotList').textContent.includes('沒有插槽')"), "篩不到東西時講清楚");
  await evaluate("(() => { const f = el('slotFilter'); f.value = ''; f.dispatchEvent(new Event('input')); })()");

  // ---------------------------------------------------------------- 只看單一插槽（Alt+點眼睛）
  const solo = await evaluate("currentFrame().draws[currentFrame().draws.length - 1].slot");
  await click(`el('slotList').querySelector('[data-eye="${solo}"]')`, { modifiers: MOD.alt });
  const s1 = await evaluate(`({ hidden: state.hidden.size, has: state.hidden.has(${solo}), slots: state.info.slots.length,
    clips: state.info.slots.filter(s => s.clip).length,
    others: currentFrame().draws.filter(d => !state.hidden.has(d.slot) && d.slot !== ${solo}).length })`);
  check(!s1.has && s1.hidden === s1.slots - 1 - s1.clips && s1.others === 0,
        "Alt+點眼睛：只畫這個插槽（clipping 遮罩不藏）", JSON.stringify(s1));
  check(await evaluate("el('visReset').disabled === false"), "只看單一插槽時「重設可視」亮起");
  await click(`el('slotList').querySelector('[data-eye="${solo}"]')`, { modifiers: MOD.alt });
  check(await evaluate("state.hidden.size === 0"), "再 Alt+點一次恢復全部顯示");
  // 屬性分頁的「只看這個」：先選另一個插槽、等它的屬性顯示出來，再換選 solo
  const other = await evaluate(`currentFrame().draws.map(d => d.slot).find(s => s !== ${solo})`);
  await evaluate(`selectSlot(${other}, true)`);
  await waitFor(`el('tab-props').querySelector('[data-solo="${other}"]') !== null`);
  await evaluate(`selectSlot(${solo}, true)`);
  // 換選取的插槽時，回應回來之前屬性分頁不能還留著上一個插槽的連結（以前會點到舊插槽的「只看這個」）
  check(await evaluate(`!el('tab-props').querySelector('[data-solo]:not([data-solo="${solo}"])')`),
        "換選取後，屬性分頁不會留著上一個插槽的連結");
  check(await waitFor(`el('tab-props').querySelector('[data-solo="${solo}"]') !== null`), "屬性分頁有「只看這個」");
  await click("el('tab-props').querySelector('[data-solo]')");
  check(await evaluate(`state.hidden.size > 0 && !state.hidden.has(${solo}) && el('tab-props').querySelector('[data-solo]').textContent === '全部顯示'`),
        "按「只看這個」後連結變成「全部顯示」");
  await click("el('tab-props').querySelector('[data-solo]')");
  check(await evaluate("state.hidden.size === 0"), "按「全部顯示」恢復");
  await key("Escape");
  check(await evaluate("state.selSlot === -1"), "Esc 取消選取插槽");

  // ---------------------------------------------------------------- 骨骼篩選
  await click("document.querySelector('.tab[data-tab=\"bones\"]')");
  const boneName = await evaluate("state.info.bones[state.info.bones.length - 1].name");
  await evaluate(`(() => { const f = el('boneFilter'); f.value = ${JSON.stringify(boneName)}; f.dispatchEvent(new Event('input')); })()`);
  const bones = await evaluate("[...el('boneList').querySelectorAll('[data-bone]')].map(r => r.querySelector('.nm').textContent.toLowerCase())");
  check(bones.length > 0 && bones.every(t => t.includes(boneName.toLowerCase())), "骨骼篩選只列出名稱符合的", `${bones.length} 列`);
  await click("el('boneList').querySelector('[data-bone]')");
  check(await evaluate("state.selBone >= 0"), "篩選後點骨骼可以選取");
  await evaluate("(() => { const f = el('boneFilter'); f.value = ''; f.dispatchEvent(new Event('input')); })()");
  check(await evaluate(`el('boneList').querySelectorAll('[data-bone]').length === state.info.bones.length`), "清空篩選後回到完整的骨骼樹");

  // ---------------------------------------------------------------- 時間軸的已載入條
  if (last > 0) {
    check(await waitFor("state.frames.size === state.last + 1", 60000), "整段動畫的影格都載入了");
    await evaluate("redraw()");
    const colors = await evaluate("[...new Set(el('buffer').style.background.match(/rgba?\\([^)]*\\)/g) || [])]");
    check(colors.length === 1, "全部載入後時間軸下方是一整條「已載入」", JSON.stringify(colors));
  }

  // ---------------------------------------------------------------- 側欄寬度
  const w0 = await evaluate("({ right: state.rightW, canvas: el('gl').clientWidth })");
  const h = await center("el('rightResizer')");
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: h.x, y: h.y });
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: h.x, y: h.y, button: "left", clickCount: 1 });
  for (let k = 1; k <= 5; k++) await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: h.x - k * 20, y: h.y, button: "left" });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: h.x - 100, y: h.y, button: "left", clickCount: 1 });
  await sleep(100);
  const w1 = await evaluate("({ right: state.rightW, canvas: el('gl').clientWidth, saved: JSON.parse(localStorage.getItem('spineview.prefs')).rightW })");
  check(w1.right === w0.right + 100 && w1.canvas === w0.canvas - 100, "拖曳右側欄邊緣調整寬度，畫面跟著縮", JSON.stringify([w0, w1]));
  check(w1.saved === w1.right, "寬度記下來了");
  await click("el('rightResizer')", { clickCount: 2 });
  check(await evaluate("state.rightW === 320"), "雙擊邊緣恢復預設寬度");

  // ---------------------------------------------------------------- 記住設定（重新整理頁面後還在）
  await evaluate("(() => { el('bg').value = '#ffffff'; el('bg').dispatchEvent(new Event('change', { bubbles: true })); })()");
  await evaluate("(() => { el('loop').click(); })()");
  const loopNow = await evaluate("el('loop').checked");
  await send("Page.reload");
  check(await waitFor(loaded, 30000), "重新整理頁面後重新載入");
  check(await evaluate(`el('bg').value === '#ffffff' && el('loop').checked === ${loopNow}`), "背景與循環的設定保留下來");
  await evaluate("(() => { el('bg').value = 'checker'; el('bg').onchange(); if (!el('loop').checked) el('loop').click(); })()");

  // ---------------------------------------------------------------- 存成 PNG
  const dl = fs.mkdtempSync(path.join(os.tmpdir(), "spineview-dl-"));
  const allowed = await send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dl });
  if (allowed.error) await send("Page.setDownloadBehavior", { behavior: "allow", downloadPath: dl });
  // 檔名要對應「按下存圖時」的那一格：讓伺服器回應慢一秒，期間按 → 換格數（大檔算圖本來就要好幾秒）
  const expected = (await evaluate(`\`\${state.info.name}_\${state.anim || "setup"}_\${String(state.frame).padStart(3, "0")}.png\``))
    .replace(/[\\/:*?"<>|]+/g, "_");
  await evaluate(`(() => { const f = window.fetch; window.fetch = async (u, ...a) => {
    if (String(u).startsWith('/api/frame')) await new Promise(r => setTimeout(r, 1000)); return f(u, ...a); }; })()`);
  await click("el('savePng')");
  await key("ArrowRight");
  let png = null;
  for (let i = 0; i < 100 && !png; i++) {
    png = fs.readdirSync(dl).find(n => n.endsWith(".png"));
    if (!png) await sleep(200);
  }
  check(!!png, "「存成 PNG」下載了檔案", png || "");
  if (png) {
    await sleep(300);
    const buf = fs.readFileSync(path.join(dl, png));
    const ok = buf.slice(1, 4).toString() === "PNG";
    const width = buf.readUInt32BE(16), height = buf.readUInt32BE(20), colorType = buf[25];
    check(ok && width > 0 && height > 0, "下載的是 PNG", `${width}×${height}`);
    check(colorType === 6, "PNG 有 alpha（透明背景）");
    check(png === expected, "檔名是按下存圖時的 骨架_動畫_格數.png（算圖期間換了格數也一樣）", `${png} / 預期 ${expected}`);
  }
  try { fs.rmSync(dl, { recursive: true, force: true }); } catch {}

  // ---------------------------------------------------------------- [ ] 切換動畫
  if (await evaluate("el('anim').options.length > 2")) {
    const a0 = await evaluate("el('anim').selectedIndex"), n = await evaluate("el('anim').options.length");
    await key("BracketRight", { key: "]" });
    check(await waitFor(`el('anim').selectedIndex === ${(a0 + 1) % n} && state.anim === el('anim').value`) &&
          await waitFor(loaded), "] 下一段動畫");
    await key("BracketLeft", { key: "[" });
    check(await waitFor(`el('anim').selectedIndex === ${a0} && state.anim === el('anim').value`), "[ 回到上一段動畫");
    await evaluate("startPlay()");
    await key("BracketRight", { key: "]" });
    check(await evaluate("state.playing === true"), "播放中按 ] 換動畫會接著播");
    await evaluate("stopPlay()");
    await key("BracketLeft", { key: "[" });
  }

  // ---------------------------------------------------------------- ↑↓ 切換檔案
  const nav = await evaluate(`(() => {
    const rows = [...document.querySelectorAll('#files .file')].filter(r => r.style.display !== 'none');
    const i = rows.findIndex(r => r.dataset.path === state.file);
    return { count: rows.length, i, next: rows[i + 1] ? rows[i + 1].dataset.path : null, prev: rows[i - 1] ? rows[i - 1].dataset.path : null };
  })()`);
  if (nav.count > 1) {
    const original = await evaluate("state.file");
    const [down, up, target] = nav.next ? ["ArrowDown", "ArrowUp", nav.next] : ["ArrowUp", "ArrowDown", nav.prev];
    await key(down);
    check(await waitFor(`state.file === ${JSON.stringify(target)} && state.info && state.info.file === state.file && ${loaded}`, 30000),
          `${down === "ArrowDown" ? "↓" : "↑"} 切到列表裡的下一個檔案`, target);
    await key(up);
    check(await waitFor(`state.file === ${JSON.stringify(original)} && state.info.file === state.file && ${loaded}`, 30000),
          "再按反方向回到原本的檔案");
    // 檔案篩選框裡按 ↑↓ 也切換檔案（打字篩選後可以直接一個個看），焦點留在框裡
    await click("el('search')");
    await key(down);
    check(await waitFor(`state.file === ${JSON.stringify(target)} && ${loaded}`, 30000), "在檔案篩選框裡按 ↑↓ 也會切換檔案");
    check(await evaluate("document.activeElement === el('search')"), "切換後焦點留在篩選框（可以繼續打字）");
    await evaluate("el('search').blur()");
    await key(up);
    await waitFor(`state.file === ${JSON.stringify(original)} && ${loaded}`, 30000);
    // 連按好幾下：只載入停下來的那一個
    const counter = await evaluate(`(() => { window.__infoCount = 0; const f = window.fetch;
      window.fetch = (u, ...a) => { if (String(u).startsWith('/api/info')) window.__infoCount++; return f(u, ...a); }; return true; })()`);
    for (let k = 0; k < 3; k++) await key(nav.next ? "ArrowDown" : "ArrowUp");
    await sleep(600);
    await waitFor(loaded, 30000);
    const count = await evaluate("window.__infoCount");
    check(counter && count === 1, "連按方向鍵只載入最後停下來的檔案", `讀了 ${count} 次 info`);

    // 按了方向鍵、還沒載入（160ms 內）就直接點另一個檔案：以點的為準，方向鍵那個不能事後蓋過去
    await evaluate("selectFile(" + JSON.stringify(original) + ")");
    await waitFor(`state.info && state.info.file === ${JSON.stringify(original)} && ${loaded}`, 30000);
    // 先算好要點的位置，按鍵後馬上點，確定落在方向鍵的 160ms 等待裡
    const other = await evaluate(`(() => { const r = [...document.querySelectorAll('#files .file')].find(r =>
      r.dataset.path !== ${JSON.stringify(original)} && r.dataset.path !== ${JSON.stringify(target)} && r.style.display !== 'none');
      if (!r) return null; r.scrollIntoView({ block: "nearest" }); const b = r.getBoundingClientRect();
      return { path: r.dataset.path, x: b.left + b.width / 2, y: b.top + b.height / 2 }; })()`);
    if (other) {
      const t0 = Date.now();
      await send("Input.dispatchKeyEvent", { type: "keyDown", code: down, key: down, windowsVirtualKeyCode: KEYS[down] });
      await send("Input.dispatchKeyEvent", { type: "keyUp", code: down, key: down, windowsVirtualKeyCode: KEYS[down] });
      await send("Input.dispatchMouseEvent", { type: "mousePressed", x: other.x, y: other.y, button: "left", clickCount: 1 });
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: other.x, y: other.y, button: "left", clickCount: 1 });
      const elapsed = Date.now() - t0;
      await sleep(500);
      check(await evaluate(`state.file === ${JSON.stringify(other.path)}`),
            "方向鍵還沒載入就點了別的檔案：以點的為準", `${other.path}（按鍵到點擊 ${elapsed}ms）`);
    }
    await waitFor(loaded, 30000);

    // 換到載入很慢的檔案（大檔）：自動更新不能把「正在換檔」誤判成「檔案被改了」而重新載入一次
    await evaluate(`(() => { window.__infoCount = 0; const f = window.fetch;
      window.fetch = async (u, ...a) => { u = String(u);
        if (u.startsWith('/api/meshes') && u.includes(encodeURIComponent(${JSON.stringify(original)}))) await new Promise(r => setTimeout(r, 3500));
        return f(u, ...a); }; })()`);
    const animBefore = await evaluate("state.anim");
    // 不等它載完（evaluate 會等 promise）：要在換檔途中檢查
    await evaluate("(() => { selectFile(" + JSON.stringify(original) + "); })()");
    await sleep(500);
    // 換檔途中：畫面、動畫、格數都還是上一個檔的，這時的操作會送出「新檔＋舊檔的動畫」這種混搭請求
    check(await evaluate("['play', 'anim', 'skin', 'timeline', 'savePng'].every(id => el(id).disabled)"),
          "換檔途中播放、動畫、時間軸、存圖都停用");
    await key("BracketRight", { key: "]" });
    await key("ArrowRight");
    await key("Space", { key: " ", text: " " });
    check(await evaluate(`state.anim === ${JSON.stringify(animBefore)} && !state.playing`),
          "換檔途中按 ] → 空白鍵不會動到舊檔的動畫與播放");
    check(await waitFor(`state.info && state.info.file === ${JSON.stringify(original)} && ${loaded}`, 30000), "載入慢的檔案最後有載入");
    await sleep(300);
    const slow = await evaluate("window.__infoCount");
    check(slow === 1, "換到載入慢的檔案時只載入一次（自動更新與途中的按鍵都沒有插手）", `讀了 ${slow} 次 info`);
  } else {
    console.log("SKIP  資料夾裡只有一個骨架檔，不測 ↑↓ 切換");
  }

  check(b.consoleErrors.length === 0, "console 沒有錯誤", b.consoleErrors.slice(0, 5).join(" | "));
  b.cleanup();
  console.log(failures() ? `${failures()} 項失敗` : "全部通過");
  process.exitCode = failures() ? 1 : 0;
})().catch(e => { console.error("FAIL", e); process.exit(1); });
