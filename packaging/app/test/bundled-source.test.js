"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { prepareBundledSource } = require("../bundled-source");
const { startLocalService, assertFreePort } = require("../local-runtime");

const repo = path.resolve(__dirname, "../../..");
function freePort() {
  return new Promise((resolve) => {
    const listener = net.createServer();
    listener.listen(0, "127.0.0.1", () => {
      const port = listener.address().port;
      listener.close(() => resolve(port));
    });
  });
}

function makeResources(parent) {
  const source = path.join(parent, "resources", "runtime-source");
  fs.mkdirSync(path.join(source, "facai"), { recursive: true });
  fs.mkdirSync(path.join(source, "scripts"), { recursive: true });
  fs.writeFileSync(path.join(source, "facai", "api.py"), "VERSION=1\n");
  fs.writeFileSync(path.join(source, "scripts", "start.py"), "print('start')\n");
  fs.writeFileSync(path.join(source, "requirements.txt"), "fastapi\n");
  return path.join(parent, "resources");
}

test("bundled runtime copies versioned source while preserving separate user data", () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "facai-resources-"));
  try {
    const resources = makeResources(parent);
    const userData = path.join(parent, "user");
    const first = prepareBundledSource(resources, userData);
    assert.equal(fs.readFileSync(path.join(first.root, "facai", "api.py"), "utf8"), "VERSION=1\n");
    assert.equal(first.dataDir, path.join(userData, "data"));
    fs.writeFileSync(path.join(first.dataDir, "facai.sqlite3"), "persistent-data");
    fs.mkdirSync(path.join(first.root, ".venv"));
    fs.writeFileSync(path.join(first.root, ".venv", "marker"), "installed");
    assert.equal(prepareBundledSource(resources, userData).root, first.root);

    fs.writeFileSync(path.join(resources, "runtime-source", "facai", "api.py"), "VERSION=2\n");
    const second = prepareBundledSource(resources, userData);
    assert.notEqual(second.root, first.root);
    assert.equal(second.dataDir, first.dataDir);
    assert.equal(fs.readFileSync(path.join(second.dataDir, "facai.sqlite3"), "utf8"), "persistent-data");
    assert.equal(fs.readFileSync(path.join(first.root, "facai", "api.py"), "utf8"), "VERSION=1\n");

    fs.writeFileSync(path.join(second.root, "facai", "api.py"), "tampered\n");
    assert.throws(() => prepareBundledSource(resources, userData), /已变化/);
    assert.equal(fs.readFileSync(path.join(second.dataDir, "facai.sqlite3"), "utf8"), "persistent-data");
  } finally {
    fs.rmSync(parent, { recursive: true, force: true });
  }
});

test("missing packaged runtime files fail without creating user data", () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "facai-resources-"));
  try {
    assert.throws(() => prepareBundledSource(parent, path.join(parent, "user")), /缺少 FACAI/);
    assert.equal(fs.existsSync(path.join(parent, "user")), false);
  } finally { fs.rmSync(parent, { recursive: true, force: true }); }
});

test("bundled source starts real FACAI and keeps Owner data outside versioned code", async () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "facai-bundled-live-"));
  const source = path.join(parent, "resources", "runtime-source");
  fs.mkdirSync(path.join(source, "scripts"), { recursive: true });
  fs.cpSync(path.join(repo, "facai"), path.join(source, "facai"), { recursive: true });
  fs.copyFileSync(path.join(repo, "scripts", "start.py"), path.join(source, "scripts", "start.py"));
  fs.copyFileSync(path.join(repo, "requirements.txt"), path.join(source, "requirements.txt"));
  const python = process.env.FACAI_DESKTOP_PYTHON || path.join(repo, ".venv", "bin", "python");
  const priorPython = process.env.FACAI_DESKTOP_PYTHON;
  const priorScheduler = process.env.FACAI_SCHEDULER_ENABLED;
  process.env.FACAI_DESKTOP_PYTHON = python;
  process.env.FACAI_SCHEDULER_ENABLED = "0";
  let service;
  try {
    const prepared = prepareBundledSource(path.join(parent, "resources"), path.join(parent, "user"));
    const port = await freePort();
    service = await startLocalService({ ...prepared, port, timeoutMs: 60000 });
    assert.match(service.url, /\/setup#bootstrap=/);
    assert.equal((await (await fetch(`${service.home}/api/v1/health`)).json()).auth_required, true);
    assert.equal(fs.existsSync(path.join(prepared.dataDir, "local-owner.json")), true);
    assert.equal(fs.existsSync(path.join(prepared.root, "data")), false);
    await service.stop();
    await assertFreePort(port);
    assert.equal(prepareBundledSource(path.join(parent, "resources"), path.join(parent, "user")).root, prepared.root);
  } finally {
    if (service) await service.stop();
    if (priorPython === undefined) delete process.env.FACAI_DESKTOP_PYTHON;
    else process.env.FACAI_DESKTOP_PYTHON = priorPython;
    if (priorScheduler === undefined) delete process.env.FACAI_SCHEDULER_ENABLED;
    else process.env.FACAI_SCHEDULER_ENABLED = priorScheduler;
    fs.rmSync(parent, { recursive: true, force: true });
  }
});
