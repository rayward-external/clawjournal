// Pure DOM-contract tests; no browser, network or real conversation is used.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

test('consent starts off, requires every checkbox, and preserves Unicode labels', async () => {
  const html = readFileSync(path.join(__dirname, '../clawjournal/redaction/contribution_review.html'), 'utf8');
  const script = html.match(/<script nonce="snippet-review">([\s\S]*?)<\/script>/)[1];
  class Element {
    constructor() { this.children = []; this.handlers = {}; this.checked = false; this.disabled = false; this.value = ''; }
    set textContent(value) { this.value = String(value); this.children = []; }
    get textContent() { return this.value + this.children.map(child => child.textContent).join(''); }
    append(child) { this.children.push(child); }
    remove() {}
    replaceChildren() {this.children=[];}
    addEventListener(name, fn) { this.handlers[name] = fn; }
    click() { if (!this.disabled && this.handlers.click) return this.handlers.click(); }
  }
  const ids = ['contribution-data','original','split','proposed','labels','rights','reviewed_context','reviewed_labels','consent','approve','decline','instructions','status'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  elements.approve.disabled = true; // matches the initial HTML attribute
  assert.match(html, /<button id="approve" disabled>/);
  assert.doesNotMatch(html, /type="checkbox"[^>]*\bchecked\b/);
  const target = 'Mira Fenwick';
  const data = {proposal_id:'test-proposal', snapshot_hash:'hash', consent_version:'test-consent',
    original:'🧪 <script>private fixture</script>', payload:{split:'dev', contributor_id:'test-pseudonym',
      text:'🧪 '+target, labels:[{start:2,end:2+target.length,decision:'redact',entity_type:'person'}]}};
  elements['contribution-data'].textContent = JSON.stringify(data);
  let downloaded;
  const document = {body:new Element(),getElementById:id=>{assert.ok(elements[id]); return elements[id];},
    createElement:()=>new Element(), createTextNode:text=>({textContent:text})};
  vm.runInNewContext(script, {document, window:{}, Blob, URL:{createObjectURL:blob=>{downloaded=blob;return 'blob:test';},revokeObjectURL:()=>{}},setTimeout:fn=>fn()});
  assert.equal(elements.original.textContent, data.original);
  assert.equal(elements.proposed.textContent, data.payload.text);
  assert.match(elements.labels.textContent, /redact · person: Mira Fenwick/);
  assert.equal(elements.approve.disabled, true);
  for (const id of ['rights','reviewed_context','reviewed_labels']) {
    elements[id].checked=true;elements[id].handlers.change();
    assert.equal(elements.approve.disabled, true);
  }
  elements.approve.click();assert.equal(downloaded, undefined);
  elements.consent.checked=true;elements.consent.handlers.change();
  assert.equal(elements.approve.disabled, false);
  elements.approve.click();
  const approval=JSON.parse(await downloaded.text());
  assert.deepEqual(approval,{proposal_id:'test-proposal',snapshot_hash:'hash',consent_version:'test-consent',
    rights:true,reviewed_context:true,reviewed_labels:true,consent:true});
  assert.ok(!Object.hasOwn(approval, 'original'));
  elements.decline.click();
  assert.equal(elements.approve.disabled,true);
  assert.equal(elements.consent.checked,false);
  assert.match(elements.instructions.textContent,/contribute withdraw test-proposal/);
});

function renderLocalPage(filename, nonce, dataId, data, ids, local) {
  const html = readFileSync(path.join(__dirname, '../clawjournal/redaction', filename), 'utf8');
  const script = html.match(new RegExp(`<script nonce="${nonce}">([\\s\\S]*?)<\\/script>`))[1];
  class Element {
    constructor() { this.children=[]; this.handlers={}; this.checked=false; this.disabled=false; this.value=''; }
    set textContent(value) { this.value=String(value); this.children=[]; }
    get textContent() { return this.value+this.children.map(c=>c.textContent).join(''); }
    append(child) { this.children.push(child); }
    remove() {}
    replaceChildren() {this.children=[];}
    addEventListener(name, fn) { this.handlers[name]=fn; }
    click() { if(!this.disabled && this.handlers.click)return this.handlers.click(); }
  }
  const elements=Object.fromEntries([...ids,dataId].map(id=>[id,new Element()]));
  elements[dataId].textContent=JSON.stringify(data);
  for(const match of html.matchAll(/<button id="([^"]+)" disabled>/g))elements[match[1]].disabled=true;
  let downloaded;
  const document={body:new Element(),addEventListener:()=>{},getElementById:id=>{assert.ok(elements[id],id);return elements[id];},createElement:()=>new Element(),createTextNode:text=>({textContent:text})};
  const window={clawjournalLocal:local,handlers:{},addEventListener(name,fn){this.handlers[name]=fn;}};
  vm.runInNewContext(script,{document,window,Blob,URL:{createObjectURL:blob=>{downloaded=blob;return 'blob:test';},revokeObjectURL:()=>{}},setTimeout:fn=>fn()});
  return {elements, window, download:()=>downloaded};
}

test('ongoing consent requires all acknowledgements and records permission, not label approval', async()=>{
  const data={mode:'per-snippet',nonce:'store-specific-nonce',version:'ongoing-v1',scope:'future',purpose:'training'};
  const ids=['current','rights','accept_automated_review','consent','enable','status','instructions'];
  const {elements:e,download}=renderLocalPage('contribution_settings.html','contribution-settings','settings-data',data,ids);
  assert.equal(e.enable.disabled,true);
  for(const id of ['rights','accept_automated_review']){
    e[id].checked=true;e[id].handlers.change();assert.equal(e.enable.disabled,true);
  }
  assert.equal(download(),undefined);
  e.consent.checked=true;e.consent.handlers.change();e.enable.click();
  const permission=JSON.parse(await download().text());
  assert.deepEqual(permission,{nonce:data.nonce,version:data.version,scope:data.scope,purpose:data.purpose,
    rights:true,accept_automated_review:true,consent:true});
  assert.ok(!Object.hasOwn(permission,'reviewed_labels'));
  assert.match(e.instructions.textContent,/redaction-eval --store YOUR_STORE serve/);
  assert.match(e.status.textContent,/has not changed yet/);
  const active=renderLocalPage('contribution_settings.html','contribution-settings','settings-data',{...data,mode:'ongoing'},ids);
  for(const id of ['rights','accept_automated_review','consent']){
    active.elements[id].checked=true;active.elements[id].handlers.change();
  }
  assert.equal(active.elements.enable.disabled,true);
  active.elements.enable.click();assert.equal(active.download(),undefined);
});

test('automatic proposal page cannot certify its labels as manually reviewed',()=>{
  const data={original:'fixture',payload:{text:'fixture',split:'dev',contributor_id:'id',context_review_status:'unreviewed',
    labels:[{start:0,end:7,decision:'preserve',entity_type:'non_pii',review_status:'assistant_draft'}]}};
  const ids=['original','split','proposed','labels','rights','reviewed_context','reviewed_labels','consent','approve','decline','instructions','status'];
  const {elements:e,download}=renderLocalPage('contribution_review.html','snippet-review','contribution-data',data,ids);
  for(const id of ['rights','reviewed_context','reviewed_labels','consent']){e[id].checked=true;e[id].handlers.change();}
  assert.equal(e.approve.disabled,true);
  e.approve.handlers.click();assert.equal(download(),undefined);
  assert.match(e.labels.textContent,/assistant_draft/);
  assert.match(e.status.textContent,/not been human-reviewed/);
});

test('original sharing is a separate unchecked choice with a restricted purpose',async()=>{
  const data={nonce:'pair-store-nonce',version:'redaction-improvement-originals-v1',
    purpose:'Original conversations are used only to evaluate and improve the redaction pipeline.',
    destination:'https://research.example.org',scope:'future-captures',retention_days:30};
  const ids=['purpose','destination','rights','share_original','share_redacted','approve','status','commands'];
  const {elements:e,download}=renderLocalPage('pair_settings.html','pair-settings','pair-data',data,ids);
  assert.equal(e.share_original.checked,false);
  assert.equal(e.approve.disabled,true);
  assert.equal(e.purpose.textContent,data.purpose);
  for(const field of ['rights','share_redacted']){e[field].checked=true;e[field].handlers.change();}
  assert.equal(e.approve.disabled,true);
  e.share_original.checked=true;e.share_original.handlers.change();e.approve.click();
  const permission=JSON.parse(await download().text());
  assert.deepEqual(permission,{...data,rights:true,share_original:true,share_redacted:true});
  assert.match(e.commands.textContent,/redaction-eval --store YOUR_STORE serve/);
});

test('local consent waits for persistence and keeps retry available on failure',async()=>{
  let resolve,reject;
  const calls=[];
  const local={save(action,document){calls.push({action,document});return new Promise((ok,no)=>{resolve=ok;reject=no;});}};
  const ids=['purpose','destination','rights','share_original','share_redacted','approve','status','commands'];
  const {elements:e,download}=renderLocalPage('pair_settings.html','pair-settings','pair-data',
    {nonce:'fresh',purpose:'only redaction improvement',destination:'https://example.org'},ids,local);
  for(const id of ['rights','share_original','share_redacted']){e[id].checked=true;e[id].handlers.change();}
  let pending=e.approve.click();
  assert.equal(e.approve.disabled,true);
  assert.match(e.status.textContent,/Saving/);
  assert.equal(download(),undefined);
  reject(new Error('Storage unavailable'));await pending;
  assert.match(e.status.textContent,/Save not confirmed/);
  assert.equal(e.approve.disabled,false);
  pending=e.approve.click();resolve({saved:true});await pending;
  assert.match(e.status.textContent,/permission saved on this computer/);
  assert.equal(e.approve.disabled,true);
  assert.equal(calls[0].action,'pairs-consent');
  assert.equal(calls[0].document.share_original,true);
  assert.equal(download(),undefined);
});

function correctionPage(local){
  const ids=['policy','unit','original','redacted','start','end','selection','annotations','add','decision','entity','note','human','status','download','contribution-command','contribution-instructions'];
  const result=renderLocalPage('evaluation_review.html','local-evaluation','capture-data',
    {capture_id:'capture',pair_hash:'pair',revision:0,policy:'policy',policy_text:'policy',
      original:{'/messages/0/content':'loss=0.1234'},redacted:{'/messages/0/content':'loss=0.1234'},annotations:[]},ids,local);
  result.elements.unit.value='/messages/0/content';
  return result;
}

function addCorrection(e,start,end){
  e.start.value=String(start);e.end.value=String(end);e.decision.value='preserve';
  e.entity.value='measurement';e.note.value='fixture';e.add.click();
}

function hasUnsavedWarning(window){
  let prevented=false;
  window.handlers.beforeunload({preventDefault(){prevented=true;}});
  return prevented;
}

test('correction failure and edits during save retain warnings; confirmed saves update revision',async()=>{
  let resolve,reject;const calls=[];
  const local={save(action,document){calls.push(JSON.parse(JSON.stringify(document)));return new Promise((ok,no)=>{resolve=ok;reject=no;});}};
  const {elements:e,window,download}=correctionPage(local);
  addCorrection(e,5,8);
  let pending=e.download.click();reject(new Error('Network failed'));await pending;
  assert.equal(hasUnsavedWarning(window),true);
  assert.match(e.status.textContent,/Save not confirmed/);
  pending=e.download.click();addCorrection(e,8,10);resolve({saved:true,revision:1});await pending;
  assert.equal(hasUnsavedWarning(window),true);
  assert.match(e.status.textContent,/newer edits are still unsaved/);
  pending=e.download.click();assert.equal(calls.at(-1).revision,1);resolve({saved:true,revision:2});await pending;
  assert.equal(hasUnsavedWarning(window),false);
  assert.match(e.status.textContent,/Corrections saved/);
  assert.equal(download(),undefined);
});

test('standalone correction download never clears the unsaved warning',async()=>{
  const {elements:e,window}=correctionPage();
  addCorrection(e,5,10);await e.download.click();
  assert.match(e.status.textContent,/Download requested/);
  assert.equal(hasUnsavedWarning(window),true);
});
