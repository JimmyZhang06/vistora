import {initI18n} from "./shared/i18n.js";
import {initTheme} from "./shared/theme.js";
initTheme(document);

const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const locale = initI18n(document);

const menu = document.querySelector("[data-menu]");
const menuTrigger = document.querySelector("[data-menu-trigger]");

function setMenu(open) {
  menu?.classList.toggle("is-open", open);
  menu?.setAttribute("aria-hidden", String(!open));
  menuTrigger?.setAttribute("aria-expanded", String(open));
  document.body.classList.toggle("is-locked", open);
}

menuTrigger?.addEventListener("click", () => {
  setMenu(menuTrigger.getAttribute("aria-expanded") !== "true");
});

menu?.addEventListener("click", (event) => {
  if (event.target.closest("a")) setMenu(false);
});

for (const video of document.querySelectorAll("video[data-src]")) {
  video.src = video.dataset.src;
  video.load();
  video.play().catch(() => {});
}

// 作品墙双行流动：克隆整组卡片直到轨道盖满两个视口宽，再按「一组内容宽度」平移循环，
// 循环点前后 DOM 完全一致所以无缝；速度按宽度换算保证两行视觉速率一致。
for (const track of document.querySelectorAll("[data-marquee-track]")) {
  const originals = [...track.children];
  if (!originals.length) continue;
  const gap = Number.parseFloat(getComputedStyle(track).columnGap) || 0;
  const measureSet = () => originals.reduce((total, node) => total + node.getBoundingClientRect().width + gap, 0);
  const setWidth = measureSet();
  if (!setWidth) continue;
  const copies = Math.max(2, Math.ceil((window.innerWidth * 2) / setWidth));
  for (let copy = 1; copy < copies; copy += 1) {
    for (const node of originals) track.appendChild(node.cloneNode(true));
  }
  const applyMetrics = () => {
    const width = measureSet();
    track.style.setProperty("--marquee-shift", `${width}px`);
    track.style.setProperty("--marquee-duration", `${Math.max(20, Math.round(width / 32))}s`);
  };
  applyMetrics();
  window.addEventListener("resize", applyMetrics, {passive: true});
  if (!prefersReducedMotion) track.classList.add("is-flowing");
}

function updateScrollProgress() {
  const progress = document.querySelector("[data-scroll-progress]");
  if (!progress) return;
  const maximum = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
  const ratio = Math.min(1, window.scrollY / maximum);
  progress.style.transform = `translateX(${ratio * 238}px)`;
}

window.addEventListener("scroll", updateScrollProgress, {passive: true});
updateScrollProgress();

if (!prefersReducedMotion && window.gsap && window.ScrollTrigger) {
  window.gsap.registerPlugin(window.ScrollTrigger);

  window.gsap.from("[data-hero-title] > span", {
    yPercent: 105,
    opacity: 0,
    duration: 1.05,
    stagger: 0.08,
    ease: "power4.out",
  });

  // 英雄区收敛动画（Ponder 原版交互）：钉住英雄区约一屏滚程，
  // 五张散落的视频卡随滚动收敛成一行，落在底部时间轴进度条上方；
  // 卡片间空隙同步收窄，标题淡出让位。进度线本身随页面滚动右移（updateScrollProgress）。
  const hero = document.querySelector("[data-hero]");
  const orbitCards = [1, 2, 3, 4, 5]
    .map((n) => document.querySelector(`[data-orbit-card='${n}']`))
    .filter(Boolean);

  if (hero && orbitCards.length === 5) {
    // 先把 CSS 里的百分比/right 定位固化成相对英雄区的像素 left/top，动画才有稳定起点
    const heroRect = hero.getBoundingClientRect();
    for (const card of orbitCards) {
      const rect = card.getBoundingClientRect();
      window.gsap.set(card, {
        left: rect.left - heroRect.left,
        top: rect.top - heroRect.top,
        right: "auto",
        width: rect.width,
        height: rect.height,
        opacity: 1,
      });
    }

    // 目标行：五张等宽卡，间距 1.6vw，水平居中；行底贴在时间轴（45px）上方
    const CARD_VW = 15.2;
    const GAP_VW = 1.6;
    const ROW_VW = 5 * CARD_VW + 4 * GAP_VW;
    const X0_VW = (100 - ROW_VW) / 2;
    const cardWidth = () => (window.innerWidth * CARD_VW) / 100;
    const cardHeight = () => cardWidth() / 1.72;      // 收敛后统一 1.72 宽高比（原核心卡口径）

    const converge = window.gsap.timeline({
      scrollTrigger: {
        trigger: hero,
        start: "top top",
        end: "+=115%",
        scrub: 0.75,
        pin: true,
        anticipatePin: 1,
        invalidateOnRefresh: true,
      },
    });

    orbitCards.forEach((card, index) => {
      converge.to(card, {
        left: () => (window.innerWidth * (X0_VW + index * (CARD_VW + GAP_VW))) / 100,
        // ⚠️行的落点按「视口」算，不按英雄区算：英雄区 112svh，钉住时只露出前 100vh，
        // 用 offsetHeight 会把整行压到折叠线以下（首版实测踩到）
        top: () => window.innerHeight - 45 - cardHeight() - 26,
        width: cardWidth,
        height: cardHeight,
        ease: "power1.inOut",
      }, 0);
    });
    // 时间轴进度条本来贴在英雄区底（折叠线下 12svh），随收敛同步上移进画面
    converge.to(".hero__timeline", {
      y: () => -(hero.offsetHeight - window.innerHeight),
      ease: "power1.inOut",
    }, 0);
    // 收敛过半后标题与「向下探索」让位
    converge.to(".hero__copy", {opacity: 0, y: -46, ease: "power1.in"}, 0.25);
    converge.to(".hero__scroll", {opacity: 0, ease: "power1.in"}, 0.25);
  }

  for (const section of document.querySelectorAll("[data-reveal]")) {
    window.gsap.from(section.querySelectorAll("h2, p, .media-frame"), {
      y: 42,
      opacity: 0,
      stagger: 0.08,
      duration: 0.9,
      ease: "power3.out",
      scrollTrigger: {trigger: section, start: "top 76%"},
    });
  }

  const processItems = [...document.querySelectorAll(".process-list li")];
  const processVisual = document.querySelector("[data-process-visual]");
  const processStage = document.querySelector("[data-process-stage]");
  const processCounter = document.querySelector("[data-process-counter]");
  const processOutput = document.querySelector("[data-process-output]");
  const processCards = [...document.querySelectorAll("[data-process-card]")];
  // 逐步产出角标：全部为秦始皇 v5 真实口径（27 句 / 03:19 / SYNC Δ39ms，出处 _timeline.json 与出片记录）
  const processOutputs = ["切片 / 27 句", "配音 · 云健 / 03:19", "素材匹配 / 27 句", "ASS 字幕 / 27 句", "SYNC Δ39MS / 过闸", "FINAL CUT / READY"];

  function activateProcess(item) {
    const index = processItems.indexOf(item);
    if (index < 0 || item.classList.contains("is-active")) return;
    for (const candidate of processItems) candidate.classList.toggle("is-active", candidate === item);
    for (const card of processCards) card.classList.add("is-changing");
    window.setTimeout(() => {
      processVisual.dataset.step = String(index + 1);
      processStage.textContent = item.querySelector("strong").textContent;
      processCounter.textContent = `${String(index + 1).padStart(2, "0")} / 06`;
      processOutput.textContent = processOutputs[index];
      for (const card of processCards) card.classList.remove("is-changing");
    }, prefersReducedMotion ? 0 : 180);
  }

  processItems.forEach((item) => {
    window.ScrollTrigger.create({
      trigger: item,
      start: "top 58%",
      end: "bottom 42%",
      onEnter: () => activateProcess(item),
      onEnterBack: () => activateProcess(item),
    });
  });
}

if (!window.ScrollTrigger) {
  const processItems = [...document.querySelectorAll(".process-list li")];
  const processVisual = document.querySelector("[data-process-visual]");
  const processStage = document.querySelector("[data-process-stage]");
  const processCounter = document.querySelector("[data-process-counter]");
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.find((entry) => entry.isIntersecting);
    if (!visible) return;
    const item = visible.target;
    const index = processItems.indexOf(item);
    for (const candidate of processItems) candidate.classList.toggle("is-active", candidate === item);
    processVisual.dataset.step = String(index + 1);
    processStage.textContent = item.querySelector("strong").textContent;
    processCounter.textContent = `${String(index + 1).padStart(2, "0")} / 06`;
  }, {rootMargin: "-42% 0px -42% 0px"});
  processItems.forEach((item) => observer.observe(item));
}

document.documentElement.dataset.locale = locale;
