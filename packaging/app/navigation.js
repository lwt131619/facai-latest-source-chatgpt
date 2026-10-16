"use strict";

function isInternal(target, home) {
  try {
    const url = new URL(target);
    const expected = new URL(home);
    return (url.protocol === "https:" ||
            (url.protocol === "http:" && url.hostname === "127.0.0.1")) &&
           url.origin === expected.origin;
  } catch (_) {
    return false;
  }
}

function mayOpenExternal(target) {
  try {
    const url = new URL(target);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch (_) {
    return false;
  }
}

module.exports = { isInternal, mayOpenExternal };
