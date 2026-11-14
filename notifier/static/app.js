'use strict';
const $ = selector => document.querySelector(selector);
const state = {token:sessionStorage.getItem('wn-key') || '', page:'tasks', taskPage:1, status:null, targets:[], candidate:null, task:null, busy:false, timer:null};
const labels = {pending:'待发送',preparing:'正在核对',dispatching:'正在发送',observed:'本地已确认',submitted:'已提交微信',unknown:'结果未知',failed:'执行失败',cancelled:'已取消',expired:'已过期',simulated:'预演完成',confirmed_manually:'人工已确认'};
const descriptions = {tasks:['通知任务','查看通知队列与发送结果'],targets:['通知目标','绑定接收通知的微信群聊或联系人'],settings:['连接与接口','检查微信连接，配置发送节奏与调用接口']};
function node(tag, text, className) { const el=document.createElement(tag); if(text!==undefined) el.textContent=text; if(className) el.className=className; return el; }
function timeLabel(seconds) { return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(seconds*1000)); }
function badge(status) { const color=['observed','confirmed_manually'].includes(status)?'green':['unknown','failed'].includes(status)?'red':['pending','preparing','dispatching'].includes(status)?'yellow':''; return node('span',labels[status]||status,'badge '+color); }
function toast(text) { $('#toast').textContent=text; $('#toast').hidden=false; clearTimeout(state.timer); state.timer=setTimeout(()=>$('#toast').hidden=true,3800); }
function fieldError(id,error) { $(id).textContent=error.message || '请求失败，请稍后重试。'; }
async function request(path,options={}) {
  const response=await fetch('/api/v1'+path,{...options,headers:{Authorization:'Bearer '+state.token,'Content-Type':'application/json',...options.headers}});
  let data; try{data=await response.json();}catch{throw new Error('服务返回异常，请确认通知应用仍在运行。');}
  if(!response.ok){ if(response.status===401){sessionStorage.removeItem('wn-key');showGate();} const error=new Error(data.error?.message||'请求未成功'); error.code=data.error?.code; error.fields=data.error?.fields; throw error; }
  return data;
}
const post=(path,data={})=>request(path,{method:'POST',body:JSON.stringify(data)});
function showGate() { $('#app').hidden=true; $('#auth-gate').hidden=false; $('#admin-key').focus(); }
function showApp() { $('#app').hidden=false; $('#auth-gate').hidden=true; }
function pageFromHash() { const name=location.hash.slice(1); return Object.hasOwn(descriptions,name)?name:'tasks'; }
function setPage(name) {
  state.page=name;
  for(const key of Object.keys(descriptions)) $('#page-'+key).hidden=key!==name;
  document.querySelectorAll('[data-page]').forEach(a=>{if(a.dataset.page===name)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  $('#page-title').textContent=descriptions[name][0]; $('#page-description').textContent=descriptions[name][1];
  document.title=descriptions[name][0]+' · 微信通知';
}
function renderStatus(data) {
  state.status=data; const rehearsal=data.connection.mode==='dry_run',enabled=data.settings.dispatch_enabled;
  $('#rehearsal-banner').hidden=!rehearsal;
  $('#dispatch-state').textContent=enabled?(rehearsal?'预演执行中':'自动发送已开启'):'发送已暂停';
  $('#dispatch-state').className='badge '+(enabled?'green':'');
  $('#toggle-dispatch').textContent=enabled?'暂停发送':'开启发送';
  $('#sidebar-state').textContent=data.connection.ready?(rehearsal?'预演环境':'微信连接已就绪'):'后台发送尚未就绪';
  $('#sidebar-dot').className='status-dot '+(data.connection.ready?'ready':'');
  $('#connection-banner').hidden=state.page==='settings'||(data.connection.ready && !data.worker.last_error);
  $('#connection-text').textContent=data.worker.last_error||data.connection.checks.filter(check=>!check.ok).map(check=>check.detail).join('；')||'后台发送尚未就绪，任务会保留在队列中。';
  $('#queue-note').textContent=enabled?'按设置的间隔依次执行':'接收正常，发送暂停';
  $('#wechat-version').textContent=rehearsal?'当前未连接真实微信':(data.connection.wechat_version?'个人微信 '+data.connection.wechat_version:'等待个人微信运行');
  $('#execution-note').textContent=rehearsal?'预演不操作真实微信。':data.connection.note||'仅后台操作；不支持时保留任务。';
  $('#check-background').hidden=rehearsal;
  $('#native-connection-settings').hidden=rehearsal||data.connection.backend!=='onebot';
  $('#permission-note').hidden=!data.connection.permission_app||!data.connection.checks.some(check=>!check.ok&&['辅助功能','屏幕录制'].includes(check.name));
  $('#permission-note').textContent=data.connection.permission_app?'授权对象是 Mac mini 桌面上的“微信群通知.app”。在系统设置 → 隐私与安全中，将它加入辅助功能和屏幕录制；授权后按系统提示重启应用，再重新检查。':'';
  const checks=$('#connection-checks'); checks.replaceChildren();
  for(const check of data.connection.checks){const row=node('div',undefined,'check-row'); row.append(node('span',check.ok?'✓':'○','check-status '+(check.ok?'ok':'')));const text=node('div');text.append(node('strong',check.name),node('p',check.detail));row.append(text);if(!rehearsal&&!check.ok&&['辅助功能','屏幕录制'].includes(check.name)){const button=node('button','在 Mac mini 打开设置','text-button');button.type='button';button.onclick=()=>withButton(button,async()=>{await post('/permissions/'+(check.name==='辅助功能'?'accessibility':'screen'));toast('请在 Mac mini 的系统设置中确认授权');});row.append(button);}checks.append(row);}
  if(!state.settingsDirty){$('#interval-seconds').value=data.settings.interval_seconds;$('#max-pending').value=data.settings.max_pending;}
  $('#compose-hint').textContent=enabled?(rehearsal?'提交后执行预演，不发送微信。':'提交后将自动发送，请核对收件人。'):'加入队列后，开启发送才会执行。';
}
function recipientIdentity(item) {
  if(item.user_id) return ['微信 ID',item.user_id];
  if(item.group_id) return ['群 ID',item.group_id];
  return null;
}
function renderTargets() {
  const container=$('#targets-list');container.replaceChildren();
  const active=state.targets.filter(t=>t.active&&t.mode===state.status?.connection.mode&&t.backend===(state.status?.connection.backend||'visual'));
  $('#targets-count').textContent=active.length+' 个可用';$('#targets-empty').hidden=state.targets.length>0;
  for(const target of state.targets){
    const row=node('div',undefined,'group-row'),info=node('div',undefined,'group-info');
    const kind=target.user_id?'个人':'群聊';
    row.append(node('span',kind[0],'group-avatar'));
    info.append(node('strong',target.label));
    const line=node('p',target.group_name+' · ');
    line.append(node('code',target.code));
    info.append(line);
    const identity=recipientIdentity(target);
    if(identity) info.append(node('p',identity.join(' · '),'footnote'));
    const status=target.active ? (target.mode==='dry_run'?'预演绑定':kind) : '已停用';
    row.append(info,node('span',status,'badge '+(target.active?'green':'')));
    if(target.active){
      const button=node('button','停用','text-button');
      button.type='button';
      button.onclick=()=>withButton(button,async()=>{
        if(!confirm('停用此目标后，待发送任务会取消。确定停用？')) return;
        await post('/targets/'+encodeURIComponent(target.code)+'/disable');
        await refresh();
        toast('通知目标已停用');
      });
      row.append(button);
    }
    container.append(row);
  }
  const select=$('#compose-target'),current=select.value;select.replaceChildren();
  if(active.length!==1){const option=node('option',active.length?'请选择收件人':'请先绑定通知目标');option.value='';select.append(option);}
  for(const target of active){
    const label=target.label===target.group_name?target.label:target.label+' · '+target.group_name;
    const option=node('option',(target.user_id?'个人 · ':'群聊 · ')+label);
    option.value=target.code;
    select.append(option);
  }
  if(active.some(t=>t.code===current))select.value=current;
}
async function renderTasks() {
  const requestedStatus=$('#task-filter').value,requestedPage=state.taskPage;
  const data=await request('/tasks?'+new URLSearchParams({status:requestedStatus,page:requestedPage,limit:25}));
  if(requestedStatus!==$('#task-filter').value||requestedPage!==state.taskPage)return;
  $('#tasks-loading').hidden=true;$('#tasks-empty').hidden=data.items.length>0;$('#tasks-table-wrap').hidden=!data.items.length;
  const filtered=Boolean($('#task-filter').value),hasTargets=state.targets.some(t=>t.active&&t.mode===state.status?.connection.mode&&t.backend===(state.status?.connection.backend||'visual'));
  $('#empty-title').textContent=filtered?'这个状态下还没有任务':'通知会在这里排队';
  $('#empty-description').textContent=filtered?'可以切换筛选条件查看其他通知。':hasTargets?'可以写一条通知加入队列，也可以等待其他应用提交。':'先绑定一个群聊或联系人，再提交第一条通知。';
  $('#empty-action').hidden=filtered||hasTargets;
  const count=data.counts;$('#count-pending').textContent=(count.pending||0)+(count.preparing||0)+(count.dispatching||0);$('#count-submitted').textContent=count.submitted||0;$('#count-observed').textContent=(count.observed||0)+(count.confirmed_manually||0);$('#count-attention').textContent=(count.unknown||0)+(count.failed||0);
  const body=$('#tasks-body');
  if(!document.activeElement.closest('#tasks-body')){body.replaceChildren();for(const task of data.items){const row=node('tr'),main=node('td');main.append(node('span',task.title||task.body.split('\n')[0],'task-title'),node('span',task.body,'task-snippet'));const group=node('td',task.group_name,'task-group'),status=node('td');status.append(badge(task.status));const date=node('td',timeLabel(task.created_at),'task-time'),actions=node('td');const button=node('button','查看','text-button');button.type='button';button.setAttribute('aria-label','查看通知 '+(task.title||task.event_id));button.addEventListener('click',()=>openTask(task.id));actions.append(button);row.append(main,group,status,date,actions);body.append(row);}}
  $('#pagination').hidden=!data.total;$('#page-info').textContent='共 '+data.total+' 条 · 第 '+state.taskPage+' / '+Math.max(1,Math.ceil(data.total/25))+' 页';$('#previous-page').disabled=state.taskPage<=1;$('#next-page').disabled=state.taskPage*25>=data.total;
}
async function refresh(fresh=false) {
  if(!state.token)return;
  if(state.busy){state.refreshPending=true;state.forceFresh=state.forceFresh||fresh;return;}
  state.busy=true;
  try{const [status,targets]=await Promise.all([request('/status'+(fresh?'?fresh=true':'')),request('/targets')]);state.targets=targets.items;renderStatus(status);renderTargets();if(state.page==='tasks')await renderTasks();$('#global-error').hidden=true;}
  catch(error){if(error.code!=='UNAUTHORIZED'){fieldError('#global-error',error);$('#global-error').hidden=false;$('#tasks-loading').hidden=true;}}
  finally{state.busy=false;if(state.refreshPending){const force=state.forceFresh;state.refreshPending=false;state.forceFresh=false;queueMicrotask(()=>refresh(force));}}
}
async function withButton(button,action,errorId='#global-error') {
  if(button.disabled)return;const old=button.textContent;button.disabled=true;button.textContent='请稍候…';
  try{await action();}catch(error){fieldError(errorId,error);$(errorId).hidden=false;}finally{button.disabled=false;if(button.textContent==='请稍候…')button.textContent=old;}
}
async function openTask(id) {
  try{state.task=await request('/tasks/'+id);renderDetail();$('#task-error').textContent='';$('#task-dialog').showModal();}catch(error){toast(error.message);}
}
function renderDetail() {
  const task=state.task,container=$('#task-detail');container.replaceChildren(badge(task.status));
  const details=node('dl',undefined,'detail-meta'),identity=recipientIdentity(task);
  const metadata=[[task.user_id?'个人收件人':'目标群',task.group_name]];
  if(identity) metadata.push(identity);
  metadata.push(['事件编号',task.event_id],['提交时间',timeLabel(task.created_at)],['有效期至',timeLabel(task.expires_at)]);
  for(const [name,value] of metadata) details.append(node('dt',name),node('dd',value));
  container.append(details,node('div',task.message,'detail-message'));
  if(task.error_message)container.append(node('p',task.error_message,'detail-warning'));
  if(task.status==='submitted')container.append(node('p','请在对应微信会话搜索通知编号，核对后可标记为人工已确认。','detail-warning'));
  if(task.status==='unknown')container.append(node('p','请在对应微信会话搜索通知编号。确认未发送后可以重试。','detail-warning'));
  const timeline=node('div',undefined,'timeline');for(const event of task.events){const row=node('div',undefined,'timeline-row');row.append(node('span',labels[event.status]||event.status),node('span',timeLabel(event.at)));timeline.append(row);}container.append(timeline);
  const actions=$('#task-actions');actions.replaceChildren();
  async function action(button,name,payload={}){await withButton(button,async()=>{state.task=await post('/tasks/'+task.id+'/'+name,payload);renderDetail();await refresh();toast('任务状态已更新');},'#task-error');}
  if(['pending','failed','unknown'].includes(task.status)){const button=node('button','取消任务','secondary');button.type='button';button.onclick=()=>action(button,'cancel');actions.append(button);}
  if(['unknown','submitted'].includes(task.status)){const confirmButton=node('button','已核对送达','secondary');confirmButton.type='button';confirmButton.onclick=()=>{if(confirm('确认已在对应会话找到这条通知？'))action(confirmButton,'confirm');};actions.append(confirmButton);}
  if(['failed','unknown'].includes(task.status)){const retry=node('button',task.status==='unknown'?'核对未发送后重试':'重新排队','primary');retry.type='button';retry.onclick=()=>{if(task.status==='unknown'&&!confirm('确认已在对应会话核对通知编号，消息尚未发送？'))return;action(retry,'retry',{checked_not_sent:task.status==='unknown'});};actions.append(retry);}
}
$('#auth-form').addEventListener('submit',async event=>{event.preventDefault();state.token=$('#admin-key').value.trim();await withButton(event.submitter,async()=>{await request('/status');sessionStorage.setItem('wn-key',state.token);$('#admin-key').value='';showApp();setPage(pageFromHash());await refresh();},'#auth-error');});
window.addEventListener('hashchange',()=>{setPage(pageFromHash());refresh();});
function saveTaskLocation(){const url=new URL(location.href);const filter=$('#task-filter').value;if(filter)url.searchParams.set('status',filter);else url.searchParams.delete('status');if(state.taskPage>1)url.searchParams.set('page',state.taskPage);else url.searchParams.delete('page');history.pushState(null,'',url);}
function restoreTaskLocation(){const query=new URLSearchParams(location.search);$('#task-filter').value=query.get('status')||'';if(!$('#task-filter').value)$('#task-filter').value='';state.taskPage=Math.min(100000,Math.max(1,Number(query.get('page'))||1));}
$('#task-filter').addEventListener('change',()=>{state.taskPage=1;saveTaskLocation();refresh();});
$('#previous-page').onclick=()=>{state.taskPage--;saveTaskLocation();refresh();};$('#next-page').onclick=()=>{state.taskPage++;saveTaskLocation();refresh();};
window.addEventListener('popstate',()=>{restoreTaskLocation();setPage(pageFromHash());refresh();});
$('#compose-open').onclick=()=>{$('#compose-form').reset();renderTargets();$('#compose-event').value='manual:'+crypto.randomUUID();$('#body-count').textContent='0 / 4000';$('#compose-error').textContent='';$('#compose-dialog').showModal();$('#compose-target').focus();};
$('#compose-body').oninput=()=>$('#body-count').textContent=$('#compose-body').value.length+' / 4000';
$('#compose-form').addEventListener('submit',async event=>{event.preventDefault();await withButton(event.submitter,async()=>{const response=await post('/notifications',{event_id:$('#compose-event').value.trim(),target_code:$('#compose-target').value,title:$('#compose-title').value,body:$('#compose-body').value});$('#compose-dialog').close();$('#compose-form').reset();toast(response.duplicate?'已存在相同通知，未重复创建':'通知已加入队列');state.taskPage=1;if(state.page!=='tasks')location.hash='tasks';await refresh();},'#compose-error');});
function chooseCandidate(item) {
  state.candidate=item;
  const identity=recipientIdentity(item),rehearsal=state.status?.connection.mode==='dry_run';
  $('#bind-form').reset();
  $('#bind-heading').textContent=item.user_id?'核对个人收件人':'核对目标群';
  $('#bind-group-name').textContent=item.group_name;
  $('#bind-preview').hidden=!item.preview;
  if(item.preview) $('#bind-title-image').src=item.preview;
  else $('#bind-title-image').removeAttribute('src');
  $('#bind-calibration-note').hidden=!item.requires_visual_confirmation;
  $('#bind-recipient-id').hidden=!identity;
  $('#bind-recipient-id').textContent=identity?.join(' · ')||'';
  $('#bind-confirm-label').textContent=item.user_id?'我已核对接收通知的联系人':'我已核对接收通知的微信群';
  $('#bind-save-note').textContent=rehearsal?'保存预演收件人':identity?'保存时会再次核对 ID 和账号':'保存时会再次核对群标题';
  $('#bind-label').value=item.group_name.slice(0,60);
  $('#bind-code').value=(item.user_id?'person-':'leads-')+crypto.randomUUID().slice(0,6);
  $('#bind-error').textContent='';
  $('#bind-dialog').showModal();
}

function clearSearchResults() {
  $('#search-error').textContent='';
  $('#search-results').hidden=true;
  $('#candidate-list').replaceChildren();
}

$('#recipient-type').addEventListener('change',()=>{
  const personal=$('#recipient-type').value==='private';
  $('#search-label').textContent=personal?'昵称、备注或微信 ID':'群名或群 ID';
  $('#group-query').placeholder=personal?'例如 文件传输助手':'输入目标微信群的名称';
  $('#search-submit').textContent=personal?'查找联系人':'查找群聊';
  $('#private-lookup').hidden=!personal;
  $('#allow-contact-lookup').checked=false;
  clearSearchResults();
});
$('#group-query').addEventListener('input',clearSearchResults);
$('#search-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const type=$('#recipient-type'),input=$('#group-query'),consent=$('#allow-contact-lookup');
  const recipientType=type.value,query=input.value.trim(),openCandidate=state.openCandidateOnSearch;
  const allowLookup=recipientType==='private'&&consent.checked;
  state.openCandidateOnSearch=false;
  consent.checked=false;
  clearSearchResults();
  type.disabled=input.disabled=consent.disabled=true;
  try {
    await withButton(event.submitter,async()=>{
      const data=await post('/targets/search',{query,recipient_type:recipientType,allow_contact_lookup:allowLookup});
      const list=$('#candidate-list'),rehearsal=state.status?.connection.mode==='dry_run';
      for(const item of data.items){
        const row=node('div',undefined,'candidate-row'),info=node('div'),identity=recipientIdentity(item);
        info.append(node('strong',item.group_name));
        const detail=rehearsal?'预演候选':identity?.join(' · ')||'来自微信本地群聊搜索';
        info.append(node('small',detail));
        const button=node('button',item.user_id?'选择联系人':'选择此群','secondary');
        button.type='button';
        button.onclick=()=>chooseCandidate(item);
        row.append(info,button);
        list.append(row);
      }
      $('#search-result-label').textContent=data.items.length?'选择收件人并核对身份':'未找到匹配的收件人';
      $('#search-results').hidden=false;
      if(openCandidate&&data.items.length===1) chooseCandidate(data.items[0]);
    },'#search-error');
  } finally {
    type.disabled=input.disabled=consent.disabled=false;
  }
});
$('#bind-form').addEventListener('submit',async event=>{
  event.preventDefault();
  await withButton(event.submitter,async()=>{
    await post('/targets/bind',{candidate_id:state.candidate.candidate_id,code:$('#bind-code').value.trim(),
      label:$('#bind-label').value.trim(),confirmed_visual:$('#bind-confirm').checked});
    $('#bind-dialog').close();
    clearSearchResults();
    await refresh();
    toast('收件人已绑定');
  },'#bind-error');
});
$('#toggle-dispatch').onclick=async event=>{if(state.status?.settings.dispatch_enabled){await withButton(event.target,async()=>{await request('/settings',{method:'PATCH',body:JSON.stringify({dispatch_enabled:false})});await refresh();toast('已暂停，未发送任务会保留');});}else{$('#enable-error').textContent='';$('#enable-title').textContent=state.status?.connection.mode==='dry_run'?'开启队列预演':'开启自动发送';$('#enable-dialog').showModal();}};
$('#enable-form').addEventListener('submit',async event=>{event.preventDefault();await withButton(event.submitter,async()=>{await request('/settings',{method:'PATCH',body:JSON.stringify({dispatch_enabled:true})});$('#enable-dialog').close();await refresh();toast('队列执行已开启');},'#enable-error');});
$('#native-connection-settings').addEventListener('toggle',async event=>{if(!event.target.open||state.bridgeDirty)return;try{const data=await request('/native/connection');$('#bridge-url').value=data.base_url;$('#bridge-token').placeholder=data.has_token?'已配置，留空保留现有密钥':'留空自动生成本机密钥';}catch(error){fieldError('#bridge-config-error',error);}});
$('#native-connection-form').addEventListener('input',()=>{state.bridgeDirty=true;});
$('#native-connection-form').addEventListener('submit',async event=>{event.preventDefault();await withButton(event.submitter,async()=>{await request('/native/connection',{method:'PUT',body:JSON.stringify({base_url:$('#bridge-url').value.trim(),token:$('#bridge-token').value})});$('#bridge-token').value='';state.bridgeDirty=false;toast('连接已保存，发送已暂停；更改密钥后需重启发送桥');await refresh(true);},'#bridge-config-error');});
$('#settings-form').addEventListener('input',()=>{state.settingsDirty=true;});
$('#settings-form').addEventListener('submit',async event=>{event.preventDefault();$('#settings-error').textContent='';await withButton(event.submitter,async()=>{await request('/settings',{method:'PATCH',body:JSON.stringify({interval_seconds:Number($('#interval-seconds').value),max_pending:Number($('#max-pending').value)})});state.settingsDirty=false;toast('设置已保存');await refresh();},'#settings-error');});
$('#check-connection').onclick=event=>withButton(event.target,()=>refresh(true));
$('#check-background').onclick=event=>withButton(event.target,async()=>{const report=await post('/background/check');const capture=report.window_capture;const text=report.backend==='onebot'?(report.ready?'原生发送桥已就绪。':'原生发送桥尚未就绪，原因见上方检查项。'):(report.ready?'后台控件检查通过；真实群发送仍需单独验证。':'后台控件检查未通过，具体原因见上方检查项。');$('#background-result').textContent=text+(capture?(capture.ok?'后台窗口读取通过。':capture.detail||'后台窗口读取未通过。'):'')+'本次没有执行发送。';$('#background-result').hidden=false;await refresh();},'#background-result');
$('#reveal-token').onclick=event=>withButton(event.target,async()=>{const data=await post('/integration-key');$('#ingest-token').value=data.token;$('#token-field').hidden=false;$('#reveal-token').hidden=true;});
$('#copy-token').onclick=async()=>{try{await navigator.clipboard.writeText($('#ingest-token').value);toast('接口密钥已复制，请妥善保管');}catch{toast('浏览器不允许复制，请在本机管理入口操作。');}};
document.querySelectorAll('.close-dialog').forEach(button=>button.onclick=()=>{const dialog=button.closest('dialog');if(dialog.id==='compose-dialog'&&$('#compose-body').value&&!confirm('通知尚未加入队列，确定关闭？'))return;dialog.close();});
$('#compose-dialog').addEventListener('cancel',event=>{if($('#compose-body').value&&!confirm('通知尚未加入队列，确定关闭？'))event.preventDefault();});
window.addEventListener('beforeunload',event=>{if(($('#compose-dialog').open&&$('#compose-body').value)||state.settingsDirty||state.bridgeDirty){event.preventDefault();event.returnValue='';}});
$('#api-endpoint').textContent=location.origin+'/api/v1/notifications';
$('#api-example').textContent=JSON.stringify({event_id:'lead:platform:user:created',target_code:'绑定时设置的目标标识',title:'新增线索',body:'用户、地区、需求原话与来源链接',expires_in_seconds:86400},null,2)+'\n\n返回 task_id 后查询 GET /api/v1/tasks/{task_id}；202 表示已入队。';
const params=new URLSearchParams(location.hash.slice(1)),requestedGroup=params.get('group');if(params.has('key')){state.token=params.get('key');sessionStorage.setItem('wn-key',state.token);history.replaceState(null,'',location.pathname+(requestedGroup?'#targets':'#tasks'));}
restoreTaskLocation();setPage(pageFromHash());if(state.token){showApp();refresh().then(()=>{if(requestedGroup){$('#group-query').value=requestedGroup;state.openCandidateOnSearch=true;$('#search-form').requestSubmit($('#search-form button[type=submit]'));}});}else showGate();
setInterval(()=>{if(!document.hidden&&!document.querySelector('dialog[open]')&&!$('#app').hidden)refresh();},10000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!document.querySelector('dialog[open]'))refresh();});
