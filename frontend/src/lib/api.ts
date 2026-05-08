const API_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://127.0.0.1:8000";
const ACCESS_TOKEN_KEY = "access_token";

let refreshPromise: Promise<string | null> | null = null;

export function getStoredToken() {
  return localStorage.getItem(ACCESS_TOKEN_KEY);
}

export function storeToken(token: string) {
  localStorage.setItem(ACCESS_TOKEN_KEY, token);
}

export function clearStoredToken() {
  localStorage.removeItem(ACCESS_TOKEN_KEY);
}

async function getErrorMessage(response: Response) {
  try {
    const data = await response.json();
    if (typeof data?.detail === "string") return data.detail;
    if (typeof data?.detail?.message === "string") return data.detail.message;
    if (typeof data?.message === "string") return data.message;
  } catch {
    // Ignore JSON parsing errors and fall back to status text.
  }
  return response.statusText || `Request failed with status ${response.status}`;
}

export async function refreshAccessToken() {
  if (refreshPromise) return refreshPromise;

  refreshPromise = (async () => {
    const response = await fetch(`${API_URL}/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });

    if (!response.ok) {
      clearStoredToken();
      return null;
    }

    const data = await response.json();
    if (data?.access_token) {
      storeToken(data.access_token);
      return data.access_token as string;
    }

    clearStoredToken();
    return null;
  })();

  try {
    return await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}

export async function apiFetch(
  path: string,
  init: RequestInit = {},
  options: { retryOn401?: boolean } = {}
) {
  const { retryOn401 = true } = options;
  const headers = new Headers(init.headers);
  const hasBody = init.body !== undefined && !(init.body instanceof FormData);

  if (hasBody && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const token = getStoredToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const requestInit: RequestInit = {
    ...init,
    headers,
    credentials: "include",
  };

  const response = await fetch(`${API_URL}${path}`, requestInit);
  if (response.status !== 401 || !retryOn401) {
    return response;
  }

  const refreshedToken = await refreshAccessToken();
  if (!refreshedToken) {
    return response;
  }

  const retryHeaders = new Headers(init.headers);
  if (hasBody && !retryHeaders.has("Content-Type")) {
    retryHeaders.set("Content-Type", "application/json");
  }
  retryHeaders.set("Authorization", `Bearer ${refreshedToken}`);

  return fetch(`${API_URL}${path}`, {
    ...init,
    headers: retryHeaders,
    credentials: "include",
  });
}

export async function apiJson<T>(
  path: string,
  init: RequestInit = {},
  options: { retryOn401?: boolean } = {}
): Promise<T> {
  const response = await apiFetch(path, init, options);
  if (!response.ok) {
    throw new Error(await getErrorMessage(response));
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}
