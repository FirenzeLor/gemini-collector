import { useState, useEffect } from "react";
import { invoke } from "@tauri-apps/api/core";
import { TopBar } from "./components/TopBar";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { AccountPicker } from "./components/AccountPicker";
import { mockConversations, mockConversationSummaries, Account } from "./data/mockData";
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

function App() {
  const [screen, setScreen] = useState<Screen>("account-picker");
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountsLoading, setAccountsLoading] = useState(true);
  const [currentAccount, setCurrentAccount] = useState<Account | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(mockConversations[0]?.id ?? null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [isDark, setIsDark] = useState(false);

  const theme = isDark ? darkTheme : lightTheme;
  const selectedConversation = mockConversations.find((c) => c.id === selectedId) ?? null;

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

  function handleSelectAccount(account: Account) {
    setCurrentAccount(account);
    setScreen("chat");
  }

  function handleSwitchAccount(account: Account) {
    setCurrentAccount(account);
    setSelectedId(mockConversations[0]?.id ?? null);
  }

  function handleSync() {
    if (syncing) return;
    setSyncing(true);
    setTimeout(() => setSyncing(false), 2000);
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
        {/* Left column */}
        <Sidebar
          conversations={mockConversationSummaries}
          selectedId={selectedId}
          onSelect={setSelectedId}
          collapsed={sidebarCollapsed}
          syncing={syncing}
          onSync={handleSync}
          currentAccount={currentAccount!}
          accounts={accounts}
          onSwitchAccount={handleSwitchAccount}
        />
        {/* Right column */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          <TopBar
            selectedConversation={selectedConversation}
            sidebarCollapsed={sidebarCollapsed}
            onToggleSidebar={() => setSidebarCollapsed((v) => !v)}
            isDark={isDark}
            onToggleDark={() => setIsDark((v) => !v)}
            onLogout={() => setScreen("account-picker")}
          />
          <ChatView conversation={selectedConversation} />
        </div>
      </div>
    </ThemeContext.Provider>
  );
}

export default App;
