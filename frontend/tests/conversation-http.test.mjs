import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { createRequire } from "node:module";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { buildSync } from "esbuild";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { createRequestId } from "../src/requestId.ts";

const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function withGlobals(values, run) {
  const original = new Map(Object.keys(values).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  try {
    for (const [key, value] of Object.entries(values)) Object.defineProperty(globalThis, key, { configurable: true, value });
    return run();
  } finally {
    for (const [key, descriptor] of original) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
}

test("HTTP fallback produces distinct cryptographic UUID v4 send receipts", () => {
  let calls = 0;
  withGlobals({ crypto: { getRandomValues(bytes) { calls++; return webcrypto.getRandomValues(bytes); } } }, () => {
    const receipts = Array.from({ length: 256 }, createRequestId);
    assert.equal(calls, receipts.length);
    assert.equal(new Set(receipts).size, receipts.length);
    for (const receipt of receipts) assert.match(receipt, uuidV4);
  });
});

test("secure contexts continue to use native UUID generation", () => {
  const receipt = webcrypto.randomUUID();
  withGlobals({ crypto: { randomUUID() { return receipt; }, getRandomValues() { assert.fail("Native UUID should be used"); } } }, () => {
    assert.equal(createRequestId(), receipt);
  });
});

const bundle = buildSync({
  entryPoints: [fileURLToPath(new URL("../src/components/ConversationsPage.tsx", import.meta.url))],
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
  jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
  define: { "import.meta.env.VITE_API_BASE_URL": '"/api"' },
});
const componentModule = { exports: {} };
new Function("module", "exports", "require", bundle.outputFiles[0].text)(componentModule, componentModule.exports, createRequire(import.meta.url));
const { ConversationsPage } = componentModule.exports;

test("opening an individual conversation on HTTP renders with no saved draft", () => {
  withGlobals({
    crypto: { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) },
    window: { location: { hash: "#conversations/42" } },
    sessionStorage: { getItem: () => null },
  }, () => {
    const markup = renderToString(createElement(ConversationsPage, { onChanged() {} }));
    assert.match(markup, /Письма компании/);
    assert.match(markup, /Загружаем письма/);
  });
});

test("opening an existing pending draft retains its send receipt without generating a new UUID", () => {
  const draft = { body: "Сохранённый ответ", replyId: 7, requestId: webcrypto.randomUUID(), pending: true };
  withGlobals({
    crypto: { getRandomValues() { assert.fail("An existing send receipt must be preserved"); } },
    window: { location: { hash: "#conversations/42" } },
    sessionStorage: { getItem(key) { assert.equal(key, "fuellead.reply-draft.42"); return JSON.stringify(draft); } },
  }, () => {
    assert.match(renderToString(createElement(ConversationsPage, { onChanged() {} })), /Письма компании/);
  });
});
