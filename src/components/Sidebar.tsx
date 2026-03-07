import { useEffect, useRef, useState, useCallback } from "react";
import { createPortal } from "react-dom";
import { invoke } from "@tauri-apps/api/core";
import { Virtuoso } from "react-virtuoso";
import { ConversationSummary, Account, SearchResult } from "../data/types";
import { useTheme } from "../theme";

interface SidebarProps {
  conversations: ConversationSummary[];
  conversationSortMode?: "updated_desc" | "size_desc" | "media_desc";
  onToggleConversationSort?: () => void;
  selectedId: string | null;
  onSelect: (id: string, messageId?: string) => void;
  collapsed: boolean;
  listSyncing: boolean;
  fullSyncing: boolean;
  onSyncList: () => void;
  onSyncFull: () => void;
  importingAccountData?: boolean;
  onImport?: () => void;
  exportingAccountData?: boolean;
  onOpenExportModal?: () => void;
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
  onDeleteConversation?: (convId: string) => void;
  onCancelList?: () => void;
  onCancelFull?: () => void;
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
  importingAccountData = false, onImport,
  exportingAccountData = false, onOpenExportModal,
  disableClearAccountData = false,
  currentAccount, accounts, onSwitchAccount,
  disableAccountSwitch = false, disableConversationSync = false,
  onSyncConversation, syncingConversationIds = [],
  onDeleteConversation,
  onCancelList,
  onCancelFull,
}: SidebarProps) {
  const t = useTheme();
  const [showSwitcher, setShowSwitcher] = useState(false);
  const [cancelConfirm, setCancelConfirm] = useState<"list" | "full" | null>(null);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; convId: string } | null>(null);
  const [showSearch, setShowSearch] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const searchPanelRef = useRef<HTMLDivElement | null>(null);
  const syncingSet = new Set(syncingConversationIds);

  const doSearch = useCallback(async (q: string) => {
    if (!q.trim()) {
      setSearchResults([]);
      setSearching(false);
      return;
    }
    setSearching(true);
    try {
      const raw = await invoke<string>("search_conversations", {
        accountId: currentAccount.id,
        query: q.trim(),
        limit: 50,
      });
      setSearchResults(JSON.parse(raw) as SearchResult[]);
    } catch (e) {
      console.error("搜索失败:", e);
      setSearchResults([]);
    } finally {
      setSearching(false);
    }
  }, [currentAccount.id]);

  // debounced search
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    if (!searchQuery.trim()) {
      setSearchResults([]);
      return;
    }
    searchTimerRef.current = setTimeout(() => { void doSearch(searchQuery); }, 300);
    return () => { if (searchTimerRef.current) clearTimeout(searchTimerRef.current); };
  }, [searchQuery, doSearch]);

  // 切换账号时清空搜索
  useEffect(() => {
    setShowSearch(false);
    setSearchQuery("");
    setSearchResults([]);
  }, [currentAccount.id]);

  // 搜索弹窗打开时聚焦输入框
  useEffect(() => {
    if (showSearch) {
      requestAnimationFrame(() => searchInputRef.current?.focus());
    }
  }, [showSearch]);

  // 点击弹窗外部关闭搜索
  useEffect(() => {
    if (!showSearch) return;
    function handleClickOutside(e: MouseEvent) {
      if (searchPanelRef.current && !searchPanelRef.current.contains(e.target as Node)) {
        setShowSearch(false);
        setSearchQuery("");
        setSearchResults([]);
      }
    }
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setShowSearch(false);
        setSearchQuery("");
        setSearchResults([]);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [showSearch]);
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
    <div
      onClick={() => setContextMenu(null)}
      style={{
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
            {/* 搜索按钮 */}
            <button
              title="搜索对话内容"
              onClick={(e) => {
                e.stopPropagation();
                setShowSearch(true);
              }}
              style={{
                width: 22,
                height: 22,
                borderRadius: 6,
                border: "none",
                background: "transparent",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
                transition: "background 0.12s",
              }}
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.background = "transparent";
              }}
            >
              <SearchIcon color={t.textMuted} />
            </button>
            {/* 导入按钮 */}
            <button
              title="导入 ZIP 压缩包到当前账号"
              onClick={(e) => {
                e.stopPropagation();
                onImport?.();
              }}
              style={{
                width: 22,
                height: 22,
                borderRadius: 6,
                border: "none",
                background: "transparent",
                cursor: importingAccountData ? "default" : "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
                opacity: importingAccountData ? 0.62 : 1,
                transition: "background 0.12s",
              }}
              onMouseEnter={(e) => {
                if (!importingAccountData) (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.background = "transparent";
              }}
            >
              <ImportIcon spinning={importingAccountData} color={importingAccountData ? "#0071e3" : t.textMuted} />
            </button>
            {/* 导出按钮 */}
            <button
              title="导出当前账号数据"
              onClick={(e) => {
                e.stopPropagation();
                onOpenExportModal?.();
              }}
              style={{
                width: 22,
                height: 22,
                borderRadius: 6,
                border: "none",
                background: "transparent",
                cursor: exportingAccountData ? "default" : "pointer",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
                opacity: exportingAccountData ? 0.62 : 1,
                transition: "background 0.12s",
              }}
              onMouseEnter={(e) => {
                if (!exportingAccountData) (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
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
        {/* 搜索弹窗 — Portal 到 body，全屏居中 */}
        {showSearch && createPortal(
          <div style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            zIndex: 9000,
            background: t.isDark ? "rgba(0,0,0,0.5)" : "rgba(0,0,0,0.22)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}>
            <div
              ref={searchPanelRef}
              style={{
                width: 480,
                maxWidth: "90vw",
                maxHeight: "70vh",
                borderRadius: 14,
                background: t.isDark ? "rgba(36,38,46,0.88)" : "rgba(255,255,255,0.85)",
                border: `1px solid ${t.divider}`,
                backdropFilter: "blur(40px) saturate(130%)",
                WebkitBackdropFilter: "blur(40px) saturate(130%)",
                boxShadow: t.isDark ? "0 16px 48px rgba(0,0,0,0.55)" : "0 16px 48px rgba(80,104,146,0.28)",
                display: "flex",
                flexDirection: "column",
                overflow: "hidden",
              }}
            >
              <div style={{ padding: "14px 14px 8px", position: "relative" }}>
                <SearchIcon color={t.textMuted} style={{ position: "absolute", left: 24, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }} />
                <style>{`.search-input::placeholder { color: ${t.textMuted}; opacity: 1; }`}</style>
                <input
                  ref={searchInputRef}
                  className="search-input"
                  type="text"
                  placeholder="搜索对话内容..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  style={{
                    width: "100%",
                    height: 36,
                    borderRadius: 10,
                    border: `1px solid ${t.divider}`,
                    background: t.isDark ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.06)",
                    color: t.text,
                    fontSize: 13,
                    paddingLeft: 32,
                    paddingRight: searchQuery ? 30 : 10,
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
                {searchQuery && (
                  <button
                    onClick={() => setSearchQuery("")}
                    style={{ position: "absolute", right: 18, top: "50%", transform: "translateY(-50%)", width: 20, height: 20, borderRadius: 6, border: "none", background: "transparent", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", color: t.textMuted, fontSize: 15 }}
                  >
                    ×
                  </button>
                )}
              </div>
              <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "0 8px 8px" }}>
                {searchQuery.trim() ? (
                  searching ? (
                    <div style={{ padding: "12px 8px", fontSize: 13, color: t.textMuted }}>搜索中...</div>
                  ) : searchResults.length === 0 ? (
                    <div style={{ padding: "12px 8px", fontSize: 13, color: t.textMuted }}>无匹配结果</div>
                  ) : (
                    searchResults.map((r, i) => (
                      <div
                        key={`${r.conversationId}-${r.messageId}-${i}`}
                        onClick={() => {
                          onSelect(r.conversationId, r.messageId);
                          setShowSearch(false);
                          setSearchQuery("");
                          setSearchResults([]);
                        }}
                        style={{
                          padding: "10px 10px",
                          borderRadius: 8,
                          margin: "1px 0",
                          cursor: "pointer",
                          background: "transparent",
                          transition: "background 0.12s",
                        }}
                        onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = t.hover; }}
                        onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = "transparent"; }}
                      >
                        <div style={{ fontSize: 13, fontWeight: 600, color: t.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", marginBottom: 4 }}>
                          {r.title || r.conversationId}
                        </div>
                        <div
                          style={{ fontSize: 12, color: t.textMuted, lineHeight: 1.5, overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}
                          dangerouslySetInnerHTML={{ __html: r.snippet }}
                        />
                      </div>
                    ))
                  )
                ) : (
                  <div style={{ padding: "12px 8px", fontSize: 13, color: t.textMuted }}>输入关键词搜索对话内容</div>
                )}
              </div>
            </div>
          </div>,
          document.body
        )}
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
                  onContextMenu={(e) => {
                    e.preventDefault();
                    setContextMenu({ x: e.clientX, y: e.clientY, convId: conv.id });
                  }}
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
            border: "none",
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
          position: "relative",
          borderRadius: 10,
          background: showSwitcher ? t.hover : "transparent",
          transition: "background 0.12s",
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "10px 10px",
        }}>
          {cancelConfirm && (
            <div
              style={{
                position: "absolute",
                bottom: "100%",
                left: 0,
                right: 0,
                marginBottom: 4,
                borderRadius: 10,
                background: t.cardBg,
                border: `1px solid ${t.divider}`,
                backdropFilter: "blur(28px) saturate(115%)",
                WebkitBackdropFilter: "blur(28px) saturate(115%)",
                boxShadow: t.isDark
                  ? "0 -6px 18px rgba(3,8,18,0.5)"
                  : "0 -6px 18px rgba(80,104,146,0.18)",
                padding: "10px 12px",
                zIndex: 200,
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <div style={{ fontSize: 12, color: t.text, marginBottom: 8 }}>
                {cancelConfirm === "list" ? "终止同步？" : "终止同步？"}
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setCancelConfirm(null);
                    cancelConfirm === "list" ? onCancelList?.() : onCancelFull?.();
                  }}
                  style={{
                    flex: 1, height: 26, borderRadius: 6, border: "none",
                    background: "#ef4444", color: "#fff",
                    fontSize: 12, fontWeight: 600, cursor: "pointer",
                  }}
                >
                  终止
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); setCancelConfirm(null); }}
                  style={{
                    flex: 1, height: 26, borderRadius: 6,
                    border: `1px solid ${t.divider}`,
                    background: "transparent", color: t.text,
                    fontSize: 12, cursor: "pointer",
                  }}
                >
                  继续
                </button>
              </div>
            </div>
          )}
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
            onClick={(e) => {
              e.stopPropagation();
              if (listSyncing) {
                setCancelConfirm(prev => prev === "list" ? null : "list");
                return;
              }
              if (!fullSyncing) onSyncList();
            }}
            title={listSyncing ? "点击终止列表同步" : "同步列表"}
            style={{
              height: 22,
              borderRadius: 6,
              border: "none",
              background: "transparent",
              cursor: (listSyncing || !fullSyncing) ? "pointer" : "default",
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
              if (listSyncing || !fullSyncing) (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLElement).style.background = "transparent";
            }}
          >
            <span style={{ fontSize: 11, fontWeight: 700, lineHeight: 1, letterSpacing: 0.4 }}>List</span>
            <SyncIcon spinning={listSyncing} color={listSyncing ? "#0071e3" : t.textSub} small />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              if (fullSyncing) {
                setCancelConfirm(prev => prev === "full" ? null : "full");
                return;
              }
              if (!listSyncing) onSyncFull();
            }}
            title={fullSyncing ? "点击终止完全同步" : "完全同步（先列表后逐会话）"}
            style={{
              height: 22,
              borderRadius: 6,
              border: "none",
              background: "transparent",
              cursor: (fullSyncing || !listSyncing) ? "pointer" : "default",
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
              if (fullSyncing || !listSyncing) (e.currentTarget as HTMLElement).style.background = t.btnHoverBg;
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
      {contextMenu && (
        <div
          style={{
            position: "fixed",
            top: contextMenu.y,
            left: contextMenu.x,
            zIndex: 3000,
            background: t.isDark ? "#1c1f25" : "#ffffff",
            borderRadius: 8,
            border: `1px solid ${t.divider}`,
            padding: "4px 0",
            minWidth: 140,
            boxShadow: "0 4px 16px rgba(0,0,0,0.18)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            onClick={() => {
              onDeleteConversation?.(contextMenu.convId);
              setContextMenu(null);
            }}
            style={{
              display: "block",
              width: "100%",
              padding: "8px 14px",
              border: "none",
              background: "transparent",
              color: "#ef4444",
              fontSize: 13,
              textAlign: "left",
              cursor: "pointer",
            }}
          >
            删除此对话
          </button>
        </div>
      )}
    </div>
  );
}

function ConversationItem({ conversation, selected, onClick, syncing, onSync, onContextMenu }: {
  conversation: ConversationSummary;
  selected: boolean;
  onClick: () => void;
  syncing: boolean;
  onSync: () => void;
  onContextMenu?: (e: React.MouseEvent<HTMLDivElement>) => void;
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
      onContextMenu={onContextMenu}
      style={{ display: "flex", alignItems: "center", width: "calc(100% - 12px)", padding: "8px 12px", borderRadius: 8, margin: "1px 6px", background: selected ? t.selectedBg : "transparent", transition: "background 0.12s", cursor: "pointer", gap: 4 }}
      onMouseEnter={(e) => { if (!selected) (e.currentTarget as HTMLElement).style.background = t.hover; }}
      onMouseLeave={(e) => { if (!selected) (e.currentTarget as HTMLElement).style.background = "transparent"; }}
    >
      {isLost && (
        <span
          title="该会话在远端已不存在"
          style={{ fontSize: 12, lineHeight: 1, flexShrink: 0 }}
        >
          ❌
        </span>
      )}
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


function ImportIcon({ spinning, color }: { spinning: boolean; color: string }) {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ animation: spinning ? "spin 0.9s linear infinite" : "none" }}>
      <style>{`@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}`}</style>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="17 8 12 3 7 8" />
      <line x1="12" y1="3" x2="12" y2="15" />
    </svg>
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

function SearchIcon({ color, style }: { color: string; style?: React.CSSProperties }) {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" style={style}>
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
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
