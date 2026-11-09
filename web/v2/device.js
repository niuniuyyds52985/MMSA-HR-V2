(() => {
  "use strict";
  // Only remember the devices chosen during this page visit.
  const preferences = { mode: "video", audioinput: "", videoinput: "", approval: null };
  const formats = [".webm", ".mp4", ".mov", ".wav", ".mp3", ".ogg", ".m4a", ".flac"];
  const maxBytes = 100 * 1024 * 1024;

  function errorHint(error) {
    switch (error.name) {
      case "NotAllowedError":
      case "SecurityError":
        return "设备权限未开启。请在地址栏的网站权限中允许使用所选设备，再重试。";
      case "NotFoundError":
      case "OverconstrainedError":
        return "找不到所选设备。请检查连接、更改设备，或选择「仅麦克风」重试。";
      case "NotReadableError":
      case "AbortError":
        return "设备无法打开。请关闭可能占用设备的程序，再重试。";
      default:
        return error.message || "设备连接失败，请重试。";
    }
  }

  function elapsedText(seconds) {
    const n = Math.floor(seconds);
    return [Math.floor(n / 60), n % 60].map(v => String(v).padStart(2, "0")).join(":");
  }

  function mount(host, { mode, savedMedia = null, onPendingChange, onPreflight, introduction = false } = {}) {
    if (!["test", "answer"].includes(mode)) throw new Error("未知的录制模式");
    const trial = mode === "test";
    const submitLabel = introduction ? "保存介绍，进入正式面试" : "保存并继续";
    host.innerHTML = `<section class="av-device" data-mode="${mode}" aria-label="${trial ? "设备检测" : "本题音视频作答"}">
      <h3>${trial ? "麦克风与摄像头检测" : introduction ? "录制自我介绍" : "录制本题回答"}</h3>
      <p class="av-help">${trial ? "请试录一句完整的话，例如「这是我的面试录音测试，我能清楚地听到自己的声音」。本机会检查音轨并识别文字，随后请回放确认。音量条变化本身不代表人声清楚。" : "录制结束后可以回放或重录。确认后点击「" + submitLabel + "」。"}</p>
      <div class="av-settings">
        <label>录制方式<select data-av="mode"><option value="video">摄像头和麦克风</option><option value="audio">仅麦克风</option></select></label>
        <div data-av="devices" class="av-inputs" aria-label="选择输入设备">
          <label>麦克风<select data-av="audioinput"><option value="">系统默认麦克风</option></select></label>
          <label data-av="camera-label">摄像头<select data-av="videoinput"><option value="">系统默认摄像头</option></select></label>
        </div>
      </div>
      <p data-av="source" class="av-source">麦克风尚未连接；连接后会显示实际使用的音源。</p>
      <p class="av-help">持续说话却听不到回放时，请直接在上方切换麦克风，再试录一次。系统默认设备不一定是你正在使用的麦克风。</p>
      <div class="av-monitor" data-av="monitor" hidden>
        <video data-av="live" aria-label="实时摄像头预览" muted autoplay playsinline hidden></video>
        <label class="av-meter">麦克风输入音量 <meter data-av="meter" min="0" max="100" value="0"></meter></label>
        <p data-av="meter-note" class="micro"></p>
      </div>
      <div class="av-actions">
        ${trial ? '<button type="button" data-av="open">开启设备预览</button>' : ""}
        <button type="button" class="primary" data-av="start">${trial ? "试录 5 秒" : "开始录制回答"}</button>
        <button type="button" data-av="stop" hidden>${trial ? "结束试录" : "结束录制"}</button>
        <button type="button" data-av="close" hidden>关闭设备</button>
        <span data-av="elapsed" class="av-timer" hidden></span>
      </div>
      <p data-av="status" class="av-status" role="status"></p>
      <div data-av="playback" class="av-playback" hidden><strong data-av="play-label"></strong>
        <audio data-av="audio-play" controls preload="metadata" aria-label="录音回放" hidden></audio>
        <video data-av="video-play" controls preload="metadata" playsinline aria-label="录像回放" hidden></video>
      </div>
      ${trial ? '<div data-av="preflight" class="av-preflight" hidden><strong data-av="preflight-title"></strong><p data-av="preflight-message"></p><blockquote data-av="preflight-transcript" hidden></blockquote><label class="av-confirm"><input type="checkbox" data-av="confirm" disabled>我已回放，确认人声清楚；识别文字如有显示，与实际所说内容相符。</label><p class="micro">试录只在本机临时处理，不保存到面试，不参与评分，不发送云端。</p></div>' : ""}
      ${trial ? "" : '<div class="av-upload"><label>或选择本机音视频文件<input type="file" data-av="file" accept="' + formats.join(",") + '"></label><span class="micro">单个文件最大 100 MB，最长 30 分钟</span></div>'}
    </section>`;
    const ui = Object.fromEntries(Array.from(host.querySelectorAll("[data-av]"), el => [el.dataset.av, el]));
    ui.mode.value = preferences.mode;
    let phase = "idle", generation = 0, disposed = false, uploading = false;
    let stream = null, recorder = null, meterContext = null, meterFrame = 0, timer = 0;
    let pending = null, clipUrl = "", playKind = "", captureNote = "";
    let sourceLabel = "";
    let statusBeforeUpload = "", inputsRequest = 0;
    let checking = false, listened = false, checkGeneration = 0;
    let preflight = { status: "not_tested", messages: [], transcript: "" };
    const recordingReady = () => Boolean(preferences.approval && preferences.approval.audioinput === preferences.audioinput);

    function renderCheck() {
      if (!trial) return;
      ui.preflight.hidden = preflight.status === "not_tested";
      ui.preflight.dataset.status = preflight.status;
      ui["preflight-title"].textContent = ({checking:"正在本机检查试录…",pass:"已识别到试录原话，等待试听确认",warning:"试录需要人工确认",fail:"试录未通过，请调整后重试"})[preflight.status] || "";
      ui["preflight-message"].textContent = (preflight.messages || []).join(" ") +
        (preflight.status === "fail" ? " 如果说话时仍听不到人声，请在上方切换麦克风后重新试录。" : "");
      ui["preflight-transcript"].textContent = preflight.transcript || "";
      ui["preflight-transcript"].hidden = !preflight.transcript;
      ui.confirm.disabled = checking || !listened || !["pass", "warning"].includes(preflight.status);
    }

    async function inspectTrial(file) {
      const ticket = ++checkGeneration;
      preferences.approval = null;
      listened = false;
      ui.confirm.checked = false;
      checking = true;
      preflight = {status:"checking", messages:["正在检查音轨质量和人声内容。可以先回放试听。"], transcript:""};
      renderCheck(); render();
      try {
        if (typeof onPreflight !== "function") throw new Error("试录检查服务未连接，请回放确认或改用文字作答。");
        const result = await onPreflight(file);
        if (disposed || ticket !== checkGeneration) return;
        if (!["pass", "warning", "fail"].includes(result?.status)) throw new Error("试录检查未返回有效结果，请重新检查。");
        preflight = result;
      } catch (error) {
        if (disposed || ticket !== checkGeneration) return;
        preflight = {status:"warning", messages:[error.message || "本机试录检查暂不可用。请回放确认人声，或改用文字作答。"], transcript:""};
      } finally {
        if (!disposed && ticket === checkGeneration) {
          checking = false;
          status(preflight.status === "fail" ? "试录未通过。请检查麦克风后重新试录。" : "请回放试听，再勾选人声确认。", preflight.status === "fail");
          renderCheck(); render();
        }
      }
    }

    function status(message, error = false) {
      ui.status.textContent = message;
      ui.status.classList.toggle("av-error", error);
    }

    function render() {
      const recording = ["recording", "stopping"].includes(phase);
      const locked = phase === "opening" || recording || uploading || checking;
      for (const input of [ui.mode, ui.audioinput, ui.videoinput, ui.start, ui.file]) {
        if (input) input.disabled = locked;
      }
      ui.start.hidden = recording;
      if (ui.open) {
        ui.open.disabled = locked;
        ui.open.hidden = Boolean(stream) || recording;
      }
      ui.stop.hidden = !recording;
      ui.stop.disabled = phase === "stopping";
      ui.close.hidden = !["opening", "preview"].includes(phase);
      ui.close.textContent = phase === "opening" ? "取消连接" : "关闭设备";
      ui.elapsed.hidden = !recording;
      ui.monitor.hidden = !stream;
      ui.live.hidden = !stream?.getVideoTracks().length;
      ui["camera-label"].hidden = ui.mode.value === "audio";
      ui.playback.hidden = !playKind || phase !== "idle";
      if (sourceLabel) ui.source.textContent = (stream ? "当前实际音源：" : "最近采集音源：") + sourceLabel;
    }

    function releaseDevices() {
      clearInterval(timer);
      cancelAnimationFrame(meterFrame);
      if (meterContext && meterContext.state !== "closed") meterContext.close().catch(() => {});
      meterContext = null;
      ui.meter.value = 0;
      if (stream) stream.getTracks().forEach(track => track.stop());
      stream = null;
      ui.live.srcObject = null;
    }

    function startMeter() {
      const Audio = window.AudioContext || window.webkitAudioContext;
      ui["meter-note"].textContent = "请说一句话，观察音量条是否变化。";
      try {
        meterContext = new Audio();
        const source = meterContext.createMediaStreamSource(stream);
        const analyser = meterContext.createAnalyser();
        analyser.fftSize = 512;
        source.connect(analyser);
        const samples = new Uint8Array(analyser.fftSize);
        let heardVoice = false;
        const update = () => {
          analyser.getByteTimeDomainData(samples);
          let energy = 0;
          for (const sample of samples) energy += ((sample - 128) / 128) ** 2;
          ui.meter.value = Math.min(100, Math.round(Math.sqrt(energy / samples.length) * 350));
          if (!heardVoice && ui.meter.value > 4) {
            heardVoice = true;
            ui["meter-note"].textContent = "检测到音量变化；也可能是噪声。请完成试录和人声检查。";
          }
          meterFrame = requestAnimationFrame(update);
        };
        const context = meterContext;
        context.resume().catch(() => {
          if (meterContext === context) ui["meter-note"].textContent = "请通过录制回放检查声音。";
        });
        update();
      } catch {
        ui["meter-note"].textContent = "音量条暂不可用，请通过录制回放检查声音。";
      }
    }

    async function listInputs() {
      if (!navigator.mediaDevices?.enumerateDevices) return;
      const request = ++inputsRequest;
      const devices = await navigator.mediaDevices.enumerateDevices().catch(() => null);
      if (!devices || disposed || request !== inputsRequest) return;
      for (const [kind, label] of [["audioinput", "麦克风"], ["videoinput", "摄像头"]]) {
        const inputs = devices.filter(d => d.kind === kind && d.deviceId && d.deviceId !== "default");
        const select = ui[kind];
        select.replaceChildren(new Option("系统默认" + label, ""));
        for (const [i, input] of inputs.entries()) {
          select.add(new Option(input.label || label + " " + (i + 1), input.deviceId));
        }
        // Retain an explicit choice when a device is temporarily absent. Falling
        // back silently could capture the wrong microphone after reconnecting.
        if (preferences[kind] && !inputs.some(d => d.deviceId === preferences[kind])) {
          select.add(new Option("已选" + label + "暂不可用，请重新选择", preferences[kind]));
        }
        select.value = preferences[kind];
      }
    }

    function resetPlayback() {
      for (const player of [ui["audio-play"], ui["video-play"]]) {
        if (!player.hasAttribute("src")) continue;
        player.pause();
        player.removeAttribute("src");
        player.load();
        player.hidden = true;
      }
      if (clipUrl) URL.revokeObjectURL(clipUrl);
      clipUrl = "";
      playKind = "";
    }

    function showClip(url, mime, name) {
      playKind = mime.startsWith("audio/") ? "audio" : "video";
      const player = ui[playKind + "-play"];
      player.hidden = false;
      player.src = url;
      ui["play-label"].textContent = name;
    }

    function setClip(file, seconds, interrupted = false) {
      resetPlayback();
      clipUrl = URL.createObjectURL(file);
      const mime = file.type || (/\.(wav|mp3|ogg|m4a|flac)$/i.test(file.name) ? "audio/" : "video/");
      showClip(clipUrl, mime, trial ? "试录回放" : "待保存 · " + file.name);
      if (!trial) {
        pending = { file, seconds };
        onPendingChange?.(pending);
      }
      status(interrupted ? "设备已中断。请回放检查已保留的片段，或重新录制。" :
        trial ? "试录完成，可播放检查。本段试录仅保留在当前页面。" :
        "录制已准备好。回放确认后点击「" + submitLabel + "」。", interrupted);
      if (trial) inspectTrial(file);
    }

    function endRecording(note = "") {
      if (!recorder || phase !== "recording") return;
      captureNote = note;
      phase = "stopping";
      status("正在生成回放…");
      render();
      if (recorder.state !== "inactive") recorder.stop();
    }

    function startRecording(ticket) {
      const video = stream.getVideoTracks().length > 0;
      const types = video ? ["video/webm;codecs=vp8,opus", "video/webm", "video/mp4"] :
        ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
      const mimeType = types.find(type => MediaRecorder.isTypeSupported?.(type));
      const instance = new MediaRecorder(stream, mimeType ? { mimeType } : {});
      const chunks = [];
      let bytes = 0;
      const started = performance.now();
      captureNote = "";
      instance.ondataavailable = e => {
        if (!e.data.size || disposed || ticket !== generation) return;
        chunks.push(e.data);
        bytes += e.data.size;
        if (bytes >= maxBytes * 0.94) endRecording();
      };
      instance.onerror = () => {
        if (ticket === generation && !disposed) endRecording("中断");
      };
      instance.onstop = () => {
        if (ticket !== generation || disposed) return;
        const seconds = (performance.now() - started) / 1000;
        const mime = instance.mimeType || chunks[0]?.type || types[1];
        const extension = mime.includes("mp4") ? (video ? "mp4" : "m4a") : mime.includes("ogg") ? "ogg" : "webm";
        recorder = null;
        releaseDevices();
        phase = "idle";
        const file = new File(chunks, (trial ? "设备试录." : "本题回答.") + extension, { type: mime });
        if (file.size && file.size <= maxBytes) setClip(file, seconds, Boolean(captureNote));
        else if (file.size > maxBytes) status("本段录制已超出 100 MB，请缩短回答后重录。", true);
        else status("没有采集到有效片段，请确认麦克风输入和摄像头连接后重试。", true);
        render();
      };
      instance.start(500);
      recorder = instance;
      phase = "recording";
      status(trial ? "正在试录，请说一句话。5 秒后自动结束。" : "正在录制本题回答，答完后点击「结束录制」。");
      function tick() {
        const seconds = (performance.now() - started) / 1000;
        ui.elapsed.textContent = "录制中 · " + elapsedText(seconds) + (trial ? " / 00:05" : "");
        if (seconds >= (trial ? 5 : 1798)) endRecording();
      }
      tick();
      timer = setInterval(tick, 200);
    }

    async function openDevices(record) {
      if (disposed || uploading || checking || !["idle", "preview"].includes(phase)) return;
      if (record && !trial && !recordingReady()) {
        status("请先返回设备测试，完成试录和人声确认后再录制。也可填写文字或上传已有文件。", true);
        return;
      }
      const ticket = ++generation;
      try {
        if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
          throw new Error("当前页面无法开启录制，请使用本机 localhost 或 HTTPS 地址，用 Edge 或 Chrome 打开。");
        }
        ui["audio-play"].pause();
        ui["video-play"].pause();
        if (!stream) {
          phase = "opening";
          status("正在连接设备，请在浏览器提示中允许使用设备。");
          render();
          const mic = ui.audioinput.value;
          const camera = ui.videoinput.value;
          const wantsVideo = ui.mode.value === "video";
          const opened = await navigator.mediaDevices.getUserMedia({
            audio: mic ? { deviceId: { exact: mic } } : true,
            video: wantsVideo && { width: { ideal: 960 }, height: { ideal: 540 },
              ...(camera ? { deviceId: { exact: camera } } : {}) }
          });
          if (ticket !== generation || disposed) {
            opened.getTracks().forEach(track => track.stop());
            return;
          }
          stream = opened;
          const actualMicrophone = opened.getAudioTracks()[0];
          sourceLabel = actualMicrophone?.label || (actualMicrophone ? "浏览器未提供设备名称" : "未收到麦克风音轨，请切换麦克风");
          for (const track of opened.getTracks()) {
            track.addEventListener("ended", () => {
              if (stream !== opened || disposed) return;
              if (recorder) endRecording("中断");
              else {
                releaseDevices();
                phase = "idle";
                status("设备连接已中断，请重新连接后重试。", true);
                render();
              }
            }, { once: true });
          }
          if (opened.getVideoTracks().length) {
            ui.live.srcObject = opened;
            ui.live.play().catch(() => {});
          }
          startMeter();
          listInputs();
        }
        if (record) startRecording(ticket);
        else {
          phase = "preview";
          status("设备已连接，可检查预览或点击「试录 5 秒」。");
        }
      } catch (error) {
        if (ticket !== generation || disposed) return;
        releaseDevices();
        recorder = null;
        phase = "idle";
        status(errorHint(error) + (trial ? "也可以继续文字作答。" : "也可上传音视频文件或填写文字回答。"), true);
      }
      render();
    }

    function closePreview() {
      generation++;
      releaseDevices();
      phase = "idle";
      status("设备已关闭。");
      render();
    }
    if (ui.open) ui.open.onclick = () => openDevices(false);
    ui.start.onclick = () => openDevices(true);
    ui.stop.onclick = () => endRecording();
    ui.close.onclick = closePreview;
    for (const kind of ["mode", "audioinput", "videoinput"]) {
      ui[kind].onchange = () => {
        if (kind === "audioinput") {
          preferences.approval = null;
          sourceLabel = "";
          ui.source.textContent = "已选择：" + ui.audioinput.selectedOptions[0].textContent + "。重新连接后会显示实际音源。";
          if (trial) {
            preflight = {status:"not_tested",messages:[],transcript:""};
            ui.confirm.checked = false;
            renderCheck();
          }
          status("麦克风已更换，请重新完成试录和人声确认。");
        }
        preferences[kind] = ui[kind].value;
        if (phase === "preview") closePreview();
        render();
      };
    }
    if (trial) {
      for (const player of [ui["audio-play"], ui["video-play"]]) player.addEventListener("timeupdate", () => {
        if (player.currentTime >= .5) { listened = true; renderCheck(); }
      });
      ui.confirm.onchange = () => {
        const allowed = !ui.confirm.disabled && ui.confirm.checked;
        preferences.approval = allowed ? {audioinput:preferences.audioinput,status:preflight.status} : null;
        status(allowed ? "已完成试听确认，可以进入录制作答。" : "请回放并确认人声后进入录制作答。");
      };
    }
    const inputChanged = () => {
      preferences.approval = null;
      if (trial) {
        checkGeneration++;
        checking = false;
        preflight = {status:"not_tested",messages:[],transcript:""};
        ui.confirm.checked = false;
        renderCheck(); render();
      }
      status("输入设备已变化，请重新试录并确认人声。");
    };
    navigator.mediaDevices?.addEventListener?.("devicechange", inputChanged);
    if (ui.file) ui.file.onchange = () => {
      const file = ui.file.files[0];
      ui.file.value = "";
      if (!file || disposed || uploading || phase !== "idle") return;
      const ext = file.name.toLowerCase().match(/\.[^.]+$/)?.[0];
      if (!file.size || file.size > maxBytes || !formats.includes(ext)) {
        status("请选择受支持的非空音视频文件（最大 100 MB）。", true);
        return;
      }
      setClip(file, null);
      const selected = pending;
      const player = ui[playKind + "-play"];
      player.onloadedmetadata = () => {
        if (pending === selected && Number.isFinite(player.duration)) selected.seconds = player.duration;
      };
      render();
    };
    if (!trial && savedMedia?.url) showClip(savedMedia.url, savedMedia.mime || "", "已保存 · " + (savedMedia.name || "本题原片"));
    status(trial ? (recordingReady() ? "当前麦克风已完成试听确认，可继续面试或重新试录。" : "设备尚未开启，请先完成试录。") : savedMedia ? "本题已有保存的媒体，可回放或重新录制。" : "本题尚未录制。");
    listInputs();
    render();

    return {
      get pending() { return pending; },
      get busy() { return checking || uploading || ["opening", "recording", "stopping"].includes(phase); },
      get preflight() { return {...preflight, confirmed:recordingReady()}; },
      get recordingReady() { return recordingReady(); },
      setUploading(value) {
        if (disposed || uploading === value) return;
        uploading = value;
        if (value) {
          statusBeforeUpload = ui.status.textContent;
          status("正在保存本题作答…");
        } else status(statusBeforeUpload);
        render();
      },
      dispose() {
        if (disposed) return;
        disposed = true;
        checkGeneration++;
        navigator.mediaDevices?.removeEventListener?.("devicechange", inputChanged);
        generation++;
        if (recorder && recorder.state !== "inactive") recorder.stop();
        recorder = null;
        releaseDevices();
        resetPlayback();
        pending = null;
      }
    };
  }
  window.InterviewDevice = { mount };
})();
