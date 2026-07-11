<div align="center">

<img src="src-tauri/icons/128x128.png" width="120" style="border-radius:20px"/>

# Gemini Collector

**Back up all your Google Gemini conversations & AI-generated media locally**

Native desktop app for macOS & Windows · Multi-account · Light / Dark theme

[**简体中文**](./README.zh-CN.md)

![GitHub Release](https://img.shields.io/github/v/release/FirenzeLor/gemini-collector?color=blue)

![Last Commit](https://img.shields.io/github/last-commit/FirenzeLor/gemini-collector) ![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)

</div>

Gemini Collector turns your entire **Gemini chat history** into a local offline archive — every conversation, uploaded attachment, and AI-generated image, music track, video, and report is saved to your machine, ready to browse and export anytime. A one-click alternative to Google Takeout for backing up Gemini.

---

## Screenshots

<div align="center">

| Light theme | Dark theme |
|:---:|:---:|
| <img src=".github/images/account-picker-light.png" width="420" alt="Google Gemini multi-account picker desktop app light theme" title="Light theme" /> | <img src=".github/images/account-picker-dark.png" width="420" alt="Google Gemini multi-account picker desktop app dark theme" title="Dark theme" /> |

| Chat view | Media messages |
|:---:|:---:|
| <img src=".github/images/chat-main.png" width="420" alt="Google Gemini local chat history viewer and backup desktop interface" title="Chat view" /> | <img src=".github/images/chat-media.png" width="420" alt="Downloading and viewing Gemini AI-generated images and media messages locally" title="Media messages" /> |

| Conversation list | Export |
|:---:|:---:|
| <img src=".github/images/chat-conversation.png" width="420" alt="Google Gemini conversation list sync and local backup management" title="Conversation list" /> | <img src=".github/images/export-dialog.png" width="420" alt="Exporting Google Gemini conversations to local Markdown and JSON formats" title="Export" /> |

| Deep Research | Canvas files |
|:---:|:---:|
| <img src=".github/images/deep-research-chat.png" width="420" alt="Google Gemini Deep Research conversation with rounds and sources summary" title="Deep Research" /> | <img src=".github/images/canvas-chat.png" width="420" alt="Multiple AI-generated HTML canvas files displayed inline in chat" title="Canvas files" /> |

| Research progress | Research report |
|:---:|:---:|
| <img src=".github/images/deep-research-progress.png" width="420" alt="Deep Research progress timeline with rounds, thinking steps, and web sources" title="Research progress" /> | <img src=".github/images/deep-research-report.png" width="420" alt="Deep Research full report viewer with table of contents" title="Research report" /> |

</div>

---

## Features

**Zero-config sync**
- **macOS**: automatically detects all Gemini accounts signed in to Chrome — one-click sync, no setup needed
- **Windows**: sign in via the built-in browser on first launch, then sync automatically
- Multi-account support with independent management, incremental updates, and resumable transfers

**Full content archival**
- Sync all conversation text with complete context
- Download user-uploaded attachments
- Save AI-generated images, music, videos, reports, and more — nothing is left behind

**Reading experience**
- Native UI with automatic light / dark theme switching
- Full rendering: Markdown, syntax-highlighted code, LaTeX math
- Timeline navigation for instant access to thousands of conversations
- Right-click to delete individual conversations

**Export**
- Filter by time range (all / last 3 days / 7 days / 1 month)
- Export formats:
  - Raw data
  - [Kelivo](https://github.com/Chevey339/kelivo)
  - [Kelivo](https://github.com/Chevey339/kelivo) split packages (for iOS devices with limited single-import size)
- Preview file count and size before exporting

---

## Security & Privacy

**Everything runs locally. No data is ever uploaded.**

- **macOS**: reads local Chrome cookies for Gemini authorization — no manual login required
- **Windows**: Google sign-in via built-in WebView2 browser, cookies stored locally only
- All synced content stays on your machine — no third-party servers involved
- No account registration or extra authorization needed

---

## Install

| Platform | Status | Instructions |
|:---|:---:|:---|
| macOS | ✅ | Download the latest `.dmg` from [Releases](https://github.com/FirenzeLor/gemini-collector/releases), drag to Applications |
| Windows | ✅ | Download the latest installer from [Releases](https://github.com/FirenzeLor/gemini-collector/releases) and run it |

> **macOS "unverified developer" warning**: Go to **System Settings → Privacy & Security** and click "Open Anyway".
>
> **macOS "damaged" warning**: Run the following in Terminal, then reopen:
> ```bash
> xattr -cr /Applications/gemini-collector.app
> ```

---

## Requirements

**macOS**
- macOS 12+
- Google Chrome installed and signed in to [Gemini](https://gemini.google.com)

**Windows**
- Windows 10 (1803+) or later
- Sign in to Google within the app on first use (Chrome not required)

---

## License

[GNU AGPL-3.0](./LICENSE) — free to use, modify, and share. If you run a modified version as a network service, or distribute it, you must release the complete corresponding source code under the same license.

**Dual licensing:** a separate [commercial license](./COMMERCIAL-LICENSE.md) is available for proprietary/closed-source or hosted use that cannot comply with the AGPL.
