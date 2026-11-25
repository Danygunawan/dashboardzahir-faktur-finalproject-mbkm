// Browser QA lokal dengan Chrome yang sudah terpasang; tidak mengunggah file/OCR.
import {spawn} from 'node:child_process';
import {existsSync, mkdirSync, readFileSync, writeFileSync, statSync, unlinkSync} from 'node:fs';
import {join, resolve, delimiter} from 'node:path';
import assert from 'node:assert/strict';

const [url, outputArg, job] = process.argv.slice(2);
const output = resolve(outputArg);
const profile = join(output, 'chrome_profile');
const downloads = join(output, 'browser_downloads');
mkdirSync(downloads, {recursive: true});
for(const name of process.env.QA_RESTORE_ONLY ? [] : ['hasil_faktur.xlsx','hasil_faktur_csv.zip','data_faktur.xlsx','data_faktur_csv.zip']) {
  const file=join(downloads,name);if(existsSync(file))unlinkSync(file);
}
mkdirSync(profile, {recursive: true});
const portFile = join(profile, 'DevToolsActivePort');
if (existsSync(portFile)) unlinkSync(portFile);
const candidates=[process.env.CHROME_BIN,'C:/Program Files/Google/Chrome/Application/chrome.exe','C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',...(process.env.PATH||'').split(delimiter).flatMap(dir=>['google-chrome','google-chrome-stable','chromium','chromium-browser'].map(name=>join(dir,name)))].filter(Boolean);
const chrome=candidates.find(path=>existsSync(path));
if(!chrome)throw new Error('Chrome/Chromium tidak ditemukan; atur CHROME_BIN.');
const browser = spawn(chrome, [
  '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
  '--no-first-run', '--no-default-browser-check', 'about:blank',
], {windowsHide: true, stdio: 'ignore'});
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
async function until(action, label) {
  for (let n = 0; n < 150; n++) {
    const value = await action();
    if (value) return value;
    await sleep(100);
  }
  throw new Error(`Timeout: ${label}`);
}
let socket;
let nextId = 0;
const pending = new Map();
async function send(method, params = {}) {
  const id = ++nextId;
  const response = new Promise((resolve, reject) => pending.set(id, {resolve, reject}));
  socket.send(JSON.stringify({id, method, params}));
  return response;
}
async function evaluate(expression) {
  const reply = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  if (reply.exceptionDetails) throw new Error(JSON.stringify(reply.exceptionDetails));
  return reply.result?.value;
}
async function capture(name,width=1280,mobile=false) {
  await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile});
  await evaluate('window.scrollTo(0,0)');
  const metrics=await send('Page.getLayoutMetrics');
  const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:{x:0,y:0,width,height:metrics.cssContentSize.height,scale:1}});
  writeFileSync(join(output,name),Buffer.from(shot.data,'base64'));
}
const errors = [];
try {
  await until(() => existsSync(portFile), 'browser startup');
  const port = readFileSync(portFile, 'utf8').split('\n')[0];
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = pages.find(x => x.type === 'page');
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => {socket.onopen = res; socket.onerror = rej;});
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
    if (!message.id) return;
    const entry = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(JSON.stringify(message.error)));
    else entry.resolve(message.result);
  };
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
  await send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads});
  await send('Page.navigate',{url});
  await until(()=>evaluate('Boolean(document.getElementById("screen-select"))'),'page ready');
  if(process.env.QA_RESTORE_ONLY) {
    const facts=Number(process.env.QA_EXPECTED_FACTS),details=Number(process.env.QA_EXPECTED_DETAILS);
    await until(()=>evaluate(`document.getElementById('screen-data')?.hidden===false && document.querySelectorAll('#data-grid tbody tr').length===${facts}`),'Data after server and browser restart');
    assert.equal(await evaluate('document.documentElement.dataset.theme'),'dark');
    assert.equal(await evaluate('document.getElementById("nav-data").getAttribute("aria-current")'),'page');
    await evaluate('document.getElementById("data-tab-detail_faktur").click()');
    assert.equal(await evaluate('document.querySelectorAll("#data-grid tbody tr").length'),details);
    await evaluate('document.getElementById("data-tab-faktur").click()');
    await capture('data_restart_dark_desktop.png');await capture('data_restart_dark_mobile.png',390,true);
    assert.equal(await evaluate('document.documentElement.scrollWidth<=390'),true);
    await evaluate('document.getElementById("nav-home").click()');
    assert.equal(await evaluate('document.getElementById("screen-select")?.hidden===false'),true);
    await evaluate('document.getElementById("theme-toggle").click()');
    assert.equal(await evaluate('document.documentElement.dataset.theme'),'light');
    await evaluate('document.getElementById("select-document").disabled ? document.getElementById("resume-home").click() : undefined');
    await capture('home_restart_light_mobile.png',390,true);
    writeFileSync(join(output,'uji_browser_restart.json'),JSON.stringify({hasil:'lolos',server_dan_browser_dibuka_ulang:true,tema_tersimpan:'dark',faktur:facts,detail:details,home_mobile:true,tanpa_ocr:true},null,2));
    console.log('Restart server/browser, persistensi Data dan tema, serta Home mobile: lolos.');
  } else {
  await evaluate("localStorage.removeItem('faktur-lokal-sesi');localStorage.removeItem('faktur-lokal-tema'); location.reload(); true");
  await until(()=>evaluate('document.getElementById("screen-select")?.hidden===false'),'first screen');
  const fixtures=await (await fetch(url+'/__qa/files')).json();
  await evaluate('document.fonts.ready.then(() => true)');
  assert.equal(await evaluate('document.fonts.check("14px Inter")'),true);
  async function choose(count,type) {
    const encoded=fixtures[type];
    return evaluate(`(() => {
      const bytes=Uint8Array.from(atob(${JSON.stringify(encoded)}),c=>c.charCodeAt(0));
      const transfer=new DataTransfer();
      for(let i=0;i<${count};i++)transfer.items.add(new File([bytes], 'uji-'+i+'.${type==='pdf'?'pdf':'png'}',{type:'${type==='pdf'?'application/pdf':'image/png'}'}));
      const input=document.getElementById('document-input');input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));
    })()`);
  }
  const visible = id => evaluate(`document.getElementById('${id}')?.hidden===false`);
  const stats = async()=>await (await fetch(url+'/__qa/stats')).json();
  async function screenshot(name,width=1280,mobile=false) {
    await send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile});
    await evaluate('window.scrollTo(0,0)');
    const metrics=await send('Page.getLayoutMetrics');
    const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:{x:0,y:0,width,height:metrics.cssContentSize.height,scale:1}});
    writeFileSync(join(output,name),Buffer.from(shot.data,'base64'));
  }
  const style=await evaluate(`(() => {const logo=document.querySelector('.header img'),r=logo.getBoundingClientRect();return {font:getComputedStyle(document.body).fontFamily,rootBackground:getComputedStyle(document.documentElement).backgroundColor,logoWidth:logo.naturalWidth,logoHeight:logo.naturalHeight,ratio:r.width/r.height,title:document.getElementById('select-title').textContent,button:document.getElementById('select-document').textContent};})()`);
  assert.deepEqual(await evaluate('[...document.querySelectorAll(".header nav button")].map(x=>x.textContent)'),['Home','Data']);
  assert.equal(style.title,'Ekstraksi Faktur');assert.equal(style.button,'Select document');
  assert.equal(style.rootBackground,'rgb(255, 255, 255)');assert.equal(style.logoWidth,2172);assert.equal(style.logoHeight,724);
  assert.ok(Math.abs(style.ratio-3)<.001);assert.ok(style.font.includes('Inter'));
  assert.equal(await visible('screen-configure'),false);assert.equal(await visible('screen-results'),false);
  await screenshot('layar_1_desktop.png');await screenshot('layar_1_mobile.png',390,true);
  await send('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false});
  await choose(61,'gambar');
  assert.ok(await evaluate('document.getElementById("app-error").textContent.includes("60 gambar")'));
  assert.equal((await stats()).staged,0);
  await choose(60,'gambar');
  await until(()=>evaluate('!document.getElementById("screen-configure").hidden && document.querySelectorAll(".file-card").length===60'),'60 images ready');
  assert.equal((await stats()).submits,0);
  assert.ok(await evaluate('document.getElementById("selection-hint").textContent.includes("60 gambar")'));
  await choose(1,'gambar');assert.ok(await evaluate('document.getElementById("app-error").textContent.includes("60 gambar")'));
  await evaluate('document.querySelector(".remove-file").click()');
  assert.equal(await evaluate('document.querySelectorAll(".file-card").length'),59);
  await choose(1,'gambar');await until(()=>evaluate('document.querySelectorAll(".file-card").length===60'),'add same kind');
  await evaluate('document.getElementById("choose-again").click();document.querySelector("input[value=pdf]").click()');
  await choose(11,'pdf');assert.ok(await evaluate('document.getElementById("app-error").textContent.includes("10 PDF")'));
  await choose(10,'pdf');await until(()=>evaluate('document.querySelectorAll(".file-card").length===10'),'10 PDFs ready');
  assert.equal(await evaluate('document.getElementById("conversion-title").textContent'),'Konversi PDF');
  assert.ok(await evaluate('document.getElementById("selection-hint").textContent.includes("10 PDF")'));
  await choose(1,'gambar');assert.ok(await evaluate('document.getElementById("app-error").textContent.includes("dicampur")'));
  assert.equal((await stats()).submits,0);
  await evaluate('document.getElementById("choose-again").click();document.querySelector("input[value=gambar]").click()');
  await choose(2,'gambar');await until(()=>evaluate('document.querySelectorAll(".file-card").length===2'),'2 images ready');
  await evaluate('document.getElementById("result-format").value="csv"');
  await evaluate('document.getElementById("back-configure").click()');
  assert.equal(await visible('screen-select'),true);
  assert.ok(await evaluate('document.getElementById("selection-hint").textContent.includes("60 gambar")'));
  await evaluate('document.getElementById("resume-home").click()');
  assert.equal(await evaluate('document.querySelectorAll(".file-card").length'),2);
  assert.equal(await evaluate('document.getElementById("result-format").value'),'csv');
  await evaluate('document.getElementById("theme-toggle").click()');
  assert.equal(await evaluate('getComputedStyle(document.documentElement).backgroundColor'),'rgb(23, 25, 30)');
  await screenshot('konversi_dark_desktop.png');
  await evaluate('document.getElementById("nav-home").click()');
  assert.equal(await visible('screen-select'),true);
  await screenshot('home_dark_desktop.png');
  await evaluate('document.getElementById("nav-data").click()');
  await until(()=>evaluate('document.querySelectorAll("#data-grid tbody tr").length===3'),'combined Data 3 invoices');
  assert.equal(await evaluate('document.getElementById("nav-data").getAttribute("aria-current")'),'page');
  await screenshot('data_dark_desktop.png');await screenshot('data_dark_mobile.png',390,true);
  assert.equal(await evaluate('document.documentElement.scrollWidth<=390'),true);
  await evaluate('document.getElementById("data-tab-detail_faktur").click()');
  assert.equal(await evaluate('document.querySelectorAll("#data-grid tbody tr").length'),7);
  await evaluate('document.getElementById("data-tab-faktur").click()');
  for(const id of ['data-excel','data-csv'])await evaluate(`document.getElementById('${id}').click()`);
  await until(()=>existsSync(join(downloads,'data_faktur.xlsx'))&&existsSync(join(downloads,'data_faktur_csv.zip')),'combined downloads');
  await evaluate(`document.querySelector('.source-session[title="${job}"]').click()`);
  await until(()=>visible('screen-results'),'session opened from Data');
  assert.equal(await evaluate('document.querySelectorAll("#data-table tbody tr").length'),2);
  await screenshot('hasil_sesi_dark_mobile.png',390,true);
  await evaluate('document.getElementById("back-results").click()');
  await until(()=>visible('screen-data'),'return to Data');
  await evaluate('document.getElementById("nav-home").click();document.getElementById("resume-home").click()');
  assert.equal(await visible('screen-configure'),true);
  assert.equal(await evaluate('document.querySelectorAll(".file-card").length'),2);
  assert.equal((await stats()).submits,0);
  await evaluate('document.getElementById("theme-toggle").click()');
  await screenshot('layar_2_desktop.png');await screenshot('layar_2_mobile.png',390,true);
  assert.equal(await evaluate('document.querySelector(".conversion-panel").getBoundingClientRect().top >= document.querySelector(".file-area").getBoundingClientRect().bottom'),true);
  assert.equal(await evaluate('document.documentElement.scrollWidth<=390'),true);
  await send('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false});
  await evaluate('document.getElementById("start-conversion").click()');
  await until(()=>evaluate('document.getElementById("progress-count").textContent.includes("1 dari 2")'),'progress count from fixture');
  assert.equal(await visible('screen-configure'),true);assert.equal(await visible('screen-results'),false);
  await evaluate('document.getElementById("back-configure").click();document.getElementById("nav-data").click()');
  await until(()=>visible('screen-data'),'Data during conversion');
  await evaluate('document.getElementById("data-tab-faktur").click()');
  await until(()=>evaluate('document.querySelectorAll("#data-grid tbody tr").length===3'),'pending session excluded');
  await evaluate(`document.querySelector('.source-session[title="${job}"]').click()`);
  await until(()=>visible('screen-results'),'old result during conversion');
  assert.equal(await evaluate('document.querySelectorAll("#data-table tbody tr").length'),2);
  assert.equal((await stats()).submits,1);
  await evaluate('document.getElementById("back-results").click();document.getElementById("nav-home").click();document.getElementById("resume-home").click()');
  assert.equal(await visible('screen-configure'),true);
  assert.equal(await evaluate('document.getElementById("progress-count").textContent.includes("1 dari 2")'),true);
  await screenshot('progres_desktop.png');await fetch(url+'/__qa/finish');
  await until(()=>visible('screen-results'),'partial results');
  assert.equal(await evaluate('document.getElementById("results-title").textContent'),'Hasil siap diunduh');
  assert.equal(await visible('partial-results'),true);
  assert.ok(await evaluate('document.getElementById("failed-files").textContent.includes("uji-1.png")'));
  assert.equal(await evaluate('document.querySelectorAll("#data-table tbody tr").length'),1);
  assert.equal(await evaluate('document.getElementById("download-result").download'),'hasil_faktur_csv.zip');
  await evaluate('document.getElementById("download-result").click()');
  await until(()=>existsSync(join(downloads,'hasil_faktur_csv.zip')),'CSV ZIP download');
  await evaluate('document.getElementById("tab-detail_faktur").click()');
  assert.equal(await evaluate('document.querySelectorAll("#data-table tbody tr").length'),3);
  await evaluate('document.getElementById("tab-faktur").click();document.getElementById("review-link").click()');
  assert.equal(await visible('review-panel'),true);
  assert.equal(await evaluate('document.querySelectorAll("#screen-results a[download]").length'),1);
  await evaluate('document.getElementById("review-link").click()');
  await screenshot('layar_3_sebagian_desktop.png');await screenshot('layar_3_mobile.png',390,true);
  await evaluate('document.getElementById("theme-toggle").click()');
  await screenshot('hasil_parsial_dark_mobile.png',390,true);
  await evaluate('document.getElementById("theme-toggle").click()');
  assert.equal(await evaluate('document.documentElement.scrollWidth<=390'),true);
  await evaluate('document.getElementById("back-results").click()');
  assert.equal(await visible('screen-configure'),true);
  assert.equal(await evaluate('document.getElementById("start-conversion").textContent'),'Lihat hasil');
  await evaluate('document.getElementById("start-conversion").click()');
  assert.equal((await stats()).submits,1);
  assert.equal(await visible('screen-results'),true);
  await evaluate('document.getElementById("start-again").click()');
  assert.equal(await visible('screen-select'),true);
  assert.equal(await evaluate('localStorage.getItem("faktur-lokal-sesi")'),null);
  await evaluate('document.querySelector("input[value=pdf]").click()');
  await choose(1,'pdf');await until(()=>visible('screen-configure'),'single PDF');
  assert.equal(await evaluate('document.getElementById("result-format").value'),'xlsx');
  await evaluate('document.getElementById("start-conversion").click()');
  await until(()=>evaluate('document.getElementById("progress-count").textContent.includes("1 dari 1")'),'one document processed');
  await evaluate('document.getElementById("nav-home").click();document.getElementById("nav-data").click()');
  await until(()=>evaluate('document.querySelectorAll("#data-grid tbody tr").length===4'),'Data while second conversion runs');
  await fetch(url+'/__qa/finish');
  await until(()=>evaluate('document.querySelectorAll("#data-grid tbody tr").length===5'),'Data updates when background conversion completes');
  assert.equal(await visible('screen-data'),true);
  await evaluate('document.getElementById("nav-home").click();document.getElementById("resume-home").click()');
  await until(()=>visible('screen-results'),'Excel result screen');
  assert.equal(await evaluate('document.getElementById("download-result").download'),'hasil_faktur.xlsx');
  assert.equal(await visible('partial-results'),false);
  await evaluate('document.getElementById("download-result").click()');
  await until(()=>existsSync(join(downloads,'hasil_faktur.xlsx')),'Excel download');
  await screenshot('layar_3_excel_desktop.png');
  await evaluate('location.reload();true');
  await until(()=>evaluate('document.getElementById("screen-results")?.hidden===false && document.querySelector("input[value=pdf]").checked'),'resume PDF kind');
  await evaluate(`localStorage.setItem('faktur-lokal-sesi',${JSON.stringify(job)});location.reload();true`);
  await until(()=>evaluate('document.getElementById("screen-results")?.hidden===false && document.querySelectorAll("#data-table tbody tr").length===2'),'resume existing real session');
  await screenshot('layar_3_sesi_lama.png');
  assert.equal((await stats()).submits,2);
  await evaluate('document.getElementById("theme-toggle").click();document.getElementById("nav-data").click()');
  await until(()=>evaluate('document.querySelectorAll("#data-grid tbody tr").length===5'),'new completed sessions added to Data');
  assert.equal(await evaluate('document.documentElement.dataset.theme'),'dark');
  assert.equal(errors.length,0,JSON.stringify(errors));
  writeFileSync(join(output,'uji_browser.json'),JSON.stringify({hasil:'lolos',style,menus:['Home','Data'],back_ke_data:true,back_tidak_ocr:true,navigasi_tidak_membatalkan_worker:true,tema_semua_layar:true,data_awal:[3,7],data_setelah_fixture:[5,13],three_screens:true,images_60_61:true,pdfs_10_11:true,mixed_rejected:true,no_conversion_on_selection:true,add_remove:true,partial_results:true,progress_from_status:true,format_retained:true,start_again:true,restored_old_session:true,desktop_mobile:true,downloads:['hasil_faktur.xlsx','hasil_faktur_csv.zip','data_faktur.xlsx','data_faktur_csv.zip'],errors},null,2));
  console.log('Tiga layar, batas 60/61 dan 10/11, campuran, progres, hasil sebagian, dan dua bundle: lolos. Worker pengujian memakai fixture tanpa OCR.');
  }
} finally {
  if (socket?.readyState===WebSocket.OPEN) {
    try {await send('Browser.close');} catch (_) {}
    socket.close();
  }
  if (browser.exitCode===null) browser.kill();
}
