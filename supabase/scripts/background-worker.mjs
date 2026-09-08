import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { processDeletions, transport } from './deletion-worker.mjs';

export async function runWorker(kind, client) {
  if (kind === 'deletion') {
    const result = await processDeletions(client);
    return { kind, ok:result.pending === 0, ...result };
  }
  if (kind === 'maintenance') {
    const result = await client.rpc('maintain_sync_batch', { p_limit:100 });
    return { kind, ok:result.accountsFailed === 0, ...result };
  }
  throw new Error('Unknown worker kind');
}

export async function main(kind, env=process.env, output=process.stdout) {
  try {
    if (!['deletion','maintenance'].includes(kind)) throw new Error('Unknown worker kind');
    const credentials = env.CREDENTIALS_DIRECTORY
      ? JSON.parse(await readFile(join(env.CREDENTIALS_DIRECTORY,'supabase.json'),'utf8'))
      : { url:env.SUPABASE_URL, key:env.SUPABASE_SERVICE_ROLE_KEY };
    const result = await runWorker(kind, transport(credentials.url,credentials.key));
    output.write(JSON.stringify(result)+'\n');
    return result.ok ? 0 : 1;
  } catch {
    // Even transport/credential errors may contain secrets; emit only this code.
    output.write(JSON.stringify({ ok:false, error:'workerFailed' })+'\n');
    return 1;
  }
}

if (import.meta.main) {
  process.exitCode = await main(process.argv[2]);
}
