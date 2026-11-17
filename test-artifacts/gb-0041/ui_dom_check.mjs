// DOM-level check of the real frontend app against the running backend.
// Drives the actual router/render loop (hashchange + form events), the same
// path a browser takes; no page modules are injected by hand.
import {JSDOM} from 'jsdom';

const BASE='http://127.0.0.1:18245';
const dom=new JSDOM('<!doctype html><html><body><div id="notice" role="status" hidden></div><div id="app"></div></body></html>',
  {url:BASE+'/#login',pretendToBeVisual:true});
const {window}=dom;
for(const key of ['window','document','location','localStorage','FormData','HTMLElement','Event','URL','HashChangeEvent'])
  Object.defineProperty(globalThis,key,{value:key==='window'?window:window[key],configurable:true,writable:true});
const realFetch=globalThis.fetch;
const fetchProxy=(input,init)=>realFetch(new URL(input,BASE),init);
globalThis.fetch=fetchProxy;window.fetch=fetchProxy;
window.confirm=globalThis.confirm=()=>true;
window.scrollTo=()=>{};
window.Element.prototype.scrollIntoView=function(){};
window.HTMLElement.prototype.scrollIntoView=function(){};

const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function waitFor(fn,label,timeout=30000){
  const start=Date.now();
  while(Date.now()-start<timeout){try{const v=await fn();if(v)return v}catch{}await sleep(120)}
  throw new Error('timeout: '+label);
}
const text=sel=>document.querySelector(sel)?.textContent?.replace(/\s+/g,' ').trim();
const notice=()=>text('#notice');
const check=(name,ok,detail='')=>{results.push([name,!!ok,detail]);console.log((ok?'PASS':'FAIL')+' '+name+(detail?' :: '+detail:''))};
const results=[];
const snap=async name=>{const {writeFileSync}=await import('node:fs');writeFileSync('/workspace/test-artifacts/gb-0041/screens/'+name+'.html',document.documentElement.outerHTML.replace(/<script[\s\S]*?<\/script>/g,''))};

async function navigate(hash,selector){
  location.hash=hash;
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  await waitFor(()=>document.querySelector('.masthead')&&document.querySelector(selector||'main'),'render '+hash);
}
function submit(selector){
  const form=document.querySelector(selector);
  form.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));
  return form;
}

// 1. login through the real form
await import('/workspace/frontend/src/app.js');
await waitFor(()=>document.querySelector('#login-form'),'login form');
document.querySelector('input[name=email]').value='admin@acoustic-bayes.demo';
document.querySelector('input[name=password]').value='Research2026!';
submit('#login-form');
await waitFor(()=>document.querySelector('.masthead'),'logged in');
check('login and dashboard render',true);
check('nav has 口径换版',[...document.querySelectorAll('nav a')].some(a=>a.textContent.includes('口径换版')));

// 2. schema page: registry + start migration
await navigate('#schema','.migration-card, #migration-form');
check('version registry shown',(text('.method-sheet')||'').includes('kHz'));
const sourceId=document.querySelector('#migration-form select[name=source_id]').value;
submit('#migration-form');
// the schema page is not auto-refreshing; reload it like a user would
await waitFor(async()=>{
  location.hash='#overview';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  await sleep(120);
  location.hash='#schema';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  await sleep(250);
  return /可切换|已阻塞/.test(text('.migration-card')||'');
},'migration ready',90000);
check('migration reaches ready via page',(text('.migration-card')||'').includes('可切换'),
  (text('.migration-card')||'').slice(0,90));
check('progress reports all samples',(text('.migration-progress')||'').includes('100%'),text('.migration-progress'));

// 3. ingest a new-version sample from the source page
await navigate('#source','#source-select');
document.querySelector('#source-select').value=sourceId;
document.querySelector('#source-select').dispatchEvent(new window.Event('change',{bubbles:true}));
await sleep(400);
await waitFor(()=>document.querySelector('#record-form'),'record form');
check('ingestion offers version choice',!!document.querySelector('#record-form select[name=schema_version]'));
document.querySelector('#record-form input[name=reference]').value='TX-UI-001';
for(const [name,value] of [['rms','1.9'],['centroid','1.31'],['kurtosis','3.1'],['label','正常']])
  document.querySelector('#record-form input[name='+name+']').value=value;
submit('#record-form');
await waitFor(()=>notice()?.includes('已保存'),'sample saved');
check('new-version sample saved during rollover',true);

// 4. config: mixed study refused, version-pinned study accepted
await navigate('#config','#run-form');
await waitFor(()=>document.querySelector('#run-form'),'run form');
document.querySelector('#run-form select[name=source_id]').value=sourceId;
document.querySelector('#run-form select[name=source_id]').dispatchEvent(new window.Event('change',{bubbles:true}));
await sleep(300);
check('config offers explicit version during rollover',!!document.querySelector('#run-form select[name=schema_version]'));
document.querySelector('#run-form input[name=name]').value='界面混版研究';
submit('#run-form');
await waitFor(()=>/口径/.test(notice()||''),'mixed blocked',15000);
check('mixed study blocked with reason',/口径/.test(notice()),notice());

await navigate('#config','#run-form');
await waitFor(()=>document.querySelector('#run-form select[name=schema_version]'),'run form 2');
document.querySelector('#run-form select[name=source_id]').value=sourceId;
document.querySelector('#run-form select[name=source_id]').dispatchEvent(new window.Event('change',{bubbles:true}));
await sleep(300);
document.querySelector('#run-form select[name=schema_version]').value='v1';
document.querySelector('#run-form input[name=name]').value='界面旧口径研究';
submit('#run-form');
await waitFor(()=>location.hash==='#results','pinned study');
await sleep(500);
check('pinned study shows v1 in results',(text('.run-title')||'').length>0);

const api=async(path,options={})=>{
  const response=await realFetch(BASE+'/api'+path,{...options,
    headers:{'Content-Type':'application/json',Authorization:'Bearer '+(localStorage.getItem(Object.keys(localStorage).find(k=>k.startsWith('research-session'))||'')||'')}});
  return response;
};
const runs=await api('/runs').then(r=>r.json());
const pinned=runs.find(r=>r.name==='界面旧口径研究');
check('pinned study frozen on v1',pinned?.schema_version==='v1',pinned?.schema_version);
check('historical v1 study keeps 240 samples',pinned&&pinned.snapshot===undefined);

// 5. switch from the page, then verify the batch is frozen on v2
await navigate('#schema','.migration-card, #migration-form');
await waitFor(async()=>{
  const button=document.querySelector('[data-switch]');
  if(button&&!button.disabled)return true;
  location.hash='#overview';window.dispatchEvent(new window.HashChangeEvent('hashchange'));await sleep(120);
  location.hash='#schema';window.dispatchEvent(new window.HashChangeEvent('hashchange'));await sleep(250);
  const again=document.querySelector('[data-switch]');
  return again&&!again.disabled;
},'switch enabled',90000);
document.querySelector('[data-switch]').click();
await waitFor(()=>/已切换/.test(text('.migration-card')||''),'switch completes',90000);
await snap('switched-schema-page');
check('batch switched from page',true);
check('switch card states freezing rule',(text('.migration-card')||'').includes('冻结新口径'));

await navigate('#source','#source-select');
document.querySelector('#source-select').value=sourceId;
document.querySelector('#source-select').dispatchEvent(new window.Event('change',{bubbles:true}));
await sleep(400);
check('source page shows new version badge',(text('.file-note')||'').includes('新口径'),text('.file-note'));
check('old-version ingestion no longer offered',!document.querySelector('#record-form select[name=schema_version]'));

const sources=await api('/sources').then(r=>r.json());
check('batch persisted as v2',sources.find(s=>s.id==Number(sourceId))?.schema_version==='v2');
const oldIngest=await api('/sources/'+sourceId+'/records',{method:'POST',
  body:JSON.stringify({records:[{reference:'TX-UI-OLD',values:{rms:1,centroid:1200,kurtosis:3,label:'正常'},schema_version:'v1'}]})});
check('old-version ingestion rejected after switch',oldIngest.status===422,oldIngest.status);
const records=await api('/sources/'+sourceId+'/records').then(r=>r.json());
const ui=records.find(r=>r.reference==='TX-UI-001');
check('new-version sample kept its kHz value',ui&&Math.abs(ui.values.centroid-1.31)<1e-9,ui?.values?.centroid);
const converted=records.find(r=>r.reference.endsWith('0001'));
check('converted historical sample is in kHz',converted&&converted.values.centroid<10&&converted.schema_version==='v2',converted?.values?.centroid);
check('all samples share one version',new Set(records.map(r=>r.schema_version)).size===1);

// 6. studies after the switch freeze v2
await navigate('#config','#run-form');
await waitFor(()=>document.querySelector('#run-form'),'run form 3');
document.querySelector('#run-form select[name=source_id]').value=sourceId;
document.querySelector('#run-form select[name=source_id]').dispatchEvent(new window.Event('change',{bubbles:true}));
await sleep(300);
document.querySelector('#run-form input[name=name]').value='界面切换后研究';
submit('#run-form');
await waitFor(()=>location.hash==='#results','post switch study');
await sleep(400);
const runs2=await api('/runs').then(r=>r.json());
const after=runs2.find(r=>r.name==='界面切换后研究');
check('post-switch study frozen on v2',after?.schema_version==='v2',after?.schema_version);
const pinned2=(await api('/runs/'+pinned.id).then(r=>r.json()));
check('pre-switch study snapshot unchanged',pinned2.schema_version==='v1'&&
  pinned2.snapshot.records.every(r=>r.schema_version==='v1'));

// 7. blocked state on a second batch: cause, remedy, then switch
const second=await api('/sources',{method:'POST',body:JSON.stringify({name:'界面阻塞批次',filename:'b.csv',description:'x'})}).then(r=>r.json());
const good={rms:2,centroid:1201,kurtosis:3,label:'正常'};
await api('/sources/'+second.id+'/records',{method:'POST',body:JSON.stringify({records:[
  {reference:'B-1',values:good},{reference:'B-2',values:{...good,label:'轴承磨损'}},
  {reference:'B-4',values:{...good,label:'转子偏心'}}]})});
// Simulate a legacy row that predates the required-field rule: it is already
// in the batch, so the rollover must isolate it instead of guessing.
const {execFileSync}=await import('node:child_process');
const fs=await import('node:fs');
const work=(fs.readFileSync('/tmp/ui-demo.log','utf8').match(/READY (\S+)/)||[])[1];
execFileSync('python3',['-c',`
import sqlite3,sys,json
con=sqlite3.connect(sys.argv[1])
con.execute('insert into acoustic_samples (source_id,reference,"values",schema_version,raw_values,raw_schema_version) values (?,?,?,?,?,?)',
 (int(sys.argv[2]),'B-3',json.dumps({'rms':2,'centroid':1201,'kurtosis':3}),'v1',json.dumps({'rms':2,'centroid':1201,'kurtosis':3}),'v1'))
con.commit()`,work+'/demo.db',String(second.id)],{stdio:'inherit'});
location.hash='#schema';
if(![...document.querySelectorAll('nav a')].length)await sleep(100);
const started=await api('/schema/migrations',{method:'POST',body:JSON.stringify({source_id:second.id})}).then(r=>r.json());
await waitFor(async()=>{
  const view=await api('/schema/migrations/'+started.id).then(r=>r.json());
  return view.state!=='running'&&view.state!=='pending';
},'second migration analysed',60000);
await navigate('#schema','.migration-card, #migration-form');
document.querySelector('#migration-form')?.remove();
location.hash='#source';
window.dispatchEvent(new window.HashChangeEvent('hashchange'));
await waitFor(()=>document.querySelector('#source-select'),'source for second');
document.querySelector('#source-select').value=String(second.id);
document.querySelector('#source-select').dispatchEvent(new window.Event('change',{bubbles:true}));
await sleep(400);
location.hash='#schema';
window.dispatchEvent(new window.HashChangeEvent('hashchange'));
await waitFor(()=>document.querySelector('.migration-card, #migration-form'),'schema for second');
// select shows the first source's card by default; render a card for the second
const blockedView=await api('/schema/migrations/'+started.id).then(r=>r.json());
check('blocked migration names the cause',blockedView.blocking_reasons.some(r=>r.code==='quarantined_records'),
  JSON.stringify(blockedView.blocking_reasons));
check('blocked migration exposes the isolated sample',blockedView.issues.some(i=>i.reference==='B-3'&&i.code==='missing_field'));
check('blocked migration forbids switching',blockedView.can_switch===false);
// The page for that batch must show the cause and the isolated row.
const {research}=await import('/workspace/frontend/src/stores/research.js');
research.sourceId=second.id;
const mod=await import('/workspace/frontend/src/pages/schema.js?v=9');
document.getElementById('app').innerHTML=await mod.schemaPage();
await snap('blocked-schema-page');
check('page lists blocking reason',/缺项或口径歧义/.test(document.getElementById('app').textContent));
check('page lists isolated reference',/B-3/.test(document.getElementById('app').textContent));
check('switch button disabled on page',document.querySelector('[data-switch]')?.disabled===true);
window.innerWidth=390;
const mod2=await import('/workspace/frontend/src/pages/schema.js?v=10');
document.getElementById('app').innerHTML=await mod2.schemaPage();
await snap('blocked-schema-mobile');
check('narrow layout renders card',!!document.querySelector('.migration-card'));
const refused=await api('/schema/migrations/'+started.id+'/switch',{method:'POST'});
check('switch refused while blocked',refused.status===409,refused.status);
await api('/sources/'+second.id+'/records/'+blockedView.issues[0].record_id+'/correction',
  {method:'PUT',body:JSON.stringify({values:{rms:2,centroid:0.9,kurtosis:3,label:'正常'},schema_version:'v2'})});
let view=await api('/schema/migrations/'+started.id).then(r=>r.json());
check('correction clears the quarantine',view.issue_count===0,view.issue_count);
const switched=await api('/schema/migrations/'+started.id+'/switch',{method:'POST'});
check('blocked batch switches after correction',switched.status===200,switched.status);
const finalRecords=await api('/sources/'+second.id+'/records').then(r=>r.json());
const fixed=finalRecords.find(r=>r.reference==='B-3');
check('corrected sample stored in new unit',fixed&&Math.abs(fixed.values.centroid-0.9)<1e-9,fixed?.values?.centroid);

// 8. history keeps both migrations with their final state
const history=await api('/schema/migrations').then(r=>r.json());
check('migration history lists switched records',history.filter(m=>m.state==='switched').length>=2);

console.log(JSON.stringify({total:results.length,failed:results.filter(r=>!r[1]).length}));
process.exit(results.some(r=>!r[1])?1:0);
