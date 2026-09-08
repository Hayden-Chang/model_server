import { createHash, randomUUID } from 'node:crypto';
export const empty = () => ({
  schemaVersion:1, tasks:[], occurrences:[], scheduledTasks:[], externalEvents:[], focusSessions:[], activeFocusSessionID:null,
});
export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value !== null && typeof value === 'object') return '{' + Object.keys(value).sort((a,b)=>Buffer.compare(Buffer.from(a),Buffer.from(b)))
    .map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
export const hash = value => 'sha256:' + createHash('sha256').update(canonical(value)).digest('hex');
export function withTask(title = 'Read') {
  const state = empty();
  const time = '2026-09-08T10:00:00.000Z';
  state.tasks.push({ id:randomUUID(), title, note:'', estimatedDurationSlots:2, repeatRule:'none', repeatForWeeks:2,
    repeatWeekdays:[], isPinned:false, colorHex:'#AABBCC', createdAt:time, updatedAt:time });
  state.occurrences.push({ id:randomUUID(), taskID:state.tasks[0].id, occurrenceDate:'2026-09-08', unscheduledDate:null,
    isAllDay:false, continuationSourceOccurrenceID:null, orderKey:'0:0000000000000000:'+randomUUID(),
    status:'todo', completedAt:null, overrides:null });
  return state;
}
export function operation(device, cloud, result, overrides = {}) {
  const fingerprints={};
  for(const collection of ['tasks','occurrences','scheduledTasks','externalEvents','focusSessions']) {
    const previous=cloud.state?.[collection] ?? [];
    for(const entity of [...previous,...result[collection]]) {
      const old=previous.find(x=>x.id===entity.id);
      fingerprints[collection+'/'+entity.id]=old?hash(old):null;
    }
  }
  return {
    operationID:randomUUID(), deviceID:device.id, syncSpaceID:cloud.syncSpaceID, generation:cloud.generation,
    baseRevision:cloud.revision, baseStateHash:cloud.stateHash, schemaVersion:1, algorithmVersion:'schedule-v1',
    kind:'task.setCompletion', effectiveAt:'2026-09-08T10:00:00.000Z',
    preconditions:{ entityFingerprints:fingerprints, readSet:[], readSetFingerprint:hash({}) },
    payload:{ occurrenceID:result.occurrences[0]?.id ?? randomUUID(),targetStatus:'completed',completedAt:'2026-09-08T10:00:00.000Z' },
    clientResultStateHash:hash(result), ...overrides,
  };
}
