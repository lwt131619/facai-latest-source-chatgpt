"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const runtime = require("../local-runtime");

const root = path.resolve(__dirname, "../../..");
function freePort() {
  return new Promise((resolve) => {
    const listener = net.createServer();
    listener.listen(0, "127.0.0.1", () => {
      const port = listener.address().port;
      listener.close(() => resolve(port));
    });
  });
}

test("source discovery fails clearly when the checkout is incomplete", () => {
  assert.throws(() => runtime.sourceRoot(os.tmpdir()), /本地源码包不完整/);
  assert.equal(runtime.sourceRoot(root), root);
});

test("a busy port is rejected before launching the child", async () => {
  const listener = net.createServer();
  await new Promise((resolve) => listener.listen(0, "127.0.0.1", resolve));
  const port = listener.address().port;
  try {
    await assert.rejects(runtime.assertFreePort(port), /已被占用/);
  } finally {
    listener.close();
  }
});

test("missing Python fails quickly and leaves the port free", async () => {
  const port = await freePort();
  const previous = process.env.FACAI_DESKTOP_PYTHON;
  process.env.FACAI_DESKTOP_PYTHON = path.join(os.tmpdir(), "facai-python-missing");
  try {
    await assert.rejects(runtime.startLocalService({ root, port, timeoutMs: 2000 }), /无法启动本地 Python/);
    await runtime.assertFreePort(port);
  } finally {
    if (previous === undefined) delete process.env.FACAI_DESKTOP_PYTHON;
    else process.env.FACAI_DESKTOP_PYTHON = previous;
  }
});

test("local desktop starts real FACAI, hands off Owner setup, then stops it", async () => {
  const data = fs.mkdtempSync(path.join(os.tmpdir(), "facai-desktop-test-"));
  const port = await freePort();
  const previous = process.env.FACAI_DATA_DIR;
  const previousScheduler = process.env.FACAI_SCHEDULER_ENABLED;
  process.env.FACAI_DATA_DIR = data;
  process.env.FACAI_SCHEDULER_ENABLED = "0";
  let service;
  try {
    service = await runtime.startLocalService({ root, port, timeoutMs: 60000 });
    assert.equal(service.home, `http://127.0.0.1:${port}`);
    assert.match(service.url, new RegExp(`^http://127\\.0\\.0\\.1:${port}/setup#bootstrap=`));
    const health = await fetch(`${service.home}/api/v1/health`);
    assert.equal(health.status, 200);
    assert.equal((await health.json()).auth_required, true);
    const setup = await fetch(`${service.home}/setup`);
    assert.equal(setup.status, 200);
    await service.stop();
    assert.equal(service.child.exitCode !== null || service.child.signalCode !== null, true);
    await runtime.assertFreePort(port);
  } finally {
    if (service) await service.stop();
    if (previous === undefined) delete process.env.FACAI_DATA_DIR;
    else process.env.FACAI_DATA_DIR = previous;
    if (previousScheduler === undefined) delete process.env.FACAI_SCHEDULER_ENABLED;
    else process.env.FACAI_SCHEDULER_ENABLED = previousScheduler;
    fs.rmSync(data, { recursive: true, force: true });
  }
});
