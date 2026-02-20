import React, { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Virtuoso } from "react-virtuoso";
import { convertFileSrc } from "@tauri-apps/api/core";
import { Attachment, Conversation, Message } from "../data/mockData";
import { useTheme } from "../theme";

function resolveUrl(att: Attachment): string {
  return att.isLocal ? convertFileSrc(att.url) : att.url;
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

interface ChatViewProps {
  conversation: Conversation | null;
}

export function ChatView({ conversation }: ChatViewProps) {
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
          itemContent={(_, msg) => <MessageBubble message={msg} />}
          style={{ flex: 1 }}
        />
      )}
    </div>
  );
}

function AttachmentStrip({ attachments }: { attachments: Attachment[] }) {
  const [lightbox, setLightbox] = useState<number | null>(null);

  return (
    <>
      <div style={{
        display: "flex",
        flexWrap: "wrap",
        gap: 6,
        justifyContent: "flex-end",
        marginBottom: 6,
      }}>
        {attachments.map((att, i) =>
          att.type === "image" ? (
            <div
              key={i}
              onClick={() => setLightbox(i)}
              style={{
                width: 120,
                height: 120,
                borderRadius: 14,
                overflow: "hidden",
                cursor: "pointer",
                flexShrink: 0,
                background: "#222",
                boxShadow: "0 2px 8px rgba(0,0,0,0.25)",
              }}
            >
              <img
                src={resolveUrl(att)}
                alt={att.name ?? `image-${i}`}
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
                draggable={false}
              />
            </div>
          ) : (
            <div
              key={i}
              onClick={() => setLightbox(i)}
              style={{
                width: 160,
                height: 110,
                borderRadius: 14,
                overflow: "hidden",
                cursor: "pointer",
                flexShrink: 0,
                background: "#111",
                boxShadow: "0 2px 8px rgba(0,0,0,0.3)",
                position: "relative",
              }}
            >
              <video
                src={resolveUrl(att)}
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
                muted
                preload="metadata"
              />
              {/* 播放按钮遮罩 */}
              <div style={{
                position: "absolute", inset: 0,
                display: "flex", alignItems: "center", justifyContent: "center",
                background: "rgba(0,0,0,0.35)",
              }}>
                <div style={{
                  width: 36, height: 36, borderRadius: "50%",
                  border: "1.5px solid rgba(255,255,255,0.85)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                }}>
                  <svg width="12" height="12" viewBox="0 0 16 16" fill="rgba(255,255,255,0.9)">
                    <polygon points="5,2 14,8 5,14" />
                  </svg>
                </div>
              </div>
            </div>
          )
        )}
      </div>

      {/* Lightbox */}
      {lightbox !== null && (
        <LightboxModal
          attachments={attachments}
          index={lightbox}
          onClose={() => setLightbox(null)}
          onChange={setLightbox}
        />
      )}
    </>
  );
}

function LightboxModal({
  attachments,
  index,
  onClose,
  onChange,
}: {
  attachments: Attachment[];
  index: number;
  onClose: () => void;
  onChange: (i: number) => void;
}) {
  const att = attachments[index];

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
      style={{
        position: "fixed", inset: 0, zIndex: 1000,
        background: "rgba(0,0,0,0.85)",
        display: "flex", alignItems: "center", justifyContent: "center",
      }}
    >
      <div onClick={e => e.stopPropagation()} style={{ position: "relative", maxWidth: "90vw", maxHeight: "90vh" }}>
        {att.type === "image" ? (
          <img
            src={resolveUrl(att)}
            alt={att.name}
            style={{ maxWidth: "90vw", maxHeight: "90vh", borderRadius: 12, objectFit: "contain", display: "block" }}
          />
        ) : (
          <video
            src={resolveUrl(att)}
            controls
            autoPlay
            style={{ maxWidth: "90vw", maxHeight: "90vh", borderRadius: 12, display: "block" }}
          />
        )}

        {/* 关闭按钮 */}
        <button
          onClick={onClose}
          style={{
            position: "absolute", top: -16, right: -16,
            width: 32, height: 32, borderRadius: "50%",
            background: "rgba(255,255,255,0.15)", border: "none",
            color: "#fff", fontSize: 18, cursor: "pointer",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
        >×</button>

        {/* 左右切换 */}
        {index > 0 && (
          <button
            onClick={() => onChange(index - 1)}
            style={{
              position: "absolute", left: -48, top: "50%", transform: "translateY(-50%)",
              width: 36, height: 36, borderRadius: "50%",
              background: "rgba(255,255,255,0.15)", border: "none",
              color: "#fff", fontSize: 20, cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >‹</button>
        )}
        {index < attachments.length - 1 && (
          <button
            onClick={() => onChange(index + 1)}
            style={{
              position: "absolute", right: -48, top: "50%", transform: "translateY(-50%)",
              width: 36, height: 36, borderRadius: "50%",
              background: "rgba(255,255,255,0.15)", border: "none",
              color: "#fff", fontSize: 20, cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >›</button>
        )}

        {/* 计数 */}
        {attachments.length > 1 && (
          <div style={{
            position: "absolute", bottom: -30, left: "50%", transform: "translateX(-50%)",
            color: "rgba(255,255,255,0.6)", fontSize: 13,
          }}>
            {index + 1} / {attachments.length}
          </div>
        )}
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: Message }) {
  const t = useTheme();
  const isUser = message.role === "user";

  return (
    <div style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start", padding: "4px 20px", gap: 8 }}>
      <div style={{ maxWidth: isUser ? "62%" : "72%" }}>
        {/* 附件区域：图片/视频横排展示 */}
        {isUser && message.attachments && message.attachments.length > 0 && (
          <AttachmentStrip attachments={message.attachments} />
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
            <span style={{ whiteSpace: "pre-wrap" }}>{message.content}</span>
          ) : (
            <div className={`prose-ai${t.isDark ? " prose-dark" : ""}`}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{fixMarkdown(message.content)}</ReactMarkdown>
            </div>
          )}
        </div>
        <div style={{ fontSize: 11, color: t.textMuted, marginTop: 3, textAlign: isUser ? "right" : "left", padding: "0 4px", display: "flex", gap: 4, justifyContent: isUser ? "flex-end" : "flex-start", alignItems: "center", flexWrap: "wrap" }}>
          <span>{message.date} {message.timestamp}</span>
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
