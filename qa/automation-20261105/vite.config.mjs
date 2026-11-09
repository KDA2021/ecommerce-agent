import base from '../../frontend/vite.config.ts';
import {fileURLToPath} from 'node:url';
export default {
  ...base,
  root:fileURLToPath(new URL('../../frontend',import.meta.url)),
  server:{host:'127.0.0.1',port:5180,strictPort:true,
    proxy:{'/api':{target:'http://127.0.0.1:8009',changeOrigin:true,rewrite:p=>p.replace(/^\/api/,'')}}},
};
