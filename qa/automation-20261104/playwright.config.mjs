import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const require = createRequire(import.meta.url);
const { defineConfig } = require(process.env.QA_PLAYWRIGHT_TEST || 'playwright/test');
const root = path.dirname(fileURLToPath(import.meta.url));
export default defineConfig({
  testDir: './web',
  workers: 1,
  retries: 0,
  timeout: 20000,
  globalTimeout: 180000,
  forbidOnly: true,
  outputDir: './artifacts/web-results',
  reporter: [['list'], ['json', {outputFile: 'artifacts/web-results.json'}], ['html', {outputFolder: 'artifacts/web-report', open: 'never'}]],
  use: {
    baseURL: 'http://127.0.0.1:5179',
    browserName: 'chromium', channel: 'chrome', headless: true,
    trace: 'retain-on-failure', screenshot: 'only-on-failure',
    serviceWorkers: 'block',
  },
  webServer: {
    cwd: path.resolve(root, '../../frontend'),
    command: `"${process.execPath}" node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5179 --strictPort`,
    url: 'http://127.0.0.1:5179', reuseExistingServer: false, timeout: 30000,
  },
});
