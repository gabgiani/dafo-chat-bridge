import * as vscode from 'vscode';
import { execFile } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

type Source = 'copilot' | 'claude' | 'codex';
type Target = Source | 'md';

interface SessionMeta {
  source: Source;
  id: string;
  title: string;
  cwd: string | null;
  updated: number;
  size: number;
  path: string;
}

interface ExportResult {
  target: Target;
  title: string;
  cwd: string;
  turns: number;
  id: string | null;
  path: string | null;
}

const PARTICIPANT = 'chatBridge.bridge';
const LABEL: Record<Target, string> = { copilot: 'Copilot', claude: 'Claude Code', codex: 'Codex', md: 'Markdown' };
const ICON: Record<Source, string> = { copilot: 'copilot', claude: 'sparkle', codex: 'terminal' };
const SOURCES: Source[] = ['copilot', 'claude', 'codex'];

let extensionContext: vscode.ExtensionContext;

function config() {
  return vscode.workspace.getConfiguration('chatBridge');
}

function workspaceFolder(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function cliEnv(): NodeJS.ProcessEnv {
  // globalStorageUri = <VS Code User dir>/globalStorage/<extension id>
  const userDir = path.dirname(path.dirname(extensionContext.globalStorageUri.fsPath));
  return { ...process.env, CHAT_BRIDGE_VSCODE_USER_DIR: userDir };
}

function pythonCommand(): string {
  const configured = config().get<string>('python', '').trim();
  return configured || (process.platform === 'win32' ? 'python' : 'python3');
}

class CliError extends Error {
  constructor(message: string, readonly exitCode: number | null, readonly stdout: string) {
    super(message);
  }
}

function runCli(args: string[], token?: vscode.CancellationToken): Promise<string> {
  const script = extensionContext.asAbsolutePath(path.join('python', 'chat-bridge'));
  return new Promise((resolve, reject) => {
    const child = execFile(pythonCommand(), [script, ...args], { env: cliEnv(), maxBuffer: 512 * 1024 * 1024 },
      (error, stdout, stderr) => {
        if (error) {
          const code = typeof error.code === 'number' ? error.code : null;
          reject(new CliError((stderr || error.message).trim(), code, stdout));
        } else {
          resolve(stdout);
        }
      });
    token?.onCancellationRequested(() => child.kill());
  });
}

function exportArgs(source: Source, id: string, target: Target, skipParticipant?: string): string[] {
  const args = ['export', source, id, '--to', target, '--json', '--tool-output', config().get<string>('toolOutput', '500')];
  const last = config().get<number>('lastTurns', 0);
  if (last > 0) {
    args.push('--last', String(last));
  }
  const folder = workspaceFolder();
  if (config().get<string>('targetCwd', 'workspace') === 'workspace' && folder) {
    args.push('--cwd', folder);
  }
  if (skipParticipant) {
    args.push('--skip-participant', skipParticipant);
  }
  return args;
}

async function exportSession(source: Source, id: string, target: Target, skipParticipant?: string,
  token?: vscode.CancellationToken): Promise<ExportResult> {
  return JSON.parse(await runCli(exportArgs(source, id, target, skipParticipant), token)) as ExportResult;
}

async function openResult(result: ExportResult): Promise<void> {
  switch (result.target) {
    case 'claude':
      await vscode.commands.executeCommand('claude-vscode.editor.open', result.id);
      break;
    case 'codex':
      await vscode.env.openExternal(vscode.Uri.parse(`${vscode.env.uriScheme}://openai.chatgpt/local/${result.id}`));
      break;
    case 'copilot':
      await vscode.commands.executeCommand('workbench.action.chat.import',
        { inputPath: vscode.Uri.file(result.path!), target: 'chatViewPane' });
      break;
    case 'md':
      await vscode.window.showTextDocument(vscode.Uri.file(result.path!));
      break;
  }
}

function relativeTime(ms: number): string {
  const minutes = Math.round((Date.now() - ms) / 60000);
  if (minutes < 60) {
    return `hace ${minutes} min`;
  }
  if (minutes < 60 * 24) {
    return `hace ${Math.round(minutes / 60)} h`;
  }
  return `hace ${Math.round(minutes / 1440)} d`;
}

function inWorkspace(cwd: string | null): boolean {
  const folders = (vscode.workspace.workspaceFolders ?? []).map(f => f.uri.fsPath);
  if (!cwd) {
    return false;
  }
  if (folders.some(f => cwd === f || cwd.startsWith(f + path.sep))) {
    return true;
  }
  // project moved: the recorded directory no longer exists but has the name of a workspace folder
  return !fs.existsSync(cwd) && folders.some(f => path.basename(f) === path.basename(cwd));
}

class GroupItem extends vscode.TreeItem {
  constructor(readonly source: Source, readonly sessions: SessionMeta[]) {
    super(`${LABEL[source]} (${sessions.length})`,
      sessions.length ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
    this.iconPath = new vscode.ThemeIcon(ICON[source]);
    this.contextValue = 'group';
  }
}

class SessionItem extends vscode.TreeItem {
  constructor(readonly session: SessionMeta) {
    super(session.title, vscode.TreeItemCollapsibleState.None);
    this.description = `${relativeTime(session.updated)} · ${(session.size / 1e6).toFixed(1)} MB`;
    const moved = session.cwd && !fs.existsSync(session.cwd) ? '\n\n_Ruta original (ya no existe):_' : '';
    this.tooltip = new vscode.MarkdownString(
      `**${session.title}**\n\n${LABEL[session.source]} \`${session.id}\`${moved}\n\n${session.cwd ?? ''}`);
    this.iconPath = new vscode.ThemeIcon(ICON[session.source]);
    this.contextValue = `session-${session.source}`;
    this.command = { command: 'chatBridge.showMarkdown', title: 'Ver como Markdown', arguments: [this] };
  }
}

class SessionsProvider implements vscode.TreeDataProvider<GroupItem | SessionItem> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private sessions: Promise<SessionMeta[]> | undefined;

  refresh(): void {
    this.sessions = undefined;
    this.changed.fire();
  }

  all(): Promise<SessionMeta[]> {
    this.sessions ??= runCli(['list', '--json', '--limit', '100000']).then(out => JSON.parse(out) as SessionMeta[]);
    return this.sessions;
  }

  async visible(): Promise<SessionMeta[]> {
    const sessions = await this.all();
    return config().get<boolean>('onlyCurrentWorkspace', true) ? sessions.filter(s => inWorkspace(s.cwd)) : sessions;
  }

  getTreeItem(element: GroupItem | SessionItem): vscode.TreeItem {
    return element;
  }

  async getChildren(element?: GroupItem | SessionItem): Promise<(GroupItem | SessionItem)[]> {
    if (element instanceof GroupItem) {
      return element.sessions.map(s => new SessionItem(s));
    }
    if (element) {
      return [];
    }
    const sessions = await this.visible();
    return SOURCES.map(source => new GroupItem(source, sessions.filter(s => s.source === source)));
  }
}

async function pickSession(provider: SessionsProvider, source?: Source): Promise<SessionMeta | undefined> {
  const sessions = (await provider.visible()).filter(s => !source || s.source === source);
  const picked = await vscode.window.showQuickPick(
    sessions.map(s => ({ label: `$(${ICON[s.source]}) ${s.title}`, description: `${LABEL[s.source]} · ${relativeTime(s.updated)}`,
      detail: s.cwd ?? undefined, session: s })),
    { placeHolder: 'Elige la conversación a pasar', matchOnDescription: true, matchOnDetail: true });
  return picked?.session;
}

async function pickTarget(session: SessionMeta): Promise<Target | undefined> {
  const targets: Target[] = ['claude', 'codex', 'copilot', 'md'];
  const picked = await vscode.window.showQuickPick(
    targets.filter(t => t !== session.source || t === 'copilot').map(t => ({ label: LABEL[t], target: t })),
    { placeHolder: `Pasar “${session.title}” a…` });
  return picked?.target;
}

function sessionRef(session: SessionMeta): string {
  // the same Copilot session can exist in several workspaces; the path selects the exact copy
  return session.source === 'copilot' ? session.path : session.id;
}

async function transfer(session: SessionMeta, target: Target, provider: SessionsProvider): Promise<void> {
  const result = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Pasando “${session.title}” a ${LABEL[target]}…`, cancellable: true },
    (_progress, token) => exportSession(session.source, sessionRef(session), target, undefined, token));
  provider.refresh();
  await openResult(result);
  vscode.window.showInformationMessage(`${result.turns} turnos pasaron a ${LABEL[target]}.`);
}

async function findActiveCopilot(wsDir: string, sinceMs: number, token: vscode.CancellationToken): Promise<SessionMeta | null> {
  // VS Code persists the session shortly after the request is sent, so poll briefly.
  for (let attempt = 0; attempt < 20 && !token.isCancellationRequested; attempt++) {
    try {
      return JSON.parse(await runCli(['active-copilot', '--ws-dir', wsDir, '--participant', PARTICIPANT, '--since', String(sinceMs)], token));
    } catch (error) {
      if (!(error instanceof CliError) || error.exitCode !== 2) {
        throw error;
      }
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  return null;
}

const chatHandler: vscode.ChatRequestHandler = async (request, _context, stream, token) => {
  const command = request.command;
  if (command !== 'claude' && command !== 'codex' && command !== 'markdown') {
    stream.markdown('Usa `@bridge /claude`, `@bridge /codex` o `@bridge /markdown` para pasar **esta conversación**.');
    return;
  }
  const target: Target = command === 'markdown' ? 'md' : command;
  if (!extensionContext.storageUri) {
    stream.markdown('Abre una carpeta o un workspace: las conversaciones de Copilot se guardan por workspace.');
    return;
  }
  const wsDir = path.dirname(extensionContext.storageUri.fsPath);
  stream.progress('Buscando esta conversación…');
  const session = await findActiveCopilot(wsDir, Date.now() - 60_000, token);
  if (!session) {
    stream.markdown('No pude identificar esta conversación en el almacenamiento de VS Code. Prueba desde la vista **DAFO Chat Bridge** de la barra lateral.');
    return;
  }
  stream.progress(`Pasando “${session.title}” a ${LABEL[target]}…`);
  const result = await exportSession('copilot', session.path, target, PARTICIPANT, token);
  stream.markdown(`Listo: **${result.turns} turnos** de *${session.title}* pasaron a **${LABEL[target]}**.`);
  stream.button({ command: 'chatBridge.openResult', title: `Abrir en ${LABEL[target]}`, arguments: [result] });
  await openResult(result);
};

export function activate(context: vscode.ExtensionContext): void {
  extensionContext = context;
  const provider = new SessionsProvider();
  const tree = vscode.window.createTreeView('chatBridge.sessions', { treeDataProvider: provider, showCollapseAll: true });

  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.text = '$(arrow-swap) Bridge';
  statusBar.tooltip = 'DAFO Chat Bridge: pasar una conversación a otro agente';
  statusBar.command = 'chatBridge.send';
  statusBar.show();

  const participant = vscode.chat.createChatParticipant(PARTICIPANT, chatHandler);
  participant.iconPath = vscode.Uri.joinPath(context.extensionUri, 'media', 'icon.svg');

  const report = (error: unknown) => vscode.window.showErrorMessage(`DAFO Chat Bridge: ${error instanceof Error ? error.message : String(error)}`);
  const guarded = (fn: (...args: any[]) => Promise<void>) => (...args: any[]) => fn(...args).catch(report);

  context.subscriptions.push(
    tree, statusBar, participant,
    tree.onDidChangeVisibility(e => e.visible && provider.refresh()),
    vscode.window.onDidChangeWindowState(e => e.focused && tree.visible && provider.refresh()),
    vscode.workspace.onDidChangeConfiguration(e => e.affectsConfiguration('chatBridge') && provider.refresh()),
    vscode.commands.registerCommand('chatBridge.refresh', () => provider.refresh()),
    vscode.commands.registerCommand('chatBridge.toggleScope', guarded(async () => {
      await config().update('onlyCurrentWorkspace', !config().get<boolean>('onlyCurrentWorkspace', true), vscode.ConfigurationTarget.Global);
    })),
    vscode.commands.registerCommand('chatBridge.send', guarded(async (item?: SessionItem) => {
      const session = item?.session ?? await pickSession(provider);
      const target = session && await pickTarget(session);
      if (session && target) {
        await transfer(session, target, provider);
      }
    })),
    vscode.commands.registerCommand('chatBridge.sendLatest', guarded(async () => {
      const source = await vscode.window.showQuickPick(SOURCES.map(s => ({ label: LABEL[s], source: s })),
        { placeHolder: '¿De qué agente es la conversación?' });
      if (!source) {
        return;
      }
      const latest = (await provider.visible()).find(s => s.source === source.source);
      if (!latest) {
        vscode.window.showWarningMessage(`No hay conversaciones de ${source.label} en este workspace.`);
        return;
      }
      const target = await pickTarget(latest);
      if (target) {
        await transfer(latest, target, provider);
      }
    })),
    vscode.commands.registerCommand('chatBridge.showMarkdown', guarded(async (item: SessionItem) => {
      const markdown = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: `Leyendo “${item.session.title}”…` },
        () => runCli(['show', item.session.source, sessionRef(item.session), '--tool-output', config().get<string>('toolOutput', '500')]));
      const doc = await vscode.workspace.openTextDocument({ language: 'markdown', content: markdown });
      await vscode.window.showTextDocument(doc, { preview: true });
    })),
    vscode.commands.registerCommand('chatBridge.openResult', guarded((result: ExportResult) => openResult(result))),
  );
}

export function deactivate(): void {}
