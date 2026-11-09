/* Synthetic navigation regression: VM/Fake DOM only, no real account, browser or writes. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const harness=fs.readFileSync(path.join(__dirname,'test_evidence_timeline.cjs'),'utf8').split('(async () => {')[0];
const shared={require,__dirname,console,URLSearchParams,Headers,Blob,Response,Date,Set,Map,Number,Array,Math,Promise,setTimeout,clearTimeout,queueMicrotask};
vm.runInNewContext(harness+'\nglobalThis.makeEnvironment=environment;',shared);
const tick=async()=>{for(let i=0;i<70;i++)await Promise.resolve();};
const storage=initial=>{const values=new Map(Object.entries(initial||{}));return{getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,String(v)),removeItem:k=>values.delete(k),values};};
function env(page,hash='',local={},session={}){
  const e=shared.makeEnvironment(page);e.local=storage(local);e.session=storage(session);e.requests=[];e.destinations=[];e.reloads=0;
  e.sandbox.localStorage=e.local;e.sandbox.sessionStorage=e.session;e.sandbox.URL=URL;
  e.sandbox.location={origin:'http://fixture',hash,pathname:'/'+page.replace('.html',''),assign:value=>e.destinations.push(value),reload:()=>e.reloads++};
  e.sandbox.history.replaceState=(_,__,url)=>{e.sandbox.location.hash=new URL(url,'http://fixture').hash;};
  e.sandbox.setInterval=()=>1;e.sandbox.clearInterval=()=>{};e.document.visibilityState='visible';
  e.get=id=>e.document.getElementById(id);
  e.addLink=attribute=>{const a=e.document.createElement('a');a.setAttribute(attribute,'');e.document.body.append(a);return a;};
  return e;
}
function item(id,ref,answers=0,created='2026-10-24T10:00:00Z'){return{session_id:id,candidate_ref:ref,created_at:created,status:answers?'interview_finished':'answering',answer_count:answers,main_answer_count:Math.min(answers,3),content_status:'not_requested',review_status:'not_reviewed'};}
const user=item('user-completed-123','',4),qa=item('qa-empty-456','SIM-MCP-QA-STDIO-fixture'),unknown=item('ordinary-sim-789','SIM-用户自己的项目');
function workbench(hash='',local={}){const e=env('workbench.html',hash,local,{'suit-workbench-access-v1':'fixture-operator'});e.home=e.addLink('data-workbench-home');
  e.sandbox.fetch=async(url,options={})=>{e.requests.push(url);if(url==='/v1/workbench/sessions')return new Response(JSON.stringify({sessions:[qa,unknown,user]}),{status:200});if(url==='/v1/workbench/sessions/'+user.session_id+'/open')return new Response(JSON.stringify({report_url:'/interview-report#session='+user.session_id+'&token=fixture-hr'}),{status:200});throw new Error('Unexpected fixture request '+url);};e.run('js/workbench.js');return e;}
function candidate(tokens={}){const id=user.session_id,e=env('interview.html','#session='+id+'&token=fixture-candidate',{}, {'suit-workbench-hr-links-v1':JSON.stringify(tokens)});e.back=e.addLink('data-workbench-link');
  e.sandbox.fetch=async(url)=>{e.requests.push(url);return new Response(JSON.stringify({...user,answers:[{answer_id:'a',round_id:'r',question_id:'q',current:true}],rounds:[{round_id:'r',kind:'main'}],current_round:null}),{status:200});};e.run('js/interview.js');return e;}
let count=0;async function check(name,fn){await fn();count++;console.log('PASS '+name);}
(async()=>{
  await check('bare workbench restores submitted user session, hides exact QA, keeps unknown SIM labels',async()=>{
    const e=workbench('',{'suit-recent-answered-interview-v1':JSON.stringify({session_id:user.session_id})});await tick();
    assert.match(e.sandbox.location.hash,/user-completed/);assert.match(e.get('selectedSession').textContent,/档案 user-complet/);
    assert.ok(e.get('sessionList').textContent.includes('SIM-用户自己的项目'));assert.ok(!e.get('sessionList').textContent.includes('SIM-MCP-QA-STDIO'));
    assert.ok(!e.get('sessionList').textContent.includes('未填写候选人编号'));assert.match(e.home.href,/session=user-completed/);
    e.get('selectedSession').querySelectorAll('button').find(b=>b.textContent==='查看整场报告').click();await tick();
    assert.deepEqual(e.destinations,['/interview-report#session='+user.session_id+'&token=fixture-hr']);
  });
  await check('no prior browser record chooses answered non-QA instead of newest empty fixture',async()=>{const e=workbench();await tick();assert.match(e.sandbox.location.hash,/user-completed/);});
  await check('explicit empty QA is preserved; report/review/research disabled; continue is available',async()=>{
    const e=workbench('#session='+qa.session_id,{'suit-recent-answered-interview-v1':JSON.stringify({session_id:user.session_id})});await tick();
    assert.equal(e.sandbox.location.hash,'#session='+qa.session_id);const buttons=e.get('selectedSession').querySelectorAll('button');
    for(const text of ['逐题复核','查看整场报告','高级研究'])assert.ok(buttons.find(b=>b.textContent===text).disabled);
    assert.ok(buttons.some(b=>b.textContent.includes('继续候选人')&&!b.disabled));assert.match(e.get('selectedSession').textContent,/尚无已提交回答/);
  });
  await check('technical records remain searchable and can be explicitly shown',async()=>{
    const e=workbench();await tick();e.get('showTechnical').checked=true;e.get('showTechnical').fire('change');assert.match(e.get('sessionList').textContent,/SIM-MCP-QA-STDIO/);
    e.get('showTechnical').checked=false;e.get('showTechnical').fire('change');e.get('sessionSearch').value='qa-empty';e.get('sessionSearch').fire('input');assert.match(e.get('sessionList').textContent,/SIM-MCP-QA-STDIO/);
  });
  await check('completion links use only same-session HR capability and remember ID without credentials',async()=>{
    const e=candidate({[user.session_id]:{token:'fixture-own-hr'},[qa.session_id]:{token:'fixture-other-hr'}});await tick();
    const links=e.get('completionRoleLinks').children;assert.equal(links.length,2);assert.ok(!e.get('completionRoleLinks').hidden);assert.ok(links[0].classList.contains('primary'));assert.equal(links[0].textContent,'查看本场报告');
    assert.deepEqual(Array.from(links,n=>new URL(n.href,'http://fixture').pathname),['/interview-report','/workbench']);
    for(const link of links.slice(0,1)){const p=new URLSearchParams(new URL(link.href,'http://fixture').hash.slice(1));assert.equal(p.get('session'),user.session_id);assert.equal(p.get('token'),'fixture-own-hr');}
    assert.match(e.back.href,/session=user-completed/);const remembered=e.local.getItem('suit-recent-answered-interview-v1');assert.equal(JSON.parse(remembered).session_id,user.session_id);assert.ok(!remembered.includes('token'));assert.equal(e.requests.length,1);
  });
  await check('candidate-only completion never borrows another session HR token or fetches role recovery',async()=>{
    const e=candidate({[qa.session_id]:{token:'fixture-other-hr'}});await tick();assert.equal(e.get('completionRoleLinks').children.length,1);assert.equal(e.get('completionRoleLinks').children[0].textContent,'返回工作台');assert.ok(!e.get('completionRoleLinks').children[0].href.includes('token'));assert.match(e.get('completionAccessNote').textContent,/报告由指导老师查看/);assert.deepEqual(e.requests,['/v1/interviews/'+user.session_id]);
  });
  await check('idle candidate hash change reloads instead of continuing old captured session',async()=>{const e=candidate();await tick();e.sandbox.location.hash='#session=new-id&token=fixture-new';e.fire('hashchange');await tick();assert.equal(e.reloads,1);});
  await check('device preview does not begin formal recording or block an idle session switch',async()=>{
    const e=candidate();await tick();e.get('consent').checked=true;
    const tracks=['audio','video'].map(kind=>({kind,readyState:'live',enabled:true,stop(){this.readyState='ended';},addEventListener(){}}));
    e.sandbox.navigator={mediaDevices:{getUserMedia:async()=>({getTracks:()=>tracks})}};
    e.sandbox.MediaRecorder=class{static isTypeSupported(){return true;}constructor(){this.state='inactive';}start(){this.state='recording';}};
    e.get('recordButton').click();await tick();e.sandbox.location.hash='#session=another&token=fixture';e.fire('hashchange');await tick();
    assert.equal(e.reloads,1);assert.match(e.get('deviceStatus').textContent,/尚未正式录制/);
  });
  console.log('Completed '+count+' synthetic navigation checks; real browser QA is separate.');
})().catch(error=>{console.error(error);process.exitCode=1;});
