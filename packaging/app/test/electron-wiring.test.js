"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const Module = require("node:module");
const path = require("node:path");

async function loadMain(mode, packaged = false) {
  const events = {};
  const windows = [];
  const dialogs = [];
  const external = [];
  let stopCount = 0;
  let runtimeOptions;
  let preparedWith;
  const child = new EventEmitter();
  const service = { home: "http://127.0.0.1:8080",
    url: "http://127.0.0.1:8080/setup#bootstrap=test",
    child, stop: async () => { stopCount++; } };
  const fakeElectron = {
    app: { isPackaged: packaged, getPath: () => "/test/user-data",
      whenReady: () => Promise.resolve(), on: (name, callback) => { events[name] = callback; },
      quit: () => { if (events['before-quit']) events['before-quit']({ preventDefault() {} }); } },
    BrowserWindow: class {
      constructor() {
        this.loaded = [];
        this.webContents = new EventEmitter();
        this.webContents.setWindowOpenHandler = (handler) => { this.openHandler = handler; };
        windows.push(this);
      }
      loadURL(url) { this.loaded.push(url); }
      static getAllWindows() { return windows; }
    },
    Menu: { setApplicationMenu() {} },
    shell: { openExternal: (url) => external.push(url) },
    dialog: { showErrorBox: (title, message) => dialogs.push([title, message]) },
  };
  const originalLoad = Module._load;
  const originalMode = process.env.FACAI_DESKTOP_MODE;
  process.env.FACAI_DESKTOP_MODE = mode;
  Module._load = function(request, parent, isMain) {
    if (request === "electron") return fakeElectron;
    if (request === "./local-runtime" && parent?.filename.endsWith(path.join('packaging', 'app', 'main.js'))) {
      return { startLocalService: async (options) => { runtimeOptions = options; return service; } };
    }
    if (request === "./bundled-source" && parent?.filename.endsWith(path.join('packaging', 'app', 'main.js'))) {
      return { prepareBundledSource: (...args) => {
        preparedWith = args;
        return { root: "/test/runtime", dataDir: "/test/user-data/data" };
      } };
    }
    return originalLoad.apply(this, arguments);
  };
  const filename = require.resolve('../main');
  delete require.cache[filename];
  try {
    require('../main');
    await new Promise(setImmediate);
  } finally {
    Module._load = originalLoad;
    if (originalMode === undefined) delete process.env.FACAI_DESKTOP_MODE;
    else process.env.FACAI_DESKTOP_MODE = originalMode;
  }
  return { events, windows, dialogs, external, child,
    stopped: () => stopCount, runtimeOptions, preparedWith };
}

test("Electron local mode loads one-time setup in its own window and stops child", async () => {
  const state = await loadMain("local");
  assert.deepEqual(state.windows[0].loaded, ["http://127.0.0.1:8080/setup#bootstrap=test"]);
  const fakeEvent = { prevented: false, preventDefault() { this.prevented = true; } };
  state.events['before-quit'](fakeEvent);
  await new Promise(setImmediate);
  assert.equal(fakeEvent.prevented, true);
  assert.equal(state.stopped(), 1);
  assert.deepEqual(state.dialogs, []);
});

test("Electron remote mode keeps the existing site and rejects lookalike hosts", async () => {
  const state = await loadMain("remote");
  assert.deepEqual(state.windows[0].loaded, ["https://whstapp.lol"]);
  state.windows[0].openHandler({ url: "https://whstapp.lol.evil.example" });
  assert.deepEqual(state.external, ["https://whstapp.lol.evil.example"]);
  state.windows[0].openHandler({ url: "javascript:alert(1)" });
  assert.equal(state.external.length, 1);
});

test("installed Electron starts bundled local service with separate persistent data", async () => {
  const state = await loadMain(undefined, true);
  assert.deepEqual(state.preparedWith, [process.resourcesPath, "/test/user-data"]);
  assert.deepEqual(state.runtimeOptions, {
    root: "/test/runtime", dataDir: "/test/user-data/data",
  });
  assert.deepEqual(state.windows[0].loaded, ["http://127.0.0.1:8080/setup#bootstrap=test"]);
});
