/* Transport failure/reload fixtures. No server, credentials, real media or cloud. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict'),{webcrypto}=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'../site/js/resumable-upload.js'),'utf8');
function server() {
  const received=new Map(),requests=[]; let failure=true,answer=null,initPayload=null,completePayload=null;
  const sandbox={Blob,Headers,Set,Array,Uint8Array,Math,Number,JSON,crypto:webcrypto};
  const status=()=>({upload_id:'upload-fixture',status:answer?'completed':'uploading',chunk_size:1048576,total_chunks:3,total_size:2097165,received_chunks:[...received.keys()]});
  sandbox.fetch=async(url,options={})=>{
    const method=options.method||'GET';requests.push({url,method});assert.equal(options.headers.get('Authorization'),'Bearer fixture-token');
    const respond=(value,code=200)=>({ok:code>=200&&code<300,status:code,json:async()=>value});
    if(url.endsWith('/uploads')&&method==='POST') {initPayload=JSON.parse(options.body);return respond(status());}
    if(url.endsWith('/upload-fixture')&&method==='GET')return respond(status());
    const match=url.match(/\/chunks\/(\d+)$/);
    if(match){const index=Number(match[1]);assert.equal(options.headers.get('Content-Type'),'application/octet-stream');assert.match(options.headers.get('X-Chunk-SHA256'),/^[a-f0-9]{64}$/);if(index===1&&failure){failure=false;throw new Error('Synthetic disconnect');}received.set(index,options.body);return respond(status());}
    if(url.endsWith('/complete')) {const next=JSON.parse(options.body);if(completePayload)assert.deepEqual(next,completePayload);completePayload=next;answer={answer_id:'same-answer',state:{status:'answering'}};return respond(answer);}
    throw new Error('Unexpected request '+url);
  };
  vm.runInNewContext(source,sandbox);
  return {sandbox,requests,received,get init(){return initPayload;},get complete(){return completePayload;}};
}
(async()=>{
  const s=server(),original={round_id:'r',attempt_id:'attempt-1',idempotency_key:'completion-1',text:'模拟转写',name:'answer.webm',blob:new Blob([new Uint8Array(2097165)],{type:'video/webm'})};
  let persisted=null; const phases=[];
  const run=draft=>s.sandbox.InterviewUploads.upload({endpoint:'/v1/interviews/s',token:'fixture-token',draft,persist:async()=>{persisted=structuredClone(draft);},onProgress:p=>phases.push(p)});
  await assert.rejects(run(original),/连接中断/);assert.deepEqual([...s.received.keys()],[0]);assert.ok(persisted.upload_id);assert.ok(!s.complete);assert.equal(s.init.sha256,await s.sandbox.InterviewUploads.digest(original.blob));
  const requestCount=s.requests.length,resumed=structuredClone(persisted),result=await run(resumed);
  assert.equal(result.answer_id,'same-answer');assert.equal(s.requests[requestCount].method,'GET');assert.deepEqual(s.requests.slice(requestCount).filter(r=>r.method==='PUT').map(r=>r.url.split('/').pop()),['1','2']);assert.equal(s.complete.idempotency_key,'completion-1');assert.equal(s.complete.transcript,'模拟转写');assert.equal(phases.at(-1).phase,'finalizing');
  const afterComplete=s.requests.length;assert.equal((await run(resumed)).answer_id,'same-answer');assert.equal(s.requests.slice(afterComplete).filter(r=>r.method==='PUT').length,0);
  console.log('PASS disconnect preserves upload identity; reload sends only missing chunks; completion retry keeps same answer request');
  const before=s.requests.length;await assert.rejects(run({...resumed,blob:new Blob()}),/没有可上传/);assert.equal(s.requests.length,before);console.log('PASS empty media never starts an upload');
})().catch(error=>{console.error(error);process.exitCode=1;});
