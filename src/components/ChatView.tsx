import React, { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Virtuoso } from "react-virtuoso";
import { convertFileSrc } from "@tauri-apps/api/core";
import { Attachment, Conversation, ConvMessage } from "../data/mockData";
import { useTheme } from "../theme";

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

// Format ISO 8601 timestamp to "HH:MM"
function formatMsgTime(iso: string): string {
  try {
    return new Date(iso).toTimeString().slice(0, 5);
  } catch {
    return iso;
  }
}

// Format ISO 8601 to "YYYY-MM-DD"
function formatMsgDate(iso: string): string {
  try {
    return iso.slice(0, 10);
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
  const parseWarning =
    conversation && typeof conversation.parseWarning === "string" && conversation.parseWarning.trim()
      ? conversation.parseWarning.trim()
      : "";

  if (!conversation) {
    return (
      <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", background: t.appBg }}>
        <div style={{ textAlign: "center", color: t.textMuted }}>
          <div style={{ fontSize: 44, marginBottom: 10 }}>💬</div>
          <div style={{ fontSize: 15, fontWeight: 600, color: t.text, marginBottom: 5 }}>选择一个对话</div>
          <div style={{ fontSize: 13 }}>从左侧列表中选择对话查看内容</div>
        </div>
      </div>
    );
  }

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", background: t.appBg, overflow: "hidden" }}>
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
      {conversation.messages.length === 0 ? (
        <div style={{ textAlign: "center", color: t.textMuted, fontSize: 13, marginTop: 60 }}>暂无消息记录</div>
      ) : (
        <Virtuoso
          key={`${conversation.id}:${conversation.syncedAt}:${mediaVersion}`}
          data={conversation.messages}
          followOutput="smooth"
          initialTopMostItemIndex={conversation.messages.length - 1}
          itemContent={(_, msg) => (
            <MessageBubble
              message={msg}
              mediaDir={mediaDir}
              cacheKey={`${conversation.id}:${conversation.syncedAt}:${mediaVersion}`}
            />
          )}
          style={{ flex: 1 }}
        />
      )}
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
  const fileAttachments = renderableAttachments.filter((a) => getKind(a.mimeType) === "file");

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
              <div
                key={i}
                onClick={() => setLightboxIdx(i)}
                style={{ width: 120, height: 120, borderRadius: 14, overflow: "hidden", cursor: "pointer", flexShrink: 0, background: "#222", boxShadow: "0 2px 8px rgba(0,0,0,0.25)" }}
              >
                <img
                  src={url}
                  alt={att.mediaId}
                  style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
                  draggable={false}
                />
              </div>
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

      {/* File attachments */}
      {fileAttachments.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, justifyContent: alignRight ? "flex-end" : "flex-start", marginBottom: 6 }}>
          {fileAttachments.map((att, i) => (
            <div
              key={i}
              style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 10px", borderRadius: 10, background: "rgba(0,0,0,0.1)", maxWidth: 200 }}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
              </svg>
              <span style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{att.mediaId}</span>
            </div>
          ))}
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

  return (
    <div style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start", padding: "4px 20px", gap: 8 }}>
      <div style={{ maxWidth: isUser ? "62%" : "94%" }}>
        {message.attachments.length > 0 && (
          <AttachmentStrip
            attachments={message.attachments}
            mediaDir={mediaDir}
            cacheKey={cacheKey}
            alignRight={isUser}
          />
        )}
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
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{fixMarkdown(message.text)}</ReactMarkdown>
              </div>
            )}
          </div>
        )}
        <div style={{ fontSize: 11, color: t.textMuted, marginTop: hasText ? 3 : 1, textAlign: isUser ? "right" : "left", padding: "0 4px", display: "flex", gap: 4, justifyContent: isUser ? "flex-end" : "flex-start", alignItems: "center", flexWrap: "wrap" }}>
          <span>{formatMsgDate(message.timestamp)} {formatMsgTime(message.timestamp)}</span>
          {!isUser && (
            <>
              <span style={{ opacity: 0.4 }}>·</span>
              <span style={{ color: t.textSub }}>{message.model || "未知模型"}</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
