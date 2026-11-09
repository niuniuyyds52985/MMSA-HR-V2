// UI contract only. Backend acoustic/ASR gates use test_preflight.py; all audio
// and recognition responses in this browser test are explicitly synthetic.
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const { once } = require("node:events");
const { chromium } = require(require.resolve("playwright", {paths:[process.cwd(),path.resolve(path.dirname(process.execPath),"..")] }));

(async () => {
  const js = await fs.readFile(path.join(process.cwd(),"web/v2/device.js"));
  const server = http.createServer((req,res) => {
    res.setHeader("Content-Type", req.url === "/device.js" ? "text/javascript" : "text/html; charset=utf-8");
    res.end(req.url === "/device.js" ? js : '<div id="trial"></div><div id="answer"></div><script src="/device.js"></script>');
  });
  server.listen(0,"127.0.0.1"); await once(server,"listening");
  let browser;
  try {
    browser = await chromium.launch({channel:"chrome",headless:true});
    const page = await browser.newPage();
    await page.addInitScript(() => {
      window.calls = 0;
      window.audioContexts = [];
      navigator.mediaDevices.getUserMedia = async constraints => {
        window.calls++;
        const context = new AudioContext(), source = context.createOscillator(), target = context.createMediaStreamDestination();
        source.frequency.value = 500; source.connect(target); source.start(); await context.resume();
        window.audioContexts.push(context);
        const microphone = target.stream.getAudioTracks()[0];
        Object.defineProperty(microphone,"label",{value:constraints.audio?.deviceId?.exact === "fixture-2" ? "合成麦克风2" : "实际系统默认合成麦克风"});
        return target.stream;
      };
      navigator.mediaDevices.enumerateDevices = async () => [
        {kind:"audioinput",deviceId:"fixture-1",label:"合成麦克风1"},
        {kind:"audioinput",deviceId:"fixture-2",label:"合成麦克风2"}];
    });
    await page.goto("http://127.0.0.1:"+server.address().port);
    await page.evaluate(() => {
      window.result = {status:"pass",messages:["合成测试响应"],transcript:"这是一段合成测试文字"};
      window.answer = InterviewDevice.mount(document.querySelector("#answer"),{mode:"answer"});
      window.trial = InterviewDevice.mount(document.querySelector("#trial"),{mode:"test",onPreflight:async () => window.result});
    });
    const target = page.locator("#trial");
    assert.equal(await target.locator('[data-av="audioinput"]').isVisible(),true,"microphone selection must be visible without expanding details");
    await page.locator('#answer [data-av="start"]').click();
    assert.equal(await page.evaluate(() => window.calls),0,"untested recording must not start");
    await page.locator('#answer [data-av="status"]').filter({hasText:"先返回设备测试"}).waitFor();
    await target.locator('[data-av="mode"]').selectOption("audio");
    await target.locator('[data-av="start"]').click();
    await target.locator('[data-av="preflight"][data-status="pass"]').waitFor({timeout:15000});
    await target.locator('[data-av="source"]').filter({hasText:"最近采集音源：实际系统默认合成麦克风"}).waitFor();
    assert.equal(await target.locator('[data-av="confirm"]').isDisabled(),true,"must listen before confirmation");
    assert.equal(await page.evaluate(() => trial.recordingReady),false);
    await target.locator('[data-av="audio-play"]').evaluate(el=>el.play());
    await target.locator('[data-av="confirm"]:enabled').waitFor();
    await target.locator('[data-av="confirm"]').check();
    assert.equal(await page.evaluate(() => trial.recordingReady),true);
    assert.equal(await page.evaluate(() => answer.recordingReady),true,"approval persists across question mounts");
    await page.locator('#answer [data-av="start"]').click();
    await page.locator('#answer [data-av="stop"]').waitFor();
    await page.locator('#answer [data-av="stop"]').click();
    await page.waitForFunction(()=>!answer.busy);
    assert.equal(await page.evaluate(() => window.calls),2);
    await page.evaluate(()=>navigator.mediaDevices.dispatchEvent(new Event("devicechange")));
    assert.equal(await page.evaluate(()=>trial.recordingReady),false,"device change invalidates the old check");
    assert.equal(await target.locator('[data-av="confirm"]').isDisabled(),true,"old playback cannot approve a changed input");

    await target.locator('[data-av="audioinput"]').selectOption("fixture-2");
    assert.equal(await page.evaluate(() => trial.recordingReady),false,"changing microphone invalidates approval");
    await page.evaluate(() => { window.result={status:"fail",messages:["合成低频噪音失败响应"],transcript:""}; });
    await target.locator('[data-av="start"]').click();
    await target.locator('[data-av="preflight"][data-status="fail"]').waitFor({timeout:15000});
    await target.locator('[data-av="source"]').filter({hasText:"最近采集音源：合成麦克风2"}).waitFor();
    await target.locator('[data-av="preflight-message"]').filter({hasText:"切换麦克风"}).waitFor();
    await target.locator('[data-av="audio-play"]').evaluate(el=>el.play());
    await page.waitForFunction(()=>document.querySelector('#trial [data-av="audio-play"]').currentTime>.7);
    assert.equal(await target.locator('[data-av="confirm"]').isDisabled(),true,"noise cannot pass by checkbox");
    assert.equal(await page.evaluate(()=>trial.recordingReady),false);
    await page.evaluate(()=>{trial.dispose();answer.dispose();audioContexts.forEach(c=>c.close());});
    console.log("PASS trial playback confirmation, per-microphone readiness, failed-noise blocking, and cross-question reuse");
  } finally { if(browser) await browser.close(); await new Promise(resolve=>server.close(resolve)); }
})().catch(error=>{console.error(error);process.exitCode=1;});
