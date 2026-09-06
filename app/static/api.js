export async function requestJSON(url, options) {
  const requestOptions = options || {};
  const response = await fetch(url, {
    ...requestOptions,
    headers: { Accept: "application/json", ...requestOptions.headers },
  });

  const contentType = response.headers.get("content-type") || "";
  let payload = null;

  if (contentType.includes("application/json") || contentType.includes("+json")) {
    payload = await response.json();
  } else {
    const text = await response.text();
    payload = text ? { detail: text } : null;
  }

  if (!response.ok) {
    const nestedError = payload && payload.error && typeof payload.error === "object"
      ? payload.error.message
      : null;
    const detail = payload && (
      payload.detail || payload.message || nestedError || payload.error
    );
    const error = new Error(detail || `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    throw error;
  }

  return payload || {};
}

export async function requestArtifact(url, options) {
  const requestOptions = options || {};
  const response = await fetch(url, {
    ...requestOptions,
    headers: {
      Accept: "application/json, text/markdown, text/plain, text/x-diff",
      ...requestOptions.headers,
    },
  });
  const mediaType = response.headers.get("content-type") || "application/octet-stream";
  const text = await response.text();
  let value = text;
  if (mediaType.includes("application/json") || mediaType.includes("+json")) {
    try {
      value = text ? JSON.parse(text) : null;
    } catch (_error) {
      throw new Error("Artifact JSON 无法解析");
    }
  }
  if (!response.ok) {
    const payload = value && typeof value === "object" ? value : {};
    const nested = payload.error && typeof payload.error === "object"
      ? payload.error.message
      : null;
    const error = new Error(
      payload.detail || payload.message || nested || `请求失败（HTTP ${response.status}）`,
    );
    error.status = response.status;
    throw error;
  }
  return {
    mediaType,
    etag: response.headers.get("etag") || "",
    text,
    value,
  };
}

export function sleep(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}
