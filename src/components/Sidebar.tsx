import { useEffect, useState } from "react";
import { Virtuoso } from "react-virtuoso";
import { ConversationSummary, Account } from "../data/types";
import { useTheme } from "../theme";

interface SidebarProps {
  conversations: ConversationSummary[];
  conversationSortMode?: "updated_desc" | "size_desc" | "media_desc";
  onToggleConversationSort?: () => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
  collapsed: boolean;
  listSyncing: boolean;
  fullSyncing: boolean;
  onSyncList: () => void;
  onSyncFull: () => void;
  exportingAccountData?: boolean;
  disableExportAccountData?: boolean;
  onExportAccountData?: () => void;
  clearingAccountData: boolean;
  disableClearAccountData?: boolean;
  onClearAccountData: () => void;
  currentAccount: Account;
  accounts: Account[];
  onSwitchAccount: (account: Account) => void;
  disableAccountSwitch?: boolean;
  disableConversationSync?: boolean;
  onSyncConversation?: (id: string) => Promise<void> | void;
  syncingConversationIds?: string[];
}

function formatConvTime(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${y}-${m}-${day} ${hh}:${mm}`;
  } catch {
    return iso;
  }
}

export function Sidebar({
  conversations, selectedId, onSelect, collapsed,
  conversationSortMode = "updated_desc", onToggleConversationSort,
  listSyncing, fullSyncing, onSyncList, onSyncFull, clearingAccountData, onClearAccountData,
  exportingAccountData = false, disableExportAccountData = false, onExportAccountData,
  disableClearAccountData = false,
  currentAccount, accounts, onSwitchAccount,
  disableAccountSwitch = false, disableConversationSync = false,
  onSyncConversation, syncingConversationIds = [],
}: SidebarProps) {
  const t = useTheme();
  const [showSwitcher, setShowSwitcher] = useState(false);
  const syncingSet = new Set(syncingConversationIds);
  const otherAccounts = accounts.filter((a) => a.id !== currentAccount.id);
  const conversationSortTitle =
    conversationSortMode === "size_desc"
      ? "当前按数量倒序（消息条数），点击切换为按媒体数量倒序"
      : conversationSortMode === "media_desc"
        ? "当前按媒体数量倒序，点击切换为按更新时间新到旧"
        : "当前按更新时间新到旧，点击切换为按数量倒序（消息条数）";
  const conversationSortLabel =
    conversationSortMode === "size_desc"
      ? "数量↓"
      : conversationSortMode === "media_desc"
        ? "媒体↓"
        : "时间↓";

  useEffect(() => {
    if (disableAccountSwitch && showSwitcher) {
      setShowSwitcher(false);
    }
  }, [disableAccountSwitch, showSwitcher]);

  function handleSyncConv(id: string) {
    if (disableConversationSync || syncingSet.has(id)) return;
    void Promise.resolve(onSyncConversation?.(id)).catch((e) => {
      console.error("同步单对话失败:", e);
    });
  }

  return (
    <div style={{
      width: collapsed ? 0 : 260,
      minWidth: collapsed ? 0 : 260,
      transition: "width 0.25s cubic-bezier(0.4,0,0.2,1), min-width 0.25s cubic-bezier(0.4,0,0.2,1)",
      overflow: "hidden",
      background: t.sidebarBg,
      borderRight: `1px solid ${t.divider}`,
      backdropFilter: "blur(34px) saturate(112%)",
      WebkitBackdropFilter: "blur(34px) saturate(112%)",
      display: "flex",
      flexDirection: "column",
      flexShrink: 0,
      position: "relative",
    }}>
      <div data-tauri-drag-region style={{ height: 52, minWidth: 260, flexShrink: 0 }} />

      <div style={{ flex: 1, minHeight: 0, padding: "0 0 4px", minWidth: 260, display: "flex", flexDirection: "column" }}>
        <div style={{ padding: "2px 12px 6px 14px", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: t.textMuted, letterSpacing: 0.5, textTransform: "uppercase" }}>
            对话历史
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <button
              onClick={(e) => {
                e.stopPropagation();
                if (exportingAccountData || disableExportAccountData) return;
                onExportAccountData?.();
              }}
              title="导出当前账号数据"
              style={{
                width: 22,
                height: 22,
                borderRadius: 6,
                border: "none",
                background: "transparent",
                cursor: (exportingAccountData || disableExportAccountData) ? "default" : "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
                opacity: (exportingAccountData || disableExportAccountData) ? 0.62 : 1,
                transition: "background 0.12s",
              }}
              onMouseEnter={(e) => {
                if (exportingAccountData || disableExportAccountData) return;
                (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.background = "transparent";
              }}
            >
              <ExportIcon spinning={exportingAccountData} color={exportingAccountData ? "#0071e3" : t.textMuted} />
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onToggleConversationSort?.();
              }}
              title={conversationSortTitle}
              style={{
                height: 22,
                borderRadius: 6,
                border: "none",
                background: "transparent",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
                padding: "0 6px",
                color: t.textMuted,
                fontSize: 10.5,
                fontWeight: 700,
                letterSpacing: 0.2,
                transition: "background 0.12s",
              }}
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.background = "transparent";
              }}
            >
              {conversationSortLabel}
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                if (clearingAccountData || disableClearAccountData) return;
                onClearAccountData();
              }}
              title="清空当前账号数据"
              style={{
                width: 22,
                height: 22,
                borderRadius: 6,
                border: "none",
                background: "transparent",
                cursor: (clearingAccountData || disableClearAccountData) ? "default" : "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
                opacity: (clearingAccountData || disableClearAccountData) ? 0.55 : 1,
                transition: "background 0.12s",
              }}
              onMouseEnter={(e) => {
                if (clearingAccountData || disableClearAccountData) return;
                (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.background = "transparent";
              }}
            >
              <TrashIcon color={clearingAccountData ? "#d34b4b" : t.textMuted} />
            </button>
          </div>
        </div>
        {conversations.length === 0 ? (
          <div style={{ padding: "10px 14px", fontSize: 12, color: t.textMuted }}>
            暂无列表数据，点击底部列表同步按钮拉取
          </div>
        ) : (
          <div style={{ flex: 1, minHeight: 0 }}>
            <Virtuoso
              style={{ height: "100%", scrollbarGutter: "stable" }}
              data={conversations}
              computeItemKey={(_, conv) => conv.id}
              increaseViewportBy={{ top: 220, bottom: 420 }}
              itemContent={(_, conv) => (
                <ConversationItem
                  conversation={conv}
                  selected={conv.id === selectedId}
                  onClick={() => onSelect(conv.id)}
                  syncing={syncingSet.has(conv.id)}
                  onSync={() => handleSyncConv(conv.id)}
                />
              )}
            />
          </div>
        )}
      </div>

      <div
        onMouseEnter={() => {
          if (disableAccountSwitch) return;
          setShowSwitcher(true);
        }}
        onMouseLeave={() => setShowSwitcher(false)}
        style={{ padding: "0 6px 6px", minWidth: 260, position: "relative" }}
      >
        {showSwitcher && (
          <div style={{
            position: "absolute",
            bottom: "100%",
            left: 6,
            right: 6,
            marginBottom: 2,
            borderRadius: 10,
            background: t.cardBg,
            border: `1px solid ${t.border}`,
            backdropFilter: "blur(28px) saturate(115%)",
            WebkitBackdropFilter: "blur(28px) saturate(115%)",
            overflow: "hidden",
            boxShadow: t.isDark ? "0 -8px 22px rgba(3,8,18,0.5)" : "0 -8px 22px rgba(80,104,146,0.18)",
          }}>
            {otherAccounts.map((account) => (
              <button
                key={account.id}
                onClick={() => {
                  if (disableAccountSwitch) return;
                  onSwitchAccount(account);
                  setShowSwitcher(false);
                }}
                style={{ display: "flex", width: "100%", alignItems: "center", gap: 10, padding: "8px 10px", border: "none", background: "transparent", cursor: disableAccountSwitch ? "default" : "pointer", textAlign: "left", transition: "background 0.1s", opacity: disableAccountSwitch ? 0.6 : 1 }}
                onMouseEnter={(e) => { if (!disableAccountSwitch) (e.currentTarget as HTMLElement).style.background = t.hover; }}
                onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = "transparent"; }}
              >
                <div style={{ width: 28, height: 28, borderRadius: "50%", background: account.avatarColor, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 12, flexShrink: 0 }}>
                  {account.avatarText}
                </div>
                <div style={{ flex: 1, overflow: "hidden" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <div style={{ fontSize: 13, fontWeight: 500, color: t.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {account.name}
                    </div>
                    {account.listSyncPending && <PendingDot />}
                  </div>
                  <div style={{ fontSize: 11, color: t.textSub, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{account.email}</div>
                </div>
              </button>
            ))}
          </div>
        )}

        <div style={{
          borderRadius: 10,
          background: showSwitcher ? t.hover : "transparent",
          transition: "background 0.12s",
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "10px 10px",
        }}>
          <div style={{ width: 28, height: 28, borderRadius: "50%", background: currentAccount.avatarColor, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 700, fontSize: 13, flexShrink: 0 }}>
            {currentAccount.avatarText}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, flex: 1, minWidth: 0 }}>
            <span style={{ fontSize: 13, fontWeight: 500, color: t.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {currentAccount.name}
            </span>
            {currentAccount.listSyncPending && <PendingDot />}
          </div>
          <button
            onClick={(e) => { e.stopPropagation(); if (!listSyncing && !fullSyncing) onSyncList(); }}
            title="同步列表"
            style={{
              height: 22,
              borderRadius: 6,
              border: "none",
              background: "transparent",
              cursor: (listSyncing || fullSyncing) ? "default" : "pointer",
              display: "flex",
              alignItems: "center",
              gap: 4,
              padding: "0 3px",
              flexShrink: 0,
              color: listSyncing ? "#0071e3" : t.textSub,
              opacity: fullSyncing && !listSyncing ? 0.65 : 1,
              transition: "background 0.12s",
            }}
            onMouseEnter={(e) => {
              e.stopPropagation();
              if (!listSyncing && !fullSyncing) (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLElement).style.background = "transparent";
            }}
          >
            <span style={{ fontSize: 11, fontWeight: 700, lineHeight: 1, letterSpacing: 0.4 }}>List</span>
            <SyncIcon spinning={listSyncing} color={listSyncing ? "#0071e3" : t.textSub} small />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); if (!fullSyncing && !listSyncing) onSyncFull(); }}
            title="完全同步（先列表后逐会话）"
            style={{
              height: 22,
              borderRadius: 6,
              border: "none",
              background: "transparent",
              cursor: (fullSyncing || listSyncing) ? "default" : "pointer",
              display: "flex",
              alignItems: "center",
              gap: 4,
              padding: "0 3px",
              flexShrink: 0,
              color: fullSyncing ? "#0071e3" : t.textSub,
              opacity: listSyncing && !fullSyncing ? 0.65 : 1,
              transition: "background 0.12s",
            }}
            onMouseEnter={(e) => {
              e.stopPropagation();
              if (!fullSyncing && !listSyncing) (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLElement).style.background = "transparent";
            }}
          >
            <span style={{ fontSize: 11, fontWeight: 700, lineHeight: 1, letterSpacing: 0.4 }}>ALL</span>
            <SyncIcon spinning={fullSyncing} color={fullSyncing ? "#0071e3" : t.textSub} small />
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
  const [copied, setCopied] = useState(false);
  const isLost = conversation.status === "lost";
  const lostTitleColor = t.isDark ? "#f87171" : "#d92d20";
  const lostMetaColor = t.isDark ? "rgba(248,113,113,0.84)" : "#b42318";

  function handleCopyConversationId() {
    void navigator.clipboard.writeText(conversation.id)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 850);
      })
      .catch((e) => {
        console.error("复制对话 ID 失败:", e);
      });
  }

  return (
    <div
      onClick={onClick}
      style={{ display: "flex", alignItems: "center", width: "calc(100% - 12px)", padding: "8px 12px", borderRadius: 8, margin: "1px 6px", background: selected ? t.selectedBg : "transparent", transition: "background 0.12s", cursor: "pointer", gap: 4 }}
      onMouseEnter={(e) => { if (!selected) (e.currentTarget as HTMLElement).style.background = t.hover; }}
      onMouseLeave={(e) => { if (!selected) (e.currentTarget as HTMLElement).style.background = "transparent"; }}
    >
      {conversation.hasFailedData && (
        <span
          title="该会话存在失败数据（通常是媒体下载失败）"
          style={{ fontSize: 12, lineHeight: 1, flexShrink: 0 }}
        >
          ⚠️
        </span>
      )}
      <div style={{ flex: 1, overflow: "hidden", minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: selected ? 600 : 400, color: isLost ? lostTitleColor : (selected ? t.selectedText : t.text), overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", marginBottom: 2 }}>
          {conversation.title}
        </div>
        <div style={{ fontSize: 11, color: isLost ? lostMetaColor : t.textMuted, display: "flex", alignItems: "center", gap: 4 }}>
          <span>{formatConvTime(conversation.updatedAt)}</span>
          <span style={{ color: isLost ? lostMetaColor : t.textMuted, opacity: 0.6 }}>·</span>
          <span>{conversation.messageCount} 条</span>
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 1, marginLeft: 3, marginRight: -2 }}>
        <button
          onClick={(e) => { e.stopPropagation(); handleCopyConversationId(); }}
          title={copied ? "已复制" : "复制对话 ID"}
          style={{ width: 24, height: 24, borderRadius: 7, border: "none", background: "transparent", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, transition: "background 0.15s" }}
          onMouseEnter={(e) => { e.stopPropagation(); (e.currentTarget as HTMLElement).style.background = t.btnHoverBg; }}
          onMouseLeave={(e) => { e.stopPropagation(); (e.currentTarget as HTMLElement).style.background = "transparent"; }}
        >
          {copied ? <CheckIcon color="#16a34a" /> : <CopyIcon color={t.textMuted} />}
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onSync(); }}
          title="同步此对话"
          style={{ width: 24, height: 24, borderRadius: 7, border: "none", background: "transparent", cursor: syncing ? "default" : "pointer", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, transition: "background 0.15s" }}
          onMouseEnter={(e) => { e.stopPropagation(); if (!syncing) (e.currentTarget as HTMLElement).style.background = t.btnHoverBg; }}
          onMouseLeave={(e) => { e.stopPropagation(); (e.currentTarget as HTMLElement).style.background = "transparent"; }}
        >
          <SyncIcon spinning={syncing} color={syncing ? "#0071e3" : t.textMuted} />
        </button>
      </div>
    </div>
  );
}

function PendingDot() {
  return (
    <span
      title="列表同步未完成"
      style={{
        width: 7,
        height: 7,
        borderRadius: "50%",
        background: "#ef4444",
        boxShadow: "0 0 0 2px rgba(239,68,68,0.16)",
        flexShrink: 0,
      }}
    />
  );
}

function ExportIcon({ spinning, color }: { spinning: boolean; color: string }) {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ animation: spinning ? "spin 0.9s linear infinite" : "none" }}>
      <style>{`@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}`}</style>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  );
}

function TrashIcon({ color }: { color: string }) {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 6 5 6 21 6" />
      <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <line x1="10" y1="11" x2="10" y2="17" />
      <line x1="14" y1="11" x2="14" y2="17" />
    </svg>
  );
}

function CopyIcon({ color }: { color: string }) {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

function CheckIcon({ color }: { color: string }) {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function SyncIcon({ spinning, color, small = false }: { spinning: boolean; color: string; small?: boolean }) {
  const size = small ? 11 : 14;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
      style={{ animation: spinning ? "spin 0.9s linear infinite" : "none" }}>
      <style>{`@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}`}</style>
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
    </svg>
  );
}
