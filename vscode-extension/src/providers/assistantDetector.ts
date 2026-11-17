/** Which AI assistants are installed in this VS Code, and what PatternVault can honestly capture from each. */
import type { ProviderInfo } from '../services/backendClient';

export interface AssistantStatus {
  key: string;
  name: string;
  installed: boolean;
  logAvailable: boolean;
  capture: 'automatic-import' | 'workspace-and-manual';
  summary: string;
}

export function detectAssistants(installedExtensionIds: string[], providers: ProviderInfo[]): AssistantStatus[] {
  const installed = new Set(installedExtensionIds.map((i) => i.toLowerCase()));
  return providers
    .filter((p) => p.key !== 'manual')
    .map((p) => {
      const caps = p.capabilities;
      const auto = caps.captures_prompts || caps.captures_responses;
      return {
        key: p.key,
        name: p.display_name,
        installed: p.vscode_extension_ids.some((id) => installed.has(id.toLowerCase())),
        logAvailable: p.available,
        capture: auto ? ('automatic-import' as const) : ('workspace-and-manual' as const),
        summary: auto
          ? `${caps.mechanism}${caps.verified_against_real_install ? '' : ' (unverified against a real install)'}`
          : `no capture API — ${caps.mechanism}`,
      };
    });
}
