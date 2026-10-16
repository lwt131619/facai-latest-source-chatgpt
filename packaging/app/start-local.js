"use strict";
const { spawn } = require("node:child_process");
const electron = require("electron");
const child = spawn(electron, ["."], {
  cwd: __dirname, stdio: "inherit",
  env: { ...process.env, FACAI_DESKTOP_MODE: "local" },
});
child.on("exit", (code) => { process.exitCode = code || 0; });
child.on("error", (error) => { console.error(error.message); process.exitCode = 1; });
