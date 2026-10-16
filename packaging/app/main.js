// FACAI 桌面客户端：Electron 壳加载 https://whstapp.lol
// 打包：cd packaging/app && npm install && npm run dist
const { app, BrowserWindow, shell, Menu } = require("electron");
const path = require("path");
const { isInternal, mayOpenExternal } = require("./navigation");

const HOME = "https://whstapp.lol";

function createWindow() {
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

  win.loadURL(HOME);

  // 外链一律走系统浏览器，客户端永远留在工作台内
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!isInternal(url, HOME) && mayOpenExternal(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    if (!isInternal(url, HOME)) {
      event.preventDefault();
      if (mayOpenExternal(url)) shell.openExternal(url);
    }
  });
}

Menu.setApplicationMenu(null); // 干净应用窗，无菜单栏

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
