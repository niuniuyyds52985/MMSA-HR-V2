// Finish the library film, then keep the welcome page open until the candidate
// enters. The introduction stays available throughout the interview.
(() => {
  const dialog = document.getElementById("opening");
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const query = new URLSearchParams(location.search);
  const candidateEntry = /^\/v2\/c\/[^/]+\/?$/.test(location.pathname) || !!query.get("candidate");
  if (!candidateEntry || !dialog?.showModal) return;

  const video = dialog.querySelector("video");
  const skip = dialog.querySelector("button");
  const campaign = document.createElement("section");
  campaign.className = "opening-campaign";
  campaign.hidden = true;
  campaign.setAttribute("aria-labelledby", "opening-campaign-title");
  campaign.innerHTML = `
    <div class="campaign-grid" aria-hidden="true"></div>
    <div class="campaign-orbit campaign-orbit-one" aria-hidden="true"></div>
    <div class="campaign-orbit campaign-orbit-two" aria-hidden="true"></div>
    <div class="campaign-layout">
      <div class="campaign-copy">
        <p class="campaign-partners">同济经管 <span>×</span> 千问办公</p>
        <p class="campaign-edition"><span></span> 第一届 AI 管理实践挑战赛</p>
        <h2 id="opening-campaign-title">用 AI<br>重塑<span>管理。</span></h2>
        <p class="campaign-lead">把真实的管理问题，变成能运行的作品。</p>
        <p class="campaign-track">人力资源与组织管理 · 参赛作品</p>
        <div class="campaign-project"><span class="campaign-project-mark" aria-hidden="true">实</span>
          <div><strong>真才实鉴</strong><span>管培生模拟面试</span></div>
        </div>
        <div class="campaign-actions"><button type="button" class="campaign-enter">进入面试 <span aria-hidden="true">↗</span></button>
          <span class="campaign-next">准备好后，点击进入面试</span></div>
      </div>
      <figure class="campaign-poster">
        <div class="welcome-sheet">
          <div class="welcome-topline"><span class="welcome-brand"><i aria-hidden="true"></i>真才实鉴</span><span class="welcome-status"><i aria-hidden="true"></i>即将开始</span></div>
          <p class="welcome-eyebrow">每一段经历，都值得被认真看见</p>
          <h3>你好，<br><span>未来的管理者。</span></h3>
          <svg class="welcome-art" viewBox="0 0 420 250" role="img" aria-label="校园建筑与对话气泡组成的面试欢迎插画">
            <defs><linearGradient id="welcome-sky" x1="0" x2="1" y1="0" y2="1"><stop stop-color="#d8ebd9"/><stop offset="1" stop-color="#edf1dc"/></linearGradient></defs>
            <circle cx="244" cy="122" r="103" fill="url(#welcome-sky)"/>
            <circle cx="326" cy="46" r="24" fill="#edcb76"/>
            <path d="M26 202H396M44 215H378" stroke="#b6c9b6" stroke-width="1.5" stroke-linecap="round"/>
            <g fill="none" stroke="#557e68" stroke-width="1.5" stroke-linejoin="round">
              <path d="M113 190V98L219 63L325 98V190" fill="#f7f4e5"/>
              <path d="M100 99L219 58L338 99M109 107H329M109 179H329"/>
              <path d="M202 190V143A17 17 0 0 1 34 0V190" fill="#b2d1b7"/>
              <path d="M219 128V190M124 190V211H315V190M139 199H300M153 207H286"/>
              <path d="M134 122H158V158H134ZM174 122H190V158H174ZM248 122H264V158H248ZM280 122H304V158H280Z" fill="#dce8d9"/>
              <path d="M146 122V158M134 140H158M292 122V158M280 140H304"/>
              <circle cx="219" cy="94" r="11" fill="#f7f4e5"/><path d="M219 87V94L224 97"/>
            </g>
            <path d="M69 198V163M59 179Q43 152 62 136Q79 139 84 159Q87 175 69 184" fill="#8eb99a" stroke="#557e68" stroke-width="1.5"/>
            <path d="M354 199V167M354 184Q335 171 343 152Q361 136 371 157Q377 176 354 184" fill="#bad5b2" stroke="#557e68" stroke-width="1.5"/>
            <g transform="translate(27 33)">
              <path d="M0 14Q0 0 14 0H119Q133 0 133 14V53Q133 67 119 67H38L22 80V67H14Q0 67 0 53Z" fill="#194d3c"/>
              <path d="M19 23H101M19 37H80M19 51H61" stroke="#bfe5ca" stroke-width="4" stroke-linecap="round"/>
              <circle cx="110" cy="47" r="11" fill="#6ed8a4"/><path d="M105 47L109 51L116 43" stroke="#194d3c" stroke-width="2" fill="none" stroke-linecap="round"/>
            </g>
            <g transform="translate(286 154) rotate(-7)">
              <rect width="100" height="61" rx="14" fill="#fffdf3" stroke="#a9c3ae"/>
              <circle cx="25" cy="26" r="10" fill="#f0cd79"/><path d="M18 41Q25 34 32 41" stroke="#a58b50" stroke-width="2" fill="none"/>
              <path d="M44 23H79M44 34H68" stroke="#88b39b" stroke-width="4" stroke-linecap="round"/>
            </g>
            <path d="M179 25V35M174 30H184M372 104V114M367 109H377" stroke="#8bb39a" stroke-width="2" stroke-linecap="round"/>
          </svg>
          <ol class="welcome-steps" aria-label="面试流程"><li><span>01</span>自我介绍</li><li><span>02</span>情境选择</li><li><span>03</span>面试问答</li></ol>
          <p class="welcome-note">从一段轻松的自我介绍开始，<br>聊聊你的经历、选择与想法。</p>
          <div class="welcome-footer"><span>同济经管 × 千问办公</span><span>AI 管理实践挑战赛 · 参赛作品</span></div>
        </div>
        <figcaption><span>真才实鉴 · 管培生模拟面试</span><a href="https://www.thepaper.cn/newsDetail_forward_34298638" target="_blank" rel="noopener noreferrer">活动原文 ↗</a></figcaption>
      </figure>
    </div>`;
  dialog.append(campaign);

  const entry = document.createElement("nav");
  entry.className = "opening-entry";
  entry.setAttribute("aria-label", "开场与面试介绍");
  entry.innerHTML = '<button type="button" class="opening-brochure">面试介绍</button><button type="button" class="opening-replay">重播开场</button>';
  const mast = document.querySelector(".mast");
  if (mast) mast.after(entry);
  else document.querySelector("main")?.prepend(entry);

  let stage = "closed", deadline = null, reveal = null, previousFocus = null;
  function dismiss() {
    clearTimeout(deadline); clearTimeout(reveal);
    stage = "closed";
    video.pause();
    dialog.close();
    dialog.classList.remove("revealing", "show-campaign");
    previousFocus?.focus?.({ preventScroll: true });
  }
  function revealWorkspace() {
    if (stage === "closed" || stage === "revealing") return;
    stage = "revealing";
    if (motion.matches) return dismiss();
    dialog.classList.add("revealing");
    reveal = setTimeout(dismiss, 420);
  }
  function showCampaign() {
    if (stage !== "film" && stage !== "opening") return;
    stage = "campaign";
    clearTimeout(deadline);
    video.pause();
    campaign.hidden = false;
    dialog.classList.add("show-campaign");
    dialog.setAttribute("aria-label", "真才实鉴面试欢迎页");
    skip.textContent = "关闭介绍";
    campaign.querySelector(".campaign-enter").focus({ preventScroll: true });
  }
  function startOpening(film = true) {
    clearTimeout(deadline); clearTimeout(reveal);
    previousFocus = document.activeElement;
    dialog.classList.remove("revealing", "show-campaign");
    campaign.scrollTop = 0;
    campaign.hidden = true;
    stage = "opening";
    if (!dialog.open) dialog.showModal();
    if (!film || motion.matches) return showCampaign();
    stage = "film";
    skip.textContent = "跳过动画";
    dialog.setAttribute("aria-label", "真才实鉴开场动画");
    if (!video.src) video.src = "/v2/ui/interview-opening.mp4?v=d479613a";
    else video.currentTime = 0;
    // Decoder failure must not trap the candidate. Successful playback retains
    // the full final library shot and reaches the welcome page on ended.
    deadline = setTimeout(showCampaign, 2500);
    video.play().catch(showCampaign);
  }

  video.addEventListener("playing", () => {
    if (stage !== "film") return;
    clearTimeout(deadline);
    const seconds = Number.isFinite(video.duration) ? video.duration : 8;
    deadline = setTimeout(showCampaign, Math.min(seconds + 1, 30) * 1000);
  });
  skip.addEventListener("click", () => stage === "film" ? showCampaign() : revealWorkspace());
  campaign.querySelector(".campaign-enter").addEventListener("click", revealWorkspace);
  entry.querySelector(".opening-brochure").addEventListener("click", () => startOpening(false));
  entry.querySelector(".opening-replay").addEventListener("click", () => startOpening(true));
  dialog.addEventListener("cancel", event => { event.preventDefault(); dismiss(); });
  motion.addEventListener("change", () => { if (stage === "film" && motion.matches) showCampaign(); });
  window.addEventListener("pagehide", dismiss);
  video.addEventListener("error", showCampaign);
  video.addEventListener("ended", showCampaign);
  const navigation = performance.getEntriesByType("navigation")[0];
  // Keep legacy brochure links working; refresh/back opens the welcome page.
  startOpening(!["brochure", "welcome"].includes(query.get("intro")) && navigation?.type !== "back_forward" && navigation?.type !== "reload");
})();
