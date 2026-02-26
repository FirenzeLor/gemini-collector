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
  appBg: "radial-gradient(1300px 900px at 10% -12%, #cae3ff 0%, #e5f2ff 38%, #edf4ff 72%, #f4f7fb 100%)",
  sidebarBg: "rgba(255, 255, 255, 0.18)",
  cardBg: "rgba(255, 255, 255, 0.12)",
  border: "rgba(255,255,255,0.66)",
  divider: "rgba(65,83,106,0.12)",
  text: "#1d1d1f",
  textSub: "#6f7a8c",
  textMuted: "#8e97a7",
  hover: "rgba(102,119,140,0.09)",
  selectedBg: "rgba(47,123,232,0.18)",
  selectedText: "#0071e3",
  aiBubbleBg: "rgba(255,255,255,0.50)",
  topBarBg: "rgba(255,255,255,0.08)",
  btnBg: "transparent",
  btnHoverBg: "rgba(102,119,140,0.11)",
};

export const darkTheme: Theme = {
  isDark: true,
  appBg: "radial-gradient(1360px 900px at 12% -20%, #363a41 0%, #282c33 36%, #1c1f25 66%, #13151a 100%)",
  sidebarBg: "rgba(23, 25, 30, 0.22)",
  cardBg: "rgba(18, 20, 24, 0.16)",
  border: "rgba(255,255,255,0.10)",
  divider: "rgba(255,255,255,0.12)",
  text: "#f2f2f7",
  textSub: "#b5bac4",
  textMuted: "#8a909c",
  hover: "rgba(255,255,255,0.08)",
  selectedBg: "rgba(255,255,255,0.12)",
  selectedText: "#f2f2f7",
  aiBubbleBg: "rgba(22,25,31,0.56)",
  topBarBg: "rgba(18,20,24,0.12)",
  btnBg: "transparent",
  btnHoverBg: "rgba(255,255,255,0.12)",
};

export const ThemeContext = createContext<Theme>(lightTheme);
export const useTheme = () => useContext(ThemeContext);
