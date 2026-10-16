'use strict';
// Keep dataset values as text, keep credentials out of URLs/downloads, preserve the existing session key.
const $ = id => document.getElementById(id);
const storageKey = 'g85-tabular-workspace-v1';
const activeKey = storageKey + '-active';
const themeKey = 'g85-tabular-theme';
const iconsUrl = '/workspace/assets/icons.svg#';
const labels = {answer:'Đã trả lời',partial:'Trả lời một phần',clarify:'Cần làm rõ',insufficient_data:'Chưa đủ dữ liệu'};
let datasets = [], active = null, busy = false, pending = null, selectedFile = null, uploading = false;
let theme = 'system';
try {
  const stored = JSON.parse(sessionStorage.getItem(storageKey) || '[]');
  datasets = Array.isArray(stored) ? stored.filter(d => d && typeof d.dataset_id === 'string' && d.dataset_token && d.version) : [];
} catch { datasets = []; }
try { theme = localStorage.getItem(themeKey) || 'system'; } catch { /* Preferences are optional. */ }
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function icon(name) {
  const element = document.createElementNS('http://www.w3.org/2000/svg','svg');
  element.setAttribute('class','icon'); element.setAttribute('aria-hidden','true');
  const use = document.createElementNS(element.namespaceURI,'use');
  use.setAttribute('href',iconsUrl + name); element.append(use); return element;
}
function button(text, symbol, action, className='secondary') {
  const element = node('button',undefined,className); element.type='button';
  if (symbol) element.append(icon(symbol)); element.append(node('span',text));
  element.addEventListener('click',action); return element;
}
document.querySelectorAll('[data-icon]').forEach(element => element.append(icon(element.dataset.icon)));
function applyTheme() {
  if (!['light','dark','system'].includes(theme)) theme='system';
  const resolved = theme === 'system' ? (systemTheme.matches?'dark':'light') : theme;
  document.documentElement.dataset.theme=resolved;
  $('theme-toggle').replaceChildren(icon(theme==='system'?'system':theme==='dark'?'moon':'sun'));
  const name = {system:'Theo hệ thống',light:'Sáng',dark:'Tối'}[theme];
  $('theme-toggle').title='Giao diện: '+name+'. Bấm để đổi.';
  $('theme-toggle').setAttribute('aria-label','Giao diện: '+name+'. Đổi giao diện');
}
applyTheme(); systemTheme.addEventListener('change',applyTheme);
$('theme-toggle').onclick=()=>{theme=theme==='system'?'light':theme==='light'?'dark':'system';try{localStorage.setItem(themeKey,theme);}catch{}applyTheme();};
function notice(text='',error=false) {
  const box=$('notice'); box.replaceChildren(); box.hidden=!text; box.className=error?'error':'';
  if (!text) return;
  box.append(icon(error?'alert':'info'),node('span',text,'notice-text'));
  const close=button('','close',()=>notice(),'icon-button'); close.setAttribute('aria-label','Ẩn thông báo'); box.append(close);
}
function persist() {
  try {
    sessionStorage.setItem(storageKey,JSON.stringify(datasets));
    if(active) sessionStorage.setItem(activeKey,active.dataset_id);
  } catch { notice('Bộ nhớ trình duyệt đã đầy. Dữ liệu mới chỉ được giữ khi trang đang mở; bạn nên tải hội thoại cần lưu.',true); }
}
function lock(value) {
  busy=value; document.body.setAttribute('aria-busy',String(value));
  ['add-data','view-profile','new-chat','conversation-menu-button'].forEach(id=>$(id).disabled=value || (id!=='add-data' && !active));
  $('question').disabled=value || !active;
  $('send').disabled=value || !active || !$('question').value.trim();
  $('dataset-search').disabled=value;
  $('datasets').querySelectorAll('button').forEach(b=>b.disabled=value);
  $('upload-form').querySelectorAll('input,button').forEach(e=>e.disabled=value);
  $('upload-panel').querySelectorAll('[data-close-dialog]').forEach(e=>e.disabled=uploading);
  $('upload-progress').hidden=!uploading;
  $('messages').querySelectorAll('.suggestion').forEach(b=>b.disabled=value);
}
function setSidebar(open,focus=true) {
  document.body.classList.toggle('sidebar-open',open);
  $('open-sidebar').setAttribute('aria-expanded',String(open)); $('sidebar-backdrop').hidden=!open;
  const mobile=matchMedia('(max-width:767px)').matches;
  $('main').inert=mobile && open;
  if (focus) (open?$('close-sidebar'):$('open-sidebar')).focus();
}
$('open-sidebar').onclick=()=>setSidebar(true);
$('close-sidebar').onclick=()=>setSidebar(false);
$('sidebar-backdrop').onclick=()=>setSidebar(false);
matchMedia('(max-width:767px)').addEventListener('change',()=>setSidebar(false,false));
function openDialog(id) { closeMenu(); if (!$(id).open) $(id).showModal(); }
document.querySelectorAll('[data-close-dialog]').forEach(b=>b.onclick=()=>{if(busy && b.closest('dialog').id==='upload-panel')return;b.closest('dialog').close();});
document.querySelectorAll('dialog').forEach(dialog=>{
  dialog.addEventListener('cancel',event=>{if(uploading && dialog.id==='upload-panel')event.preventDefault();});
  dialog.addEventListener('click',event=>{
    if(event.target!==dialog || (uploading && dialog.id==='upload-panel'))return;
    const r=dialog.getBoundingClientRect();
    if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();
  });
});
function closeMenu() { $('conversation-menu').hidden=true; $('conversation-menu-button').setAttribute('aria-expanded','false'); }
$('conversation-menu-button').onclick=()=>{const open=$('conversation-menu').hidden;$('conversation-menu').hidden=!open;$('conversation-menu-button').setAttribute('aria-expanded',String(open));if(open)$('export-chat').focus();};
document.addEventListener('click',event=>{if(!event.target.closest('.header-actions'))closeMenu();});
document.addEventListener('keydown',event=>{
  if(event.key==='Tab' && document.body.classList.contains('sidebar-open') && !document.querySelector('dialog[open]')){
    const available=[...$('sidebar').querySelectorAll('button:not(:disabled),input:not(:disabled),a[href]')].filter(e=>e.getClientRects().length);
    const first=available[0],last=available[available.length-1];
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  }
  if(event.key==='Escape'){
    if(!$('conversation-menu').hidden){closeMenu();$('conversation-menu-button').focus();}
    if(document.body.classList.contains('sidebar-open'))setSidebar(false);
  }
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'&&!document.querySelector('dialog[open]')){
    event.preventDefault();if(busy)return;if(matchMedia('(max-width:767px)').matches)setSidebar(true,false);$('dataset-search').focus();
  }
});
$('help').onclick=()=>{setSidebar(false,false);openDialog('help-dialog');};
function openUpload() {if(busy)return;setSidebar(false,false);$('upload-error').hidden=true;openDialog('upload-panel');}
$('add-data').onclick=openUpload;
function uploadError(text) {$('upload-error').textContent=text;$('upload-error').hidden=!text;}
function pickFile(file) {
  if(!file)return;
  if(!/\.(csv|xlsx)$/i.test(file.name)){selectedFile=null;uploadError('Hãy chọn file CSV hoặc Excel (.xlsx).');}
  else if(file.size>20*1024*1024){selectedFile=null;uploadError('File vượt quá 20 MB. Hãy giảm kích thước rồi thử lại.');}
  else if(!file.size){selectedFile=null;uploadError('File đang trống. Hãy chọn file có dữ liệu.');}
  else {selectedFile=file;uploadError('');}
  const f=selectedFile;
  $('file-label').textContent=f?f.name:'Kéo thả bảng dữ liệu vào đây';
  $('file-description').textContent=f?(f.size<1024*1024?Math.max(1,Math.round(f.size/1024))+' KB':(f.size/1024/1024).toFixed(1)+' MB')+' · Bấm để chọn file khác':'hoặc bấm để chọn file';
  $('drop-zone').classList.toggle('has-file',!!f);
  if(!f)$('file').value='';
}
$('file').onchange=()=>pickFile($('file').files[0]);
['dragenter','dragover'].forEach(type=>$('drop-zone').addEventListener(type,event=>{event.preventDefault();if(!busy)$('drop-zone').classList.add('drag-over');}));
['dragleave','drop'].forEach(type=>$('drop-zone').addEventListener(type,event=>{event.preventDefault();$('drop-zone').classList.remove('drag-over');}));
$('drop-zone').addEventListener('drop',event=>{
  if(busy)return;
  const files=event.dataTransfer.files;if(files.length!==1){uploadError('Chọn một bảng dữ liệu mỗi lần tải lên.');return;}
  pickFile(files[0]);
  if(selectedFile){const transfer=new DataTransfer();transfer.items.add(selectedFile);$('file').files=transfer.files;}
});
const route = d => '/v1/datasets/'+d.dataset_id;
function errorText(status,detail) {
  const known={
    MULTIPLE_SHEETS_REQUIRE_SELECTION:'File có nhiều sheet. Mở “Tùy chọn nhập dữ liệu” và nhập tên sheet muốn dùng.',
    UNKNOWN_WORKSHEET:'Không có sheet này. Hãy kiểm tra lại tên sheet trong file Excel.',
    INVALID_HEADERS_OR_TABLE_LIMIT:'Tên cột bị trống, trùng hoặc không hợp lệ. Hãy kiểm tra dòng header đầu tiên.',
    FORMULAS_REQUIRE_PRECOMPUTED_VALUES:'File có công thức Excel. Hãy chuyển các ô công thức thành giá trị rồi tải lại.',
    PACKAGE_LIMIT_32_COLUMNS_100000_CELLS:'Bảng vượt giới hạn 32 cột hoặc 100.000 ô. Hãy chia thành bảng nhỏ hơn.',
    SUPPORTED_MAX_32_COLUMNS:'Bảng có hơn 32 cột. Hãy chọn các cột cần dùng.',
    EMPTY_TABLE:'File chưa có dòng dữ liệu bên dưới tên cột.',
    RAGGED_OR_OVERSIZED_ROW:'Có dòng thiếu/thừa cột hoặc ô quá dài. Hãy kiểm tra lại file.',
    MODEL_OUTPUT_TRUNCATED:'Lượt phân tích chưa hoàn tất. Hãy thử lại hoặc dùng bảng nhỏ hơn.',
  };
  if(typeof detail==='string' && known[detail])return known[detail];
  return ({409:'Dữ liệu hoặc hội thoại đã thay đổi. Trạng thái sẽ được tải lại; hãy gửi lại câu hỏi.',429:'Hệ thống đang đủ lượt xử lý. Bạn có thể thử lại sau.',503:'Bộ xử lý chưa sẵn sàng. Vui lòng kiểm tra dịch vụ model rồi thử lại.',404:'Không tìm thấy dữ liệu hoặc khóa truy cập không còn hợp lệ.',413:'File vượt quá 20 MB.'})[status] || ('Không đọc được yêu cầu. '+(typeof detail==='string'?detail:'Hãy kiểm tra file và các trường đã nhập.'));
}
async function api(path,options={},dataset=null) {
  const headers={...options.headers};if(dataset)headers['X-Dataset-Token']=dataset.dataset_token;
  let response;try{response=await fetch(path,{...options,headers});}catch{throw new Error('Không kết nối được máy chủ. Hãy kiểm tra kết nối rồi thử lại.');}
  let body;try{body=await response.json();}catch{throw new Error('Máy chủ chưa trả được kết quả. Vui lòng thử lại.');}
  if(!response.ok){const error=new Error(errorText(response.status,body.detail));error.status=response.status;throw error;}
  return body;
}
const displayValue=value=>value===null?'Chưa có dữ liệu':typeof value==='object'?JSON.stringify(value):String(value??'');
const columnsOf=rows=>[...new Set(rows.flatMap(row=>Object.keys(row)))];
function table(rows,caption='Bảng kết quả') {
  const wrap=node('div',undefined,'table-wrap');if(!rows?.length)return wrap;
  const columns=columnsOf(rows),scroll=node('div',undefined,'table-scroll'),t=node('table'),head=node('tr');
  t.setAttribute('aria-label',caption);scroll.tabIndex=0;scroll.setAttribute('aria-label','Cuộn bảng kết quả');
  columns.forEach(c=>{const th=node('th',c);th.scope='col';head.append(th);});
  const thead=node('thead');thead.append(head);t.append(thead);
  const body=node('tbody');t.append(body);scroll.append(t);wrap.append(scroll);
  let shown=0;const footer=node('div',undefined,'table-footer'),count=node('span');
  const more=button('Xem thêm dòng',null,append,'text-button');
  function append(){
    const end=Math.min(shown+50,rows.length);
    rows.slice(shown,end).forEach(row=>{
      const tr=node('tr');columns.forEach(c=>tr.append(node('td',displayValue(row[c]),row[c]===null?'empty-value':typeof row[c]==='number'?'numeric':undefined)));body.append(tr);
    });shown=end;count.textContent='Hiển thị '+shown+' / '+rows.length+' dòng kết quả';more.hidden=shown>=rows.length;
  }
  append();footer.append(count,more);if(rows.length>8)wrap.append(footer);return wrap;
}
function dataView(rows) {
  if(rows.length===1 && Object.values(rows[0]).some(v=>typeof v==='string'&&v.length>100)) {
    const list=node('dl',undefined,'field-values');
    Object.entries(rows[0]).forEach(([name,value])=>{const group=node('div');group.append(node('dt',name),node('dd',displayValue(value),value===null?'empty-value':undefined));list.append(group);});return list;
  }
  return table(rows);
}
function locatorText(locator={}) {
  if(locator.sheet)return 'Sheet '+locator.sheet+', dòng '+locator.row+(locator.column?', cột '+locator.column:'');
  if(locator.line_start)return 'Dòng '+locator.line_start+(locator.line_end!==locator.line_start?' đến '+locator.line_end:'')+(locator.column?', cột '+locator.column:'');
  return locator.record_index?'Bản ghi '+locator.record_index+(locator.column?', cột '+locator.column:''):'Vị trí nguồn chưa được cung cấp';
}
function evidenceView(ev) {
  const detail=node('details',undefined,'evidence'),summary=node('summary');
  summary.append(icon('source'),node('span','Đối chiếu nguồn'+(Number.isInteger(ev.matching_rows)?' · '+ev.matching_rows+' dòng khớp':'')));detail.append(summary);
  const content=node('div',undefined,'evidence-content');let displayed=false;
  (ev.source_rows_preview||[]).forEach(row=>{const item=node('div',undefined,'source-record');item.append(node('strong','Bản ghi '+row.row_id),node('p',locatorText(row.locator)));content.append(item);displayed=true;});
  // Show per-cell locators even when the API provides no preview rows.
  if(!displayed)(ev.source_cells||[]).slice(0,20).forEach(source=>{
    const item=node('div',undefined,'source-record');item.append(node('strong',source.title||'Ô dữ liệu nguồn'),node('p',locatorText(source.metadata?.source_locator)));
    if(source.text)item.append(node('blockquote',source.text,'source-quote'));content.append(item);displayed=true;
  });
  if(!displayed)content.append(node('p',ev.query?'Kết quả được tính từ các dòng thỏa điều kiện truy vấn trong bảng.':'Thông tin nguồn chi tiết do bộ xử lý cung cấp.'));
  if(ev.lineage_preview_truncated)content.append(node('p','Chỉ hiển thị 20 vị trí nguồn đầu tiên.','hint'));
  const technical=node('details',undefined,'technical-detail');technical.append(node('summary','Chi tiết kỹ thuật'),node('pre',JSON.stringify(ev,null,2)));content.append(technical);detail.append(content);return detail;
}
function resultText(result) {
  const parts=[result.request_text||'Kết quả',labels[result.status]||result.status||''];
  if(result.reason)parts.push(result.reason);
  const groups=result.semantic_groups||[{data:result.data||[]}];
  groups.forEach(group=>{if(group.label)parts.push(group.label);(group.data||[]).forEach((row,i)=>parts.push((group.data.length>1?'Bản ghi '+(i+1)+'\n':'')+Object.entries(row).map(([k,v])=>k+': '+displayValue(v)).join('\n')));});
  return parts.join('\n\n');
}
function safeName(name) {return String(name||'ket-qua').replace(/[<>:"/\\|?*\u0000-\u001f]/g,'-').slice(0,90);}
function download(content,name,type) {
  const url=URL.createObjectURL(content instanceof Blob?content:new Blob([content],{type}));
  const link=node('a');link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function downloadCsv(result) {
  const rows=result.data||[];if(!rows.length)return;
  function cell(value){
    let text=value===null?'':displayValue(value);
    if(typeof value==='string'&&/^[=+\-@\t\r]/.test(text))text="'"+text;
    return '"'+text.replaceAll('"','""')+'"';
  }
  const columns=columnsOf(rows);const csv=[columns.map(cell).join(','),...rows.map(row=>columns.map(c=>cell(row[c])).join(','))].join('\r\n');
  download('\ufeff'+csv,safeName(result.request_text)+'.csv','text/csv;charset=utf-8');
}
async function copyResult(result,element) {
  try{await navigator.clipboard.writeText(resultText(result));element.replaceChildren(icon('check'),node('span','Đã sao chép'));clearTimeout(element.copyTimer);element.copyTimer=setTimeout(()=>{element.replaceChildren(icon('copy'),node('span','Sao chép'));},2000);}
  catch{notice('Trình duyệt chưa cho phép sao chép. Bạn có thể chọn văn bản hoặc tải hội thoại.',true);}
}
function renderResult(result) {
  const section=node('section',undefined,'answer'),heading=node('div',undefined,'answer-heading'),status=node('span',undefined,'status '+result.status);
  status.append(icon(result.status==='answer'?'check':result.status==='clarify'?'help':'info'),node('span',labels[result.status]||result.status));
  heading.append(node('h3',result.request_text||'Kết quả'),status);section.append(heading);
  if(result.reason)section.append(node('p',result.reason,'answer-reason'));
  if(result.semantic_groups){
    section.append(node('p','Phân loại tự động theo nguồn; hãy đối chiếu điều kiện trong từng dòng.','group-note'));
    result.semantic_groups.forEach(group=>{section.append(node('h4',group.label));if(group.data?.length)section.append(dataView(group.data));});
  }else if(result.data?.length)section.append(dataView(result.data));
  if(result.evidence && !Array.isArray(result.evidence))section.append(evidenceView(result.evidence));
  const tools=node('div',undefined,'answer-tools');
  const copy=button('Sao chép','copy',()=>copyResult(result,copy),'text-button');tools.append(copy);
  if(result.data?.length)tools.append(button('Tải CSV','download',()=>downloadCsv(result),'text-button'));section.append(tools);return section;
}
function assistantHeading() {
  const heading=node('div',undefined,'assistant-heading'),mark=node('span',undefined,'assistant-mark');
  mark.append(icon('table'));heading.append(mark,node('span','Trợ lý dữ liệu'));return heading;
}
function suggestions() {
  const columns=active?.profile?.physical?.columns||[];
  const questions=['Bảng này có bao nhiêu dòng?'];
  const measure=columns.find(c=>['BIGINT','DOUBLE'].includes(c.type) && c.name!=='_row_id');
  if(measure)questions.push('Giá trị trung bình của cột "'+measure.name+'" là bao nhiêu?');
  if(columns.length)questions.push('Liệt kê '+columns.slice(0,Math.min(2,columns.length)).map(c=>'"'+c.name+'"').join(' và ')+' trong bảng.');
  return questions;
}
function welcomeView() {
  const box=node('div',undefined,'welcome');box.id='welcome';
  if(!active){
    const symbol=node('div',undefined,'welcome-symbol');symbol.append(icon('chat'));box.append(symbol,node('h2','Hiểu dữ liệu bằng một câu hỏi.'),node('p','Tải bảng của bạn lên. Tra cứu, lọc danh sách hoặc tính toán, kèm nguồn để đối chiếu.'));
    const drop=node('div',undefined,'welcome-upload');drop.setAttribute('role','button');drop.tabIndex=0;
    const text=node('div');text.append(node('h3','Bắt đầu với CSV hoặc Excel'),node('p','Kéo thả một file vào đây, hoặc bấm để chọn.'));
    drop.append(text,icon('upload'));drop.onclick=openUpload;drop.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openUpload();}};
    drop.ondragover=e=>e.preventDefault();drop.ondrop=e=>{e.preventDefault();openUpload();if(e.dataTransfer.files.length===1){pickFile(e.dataTransfer.files[0]);if(selectedFile){const t=new DataTransfer();t.items.add(selectedFile);$('file').files=t.files;}}else uploadError('Chọn một file mỗi lần tải lên.');};
    box.append(drop,node('p','Hỗ trợ tối đa 20 MB, 32 cột và 100.000 ô mỗi bảng.','welcome-note'));
  } else {
    const ready=node('div',undefined,'ready-label');ready.append(icon('check'),node('span','Dữ liệu đã sẵn sàng'));box.append(ready,node('h2','Bạn muốn tìm hiểu điều gì?'),node('p','Hỏi thông tin trong bảng bằng ngôn ngữ tự nhiên. Bạn có thể bắt đầu với một gợi ý bên dưới.'));
    const list=node('div',undefined,'suggestions');suggestions().forEach(q=>list.append(button(q,'right',()=>{$('question').value=q;updateComposer();$('question').focus();},'suggestion')));box.append(list);
  }
  return box;
}
function renderMessages() {
  const box=$('messages');box.replaceChildren();
  const refs=active?.conversation?.recent_entities||[];
  const context=refs.map(r=>r.identified_by?.map(p=>p.value).join(', ')||'Bản ghi '+r.row_id);
  $('context-hint').textContent=context.length?'Đang nhớ: '+context.join(' / ')+'. Có thể hỏi tiếp về các bản ghi này.':'Tối đa 8 intent mỗi tin nhắn. Có thể hỏi tiếp về 2 bản ghi gần nhất.';
  if(!active?.messages?.length && !pending)box.append(welcomeView());
  (active?.messages||[]).forEach(message=>{
    const article=node('article',undefined,'message');article.append(node('p',message.question,'user-message'),assistantHeading());
    (message.results||[]).forEach(result=>article.append(renderResult(result)));
    if(typeof message.seconds==='number'){const foot=node('p',undefined,'turn-footer');foot.append(icon('clock'),node('span','Xử lý trong '+message.seconds+' giây'));article.append(foot);}
    box.append(article);
  });
  if(pending && pending.datasetId===active?.dataset_id){
    const article=node('article',undefined,'message');article.id='pending-message';article.append(node('p',pending.question,'user-message'),assistantHeading());
    if(pending.error){
      const error=node('div',undefined,'pending-error');error.setAttribute('role','alert');error.append(node('p',pending.error),button('Thử lại','refresh',()=>submitQuestion(pending.question),'text-button'));article.append(error);
    }else{
      const loading=node('div',undefined,'pending-answer');loading.setAttribute('role','status');loading.append(node('strong','Đang đối chiếu với dữ liệu…'),node('div',undefined,'skeleton'),node('div',undefined,'skeleton short'));article.append(loading);
    }box.append(article);
  }
}
function renderDatasets() {
  const folded=value=>String(value).normalize('NFD').replace(/\p{M}/gu,'').toLowerCase().replaceAll('đ','d');
  const nav=$('datasets'),search=folded($('dataset-search').value);
  nav.replaceChildren();$('dataset-count').textContent=datasets.length;
  const shown=datasets.filter(d=>folded(d.name||'Bảng dữ liệu').includes(search));
  if(!shown.length)nav.append(node('p',datasets.length?'Không có bảng khớp tên tìm kiếm.':'Chưa có bảng nào.\nThêm dữ liệu để bắt đầu.','empty-datasets'));
  shown.forEach(d=>{
    const b=node('button',undefined,'dataset-button');b.type='button';b.title=d.name||'Bảng dữ liệu';b.setAttribute('aria-current',String(d===active));
    const mark=node('span',undefined,'dataset-icon');mark.append(icon(/\.xlsx$/i.test(d.original_name||d.name)?'excel':'file'));
    const text=node('span',undefined,'dataset-label');text.append(node('span',d.name||'Bảng dữ liệu','dataset-name'));
    const p=d.profile?.physical;text.append(node('span',p?p.row_count+' dòng · '+p.columns.length+' cột':'Bảng dữ liệu','dataset-meta'));b.append(mark,text);
    b.onclick=()=>selectDataset(d);b.disabled=busy;nav.append(b);
  });
}
$('dataset-search').oninput=renderDatasets;
function renderProfile() {
  const content=$('profile-content');content.replaceChildren();const p=active?.profile?.physical;if(!p)return;
  $('profile-subtitle').textContent=active.original_name||active.name;
  const stats=node('div',undefined,'profile-summary');
  [[p.row_count,'dòng dữ liệu'],[p.columns.length,'cột thông tin'],[p.columns.reduce((s,c)=>s+(c.null_count||0),0),'ô thiếu']].forEach(([v,label])=>{const item=node('div');item.append(node('strong',v),node('span',label));stats.append(item);});content.append(stats);
  const pkg=active.profile.package;
  if(pkg)content.append(node('p','Đã tạo package với liên kết về từng ô nguồn. Metadata tự động vẫn cần được đối chiếu.','profile-notice'));
  const section=node('section',undefined,'profile-section');section.append(node('h3','Các cột trong bảng'));
  const search=node('input');search.type='search';search.placeholder='Tìm tên cột…';search.setAttribute('aria-label','Tìm tên cột');section.append(search);
  const list=node('div',undefined,'column-list');section.append(list);
  const fields=pkg?.mapping?.fields||[];
  function paint(){
    list.replaceChildren();const needle=search.value.toLocaleLowerCase('vi');
    p.columns.filter(c=>c.name.toLocaleLowerCase('vi').includes(needle)).forEach(c=>{
      const row=node('div',undefined,'column-item'),head=node('div',undefined,'column-heading');head.append(node('strong',c.name),node('span',c.type,'column-type'));row.append(head);
      const f=fields.find(f=>f.column===c.name);if(f?.description && f.description!==c.name)row.append(node('p',f.description,'column-description'));
      const meta=node('div',undefined,'column-meta');meta.append(node('span',(c.null_count||0)+' ô thiếu'));
      if(c.distinct_count!==undefined)meta.append(node('span',c.distinct_count+' giá trị phân biệt'));row.append(meta);
      if(c.examples?.length)row.append(node('p','Ví dụ: '+c.examples.slice(0,3).map(displayValue).join(' / '),'column-samples'));list.append(row);
    });
    if(!list.children.length)list.append(node('p','Không có cột khớp tên tìm kiếm.','hint'));
  }
  search.oninput=paint;paint();content.append(section);
  const info=node('section',undefined,'profile-section');info.append(node('h3','Phạm vi hỏi đáp'),node('p','Tra cứu bản ghi, lọc theo giá trị, đếm và tính tổng hợp. Mỗi câu hỏi dùng bảng đang chọn. Những điều kiện cần diễn giải ngoại lệ trong văn bản có thể cần hỏi lại.'));content.append(info);
  if(pkg)content.append(button('Tải gói dữ liệu','download',downloadPackage,'secondary package-download'));
  const technical=node('details',undefined,'technical-detail');technical.append(node('summary','Phiên bản và thông tin kỹ thuật'),node('pre',JSON.stringify({dataset_id:active.dataset_id,source_sha256:active.version,package_version:pkg?.version,format:p.format,missing_values:p.missing_values},null,2)));content.append(technical);
}
function render() {
  $('title').textContent=active?.name||'Hỏi từ dữ liệu của bạn';const p=active?.profile?.physical;
  $('subtitle').textContent=p?p.row_count+' dòng dữ liệu · '+p.columns.length+' cột':'Một nơi để tra cứu và đối chiếu nguồn.';
  renderDatasets();renderProfile();renderMessages();lock(busy);
}
function updateComposer() {
  const input=$('question');input.style.height='auto';input.style.height=Math.min(180,Math.max(62,input.scrollHeight))+'px';
  $('character-count').textContent=input.value.length+' / 6000';$('character-count').hidden=input.value.length<5400;
  $('send').disabled=busy||!active||!input.value.trim();if(active)active.draft=input.value;
}
$('question').oninput=updateComposer;
$('question').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!busy && active && $('question').value.trim())$('question-form').requestSubmit();}};
async function selectDataset(d) {
  if(busy)return;if(active)active.draft=$('question').value;
  pending=null;active=d;$('question').value=d.draft||'';setSidebar(false,false);closeMenu();render();updateComposer();lock(true);notice();
  try{
    const info=await api(route(d),{},d);if(info.version!==d.version)throw new Error('Phiên bản dữ liệu đã thay đổi. Hãy tải lại bảng để tiếp tục.');
    d.profile=info.profile;
    if(d.conversation)d.conversation=await api(route(d)+'/conversations/'+d.conversation.conversation_id,{},d);
    persist();
  }catch(error){notice(error.message,true);}
  finally{lock(false);render();updateComposer();}
}
async function newConversation(d) {
  const state=await api(route(d)+'/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:d.version})},d);
  d.conversation=state;d.messages=[];d.draft='';pending=null;
}
$('upload-form').onsubmit=async event=>{
  event.preventDefault();if(busy)return;const file=selectedFile||$('file').files[0];if(!file)return;
  pickFile(file);if(!selectedFile)return;
  uploading=true;lock(true);uploadError('');
  try{
    const params=new URLSearchParams({filename:file.name});if($('sheet').value.trim())params.set('sheet',$('sheet').value.trim());
    params.set('missing_values',JSON.stringify(['',...$('missing').value.split(',').map(v=>v.trim()).filter(Boolean)]));
    const d=await api('/v1/datasets?'+params,{method:'POST',body:file,headers:{'Content-Type':'application/octet-stream'}});
    if(active)active.draft=$('question').value;
    d.name=file.name;d.original_name=file.name;d.messages=[];datasets.push(d);active=d;pending=null;$('question').value='';persist();
    // Preserve a successful upload even if starting its conversation fails.
    try{await newConversation(d);}catch(error){notice('Bảng đã tải lên. Chưa tạo được hội thoại: '+error.message,true);}
    persist();$('dataset-search').value='';$('upload-panel').close();$('upload-form').reset();selectedFile=null;pickFile(null);
    $('file-label').textContent='Kéo thả bảng dữ liệu vào đây';$('file-description').textContent='hoặc bấm để chọn file';$('drop-zone').classList.remove('has-file');
  }catch(error){uploadError(error.message);}
  finally{uploading=false;lock(false);render();updateComposer();}
};
$('view-profile').onclick=()=>{if(active){renderProfile();openDialog('profile');}};
async function downloadPackage() {
  if(!active||busy)return;const d=active;lock(true);
  try{
    const response=await fetch(route(d)+'/package',{headers:{'X-Dataset-Token':d.dataset_token}});
    if(!response.ok){let data={};try{data=await response.json();}catch{}throw new Error(errorText(response.status,data.detail));}
    download(await response.blob(),safeName(d.name.replace(/\.[^.]+$/,''))+'-package.zip','application/zip');
  }catch(error){notice(error.message||'Chưa tải được gói dữ liệu.',true);}
  finally{lock(false);}
}
$('new-chat').onclick=()=>{if(!active||busy)return;if(active.messages?.length)openDialog('confirm-dialog');else resetChat();};
async function resetChat() {
  if(!active||busy)return;lock(true);
  try{await newConversation(active);$('confirm-dialog').close();$('question').value='';persist();renderMessages();notice();updateComposer();}
  catch(error){notice(error.message,true);}
  finally{lock(false);$('question').focus();}
}
$('confirm-new-chat').onclick=resetChat;
$('rename-data').onclick=()=>{if(!active)return;closeMenu();$('dataset-name').value=active.name||'';openDialog('rename-dialog');$('dataset-name').select();};
$('rename-form').onsubmit=event=>{event.preventDefault();if(!active)return;const name=$('dataset-name').value.trim();if(!name){$('dataset-name').setCustomValidity('Hãy nhập tên bảng.');$('dataset-name').reportValidity();return;}active.original_name||=active.name;active.name=name;persist();$('rename-dialog').close();render();};
$('dataset-name').oninput=()=>$('dataset-name').setCustomValidity('');
$('export-chat').onclick=()=>{
  closeMenu();if(!active?.messages?.length){notice('Chưa có câu trả lời để tải xuống.');return;}
  const content=['# Hội thoại: '+active.name,'Nguồn SHA-256: '+active.version,''];
  active.messages.forEach(message=>{content.push('## Câu hỏi\n\n'+message.question);(message.results||[]).forEach(result=>{
    content.push('### '+(result.request_text||'Kết quả'),resultText(result));
    if(result.evidence && !Array.isArray(result.evidence))content.push('Bằng chứng nguồn:\n\n'+JSON.stringify(result.evidence,null,2));
  });});
  download(content.join('\n\n'),safeName(active.name)+'-hoi-thoai.md','text/markdown;charset=utf-8');
};
async function submitQuestion(question) {
  if(busy||!active||!question.trim())return;const d=active;question=question.trim();notice();closeMenu();
  pending={datasetId:d.dataset_id,question};lock(true);renderMessages();
  $('pending-message')?.scrollIntoView({block:'start',behavior:'instant'});
  try{
    if(!d.conversation)await newConversation(d);
    const response=await api(route(d)+'/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,version:d.version,conversation_id:d.conversation.conversation_id,revision:d.conversation.revision})},d);
    d.conversation=response.conversation;
    d.messages=[...(d.messages||[]),{question,results:response.results,seconds:response.seconds}].slice(-20);
    pending=null;d.draft='';$('question').value='';persist();renderMessages();updateComposer();
    $('messages').lastElementChild?.scrollIntoView({block:'start',behavior:'instant'});
  }catch(error){
    pending={datasetId:d.dataset_id,question,error:error.message};renderMessages();
    if(error.status===409 && d.conversation){try{d.conversation=await api(route(d)+'/conversations/'+d.conversation.conversation_id,{},d);persist();}catch{/* Keep the original error and draft for retry. */}}
  }finally{lock(false);updateComposer();$('question').focus({preventScroll:true});}
}
$('question-form').onsubmit=event=>{event.preventDefault();submitQuestion($('question').value);};
render();
if(datasets.length){
  let saved;try{saved=sessionStorage.getItem(activeKey);}catch{}
  selectDataset(datasets.find(d=>d.dataset_id===saved)||datasets[datasets.length-1]);
}
