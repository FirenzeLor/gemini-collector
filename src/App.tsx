import { useState, useEffect } from "react";
import { invoke } from "@tauri-apps/api/core";
import { TopBar } from "./components/TopBar";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { AccountPicker } from "./components/AccountPicker";
import { Account, Conversation, ConversationSummary } from "./data/mockData";
import { ThemeContext, lightTheme, darkTheme } from "./theme";

type Screen = "account-picker" | "chat";

function parseAccountsPayload(json: string): Account[] {
  try {
    const parsed: unknown = JSON.parse(json);
    return Array.isArray(parsed) ? (parsed as Account[]) : [];
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

function App() {
  const [screen, setScreen] = useState<Screen>("account-picker");
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountsLoading, setAccountsLoading] = useState(true);
  const [currentAccount, setCurrentAccount] = useState<Account | null>(null);
  const [conversationSummaries, setConversationSummaries] = useState<ConversationSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [isDark, setIsDark] = useState(false);

  const theme = isDark ? darkTheme : lightTheme;

  const selectedSummary = conversationSummaries.find((c) => c.id === selectedId) ?? null;
  const selectedConversation: Conversation | null = selectedSummary
    ? {
        id: selectedSummary.id,
        accountId: currentAccount?.id ?? "",
        title: selectedSummary.title,
        createdAt: selectedSummary.updatedAt,
        updatedAt: selectedSummary.updatedAt,
        syncedAt: selectedSummary.syncedAt ?? selectedSummary.updatedAt,
        remoteHash: selectedSummary.remoteHash,
        messages: [],
      }
    : null;

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

  function handleSelectAccount(account: Account) {
    setCurrentAccount(account);
    setScreen("chat");
  }

  function handleSwitchAccount(account: Account) {
    setCurrentAccount(account);
  }

  async function handleSync() {
    if (syncing || !currentAccount) return;
    setSyncing(true);
    try {
      await invoke("run_list_sync", { accountId: currentAccount.id });
      const refreshedAccounts = await reloadAccounts();
      const refreshedCurrent =
        refreshedAccounts.find((a) => a.id === currentAccount.id) ?? currentAccount;
      setCurrentAccount(refreshedCurrent);
      await loadSummaries(refreshedCurrent.id);
    } catch (e) {
      console.error("同步列表失败:", e);
    } finally {
      setSyncing(false);
    }
  }

  if (screen === "account-picker") {
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
          syncing={syncing}
          onSync={handleSync}
          currentAccount={currentAccount!}
          accounts={accounts}
          onSwitchAccount={handleSwitchAccount}
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
          <ChatView conversation={selectedConversation} />
        </div>
      </div>
    </ThemeContext.Provider>
  );
}

export default App;
