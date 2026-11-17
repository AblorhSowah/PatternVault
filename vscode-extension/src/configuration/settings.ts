/** Typed access to `patternvault.*` settings. Pure: takes a reader so it can be tested without `vscode`. */

export interface ConfigReader {
  get<T>(key: string, defaultValue: T): T;
}

export interface PatternVaultSettings {
  autoStart: boolean;
  pythonPath: string;
  home: string;
  startupTimeoutMs: number;
}

export function readSettings(cfg: ConfigReader): PatternVaultSettings {
  const timeout = Number(cfg.get<number>('backend.startupTimeoutMs', 30000));
  return {
    autoStart: Boolean(cfg.get<boolean>('backend.autoStart', true)),
    pythonPath: String(cfg.get<string>('backend.pythonPath', '')),
    home: String(cfg.get<string>('backend.home', '')),
    startupTimeoutMs: Number.isFinite(timeout) && timeout >= 1000 ? timeout : 30000,
  };
}
