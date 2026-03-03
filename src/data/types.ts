// Type interfaces used by frontend state and Tauri payloads.

export interface Attachment {
  mediaId: string;   // filename with extension in media/ dir
  mimeType: string;
  size?: number;     // file size in bytes, injected by Rust at load time
  previewMediaId?: string; // optional preview image for video
  downloadFailed?: boolean;
  downloadError?: string;
}

export interface ConvMessage {
  type: "message";
  id: string;
  role: "user" | "model";
  text: string;
  attachments: Attachment[];
  timestamp: string;   // ISO 8601
  model?: string;      // only when role=="model"
  thinking?: string;   // only when role=="model" and thinking exists
}

export interface Conversation {
  id: string;
  accountId: string;
  title: string;
  createdAt: string;   // ISO 8601
  updatedAt: string;   // ISO 8601
  remoteHash: string | null;
  parseWarning?: string;
  messages: ConvMessage[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  lastMessage: string;      // plain text, max 80 chars
  messageCount: number;
  hasMedia: boolean;
  hasFailedData?: boolean;
  imageCount?: number;
  videoCount?: number;
  status?: string;          // normal | lost | hidden | ...
  updatedAt: string;        // ISO 8601
  remoteHash: string | null;
}

export interface Account {
  id: string;
  name: string;
  email: string;
  avatarText: string;
  avatarColor: string;
  conversationCount: number;
  remoteConversationCount: number | null;
  lastSyncAt: string | null;  // ISO 8601
  lastSyncResult: "success" | "partial" | "failed" | null;
  authuser?: string | null;
  listSyncPending?: boolean;
}
