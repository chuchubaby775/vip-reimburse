import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
const source=fs.readFileSync(new URL('../public/app.js',import.meta.url),'utf8').replace(/^import .*\n/,'').replace(/\ninit\(\);\s*$/,'');
const drain=async()=>{for(let i=0;i<10;i++)await Promise.resolve()};
const item=(id='a'.repeat(32),extra={})=>({id,reference:'SYNTHETIC-REF',applicant:{name:'虚构测试嘉宾',mobile:'13800138000',organization:'虚构单位'},totalAmount:106,expenseCount:1,status:'pending',createdAt:'2026-11-13T01:00:00Z',updatedAt:'2026-11-13T01:00:00Z',deletedAt:'',expenses:[],supportingFiles:[],payment:{},...extra});
function setup(){
 let approved=true;const prompts=[],requests=[],messages=[],app={innerHTML:'',addEventListener(){}};
 const document={querySelector:s=>s==='#app'?app:null,querySelectorAll:()=>[],addEventListener(){},body:{dataset:{},style:{}}};
 const context=vm.createContext({document,crypto:webcrypto,location:{pathname:'/admin'},window:{addEventListener(){},confirm:text=>{prompts.push(text);return approved}},navigator:{},setTimeout:()=>0,clearTimeout(){},requestAnimationFrame:fn=>fn(),Blob,TextEncoder,URLSearchParams,createDraftStore:()=>({}),__http:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),__toast:message=>messages.push(message)});
 vm.runInContext(source+`\napi=__http;toast=__toast;globalThis.h={state,render,loadAdmin,changeSubmissionDeletion,reviewSubmit};`,context);
 const h=context.h;h.state.admin.authenticated=true;h.state.config={eventName:'虚构大会'};
 return {...h,app,prompts,requests,messages,approve:value=>{approved=value}};
}
test('canceling deletion keeps the record and sends no request',async()=>{
 const h=setup(),row=item();h.state.admin.items=[row];h.approve(false);
 assert.equal(await h.changeSubmissionDeletion(row.id),false);assert.equal(h.requests.length,0);assert.equal(h.state.admin.items[0],row);
 for(const text of [row.applicant.name,row.applicant.mobile,row.reference,'106.00','回收站'])assert.ok(h.prompts[0].includes(text));
});
test('confirmed deletion is serialized and refreshes active list and trash count',async()=>{
 const h=setup(),row=item();h.state.admin.items=[row];h.state.admin.selected=row.id;
 const pending=h.changeSubmissionDeletion(row.id);assert.equal(h.state.busy,true);
 assert.equal(await h.changeSubmissionDeletion(row.id),false);assert.equal(h.requests.length,1);
 assert.equal(h.requests[0].options.method,'DELETE');assert.equal(h.requests[0].url,'/api/admin/submissions/'+row.id);
 h.requests[0].resolve({ok:true,deletedAt:'2026-11-13T01:10:00Z'});await drain();
 assert.equal(h.state.admin.items.length,0);assert.equal(h.state.admin.selected,null);assert.equal(h.state.admin.trashCount,1);
 assert.equal(h.requests[1].url,'/api/admin/submissions');h.requests[1].resolve({items:[],trashCount:1});assert.equal(await pending,true);assert.equal(h.state.busy,false);
});
test('a failed delete retains record and selection so it can be retried',async()=>{
 const h=setup(),row=item();h.state.admin.items=[row];h.state.admin.selected=row.id;
 const pending=h.changeSubmissionDeletion(row.id);h.requests[0].reject(new Error('synthetic connection failure'));
 assert.equal(await pending,false);assert.equal(h.state.admin.items[0],row);assert.equal(h.state.admin.selected,row.id);assert.equal(h.state.busy,false);
 assert.ok(h.messages.includes('synthetic connection failure'));
});
test('restore uses its own endpoint and remains in the recycle bin after refresh',async()=>{
 const h=setup(),row=item(undefined,{deletedAt:'2026-11-13T01:10:00Z'});h.state.admin.items=[row];h.state.admin.showTrash=true;h.state.admin.trashCount=1;
 const pending=h.changeSubmissionDeletion(row.id,true);
 assert.equal(h.prompts.length,0);assert.equal(h.requests[0].options.method,'POST');assert.equal(h.requests[0].url,'/api/admin/submissions/'+row.id+'/restore');
 h.requests[0].resolve({ok:true,deletedAt:''});await drain();assert.equal(h.requests[1].url,'/api/admin/submissions?deleted=1');
 h.requests[1].resolve({items:[],trashCount:0});assert.equal(await pending,true);assert.equal(h.state.admin.showTrash,true);assert.equal(h.state.admin.trashCount,0);
});
test('restore conflict leaves the deleted record available and displays server reason',async()=>{
 const h=setup(),row=item(undefined,{deletedAt:'2026-11-13T01:10:00Z'});h.state.admin.items=[row];h.state.admin.showTrash=true;
 const pending=h.changeSubmissionDeletion(row.id,true);h.requests[0].reject(new Error('相同票据已用于另一份有效申请'));
 assert.equal(await pending,false);assert.equal(h.state.admin.items[0],row);assert.ok(h.messages.includes('相同票据已用于另一份有效申请'));
});
test('recycle bin hides export links and review editing while preserving restore controls',async()=>{
 const h=setup(),row=item(undefined,{deletedAt:'2026-11-13T01:10:00Z'});h.state.admin.items=[row];h.state.admin.selected=row.id;h.state.admin.showTrash=true;h.render();
 const html=h.app.innerHTML;
 for(const text of ['申请回收站','删除时间','恢复申请','返回申请','回收站记录不计入报销汇总'])assert.ok(html.includes(text),text);
 assert.doesNotMatch(html,/\/api\/admin\/export\.|id="review-status"|id="review-note"|保存处理结果|data-action="delete-submission"/);
 await h.reviewSubmit();assert.equal(h.requests.length,0);
 h.state.admin.showTrash=false;h.state.admin.items=[item()];h.state.admin.selected=null;h.render();assert.match(h.app.innerHTML,/data-action="delete-submission"/);assert.match(h.app.innerHTML,/\/api\/admin\/export.xlsx/);
});
test('a stale active-list response cannot replace a newer recycle-bin result',async()=>{
 const h=setup();const first=h.loadAdmin();h.state.admin.showTrash=true;const second=h.loadAdmin();
 h.requests[1].resolve({items:[item('b'.repeat(32),{deletedAt:'2026-11-13T01:10:00Z'})],trashCount:1});await second;
 h.requests[0].resolve({items:[item()],trashCount:0});await first;
 assert.equal(h.state.admin.items[0].id,'b'.repeat(32));assert.equal(h.state.admin.trashCount,1);
});
