// Installed desktop starts its bundled local service; source checkout can opt in.
const { app, BrowserWindow, shell, Menu, dialog } = require("electron");
const path = require("path");
const { isInternal, mayOpenExternal } = require("./navigation");
const { startLocalService } = require("./local-runtime");
const { prepareBundledSource } = require("./bundled-source");

const HOME = "https://whstapp.lol";

let localService = null;
let stopping = false;

function createWindow(home = HOME, target = home) {
  const win = new BrowserWindow({
    width: 1380,
    height: 900,
    minWidth: 980,
    minHeight: 640,
    backgroundColor: "#050b16",
    autoHideMenuBar: true,
    title: "FACAI 任务工作台",
    icon: path.join(__dirname, "icon.png"),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });

  win.loadURL(target);

  // 外链一律走系统浏览器，客户端永远留在工作台内
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isInternal(url, home)) win.loadURL(url);
    else if (mayOpenExternal(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    if (!isInternal(url, home)) {
      event.preventDefault();
      if (mayOpenExternal(url)) shell.openExternal(url);
    }
  });
}

Menu.setApplicationMenu(null); // 干净应用窗，无菜单栏

app.whenReady().then(async () => {
  try {
    const mode = process.env.FACAI_DESKTOP_MODE;
    if (mode === "local" || (app.isPackaged && mode !== "remote")) {
      const prepared = app.isPackaged && !process.env.FACAI_SOURCE_DIR
        ? prepareBundledSource(process.resourcesPath, app.getPath("userData")) : null;
      localService = await startLocalService(prepared
        ? { root: prepared.root, dataDir: process.env.FACAI_DATA_DIR || prepared.dataDir }
        : {});
      localService.child.once("exit", () => {
        if (!stopping) {
          dialog.showErrorBox("FACAI 本地服务已停止", "本地服务意外退出；请检查 Python 环境后重新打开 FACAI。");
          app.quit();
        }
      });
      createWindow(localService.home, localService.url);
    } else {
      createWindow();
    }
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        createWindow(localService ? localService.home : HOME);
      }
    });
  } catch (error) {
    dialog.showErrorBox("FACAI 本地服务启动失败", error.message);
    app.quit();
  }
});

app.on("before-quit", (event) => {
  if (!localService || stopping) return;
  event.preventDefault();
  stopping = true;
  localService.stop().finally(() => app.quit());
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
