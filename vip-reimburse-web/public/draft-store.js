// Only server-side snapshots contain personal data. The browser keeps no form storage.
export function createDraftStore({api,snapshot,restore,onStatus,delay=600,clientId=crypto.randomUUID()}){
 let ready=false,revision=0,sequence=0,generation=0,savedGeneration=0;
 let timer=null,inFlight=null,paused=false,completed=false,conflict=false,lastSaved='';
 const status=(phase,message='')=>onStatus({phase,message,lastSaved});
 const cancelTimer=()=>{clearTimeout(timer);timer=null};
 const packet=()=>({draft:snapshot(),clientId,sequence:++sequence,baseRevision:revision});
 async function initialize(){
  status('loading');
  try{
   const data=await api('/api/guest/draft/session',{method:'POST',body:'{}'});
   if(completed)return true;
   revision=data.revision||0;ready=true;
   if(generation&&data.draft){conflict=true;status('conflict','检测到另一份已保存草稿，请重新载入后继续。');return false}
   if(!generation)restore(data);
   lastSaved=data.updatedAt||'';status(data.draft?'restored':'ready');
   if(generation>savedGeneration)void flush();
   return true;
  }catch(error){if(completed)return true;status('error','草稿服务暂时未连接，请重试。');return false}
 }
 function changed(immediate=false){
  if(paused||completed)return;
  generation++;cancelTimer();
  if(conflict){status('conflict','另一页面保存了新草稿，本页修改尚未保存，请重新载入。');return}
  status('waiting');
  if(immediate)void flush();else timer=setTimeout(()=>void flush(),delay);
 }
 async function flush(){
  cancelTimer();
  if(completed)return true;
  if(paused||conflict||!ready)return false;
  if(inFlight)return inFlight;
  if(savedGeneration>=generation)return true;
  inFlight=(async()=>{
   while(savedGeneration<generation&&!completed&&!paused&&!conflict){
    const savingGeneration=generation;
    status('saving');
    try{
     const data=await api('/api/guest/draft',{method:'POST',body:JSON.stringify(packet())});
     if(completed)return true;
     if(data.committed){complete();return true}
     revision=Math.max(revision,data.revision||0);lastSaved=data.updatedAt||lastSaved;
     savedGeneration=savingGeneration;
     if(savedGeneration===generation)status('saved');
    }catch(error){
     if(completed)return true;
     conflict=error.status===409&&error.code==='draft_conflict';
     status(conflict?'conflict':'error',conflict?'另一页面保存了新草稿，本页修改尚未保存，请重新载入。':'保存失败，当前页面的填写仍在，请重试后再退出。');
     return false;
    }
   }
   return savedGeneration>=generation;
  })();
  try{return await inFlight}finally{inFlight=null}
 }
 async function retry(){
  if(completed||conflict)return false;
  if(!ready&&!await initialize())return false;
  if(generation===savedGeneration){generation++;status('waiting')}
  return flush();
 }
 function beacon(send){
  if(!ready||paused||completed||conflict||generation<=savedGeneration)return false;
  cancelTimer();
  const body=JSON.stringify(packet());
  // sendBeacon queueing is not a server acknowledgement; keep the draft dirty.
  return new TextEncoder().encode(body).length<=60*1024&&send('/api/guest/draft',body);
 }
 function suspend(){paused=true;cancelTimer()}
 function resume(){if(!completed){paused=false;if(generation>savedGeneration)changed(true)}}
 function complete(){completed=true;paused=true;cancelTimer();savedGeneration=generation;status('submitted')}
 return {initialize,changed,flush,retry,beacon,suspend,resume,complete,
  getState:()=>({ready,revision,sequence,generation,savedGeneration,paused,completed,conflict})};
}
