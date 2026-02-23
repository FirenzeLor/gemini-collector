import { useState, useEffect, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { TopBar } from "./components/TopBar";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { AccountPicker } from "./components/AccountPicker";
import { Account, Conversation, ConversationSummary } from "./data/mockData";
import { ThemeContext, lightTheme, darkTheme } from "./theme";

type Screen = "account-picker" | "chat";
const AUTO_SYNC_RETRY_MS = 60 * 1000;
const AUTO_SYNC_STALE_MS = 24 * 60 * 60 * 1000;
const AUTO_SYNC_TRACK_MAX = 500;
const WORKER_JOB_STATE_EVENT = "worker://job_state";

type JobType = "sync_list" | "sync_conversation" | "sync_full" | "sync_incremental";

interface WorkerJobError {
  code?: string;
  message?: string;
  retryable?: boolean;
}

interface WorkerJobStatePayload {
  jobId: string;
  state: "queued" | "running" | "done" | "failed";
  type: JobType;
  accountId: string;
  conversationId?: string;
  phase?: string;
  progress?: { current?: number; total?: number };
  error?: WorkerJobError;
}

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function toStringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function toNonEmptyStringOrNull(value: unknown): string | null {
  const s = toStringOrNull(value)?.trim();
  return s ? s : null;
}

function toNullableNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number" || Number.isNaN(value)) return null;
  return value;
}

function toSafeNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && !Number.isNaN(value) ? value : fallback;
}

function toAccount(raw: unknown): Account | null {
  if (!isObjectRecord(raw)) return null;

  const id = toNonEmptyStringOrNull(raw.id);
  if (!id) return null;

  const name = toNonEmptyStringOrNull(raw.name) ?? id;
  const email = toStringOrNull(raw.email) ?? "";
  const fallbackAvatarText = name.charAt(0).toUpperCase() || "?";
  const avatarText =
    toNonEmptyStringOrNull(raw.avatarText) ?? fallbackAvatarText;
  const avatarColor = toNonEmptyStringOrNull(raw.avatarColor) ?? "#667eea";

  const lastSyncResultRaw = raw.lastSyncResult;
  const lastSyncResult: Account["lastSyncResult"] =
    lastSyncResultRaw === "success" ||
    lastSyncResultRaw === "partial" ||
    lastSyncResultRaw === "failed"
      ? lastSyncResultRaw
      : null;

  return {
    id,
    name,
    email,
    avatarText,
    avatarColor,
    conversationCount: toSafeNumber(raw.conversationCount, 0),
    remoteConversationCount: toNullableNumber(raw.remoteConversationCount),
    lastSyncAt: toStringOrNull(raw.lastSyncAt),
    lastSyncResult,
    authuser: toStringOrNull(raw.authuser),
    listSyncPending: typeof raw.listSyncPending === "boolean" ? raw.listSyncPending : undefined,
  };
}

function parseAccountsPayload(json: string): Account[] {
  try {
    const parsed: unknown = JSON.parse(json);
    if (!Array.isArray(parsed)) return [];

    const accounts: Account[] = [];
    for (const item of parsed) {
      const account = toAccount(item);
      if (account) {
        accounts.push(account);
      }
    }
    return accounts;
  } catch {
    return [];
  }
}

function parseSummariesPayload(json: string): ConversationSummary[] {
  try {
    const parsed: unknown = JSON.parse(json);
    if (!Array.isArray(parsed)) return [];
    const items = parsed as ConversationSummary[];
    return [...items].sort((a, b) => {
      const ta = Date.parse(a.updatedAt ?? "");
      const tb = Date.parse(b.updatedAt ?? "");
      const va = Number.isNaN(ta) ? -Infinity : ta;
      const vb = Number.isNaN(tb) ? -Infinity : tb;
      return vb - va;
    });
  } catch {
    return [];
  }
}

function parseConversationPayload(json: string): Conversation | null {
  try {
    const parsed: unknown = JSON.parse(json);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return null;
    }
    return parsed as Conversation;
  } catch {
    return null;
  }
}

function App() {
  const [screen, setScreen] = useState<Screen>("account-picker");
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountsLoading, setAccountsLoading] = useState(true);
  const [currentAccount, setCurrentAccount] = useState<Account | null>(null);
  const [conversationSummaries, setConversationSummaries] = useState<ConversationSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedConversation, setSelectedConversation] = useState<Conversation | null>(null);
  const [mediaDir, setMediaDir] = useState<string | undefined>(undefined);
  const [mediaVersion, setMediaVersion] = useState(0);
  const [syncingConversationIds, setSyncingConversationIds] = useState<string[]>([]);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [listSyncing, setListSyncing] = useState(false);
  const [fullSyncing, setFullSyncing] = useState(false);
  const [clearingAccountData, setClearingAccountData] = useState(false);
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [isDark, setIsDark] = useState(false);
  const autoSyncAttemptedAtRef = useRef<Map<string, number>>(new Map());
  const hasSyncingRef = useRef(false);
  const syncingIdsRef = useRef<string[]>([]);
  const currentAccountIdRef = useRef<string | null>(null);
  const selectedIdRef = useRef<string | null>(null);

  const theme = isDark ? darkTheme : lightTheme;

  function pruneAutoSyncAttempts(nowMs: number) {
    const map = autoSyncAttemptedAtRef.current;
    for (const [key, ts] of map.entries()) {
      if (nowMs - ts > AUTO_SYNC_STALE_MS) {
        map.delete(key);
      }
    }
    if (map.size <= AUTO_SYNC_TRACK_MAX) return;

    const ordered = [...map.entries()].sort((a, b) => a[1] - b[1]);
    const removeCount = map.size - AUTO_SYNC_TRACK_MAX;
    for (let i = 0; i < removeCount; i += 1) {
      map.delete(ordered[i][0]);
    }
  }

  function shouldAttemptAutoSync(autoKey: string): boolean {
    const nowMs = Date.now();
    pruneAutoSyncAttempts(nowMs);
    const lastAttemptAt = autoSyncAttemptedAtRef.current.get(autoKey);
    return lastAttemptAt === undefined || nowMs - lastAttemptAt >= AUTO_SYNC_RETRY_MS;
  }

  function markAutoSyncAttempt(autoKey: string) {
    const nowMs = Date.now();
    autoSyncAttemptedAtRef.current.set(autoKey, nowMs);
    pruneAutoSyncAttempts(nowMs);
  }

  async function reloadAccounts(): Promise<Account[]> {
    const loaded = parseAccountsPayload(await invoke<string>("load_accounts"));
    setAccounts(loaded);
    return loaded;
  }

  async function loadSummaries(accountId: string): Promise<void> {
    const loaded = parseSummariesPayload(
      await invoke<string>("load_conversation_summaries", { accountId }),
    );
    setConversationSummaries(loaded);
    setSelectedId((prev) =>
      prev && loaded.some((c) => c.id === prev) ? prev : (loaded[0]?.id ?? null),
    );
  }

  async function enqueueJob(payload: {
    type: JobType;
    accountId: string;
    conversationId?: string;
  }): Promise<string> {
    return invoke<string>("enqueue_job", { req: payload });
  }

  // On startup: load local accounts, auto-import from browser cookies if empty.
  useEffect(() => {
    let cancelled = false;

    async function bootstrapAccounts() {
      try {
        let loaded = parseAccountsPayload(await invoke<string>("load_accounts"));
        if (loaded.length === 0) {
          try {
            await invoke("run_accounts_import");
          } catch (e) {
            console.error("自动导入账号失败:", e);
          }
          loaded = parseAccountsPayload(await invoke<string>("load_accounts"));
        }
        if (!cancelled) {
          setAccounts(loaded);
        }
      } catch (e) {
        console.error("启动加载账号失败:", e);
        if (!cancelled) {
          setAccounts([]);
        }
      } finally {
        if (!cancelled) {
          setAccountsLoading(false);
        }
      }
    }

    void bootstrapAccounts();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const accountId = currentAccount?.id;
    if (!accountId) {
      setConversationSummaries([]);
      setSelectedId(null);
      setSelectedConversation(null);
      setMediaDir(undefined);
      return;
    }

    async function loadForCurrent() {
      try {
        const loaded = parseSummariesPayload(
          await invoke<string>("load_conversation_summaries", { accountId }),
        );
        if (cancelled) return;
        setConversationSummaries(loaded);
        setSelectedId((prev) =>
          prev && loaded.some((c) => c.id === prev) ? prev : (loaded[0]?.id ?? null),
        );
      } catch (e) {
        console.error("加载对话列表失败:", e);
        if (!cancelled) {
          setConversationSummaries([]);
          setSelectedId(null);
        }
      }
    }

    void loadForCurrent();
    return () => {
      cancelled = true;
    };
  }, [currentAccount?.id]);

  useEffect(() => {
    let cancelled = false;
    const accountId = currentAccount?.id;
    if (!accountId) {
      setMediaDir(undefined);
      return;
    }
    const stableAccountId: string = accountId;

    async function resolveMediaDir() {
      try {
        const dir = await invoke<string>("get_account_media_dir", { accountId: stableAccountId });
        if (!cancelled) {
          setMediaDir(dir || undefined);
        }
      } catch (e) {
        console.error("解析媒体目录失败:", e);
        if (!cancelled) {
          setMediaDir(undefined);
        }
      }
    }

    void resolveMediaDir();
    return () => {
      cancelled = true;
    };
  }, [currentAccount?.id]);

  useEffect(() => {
    currentAccountIdRef.current = currentAccount?.id ?? null;
  }, [currentAccount?.id]);

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  useEffect(() => {
    hasSyncingRef.current =
      syncingConversationIds.length > 0 || listSyncing || fullSyncing;
    syncingIdsRef.current = syncingConversationIds;
  }, [syncingConversationIds, listSyncing, fullSyncing]);

  useEffect(() => {
    let cancelled = false;
    const accountId = currentAccount?.id;
    if (!accountId) return;

    async function pollSummaries() {
      try {
        const loaded = parseSummariesPayload(
          await invoke<string>("load_conversation_summaries", { accountId }),
        );
        if (cancelled) return;
        setConversationSummaries(loaded);
        setSelectedId((prev) =>
          prev && loaded.some((c) => c.id === prev) ? prev : (loaded[0]?.id ?? null),
        );
      } catch (e) {
        console.error("轮询刷新对话列表失败:", e);
      }
    }

    const timer = window.setInterval(() => {
      if (hasSyncingRef.current) {
        void pollSummaries();
      }
    }, 900);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [currentAccount?.id]);

  useEffect(() => {
    const unlistenPromise = listen<WorkerJobStatePayload>(
      WORKER_JOB_STATE_EVENT,
      (event) => {
        const payload = event.payload;
        if (!payload || !payload.type || !payload.state) return;

        if (payload.type === "sync_list") {
          if (payload.state === "queued" || payload.state === "running") {
            setListSyncing(true);
          } else if (payload.state === "done" || payload.state === "failed") {
            setListSyncing(false);
          }
        } else if (payload.type === "sync_full") {
          if (payload.state === "queued" || payload.state === "running") {
            setFullSyncing(true);
          } else if (payload.state === "done" || payload.state === "failed") {
            setFullSyncing(false);
          }
        } else if (payload.type === "sync_conversation") {
          const conversationId = payload.conversationId?.trim();
          if (!conversationId) return;
          if (payload.state === "queued" || payload.state === "running") {
            setSyncingConversationIds((prev) =>
              prev.includes(conversationId) ? prev : [...prev, conversationId],
            );
          } else if (payload.state === "done" || payload.state === "failed") {
            setSyncingConversationIds((prev) =>
              prev.filter((id) => id !== conversationId),
            );
          }
        }

        if (payload.state === "done" || payload.state === "failed") {
          const accountId = payload.accountId;
          if (!accountId) return;
          const conversationId = payload.conversationId?.trim() ?? "";
          const shouldReloadDetail =
            payload.state === "done" &&
            payload.type === "sync_conversation" &&
            currentAccountIdRef.current === accountId &&
            selectedIdRef.current === conversationId;

          if (payload.state === "done" && payload.type === "sync_conversation" && conversationId) {
            autoSyncAttemptedAtRef.current.delete(`${accountId}:${conversationId}`);
          }

          if (payload.type === "sync_conversation") {
            if (shouldReloadDetail) {
              void refreshAfterSync(accountId, true).catch((e) => {
                console.error("任务完成后刷新失败:", e);
              });
            } else if (currentAccountIdRef.current === accountId) {
              void Promise.all([reloadAccounts(), loadSummaries(accountId)]).catch((e) => {
                console.error("任务完成后刷新失败:", e);
              });
            }
          } else {
            void refreshAfterSync(accountId, false).catch((e) => {
              console.error("任务完成后刷新失败:", e);
            });
          }
        }
      },
    );

    return () => {
      void unlistenPromise.then((unlisten) => {
        unlisten();
      });
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const accountId = currentAccount?.id;
    const conversationId = selectedId;

    if (!accountId || !conversationId) {
      setSelectedConversation(null);
      return;
    }
    const stableAccountId: string = accountId;
    const stableConversationId: string = conversationId;

    async function loadDetail() {
      try {
        const detail = parseConversationPayload(
          await invoke<string>("load_conversation_detail", {
            accountId: stableAccountId,
            conversationId: stableConversationId,
          }),
        );
        if (cancelled) return;
        if (detail) {
          autoSyncAttemptedAtRef.current.delete(`${stableAccountId}:${stableConversationId}`);
          setSelectedConversation(detail);
          return;
        }
        setSelectedConversation(null);

        const autoKey = `${stableAccountId}:${stableConversationId}`;
        if (shouldAttemptAutoSync(autoKey) && !syncingIdsRef.current.includes(stableConversationId)) {
          markAutoSyncAttempt(autoKey);
          await syncConversation(stableConversationId);
        }
      } catch (e) {
        console.error("加载单对话详情失败:", e);
        if (cancelled) return;
        setSelectedConversation(null);
      }
    }

    void loadDetail();
    return () => {
      cancelled = true;
    };
  }, [currentAccount?.id, selectedId]);

  function handleSelectAccount(account: Account) {
    setCurrentAccount(account);
    setScreen("chat");
  }

  function handleSwitchAccount(account: Account) {
    if (listSyncing || fullSyncing || clearingAccountData) return;
    setCurrentAccount(account);
  }

  async function refreshAfterSync(accountId: string, reloadSelectedDetail = false): Promise<void> {
    const refreshedAccounts = await reloadAccounts();
    if (currentAccountIdRef.current === accountId) {
      setCurrentAccount((prev) =>
        refreshedAccounts.find((a) => a.id === accountId) ?? prev,
      );
    }
    await loadSummaries(accountId);

    const selectedNow = selectedIdRef.current;
    if (!reloadSelectedDetail || currentAccountIdRef.current !== accountId || !selectedNow) {
      return;
    }
    const refreshedDetail = parseConversationPayload(
      await invoke<string>("load_conversation_detail", {
        accountId,
        conversationId: selectedNow,
      }),
    );
    setSelectedConversation(refreshedDetail);
    setMediaVersion((v) => v + 1);
  }

  async function handleSyncList() {
    if (listSyncing || fullSyncing || !currentAccount) return;
    setListSyncing(true);
    try {
      await enqueueJob({
        type: "sync_list",
        accountId: currentAccount.id,
      });
    } catch (e) {
      console.error("同步列表失败:", e);
      setListSyncing(false);
    }
  }

  async function syncConversation(
    conversationId: string,
    options?: { accountId?: string; allowDuringFullSync?: boolean },
  ): Promise<boolean> {
    const accountId = options?.accountId ?? currentAccount?.id;
    if (
      !accountId ||
      !conversationId ||
      syncingConversationIds.includes(conversationId) ||
      listSyncing ||
      (fullSyncing && !options?.allowDuringFullSync)
    ) {
      return false;
    }
    setSyncingConversationIds((prev) =>
      prev.includes(conversationId) ? prev : [...prev, conversationId],
    );
    try {
      await enqueueJob({
        type: "sync_conversation",
        accountId,
        conversationId,
      });
      return true;
    } catch (e) {
      console.error("同步单对话失败:", e);
      setSyncingConversationIds((prev) => prev.filter((id) => id !== conversationId));
      return false;
    }
  }

  async function handleSyncConversation(conversationId: string) {
    if (!currentAccount || !conversationId) return;
    autoSyncAttemptedAtRef.current.delete(`${currentAccount.id}:${conversationId}`);
    await syncConversation(conversationId);
  }

  async function handleClearAccountData() {
    if (!currentAccount || clearingAccountData) {
      return;
    }
    if (listSyncing || fullSyncing || syncingConversationIds.length > 0) {
      window.alert("当前有同步任务进行中，暂时不能清空账号数据。请等待同步结束后重试。");
      return;
    }
    setShowClearConfirm(true);
  }

  async function confirmClearAccountData() {
    if (!currentAccount || clearingAccountData) {
      setShowClearConfirm(false);
      return;
    }
    if (listSyncing || fullSyncing || syncingConversationIds.length > 0) {
      setShowClearConfirm(false);
      window.alert("当前有同步任务进行中，暂时不能清空账号数据。请等待同步结束后重试。");
      return;
    }

    setShowClearConfirm(false);
    setClearingAccountData(true);
    try {
      const accountId = currentAccount.id;
      await invoke("clear_account_data", { accountId });

      const nextAttemptMap = new Map<string, number>();
      for (const [k, v] of autoSyncAttemptedAtRef.current.entries()) {
        if (!k.startsWith(`${accountId}:`)) {
          nextAttemptMap.set(k, v);
        }
      }
      autoSyncAttemptedAtRef.current = nextAttemptMap;
      setSelectedConversation(null);
      setSelectedId(null);
      setMediaVersion((v) => v + 1);

      const refreshedAccounts = await reloadAccounts();
      const refreshedCurrent =
        refreshedAccounts.find((a) => a.id === accountId) ?? currentAccount;
      setCurrentAccount(refreshedCurrent);
      await loadSummaries(accountId);
    } catch (e) {
      console.error("清空账号数据失败:", e);
      const msg = e instanceof Error ? e.message : String(e);
      window.alert(`清空账号数据失败：${msg}`);
    } finally {
      setClearingAccountData(false);
    }
  }

  async function handleSyncAll() {
    if (fullSyncing || listSyncing || !currentAccount) return;

    setFullSyncing(true);
    try {
      await enqueueJob({
        type: "sync_full",
        accountId: currentAccount.id,
      });
    } catch (e) {
      console.error("完全同步失败:", e);
      setFullSyncing(false);
    }
  }

  if (screen === "account-picker" || !currentAccount) {
    return (
      <ThemeContext.Provider value={theme}>
        <AccountPicker
          accounts={accounts}
          loading={accountsLoading}
          onSelect={handleSelectAccount}
          isDark={isDark}
          onToggleDark={() => setIsDark((v) => !v)}
        />
      </ThemeContext.Provider>
    );
  }

  return (
    <ThemeContext.Provider value={theme}>
      <div style={{ display: "flex", height: "100vh", width: "100vw", overflow: "hidden", background: theme.appBg }}>
        <Sidebar
          conversations={conversationSummaries}
          selectedId={selectedId}
          onSelect={setSelectedId}
          collapsed={sidebarCollapsed}
          listSyncing={listSyncing}
          fullSyncing={fullSyncing}
          onSyncList={handleSyncList}
          onSyncFull={handleSyncAll}
          clearingAccountData={clearingAccountData}
          disableClearAccountData={listSyncing || fullSyncing || syncingConversationIds.length > 0}
          onClearAccountData={handleClearAccountData}
          currentAccount={currentAccount}
          accounts={accounts}
          onSwitchAccount={handleSwitchAccount}
          disableAccountSwitch={listSyncing || fullSyncing || clearingAccountData}
          disableConversationSync={listSyncing || fullSyncing || clearingAccountData}
          onSyncConversation={handleSyncConversation}
          syncingConversationIds={syncingConversationIds}
        />
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          <TopBar
            selectedConversation={selectedConversation}
            sidebarCollapsed={sidebarCollapsed}
            onToggleSidebar={() => setSidebarCollapsed((v) => !v)}
            isDark={isDark}
            onToggleDark={() => setIsDark((v) => !v)}
            onLogout={() => {
              setCurrentAccount(null);
              setConversationSummaries([]);
              setSelectedId(null);
              setScreen("account-picker");
            }}
          />
          <ChatView conversation={selectedConversation} mediaDir={mediaDir} mediaVersion={mediaVersion} />
        </div>
      </div>
      {showClearConfirm && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 9999,
            background: "rgba(0,0,0,0.32)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            style={{
              width: 380,
              maxWidth: "calc(100vw - 32px)",
              borderRadius: 12,
              background: theme.cardBg,
              border: `1px solid ${theme.border}`,
              boxShadow: theme.isDark ? "0 18px 40px rgba(0,0,0,0.45)" : "0 18px 40px rgba(0,0,0,0.2)",
              padding: 16,
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 700, color: theme.text, marginBottom: 8 }}>
              确认清空本地数据？
            </div>
            <div style={{ fontSize: 13, color: theme.textSub, lineHeight: 1.5, marginBottom: 14 }}>
              账号「{currentAccount.name || currentAccount.email || currentAccount.id}」的会话与媒体缓存将被删除，且不可恢复。
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button
                onClick={() => setShowClearConfirm(false)}
                style={{
                  border: `1px solid ${theme.border}`,
                  background: "transparent",
                  color: theme.text,
                  borderRadius: 8,
                  padding: "7px 12px",
                  fontSize: 12,
                  cursor: "pointer",
                }}
              >
                取消
              </button>
              <button
                onClick={() => void confirmClearAccountData()}
                style={{
                  border: "none",
                  background: "#d34b4b",
                  color: "#fff",
                  borderRadius: 8,
                  padding: "7px 12px",
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                确认清空
              </button>
            </div>
          </div>
        </div>
      )}
    </ThemeContext.Provider>
  );
}

export default App;
