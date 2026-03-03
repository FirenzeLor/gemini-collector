import React, { useMemo, useState, useRef, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import { Virtuoso, VirtuosoHandle } from "react-virtuoso";
import { convertFileSrc } from "@tauri-apps/api/core";
import { openUrl } from "@tauri-apps/plugin-opener";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight, vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";
import { Attachment, Conversation, ConvMessage } from "../data/types";
import { useTheme } from "../theme";

const loadedImageUrlCache = new Set<string>();

function getKind(mimeType: string): "image" | "video" | "file" {
  if (mimeType.startsWith("image/")) return "image";
  if (mimeType.startsWith("video/")) return "video";
  return "file";
}

function buildUrl(mediaId: string, mediaDir?: string, cacheKey?: string): string {
  if (!mediaDir || !mediaId) return "";
  const base = convertFileSrc(`${mediaDir}/${mediaId}`);
  if (!cacheKey) return base;
  return `${base}?v=${encodeURIComponent(cacheKey)}`;
}

function dedupeLikelyFormatVariants(attachments: Attachment[]): Attachment[] {
  // Gemini image_generation 在部分版本会返回同一图片的 png/jpeg 双格式；优先保留 png。
  if (attachments.length !== 2) return attachments;

  const imageAttachments = attachments.filter((a) => getKind(a.mimeType) === "image");
  if (imageAttachments.length !== 2) return attachments;

  const mimes = imageAttachments.map((a) => (a.mimeType || "").toLowerCase());
  const hasPng = mimes.includes("image/png");
  const hasJpeg = mimes.includes("image/jpeg") || mimes.includes("image/jpg");
  if (!hasPng || !hasJpeg) return attachments;

  const preferred = imageAttachments.find((a) => (a.mimeType || "").toLowerCase() === "image/png") ?? imageAttachments[0];
  return [preferred];
}

function hammingDistance(a: number[], b: number[]): number {
  if (a.length !== b.length) return Number.MAX_SAFE_INTEGER;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) diff += 1;
  }
  return diff;
}

async function computeImageDHash(url: string, size = 8): Promise<number[] | null> {
  return new Promise((resolve) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => {
      try {
        const canvas = document.createElement("canvas");
        canvas.width = size + 1;
        canvas.height = size;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          resolve(null);
          return;
        }
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const gray: number[] = [];
        for (let i = 0; i < data.length; i += 4) {
          gray.push((data[i] * 0.299) + (data[i + 1] * 0.587) + (data[i + 2] * 0.114));
        }
        const bits: number[] = [];
        for (let y = 0; y < size; y += 1) {
          const rowOffset = y * (size + 1);
          for (let x = 0; x < size; x += 1) {
            bits.push(gray[rowOffset + x] > gray[rowOffset + x + 1] ? 1 : 0);
          }
        }
        resolve(bits);
      } catch {
        resolve(null);
      }
    };
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

// Fix CommonMark bold/italic parsing failure when ** is adjacent to CJK/Unicode chars.
// Inserts zero-width space (U+200B) between non-ASCII characters and * markers.
function fixMarkdown(content: string): string {
  return content
    // Strip Gemini internal iemoji: markers, keep the code value
    .replace(/iemoji:([^:\s)]{1,20})/g, "$1")
    .replace(/([^\x00-\x7F])(\*+)/g, "$1\u200B$2")
    .replace(/(\*+)([^\x00-\x7F])/g, "$1\u200B$2");
}

// ─── Timeline constants ────────────────────────────────────────────────────
const TL_PAD = 18;       // top/bottom padding inside long canvas (px)
const TL_MIN_GAP = 18;   // minimum vertical gap between dots (px)
const TL_BAR_WIDTH = 25; // total bar width (px)
const TL_BAR_RIGHT = 8;  // bar distance from right edge of parent (px)
const TL_HIT = 20;       // dot hit-area size (px)
const TL_DOT = 9;        // normal dot visual diameter (px)
const TL_DOT_ACTIVE = 9; // active dot visual diameter (px)

// ─── Timeline utility functions ────────────────────────────────────────────

/** Three-pass min-gap enforcement (forward → backward → forward). */
function applyMinGap(
  positions: number[],
  minTop: number,
  maxTop: number,
  gap: number,
): number[] {
  const n = positions.length;
  if (n === 0) return positions;
  const out = positions.slice();

  out[0] = Math.max(minTop, Math.min(out[0], maxTop));
  for (let i = 1; i < n; i++) {
    out[i] = Math.max(positions[i], out[i - 1] + gap);
  }

  if (out[n - 1] > maxTop) {
    out[n - 1] = maxTop;
    for (let i = n - 2; i >= 0; i--) {
      out[i] = Math.min(out[i], out[i + 1] - gap);
    }
    if (out[0] < minTop) {
      out[0] = minTop;
      for (let i = 1; i < n; i++) {
        out[i] = Math.max(out[i], out[i - 1] + gap);
      }
    }
  }

  for (let i = 0; i < n; i++) {
    out[i] = Math.max(minTop, Math.min(maxTop, out[i]));
  }
  return out;
}

/** First index where arr[i] >= x. */
function lowerBound(arr: number[], x: number): number {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] < x) lo = mid + 1; else hi = mid;
  }
  return lo;
}

/** Last index where arr[i] <= x. */
function upperBound(arr: number[], x: number): number {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] <= x) lo = mid + 1; else hi = mid;
  }
  return lo - 1;
}

// ─── ConversationTimeline ──────────────────────────────────────────────────

interface TimelineProps {
  messages: ConvMessage[];
  scrollerEl: HTMLElement | null;
  visibleRange: { startIndex: number; endIndex: number };
  onJumpTo: (globalIndex: number) => void;
}

interface HoveredInfo {
  localIdx: number;
  /** screen-space Y of the dot center */
  screenY: number;
  /** screen-space X of the bar's left edge (tooltip anchor) */
  barLeft: number;
}

function ConversationTimeline({ messages, scrollerEl, visibleRange, onJumpTo }: TimelineProps) {
  const t = useTheme();
  const barRef = useRef<HTMLDivElement>(null);
  // Long-canvas inner div; moved via translateY (no scroll container = no scrollbar artifact).
  const innerRef = useRef<HTMLDivElement>(null);
  // Current timeline offset in px (mirrors innerRef transform).
  const offsetRef = useRef(0);
  const [barHeight, setBarHeight] = useState(0);
  const [dotRange, setDotRange] = useState({ start: 0, end: -1 });
  const [hovered, setHovered] = useState<{ info: HoveredInfo; visible: boolean } | null>(null);
  const tooltipTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── Refs holding latest geometry so event listeners stay stable ────────
  const yPositionsRef = useRef<number[]>([]);
  const barHeightRef = useRef(0);
  const contentHeightRef = useRef(0);

  // ── Collect user messages with global indices ──────────────────────────
  const userMsgs = useMemo(() => {
    const result: { globalIndex: number; text: string }[] = [];
    for (let i = 0; i < messages.length; i++) {
      if (messages[i].role === "user") {
        result.push({ globalIndex: i, text: messages[i].text });
      }
    }
    return result;
  }, [messages]);

  const N = userMsgs.length;

  // ── Long-canvas geometry ───────────────────────────────────────────────
  const { contentHeight, yPositions } = useMemo((): {
    contentHeight: number;
    yPositions: number[];
  } => {
    if (N === 0 || barHeight === 0) return { contentHeight: barHeight, yPositions: [] };
    const needed = 2 * TL_PAD + Math.max(0, N - 1) * TL_MIN_GAP;
    const ch = Math.max(barHeight, Math.ceil(needed));
    const usableC = Math.max(1, ch - 2 * TL_PAD);
    const desired = userMsgs.map((_, i) =>
      TL_PAD + (N <= 1 ? 0 : i / (N - 1)) * usableC,
    );
    const yPos = applyMinGap(desired, TL_PAD, TL_PAD + usableC, TL_MIN_GAP);
    return { contentHeight: ch, yPositions: yPos };
  }, [N, barHeight, userMsgs]);

  // Keep refs in sync with latest geometry values
  useEffect(() => { yPositionsRef.current = yPositions; }, [yPositions]);
  useEffect(() => { barHeightRef.current = barHeight; }, [barHeight]);
  useEffect(() => { contentHeightRef.current = contentHeight; }, [contentHeight]);

  // ── Active local index ─────────────────────────────────────────────────
  const activeLocalIdx = useMemo(() => {
    if (N === 0) return 0;
    const mid = Math.round((visibleRange.startIndex + visibleRange.endIndex) / 2);
    let idx = 0;
    for (let i = 0; i < N; i++) {
      if (userMsgs[i].globalIndex <= mid) idx = i;
      else break;
    }
    return idx;
  }, [visibleRange, userMsgs, N]);

  // ── Stable helper: recompute dotRange from a given scrollTop ──────────
  // Uses refs so this function never changes reference, keeping listeners stable.
  const updateDotRange = React.useCallback((scrollTop: number) => {
    const yPos = yPositionsRef.current;
    const bh = barHeightRef.current;
    if (yPos.length === 0 || bh === 0) return;
    const buffer = Math.max(100, bh);
    const s = lowerBound(yPos, scrollTop - buffer);
    const e = Math.max(s - 1, upperBound(yPos, scrollTop + bh + buffer));
    setDotRange(prev => prev.start === s && prev.end === e ? prev : { start: s, end: e });
  }, []);

  // ── Cleanup tooltip fade-out timer on unmount ─────────────────────────
  useEffect(() => () => { if (tooltipTimerRef.current) clearTimeout(tooltipTimerRef.current); }, []);

  // ── ResizeObserver for bar height ──────────────────────────────────────
  useEffect(() => {
    const el = barRef.current;
    if (!el) return;
    const h0 = el.clientHeight;
    if (h0 > 0) setBarHeight(h0);
    const ro = new ResizeObserver(([entry]) => {
      const h = entry.contentRect.height;
      if (h > 0) setBarHeight(h);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // ── Recompute visible dots when geometry changes ───────────────────────
  useEffect(() => {
    if (yPositions.length === 0 || barHeight === 0) return;
    updateDotRange(offsetRef.current);
  }, [yPositions, barHeight, updateDotRange]);

  // ── Wheel capture on bar: independent timeline scroll ─────────────────
  useEffect(() => {
    const bar = barRef.current;
    if (!bar) return;
    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const ch = contentHeightRef.current;
      const bh = barHeightRef.current;
      const maxOffset = Math.max(0, ch - bh);
      const newOffset = Math.max(0, Math.min(maxOffset, offsetRef.current + e.deltaY));
      if (Math.abs(newOffset - offsetRef.current) > 0.5) {
        offsetRef.current = newOffset;
        if (innerRef.current) {
          innerRef.current.style.transform = `translateY(-${newOffset}px)`;
        }
        updateDotRange(newOffset);
      }
    };
    bar.addEventListener("wheel", handleWheel, { passive: false });
    return () => bar.removeEventListener("wheel", handleWheel);
  }, [updateDotRange]);

  // ── Main scroller → translateY sync ───────────────────────────────────
  useEffect(() => {
    if (!scrollerEl) return;
    let rafId: number | null = null;
    const sync = () => {
      rafId = null;
      const inner = innerRef.current;
      if (!inner) return;
      const bh = barHeightRef.current;
      const ch = contentHeightRef.current;
      if (bh === 0 || yPositionsRef.current.length === 0) return;
      const { scrollTop, scrollHeight, clientHeight } = scrollerEl;
      const maxMain = Math.max(1, scrollHeight - clientHeight);
      const ratio = Math.max(0, Math.min(1, scrollTop / maxMain));
      const maxTimeline = Math.max(0, ch - bh);
      const target = Math.round(ratio * maxTimeline);
      if (Math.abs(offsetRef.current - target) > 1) {
        offsetRef.current = target;
        inner.style.transform = `translateY(-${target}px)`;
        updateDotRange(target);
      }
    };
    const onScroll = () => { if (rafId !== null) return; rafId = requestAnimationFrame(sync); };
    scrollerEl.addEventListener("scroll", onScroll, { passive: true });
    sync();
    return () => { scrollerEl.removeEventListener("scroll", onScroll); if (rafId !== null) cancelAnimationFrame(rafId); };
  }, [scrollerEl, updateDotRange]);

  // ── Inject CSS once: hide scrollbar + dot hover / focus styles ─────────
  useEffect(() => {
    const id = "conv-timeline-styles";
    if (document.getElementById(id)) return;
    const style = document.createElement("style");
    style.id = id;
    style.textContent = `
      .conv-tl-dot { outline: none; }
      .conv-tl-dot:hover .conv-tl-pip { transform: scale(1.55) !important; }
      .conv-tl-dot:focus-visible .conv-tl-pip {
        outline: 2px solid #0071e3; outline-offset: 3px;
      }
      @keyframes conv-tl-tooltip-in {
        from { opacity: 0; transform: translateY(-50%) translateX(6px); }
        to   { opacity: 1; transform: translateY(-50%) translateX(0); }
      }
      @keyframes conv-tl-tooltip-out {
        from { opacity: 1; transform: translateY(-50%) translateX(0); }
        to   { opacity: 0; transform: translateY(-50%) translateX(6px); }
      }
    `;
    document.head.appendChild(style);
  }, []);

  if (N === 0) return null;

  const dotColor = t.isDark ? "rgba(255,255,255,0.30)" : "rgba(0,0,0,0.22)";

  // Tooltip text: first ~150 chars of the hovered user message
  const tooltipText = hovered !== null
    ? (userMsgs[hovered.info.localIdx]?.text ?? "").trim().replace(/\s+/g, " ").slice(0, 150)
    : "";

  return (
    <>
      {/* ── Floating frosted-glass bar (absolute, overlaid) ── */}
      <div
        ref={barRef}
        style={{
          position: "absolute",
          // Hug the right edge; dots center at parentRight-18px,
          // which clears the 20px message padding zone (no text underneath).
          right: TL_BAR_RIGHT,
          top: 0,
          bottom: 0,
          width: TL_BAR_WIDTH,
          // Fully transparent — only the dots themselves are visible.
          background: "transparent",
          zIndex: 10,
          overflow: "hidden",
        }}
      >
        {/* ── Long-canvas inner div, moved via translateY (no scroll container) ── */}
        <div
          ref={innerRef}
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            width: "100%",
            height: contentHeight,
            willChange: "transform",
          }}
        >
          {userMsgs.slice(dotRange.start, dotRange.end + 1).map((msg, i) => {
              const localIdx = dotRange.start + i;
              const y = yPositions[localIdx];
              const isActive = localIdx === activeLocalIdx;
              const dotSize = isActive ? TL_DOT_ACTIVE : TL_DOT;

              return (
                <button
                  key={msg.globalIndex}
                  className="conv-tl-dot"
                  aria-label={`跳转到：${msg.text.slice(0, 40)}`}
                  onClick={() => onJumpTo(msg.globalIndex)}
                  onMouseEnter={(e) => {
                    if (tooltipTimerRef.current) clearTimeout(tooltipTimerRef.current);
                    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                    const barRect = barRef.current?.getBoundingClientRect();
                    setHovered({
                      info: {
                        localIdx,
                        screenY: rect.top + rect.height / 2,
                        barLeft: barRect?.left ?? rect.left,
                      },
                      visible: true,
                    });
                  }}
                  onMouseLeave={() => {
                    setHovered(prev => prev ? { ...prev, visible: false } : null);
                    tooltipTimerRef.current = setTimeout(() => setHovered(null), 180);
                  }}
                  style={{
                    position: "absolute",
                    top: y,
                    left: "50%",
                    transform: "translate(-50%, -50%)",
                    width: TL_HIT,
                    height: TL_HIT,
                    border: "none",
                    background: "transparent",
                    cursor: "pointer",
                    padding: 0,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    borderRadius: "50%",
                  }}
                >
                  <span
                    className="conv-tl-pip"
                    style={{
                      display: "block",
                      width: dotSize,
                      height: dotSize,
                      borderRadius: "50%",
                      background: isActive ? "#0071e3" : dotColor,
                      boxShadow: isActive
                        ? "0 0 0 2.5px #0071e340, 0 0 8px #0071e360"
                        : "none",
                      transition:
                        "width 0.15s ease, height 0.15s ease, background 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease",
                      flexShrink: 0,
                    }}
                  />
                </button>
              );
            })}
        </div>
      </div>

      {/* ── Tooltip card (position:fixed — not clipped by parent overflow) ── */}
      {hovered !== null && tooltipText && (
        <div
          style={{
            position: "fixed",
            // Fixed offset from viewport right: bar occupies [TL_BAR_RIGHT, TL_BAR_RIGHT+TL_BAR_WIDTH], +6px gap
            right: TL_BAR_RIGHT + TL_BAR_WIDTH + 6,
            // center on the dot's Y, clamped to stay on screen
            top: Math.max(8, Math.min(
              window.innerHeight - 120,
              hovered.info.screenY,
            )),
            // Symmetric fade in/out: same keyframe magnitude both directions
            animation: hovered.visible
              ? "conv-tl-tooltip-in 0.16s ease forwards"
              : "conv-tl-tooltip-out 0.16s ease forwards",
            zIndex: 1000,
            maxWidth: 240,
            pointerEvents: "none",
            background: t.isDark
              ? "rgba(20,23,30,0.92)"
              : "rgba(255,255,255,0.94)",
            backdropFilter: "blur(18px) saturate(120%)",
            WebkitBackdropFilter: "blur(18px) saturate(120%)",
            borderRadius: 10,
            border: t.isDark
              ? "1px solid rgba(255,255,255,0.13)"
              : "1px solid rgba(0,0,0,0.09)",
            padding: "9px 13px",
            fontSize: 12.5,
            lineHeight: 1.55,
            color: t.text,
            boxShadow: "0 6px 24px rgba(0,0,0,0.22)",
            wordBreak: "break-word",
            whiteSpace: "pre-wrap",
            overflow: "hidden",
          }}
        >
          {tooltipText}
        </div>
      )}
    </>
  );
}

function markdownCodeLanguage(className?: string): string {
  const matched = /language-([\w-]+)/.exec(className || "");
  return matched?.[1] || "text";
}

function MarkdownCodeBlock({
  code,
  language,
  isDark,
}: {
  code: string;
  language: string;
  isDark: boolean;
}) {
  const [copied, setCopied] = useState(false);

  function handleCopy() {
    void navigator.clipboard.writeText(code)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 900);
      })
      .catch((e) => {
        console.error("复制代码失败:", e);
      });
  }

  return (
    <div style={{ position: "relative", margin: "0.6em 0" }}>
      <button
        onClick={handleCopy}
        title={copied ? "已复制" : "复制代码"}
        style={{
          position: "absolute",
          top: 8,
          right: 8,
          zIndex: 2,
          border: "none",
          borderRadius: 6,
          padding: "2px 8px",
          fontSize: 11,
          background: copied
            ? "rgba(22,163,74,0.9)"
            : (isDark ? "rgba(15,23,42,0.7)" : "rgba(255,255,255,0.85)"),
          color: copied ? "#fff" : (isDark ? "#dbeafe" : "#334155"),
          borderColor: isDark ? "rgba(148,163,184,0.25)" : "rgba(148,163,184,0.45)",
          borderStyle: "solid",
          borderWidth: 1,
          cursor: "pointer",
        }}
      >
        {copied ? "已复制" : "复制"}
      </button>
      <SyntaxHighlighter
        language={language}
        style={isDark ? vscDarkPlus : oneLight}
        customStyle={{
          margin: 0,
          borderRadius: 8,
          padding: "12px 14px",
          fontSize: 12.5,
          lineHeight: 1.6,
          background: isDark ? "#121826" : "#f8fafc",
        }}
        codeTagProps={{
          style: {
            fontFamily: "\"SF Mono\", \"Fira Code\", monospace",
          },
        }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}

// Format ISO 8601 timestamp to "HH:MM"
function formatMsgTime(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  } catch {
    return iso;
  }
}

// Format ISO 8601 to "YYYY-MM-DD"
function formatMsgDate(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  } catch {
    return iso;
  }
}

interface ChatViewProps {
  conversation: Conversation | null;
  mediaDir?: string;  // path to accounts/{id}/media/
  mediaVersion?: number;
}

export function ChatView({ conversation, mediaDir, mediaVersion = 0 }: ChatViewProps) {
  const t = useTheme();
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const [scrollerEl, setScrollerEl] = useState<HTMLElement | null>(null);
  const [visibleRange, setVisibleRange] = useState({ startIndex: 0, endIndex: 0 });
  const parseWarning =
    conversation && typeof conversation.parseWarning === "string" && conversation.parseWarning.trim()
      ? conversation.parseWarning.trim()
      : "";

  if (!conversation) {
    return (
      <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: "transparent" }}>
        <div style={{ textAlign: "center", color: t.textMuted }}>
          <div style={{ fontSize: 44, marginBottom: 10 }}>💬</div>
          <div style={{ fontSize: 15, fontWeight: 600, color: t.text, marginBottom: 5 }}>选择一个对话</div>
          <div style={{ fontSize: 13 }}>从左侧列表中选择对话查看内容</div>
        </div>
      </div>
    );
  }

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", background: "transparent", overflow: "hidden" }}>
      {parseWarning && (
        <div
          style={{
            margin: "8px 14px 0",
            padding: "8px 10px",
            borderRadius: 8,
            fontSize: 12,
            color: t.isDark ? "#ffd28a" : "#9a5b00",
            background: t.isDark ? "rgba(255,173,51,0.14)" : "rgba(255,173,51,0.15)",
            border: t.isDark ? "1px solid rgba(255,173,51,0.35)" : "1px solid rgba(255,173,51,0.4)",
          }}
        >
          {parseWarning}
        </div>
      )}
      {(() => {
        const toRemove = new Set<number>();
        conversation.messages.forEach((msg, i) => {
          if (msg.text.includes("action_card_content")) {
            toRemove.add(i);
            for (let j = i - 1; j >= 0; j--) {
              if (conversation.messages[j].role === "user") { toRemove.add(j); break; }
              if (conversation.messages[j].role === "model") break;
            }
          }
        });
        const visibleMessages = conversation.messages.filter((_, i) => !toRemove.has(i));
        return visibleMessages.length === 0 ? (
          <div style={{ textAlign: "center", color: t.textMuted, fontSize: 13, marginTop: 60 }}>暂无消息记录</div>
        ) : (
          // position:relative so the absolutely-positioned timeline bar can anchor to it
          <div style={{ flex: 1, position: "relative", overflow: "hidden" }}>
            <Virtuoso
              ref={virtuosoRef}
              scrollerRef={(ref) => {
                if (ref instanceof HTMLElement) {
                  ref.setAttribute("data-tl-scroller", "");
                  setScrollerEl(ref);
                } else {
                  setScrollerEl(null);
                }
              }}
              rangeChanged={setVisibleRange}
              key={`${conversation.id}:${conversation.updatedAt}:${mediaVersion}`}
              data={visibleMessages}
              followOutput="smooth"
              initialTopMostItemIndex={visibleMessages.length - 1}
              itemContent={(_, msg) => (
                <MessageBubble
                  message={msg}
                  mediaDir={mediaDir}
                  cacheKey={`${conversation.id}:${conversation.updatedAt}:${mediaVersion}`}
                />
              )}
              style={{ position: "absolute", inset: 0 }}
            />
            <ConversationTimeline
              messages={visibleMessages}
              scrollerEl={scrollerEl}
              visibleRange={visibleRange}
              onJumpTo={(idx) =>
                virtuosoRef.current?.scrollToIndex({ index: idx, behavior: "smooth", align: "start" })
              }
            />
          </div>
        );
      })()}
    </div>
  );
}

function AttachmentStrip({
  attachments,
  mediaDir,
  cacheKey,
  alignRight,
}: {
  attachments: Attachment[];
  mediaDir?: string;
  cacheKey: string;
  alignRight: boolean;
}) {
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null);
  const failedAttachments = attachments.filter((a) => a.downloadFailed);
  const renderableAttachments = attachments.filter((a) => !a.downloadFailed);
  const mediaAttachmentsBase = useMemo(
    () => dedupeLikelyFormatVariants(renderableAttachments.filter((a) => getKind(a.mimeType) !== "file")),
    [renderableAttachments],
  );
  const mediaKey = useMemo(
    () => mediaAttachmentsBase.map((a) => `${a.mediaId}:${a.mimeType}`).join("|"),
    [mediaAttachmentsBase],
  );
  const [collapseTwinImages, setCollapseTwinImages] = useState(false);
  const mediaAttachments = collapseTwinImages ? [mediaAttachmentsBase[0]] : mediaAttachmentsBase;

  React.useEffect(() => {
    let cancelled = false;
    setCollapseTwinImages(false);

    const imageAttachments = mediaAttachmentsBase.filter((a) => getKind(a.mimeType) === "image");
    if (imageAttachments.length !== 2 || mediaAttachmentsBase.length !== 2) return () => { cancelled = true; };

    const leftUrl = buildUrl(imageAttachments[0].mediaId, mediaDir, cacheKey);
    const rightUrl = buildUrl(imageAttachments[1].mediaId, mediaDir, cacheKey);
    if (!leftUrl || !rightUrl) return () => { cancelled = true; };

    (async () => {
      const [leftHash, rightHash] = await Promise.all([
        computeImageDHash(leftUrl),
        computeImageDHash(rightUrl),
      ]);
      if (cancelled || !leftHash || !rightHash) return;
      if (hammingDistance(leftHash, rightHash) <= 2) {
        setCollapseTwinImages(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [mediaKey, mediaDir, cacheKey]);

  return (
    <>
      {failedAttachments.length > 0 && (
        <div
          style={{
            fontSize: 11,
            color: "#d97706",
            marginBottom: 6,
            opacity: 0.9,
            textAlign: alignRight ? "right" : "left",
          }}
        >
          {failedAttachments.length} 个附件下载失败，点击同步可重试
        </div>
      )}
      {/* Media thumbnails */}
      {mediaAttachments.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, justifyContent: alignRight ? "flex-end" : "flex-start", marginBottom: 6 }}>
          {mediaAttachments.map((att, i) => {
            const url = buildUrl(att.mediaId, mediaDir, cacheKey);
            const kind = getKind(att.mimeType);
            return kind === "image" ? (
              <ImageThumbnail
                key={i}
                url={url}
                alt={att.mediaId}
                onClick={() => setLightboxIdx(i)}
              />
            ) : (
              <div
                key={i}
                onClick={() => setLightboxIdx(i)}
                style={{ width: 160, height: 110, borderRadius: 14, overflow: "hidden", cursor: "pointer", flexShrink: 0, background: "#111", boxShadow: "0 2px 8px rgba(0,0,0,0.3)", position: "relative" }}
              >
                <VideoThumbnail
                  videoUrl={url}
                  previewUrl={att.previewMediaId ? buildUrl(att.previewMediaId, mediaDir, cacheKey) : ""}
                />
                <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.35)" }}>
                  <div style={{ width: 36, height: 36, borderRadius: "50%", border: "1.5px solid rgba(255,255,255,0.85)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                    <svg width="12" height="12" viewBox="0 0 16 16" fill="rgba(255,255,255,0.9)">
                      <polygon points="5,2 14,8 5,14" />
                    </svg>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}


      {/* Lightbox */}
      {lightboxIdx !== null && (
        <LightboxModal
          attachments={mediaAttachments}
          index={lightboxIdx}
          mediaDir={mediaDir}
          cacheKey={cacheKey}
          onClose={() => setLightboxIdx(null)}
          onChange={setLightboxIdx}
        />
      )}
    </>
  );
}

function ImageThumbnail({
  url,
  alt,
  onClick,
}: {
  url: string;
  alt: string;
  onClick: () => void;
}) {
  const t = useTheme();
  const imgRef = React.useRef<HTMLImageElement | null>(null);
  const [loading, setLoading] = useState(() => !!url && !loadedImageUrlCache.has(url));

  React.useEffect(() => {
    if (!url || loadedImageUrlCache.has(url)) {
      setLoading(false);
      return;
    }
    setLoading(true);
  }, [url]);

  React.useEffect(() => {
    const img = imgRef.current;
    if (!img || !url) return;
    if (img.complete && img.naturalWidth > 0) {
      loadedImageUrlCache.add(url);
      setLoading(false);
    }
  }, [url]);

  return (
    <div
      onClick={onClick}
      style={{
        width: 120,
        height: 120,
        borderRadius: 14,
        overflow: "hidden",
        cursor: "pointer",
        flexShrink: 0,
        background: t.isDark ? "#1a1a1c" : "#d9d9dc",
        boxShadow: "0 2px 8px rgba(0,0,0,0.25)",
        position: "relative",
      }}
    >
      <img
        ref={imgRef}
        src={url}
        alt={alt}
        onLoad={() => {
          if (url) loadedImageUrlCache.add(url);
          setLoading(false);
        }}
        onError={() => setLoading(false)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          display: "block",
          opacity: loading ? 0.62 : 1,
          transition: "opacity 0.22s ease-out",
        }}
        draggable={false}
      />
      {loading && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            pointerEvents: "none",
            background: t.isDark ? "rgba(0,0,0,0.15)" : "rgba(255,255,255,0.2)",
          }}
        >
          <div
            style={{
              position: "absolute",
              inset: 0,
              backgroundImage: t.isDark
                ? "repeating-linear-gradient(135deg, rgba(255,255,255,0.03) 0 16px, rgba(255,255,255,0.12) 16px 32px, rgba(255,255,255,0.03) 32px 48px)"
                : "repeating-linear-gradient(135deg, rgba(255,255,255,0.10) 0 16px, rgba(255,255,255,0.30) 16px 32px, rgba(255,255,255,0.10) 32px 48px)",
              backgroundSize: "260px 260px",
              animation: "mediaLoadingDiagonalSweep 3.2s linear infinite",
              opacity: t.isDark ? 0.55 : 0.5,
              willChange: "background-position",
            }}
          />
        </div>
      )}
    </div>
  );
}

function VideoThumbnail({ videoUrl, previewUrl }: { videoUrl: string; previewUrl: string }) {
  const [previewFailed, setPreviewFailed] = useState(false);
  const canUsePreview = !!previewUrl && !previewFailed;

  if (canUsePreview) {
    return (
      <img
        src={previewUrl}
        alt="video preview"
        onError={() => setPreviewFailed(true)}
        style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
        draggable={false}
      />
    );
  }

  return (
    <video
      src={videoUrl}
      style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
      muted
      preload="metadata"
    />
  );
}

function LightboxModal({
  attachments,
  index,
  mediaDir,
  cacheKey,
  onClose,
  onChange,
}: {
  attachments: Attachment[];
  index: number;
  mediaDir?: string;
  cacheKey: string;
  onClose: () => void;
  onChange: (i: number) => void;
}) {
  const att = attachments[index];
  const url = buildUrl(att.mediaId, mediaDir, cacheKey);
  const kind = getKind(att.mimeType);

  React.useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft" && index > 0) onChange(index - 1);
      if (e.key === "ArrowRight" && index < attachments.length - 1) onChange(index + 1);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [index, attachments.length]);

  return (
    <div
      onClick={onClose}
      style={{ position: "fixed", inset: 0, zIndex: 1000, background: "rgba(0,0,0,0.85)", display: "flex", alignItems: "center", justifyContent: "center" }}
    >
      <div onClick={(e) => e.stopPropagation()} style={{ position: "relative", maxWidth: "90vw", maxHeight: "90vh" }}>
        {kind === "image" ? (
          <img
            src={url}
            alt={att.mediaId}
            style={{ maxWidth: "90vw", maxHeight: "90vh", borderRadius: 12, objectFit: "contain", display: "block" }}
          />
        ) : (
          <video
            src={url}
            controls
            autoPlay
            style={{ maxWidth: "90vw", maxHeight: "90vh", borderRadius: 12, display: "block" }}
          />
        )}

        {/* Close button */}
        <button
          onClick={onClose}
          style={{ position: "absolute", top: -16, right: -16, width: 32, height: 32, borderRadius: "50%", background: "rgba(255,255,255,0.15)", border: "none", color: "#fff", fontSize: 18, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}
        >×</button>

        {index > 0 && (
          <button
            onClick={() => onChange(index - 1)}
            style={{ position: "absolute", left: -48, top: "50%", transform: "translateY(-50%)", width: 36, height: 36, borderRadius: "50%", background: "rgba(255,255,255,0.15)", border: "none", color: "#fff", fontSize: 20, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}
          >‹</button>
        )}
        {index < attachments.length - 1 && (
          <button
            onClick={() => onChange(index + 1)}
            style={{ position: "absolute", right: -48, top: "50%", transform: "translateY(-50%)", width: 36, height: 36, borderRadius: "50%", background: "rgba(255,255,255,0.15)", border: "none", color: "#fff", fontSize: 20, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center" }}
          >›</button>
        )}

        {attachments.length > 1 && (
          <div style={{ position: "absolute", bottom: -30, left: "50%", transform: "translateX(-50%)", color: "rgba(255,255,255,0.6)", fontSize: 13 }}>
            {index + 1} / {attachments.length}
          </div>
        )}
      </div>
    </div>
  );
}

function MessageBubble({
  message,
  mediaDir,
  cacheKey,
}: {
  message: ConvMessage;
  mediaDir?: string;
  cacheKey: string;
}) {
  const t = useTheme();
  const isUser = message.role === "user";
  const hasText = (message.text || "").trim().length > 0;
  const attachmentsBlock = message.attachments.length > 0 ? (
    <AttachmentStrip
      attachments={message.attachments}
      mediaDir={mediaDir}
      cacheKey={cacheKey}
      alignRight={isUser}
    />
  ) : null;

  return (
    <div style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start", padding: "4px 26px 4px 20px", gap: 8 }}>
      <div style={{ maxWidth: isUser ? "62%" : "94%" }}>
        {isUser && attachmentsBlock}
        {hasText && (
          <div style={{
            padding: isUser ? "10px 14px" : "12px 16px",
            borderRadius: isUser ? "18px 18px 6px 18px" : "18px 18px 18px 6px",
            background: isUser ? "linear-gradient(135deg, #0071e3 0%, #0077ed 100%)" : t.aiBubbleBg,
            color: isUser ? "#fff" : t.text,
            fontSize: 14,
            lineHeight: 1.55,
            boxShadow: isUser ? "0 2px 8px rgba(0,113,227,0.22)" : t.isDark ? "0 1px 3px rgba(0,0,0,0.3)" : "0 1px 3px rgba(0,0,0,0.07)",
            wordBreak: "break-word",
          }}>
            {isUser ? (
              <span style={{ whiteSpace: "pre-wrap" }}>{message.text}</span>
            ) : (
              <div className={`prose-ai${t.isDark ? " prose-dark" : ""}`}>
                <ReactMarkdown
                  remarkPlugins={[remarkGfm, remarkMath]}
                  rehypePlugins={[rehypeKatex]}
                  components={{
                    a: ({ href, children, ...props }) => (
                      <a
                        {...props}
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(e) => {
                          e.preventDefault();
                          if (!href) return;
                          void openUrl(href);
                        }}
                      >
                        {children}
                      </a>
                    ),
                    pre: ({ children }) => <>{children}</>,
                    code: ({ className, children, ...props }) => {
                      const content = String(children ?? "");
                      const isBlock =
                        (className || "").includes("language-") || content.includes("\n");
                      if (!isBlock) {
                        return (
                          <code className={className} {...props}>
                            {children}
                          </code>
                        );
                      }
                      return (
                        <MarkdownCodeBlock
                          code={content.replace(/\n$/, "")}
                          language={markdownCodeLanguage(className)}
                          isDark={t.isDark}
                        />
                      );
                    },
                  }}
                >
                  {fixMarkdown(message.text)}
                </ReactMarkdown>
              </div>
            )}
          </div>
        )}
        {!isUser && attachmentsBlock}
        <div style={{ fontSize: 11, color: t.textMuted, marginTop: hasText ? 3 : 1, textAlign: isUser ? "right" : "left", padding: "0 4px", display: "flex", gap: 4, justifyContent: isUser ? "flex-end" : "flex-start", alignItems: "center", flexWrap: "wrap" }}>
          <span>{formatMsgDate(message.timestamp)} {formatMsgTime(message.timestamp)}</span>
          {!isUser && (
            <>
              <span style={{ opacity: 0.4 }}>·</span>
              <span style={{ color: t.textSub }}>{message.model || "未知模型"}</span>
              {message.attachments.length > 0 && (() => {
                const atts = message.attachments;
                // 音乐文件：Gemini 对音乐同时输出一个 video/* (封面合并版) 和一个 audio/*，
                // 实际代表同一首音乐，计为 audio ×1。
                const hasVideo = atts.some((a) => a.mimeType.startsWith("video/"));
                const hasAudio = atts.some((a) => a.mimeType.startsWith("audio/"));
                const isMusicPair = atts.length === 2 && hasVideo && hasAudio;
                let displayCount: number;
                let mediaType: string;
                if (isMusicPair) {
                  mediaType = "audio";
                  displayCount = 1;
                } else {
                  const first = atts[0];
                  if (first.mimeType.startsWith("video/")) mediaType = "video";
                  else if (first.mimeType.startsWith("audio/")) mediaType = "audio";
                  else if (first.mimeType.startsWith("image/")) mediaType = "image";
                  else mediaType = "file";
                  displayCount = atts.length;
                }
                const countText = displayCount > 1 ? `${mediaType} ×${displayCount}` : mediaType;
                // 累加附件体积（来自 Rust 注入的 size 字段，单位 bytes）
                const totalBytes = atts.reduce((sum, a) => sum + (a.size ?? 0), 0);
                const sizeText = totalBytes > 0
                  ? ` · ${(totalBytes / 1048576).toFixed(1)} MB`
                  : "";
                return (
                  <span style={{
                    fontSize: 10,
                    fontWeight: 500,
                    color: t.textMuted,
                    background: t.hover,
                    borderRadius: 4,
                    padding: "1px 5px",
                    marginLeft: 5,
                    letterSpacing: 0.2,
                  }}>
                    {countText}{sizeText}
                  </span>
                );
              })()}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
