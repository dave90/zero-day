import type { Health } from "./types";
export const BASE = "/api/v1";
let csrf = "";
let boot: Promise<Health> | null = null;
export async function health(): Promise<Health> {
  if (!boot)
    boot = fetch(BASE + "/health", { credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) throw new Error("Local service is unavailable");
        const h = await r.json();
        csrf = h.csrf_token;
        return h;
      })
      .catch((e) => {
        boot = null;
        throw e;
      });
  return boot;
}
export async function api<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  if (method !== "GET") await health();
  const response = await fetch(BASE + path, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(method !== "GET" ? { "X-Zero-CSRF": csrf } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const data = await response
      .json()
      .catch(() => ({ error: `Request failed (${response.status})` }));
    if (response.status === 403) {
      boot = null;
      csrf = "";
    }
    throw new Error(data.error || "Request failed");
  }
  return response.json();
}
export const key = () => crypto.randomUUID();
export const action = <T = Record<string, unknown>>(
  sid: string,
  name: string,
  body: Record<string, unknown> = {},
) =>
  api<T>(`/sessions/${sid}/${name}`, "POST", {
    ...body,
    idempotency_key: key(),
  });
export const resource = (eid: string, name: string, sid?: string) =>
  `/engagements/${eid}/${name}${sid ? "?session_id=" + sid : ""}`;
export function download(eid: string, aid: string, sid?: string) {
  return (
    BASE +
    resource(eid, `artifacts/${aid}`, sid) +
    (sid ? "&" : "?") +
    "download=1"
  );
}

export async function uploadFile(eid: string, file: File) {
  if (file.size > 1_000_000) throw new Error(`${file.name} exceeds 1 MB`);
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 16384) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 16384));
  }
  return api(`/engagements/${eid}/files`, "POST", {
    name: file.webkitRelativePath || file.name,
    data_base64: btoa(binary),
  });
}
