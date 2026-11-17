/** Pure presentation logic for the status bar / status tree. */
import type { BackendStatus } from './backendProcess';
import type { Dashboard, Mode } from './backendClient';

export interface StatusBarModel {
  text: string;
  tooltip: string;
  command: string;
  warning: boolean;
}

const MODE_LABEL: Record<Mode, string> = { learning: 'Learning', offline: 'Offline', hybrid: 'Hybrid' };

export function modeLabel(mode: Mode): string {
  return MODE_LABEL[mode];
}

export function statusBarModel(status: BackendStatus, dash: Dashboard | undefined, error?: string): StatusBarModel {
  switch (status) {
    case 'running': {
      const mode = dash ? modeLabel(dash.mode) : '…';
      const verified = dash ? dash.counts.verified_patterns : 0;
      const patterns = dash ? dash.counts.patterns : 0;
      return {
        text: `$(lock) PatternVault: ${mode}`,
        tooltip: `Backend running · mode ${mode} · ${patterns} patterns (${verified} verified)\nClick to switch mode`,
        command: 'patternvault.switchMode',
        warning: false,
      };
    }
    case 'starting':
      return { text: '$(sync~spin) PatternVault: starting', tooltip: 'Starting local backend…', command: 'patternvault.showLog', warning: false };
    case 'failed':
      return {
        text: '$(error) PatternVault: failed',
        tooltip: `Backend failed: ${error ?? 'unknown error'}\nClick to view the log`,
        command: 'patternvault.showLog',
        warning: true,
      };
    default:
      return { text: '$(circle-slash) PatternVault: stopped', tooltip: 'Backend is not running. Click to start.', command: 'patternvault.startBackend', warning: false };
  }
}
