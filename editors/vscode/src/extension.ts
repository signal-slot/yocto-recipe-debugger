// Copyright (C) 2026 Signal Slot Inc.
// SPDX-License-Identifier: MIT

import * as path from "path";
import * as vscode from "vscode";
import { LanguageClient, LanguageClientOptions, ServerOptions } from "vscode-languageclient/node";

const SECTION = "yoctoRecipeDebugger";
let client: LanguageClient | undefined;

function substitute(value: string): string {
  const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? "";
  return value.split("${workspaceFolder}").join(folder);
}

function settings() {
  const config = vscode.workspace.getConfiguration(SECTION);
  return {
    buildDir: substitute(config.get<string>("buildDir", "")),
    initScript: substitute(config.get<string>("initScript", "")),
    commandWrapper: substitute(config.get<string>("commandWrapper", "")),
    python: config.get<string>("python", "python3"),
  };
}

async function start(context: vscode.ExtensionContext): Promise<void> {
  const s = settings();
  // The server and its Python dependencies are bundled in server/.
  const serverDir = context.asAbsolutePath("server");
  const pythonPath = [path.join(serverDir, "src"), path.join(serverDir, "lib")].join(path.delimiter);
  const serverOptions: ServerOptions = {
    command: s.python,
    args: ["-m", "yocto_recipe_debugger"],
    options: { env: { ...process.env, PYTHONPATH: pythonPath } },
  };
  const clientOptions: LanguageClientOptions = {
    documentSelector: [
      { scheme: "file", language: "bitbake" },
      { scheme: "file", pattern: "**/*.{bb,bbappend,inc}" },
    ],
    initializationOptions: {
      buildDir: s.buildDir,
      initScript: s.initScript,
      commandWrapper: s.commandWrapper,
    },
  };
  client = new LanguageClient(SECTION, "Yocto Recipe Debugger", serverOptions, clientOptions);
  await client.start();
}

async function restart(context: vscode.ExtensionContext): Promise<void> {
  if (client) {
    await client.stop();
    client = undefined;
  }
  await start(context);
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  context.subscriptions.push(
    vscode.commands.registerCommand(`${SECTION}.restart`, () => restart(context)),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration(SECTION)) {
        void restart(context);
      }
    }),
  );
  await start(context);
}

export async function deactivate(): Promise<void> {
  await client?.stop();
}
