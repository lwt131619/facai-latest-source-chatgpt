"use strict";

const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const PORT = 8080;
function sourceRoot(candidate) {
  const root = path.resolve(candidate);
  for (const name of ["scripts/start.py", "facai/api.py", "requirements.txt"]) {
    if (!fs.statSync(path.join(root, name), { throwIfNoEntry: false })?.isFile()) {
      throw new Error("本地源码包不完整：缺少 " + name + "。请指定 FACAI_SOURCE_DIR。 ");
    }
  }
  return root;
}

function pythonFor(root) {
  if (process.env.FACAI_DESKTOP_PYTHON) return process.env.FACAI_DESKTOP_PYTHON;
  const local = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  if (fs.existsSync(local)) return local;
  return process.platform === "win32" ? "python" : "python3";
}

function assertFreePort(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", () => reject(new Error("端口 " + port + " 已被占用；请关闭占用程序后重试。")));
    server.listen(port, "127.0.0.1", () => server.close(resolve));
  });
}

function probe(port) {
  return new Promise((resolve) => {
    const request = http.get({ hostname: "127.0.0.1", port, path: "/api/v1/health", timeout: 900 }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        try {
          const data = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          resolve(response.statusCode === 200 && data.auth_required === true);
        } catch (_) { resolve(false); }
      });
    });
    request.once("error", () => resolve(false));
    request.once("timeout", () => { request.destroy(); resolve(false); });
  });
}

async function waitReady(port, child, target, timeoutMs = 180000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.facaiStartError) {
      throw new Error("无法启动本地 Python：" + child.facaiStartError.message);
    }
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error("本地 FACAI 服务启动失败；进程已退出。检查 Python、依赖和本机 8080 端口。");
    }
    if (target() && await probe(port)) return target();
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error("本地 FACAI 服务未在 180 秒内就绪；请检查 Python 环境与启动日志。");
}

async function stopChild(child) {
  if (!child || !child.pid || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { timeout: 10000, windowsHide: true });
    return;
  }
  child.kill("SIGINT");
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    new Promise((resolve) => setTimeout(resolve, 4000)),
  ]);
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
}

async function startLocalService(options = {}) {
  const root = sourceRoot(options.root || process.env.FACAI_SOURCE_DIR || path.resolve(__dirname, "../.."));
  const port = options.port || PORT;
  await assertFreePort(port);
  let target = null;
  const child = spawn(pythonFor(root), [path.join(root, "scripts/start.py"),
    "--port", String(port), "--no-browser", "--desktop-handoff"], {
    cwd: root,
    env: process.env.FACAI_DESKTOP_PYTHON
      ? { ...process.env, FACAI_START_PYTHON_INSTALLED: "1" } : process.env,
    stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
  });
  child.once("error", (error) => { child.facaiStartError = error; });
  let lineBuffer = "";
  child.stdout.on("data", (chunk) => {
    lineBuffer += chunk.toString("utf8");
    if (lineBuffer.length > 8192) lineBuffer = lineBuffer.slice(-8192);
    const lines = lineBuffer.split(/\r?\n/);
    lineBuffer = lines.pop();
    for (const line of lines) {
      if (line.startsWith("FACAI_DESKTOP_TARGET=")) {
        const candidate = line.slice("FACAI_DESKTOP_TARGET=".length);
        if (candidate === `http://127.0.0.1:${port}` ||
            candidate.startsWith(`http://127.0.0.1:${port}/setup#bootstrap=`)) target = candidate;
      }
    }
  });
  // Consume child stderr but never echo it or the bootstrap URL into app logs.
  child.stderr.on("data", () => {});
  try {
    const url = await waitReady(port, child, () => target, options.timeoutMs);
    return { child, url, home: `http://127.0.0.1:${port}`, stop: () => stopChild(child) };
  } catch (error) {
    await stopChild(child);
    throw error;
  }
}

module.exports = { PORT, sourceRoot, assertFreePort, probe, waitReady, stopChild, startLocalService };
