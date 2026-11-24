/** MaiSwitch 看板前端 HTTP API 轻量客户端。 */

const TOKEN_STORAGE_KEY = "maiswitch_auth_token";

export function getStoredToken(): string {
  return localStorage.getItem(TOKEN_STORAGE_KEY) || "";
}

export function setStoredToken(token: string): void {
  localStorage.setItem(TOKEN_STORAGE_KEY, token.trim());
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers || {});
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  const token = getStoredToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    let detail = `请求失败 (${res.status})`;
    try {
      const data = (await res.json()) as { detail?: string };
      if (data.detail) detail = data.detail;
    } catch {
      // 忽略非 JSON 错误体
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export function formatMs(ts?: number | null): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return d.toLocaleString("zh-CN", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
