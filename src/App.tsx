import { useState, useEffect, useMemo, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import { TopBar } from "./components/TopBar";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { AccountPicker } from "./components/AccountPicker";
import { Account, Conversation, ConversationSummary } from "./data/types";
import { ThemeContext, lightTheme, darkTheme } from "./theme";

type Screen = "account-picker" | "chat";
type ConversationSortMode = "updated_desc" | "size_desc";
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

interface AccountExportStats {
  accountId: string;
  conversationCount: number;
  conversationFileCount: number;
  mediaFileCount: number;
  totalFileCount: number;
  totalBytes: number;
  estimatedZipBytes: number;
}

interface AccountExportResult extends AccountExportStats {
  zipPath: string;
  fileName: string;
  zipSizeBytes: number;
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

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let val = bytes;
  let idx = 0;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  const fixed = idx === 0 ? 0 : (val >= 100 ? 0 : 1);
  return `${val.toFixed(fixed)} ${units[idx]}`;
}

function parseAccountExportStatsPayload(json: string): AccountExportStats | null {
  try {
    const parsed: unknown = JSON.parse(json);
    if (!isObjectRecord(parsed)) return null;
    const accountId = toNonEmptyStringOrNull(parsed.accountId);
    if (!accountId) return null;
    return {
      accountId,
      conversationCount: toSafeNumber(parsed.conversationCount, 0),
      conversationFileCount: toSafeNumber(parsed.conversationFileCount, 0),
      mediaFileCount: toSafeNumber(parsed.mediaFileCount, 0),
      totalFileCount: toSafeNumber(parsed.totalFileCount, 0),
      totalBytes: toSafeNumber(parsed.totalBytes, 0),
      estimatedZipBytes: toSafeNumber(parsed.estimatedZipBytes, 0),
    };
  } catch {
    return null;
  }
}

function parseAccountExportResultPayload(json: string): AccountExportResult | null {
  const stats = parseAccountExportStatsPayload(json);
  if (!stats) return null;
  try {
    const parsed: unknown = JSON.parse(json);
    if (!isObjectRecord(parsed)) return null;
    const zipPath = toNonEmptyStringOrNull(parsed.zipPath);
    if (!zipPath) return null;
    return {
      ...stats,
      zipPath,
      fileName: toNonEmptyStringOrNull(parsed.fileName) ?? "account-export.zip",
      zipSizeBytes: toSafeNumber(parsed.zipSizeBytes, 0),
    };
  } catch {
    return null;
  }
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
    return parsed
      .filter((item): item is Record<string, unknown> => isObjectRecord(item))
      .map((item) => ({
        ...(item as unknown as ConversationSummary),
        status: toNonEmptyStringOrNull(item.status) ?? "normal",
      }));
  } catch {
    return [];
  }
}

function isHiddenSummary(summary: ConversationSummary): boolean {
  return summary.status === "hidden";
}

function summaryUpdatedSortValue(summary: ConversationSummary): number {
  const ts = Date.parse(summary.updatedAt ?? "");
  return Number.isNaN(ts) ? -Infinity : ts;
}

function summarySizeSortValue(summary: ConversationSummary): number {
  if (!Number.isFinite(summary.messageCount)) return 0;
  return Math.max(0, summary.messageCount);
}

function sortConversationSummaries(
  items: ConversationSummary[],
  mode: ConversationSortMode,
): ConversationSummary[] {
  return [...items].sort((a, b) => {
    const updatedDiff = summaryUpdatedSortValue(b) - summaryUpdatedSortValue(a);
    const sizeDiff = summarySizeSortValue(b) - summarySizeSortValue(a);

    if (mode === "size_desc") {
      if (sizeDiff !== 0) return sizeDiff;
      if (updatedDiff !== 0) return updatedDiff;
    } else {
      if (updatedDiff !== 0) return updatedDiff;
      if (sizeDiff !== 0) return sizeDiff;
    }

    return a.id.localeCompare(b.id);
  });
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
  const [preparingExportData, setPreparingExportData] = useState(false);
  const [exportingAccountData, setExportingAccountData] = useState(false);
  const [showExportConfirm, setShowExportConfirm] = useState(false);
  const [exportStats, setExportStats] = useState<AccountExportStats | null>(null);
  const [exportNotice, setExportNotice] = useState<{ title: string; lines: string[] } | null>(null);
  const [clearingAccountData, setClearingAccountData] = useState(false);
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [conversationSortMode, setConversationSortMode] = useState<ConversationSortMode>("updated_desc");
  const [isDark, setIsDark] = useState(false);
  const autoSyncAttemptedAtRef = useRef<Map<string, number>>(new Map());
  const hasSyncingRef = useRef(false);
  const syncingIdsRef = useRef<string[]>([]);
  const currentAccountIdRef = useRef<string | null>(null);
  const selectedIdRef = useRef<string | null>(null);

  const theme = isDark ? darkTheme : lightTheme;

  // Sync dark mode to <html> class so index.css scrollbar selectors work
  useEffect(() => {
    document.documentElement.classList.toggle("dark", isDark);
  }, [isDark]);

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
    const visibleLoaded = loaded.filter((c) => !isHiddenSummary(c));
    setConversationSummaries(loaded);
    setSelectedId((prev) =>
      prev && visibleLoaded.some((c) => c.id === prev) ? prev : (visibleLoaded[0]?.id ?? null),
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
        const visibleLoaded = loaded.filter((c) => !isHiddenSummary(c));
        if (cancelled) return;
        setConversationSummaries(loaded);
        setSelectedId((prev) =>
          prev && visibleLoaded.some((c) => c.id === prev) ? prev : (visibleLoaded[0]?.id ?? null),
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
        const visibleLoaded = loaded.filter((c) => !isHiddenSummary(c));
        if (cancelled) return;
        setConversationSummaries(loaded);
        setSelectedId((prev) =>
          prev && visibleLoaded.some((c) => c.id === prev) ? prev : (visibleLoaded[0]?.id ?? null),
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

  async function handleExportAccountData() {
    if (!currentAccount || exportingAccountData || preparingExportData || clearingAccountData) return;
    setPreparingExportData(true);
    try {
      const accountId = currentAccount.id;
      const stats = parseAccountExportStatsPayload(
        await invoke<string>("get_account_export_stats", { accountId }),
      );
      if (!stats) {
        throw new Error("读取导出统计失败");
      }
      setExportStats(stats);
      setShowExportConfirm(true);
    } catch (e) {
      console.error("导出账号数据失败:", e);
      const msg = e instanceof Error ? e.message : String(e);
      setExportNotice({
        title: "导出账号数据失败",
        lines: [msg],
      });
    } finally {
      setPreparingExportData(false);
    }
  }

  async function confirmExportAccountData() {
    if (!currentAccount || !exportStats || exportingAccountData || preparingExportData) {
      setShowExportConfirm(false);
      return;
    }

    setShowExportConfirm(false);
    setExportStats(null);
    const startedAt = Date.now();
    setExportingAccountData(true);
    try {
      const accountId = currentAccount.id;
      const selectedOutput = await open({
        directory: true,
        multiple: false,
        title: "选择导出目录",
      });
      if (!selectedOutput) {
        return;
      }
      const outputDir = Array.isArray(selectedOutput) ? selectedOutput[0] : selectedOutput;
      if (!outputDir || typeof outputDir !== "string") {
        throw new Error("未选择有效导出目录");
      }

      const result = parseAccountExportResultPayload(
        await invoke<string>("export_account_zip", { accountId, outputDir }),
      );
      if (!result) {
        throw new Error("导出失败：返回结果异常");
      }
      try {
        await revealItemInDir(result.zipPath);
      } catch (revealErr) {
        console.error("定位导出文件失败:", revealErr);
      }

      setExportNotice({
        title: "导出完成",
        lines: [
          `文件: ${result.fileName}`,
          `大小: ${formatBytes(result.zipSizeBytes)}`,
          `路径: ${result.zipPath}`,
        ],
      });
    } catch (e) {
      console.error("导出账号数据失败:", e);
      const msg = e instanceof Error ? e.message : String(e);
      setExportNotice({
        title: "导出账号数据失败",
        lines: [msg],
      });
    } finally {
      const elapsed = Date.now() - startedAt;
      if (elapsed < 450) {
        await new Promise((resolve) => window.setTimeout(resolve, 450 - elapsed));
      }
      setExportingAccountData(false);
    }
  }

  async function handleClearAccountData() {
    if (!currentAccount || clearingAccountData || exportingAccountData || preparingExportData) {
      return;
    }
    if (listSyncing || fullSyncing || syncingConversationIds.length > 0 || exportingAccountData || preparingExportData) {
      window.alert("当前有任务进行中，暂时不能清空账号数据。请等待任务结束后重试。");
      return;
    }
    setShowClearConfirm(true);
  }

  async function confirmClearAccountData() {
    if (!currentAccount || clearingAccountData || exportingAccountData || preparingExportData) {
      setShowClearConfirm(false);
      return;
    }
    if (listSyncing || fullSyncing || syncingConversationIds.length > 0 || exportingAccountData || preparingExportData) {
      setShowClearConfirm(false);
      window.alert("当前有任务进行中，暂时不能清空账号数据。请等待任务结束后重试。");
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

  const anySyncTaskRunning =
    listSyncing || fullSyncing || syncingConversationIds.length > 0 || exportingAccountData || preparingExportData;
  const visibleConversationSummaries = useMemo(() => {
    const visibleItems = conversationSummaries.filter((c) => !isHiddenSummary(c));
    return sortConversationSummaries(visibleItems, conversationSortMode);
  }, [conversationSummaries, conversationSortMode]);
  const selectedSummary = selectedId
    ? visibleConversationSummaries.find((c) => c.id === selectedId) ?? null
    : null;
  const clearDialogBg = theme.isDark ? "#171b22" : "#ffffff";
  const clearDialogBorder = theme.isDark ? "rgba(255,255,255,0.14)" : "rgba(15,23,42,0.14)";

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
      <div
        style={{
          display: "flex",
          height: "100vh",
          width: "100vw",
          overflow: "hidden",
          background: theme.appBg,
          position: "relative",
        }}
      >
        <div
          style={{
            position: "absolute",
            inset: 0,
            pointerEvents: "none",
            background: theme.isDark
              ? "radial-gradient(940px 560px at 90% 8%, rgba(255,255,255,0.12), transparent 66%), radial-gradient(860px 540px at -6% 92%, rgba(255,255,255,0.08), transparent 62%), repeating-linear-gradient(128deg, rgba(255,255,255,0.03) 0 1px, transparent 1px 28px)"
              : "radial-gradient(900px 520px at 89% 9%, rgba(126,181,255,0.3), transparent 67%), radial-gradient(860px 520px at -4% 91%, rgba(183,209,255,0.3), transparent 62%), linear-gradient(115deg, rgba(255,255,255,0.30) 0%, transparent 36%)",
          }}
        />
        <Sidebar
          conversations={visibleConversationSummaries}
          conversationSortMode={conversationSortMode}
          onToggleConversationSort={() =>
            setConversationSortMode((prev) => (prev === "updated_desc" ? "size_desc" : "updated_desc"))
          }
          selectedId={selectedId}
          onSelect={setSelectedId}
          collapsed={sidebarCollapsed}
          listSyncing={listSyncing}
          fullSyncing={fullSyncing}
          onSyncList={handleSyncList}
          onSyncFull={handleSyncAll}
          exportingAccountData={exportingAccountData || preparingExportData}
          disableExportAccountData={clearingAccountData || exportingAccountData || preparingExportData}
          onExportAccountData={handleExportAccountData}
          clearingAccountData={clearingAccountData}
          disableClearAccountData={listSyncing || fullSyncing || syncingConversationIds.length > 0 || exportingAccountData || preparingExportData}
          onClearAccountData={handleClearAccountData}
          currentAccount={currentAccount}
          accounts={accounts}
          onSwitchAccount={handleSwitchAccount}
          disableAccountSwitch={anySyncTaskRunning || clearingAccountData}
          disableConversationSync={listSyncing || fullSyncing || clearingAccountData}
          onSyncConversation={handleSyncConversation}
          syncingConversationIds={syncingConversationIds}
        />
        <div
          style={{
            flex: 1,
            minWidth: 0,
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
            background: theme.cardBg,
            backdropFilter: "blur(36px) saturate(112%)",
            WebkitBackdropFilter: "blur(36px) saturate(112%)",
            position: "relative",
            zIndex: 1,
          }}
        >
          <TopBar
            selectedConversation={selectedConversation}
            selectedSummary={selectedSummary}
            sidebarCollapsed={sidebarCollapsed}
            onToggleSidebar={() => setSidebarCollapsed((v) => !v)}
            isDark={isDark}
            onToggleDark={() => setIsDark((v) => !v)}
            disableLogout={anySyncTaskRunning}
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
      {showExportConfirm && exportStats && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 10000,
            background: "rgba(0,0,0,0.32)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            style={{
              width: 420,
              maxWidth: "calc(100vw - 32px)",
              borderRadius: 12,
              background: clearDialogBg,
              border: `1px solid ${clearDialogBorder}`,
              boxShadow: theme.isDark ? "0 18px 40px rgba(0,0,0,0.45)" : "0 18px 40px rgba(0,0,0,0.2)",
              padding: 16,
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 700, color: theme.text, marginBottom: 8 }}>
              导出当前账号数据
            </div>
            <div style={{ fontSize: 13, color: theme.textSub, lineHeight: 1.55, marginBottom: 12 }}>
              账号「{currentAccount.name || currentAccount.email || currentAccount.id}」将打包为 ZIP。
            </div>
            <div style={{ fontSize: 12, color: theme.textSub, lineHeight: 1.6, marginBottom: 14 }}>
              <div>对话数: {exportStats.conversationCount}（详情文件 {exportStats.conversationFileCount}）</div>
              <div>媒体文件: {exportStats.mediaFileCount}</div>
              <div>文件总数: {exportStats.totalFileCount}</div>
              <div>当前体积: {formatBytes(exportStats.totalBytes)}</div>
              <div>预估压缩后: {formatBytes(exportStats.estimatedZipBytes)}</div>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button
                onClick={() => {
                  setShowExportConfirm(false);
                  setExportStats(null);
                }}
                style={{
                  border: `1px solid ${clearDialogBorder}`,
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
                onClick={() => { void confirmExportAccountData(); }}
                style={{
                  border: "none",
                  background: "#0071e3",
                  color: "#fff",
                  borderRadius: 8,
                  padding: "7px 12px",
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                开始导出
              </button>
            </div>
          </div>
        </div>
      )}
      {exportNotice && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 10001,
            background: "rgba(0,0,0,0.32)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            style={{
              width: 430,
              maxWidth: "calc(100vw - 32px)",
              borderRadius: 12,
              background: clearDialogBg,
              border: `1px solid ${clearDialogBorder}`,
              boxShadow: theme.isDark ? "0 18px 40px rgba(0,0,0,0.45)" : "0 18px 40px rgba(0,0,0,0.2)",
              padding: 16,
            }}
          >
            <div style={{ fontSize: 15, fontWeight: 700, color: theme.text, marginBottom: 8 }}>
              {exportNotice.title}
            </div>
            <div style={{ fontSize: 12, color: theme.textSub, lineHeight: 1.6, marginBottom: 14 }}>
              {exportNotice.lines.map((line, idx) => (
                <div key={`${idx}_${line}`}>{line}</div>
              ))}
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button
                onClick={() => setExportNotice(null)}
                style={{
                  border: "none",
                  background: "#0071e3",
                  color: "#fff",
                  borderRadius: 8,
                  padding: "7px 12px",
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                知道了
              </button>
            </div>
          </div>
        </div>
      )}
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
              background: clearDialogBg,
              border: `1px solid ${clearDialogBorder}`,
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
                  border: `1px solid ${clearDialogBorder}`,
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
