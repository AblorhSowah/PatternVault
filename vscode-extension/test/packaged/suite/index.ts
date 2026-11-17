import Mocha from 'mocha';
import * as fs from 'node:fs';
import * as path from 'node:path';

export function run(): Promise<void> {
  const mocha = new Mocha({ ui: 'bdd', color: true, timeout: 900000 });
  for (const f of fs.readdirSync(__dirname)) if (f.endsWith('.test.js')) mocha.addFile(path.join(__dirname, f));
  return new Promise((resolve, reject) => mocha.run((failures) => (failures ? reject(new Error(`${failures} test(s) failed`)) : resolve())));
}
