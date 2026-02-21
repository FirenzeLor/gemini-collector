import React, { useState } from "react";
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

function buildUrl(mediaId: string, mediaDir?: string): string {
  if (!mediaDir) return "";
  return convertFileSrc(`${mediaDir}/${mediaId}`);
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
}

export function ChatView({ conversation, mediaDir }: ChatViewProps) {
  const t = useTheme();

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
      {conversation.messages.length === 0 ? (
        <div style={{ textAlign: "center", color: t.textMuted, fontSize: 13, marginTop: 60 }}>暂无消息记录</div>
      ) : (
        <Virtuoso
          key={conversation.id}
          data={conversation.messages}
          followOutput="smooth"
          initialTopMostItemIndex={conversation.messages.length - 1}
          itemContent={(_, msg) => <MessageBubble message={msg} mediaDir={mediaDir} />}
          style={{ flex: 1 }}
        />
      )}
    </div>
  );
}

function AttachmentStrip({ attachments, mediaDir }: { attachments: Attachment[]; mediaDir?: string }) {
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null);
  const mediaAttachments = attachments.filter((a) => getKind(a.mimeType) !== "file");
  const fileAttachments = attachments.filter((a) => getKind(a.mimeType) === "file");

  return (
    <>
      {/* Media thumbnails */}
      {mediaAttachments.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, justifyContent: "flex-end", marginBottom: 6 }}>
          {mediaAttachments.map((att, i) => {
            const url = buildUrl(att.mediaId, mediaDir);
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
                <video
                  src={url}
                  style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
                  muted
                  preload="metadata"
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
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, justifyContent: "flex-end", marginBottom: 6 }}>
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
          onClose={() => setLightboxIdx(null)}
          onChange={setLightboxIdx}
        />
      )}
    </>
  );
}

function LightboxModal({
  attachments,
  index,
  mediaDir,
  onClose,
  onChange,
}: {
  attachments: Attachment[];
  index: number;
  mediaDir?: string;
  onClose: () => void;
  onChange: (i: number) => void;
}) {
  const att = attachments[index];
  const url = buildUrl(att.mediaId, mediaDir);
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

function MessageBubble({ message, mediaDir }: { message: ConvMessage; mediaDir?: string }) {
  const t = useTheme();
  const isUser = message.role === "user";

  return (
    <div style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start", padding: "4px 20px", gap: 8 }}>
      <div style={{ maxWidth: isUser ? "62%" : "72%" }}>
        {isUser && message.attachments.length > 0 && (
          <AttachmentStrip attachments={message.attachments} mediaDir={mediaDir} />
        )}
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
        <div style={{ fontSize: 11, color: t.textMuted, marginTop: 3, textAlign: isUser ? "right" : "left", padding: "0 4px", display: "flex", gap: 4, justifyContent: isUser ? "flex-end" : "flex-start", alignItems: "center", flexWrap: "wrap" }}>
          <span>{formatMsgDate(message.timestamp)} {formatMsgTime(message.timestamp)}</span>
          {!isUser && (message.model || null) && (
            <>
              <span style={{ opacity: 0.4 }}>·</span>
              <span style={{ color: t.textSub }}>{message.model || "Gemini"}</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
