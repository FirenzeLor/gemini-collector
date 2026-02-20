import { useState } from "react";
import { TopBar } from "./components/TopBar";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { AccountPicker } from "./components/AccountPicker";
import { mockConversations, mockAccounts, Account } from "./data/mockData";
import { ThemeContext, lightTheme, darkTheme } from "./theme";

type Screen = "account-picker" | "chat";

function App() {
  const [screen, setScreen] = useState<Screen>("account-picker");
  const [currentAccount, setCurrentAccount] = useState<Account | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(mockConversations[0]?.id ?? null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [isDark, setIsDark] = useState(false);

  const theme = isDark ? darkTheme : lightTheme;
  const selectedConversation = mockConversations.find((c) => c.id === selectedId) ?? null;

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
        <AccountPicker accounts={mockAccounts} onSelect={handleSelectAccount} isDark={isDark} onToggleDark={() => setIsDark((v) => !v)} />
      </ThemeContext.Provider>
    );
  }

  return (
    <ThemeContext.Provider value={theme}>
      <div style={{ display: "flex", height: "100vh", width: "100vw", overflow: "hidden", background: theme.appBg }}>
        {/* Left column */}
        <Sidebar
          conversations={mockConversations}
          selectedId={selectedId}
          onSelect={setSelectedId}
          collapsed={sidebarCollapsed}
          syncing={syncing}
          onSync={handleSync}
          currentAccount={currentAccount!}
          accounts={mockAccounts}
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
