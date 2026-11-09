import base from '../automation-20261104/playwright.config.mjs';
import {fileURLToPath} from 'node:url';
export default {...base,
  testDir:fileURLToPath(new URL('../automation-20261104/web',import.meta.url)),
  outputDir:'./artifacts/mock-browser-results',
  reporter:[['list'],['json',{outputFile:'artifacts/mock-browser.json'}],['html',{outputFolder:'artifacts/mock-browser-report',open:'never'}]],
};
