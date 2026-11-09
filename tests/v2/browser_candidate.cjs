// Run from the project with node tests/v2/browser_candidate.cjs. Requires Playwright,
// JSZip, Chrome, and the project Python environment. All interview data is temporary.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const { pathToFileURL } = require("node:url");
const { setTimeout: delay } = require("node:timers/promises");

const project = path.resolve(__dirname, "../..");
const searchPaths = [project, path.resolve(path.dirname(process.execPath), "..")];
const { chromium } = require(require.resolve("playwright", { paths: searchPaths }));
const JSZip = require(require.resolve("jszip", { paths: searchPaths }));
const python = process.env.MMSA_TEST_PYTHON || path.join(project, ".venv-suit", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");

function startServer(directory) {
  const code = "from src.mmsa_v2.access import operator_key; operator_key(); import uvicorn; uvicorn.run('src.mmsa_v2.api:app', host='127.0.0.1', port=0, access_log=False)";
  const child = spawn(python, ["-u", "-c", code], {
    cwd: project, windowsHide: true, env: { ...process.env, MMSA_V2_DATA_DIR: directory, MMSA_V2_CONTENT_PROVIDER: "local_demo" }
  });
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Timed out starting the test server")), 20000);
    const lines = readline.createInterface({ input: child.stderr });
    const stderr = [];
    lines.on("line", line => {
      stderr.push(line);
      const address = line.match(/Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/);
      if (address) { clearTimeout(timer); resolve(address[1]); }
    });
    child.once("error", err => { clearTimeout(timer); reject(err); });
    child.once("exit", () => { clearTimeout(timer); reject(new Error(stderr.join("\n"))); });
  });
  return { child, ready };
}

async function createInterview(request) {
  async function post(url, options) {
    const response = await request.post("/v2/api" + url, options);
    assert.equal(response.status(), 200, (await response.text()).slice(0, 180));
    return response.json();
  }
  const batch = await post("/batches", { data: { name: "浏览器验收" } });
  const zip = new JSZip();
  const resume = ["模拟同学", "校园经历", "迎新培训项目", "2025.03–2025.05",
    "我牵头组建小组，协调分工、带教新人。每期复盘反馈，并调整培训方法。"];
  const paragraphs = resume.map(text => "<w:p><w:r><w:t>" + text + "</w:t></w:r></w:p>").join("");
  zip.file("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + paragraphs + '</w:body></w:document>');
  const imported = await post("/batches/" + batch.id + "/resumes", {
    multipart: { file: { name: "模拟同学.docx", mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buffer: await zip.generateAsync({ type: "nodebuffer" }) } }
  });
  return post("/batches/" + batch.id + "/sessions", { data: { candidate_id: imported.candidates[0].id } });
}

function device(page, mode, control) {
  return page.locator('.av-device[data-mode="' + mode + '"] [data-av="' + control + '"]');
}

async function progress(page, id, answered, total) {
  await page.waitForFunction(({ id, expected }) => document.getElementById(id)?.textContent.replace(/\s/g, "").includes(expected), {
    id, expected: "已作答" + answered + "/" + total + "题"
  });
}

async function openReviewTools(teacher) {
  const tools = teacher.locator("details#review-tools");
  if (!await tools.evaluate(el => el.open)) {
    await teacher.locator('a[href="#review-tools"]:not([data-review-question]):visible').first().click();
    await teacher.locator("#review-tools[open]").waitFor();
  }
}

async function reportOnEntry(teacher, candidate) {
  const frame = teacher.locator("iframe#teacher-report");
  const report = frame.contentFrame();
  const title = report.locator(".hd-info h1").filter({hasText:candidate});
  await title.waitFor();
  assert.equal(await report.locator("h1").count(), 1);
  const titleBox = await title.boundingBox();
  assert(titleBox.y >= 0 && titleBox.y + titleBox.height <= teacher.viewportSize().height);
  assert.deepEqual(await report.locator(".rep > .sec").allTextContents(), [
    "核心评分", "能力画像", "面试过程状态感知（多模态辅助）", "综合意见"
  ]);
  assert.equal(await report.locator(".score-row > .sc").count(),3);
  assert.equal(await report.locator(".charts > .card canvas").count(),3);
  assert.equal(await report.locator(".emotion .bar-track").count(),5);
  assert.equal(await report.locator(".rep").evaluate(el => getComputedStyle(el).maxWidth),"1000px");
  assert(await frame.evaluate(el => !!(el.compareDocumentPosition(document.querySelector("#review-tools")) &
    Node.DOCUMENT_POSITION_FOLLOWING)), "personal report must precede the review workspace");
  assert.equal(await teacher.locator("#review-tools").evaluate(el => el.open), false);
  return report;
}

async function reportGeometry(view) {
  return view.locator(".rep").evaluate(root => [".brand",".hd",".avatar",".score-row",".charts",".emotion",".verdict"].map(selector => {
    const element = root.querySelector(selector);
    const box = element.getBoundingClientRect(), style = getComputedStyle(element);
    return [selector, Math.round(box.x), Math.round(box.y), Math.round(box.width), style.borderRadius, style.padding];
  }));
}

async function playReportVideo(report, questionId, expectedText) {
  const appendix = report.locator("details#report-appendix");
  if (!await appendix.evaluate(el => el.open)) await appendix.locator("summary").first().click();
  const entry = report.locator("#q-" + questionId);
  if (!await entry.evaluate(el => el.open)) await entry.locator("summary").first().click();
  assert((await entry.locator("blockquote").first().textContent()).includes(expectedText));
  await playRecording(entry.locator("video"));
}

async function playRecording(player) {
  await player.waitFor({ state: "visible" });
  await player.evaluate(async el => {
    const played = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("No playable frames")), 7000);
      el.addEventListener("timeupdate", function check() {
        if (el.currentTime > 0) { el.removeEventListener("timeupdate", check); clearTimeout(timer); resolve(); }
      });
    });
    el.currentTime = 0;
    await el.play();
    await played;
    el.pause();
  });
}

async function fakeCamera(directory) {
  // A small looping YUV clip also works on Windows hosts with no display or camera.
  const camera = path.join(directory, "camera.y4m");
  const width = 160, height = 120;
  const chunks = [Buffer.from("YUV4MPEG2 W160 H120 F15:1 Ip A1:1 C420jpeg\n")];
  for (let i = 0; i < 15; i++) {
    const frame = Buffer.alloc(width * height * 3 / 2, 128);
    frame.fill(32 + i * 12, 0, width * height);
    chunks.push(Buffer.from("FRAME\n"), frame);
  }
  await fs.writeFile(camera, Buffer.concat(chunks));
  return camera;
}

async function recordAndPlay(page, mode, trackType) {
  await device(page, mode, "mode").selectOption(trackType);
  if (mode === "test") {
    await device(page, mode, "open").click();
    await page.waitForFunction(() => {
      const stream = document.querySelector('[data-av="live"]').srcObject;
      return stream?.getVideoTracks().length === 1 && stream?.getAudioTracks().length === 1;
    });
    assert(await device(page, mode, "meter").isVisible());
  }
  await device(page, mode, "start").click();
  if (mode === "answer") {
    await device(page, mode, "stop").waitFor({ state: "visible" });
    await delay(1200); // Give the real MediaRecorder enough samples for decoding and playback.
    await device(page, mode, "stop").click();
  }
  try {
    await playRecording(device(page, mode, trackType + "-play"));
  } catch (error) {
    throw new Error(mode + " " + trackType + " recording: " + await device(page, mode, "status").textContent(), { cause: error });
  }
}

async function commit(page, qid) {
  await page.locator("#save-answer").click();
  await page.waitForFunction(oldId => {
    const field = document.querySelector("#question-picker");
    return field ? field.value !== oldId : !!document.querySelector("#finish");
  }, qid);
}

async function deniedDevice(browser, url, errorName) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await context.addInitScript(errorName => {
    navigator.mediaDevices.getUserMedia = async () => { throw new DOMException("Test device unavailable", errorName); };
  }, errorName);
  const page = await context.newPage();
  await page.goto(url);
  await page.locator("#opening .campaign-enter").click();
  await page.locator("#opening").waitFor({state:"hidden"});
  await device(page, "test", "open").click();
  const message = errorName === "NotAllowedError" ? /权限|授权|允许/ : /找不到|未找到/;
  await device(page, "test", "status").filter({ hasText: message }).waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "device test overflows on mobile");
  await page.locator("#enter-without-recording").click();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "question overflows on mobile");
  return { context, page };
}

async function checkBrowser(browser, origin, directory) {
  const staff = await browser.newContext({ baseURL: origin });
  const key = await fs.readFile(path.join(directory, ".operator-key"), "ascii");
  const login = await staff.request.post("/v2/auth", { headers: { Authorization: "Bearer " + key } });
  assert(login.ok());
  const session = await createInterview(staff.request);
  let teacher = await staff.newPage();
  await teacher.goto("/v2/?session=" + session.id);
  assert.equal(session.interview_mode, "short_5_3");
  assert.equal(session.expected_questions, 8);
  const total = session.expected_questions;
  assert.equal(session.plan.length, 5);
  assert(session.plan.every(q => q.kind === "choice" && q.stage === "screening" && q.id.endsWith("-I")));
  await teacher.locator("#workspace[aria-busy=false]").waitFor();
  assert.equal(await teacher.locator("#notices.alert").count(), 0, await teacher.locator("#notices").textContent());
  await reportOnEntry(teacher, session.candidate);
  const link = teacher.locator('a[href^="/v2/c/"]:visible').first();
  const url = new URL(await link.getAttribute("href"), origin).href;
  const candidate = await browser.newContext({ permissions: ["camera", "microphone"] });
  // This suite verifies the interview UI with Chrome's synthetic tone/video.
  // Acoustic rejection is separately tested in test_preflight.py. A clearly
  // labelled fixture permits recording here without claiming the tone is speech.
  await candidate.route("**/v2/api/c/*/preflight", route => route.fulfill({
    status:200, contentType:"application/json", body:JSON.stringify({status:"pass",transcript:"合成测试响应，并非实际语音识别",messages:["浏览器业务流程的预检夹具"],local_only:true,network_used:false})
  }));
  const privateReport = await candidate.request.get(origin + "/v2/api/sessions/" + session.id + "/report.html?workbench=true");
  assert.equal(privateReport.status(), 401);
  await candidate.addInitScript(() => {
    const capture = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    window.testTracks = [];
    navigator.mediaDevices.getUserMedia = async settings => {
      const stream = await capture(settings);
      window.testTracks.push(...stream.getTracks());
      return stream;
    };
  });
  const student = await candidate.newPage();
  const api = origin + "/v2/api/c/" + session.token;
  const state = async () => (await candidate.request.get(api)).json();
  await student.goto(url);
  await student.locator("#opening .campaign-enter").click();
  await student.locator("#opening").waitFor({state:"hidden"});
  await recordAndPlay(student, "test", "video");
  await device(student, "test", "preflight").filter({hasText:"等待试听确认"}).waitFor();
  await device(student, "test", "video-play").evaluate(el=>el.play());
  await device(student, "test", "confirm").waitFor();
  await student.waitForFunction(()=>!document.querySelector('[data-mode="test"] [data-av="confirm"]').disabled);
  await device(student, "test", "video-play").evaluate(el=>el.pause());
  await device(student, "test", "confirm").check();
  assert.deepEqual((await state()).answers, {}, "test recording mutated the interview");
  await student.locator("#enter-interview").click();
  await student.waitForFunction(() => window.testTracks.every(track => track.readyState === "ended"));
  await student.locator("#introduction-device").waitFor();
  await recordAndPlay(student, "answer", "audio");
  const introductionText = "大家好，我是模拟同学，平时喜欢阅读，今天来体验模拟面试。";
  await student.locator("#written").fill(introductionText);
  await student.locator("#save-introduction").click();
  await student.locator("#question-picker").waitFor();
  const afterIntroduction = await state();
  assert.equal(afterIntroduction.introduction.text, introductionText);
  assert(afterIntroduction.introduction.media.duration_seconds > 0);
  assert.deepEqual(afterIntroduction.answers, {}, "warm-up must be separate from scored answers");
  assert.equal(afterIntroduction.plan.length, 5);
  await progress(student, "candidate-progress", 0, total);
  await progress(teacher, "session-progress", 0, total);
  for (const [i, question] of session.plan.entries()) {
    assert.equal(await student.locator("#question-picker").inputValue(), question.id);
    assert.equal(await student.locator("#answer-device").count(), 0);
    assert(!(await student.locator(".interview-main .between").innerText()).includes(question.factor_name));
    assert(!(await student.locator("#question-picker").innerText()).includes(question.factor_name));
    const choices = student.locator('input[name="choice"]');
    await choices.nth(i % await choices.count()).check();
    await commit(student, question.id);
    await progress(student, "candidate-progress", i + 1, total);
    const current = await state();
    assert.equal(Object.keys(current.answers).length, i + 1);
    assert.equal(current.plan.length, i === 4 ? 8 : 5);
  }
  const beforeRecording = await state();
  const spoken = beforeRecording.plan.slice(5);
  assert(spoken.every(q => q.kind === "text" && q.stage === "interview"));
  assert.equal(new Set(beforeRecording.plan.map(q => q.id)).size, 8);
  assert.equal(new Set(beforeRecording.plan.map(q => q.factor_id)).size, 8);
  const [first, videoQuestion, otherQuestion] = spoken.map(q => q.id);
  assert.equal(await student.locator("#question-picker").inputValue(), first);
  await recordAndPlay(student, "answer", "audio");
  assert.deepEqual((await state()).answers, beforeRecording.answers, "unsubmitted recording mutated the interview");
  await progress(teacher, "session-progress", 5, total);
  await teacher.waitForFunction(() => {
    const view = document.querySelector("iframe#teacher-report")?.contentWindow;
    return view?.Chart?.getChart("r2")?.data.datasets[0].data.filter(Number.isFinite).length === 5;
  });
  await commit(student, first);
  await progress(student, "candidate-progress", 6, total);
  const withRecording = await state();
  assert(withRecording.answers[first].media.duration_seconds > 0);
  const lockedChoice = session.plan[0];
  const differentOption = lockedChoice.options.find(o => o.id !== withRecording.answers[lockedChoice.id].value);
  const update = await candidate.request.post(api + "/answers/" + lockedChoice.id, {
    data: { value: differentOption.id, version: withRecording.version }
  });
  assert.equal(update.status(), 409, "changing an upstream choice must not discard a saved spoken answer");
  assert.equal((await state()).version, withRecording.version);
  await progress(teacher, "session-progress", 6, total);
  await openReviewTools(teacher);
  await teacher.locator("#question-view").selectOption("5");
  await playRecording(teacher.locator("#q-evidence audio"));
  await student.locator("#question-picker").selectOption(first);
  await playRecording(device(student, "answer", "audio-play"));
  const missing = await deniedDevice(browser, url, "NotFoundError");
  assert.equal(await missing.page.locator("#question-picker").inputValue(), videoQuestion);
  await missing.context.close();
  await student.locator("#question-picker").selectOption(videoQuestion);
  await recordAndPlay(student, "answer", "video");
  const text = "我为小组录制了演示视频。";
  await student.locator("#written").fill(text);

  // Force an authentic version conflict: the other tab saves a different question
  // after this tab has prepared a recording. The original bytes and text must survive.
  const denied = await deniedDevice(browser, url, "NotAllowedError");
  const other = denied.page;
  await progress(other, "candidate-progress", 6, total);
  await other.locator("#question-picker").selectOption(otherQuestion);
  const otherText = "我在另一标签页保存了这一题。";
  await other.locator("#written").fill(otherText);
  await commit(other, otherQuestion);
  await denied.context.close();
  const uploadCodes = [];
  student.on("response", response => {
    if (response.request().method() === "POST" && response.url() === api + "/answers/" + videoQuestion + "/media") {
      uploadCodes.push(response.status());
    }
  });
  await commit(student, videoQuestion);
  assert.deepEqual(uploadCodes, [409, 200]);
  const recorded = await state();
  assert.equal(recorded.answers[videoQuestion].text, text);
  assert.equal(recorded.answers[otherQuestion].text, otherText);
  assert(recorded.answers[videoQuestion].media.mime.startsWith("video/"));
  assert.deepEqual(recorded.plan, beforeRecording.plan, "spoken questions changed after answers were saved");
  await progress(teacher, "session-progress", 8, total);
  await openReviewTools(teacher);
  await teacher.locator("#question-view").selectOption("6");
  await playRecording(teacher.locator("#q-evidence video"));
  console.log("PASS five choices release three spoken questions, trial recording, audio and video answers, actual two-tab 409/retry, teacher and student playback");
  await student.locator("#finish").click();
  await student.getByText(/面试已提交/).first().waitFor();
  await student.locator("#submission-progress").filter({hasText:"分析已结束，部分评分待核实"}).waitFor({timeout:240000});
  const done = await state();
  assert.equal(done.status, "draft");
  assert.equal(done.auto_scoring, "partial");
  assert(!Object.hasOwn(done, "draft"));
  assert.equal(Object.keys(done.answers).length, done.plan.length);
  await candidate.close();
  await progress(teacher, "session-progress", done.plan.length, done.plan.length);
  await teacher.waitForFunction(() => document.querySelector("#analysis-progress")?.textContent.includes("分析已结束，部分评分待核实"));
  console.log("PASS absent/denied devices, mobile resume, third spoken answer by text, submit and teacher completion (8/8)");
  await teacher.close();
  teacher = await staff.newPage();
  await teacher.goto("/v2/?session=" + session.id);
  const report = await reportOnEntry(teacher, done.candidate);
  const embeddedGeometry = await reportGeometry(report);
  if (process.env.MMSA_TEST_REPORT_SCREENSHOT) {
    await teacher.screenshot({path: process.env.MMSA_TEST_REPORT_SCREENSHOT});
  }
  assert.equal(await report.locator('#report-questions details[id^="q-"]').count(), 8);
  const factorValues = await report.locator("#r2").evaluate(el =>
    window.Chart.getChart(el).data.datasets[0].data);
  assert.equal(factorValues.length, 8);
  const currentReport=await (await staff.request.get("/v2/api/sessions/"+session.id+"/report?version="+done.version)).json();
  assert.equal(currentReport.introduction.text, introductionText);
  assert.equal(currentReport.questions.length, 8, "warm-up must stay outside multimodal scoring");
  assert.equal(currentReport.introduction.processing, undefined, "warm-up must not trigger analysis");
  assert.deepEqual(factorValues,currentReport.factors.map(f=>f.verification_gaps?.length?null:f.rating));
  const saved = await (await staff.request.get("/v2/api/sessions/" + session.id)).json();
  for (const q of session.plan) {
    assert.equal(factorValues[Number(q.factor_id.slice(1)) - 1],saved.answers[q.id].sjt_score);
    assert((await report.locator("#q-"+q.id+" blockquote").textContent()).includes(saved.answers[q.id].answer_label));
  }
  assert.equal(await report.locator("#q-" + first + " audio").count(), 1);
  assert.equal(await report.locator("#q-" + otherQuestion + " blockquote").textContent(), otherText);
  await playReportVideo(report, videoQuestion, text);
  await report.locator('#q-' + videoQuestion + ' [data-review-question]').click();
  await teacher.locator("#review-tools[open]").waitFor();
  assert.equal(await teacher.locator("#question-view").inputValue(), String(done.plan.findIndex(q => q.id === videoQuestion)));
  await playRecording(teacher.locator("#q-evidence video"));
  const reportLink = teacher.locator('#standalone-report');
  let standalone = teacher;
  if (await reportLink.getAttribute("target") === "_blank") {
    [standalone] = await Promise.all([staff.waitForEvent("page"), reportLink.click()]);
  } else await reportLink.click();
  await standalone.waitForURL(url => url.pathname.endsWith("/report.html"));
  await standalone.locator(".hd-info h1").filter({hasText:done.candidate}).waitFor();
  assert.deepEqual(await reportGeometry(standalone),embeddedGeometry);
  assert.equal(await standalone.locator('details[id^="q-"]').count(), done.plan.length);
  await playReportVideo(standalone, videoQuestion, text);
  const exported = await staff.request.post("/v2/api/sessions/"+session.id+"/export",{data:{version:done.version}});
  assert(exported.ok());
  const localPage = await staff.newPage();
  const remoteRequests = [];
  await localPage.route(/^https?:/, route => { remoteRequests.push(route.request().url()); return route.abort(); });
  await localPage.goto(pathToFileURL((await exported.json()).file).href);
  assert.deepEqual(await reportGeometry(localPage), embeddedGeometry);
  assert.deepEqual(await localPage.locator("#r2").evaluate(el => Chart.getChart(el).data.datasets[0].data),factorValues);
  await playReportVideo(localPage, videoQuestion, text);
  const warmup = localPage.locator("#warmup-introduction");
  await warmup.locator("summary").click();
  assert((await warmup.innerText()).includes(introductionText));
  await playRecording(warmup.locator("audio"));
  assert.equal(remoteRequests.length,0,"the exported report should render and play media fully offline");
  console.log("PASS matching teacher, standalone and offline report layout, 8 source-backed factors and original media playback");
}

async function main() {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "mmsa-v2-browser-"));
  const { child, ready } = startServer(directory);
  let browser;
  try {
    const origin = await ready;
    const camera = await fakeCamera(directory);
    browser = await chromium.launch({
      channel: "chrome", headless: true,
      args: ["--use-fake-device-for-media-stream", "--use-file-for-fake-video-capture=" + camera, "--autoplay-policy=no-user-gesture-required"]
    });
    await checkBrowser(browser, origin, directory);
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null && child.pid) {
      const closed = once(child, "close");
      child.kill();
      await closed;
    }
    await fs.rm(directory, { recursive: true, force: true, maxRetries: 3 });
  }
}

main().catch(err => { console.error(err); process.exitCode = 1; });
