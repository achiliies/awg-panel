/**
 * The one place the SPA talks to the panel.
 *
 * Three things make this less trivial than fetch():
 *
 * 1. The panel is mounted under a secret base path chosen at install time
 *    ("/ab12cd34/"), so no URL may be hardcoded to the server root. Django's
 *    index template injects it as window.__AWG__ and everything is built from
 *    that value.
 * 2. Django's session auth needs the cookie on every request and an
 *    X-CSRFToken header that matches the awgcsrftoken cookie on every mutation.
 * 3. The API answers failures with {detail, errors}. Turning that into one
 *    error type means no caller has to inspect a Response, and a 401 stays
 *    distinguishable so the router can send the user to the login page instead
 *    of reloading the whole app.
 */

import { ApiError, type ApiErrorKind, type FieldErrors } from "./types";

export { ApiError, isApiError } from "./types";

/** Injected by Django's SPA template; index.html defines a dev fallback. */
export interface AwgBootstrap {
  basePath: string;
  version: string;
}

declare global {
  interface Window {
    __AWG__: AwgBootstrap;
  }
}

/** Django's CSRF cookie name, set in awgui/settings.py. */
const CSRF_COOKIE = "awgcsrftoken";

/**
 * Requests that reach a healthy panel answer in milliseconds; anything past
 * this is a service that is down or restarting, and the UI would rather show
 * that than hang. Backup and restore pass a longer timeout of their own.
 */
const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * Dispatched on window whenever a request comes back 401. The shell listens for
 * it so a session that expired in a background poll routes to the login page
 * without a full page load.
 */
export const UNAUTHORIZED_EVENT = "awg:unauthorized";

type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

const MUTATING: ReadonlySet<string> = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export interface RequestOptions {
  /** Caller's cancellation, combined with the timeout below. */
  signal?: AbortSignal;
  /** 0 disables the timeout entirely (long uploads). */
  timeoutMs?: number;
  headers?: Record<string, string>;
}

function normalizeBasePath(value: unknown): string {
  const raw = typeof value === "string" ? value.trim() : "";
  if (raw === "" || raw === "/") {
    return "/";
  }
  const withLead = raw.startsWith("/") ? raw : `/${raw}`;
  return withLead.endsWith("/") ? withLead : `${withLead}/`;
}

function readBootstrap(): AwgBootstrap {
  const injected = typeof window === "undefined" ? undefined : window.__AWG__;
  const version = typeof injected?.version === "string" ? injected.version.trim() : "";
  return {
    basePath: normalizeBasePath(injected?.basePath),
    // A missing bootstrap means someone opened index.html directly; the app
    // still has to render rather than crash on a blank page.
    version: version || "dev",
  };
}

/** basePath and version as injected by the server. */
export const bootstrap: AwgBootstrap = readBootstrap();

/** Absolute path every endpoint hangs off, e.g. "/ab12cd34/api/v1/". */
export const apiBase = `${bootstrap.basePath}api/v1/`;

/** Read one cookie. Returns null when it is absent or the document is not available. */
export function getCookie(name: string): string | null {
  if (typeof document === "undefined") {
    return null;
  }
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const item = part.trim();
    if (item.startsWith(prefix)) {
      return decodeURIComponent(item.slice(prefix.length));
    }
  }
  return null;
}

/** The CSRF token Django expects back in the X-CSRFToken header. */
export function csrfToken(): string | null {
  return getCookie(CSRF_COOKIE);
}

function stripLeadingSlash(path: string): string {
  return path.startsWith("/") ? path.slice(1) : path;
}

function resolve(path: string): string {
  return `${apiBase}${stripLeadingSlash(path)}`;
}

/**
 * Same-origin URL for anything the browser fetches on its own: a client config
 * download, the QR png, the export zip, the backup archive. Same base path
 * rules as every other request, so these keep working under the secret prefix.
 */
export function blobUrl(path: string): string {
  const target = resolve(path);
  if (typeof window === "undefined") {
    return target;
  }
  return new URL(target, window.location.origin).toString();
}

function statusFallback(status: number, statusText: string): string {
  // Only used when the server did not send our error shape, which in practice
  // means a proxy or a crash page answered instead of Django.
  switch (status) {
    case 401:
      return "Your session has expired. Sign in again.";
    case 403:
      return "The panel refused this request. Reload the page and try again.";
    case 404:
      return "That item no longer exists.";
    case 409:
      return "That name is already taken.";
    case 502:
      return "The AmneziaWG tools reported an error. Check the server logs.";
    case 503:
      return "The configuration is locked by another change. Try again in a moment.";
    default:
      return statusText ? `${status} ${statusText}` : `Request failed with status ${status}`;
  }
}

/** DRF sends either a string or a list of strings per field; the UI wants one line. */
function flattenFieldErrors(raw: unknown): FieldErrors {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return {};
  }
  const out: Record<string, string> = {};
  for (const [field, value] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof value === "string") {
      out[field] = value;
    } else if (Array.isArray(value)) {
      out[field] = value.filter((item) => typeof item === "string").join(" ");
    } else if (value !== null && value !== undefined) {
      out[field] = String(value);
    }
  }
  return out;
}

async function errorFromResponse(response: Response, path: string): Promise<ApiError> {
  let detail = "";
  let code = "";
  let errors: FieldErrors = {};

  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("json")) {
    try {
      const body: unknown = await response.json();
      if (typeof body === "object" && body !== null) {
        const record = body as Record<string, unknown>;
        if (typeof record.detail === "string") {
          detail = record.detail;
        }
        if (typeof record.code === "string") {
          code = record.code;
        }
        errors = flattenFieldErrors(record.errors);
        if (Object.keys(errors).length === 0) {
          // DRF's own serializer errors arrive at the top level rather than
          // under "errors"; treat them the same so fields still light up.
          //
          // Every key the panel's own envelope defines is dropped first, or
          // this reads them as fields: the API sends "errors" on every failure
          // and an empty one would arrive here as a field called "errors" whose
          // message is the string an object stringifies to.
          const { detail: _detail, errors: _errors, code: _code, ...rest } = record;
          errors = flattenFieldErrors(rest);
        }
        if (!detail) {
          const first = Object.values(errors)[0];
          detail = first ?? "";
        }
      }
    } catch {
      // Truncated or malformed body: the status is still meaningful.
    }
  }

  return new ApiError({
    status: response.status,
    detail: detail || statusFallback(response.status, response.statusText),
    errors,
    path,
    code,
    kind: "http",
  });
}

function transportError(cause: unknown, path: string): ApiError {
  const aborted =
    cause instanceof DOMException && (cause.name === "AbortError" || cause.name === "TimeoutError");
  const kind: ApiErrorKind = aborted ? "timeout" : "network";
  return new ApiError({
    status: 0,
    kind,
    path,
    detail: aborted
      ? "The panel did not answer in time. It may be restarting."
      : "Cannot reach the panel. Check your connection and whether awg-panel-web is running.",
  });
}

async function parseBody<T>(response: Response, path: string): Promise<T> {
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined as T;
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("json")) {
    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiError({
        status: response.status,
        kind: "parse",
        path,
        detail: "The panel sent a response this version cannot read. Reload the page.",
      });
    }
  }
  // Client configs come back as text/plain; hand them over unchanged.
  return (await response.text()) as unknown as T;
}

async function request<T>(
  method: HttpMethod,
  path: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;

  if (body !== undefined && !isForm) {
    headers["Content-Type"] = "application/json";
  }
  if (MUTATING.has(method)) {
    const token = csrfToken();
    if (token) {
      headers["X-CSRFToken"] = token;
    }
  }

  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const timer =
    timeoutMs > 0
      ? setTimeout(
          () => controller.abort(new DOMException("Request timed out", "TimeoutError")),
          timeoutMs,
        )
      : undefined;
  const forward = (): void => controller.abort(options.signal?.reason);
  options.signal?.addEventListener("abort", forward, { once: true });

  let response: Response;
  try {
    response = await fetch(resolve(path), {
      method,
      headers,
      // Session and CSRF cookies only; the panel is never cross-origin.
      credentials: "same-origin",
      cache: "no-store",
      redirect: "follow",
      signal: controller.signal,
      body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
    });
  } catch (cause) {
    throw transportError(cause, path);
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
    options.signal?.removeEventListener("abort", forward);
  }

  if (!response.ok) {
    const error = await errorFromResponse(response, path);
    if (error.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT, { detail: { path } }));
    }
    throw error;
  }

  return parseBody<T>(response, path);
}

/** Filename the server suggested, so a download keeps its real name. */
function filenameFrom(response: Response, fallback: string): string {
  const header = response.headers.get("content-disposition") ?? "";
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (utf8?.[1]) {
    try {
      return decodeURIComponent(utf8[1]);
    } catch {
      // Malformed encoding: fall through to the plain form.
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain?.[1]?.trim() || fallback;
}

/**
 * Fetch a binary payload through the same auth and error handling as everything
 * else. An <a download> would send the user to a raw 500 page instead of an
 * error message, which is why backups do not use one.
 */
async function blob(
  path: string,
  fallbackName = "download",
  options: RequestOptions = {},
): Promise<{ blob: Blob; filename: string }> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? 0;
  const timer =
    timeoutMs > 0
      ? setTimeout(
          () => controller.abort(new DOMException("Request timed out", "TimeoutError")),
          timeoutMs,
        )
      : undefined;
  const forward = (): void => controller.abort(options.signal?.reason);
  options.signal?.addEventListener("abort", forward, { once: true });

  let response: Response;
  try {
    response = await fetch(resolve(path), {
      method: "GET",
      headers: { ...options.headers },
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
  } catch (cause) {
    throw transportError(cause, path);
  } finally {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
    options.signal?.removeEventListener("abort", forward);
  }

  if (!response.ok) {
    const error = await errorFromResponse(response, path);
    if (error.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT, { detail: { path } }));
    }
    throw error;
  }

  return { blob: await response.blob(), filename: filenameFrom(response, fallbackName) };
}

/** Save a binary payload to disk. Returns what was saved, for a toast. */
async function download(
  path: string,
  fallbackName = "download",
  options: RequestOptions = {},
): Promise<{ filename: string; size: number }> {
  const result = await blob(path, fallbackName, options);
  const href = URL.createObjectURL(result.blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = result.filename;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    // Revoking immediately cancels the download in Safari; one tick is enough.
    setTimeout(() => URL.revokeObjectURL(href), 1000);
  }
  return { filename: result.filename, size: result.blob.size };
}

export const api = {
  get: <T>(path: string, options?: RequestOptions): Promise<T> =>
    request<T>("GET", path, undefined, options),
  post: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>("POST", path, body, options),
  put: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>("PUT", path, body, options),
  del: <T>(path: string, options?: RequestOptions): Promise<T> =>
    request<T>("DELETE", path, undefined, options),
  blob,
  download,
  blobUrl,
} as const;
