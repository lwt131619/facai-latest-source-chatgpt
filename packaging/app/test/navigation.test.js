"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { isInternal, mayOpenExternal } = require("../navigation");

test("only the configured origin remains inside the application", () => {
  assert.equal(isInternal("https://whstapp.lol/missions/1", "https://whstapp.lol"), true);
  assert.equal(isInternal("https://whstapp.lol.evil.example/", "https://whstapp.lol"), false);
  assert.equal(isInternal("https://whstapp.lol@evil.example/", "https://whstapp.lol"), false);
  assert.equal(isInternal("http://whstapp.lol/", "https://whstapp.lol"), false);
  assert.equal(isInternal("http://127.0.0.1:8080/setup", "http://127.0.0.1:8080"), true);
  assert.equal(isInternal("http://127.0.0.1:9999/", "http://127.0.0.1:8080"), false);
});

test("external opening only accepts browser URLs", () => {
  assert.equal(mayOpenExternal("https://example.com"), true);
  assert.equal(mayOpenExternal("javascript:alert(1)"), false);
  assert.equal(mayOpenExternal("file:///etc/passwd"), false);
});
