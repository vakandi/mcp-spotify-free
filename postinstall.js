#!/usr/bin/env node
"use strict";

/** One-time setup for the npx/npm install: make sure Python deps are there. */

const { execSync } = require("child_process");
const path = require("path");
const fs = require("fs");

const NAME = "mcp-spotify-free";
const ROOT = __dirname;

function log(msg) {
  console.log(`\x1b[36m[${NAME}]\x1b[0m ${msg}`);
}

function warn(msg) {
  console.warn(`\x1b[33m[${NAME}]\x1b[0m ${msg}`);
}

function findPython() {
  for (const cmd of ["python3", "python"]) {
    try {
      const out = execSync(`${cmd} -c "import sys; print(sys.version_info[0], sys.version_info[1])"`, {
        encoding: "utf-8",
        timeout: 5000,
        stdio: ["pipe", "pipe", "pipe"],
      }).trim();
      const [major, minor] = out.split(/\s+/).map(Number);
      if (major >= 3 && minor >= 10) return cmd;
    } catch {
      continue;
    }
  }
  return null;
}

const python = findPython();
if (!python) {
  warn("Python 3.10+ not found - install it, then run: npx " + NAME + " login");
} else {
  let ready = true;
  try {
    execSync(`${python} -c "import fastmcp, requests, cryptography"`, { stdio: "ignore", timeout: 8000 });
  } catch {
    try {
      execSync(
        `${python} -m pip install --quiet --no-warn-script-location --disable-pip-version-check "fastmcp>=2.0" "requests>=2.31" "cryptography>=41.0"`,
        { stdio: "inherit", timeout: 240000 }
      );
      log("Python dependencies installed ✓");
    } catch {
      ready = false;
      warn("Could not install Python dependencies - run: pip install fastmcp requests cryptography");
    }
  }
  if (ready) {
    log("Python dependencies OK ✓");
    log(`Next: npx ${NAME} login   (captures your Spotify session)`);
  }
}
