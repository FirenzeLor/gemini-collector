import { createContext, useContext } from "react";

export interface Theme {
  isDark: boolean;
  // backgrounds
  appBg: string;
  sidebarBg: string;
  cardBg: string;
  // borders
  border: string;
  divider: string;
  // text
  text: string;
  textSub: string;
  textMuted: string;
  // interactive
  hover: string;
  selectedBg: string;
  selectedText: string;
  // messages
  aiBubbleBg: string;
  // topbar
  topBarBg: string;
  // buttons
  btnBg: string;
  btnHoverBg: string;
}

export const lightTheme: Theme = {
  isDark: false,
  appBg: "#f5f5f7",
  sidebarBg: "rgba(247,247,249,0.98)",
  cardBg: "#ffffff",
  border: "rgba(0,0,0,0.07)",
  divider: "rgba(0,0,0,0.06)",
  text: "#1d1d1f",
  textSub: "#8e8e93",
  textMuted: "#aeaeb2",
  hover: "rgba(0,0,0,0.05)",
  selectedBg: "rgba(0,113,227,0.09)",
  selectedText: "#0071e3",
  aiBubbleBg: "#ffffff",
  topBarBg: "transparent",
  btnBg: "transparent",
  btnHoverBg: "rgba(0,0,0,0.06)",
};

export const darkTheme: Theme = {
  isDark: true,
  appBg: "#1c1c1e",
  sidebarBg: "#252527",
  cardBg: "#2c2c2e",
  border: "rgba(255,255,255,0.08)",
  divider: "rgba(255,255,255,0.07)",
  text: "#f2f2f7",
  textSub: "#98989d",
  textMuted: "#636366",
  hover: "rgba(255,255,255,0.06)",
  selectedBg: "rgba(0,113,227,0.22)",
  selectedText: "#4da3ff",
  aiBubbleBg: "#2c2c2e",
  topBarBg: "transparent",
  btnBg: "transparent",
  btnHoverBg: "rgba(255,255,255,0.08)",
};

export const ThemeContext = createContext<Theme>(lightTheme);
export const useTheme = () => useContext(ThemeContext);
