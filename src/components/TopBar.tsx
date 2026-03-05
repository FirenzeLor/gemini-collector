import React from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import { Conversation, ConversationSummary } from "../data/types";
import { useTheme } from "../theme";

function formatUpdatedAt(iso: string): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

interface TopBarProps {
  selectedConversation: Conversation | null;
  selectedSummary?: ConversationSummary | null;
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
  isDark: boolean;
  onToggleDark: () => void;
  disableLogout?: boolean;
  onLogout: () => void;
  authuser?: string | null;
}

export function TopBar({
  selectedConversation,
  selectedSummary = null,
  sidebarCollapsed,
  onToggleSidebar,
  isDark, onToggleDark, disableLogout = false, onLogout,
  authuser = null,
}: TopBarProps) {
  const t = useTheme();
  const imageCount = Math.max(0, selectedSummary?.imageCount ?? 0);
  const videoCount = Math.max(0, selectedSummary?.videoCount ?? 0);
  const createdAt = selectedConversation?.createdAt || selectedSummary?.updatedAt || "";
  const subtitleParts: string[] = [];
  if (imageCount > 0) subtitleParts.push(`图片 ${imageCount}`);
  if (videoCount > 0) subtitleParts.push(`视频 ${videoCount}`);
  subtitleParts.push(`创建于 ${formatUpdatedAt(createdAt)}`);
  const subtitle = subtitleParts.join(" · ");

  return (
    <div
      data-tauri-drag-region
      style={{
        height: 52,
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        paddingLeft: 12,
        paddingRight: 12,
        position: "relative",
        background: t.topBarBg,
        backdropFilter: "blur(30px) saturate(112%)",
        WebkitBackdropFilter: "blur(30px) saturate(112%)",
      }}
    >
      {/* Toggle sidebar button */}
      <button
        onClick={onToggleSidebar}
        title={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
        style={{
          ...iconBtn(t.btnHoverBg),
          marginLeft: sidebarCollapsed ? 68 : 0,
          transition: "background 0.15s, margin-left 0.25s cubic-bezier(0.4,0,0.2,1)",
        }}
        onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.background = t.btnHoverBg)}
        onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.background = "transparent")}
      >
        <SidebarIcon collapsed={sidebarCollapsed} color={t.textSub} />
      </button>

      {/* Title - centered and width-constrained to avoid overlapping controls */}
      {selectedConversation && (
        <div
          style={{
            position: "absolute",
            left: sidebarCollapsed ? 152 : 84,
            right: 96,
            top: "50%",
            transform: "translateY(-50%)",
            textAlign: "center",
            pointerEvents: "none",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
          }}
        >
          <div
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: t.text,
              width: "60%",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {selectedConversation.title}
          </div>
          <div
            style={{
              fontSize: 11,
              color: t.textSub,
              marginTop: 1,
              width: "60%",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {subtitle}
          </div>
        </div>
      )}

      {/* Right: open in browser + dark mode toggle + logout */}
      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 4 }}>
        {/* Open in Gemini */}
        {selectedConversation && (
          <button
            onClick={() => {
              const bareId = selectedConversation.id.startsWith("c_")
                ? selectedConversation.id.slice(2)
                : selectedConversation.id;
              const au = authuser ?? "0";
              void openUrl(`https://gemini.google.com/u/${au}/app/${bareId}`);
            }}
            title="在浏览器中打开"
            style={iconBtn(t.btnHoverBg)}
            onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.background = t.btnHoverBg)}
            onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.background = "transparent")}
          >
            <ExternalLinkIcon color={t.textSub} />
          </button>
        )}
        {/* Dark mode toggle */}
        <button
          onClick={onToggleDark}
          title={isDark ? "切换到亮色模式" : "切换到暗色模式"}
          style={iconBtn(t.btnHoverBg)}
          onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.background = t.btnHoverBg)}
          onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.background = "transparent")}
        >
          {isDark ? <SunIcon color={t.textSub} /> : <MoonIcon color={t.textSub} />}
        </button>

        {/* Logout */}
        <button
          onClick={() => {
            if (disableLogout) return;
            onLogout();
          }}
          title="退出账号"
          style={{ ...iconBtn(t.btnHoverBg), opacity: disableLogout ? 0.55 : 1, cursor: disableLogout ? "default" : "pointer" }}
          onMouseEnter={(e) => {
            if (disableLogout) return;
            (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
          }}
          onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.background = "transparent")}
        >
          <LogoutIcon color={t.textSub} />
        </button>
      </div>
    </div>
  );
}

function iconBtn(_hoverBg?: string): React.CSSProperties {
  return {
    width: 28,
    height: 28,
    borderRadius: 7,
    border: "none",
    background: "transparent",
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    transition: "background 0.15s",
  };
}

function SidebarIcon({ collapsed, color }: { collapsed: boolean; color: string }) {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ transform: collapsed ? "rotate(180deg)" : "none", transition: "transform 0.25s cubic-bezier(0.4,0,0.2,1)" }}>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <line x1="9" y1="3" x2="9" y2="21" />
    </svg>
  );
}

function MoonIcon({ color }: { color: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

function SunIcon({ color }: { color: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="5" />
      <line x1="12" y1="1" x2="12" y2="3" />
      <line x1="12" y1="21" x2="12" y2="23" />
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
      <line x1="1" y1="12" x2="3" y2="12" />
      <line x1="21" y1="12" x2="23" y2="12" />
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
    </svg>
  );
}

function ExternalLinkIcon({ color }: { color: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  );
}

function LogoutIcon({ color }: { color: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <polyline points="16 17 21 12 16 7" />
      <line x1="21" y1="12" x2="9" y2="12" />
    </svg>
  );
}
