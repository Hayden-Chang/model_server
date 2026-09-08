import EmbeddedPostgres from 'embedded-postgres';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'node:net';
import { randomUUID } from 'node:crypto';

export async function database() {
  const directory = await mkdtemp(join(tmpdir(), 'daymosaic-sync-test-'));
  const socket = createServer();
  await new Promise((resolve, reject) => { socket.once('error', reject); socket.listen(0, '127.0.0.1', resolve); });
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const pg = new EmbeddedPostgres({
    databaseDir: join(directory, 'db'), port, user: 'postgres', password: randomUUID(),
    persistent: true, initdbFlags: ['--encoding=UTF8', '--locale=C'],
    postgresFlags: ['-h', '127.0.0.1', '-k', directory],
    onLog: () => {}, onError: message => { if (String(message).includes('FATAL')) process.stderr.write(String(message)); },
  });
  let admin;
  try {
    await pg.initialise();
    await pg.start();
    admin = pg.getPgClient();
    await admin.connect();
    await admin.query(`
      create role anon; create role authenticated; create role service_role bypassrls;
      create schema auth;
      create table auth.users(id uuid primary key, email_confirmed_at timestamptz, is_anonymous boolean default false);
      create table auth.sessions(id uuid primary key,user_id uuid references auth.users(id) on delete cascade);
      create function auth.jwt() returns jsonb language sql stable as
        $$ select coalesce(nullif(current_setting('request.jwt.claims',true),''),'{}')::jsonb $$;
      create function auth.uid() returns uuid language sql stable as
        $$ select (auth.jwt()->>'sub')::uuid $$;
      grant usage on schema public,auth to anon,authenticated,service_role;
      grant execute on all functions in schema auth to anon,authenticated,service_role;
    `);
    for (const migration of (await readdir(new URL('../migrations/', import.meta.url))).filter(x => x.endsWith('.sql')).sort()) {
      await admin.query(await readFile(new URL('../migrations/' + migration, import.meta.url), 'utf8'));
    }
  } catch (error) {
    if (admin) await admin.end();
    await pg.stop().catch(() => {});
    throw error;
  }
  async function account() {
    const id = randomUUID();
    await admin.query('insert into auth.users values($1,now(),false)', [id]);
    return { id, async device() {
      const session = randomUUID();
      await admin.query('insert into auth.sessions values($1,$2)', [session, id]);
      return { user: id, session, id: randomUUID() };
    } };
  }
  async function call(device, sql, values = [], role = 'authenticated') {
    const client = pg.getPgClient();
    await client.connect();
    try {
      await client.query('begin');
      await client.query('set local role ' + role);
      await client.query("select set_config('request.jwt.claims',$1,true)", [JSON.stringify({
        sub: device?.user, session_id: device?.session, amr: [{ method: 'otp', timestamp: Math.floor(Date.now()/1000) }],
      })]);
      const result = await client.query(sql, values);
      await client.query('commit');
      return result.rows;
    } catch (error) {
      await client.query('rollback');
      throw error;
    } finally { await client.end(); }
  }
  async function rpc(device, name, values = [], role) {
    const rows = await call(device, 'select public.' + name + '(' + values.map((_,i)=>'$'+(i+1)).join(',') + ') as value', values, role);
    return rows[0].value;
  }
  return { admin, account, call, rpc, async close() { await admin.end(); await pg.stop(); await rm(directory, { recursive:true, force:true }); } };
}
