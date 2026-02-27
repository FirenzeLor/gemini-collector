import React from "react";
import { Account } from "../data/types";
import { useTheme } from "../theme";

interface AccountPickerProps {
  accounts: Account[];
  loading: boolean;
  onSelect: (account: Account) => void;
  isDark: boolean;
  onToggleDark: () => void;
}

export function AccountPicker({ accounts, loading, onSelect, isDark, onToggleDark }: AccountPickerProps) {
  const t = useTheme();

  return (
    <div style={{ width: "100vw", height: "100vh", background: t.appBg, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", position: "relative" }}>
      {/* Drag region for window */}
      <div data-tauri-drag-region style={{ position: "absolute", top: 0, left: 0, right: 0, height: 52 }} />

      {/* Dark mode toggle */}
      <button
        onClick={onToggleDark}
        title={isDark ? "切换到亮色模式" : "切换到暗色模式"}
        style={{ position: "absolute", top: 14, right: 14, width: 28, height: 28, borderRadius: 7, border: "none", background: t.topBarBg, cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", transition: "background 0.15s", backdropFilter: "blur(24px) saturate(112%)", WebkitBackdropFilter: "blur(24px) saturate(112%)" }}
        onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.background = t.btnHoverBg)}
        onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.background = "transparent")}
      >
        {isDark
          ? <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={t.textSub} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
          : <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={t.textSub} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
        }
      </button>

      {/* App identity */}
      <div style={{ textAlign: "center", marginBottom: 36 }}>
        <div style={{ width: 56, height: 56, borderRadius: 16, background: "linear-gradient(135deg, #4285f4 0%, #34a853 50%, #ea4335 100%)", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 14px", boxShadow: "0 4px 16px rgba(66,133,244,0.3)" }}>
          <svg width="28" height="28" viewBox="0 0 24 24" fill="white"><path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4L12 17l-6.2 4.3 2.4-7.4L2 9.4h7.6z" /></svg>
        </div>
        <div style={{ fontSize: 22, fontWeight: 700, color: t.text, letterSpacing: -0.3 }}>Gemini Chat</div>
        <div style={{ fontSize: 13, color: t.textSub, marginTop: 4 }}>选择要使用的账号</div>
      </div>

      {/* Content area */}
      <div style={{ width: 360, background: t.cardBg, borderRadius: 16, boxShadow: t.isDark ? "0 16px 34px rgba(5,10,20,0.42)" : "0 16px 34px rgba(70,102,156,0.2)", backdropFilter: "blur(32px) saturate(115%)", WebkitBackdropFilter: "blur(32px) saturate(115%)", overflow: "hidden", minHeight: 64 }}>
        {loading ? (
          /* Loading state */
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: 32 }}>
            <SpinnerIcon color={t.textMuted} />
          </div>
        ) : accounts.length === 0 ? (
          /* No accounts */
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", padding: "28px 24px" }}>
            <div style={{ fontSize: 13, color: t.textSub, textAlign: "center", lineHeight: 1.6 }}>
              未找到本地账号数据。<br />
              应用已自动尝试从本地浏览器 Cookies 导入账号。<br />
              请确认已在 Chrome 登录 Gemini 后重新打开应用。
            </div>
          </div>
        ) : (
          /* Account list */
          accounts.map((account, i) => (
            <AccountRow
              key={account.id}
              account={account}
              showDivider={i < accounts.length - 1}
              onClick={() => onSelect(account)}
            />
          ))
        )}
      </div>
    </div>
  );
}

function AccountRow({ account, showDivider, onClick }: { account: Account; showDivider: boolean; onClick: () => void }) {
  const t = useTheme();
  const [hovered, setHovered] = React.useState(false);

  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{ padding: "13px 18px", display: "flex", alignItems: "center", gap: 12, cursor: "pointer", borderBottom: showDivider ? `1px solid ${t.divider}` : "none", background: hovered ? t.hover : "transparent", transition: "background 0.12s" }}
    >
      <div style={{ width: 36, height: 36, borderRadius: "50%", background: account.avatarColor, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 15, flexShrink: 0, boxShadow: "0 1px 4px rgba(0,0,0,0.12)" }}>
        {account.avatarText}
      </div>
      <div style={{ flex: 1, overflow: "hidden" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <div style={{ fontSize: 14, fontWeight: 600, color: t.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{account.name}</div>
          {account.listSyncPending && (
            <span
              title="列表同步未完成"
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: "#ef4444",
                boxShadow: "0 0 0 2px rgba(239,68,68,0.16)",
                flexShrink: 0,
              }}
            />
          )}
        </div>
        <div style={{ fontSize: 12, color: t.textSub, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", marginTop: 1 }}>{account.email}</div>
      </div>
      <div style={{ textAlign: "right", flexShrink: 0 }}>
        <div style={{ fontSize: 12, color: t.textMuted }}>{account.conversationCount} 条对话</div>
        <div style={{ fontSize: 11, color: t.textMuted, marginTop: 1 }}>{account.lastSyncAt ? account.lastSyncAt.slice(0, 10) : "未同步"}</div>
      </div>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={t.textMuted} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6" /></svg>
    </div>
  );
}

function SpinnerIcon({ color }: { color: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
      style={{ animation: "spin 0.9s linear infinite", flexShrink: 0 }}>
      <style>{`@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}`}</style>
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
    </svg>
  );
}
