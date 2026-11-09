/* A formal answer is created only after every media chunk is verified. */
(() => {
  'use strict';
  async function digest(blob) {
    if (!globalThis.crypto?.subtle) throw new Error('当前环境无法校验上传文件，请通过本机 localhost 或 HTTPS 打开页面。');
    const bytes = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
    return Array.from(new Uint8Array(bytes), value => value.toString(16).padStart(2, '0')).join('');
  }
  async function request(url, token, method = 'GET', body, checksum) {
    const headers = new Headers({Authorization:'Bearer ' + token});
    if (body instanceof Blob) { headers.set('Content-Type','application/octet-stream'); headers.set('X-Chunk-SHA256',checksum); }
    else if (body !== undefined) headers.set('Content-Type','application/json');
    let response;
    try { response = await fetch(url,{method,headers,cache:'no-store',body:body instanceof Blob ? body : body === undefined ? undefined : JSON.stringify(body)}); }
    catch (_) { throw new Error('连接中断，已上传分片和浏览器草稿会保留。恢复网络后点击继续上传。'); }
    let data = {}; try { data = await response.json(); } catch (_) { /* Preserve HTTP failure status. */ }
    if (!response.ok) {
      const detail = data.detail, error = new Error(typeof detail === 'string' ? detail : detail?.message || '上传未完成（' + response.status + '），请重试。');
      error.status=response.status; error.code=detail?.code; throw error;
    }
    return data;
  }
  async function upload({endpoint,token,draft,persist,onProgress=()=>{}}) {
    if (!(draft.blob instanceof Blob) || !draft.blob.size) throw new Error('没有可上传的音视频，请重新选择文件或录制。');
    const base=endpoint+'/uploads';
    onProgress({phase:'checking',sent:0,total:draft.blob.size});
    if (!draft.upload_sha256) { draft.upload_sha256=await digest(draft.blob); await persist(); }
    let status;
    if (draft.upload_id) {
      try { status=await request(base+'/'+encodeURIComponent(draft.upload_id),token); }
      catch(error) { if(error.status!==404&&error.status!==410) throw error; draft.upload_id=''; await persist(); }
    }
    if (!status) {
      status=await request(base,token,'POST',{round_id:draft.round_id,attempt_id:draft.attempt_id,filename:draft.name||'answer.webm',total_size:draft.blob.size,chunk_size:1048576,sha256:draft.upload_sha256,idempotency_key:draft.upload_init_key||draft.attempt_id});
      if(!status.upload_id) throw new Error('服务未返回上传编号，草稿已保留，请重试。');
      draft.upload_id=status.upload_id; await persist();
    }
    const chunkSize=Number(status.chunk_size), totalChunks=Math.ceil(draft.blob.size/chunkSize);
    if(!Number.isInteger(chunkSize)||chunkSize<65536||chunkSize>4194304||totalChunks!==Number(status.total_chunks)||Number(status.total_size)!==draft.blob.size) throw new Error('上传配置与本轮文件不一致，请保留草稿并联系组织者。');
    const received=new Set((status.received_chunks||[]).filter(i=>Number.isInteger(i)&&i>=0&&i<totalChunks));
    const receivedBytes=()=>Array.from(received).reduce((sum,i)=>sum+Math.min(chunkSize,draft.blob.size-i*chunkSize),0);
    onProgress({phase:'uploading',sent:receivedBytes(),total:draft.blob.size,resumed:received.size>0});
    for(let index=0;index<totalChunks;index++) {
      if(received.has(index)) continue;
      const chunk=draft.blob.slice(index*chunkSize,Math.min((index+1)*chunkSize,draft.blob.size));
      await request(base+'/'+encodeURIComponent(draft.upload_id)+'/chunks/'+index,token,'PUT',chunk,await digest(chunk));
      received.add(index); onProgress({phase:'uploading',sent:receivedBytes(),total:draft.blob.size});
    }
    onProgress({phase:'finalizing',sent:draft.blob.size,total:draft.blob.size});
    return request(base+'/'+encodeURIComponent(draft.upload_id)+'/complete',token,'POST',{transcript:draft.text||'',language:'zh',replace_answer_id:null,idempotency_key:draft.idempotency_key});
  }
  globalThis.InterviewUploads=Object.freeze({upload,digest});
})();
