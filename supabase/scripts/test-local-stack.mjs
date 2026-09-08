import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root=fileURLToPath(new URL('../../',import.meta.url));
function cli(args) {
  const result=spawnSync('supabase',args,{cwd:root,encoding:'utf8',timeout:600000,maxBuffer:16*1024*1024});
  if(result.status!==0) {
    const diagnostic=(result.stderr ?? '').replace(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g,'[redacted]')
      .replace(/sb_(secret|publishable)_[A-Za-z0-9_-]+/g,'[redacted]');
    throw new Error('Local Supabase command failed: '+args[0]+'\n'+diagnostic);
  }
  return result.stdout;
}
try {
  cli(['start','--exclude','studio,imgproxy,edge-runtime,logflare,vector,supavisor,storage-api']);
  const status=JSON.parse(cli(['status','--output','json']));
  const tests=spawnSync(process.execPath,['--test','tests/hosted.test.mjs'],{
    cwd:fileURLToPath(new URL('../',import.meta.url)),stdio:'inherit',
    env:{...process.env,SYNC_TEST_ALLOW_WRITE:'1',SUPABASE_URL:status.API_URL,
      SUPABASE_PUBLISHABLE_KEY:status.ANON_KEY,SUPABASE_SERVICE_ROLE_KEY:status.SERVICE_ROLE_KEY},
  });
  process.exitCode=tests.status ?? 1;
} finally {
  spawnSync('supabase',['stop','--no-backup','--project-id','daymosaic-account-sync'],{cwd:root,stdio:'ignore',timeout:60000});
}
