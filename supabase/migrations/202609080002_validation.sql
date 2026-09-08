-- Restricted validator for the keywords used by the versioned v1 contracts.
-- Contract tests also validate every fixture with independent AJV.
create function sync_private.matches_schema(v jsonb, s jsonb, root jsonb)
returns boolean language plpgsql immutable set search_path = '' as $$
declare k text; x jsonb; t text; matched integer; n numeric;
begin
  if v is null then return false; end if;
  if s ? '$ref' then
    return sync_private.matches_schema(v, root #> string_to_array(substr(s->>'$ref', 3), '/'), root);
  end if;
  if s ? 'const' and v <> s->'const' then return false; end if;
  if s ? 'enum' and not exists (select 1 from jsonb_array_elements(s->'enum') e where e = v) then return false; end if;
  if s ? 'anyOf' or s ? 'oneOf' then
    select count(*) into matched from jsonb_array_elements(coalesce(s->'anyOf', s->'oneOf')) e
    where sync_private.matches_schema(v, e, root);
    if (s ? 'anyOf' and matched = 0) or (s ? 'oneOf' and matched <> 1) then return false; end if;
  end if;
  t := jsonb_typeof(v);
  if s ? 'type' and s->>'type' <> t and not (s->>'type' = 'integer' and t = 'number') then return false; end if;
  if t = 'object' then
    if s ? 'required' and exists (select 1 from jsonb_array_elements_text(s->'required') r where not v ? r) then return false; end if;
    select count(*) into matched from jsonb_object_keys(v);
    if matched < coalesce((s->>'minProperties')::int, 0) or matched > coalesce((s->>'maxProperties')::int, 100000) then return false; end if;
    for k, x in select * from jsonb_each(v) loop
      if (s->'properties') ? k then
        if not sync_private.matches_schema(x, s->'properties'->k, root) then return false; end if;
      elsif s->'additionalProperties' = 'false'::jsonb then return false;
      elsif jsonb_typeof(s->'additionalProperties') = 'object' then
        if not sync_private.matches_schema(x, s->'additionalProperties', root) then return false; end if;
      end if;
    end loop;
  elsif t = 'array' then
    n := jsonb_array_length(v);
    if n < coalesce((s->>'minItems')::int, 0) or n > coalesce((s->>'maxItems')::int, 100000) then return false; end if;
    if s->>'uniqueItems' = 'true' and n <> (select count(distinct e) from jsonb_array_elements(v) e) then return false; end if;
    if s ? 'items' and exists (select 1 from jsonb_array_elements(v) e where not sync_private.matches_schema(e, s->'items', root)) then return false; end if;
  elsif t = 'string' then
    k := v #>> '{}';
    if char_length(k) < coalesce((s->>'minLength')::int, 0) or char_length(k) > coalesce((s->>'maxLength')::int, 100000) then return false; end if;
    if s ? 'pattern' and k !~ (s->>'pattern') then return false; end if;
    if s->>'format' = 'date' then
      if to_char(k::date, 'YYYY-MM-DD') <> k then return false; end if;
    elsif s->>'format' = 'date-time' then
      if to_char(k::timestamptz at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') <> k then return false; end if;
    end if;
  elsif t = 'number' then
    if v::text !~ '^-?(0|[1-9][0-9]*)$' then return false; end if;
    n := v::text::numeric;
    if n < coalesce((s->>'minimum')::numeric, n) or n > coalesce((s->>'maximum')::numeric, n) then return false; end if;
  end if;
  return true;
exception when invalid_datetime_format or datetime_field_overflow or invalid_text_representation or numeric_value_out_of_range then
  return false;
end $$;

create function sync_private.canonical_json(v jsonb)
returns text language plpgsql immutable strict set search_path = '' as $$
begin
  case jsonb_typeof(v)
    when 'object' then return '{' || coalesce((select string_agg(to_jsonb(key)::text || ':' || sync_private.canonical_json(value), ',' order by convert_to(key, 'UTF8')) from jsonb_each(v)), '') || '}';
    when 'array' then return '[' || coalesce((select string_agg(sync_private.canonical_json(value), ',' order by ord) from jsonb_array_elements(v) with ordinality e(value,ord)), '') || ']';
    else return v::text;
  end case;
end $$;

create function sync_private.hash_json(v jsonb)
returns text language sql immutable strict set search_path = '' as $$
  select 'sha256:' || encode(sha256(convert_to(sync_private.canonical_json(v), 'UTF8')), 'hex')
$$;

create function sync_private.normalize_state(v jsonb)
returns jsonb language plpgsql immutable strict set search_path = '' as $$
declare collection text; items jsonb; e jsonb; normalized jsonb := v;
begin
  foreach collection in array array['tasks','occurrences','scheduledTasks','externalEvents','focusSessions'] loop
    items := '[]';
    for e in select value from jsonb_array_elements(v->collection) order by value->>'id' collate "C" loop
      if e ? 'repeatWeekdays' then
        e := jsonb_set(e, '{repeatWeekdays}', (select coalesce(jsonb_agg(w order by w::int), '[]') from jsonb_array_elements(e->'repeatWeekdays') w));
      end if;
      if jsonb_typeof(e->'overrides') = 'object' then
        e := jsonb_set(e, '{overrides,repeatWeekdays}', (select coalesce(jsonb_agg(w order by w::int), '[]') from jsonb_array_elements(e#>'{overrides,repeatWeekdays}') w));
      end if;
      if e ? 'segments' then
        e := jsonb_set(e, '{segments}', (select coalesce(jsonb_agg(s order by (s->>'startSlot')::int, (s->>'endSlot')::int, s->>'id' collate "C"), '[]') from jsonb_array_elements(e->'segments') s));
      end if;
      items := items || jsonb_build_array(e);
    end loop;
    normalized := jsonb_set(normalized, array[collection], items);
  end loop;
  return normalized;
end $$;

create function sync_private.validate_state(v jsonb)
returns jsonb language plpgsql stable set search_path = '' as $$
declare contract jsonb; collection text; e jsonb; fields jsonb; active_ids jsonb; total integer := 0;
begin
  if v is null or octet_length(v::text) > 4194304 then raise exception 'payloadInvalid'; end if;
  select schema into contract from sync_private.contracts where name = 'cloud-state';
  if not sync_private.matches_schema(v, contract, contract) then raise exception 'payloadInvalid'; end if;
  foreach collection in array array['tasks','occurrences','scheduledTasks','externalEvents','focusSessions'] loop
    total := total + jsonb_array_length(v->collection);
    if (select count(distinct x->>'id') from jsonb_array_elements(v->collection) x) <> jsonb_array_length(v->collection) then raise exception 'payloadInvalid'; end if;
  end loop;
  if total > 20000 then raise exception 'payloadInvalid'; end if;
  for fields in select x from jsonb_array_elements(v->'tasks') x union all select x->'overrides' from jsonb_array_elements(v->'occurrences') x where jsonb_typeof(x->'overrides') = 'object' loop
    if (fields->>'repeatRule' = 'weekly') <> (jsonb_array_length(fields->'repeatWeekdays') > 0) then raise exception 'payloadInvalid'; end if;
  end loop;
  if exists (select 1 from jsonb_array_elements(v->'occurrences') o where not exists (select 1 from jsonb_array_elements(v->'tasks') t where t->>'id'=o->>'taskID')) then raise exception 'payloadInvalid'; end if;
  if exists (select 1 from jsonb_array_elements(v->'scheduledTasks') s where not exists (select 1 from jsonb_array_elements(v->'occurrences') o where o->>'id'=s->>'taskOccurrenceID')) then raise exception 'payloadInvalid'; end if;
  if (select count(distinct s->>'taskOccurrenceID') from jsonb_array_elements(v->'scheduledTasks') s) <> jsonb_array_length(v->'scheduledTasks') then raise exception 'payloadInvalid'; end if;
  foreach collection in array array['scheduledTasks','externalEvents'] loop
    if (select count(*) from jsonb_array_elements(v->collection) parent, jsonb_array_elements(parent->'segments') seg) <>
      (select count(distinct seg->>'id') from jsonb_array_elements(v->collection) parent, jsonb_array_elements(parent->'segments') seg) then raise exception 'payloadInvalid'; end if;
    for e in select value from jsonb_array_elements(v->collection) loop
      if collection = 'externalEvents' and (e->>'sourceStartSlot')::int >= (e->>'sourceEndSlot')::int then raise exception 'payloadInvalid'; end if;
      if (select count(distinct s->>'id') from jsonb_array_elements(e->'segments') s) <> jsonb_array_length(e->'segments') then raise exception 'payloadInvalid'; end if;
      if exists (select 1 from jsonb_array_elements(e->'segments') s where s->>(case when collection='scheduledTasks' then 'scheduledTaskID' else 'externalEventID' end) <> e->>'id'
        or (s->>'endSlot')::int <= (s->>'startSlot')::int or (s->>'endSlot')::int - (s->>'startSlot')::int > 96) then raise exception 'payloadInvalid'; end if;
      if exists (select 1 from jsonb_array_elements(e->'segments') a, jsonb_array_elements(e->'segments') b
        where a->>'id' < b->>'id' and (a->>'startSlot')::int < (b->>'endSlot')::int and (b->>'startSlot')::int < (a->>'endSlot')::int) then raise exception 'payloadInvalid'; end if;
    end loop;
  end loop;
  select coalesce(jsonb_agg(s->'id'), '[]') into active_ids from jsonb_array_elements(v->'focusSessions') s where s->>'status' in ('running','paused');
  if jsonb_array_length(active_ids) > 1 or (jsonb_array_length(active_ids) = 0 and v->'activeFocusSessionID' <> 'null') or (jsonb_array_length(active_ids)=1 and v->'activeFocusSessionID' <> active_ids->0) then raise exception 'payloadInvalid'; end if;
  return sync_private.normalize_state(v);
end $$;

create function sync_private.read_set(v jsonb, selectors jsonb)
returns jsonb language plpgsql immutable set search_path = '' as $$
declare selector jsonb; e jsonb; result jsonb := '{}'; collection text;
begin
  for selector in select value from jsonb_array_elements(selectors) loop
    collection := selector->>'collection';
    if collection = 'activeFocusSessionID' then
      result := result || jsonb_build_object(collection, v->collection);
    else
      -- Include an empty marker: adding the first entity changes the fingerprint.
      result := result || jsonb_build_object(sync_private.canonical_json(selector), '[]'::jsonb);
      for e in select value from jsonb_array_elements(v->collection) where
        (not selector ? 'ids' or selector->'ids' ? (value->>'id')) and
        (not selector ? 'dates' or selector->'dates' ? coalesce(value->>'date', value->>'occurrenceDate')) and
        (not selector ? 'taskIDs' or selector->'taskIDs' ? (value->>'taskID'))
      loop result := result || jsonb_build_object(collection || '/' || (e->>'id'), e); end loop;
    end if;
  end loop;
  return result;
end $$;

create function sync_private.validate_operation(op jsonb, current_state jsonb, candidate jsonb)
returns void language plpgsql stable set search_path = '' as $$
declare contract jsonb; k text; expected jsonb; actual jsonb; collection text; entity_id text; state_entities jsonb := '{}';
begin
  select schema into contract from sync_private.contracts where name='operation';
  if op is null or octet_length(op::text)>1048576 or not sync_private.matches_schema(op, contract, contract) then raise exception 'payloadInvalid'; end if;
  if op->>'clientResultStateHash' <> sync_private.hash_json(candidate) then raise exception 'stateHashMismatch'; end if;
  -- A caller cannot omit the before fingerprint for an entity it changes.
  -- Domain replay remains client-owned; this validates the complete write set.
  foreach collection in array array['tasks','occurrences','scheduledTasks','externalEvents','focusSessions'] loop
    state_entities := state_entities || (select coalesce(jsonb_object_agg(collection || '/' || (value->>'id'),value),'{}') from jsonb_array_elements(current_state->collection));
    for entity_id in select coalesce(a.entity->>'id',b.entity->>'id')
      from jsonb_array_elements(current_state->collection) a(entity)
      full join jsonb_array_elements(candidate->collection) b(entity) on a.entity->>'id'=b.entity->>'id'
      where a.entity is distinct from b.entity
    loop
      k := collection || '/' || entity_id;
      if not (op#>'{preconditions,entityFingerprints}') ? k then raise exception 'preconditionRequired'; end if;
    end loop;
  end loop;
  for k, expected in select * from jsonb_each(op#>'{preconditions,entityFingerprints}') loop
    collection := split_part(k, '/', 1);
    if collection not in ('tasks','occurrences','scheduledTasks','externalEvents','focusSessions') or k !~ '^[a-zA-Z]+/[0-9a-f-]{36}$' then raise exception 'payloadInvalid'; end if;
    actual := state_entities->k;
    if coalesce(to_jsonb(sync_private.hash_json(actual)), 'null') <> expected then raise exception 'preconditionFailed'; end if;
  end loop;
  if sync_private.hash_json(sync_private.read_set(current_state, op#>'{preconditions,readSet}')) <> op#>>'{preconditions,readSetFingerprint}' then raise exception 'preconditionFailed'; end if;
end $$;

revoke all on all functions in schema sync_private from public, anon, authenticated;
