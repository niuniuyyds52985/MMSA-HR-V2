// Source-only delivery UI: real app.js, mocked local API, no participant data.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const http = require("node:http");
const path = require("node:path");
const {once} = require("node:events");
const {chromium} = require(require.resolve("playwright", {paths:[process.cwd(),path.resolve(path.dirname(process.execPath),"..")] }));

(async () => {
  const app = await fs.readFile(path.join(process.cwd(), "web/v2/app.js"));
  let sampleCount = 0, created = 0, imports = 0;
  const server = http.createServer((req, res) => {
    const address = new URL(req.url, "http://127.0.0.1");
    if(address.pathname === "/v2/ui/app.js") {
      res.setHeader("Content-Type", "text/javascript"); return res.end(app);
    }
    if(address.pathname.startsWith("/v2/api/")) {
      res.setHeader("Content-Type", "application/json");
      let payload;
      if(address.pathname === "/v2/api/status") payload = {demo_resume_count:sampleCount};
      else if(address.pathname === "/v2/api/batches") {
        if(req.method === "POST") { created++; payload={id:"fixture"}; }
        else payload = {batches:[]};
      } else if(address.pathname === "/v2/api/batches/fixture") payload={id:"fixture",name:"自有简历批次",candidates:[],stats:{}};
      else if(address.pathname === "/v2/api/batches/fixture/import-demo") {imports++;payload={};}
      else {res.statusCode=404;payload={detail:"unexpected fixture request"};}
      return res.end(JSON.stringify(payload));
    }
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.end('<div id="role-label"></div><div id="notices" hidden></div><main id="workspace" aria-busy="true"></main><script src="/v2/ui/app.js"></script>');
  });
  server.listen(0, "127.0.0.1"); await once(server,"listening");
  let browser;
  try {
    browser = await chromium.launch({channel:"chrome",headless:true});
    const page = await browser.newPage();
    const base = "http://127.0.0.1:" + server.address().port + "/v2/";
    await page.goto(base);
    await page.locator('#workspace[aria-busy="false"]').waitFor();
    assert.equal(await page.locator("#open-cohort").count(),0);
    assert.equal(created,0);
    assert.match(await page.locator("#workspace").innerText(),/自己的 DOCX/);
    await page.locator("#batch-name").fill("独立使用批次");
    await page.locator('#batch-form button[type="submit"]').click();
    await page.waitForURL(/batch=fixture/);
    await page.locator("#import-files").waitFor();
    assert.equal(await page.locator("#import-demo").count(),0);
    assert.equal(await page.locator('#resumes[multiple][accept=".docx"]').count(),1);
    assert.equal(created,1);
    assert.equal(imports,0);

    sampleCount=3;
    await page.goto(base);
    await page.locator("#open-cohort").waitFor();
    assert.match(await page.locator("#open-cohort").innerText(),/3 人/);
    sampleCount=0; // The folder disappeared after this page was displayed.
    await page.locator("#open-cohort").click();
    await page.locator("#notices").filter({hasText:"自己的 DOCX"}).waitFor();
    assert.equal(created,1,"stale demo availability must not create an empty cohort");
    assert.equal(imports,0);
    assert.equal(await page.locator("#open-cohort").count(),0);

    sampleCount=2;
    await page.goto(base+"?batch=fixture");
    await page.locator("#import-demo").waitFor();
    assert.match(await page.locator("#import-demo").innerText(),/2 份/);
    console.log("PASS empty delivery, own DOCX entry, actual sample count, and stale demo availability");
  } finally {
    if(browser) await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
