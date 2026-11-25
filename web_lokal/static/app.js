'use strict';
const $ = id => document.getElementById(id);
const limits = {gambar:60,pdf:10};
const imageExtensions = ['jpg','jpeg','png','bmp','webp','tif','tiff'];
const labels = {menunggu:'Menunggu',diproses:'Diproses',selesai:'Selesai',perlu_review:'Perlu review',gagal:'Gagal',terputus:'Terputus'};
let kind='gambar',selected=[],activeJob=null,resultFormat='xlsx',results=null;
let busy=false,selecting=false,polling=false,timer=null;
let menu='home',visibleScreen='select',resultOrigin='home';
let conversionState=null,conversionResults=null,dataResults=null,dataTab='faktur';
let dataRequest=0,viewRequest=0;
function el(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function screen(name){visibleScreen=name;for(const item of ['select','configure','results','data'])$(`screen-${item}`).hidden=item!==name;}
function setMenu(value){
  menu=value;
  for(const item of ['home','data']){const button=$(`nav-${item}`);button.classList.toggle('active',item===menu);if(item===menu)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');}
  history.replaceState(null,'',value==='data'?'#data':'#home');
}
function error(message=''){$('app-error').textContent=message;$('app-error').hidden=!message;}
function remember(){try{if(activeJob)localStorage.setItem('faktur-lokal-sesi',JSON.stringify({id:activeJob,format:resultFormat}));else localStorage.removeItem('faktur-lokal-sesi');}catch(_){}}
async function api(url,options){const response=await fetch(url,options);const data=await response.json();if(!response.ok){const failure=new Error(data.error||`Permintaan gagal (${response.status}).`);failure.status=response.status;throw failure;}return data;}
function syncControls(){
  const locked=busy||selecting||Boolean(conversionState?.terminal);
  for(const id of ['add-files','result-format'])$(id).disabled=locked;
  $('choose-again').disabled=selecting;
  $('start-again').disabled=selecting;
  $('start-conversion').disabled=busy||selecting||!selected.length||Boolean(conversionState?.terminal&&!conversionResults);
  $('start-conversion').textContent=busy?'Konversi berjalan…':conversionResults?'Lihat hasil':'Mulai konversi →';
  document.querySelectorAll('.remove-file').forEach(node=>node.disabled=locked);
  $('select-document').disabled=busy||selecting;
  document.querySelectorAll('input[name="jenis"]').forEach(node=>node.disabled=busy||selecting||(selected.length>0&&!conversionState?.terminal));
  $('resume-home').hidden=!(selected.length||activeJob||conversionResults);
  $('resume-home').textContent=busy?'Lihat progres':conversionResults?'Lihat hasil':'Lanjutkan pilihan';
}
function setBusy(value){busy=value;syncControls();}
function updateKind(){
  document.querySelectorAll('input[name="jenis"]').forEach(node=>node.checked=node.value===kind);
  $('document-input').accept=kind==='pdf'?'.pdf':imageExtensions.map(x=>'.'+x).join(',');
  $('selection-hint').textContent=kind==='gambar'?'Maksimal 60 gambar · 25 MB per file':'Maksimal 10 PDF · 25 MB per file · 20 halaman per PDF';
  $('conversion-title').textContent=kind==='gambar'?'Konversi Gambar':'Konversi PDF';syncControls();
}
function renderFiles(){
  $('file-count').textContent=`${selected.length} ${kind==='gambar'?'gambar':'PDF'} dipilih`;
  $('file-list').replaceChildren();
  selected.forEach((entry,index)=>{
    const card=el('li',undefined,'file-card'),remove=el('button','×','remove-file');remove.type='button';
    remove.setAttribute('aria-label',`Hapus ${entry.name}`);
    remove.addEventListener('click',()=>{if(busy||selecting||conversionState?.terminal)return;selected.splice(index,1);renderFiles();if(!selected.length)screen('select');});
    card.append(remove,el('span',entry.name.split('.').pop().toUpperCase(),'document-icon'),el('strong',entry.name),el('span',`${(entry.size/1024/1024).toFixed(2)} MB`,'file-size'));$('file-list').append(card);
  });syncControls();
}
function preflight(files){
  for(const file of files){const suffix=file.name.split('.').pop().toLowerCase();if(kind==='gambar'?!imageExtensions.includes(suffix):suffix!=='pdf')throw new Error('Gambar dan PDF tidak boleh dicampur. Pilih file sesuai jenis sesi.');if(!file.size||file.size>25*1024*1024)throw new Error(`${file.name}: file harus berisi data dan maksimal 25 MB.`);}
  if(selected.length+files.length>limits[kind])throw new Error(`Maksimal ${limits[kind]} ${kind==='gambar'?'gambar':'PDF'} per sesi. Hapus file atau pilih lebih sedikit.`);
}
async function addFiles(files){
  if(busy||selecting||conversionState?.terminal||!files.length)return;
  const selectionView=viewRequest;
  error();try{preflight(files);}catch(failure){error(failure.message);return;}
  selecting=true;syncControls();renderFiles();
  try{
    const additions=[];
    for(const [index,file] of files.entries()){
      $('selection-status').textContent=`Memeriksa ${index+1} dari ${files.length} dokumen…`;
      if(visibleScreen==='configure')$('file-count').textContent=`Memeriksa ${index+1} dari ${files.length} dokumen…`;
      const form=new FormData();form.append(kind,file);
      const metadata=await api('/api/uploads',{method:'POST',headers:{'X-Local-Upload':'1'},body:form});
      additions.push({id:metadata.id,name:file.name,size:file.size});
    }
    selected.push(...additions);if(menu==='home'&&selectionView===viewRequest)screen('configure');updateKind();
  }catch(failure){error(failure.message);}
  finally{selecting=false;$('selection-status').textContent='';setBusy(false);renderFiles();$('document-input').value='';}
}
$('select-document').addEventListener('click',()=>{if(conversionState?.terminal)reset();$('document-input').value='';$('document-input').click();});
$('add-files').addEventListener('click',()=>{$('document-input').value='';$('document-input').click();});
$('document-input').addEventListener('change',event=>addFiles([...event.target.files]));
document.querySelectorAll('input[name="jenis"]').forEach(node=>node.addEventListener('change',()=>{if(conversionState?.terminal)reset();kind=node.value;error();updateKind();}));
function goHome(){viewRequest++;setMenu('home');error();screen('select');updateKind();}
function resumeHome(){
  setMenu('home');error();resultOrigin='home';
  if(conversionResults){renderResults(conversionState,conversionResults);screen('results');}
  else{screen('configure');renderFiles();syncControls();}
}
function reset(){
  if(selecting)return;
  // Hanya membuat pilihan baru di browser. Worker yang sudah diterima server tetap berjalan.
  clearTimeout(timer);activeJob=null;selected=[];results=null;conversionState=null;conversionResults=null;resultFormat='xlsx';busy=false;remember();
  $('result-format').value='xlsx';$('progress-panel').hidden=true;$('review-panel').hidden=true;$('failed-restart').hidden=true;$('retry-status').hidden=true;
  goHome();renderFiles();window.scrollTo({top:0,behavior:'smooth'});
}
$('nav-home').addEventListener('click',goHome);
$('brand-home').addEventListener('click',event=>{event.preventDefault();goHome();});
$('resume-home').addEventListener('click',resumeHome);
$('back-configure').addEventListener('click',goHome);
$('back-results').addEventListener('click',()=>{if(resultOrigin==='data')goData();else{setMenu('home');screen('configure');error();renderFiles();}});
for(const id of ['start-again','choose-again','failed-restart'])$(id).addEventListener('click',reset);
$('choose-again').textContent='Mulai lagi';
$('start-conversion').addEventListener('click',async()=>{
  if(conversionResults){resultOrigin='home';setMenu('home');renderResults(conversionState,conversionResults);screen('results');return;}
  if(busy||selecting||!selected.length||conversionState?.terminal)return;
  error();resultFormat=$('result-format').value;setBusy(true);
  try{
    const data=await api('/api/jobs',{method:'POST',headers:{'X-Local-Upload':'1','Content-Type':'application/json'},body:JSON.stringify({jenis:kind,format:resultFormat,unggahan:selected.map(x=>x.id)})});
    activeJob=data.id;remember();$('progress-panel').hidden=false;$('progress-count').textContent=`0 dari ${selected.length} dokumen selesai`;await poll();
  }catch(failure){error(failure.message);setBusy(false);}
});
async function poll(){
  if(!activeJob||polling)return;
  const job=activeJob;polling=true;clearTimeout(timer);$('retry-status').hidden=true;
  try{
    const state=await api(`/api/jobs/${job}`);if(activeJob!==job)return;
    conversionState=state;resultFormat=state.format||resultFormat;$('result-format').value=resultFormat;
    if(!selected.length){kind=state.jenis||state.unggahan[0]?.jenis||'gambar';selected=state.unggahan.map(x=>({name:x.nama_file,size:x.ukuran_byte}));updateKind();renderFiles();}
    $('progress-panel').hidden=false;$('progress-count').textContent=`${state.jumlah_selesai} dari ${state.jumlah_dokumen} dokumen selesai`;
    $('progress-bar').max=state.jumlah_dokumen||1;$('progress-bar').value=state.jumlah_selesai;$('progress-message').textContent=state.pesan;$('processing-files').replaceChildren();
    for(const doc of state.dokumen){const item=el('li',doc.nama_file);item.append(el('span',`${labels[doc.status]||doc.status}${doc.error?': '+doc.error:''}`,'state'));$('processing-files').append(item);}
    if(state.terminal){
      if(state.hasil_tersedia&&state.dokumen.some(x=>['selesai','perlu_review'].includes(x.status))){
        const payload=await api(`/api/jobs/${job}/result`);if(activeJob!==job)return;conversionResults=payload;
        if(menu==='home'&&visibleScreen==='configure'){resultOrigin='home';renderResults(state,payload);screen('results');}
        setBusy(false);if(menu==='data'&&visibleScreen==='data')loadData();
      }else{
        setBusy(false);$('failed-restart').hidden=false;
        if(menu==='home'&&visibleScreen==='configure')error('Tidak ada dokumen yang berhasil diproses. Periksa keterangan file di panel konversi.');
      }
    }else{setBusy(true);timer=setTimeout(poll,1500);}
  }catch(failure){
    if(activeJob!==job)return;
    if(menu==='home'&&visibleScreen==='configure')error(`Tidak dapat membaca sesi: ${failure.message}`);
    $('progress-panel').hidden=false;$('retry-status').hidden=false;
    if(failure.status===404){setBusy(false);activeJob=null;selected=[];conversionState=null;remember();if(menu==='home')screen('select');syncControls();}
  }finally{polling=false;if(activeJob&&activeJob!==job)timer=setTimeout(poll,0);}
}
$('retry-status').addEventListener('click',poll);
function renderResults(state,payload){
  results=payload;error();
  const format=state.format||'xlsx';$('result-count').textContent=`${results.faktur.baris.length} faktur · ${results.detail_faktur.baris.length} detail barang`;
  const name=format==='csv'?'hasil_faktur_csv.zip':'hasil_faktur.xlsx';
  $('download-result').textContent=format==='csv'?'Unduh CSV (.zip)':'Unduh Excel (.xlsx)';$('download-result').href=results.unduhan[name];$('download-result').download=name;
  $('download-note').textContent=format==='csv'?'ZIP berisi faktur.csv dan detail_faktur.csv. Saat impor ke Excel, pilih tipe Teks untuk kolom identitas.':'Satu file dengan sheet Faktur dan Detail Faktur. Nilai disimpan sebagai teks.';
  const failed=state.dokumen.filter(x=>['gagal','terputus'].includes(x.status));$('partial-results').hidden=!failed.length;$('failed-files').replaceChildren();
  $('partial-summary').textContent=`${failed.length} dokumen gagal. Hasil dokumen yang berhasil tetap tersedia.`;
  failed.forEach(doc=>$('failed-files').append(el('li',`${doc.nama_file}: ${doc.error||labels[doc.status]}`)));
  const notes=results.review.baris,issues=results.dokumen.flatMap(doc=>(doc.kendala||[]).map(note=>({pesan:note})));
  $('review-link').hidden=!(notes.length+issues.length);$('review-link').textContent=`Catatan review (${notes.length+issues.length})`;
  $('review-panel').hidden=true;$('review-link').setAttribute('aria-expanded','false');const list=el('ul');
  [...notes,...issues].forEach(note=>list.append(el('li',[note.id_faktur,note.kolom,note.pesan].filter(Boolean).join(' · '))));
  $('review-panel').replaceChildren(el('p','Hasil otomatis belum diverifikasi manusia.'),list);showTable('faktur');
}
$('review-link').addEventListener('click',()=>{$('review-panel').hidden=!$('review-panel').hidden;$('review-link').setAttribute('aria-expanded',String(!$('review-panel').hidden));});
function drawTable(data,target,sourceLinks=false){
  const container=$(target);container.replaceChildren();
  if(!data.baris.length){container.append(el('p','Tidak ada baris hasil untuk tabel ini.','empty'));return;}
  const table=el('table'),head=el('thead'),header=el('tr'),body=el('tbody');
  data.kolom.forEach(column=>{const cell=el('th',column);cell.scope='col';header.append(cell);});head.append(header);
  data.baris.forEach(record=>{const row=el('tr');data.kolom.forEach(column=>{
    const value=record[column],cell=el('td'),content=el('div',value===''?'—':value,'cell-value');
    if(sourceLinks&&column==='id_sesi'){
      const button=el('button',`Buka ${value.slice(0,8)}`,'text-button source-session');button.type='button';button.title=value;button.setAttribute('aria-label',`Buka hasil sesi ${value}`);button.addEventListener('click',()=>openSession(value));content.replaceChildren(button);
    }else if(value&&value.length>120)content.tabIndex=0;
    if(value===''){cell.className='empty-value';cell.title='Nilai kosong dari ekstraksi';}cell.append(content);row.append(cell);
  });body.append(row);});table.append(head,body);container.append(table);
}
function selectTab(scope,key){document.querySelectorAll(`${scope} .tab`).forEach(tab=>{const active=tab.dataset.table===key;tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;});}
function showTable(key){if(!results)return;selectTab('#screen-results',key);$('data-table').setAttribute('aria-labelledby',`tab-${key}`);drawTable(results[key],'data-table');}
function showDataTable(key){dataTab=key;if(!dataResults)return;selectTab('#screen-data',key);$('data-grid').setAttribute('aria-labelledby',`data-tab-${key}`);drawTable(dataResults[key],'data-grid',true);}
async function loadData(){
  const request=++dataRequest;$('data-refresh').disabled=true;$('data-count').textContent='Membaca hasil tersimpan…';
  try{
    const payload=await api('/api/data');if(request!==dataRequest)return;dataResults=payload;
    $('data-count').textContent=`${payload.faktur.baris.length} faktur · ${payload.detail_faktur.baris.length} detail barang`;
    $('data-excel').href=payload.unduhan.xlsx;$('data-csv').href=payload.unduhan.csv;
    $('data-problems').hidden=!payload.kendala.length;$('data-problems').textContent=payload.kendala.map(x=>`Sesi ${x.id_sesi.slice(0,8)}: ${x.pesan}`).join(' ');showDataTable(dataTab);
  }catch(failure){if(request===dataRequest){$('data-count').textContent='Data belum dapat dibaca.';if(menu==='data'&&visibleScreen==='data')error(failure.message);}}
  finally{if(request===dataRequest)$('data-refresh').disabled=false;}
}
function goData(){viewRequest++;setMenu('data');error();screen('data');loadData();}
async function openSession(id){
  const request=++viewRequest;
  try{const [state,payload]=await Promise.all([api(`/api/jobs/${id}`),api(`/api/jobs/${id}/result`)]);if(request!==viewRequest||menu!=='data')return;resultOrigin='data';renderResults(state,payload);screen('results');}
  catch(failure){if(request===viewRequest)error(failure.message);}
}
$('nav-data').addEventListener('click',goData);$('data-refresh').addEventListener('click',loadData);
for(const [scope,callback] of [['#screen-results',showTable],['#screen-data',showDataTable]]){
  const tabs=[...document.querySelectorAll(`${scope} .tab`)];
  tabs.forEach((tab,index)=>{tab.addEventListener('click',()=>callback(tab.dataset.table));tab.addEventListener('keydown',event=>{
    let next;if(event.key==='ArrowRight')next=(index+1)%tabs.length;else if(event.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;else return;
    event.preventDefault();callback(tabs[next].dataset.table);tabs[next].focus();
  });});
}
function syncTheme(){const dark=document.documentElement.dataset.theme==='dark';$('theme-toggle').firstElementChild.textContent=dark?'☀':'☾';$('theme-toggle').setAttribute('aria-pressed',String(dark));$('theme-toggle').setAttribute('aria-label',dark?'Aktifkan tema terang':'Aktifkan tema gelap');$('theme-toggle').title=dark?'Tema terang':'Tema gelap';}
$('theme-toggle').addEventListener('click',()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=theme;try{localStorage.setItem('faktur-lokal-tema',theme);}catch(_){}syncTheme();});
syncTheme();updateKind();
const initialData=location.hash==='#data';if(initialData)goData();
try{
  const raw=localStorage.getItem('faktur-lokal-sesi');const saved=raw&&(/^[a-f0-9]{32}$/.test(raw)?{id:raw,format:'xlsx'}:JSON.parse(raw));
  if(saved&&/^[a-f0-9]{32}$/.test(saved.id)){activeJob=saved.id;resultFormat=saved.format==='csv'?'csv':'xlsx';if(!initialData)screen('configure');setBusy(true);poll();}
}catch(_){}
