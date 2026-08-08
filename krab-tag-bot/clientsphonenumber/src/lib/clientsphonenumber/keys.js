import { randomBytes } from "crypto";

function base64Url(buffer) {
  return buffer
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

export function generateSecretKey(bytes = 16) {
  return base64Url(randomBytes(bytes));
}

export function generateMetadataKey(bytes = 16) {
  return base64Url(randomBytes(bytes));
}

