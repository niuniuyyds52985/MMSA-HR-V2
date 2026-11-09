// Connect V1's audited module viewer to each recording in the portable V2 report.
(() => {
  "use strict";
  for (const question of document.querySelectorAll("#report-questions > details")) {
    const player = question.querySelector("video, audio");
    if (!player) continue;
    async function seek(milliseconds, options = {}) {
      if (player.readyState < HTMLMediaElement.HAVE_METADATA) {
        await new Promise((resolve, reject) => {
          const controller = new AbortController();
          player.addEventListener("loadedmetadata", () => { controller.abort(); resolve(); }, {signal:controller.signal});
          player.addEventListener("error", () => { controller.abort(); reject(new Error("原片无法读取，请检查随报告导出的 assets 目录。")); }, {signal:controller.signal});
          player.preload = "metadata";
          player.load();
        });
      }
      player.currentTime = Math.max(0, milliseconds / 1000);
      if (options.play === false) player.pause();
    }
    const error = document.createElement("p");
    error.setAttribute("role", "status");
    player.closest(".evidence-timeline-player").after(error);
    for (const button of question.querySelectorAll("[data-seek-seconds]")) {
      button.addEventListener("click", () => seek(Number(button.dataset.seekSeconds) * 1000).catch(e => error.textContent=e.message));
    }
    const source = question.querySelector("script.v1-data");
    if (!source || !window.SuitMultimodalView) continue;
    const data = JSON.parse(source.textContent);
    let viewer;
    const render = () => {
      if (viewer || !question.open || !source.isConnected) return;
      question.removeEventListener("toggle", render);
      const overlay = question.querySelector(".evidence-player-overlay");
      let notify = () => {};
      const refreshTime = () => notify(player.currentTime * 1000, player);
      const timeline = {
        seek, refreshTime,
        attachOverlay(svg) { overlay.append(svg); return () => svg.remove(); },
        subscribeTime(callback) {
          notify = callback;
          const events = ["loadedmetadata", "seeked", "timeupdate"];
          for (const event of events) player.addEventListener(event, refreshTime);
          refreshTime();
          return () => { for (const event of events) player.removeEventListener(event, refreshTime); };
        }
      };
      viewer = window.SuitMultimodalView.mount({data, timeline, sourceLabel:"本题原片 · 本机算法观测"});
      question.querySelector(".v1-evidence").append(viewer.element);
      const geometry = data.modules.gaze?.geometry || data.modules.au?.geometry;
      if (player.tagName === "VIDEO" && geometry?.observations?.some(frame => frame.quality === "valid" && frame.landmarks?.length === 68)) {
        viewer.setChannel("geometry");
      }
    };
    question.addEventListener("toggle", render);
    for (const link of document.querySelectorAll('#multimodal-evidence a[data-media-channel]')) {
      if (link.hash !== "#" + question.id) continue;
      link.addEventListener("click", async event => {
        event.preventDefault();
        question.open = true;
        render();
        viewer.setChannel(link.dataset.mediaChannel);
        player.scrollIntoView({block:"center"});
        try {
          await seek(Number(link.dataset.atMs), {play:false});
        } catch (e) { error.textContent=e.message; }
      });
    }
    render();
  }
})();
