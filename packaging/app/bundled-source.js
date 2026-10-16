"use strict";
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const SKIP = new Set([".venv", "__pycache__", ".facai-requirements"]);
function digestTree(root) {
  const hash = crypto.createHash("sha256");
  function visit(directory) {
    for (const name of fs.readdirSync(directory).sort()) {
      if (SKIP.has(name)) continue;
      const file = path.join(directory, name);
      const info = fs.lstatSync(file);
      if (info.isSymbolicLink()) throw new Error("运行资源包含符号链接：" + name);
      if (info.isDirectory()) visit(file);
      else if (info.isFile()) {
        hash.update(path.relative(root, file).split(path.sep).join("/"));
        hash.update("\0");
        hash.update(fs.readFileSync(file));
      }
    }
  }
  visit(root);
  return hash.digest("hex").slice(0, 20);
}

function checkRoot(root) {
  for (const name of ["scripts/start.py", "facai/api.py", "requirements.txt"]) {
    if (!fs.statSync(path.join(root, name), { throwIfNoEntry: false })?.isFile()) {
      throw new Error("安装包缺少 FACAI 运行文件：" + name);
    }
  }
}

function prepareBundledSource(resourcesPath, userData) {
  const source = path.join(resourcesPath, "runtime-source");
  checkRoot(source);
  const digest = digestTree(source);
  const runtimeParent = path.join(userData, "runtime");
  const destination = path.join(runtimeParent, digest);
  fs.mkdirSync(runtimeParent, { recursive: true, mode: 0o700 });
  const dataDir = path.join(userData, "data");
  fs.mkdirSync(dataDir, { recursive: true, mode: 0o700 });
  if (!fs.existsSync(destination)) {
    const staging = path.join(runtimeParent, ".stage-" + digest + "-" + process.pid);
    try {
      fs.cpSync(source, staging, { recursive: true, errorOnExist: true });
      if (digestTree(staging) !== digest) throw new Error("运行资源复制后哈希不一致");
      fs.renameSync(staging, destination);
    } catch (error) {
      fs.rmSync(staging, { recursive: true, force: true });
      if (!fs.existsSync(destination)) throw error;
    }
  }
  checkRoot(destination);
  if (digestTree(destination) !== digest) {
    throw new Error("本地 FACAI 运行文件已变化，请重新安装应用；用户数据目录不会被删除。");
  }
  return { root: destination, dataDir, digest };
}

module.exports = { digestTree, prepareBundledSource };
