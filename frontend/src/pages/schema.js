import {domain} from '../constants/domain.js';
import {research} from '../stores/research.js';
import {session} from '../stores/session.js';
import {sourcesApi,schemaApi} from '../api/research.js';
import {status} from '../components/common/status.js';
import {metrics} from '../components/common/business.js';
import {table} from '../components/common/table.js';
import {esc,fmt,localTime} from '../utils/format.js';
import {bind,toast} from '../hooks/actions.js';

const versionName=v=>v==='v2'?'新口径 · kHz':v==='v1'?'旧口径 · Hz':(v||'—');

function sourceChoices(){
  return research.sources.map(s=>'<option value="'+s.id+'" '+(s.id==research.sourceId?'selected':'')+'>'+esc(s.name)+' · '+research.sources.find(x=>x.id==s.id).record_count+'条 · '+versionName(s.schema_version)+'</option>').join('');
}

function progressBar(view){
  const total=view.total_records||0,ready=view.ready_records||0;
  const percent=total?Math.min(100,Math.round(ready/total*100)):0;
  return '<div class="migration-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="'+percent+'"><i style="--w:'+percent+'%"></i><span>'+ready+' / '+total+' 条已按'+esc(view.to_label)+'换算 · '+percent+'%</span></div>';
}

function reasonList(view){
  if(!view.blocking_reasons.length)return '<p class="fine ok-text">没有阻塞项：全部有效样本已按新口径就绪。</p>';
  return '<ul class="blocking-list">'+view.blocking_reasons.map(r=>'<li><b>'+esc(r.code)+'</b><span>'+esc(r.detail)+'</span></li>').join('')+'</ul>';
}

function issueTable(view){
  if(!view.issues||!view.issues.length)return '';
  return '<h3>隔离样本 <small>修正后才可切换；原值仍保留</small></h3>'+
    '<div class="table-wrap"><table><thead><tr><th>样本编号</th><th>原因</th><th>字段</th><th>说明</th><th></th></tr></thead><tbody>'+
    view.issues.map(i=>'<tr><td>'+esc(i.reference)+'</td><td>'+esc(i.code)+'</td><td>'+esc(i.field)+'</td><td>'+esc(i.detail)+'</td><td>'+
      (session.writable?'<button class="secondary" data-fix="'+i.record_id+'" data-ref="'+esc(i.reference)+'">修正</button>':'')+
    '</td></tr>').join('')+'</tbody></table></div>';
}

function fixForm(recordId,reference,view){
  return '<form id="fix-form" data-record="'+recordId+'"><h3>修正隔离样本 '+esc(reference)+'</h3>'+
    '<p class="fine">按'+esc(view.to_label)+'填写；原始上报值仍保存在审计链路中。</p>'+
    '<div class="parameter-fields">'+domain.fields.map(([key,label,kind])=>
      '<label>'+esc(label)+'<input name="'+key+'" type="'+(kind==='number'?'number':'text')+'" '+(kind==='number'?'step="any"':'')+' required></label>').join('')+
    '</div><input type="hidden" name="schema_version" value="'+esc(view.to_version)+'">'+
    '<button type="submit">保存修正并重新检查 →</button></form>';
}

function activeCard(view){
  const switched=view.state==='switched';
  return '<article class="migration-card">'+
    '<header><div><h2>迁移 #'+view.id+' · '+esc(view.source_name)+'</h2><p class="fine">'+esc(view.from_label)+' → '+esc(view.to_label)+' · 发起人 '+esc(view.created_by)+' · '+esc(localTime(view.created_at))+'</p></div>'+status(view.state)+'</header>'+
    progressBar(view)+
    '<div class="migration-actions">'+
      (session.writable&&!switched?'<button class="secondary" data-resume="'+view.id+'">重新检查并继续换算 ↻</button>':'')+
      (session.user.role==='admin'&&!switched?'<button data-switch="'+view.id+'" '+(view.can_switch?'':'disabled')+'>'+(view.can_switch?'全部就绪，整批切换 →':'待全部有效样本就绪后可切换')+'</button>':'')+
    '</div>'+
    (switched?'<p class="fine ok-text">该批次已冻结为'+esc(view.to_label)+'。切换后的研究冻结新口径与样本；此前建立的研究保持原样。</p>':'')+
    '<h3>阻塞原因</h3>'+reasonList(view)+
    issueTable(view)+
    '</article>';
}

export async function schemaPage(){
  const registry=await schemaApi.registry();
  research.schemaRegistry=registry;
  const migrations=await schemaApi.list();
  research.migrations=migrations;
  const selected=migrations.find(m=>m.source_id==research.sourceId)||null;
  const detail=selected?await schemaApi.get(selected.id):null;
  const candidates=research.sources.filter(s=>s.schema_version==='v1');
  const createForm=!detail&&session.writable&&candidates.length?
    '<form id="migration-form"><label>迁移'+esc(domain.source)+'<select name="source_id">'+sourceChoices()+'</select></label>'+
    '<p class="fine">迁移期间新旧口径都可入样；全部有效样本按新口径就绪后才允许整批切换。</p>'+
    '<button type="submit">发起口径迁移 →</button></form>':
    (!detail&&!candidates.length?'<p class="fine">当前没有处于旧口径的'+esc(domain.source)+'。</p>':'');
  return '<section class="page-heading"><p class="eyebrow">SCHEMA / UNIT ROLLOVER</p><h1>换设备，不换账本。</h1><p>声检仪更换后频谱质心以 kHz 上报。口径在线换版保留原值，新旧口径带版本入样，历史样本分批换算；缺项或歧义样本隔离且阻止切换。</p></section>'+
    '<div class="split"><section>'+
      (detail?activeCard(detail):createForm)+
    '</section><aside class="method-sheet"><p class="eyebrow">VERSION REGISTRY</p><h2>口径定义</h2>'+
      Object.entries(registry.versions).map(([name,v])=>'<div class="schema-version"><b>'+esc(name)+' · '+esc(v.label)+'</b><p>'+esc(v.note)+'</p><ul>'+
        Object.entries(v.fields).map(([key,f])=>'<li>'+esc(f.label)+(f.unit?'/'+esc(f.unit):'')+' <code>'+esc(key)+'</code>'+(f.required?' · 必填':'')+'</li>').join('')+
      '</ul></div>').join('')+
      '<p class="fine">字段别名：<code>centroid_hz</code>、<code>centroid_khz</code>；同一字段多个别名数值不一致时按歧义隔离。</p>'+
    '</aside></div>'+
    '<h2>迁移记录</h2>'+migrationTable(migrations,detail?.id);
}

function migrationTable(migrations,activeId){
  if(!migrations.length)return '<p class="empty">还没有口径迁移记录。</p>';
  return '<div class="table-wrap"><table><thead><tr><th>#</th><th>'+esc(domain.source)+'</th><th>口径</th><th>状态</th><th>进度</th><th>隔离</th><th>更新时间</th><th></th></tr></thead><tbody>'+
    migrations.map(m=>'<tr><td>'+m.id+'</td><td>'+esc(m.source_name)+'</td><td>'+esc(m.from_version)+' → '+esc(m.to_version)+'</td><td>'+status(m.state)+'</td><td>'+m.ready_records+' / '+m.total_records+'</td><td>'+m.issue_count+'</td><td>'+esc(localTime(m.updated_at))+'</td><td>'+(m.id==activeId?'当前':'')+'</td></tr>').join('')+'</tbody></table></div>';
}

export function bindSchema(refresh){
  bind('#migration-form','submit',async event=>{
    event.preventDefault();
    const data=Object.fromEntries(new FormData(event.target));
    try{await schemaApi.create(Number(data.source_id));await refresh();toast('口径迁移已发起，worker 将分批换算')}catch(error){toast(error.message,true)}
  });
  for(const button of document.querySelectorAll('[data-resume]'))button.onclick=async()=>{
    try{await schemaApi.resume(button.dataset.resume);await refresh();toast('已重新排队换算')}catch(error){toast(error.message,true)}
  };
  for(const button of document.querySelectorAll('[data-switch]'))button.onclick=async()=>{
    if(!confirm('全部有效样本已按新口径就绪。确认整批切换？此操作不可回退。'))return;
    try{await schemaApi.switch(button.dataset.switch);await refresh();toast('批次已整批切换到新口径')}catch(error){toast(error.message,true);await refresh()}
  };
  for(const button of document.querySelectorAll('[data-fix]'))button.onclick=async()=>{
    const view=research.migrations.find(m=>m.source_id==research.sourceId);
    const host=document.querySelector('.migration-card');
    host.querySelector('#fix-form')?.remove();
    const holder=document.createElement('div');
    holder.innerHTML=fixForm(button.dataset.fix,button.dataset.ref,await schemaApi.get(view.id));
    host.appendChild(holder.firstChild);
    const form=document.getElementById('fix-form');
    form.onsubmit=async event=>{
      event.preventDefault();
      const data=Object.fromEntries(new FormData(form));
      const values={};for(const [key,,kind] of domain.fields)values[key]=kind==='number'?Number(data[key]):data[key];
      try{await schemaApi.correct(research.sourceId,Number(form.dataset.record),{values,schema_version:data.schema_version});await refresh();toast('修正已保存，正在重新检查')}catch(error){toast(error.message,true)}
    };
    form.scrollIntoView({block:'center'});
  };
}
