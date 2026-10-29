import * as z from 'zod/v4';

export const VERSION = '0.2.2';
export const SCHEMA_VERSION = 1;
export const id = z.string().regex(/^[a-z0-9][a-z0-9_-]{0,95}$/);
const text = z.string().max(12000);
const lines = z.array(text).max(200).default([]);
export const taskSchema = z.object({
  project_id: id,
  title: z.string().min(1).max(300),
  goal: z.string().min(1).max(12000),
  current_stage: id.optional(),
  plan: z.array(z.object({
    id,
    title: z.string().min(1).max(1000),
    done_criterion: text,
    status: z.enum(['pending','in_progress','blocked','completed','cancelled']),
    verification: text.default('')
  }).strict()).max(50).default([]),
  session_status: z.enum(['open', 'closed']).default('closed'),
  task_status: z.enum(['in_progress', 'waiting_for_user', 'blocked', 'completed', 'cancelled']),
  done_criteria: lines,
  constraints: lines,
  completed: lines,
  claims: z.array(z.object({
    id, text,
    evidence_status: z.enum(['confirmed', 'partial', 'hypothesis', 'refuted', 'needs_recheck']),
    confidence: z.enum(['high', 'medium', 'low']).optional(),
    reason: text,
    supporting_evidence: lines,
    counter_evidence: lines,
    next_check: text.default('')
  }).refine(c => c.evidence_status !== 'confirmed' || c.supporting_evidence.length > 0,
    'Confirmed claims require evidence')).max(200).default([]),
  decisions: z.array(z.object({
    id, text,
    status: z.enum(['proposed', 'accepted', 'rejected', 'superseded']),
    rationale: text,
    alternatives: lines,
    evidence: lines
  })).max(200).default([]),
  verification: z.array(z.object({
    check: text, result: text,
    status: z.enum(['passed', 'failed', 'not_run']),
    evidence: text
  })).max(200).default([]),
  unfinished: lines,
  next_steps: lines,
  source_session: z.object({provider: text, session_id: text}).optional(),
  result_records: z.array(id).max(200).default([])
}).strict().superRefine((t, ctx) => {
  if (t.task_status === 'completed' && (!t.done_criteria.length || !t.verification.some(v => v.status === 'passed'))) {
    ctx.addIssue({code: 'custom', message: 'Completion requires criteria and at least one recorded successful verification'});
  }
  if(t.current_stage && !t.plan.some(p=>p.id===t.current_stage))ctx.addIssue({code:'custom',message:'Current stage must exist in the plan'});
  if(new Set(t.plan.map(p=>p.id)).size!==t.plan.length)ctx.addIssue({code:'custom',message:'Stage IDs must be unique'});
  if(t.task_status==='completed'&&t.plan.some(p=>!['completed','cancelled'].includes(p.status)))ctx.addIssue({code:'custom',message:'An unfinished stage prevents task completion'});
});
export const kindSchema = z.enum(['project', 'task', 'result', 'skill', 'mcp', 'profile', 'credentials']);
export function validatePayload(kind, data) {
  kindSchema.parse(kind);
  if (kind === 'task') return taskSchema.parse(data);
  if (kind === 'project') return z.object({name: z.string().min(1).max(300), description: text.default('')}).strict().parse(data);
  if (data === null || typeof data !== 'object' || Array.isArray(data)) throw new Error('Payload must be an object');
  return structuredClone(data);
}
