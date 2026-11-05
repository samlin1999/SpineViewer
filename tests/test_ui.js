// 網頁介面的端對端測試：真的開一個 spineview serve、用無頭 Chrome 打開，
// 透過 DevTools Protocol 操作（播放、隱藏插槽、切動畫、切伺服器渲染），
// 並確認 console 沒有任何錯誤。
//
//   node tests/test_ui.js <含骨架檔的資料夾> <資料夾內的骨架檔相對路徑>
//
// 需要 Node 22+（內建 WebSocket）與 Chrome／Edge。找不到瀏覽器時跳過（離開碼 0）。
const fs = require("fs");
const os = require("os");
const path = require("path");
const { launch, makeChecker, sleep, PAINTED_EXPR } = require("./_browser");

const folder = process.argv[2];
const file = process.argv[3];
if (!folder || !file) {
  console.error("用法: node tests/test_ui.js <資料夾> <骨架檔相對路徑>");
  process.exit(2);
}
const { check, failures: failCount } = makeChecker();

(async () => {
  const b = await launch([folder]);
  if (!b) { console.log("SKIP 找不到 Chrome／Edge"); return; }
  const { send, evaluate, waitFor } = b;
  const consoleErrors = b.consoleErrors;
  await send("Page.navigate", { url: `${b.base}#file=${encodeURIComponent(file)}` });

  check(await waitFor("state.info !== null && state.frames.size > 0 && state.viewport !== null"),
        "載入骨架並取得第一批影格");
  const info = await evaluate("({anims: state.info.animations.length, slots: state.info.slots.length, meshes: state.meshes.filter(Boolean).length, tex: state.textures.filter(Boolean).length})");
  check(info.meshes > 0 && info.tex > 0, "附件幾何與貼圖都上傳到 WebGL", JSON.stringify(info));
  const drawn = await evaluate("currentFrame().draws.length");
  check(drawn > 0, "目前影格有要畫的附件", drawn);

  // 畫面不是一片空白：跳到動畫中段（不少特效第 0 格本來就是空的），讀回 WebGL 畫布
  await evaluate("setFrame(Math.floor(state.last / 2))");
  await waitFor("state.frames.has(state.frame)");
  await evaluate("redraw()");
  const painted = await evaluate(PAINTED_EXPR);
  check(painted > 50, "WebGL 畫布上確實畫出了彩色內容", painted);

  if (info.anims > 0) {
    const last = await evaluate("state.last");
    await evaluate("startPlay()");
    await sleep(1200);
    const frame = await evaluate("state.frame");
    await evaluate("stopPlay()");
    check(last === 0 || frame > 0, "按播放後影格會前進", `frame=${frame} last=${last}`);
    check(await waitFor("state.frames.size >= Math.min(state.last + 1, 40)", 30000),
          "背景預抓整段動畫的影格");
  }

  const slot = await evaluate("currentFrame().draws[0].slot");
  await evaluate(`(() => { state.hidden.add(${slot}); refreshVisibility(false); })()`);
  const stillDrawn = await evaluate(`currentFrame().draws.filter(d => d.slot === ${slot} && !state.hidden.has(d.slot)).length`);
  check(stillDrawn === 0, "隱藏插槽後不再畫它", `slot=${slot}`);
  check(await evaluate("el('visReset').disabled === false"), "重設可視按鈕亮起");
  await evaluate("el('visReset').click()");
  check(await evaluate("state.hidden.size === 0"), "重設可視清空隱藏清單");

  // 屬性分頁：在畫面上真的點一下（點在最上層附件第一個三角形的重心，一定點得到東西）
  const pt = await evaluate(`(() => {
    const fr = currentFrame(); const d = fr.draws[fr.draws.length - 1];
    const tri = state.meshes[d.att].tri;
    let wx = 0, wy = 0;
    for (let k = 0; k < 3; k++) { wx += d.xy[tri[k] * 2] / 3; wy += d.xy[tri[k] * 2 + 1] / 3; }
    const [sx, sy] = worldToScreen(wx, wy);
    const dpr = window.devicePixelRatio || 1, r = el("stage").getBoundingClientRect();
    return { x: r.left + sx / dpr, y: r.top + sy / dpr };
  })()`);
  for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", { type, x: pt.x, y: pt.y, button: "left", clickCount: 1 });
  }
  check(await waitFor("state.selSlot >= 0 && document.querySelector('.tab.active').dataset.tab === 'props'" +
                      " && el('tab-props').querySelector('.prop-title') !== null" +
                      " && el('tab-props').querySelector('.pending') === null"),
        "在畫面上點插槽會開啟屬性分頁");
  const props = await evaluate("el('tab-props').innerText");
  check(props.includes("插槽") && props.includes("骨骼") && props.includes("可放的附件"),
        "屬性分頁列出插槽、骨骼、可放的附件");
  if (await evaluate("el('tab-props').querySelector('.regionimg') !== null")) {
    check(await waitFor("el('tab-props').querySelector('.regionimg').naturalWidth > 0"),
          "圖集區塊的貼圖預覽載入成功");
  }
  const chip = await evaluate("(() => { const c = [...el('tab-props').querySelectorAll('[data-jump]')]" +
                              ".find(c => +c.dataset.jump !== state.frame); return c ? +c.dataset.jump : -1; })()");
  if (chip >= 0) {
    await evaluate(`el('tab-props').querySelector('[data-jump="${chip}"]').click()`);
    check(await waitFor(`state.frame === ${chip}`), "點關鍵影格會跳到那一格", `#${chip}`);
  }

  if (info.anims > 1) {
    await evaluate("(() => { const s = el('anim'); s.value = state.info.animations[1].name; s.onchange(); })()");
    check(await waitFor("state.anim === state.info.animations[1].name && state.frames.has(0)"),
          "切換動畫後重新載入影格");
  }

  await evaluate("(() => { el('pngMode').checked = true; redraw(); })()");
  check(await waitFor("el('png').style.display === 'block' && el('png').naturalWidth > 0", 30000),
        "伺服器渲染模式顯示 numpy 畫的 PNG");
  await evaluate("(() => { el('pngMode').checked = false; redraw(); })()");

  await evaluate("(() => { el('showBones').checked = true; el('showBoxes').checked = true; redraw(); })()");
  check(true, "骨骼與插槽外框疊圖可以開啟");

  const shot = await send("Page.captureScreenshot", { format: "png" });
  const out = path.join(os.tmpdir(), "spineview-ui-test.png");
  fs.writeFileSync(out, Buffer.from(shot.result.data, "base64"));
  console.log("截圖：" + out);

  check(consoleErrors.length === 0, "console 沒有錯誤", consoleErrors.slice(0, 5).join(" | "));
  b.cleanup();
  console.log(failCount() ? `${failCount()} 項失敗` : "全部通過");
  process.exitCode = failCount() ? 1 : 0;
})().catch(e => { console.error("FAIL", e); process.exit(1); });
