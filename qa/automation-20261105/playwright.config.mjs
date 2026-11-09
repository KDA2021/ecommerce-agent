import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const require=createRequire(import.meta.url);
const {defineConfig}=require(process.env.QA_PLAYWRIGHT_TEST || 'playwright/test');
const folder=path.dirname(fileURLToPath(import.meta.url));
const project=path.resolve(folder,'../..');
const python=process.env.QA_PYTHON || path.join(project,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
export default defineConfig({
  maxFailures: 1,
  testDir:'./web',workers:1,retries:0,forbidOnly:true,timeout:35000,globalTimeout:240000,
  outputDir:'./artifacts/browser-results',
  reporter:[['list'],['json',{outputFile:'artifacts/browser.json'}],['html',{outputFolder:'artifacts/browser-report',open:'never'}]],
  use:{baseURL:'http://127.0.0.1:5180',browserName:'chromium',channel:process.env.QA_CHROME_CHANNEL || 'chrome',
       trace:'retain-on-failure',screenshot:'only-on-failure',serviceWorkers:'block'},
  webServer:[
    {command:`"${python}" qa/automation-20261105/qa_harness.py 8009`,cwd:project,
     env:{PYTHONPATH:project,LLM_API_KEY:'',OPENAI_API_KEY:''},url:'http://127.0.0.1:8009/health',reuseExistingServer:false,timeout:60000},
    {command:`"${process.execPath}" frontend/node_modules/vite/bin/vite.js --config qa/automation-20261105/vite.config.mjs`,
     cwd:project,url:'http://127.0.0.1:5180',reuseExistingServer:false,timeout:30000},
  ],
});
