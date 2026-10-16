// UI acceptance with explicit mock APIs. No LLM score or inference timing is claimed.
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const assert=require('node:assert/strict');
const {createRequire}=require('node:module');
const {chromium}=createRequire(path.resolve(__dirname,'../../tools/package.json'))('@playwright/test');
const root=path.join(__dirname,'static');
const output=path.resolve(__dirname,'../../.runtime/g85-tabular-lab/ui-redesign-20261016');
const baseline=process.argv.includes('--baseline');
const backup=process.env.G85_UI_BASELINE;
const mime={'.html':'text/html;charset=utf-8','.css':'text/css;charset=utf-8','.js':'application/javascript;charset=utf-8','.svg':'image/svg+xml','.woff2':'font/woff2'};
const server=http.createServer((request,response)=>{
  const url=new URL(request.url,'http://localhost');
  const relative=url.pathname==='/'?'index.html':url.pathname.replace(/^\/workspace\//,'');
  const selected=baseline&&backup?backup:root;
  const file=path.resolve(selected,relative);
  if(!file.startsWith(path.resolve(selected)+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){response.writeHead(404);response.end();return;}
  response.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream'});fs.createReadStream(file).pipe(response);
});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  fs.mkdirSync(output,{recursive:true});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  const checks=[],errors=[];
  try{
    const context=await browser.newContext({viewport:{width:1440,height:1000},colorScheme:'light',permissions:['clipboard-read','clipboard-write'],acceptDownloads:true});
    const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
    if(baseline){await page.goto(base);await page.screenshot({path:path.join(output,'before-empty.png'),fullPage:true});return;}
    const records=new Map();let created=0,queryCount=0,failNext=false,failUpload=false;
    const cells=Array.from({length:58},(_,i)=>({car_name:i===0?'toyota corolla 1200':i===1?'=HYPERLINK("bad")':i===2?'<img src=x onerror=alert(1)>':'toyota '+(i+1),mpg:i===3?null:24+i%9}));
    function fixture(id,name){return {dataset_id:id,version:'a'.repeat(64),dataset_token:'private-test-token-'+id,name,status:'ready',profile:{physical:{row_count:398,columns:[{name:'car_name',type:'VARCHAR',null_count:0,distinct_count:305,examples:['toyota corolla 1200','ford torino']},{name:'mpg',type:'DOUBLE',null_count:0,distinct_count:129,examples:['24','32']},{name:'cylinders',type:'BIGINT',null_count:0,distinct_count:5,examples:['4','6','8']}],missing_values:[''],format:{type:'csv',delimiter:','}},package:{rows:398,version:'c'.repeat(64),mapping:{fields:[{column:'car_name',description:'Tên xe trong bảng nguồn'},{column:'mpg',description:'Mức tiêu thụ nhiên liệu theo chỉ số MPG'}]}},semantic_proposal:{domain:'Xe ô tô'}}};}
    await page.route('**/v1/datasets**',async route=>{
      const request=route.request(),url=new URL(request.url());
      let body,status=200;
      if(url.pathname==='/v1/datasets'){
        if(failUpload){failUpload=false;await route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({detail:'MULTIPLE_SHEETS_REQUIRE_SELECTION'})});return;}
        created++;const id=String(created).padStart(32,'0');body=fixture(id,url.searchParams.get('filename'));
        records.set(id,{dataset:body,revision:0});status=201;
        assert.equal(request.headers()['content-type'],'application/octet-stream');
        assert.deepEqual(JSON.parse(url.searchParams.get('missing_values')),['']);
        await delay(120);
      }else{
        const id=url.pathname.split('/')[3],record=records.get(id);assert(record);
        assert.equal(request.headers()['x-dataset-token'],record.dataset.dataset_token);
        if(url.pathname.endsWith('/package')){await route.fulfill({contentType:'application/zip',body:Buffer.from('mock-zip-package')});return;}
        if(url.pathname.endsWith('/query')){
          queryCount++;const input=request.postDataJSON();assert.equal(input.revision,record.revision);
          if(failNext){failNext=false;record.revision++;await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'CONVERSATION_REVISION_MISMATCH'})});return;}
          await delay(200);record.revision++;
          body={dataset_id:id,version:record.dataset.version,seconds:3.48,conversation:{conversation_id:id,revision:record.revision,recent_entities:[{row_id:1,identified_by:[{column:'car_name',value:'toyota corolla 1200'}]}]},results:[{request_text:input.question,status:'answer',data:cells,evidence:{matching_rows:58,source_sha256:'a'.repeat(64),source_rows_preview:[{row_id:1,locator:{line_start:2,line_end:2}}],query:'SELECT car_name, mpg FROM data WHERE cylinders = ?',params:[4]}}]};
          if(input.question==='Giấy tờ của A và lệ phí của B?')body.results=[
            {request_text:'Hồ sơ của bản ghi A',status:'answer',data:[{'Thành phần hồ sơ':'- Giấy tờ xác nhận thông tin theo mẫu trong nguồn.\n- Bản sao giấy tờ định danh và bản chính để đối chiếu.\n- Tài liệu bổ sung khi thuộc trường hợp được ghi trong bảng.'}],evidence:{matching_rows:1,source_cells:[{title:'Bản ghi A / Thành phần hồ sơ',metadata:{source_locator:{sheet:'Sheet1',row:2,column:'Thành phần hồ sơ'}}}]}},
            {request_text:'Lệ phí của bản ghi B',status:'partial',reason:'Có mức phí cơ bản; bảng chưa ghi mức phí cho trường hợp bổ sung.',data:[{'Lệ phí':0,'Mức phí bổ sung':null}],evidence:{matching_rows:1,source_rows_preview:[{row_id:2,locator:{sheet:'Sheet1',row:3}}]}},
          ];
        }else if(url.pathname.includes('/conversations')){
          if(request.method()==='POST')record.revision=0;
          body={conversation_id:id,version:record.dataset.version,revision:record.revision,recent_entities:[]};
        }else{body=record.dataset;}
      }
      await route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    });
    await page.goto(base);await page.evaluate(()=>document.fonts.ready);
    await page.screenshot({path:path.join(output,'empty-desktop.png'),fullPage:true});
    assert(await page.getByRole('heading',{name:'Hiểu dữ liệu bằng một câu hỏi.'}).isVisible());checks.push('empty state and local fonts');
    await page.locator('#add-data').click();await page.locator('#upload-panel').waitFor({state:'visible'});
    await page.screenshot({path:path.join(output,'upload-desktop.png')});
    await page.keyboard.press('Tab');assert(await page.evaluate(()=>document.getElementById('upload-panel').contains(document.activeElement)));
    await page.keyboard.press('Escape');assert(!(await page.locator('#upload-panel').isVisible()));checks.push('modal focus and Escape');
    await page.locator('#add-data').click();
    await page.locator('#file').setInputFiles({name:'wrong.txt',mimeType:'text/plain',buffer:Buffer.from('test')});
    await page.getByText('Hãy chọn file CSV hoặc Excel (.xlsx).',{exact:true}).waitFor();checks.push('invalid file type feedback');
    // Drop a CSV through the actual drop handler, rather than only file input.
    await page.locator('#drop-zone').evaluate(element=>{
      const data=new DataTransfer();data.items.add(new File(['car_name,mpg,cylinders\ntoyota corolla 1200,24,4\n'],'auto-mpg.csv',{type:'text/csv'}));
      element.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:data}));
    });
    await page.getByRole('button',{name:'Tải lên và phân tích',exact:true}).click();
    await page.waitForFunction(()=>!document.getElementById('question').disabled);checks.push('drag-and-drop upload and conversation creation');
    await page.getByRole('button',{name:'Bảng này có bao nhiêu dòng?',exact:true}).click();assert.equal(await page.locator('#question').inputValue(),'Bảng này có bao nhiêu dòng?');checks.push('schema-based question suggestions');
    await page.locator('#question').fill('Liệt kê tên xe (car_name) và mpg của những xe có 4 xi-lanh (cylinders = 4).');
    await page.locator('#question').press('Shift+Enter');assert((await page.locator('#question').inputValue()).includes('\n'));
    await page.locator('#question').press('Enter');await page.getByText('Đã trả lời',{exact:true}).waitFor();
    assert.equal(queryCount,1);assert.equal(await page.locator('#messages tbody tr').count(),50);checks.push('Enter send, Shift Enter newline, pending and answer');
    assert.equal(await page.locator('#messages img').count(),0);assert.equal(await page.getByRole('cell',{name:'<img src=x onerror=alert(1)>',exact:true}).count(),1);checks.push('source cells render as text, not HTML');
    await page.getByRole('button',{name:'Xem thêm dòng'}).click();assert.equal(await page.locator('#messages tbody tr').count(),58);checks.push('result pagination preserves all returned rows');
    await page.locator('.evidence>summary').click();await page.getByText('Dòng 2',{exact:true}).waitFor();
    await page.locator('.technical-detail>summary').first().click();await page.getByText('Chi tiết kỹ thuật',{exact:true}).waitFor();checks.push('human-readable source locations and technical detail');
    await page.getByRole('button',{name:'Sao chép',exact:true}).click();assert((await page.evaluate(()=>navigator.clipboard.readText())).includes('toyota corolla'));checks.push('copy answer');
    const csvDownload=page.waitForEvent('download');await page.getByRole('button',{name:'Tải CSV',exact:true}).click();const csv=await csvDownload;const csvPath=await csv.path();const csvText=fs.readFileSync(csvPath,'utf8');assert(csvText.includes("'=HYPERLINK"));assert(csvText.includes('toyota corolla'));assert(!csvText.includes('private-test-token'));checks.push('CSV download, formula escaping and no credentials');
    await page.locator('.evidence>summary').click();
    await page.locator('#view-profile').click();await page.getByRole('heading',{name:'Cấu trúc và lưu ý dữ liệu'}).waitFor();
    await page.getByRole('searchbox',{name:'Tìm tên cột'}).fill('mpg');assert.equal(await page.locator('.column-item').count(),1);checks.push('schema drawer and column search');
    const zipDownload=page.waitForEvent('download');await page.getByRole('button',{name:'Tải gói dữ liệu',exact:true}).click();assert((await zipDownload).suggestedFilename().endsWith('-package.zip'));checks.push('authenticated package download');
    await page.keyboard.press('Escape');await page.locator('#conversation-menu-button').click();
    const chatDownload=page.waitForEvent('download');await page.locator('#export-chat').click();const chat=await chatDownload;const chatText=fs.readFileSync(await chat.path(),'utf8');assert(chatText.includes('toyota corolla'));assert(!chatText.includes('private-test-token'));checks.push('Markdown conversation export without tokens');
    await page.locator('#conversation-menu-button').click();await page.locator('#rename-data').click();await page.locator('#dataset-name').fill('Xe ô tô · dữ liệu thử');await page.getByRole('button',{name:'Lưu tên',exact:true}).click();assert.equal(await page.locator('#title').innerText(),'Xe ô tô · dữ liệu thử');checks.push('local display-name rename');
    await page.reload();await page.waitForFunction(()=>!document.getElementById('question').disabled);await page.getByText('Đã trả lời',{exact:true}).waitFor();assert.equal(await page.locator('#title').innerText(),'Xe ô tô · dữ liệu thử');checks.push('existing session format and active dataset restored');
    await page.keyboard.press('Control+k');assert.equal(await page.locator('#dataset-search').evaluate(e=>document.activeElement===e),true);await page.locator('#dataset-search').fill('khong-co');assert.equal(await page.locator('#datasets button').count(),0);await page.locator('#dataset-search').fill('');checks.push('dataset search and keyboard shortcut');
    await page.locator('#question').fill('Câu hỏi thử lỗi revision.');failNext=true;await page.locator('#question').press('Enter');await page.getByRole('button',{name:'Thử lại',exact:true}).waitFor();assert.equal(await page.locator('#question').inputValue(),'Câu hỏi thử lỗi revision.');await page.getByRole('button',{name:'Thử lại',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('question').disabled);assert.equal(await page.getByText('Đã trả lời',{exact:true}).count(),2);checks.push('409 revision recovery, preserved draft and retry');
    await page.locator('#add-data').click();await page.locator('#file').setInputFiles({name:'loans.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',buffer:Buffer.from('mock')});failUpload=true;await page.locator('#upload-submit').click();await page.getByText('File có nhiều sheet. Mở “Tùy chọn nhập dữ liệu” và nhập tên sheet muốn dùng.',{exact:true}).waitFor();await page.keyboard.press('Escape');checks.push('actionable upload error without losing current dataset');
    // A second dataset; keep each history scoped to its dataset and preserve a draft.
    await page.locator('#question').fill('Bản nháp chưa gửi');await page.locator('#add-data').click();await page.locator('#file').setInputFiles({name:'bang-thu-hai.csv',mimeType:'text/csv',buffer:Buffer.from('car_name,mpg\nford,20\n')});await page.locator('#upload-submit').click();await page.waitForFunction(()=>!document.getElementById('question').disabled);assert.equal(await page.locator('#title').innerText(),'bang-thu-hai.csv');assert.equal(await page.locator('#messages .message').count(),0);
    await page.locator('#datasets button').first().click();await page.waitForFunction(()=>!document.getElementById('question').disabled);assert.equal(await page.locator('#messages .message').count(),2);assert.equal(await page.locator('#question').inputValue(),'Bản nháp chưa gửi');checks.push('dataset isolation, switching and per-dataset draft');
    await page.locator('#question').fill('');await page.locator('#question').dispatchEvent('input');await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(output,'chat-desktop-light.png'),fullPage:true});
    await page.locator('#theme-toggle').click();await page.locator('#theme-toggle').click();assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');await page.screenshot({path:path.join(output,'chat-desktop-dark.png'),fullPage:true});
    await page.reload();await page.waitForFunction(()=>!document.getElementById('question').disabled);assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');checks.push('dark theme persistence');
    const responsive=[];
    for(const width of [390,320,768,1024,1440]){
      await page.setViewportSize({width,height:844});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'overflow at '+width);
      responsive.push(width);
    }
    await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(output,'chat-mobile-dark.png'),fullPage:true});
    await page.locator('#open-sidebar').click();assert(await page.locator('#dataset-search').isVisible());await page.keyboard.press('Escape');assert.equal(await page.locator('#open-sidebar').getAttribute('aria-expanded'),'false');checks.push('responsive no overflow at '+responsive.join('/')+' and mobile navigation');
    await page.locator('#view-profile').click();assert(await page.locator('#profile').isVisible());await page.screenshot({path:path.join(output,'schema-mobile.png')});await page.keyboard.press('Escape');checks.push('mobile schema drawer');
    await page.locator('#new-chat').click();await page.getByRole('heading',{name:'Bắt đầu cuộc trò chuyện mới?'}).waitFor();await page.getByRole('button',{name:'Quay lại',exact:true}).click();assert.equal(await page.locator('#messages .message').count(),2);
    await page.locator('#new-chat').click();await page.locator('#confirm-new-chat').click();await page.getByRole('heading',{name:'Bạn muốn tìm hiểu điều gì?',exact:true}).waitFor();checks.push('new-chat confirmation and context reset');
    await page.locator('#question').fill('Giấy tờ của A và lệ phí của B?');await page.locator('#question').press('Enter');await page.waitForFunction(()=>!document.getElementById('question').disabled);
    assert.equal(await page.locator('#messages section.answer').count(),2);assert.equal(await page.locator('.field-values').count(),1);await page.getByText('Trả lời một phần',{exact:true}).waitFor();assert.equal(await page.getByRole('cell',{name:'0',exact:true}).count(),1);assert.equal(await page.getByRole('cell',{name:'Chưa có dữ liệu',exact:true}).count(),1);
    await page.locator('.evidence>summary').first().click();await page.getByText('Sheet Sheet1, dòng 2, cột Thành phần hồ sơ',{exact:true}).waitFor();checks.push('multi-intent, readable long prose, partial status, zero and null');
    await page.setViewportSize({width:1440,height:1000});await page.locator('#theme-toggle').click();await page.waitForTimeout(220);await page.screenshot({path:path.join(output,'multi-intent-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.screenshot({path:path.join(output,'multi-intent-mobile.png'),fullPage:true});
    assert.equal(errors.length,0,errors.join('\n'));
    fs.writeFileSync(path.join(output,'checks.json'),JSON.stringify({passed:checks.length,checks,api:'mocked explicitly; not LLM inference',page_errors:errors},null,2));
    console.log(JSON.stringify({passed:checks.length,artifacts:output}));
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;server.close();});
