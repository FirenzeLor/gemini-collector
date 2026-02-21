import { useState } from "react";
import { ConversationSummary, Account } from "../data/mockData";
import { useTheme } from "../theme";

interface SidebarProps {
  conversations: ConversationSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  collapsed: boolean;
  syncing: boolean;
  onSync: () => void;
  currentAccount: Account;
  accounts: Account[];
  onSwitchAccount: (account: Account) => void;
  onSyncConversation?: (id: string) => void;
}

// Format ISO 8601 date for sidebar: time if today, "MM-DD" otherwise
function formatConvTime(iso: string): string {
  try {
    const d = new Date(iso);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) {
      return d.toTimeString().slice(0, 5);
    }
    return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  } catch {
    return iso;
  }
}

export function Sidebar({
  conversations, selectedId, onSelect, collapsed,
  syncing, onSync, currentAccount, accounts, onSwitchAccount,
  onSyncConversation,
}: SidebarProps) {
  const t = useTheme();
  const [showSwitcher, setShowSwitcher] = useState(false);
  const [syncingIds, setSyncingIds] = useState<Set<string>>(new Set());
  const otherAccounts = accounts.filter((a) => a.id !== currentAccount.id);

  function handleSyncConv(id: string) {
    if (syncingIds.has(id)) return;
    setSyncingIds((prev) => new Set(prev).add(id));
    onSyncConversation?.(id);
    setTimeout(() => {
      setSyncingIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }, 2000);
  }

  return (
    <div style={{
      width: collapsed ? 0 : 260,
      minWidth: collapsed ? 0 : 260,
      transition: "width 0.25s cubic-bezier(0.4,0,0.2,1), min-width 0.25s cubic-bezier(0.4,0,0.2,1)",
      overflow: "hidden",
      background: t.sidebarBg,
      borderRight: `1px solid ${t.border}`,
      display: "flex",
      flexDirection: "column",
      flexShrink: 0,
    }}>
      {/* Traffic lights drag spacer */}
      <div data-tauri-drag-region style={{ height: 52, minWidth: 260, flexShrink: 0 }} />

      {/* Conversation list */}
      <div style={{ flex: 1, overflowY: "auto", padding: "0 0 4px", minWidth: 260, scrollbarGutter: "stable" }}>
        <div style={{ padding: "2px 14px 6px", fontSize: 11, fontWeight: 600, color: t.textMuted, letterSpacing: 0.5, textTransform: "uppercase" }}>
          对话历史
        </div>
        {conversations.map((conv) => (
          <ConversationItem
            key={conv.id}
            conversation={conv}
            selected={conv.id === selectedId}
            onClick={() => onSelect(conv.id)}
            syncing={syncingIds.has(conv.id)}
            onSync={() => handleSyncConv(conv.id)}
          />
        ))}
      </div>

      {/* Bottom account switcher */}
      <div
        onMouseEnter={() => setShowSwitcher(true)}
        onMouseLeave={() => setShowSwitcher(false)}
        style={{ padding: "0 6px 6px", minWidth: 260, position: "relative" }}
      >
        {/* Other accounts popup — absolutely positioned, does not affect flex layout */}
        {showSwitcher && (
          <div style={{
            position: "absolute",
            bottom: "100%",
            left: 6,
            right: 6,
            marginBottom: 2,
            borderRadius: 10,
            background: t.sidebarBg,
            border: `1px solid ${t.border}`,
            overflow: "hidden",
            boxShadow: t.isDark ? "0 -4px 16px rgba(0,0,0,0.4)" : "0 -4px 16px rgba(0,0,0,0.12)",
          }}>
            {otherAccounts.map((account) => (
              <button
                key={account.id}
                onClick={() => { onSwitchAccount(account); setShowSwitcher(false); }}
                style={{ display: "flex", width: "100%", alignItems: "center", gap: 10, padding: "8px 10px", border: "none", background: "transparent", cursor: "pointer", textAlign: "left", transition: "background 0.1s" }}
                onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.background = t.hover)}
                onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.background = "transparent")}
              >
                <div style={{ width: 28, height: 28, borderRadius: "50%", background: account.avatarColor, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 12, flexShrink: 0 }}>
                  {account.avatarText}
                </div>
                <div style={{ flex: 1, overflow: "hidden" }}>
                  <div style={{ fontSize: 13, fontWeight: 500, color: t.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{account.name}</div>
                  <div style={{ fontSize: 11, color: t.textSub, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{account.email}</div>
                </div>
              </button>
            ))}
          </div>
        )}

        {/* Current account — fixed height, always in flow */}
        <div style={{
          borderRadius: 10,
          background: showSwitcher ? t.hover : "transparent",
          transition: "background 0.12s",
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "10px 10px",
        }}>
          <div style={{ width: 28, height: 28, borderRadius: "50%", background: currentAccount.avatarColor, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 13, flexShrink: 0 }}>
            {currentAccount.avatarText}
          </div>
          <span style={{ fontSize: 13, fontWeight: 500, color: t.text, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {currentAccount.name}
          </span>
          <button
            onClick={(e) => { e.stopPropagation(); onSync(); }}
            title="Sync"
            style={{ width: 26, height: 26, borderRadius: 7, border: "none", background: "transparent", cursor: syncing ? "default" : "pointer", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, transition: "background 0.15s" }}
            onMouseEnter={(e) => { e.stopPropagation(); if (!syncing) (e.currentTarget as HTMLElement).style.background = t.btnHoverBg; }}
            onMouseLeave={(e) => { e.stopPropagation(); if (!syncing) (e.currentTarget as HTMLElement).style.background = "transparent"; }}
          >
            <SyncIcon spinning={syncing} color={syncing ? "#0071e3" : t.textSub} />
          </button>
        </div>
      </div>
    </div>
  );
}

function ConversationItem({ conversation, selected, onClick, syncing, onSync }: {
  conversation: ConversationSummary;
  selected: boolean;
  onClick: () => void;
  syncing: boolean;
  onSync: () => void;
}) {
  const t = useTheme();

  return (
    <div
      onClick={onClick}
      style={{ display: "flex", alignItems: "center", width: "calc(100% - 12px)", padding: "8px 8px 8px 12px", borderRadius: 8, margin: "1px 6px", background: selected ? t.selectedBg : "transparent", transition: "background 0.12s", cursor: "pointer", gap: 4 }}
      onMouseEnter={(e) => { if (!selected) (e.currentTarget as HTMLElement).style.background = t.hover; }}
      onMouseLeave={(e) => { if (!selected) (e.currentTarget as HTMLElement).style.background = "transparent"; }}
    >
      {/* Text content */}
      <div style={{ flex: 1, overflow: "hidden", minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: selected ? 600 : 400, color: selected ? t.selectedText : t.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", marginBottom: 2 }}>
          {conversation.title}
        </div>
        <div style={{ fontSize: 11, color: t.textMuted, display: "flex", alignItems: "center", gap: 4 }}>
          <span>{formatConvTime(conversation.updatedAt)}</span>
          <span style={{ color: t.textMuted, opacity: 0.6 }}>·</span>
          <span>{conversation.messageCount} 条</span>
        </div>
      </div>
      {/* Sync icon - right aligned */}
      <button
        onClick={(e) => { e.stopPropagation(); onSync(); }}
        title="同步此对话"
        style={{ width: 26, height: 26, borderRadius: 7, border: "none", background: "transparent", cursor: syncing ? "default" : "pointer", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, transition: "background 0.15s" }}
        onMouseEnter={(e) => { e.stopPropagation(); if (!syncing) (e.currentTarget as HTMLElement).style.background = t.btnHoverBg; }}
        onMouseLeave={(e) => { e.stopPropagation(); (e.currentTarget as HTMLElement).style.background = "transparent"; }}
      >
        <SyncIcon spinning={syncing} color={syncing ? "#0071e3" : t.textMuted} />
      </button>
    </div>
  );
}

function SyncIcon({ spinning, color }: { spinning: boolean; color: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
      style={{ animation: spinning ? "spin 0.9s linear infinite" : "none" }}>
      <style>{`@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}`}</style>
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
    </svg>
  );
}
