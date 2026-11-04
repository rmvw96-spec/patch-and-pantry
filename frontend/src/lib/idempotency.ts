/**
 * Returns a random UUID (version 4) to use as an Idempotency-Key.
 *
 * Deliberately avoids crypto.randomUUID(): browsers only provide it in secure contexts (HTTPS or
 * localhost), and the app is also served over plain HTTP on other hostnames, such as
 * http://backend:8000 in CI. crypto.getRandomValues() works everywhere, so one code path runs in
 * every environment.
 */
export function newIdempotencyKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40 // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80 // RFC 4122 variant
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-")
}
