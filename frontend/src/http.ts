/** Same-origin cookies authenticate REST and SSE without exposing access keys. */
export async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { ...init, credentials: "same-origin" });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof body?.detail === "string" ? body.detail :
      typeof body?.message === "string" ? body.message : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  if (body === null) throw new Error("服务器返回了无效响应 / Invalid server response");
  return body as T;
}
