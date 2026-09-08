import { pathToFileURL } from 'node:url';

// The worker keeps failed jobs pending; its logs never contain requests, receipts,
// credentials, user IDs, response bodies, or task data.
export async function processDeletions({ rpc, deleteUser }) {
  const jobs = await rpc('pending_account_deletions', {});
  const result = { completed:0, pending:0 };
  for (const job of jobs) {
    try {
      const prepared = await rpc('prepare_account_deletion', { p_request_id:job.requestID });
      if (prepared.status !== 'completed') {
        await deleteUser(prepared.userID);
        await rpc('complete_account_deletion', { p_request_id:job.requestID });
      }
      result.completed++;
    } catch { result.pending++; }
  }
  return result;
}

export function transport(url, key, fetcher = fetch) {
  if (!url || !key) throw new Error('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required');
  const parsed = new URL(url);
  if (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && ['127.0.0.1','localhost'].includes(parsed.hostname))) {
    throw new Error('Use HTTPS except for local Supabase');
  }
  async function request(path, method, body) {
    const response = await fetcher(new URL(path, parsed), {
      method, headers:{ apikey:key, Authorization:'Bearer '+key, 'Content-Type':'application/json' },
      body:body === undefined ? undefined : JSON.stringify(body), signal:AbortSignal.timeout(15000),
    });
    if (!response.ok) {
      // A missing Auth user is expected after an earlier successful delete whose
      // response was lost. Database completion still independently checks absence.
      if (method==='DELETE' && response.status===404) return;
      throw new Error('Service request failed: '+response.status);
    }
    return response.status===204 ? undefined : response.json();
  }
  return {
    rpc:(name,body)=>request('/rest/v1/rpc/'+name,'POST',body),
    deleteUser:id=>request('/auth/v1/admin/users/'+encodeURIComponent(id),'DELETE'),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const client = transport(process.env.SUPABASE_URL,process.env.SUPABASE_SERVICE_ROLE_KEY);
  const result = await processDeletions(client);
  process.stdout.write(JSON.stringify(result)+'\n');
  if (result.pending) process.exitCode=1;
}
